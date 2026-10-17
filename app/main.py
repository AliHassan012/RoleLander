from __future__ import annotations

import secrets
import uuid
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.exceptions import RequestValidationError

from app.config import Settings
from app.db import connect, initialize
from app.sessions import COOKIE_NAME, create_session, current_session, hash_cookie, require_csrf


def _error(status: int, code: str, message: str, field: str | None = None):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "field": field, "retryable": status >= 500}},
        headers={"Cache-Control": "no-store"},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize(app.state.db_path)
        from app.runner import recover_runs

        app.state.run_lock = asyncio.Lock()
        app.state.run_tasks = set()
        for run_id in recover_runs(app.state.db_path):
            task = asyncio.create_task(app.state.run_segment(run_id))
            app.state.run_tasks.add(task)
            task.add_done_callback(app.state.run_tasks.discard)
        yield
        for task in list(app.state.run_tasks):
            task.cancel()

    app = FastAPI(title="RoleLander", lifespan=lifespan)
    app.state.settings = settings
    app.state.db_path = settings.data_dir / "rolelander.sqlite3"
    from app.runner import process_run

    async def run_segment(run_id: str):
        await process_run(app, run_id)

    app.state.run_lock = asyncio.Lock()
    app.state.run_tasks = set()
    app.state.run_segment = run_segment
    # This key protects only short-lived Authlib OAuth state. Opaque login sessions
    # are independently stored in SQLite. A restart invalidates in-flight sign-ins.
    app.add_middleware(SessionMiddleware, secret_key=secrets.token_urlsafe(48), same_site="lax", https_only=settings.cookie_secure)

    @app.middleware("http")
    async def no_store_private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        codes = {401:"AUTH_REQUIRED",403:"REQUEST_FORBIDDEN",404:"NOT_FOUND",409:"STATE_CONFLICT",413:"INPUT_TOO_LARGE",415:"UNSUPPORTED_MEDIA_TYPE",422:"INVALID_INPUT",429:"RATE_LIMITED",503:"SERVICE_UNAVAILABLE"}
        return _error(exc.status_code, codes.get(exc.status_code,"REQUEST_FAILED"), detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return _error(422,"VALIDATION_ERROR","One or more request fields are invalid.")

    oauth = OAuth()
    if settings.google_oidc_configured:
        oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": settings.build_id}

    @app.get("/api/capabilities")
    async def capabilities(request: Request):
        current_session(request)
        return {
            "providers": {
                "gemini": bool(settings.gemini_api_key),
                "tavily": bool(settings.tavily_api_key),
            },
            "google_oidc": settings.google_oidc_configured,
            "personal_data_mode": settings.personal_data_mode,
            "free_only": settings.free_only,
            "storage_persistent": settings.storage_persistent,
            "country_support": "ISO-3166-1 alpha-2",
            "location_support": True,
        }

    @app.get("/api/me")
    async def me(request: Request):
        row = current_session(request)
        return {"user": {"id": row["user_id"], "email": row["verified_email"]}, "csrf_token": row["csrf_token"]}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        row = current_session(request)
        require_csrf(request, row)
        cookie_value = request.cookies[COOKIE_NAME]
        with connect(request.app.state.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE cookie_hash=?", (hash_cookie(cookie_value),))
        response.delete_cookie(COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        return {"status": "signed_out"}

    @app.get("/auth/google/start")
    async def google_start(request: Request):
        if not settings.google_oidc_configured:
            return _error(503, "OAUTH_NOT_CONFIGURED", "Google sign-in is not configured.")
        request.session.clear()
        redirect_uri = settings.google_redirect_uri
        response = await oauth.google.authorize_redirect(
            request,
            redirect_uri,
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/auth/google/callback")
    async def google_callback(request: Request):
        if not settings.google_oidc_configured:
            return _error(503, "OAUTH_NOT_CONFIGURED", "Google sign-in is not configured.")
        try:
            token = await oauth.google.authorize_access_token(request)
            claims = token.get("userinfo")
            if claims is None:
                nonce = request.session.get("_authlib_nonce_google")
                claims = await oauth.google.parse_id_token(request, token, nonce=nonce)
            issuer = claims.get("iss")
            subject = claims.get("sub")
            email = claims.get("email")
            if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
                raise ValueError("Unexpected identity issuer")
            if not subject or not email or claims.get("email_verified") is not True:
                raise ValueError("Google account email is not verified")
        except Exception:
            request.session.clear()
            return _error(401, "OAUTH_CALLBACK_INVALID", "Google sign-in could not be verified.")

        user_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with connect(request.app.state.db_path) as conn:
            conn.execute(
                """INSERT INTO users(id, google_issuer, google_subject, verified_email, created_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(google_issuer, google_subject)
                   DO UPDATE SET verified_email=excluded.verified_email""",
                (user_id, issuer, subject, email, now),
            )
            user = conn.execute(
                "SELECT id FROM users WHERE google_issuer=? AND google_subject=?", (issuer, subject)
            ).fetchone()
        cookie_value, _csrf = create_session(request.app.state.db_path, user["id"])
        request.session.clear()
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            cookie_value,
            max_age=14 * 24 * 60 * 60,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/auth/google/gmail/callback")
    async def gmail_callback_not_implemented():
        return _error(503, "GMAIL_NOT_CONFIGURED", "Gmail access is a separate consent flow and is not available yet.")

    @app.get("/")
    async def index():
        return FileResponse("app/static/index.html", headers={"Cache-Control": "no-store"})

    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    from app.profile_routes import router as profile_router
    from app.run_routes import router as run_router
    from app.application_routes import router as application_router
    from app.company_routes import router as company_router

    app.include_router(profile_router)
    app.include_router(run_router)
    app.include_router(application_router)
    app.include_router(company_router)
    return app


