from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_local_env(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE development entries without overriding host env."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", text)
        if not match:
            continue
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    gemini_model: str
    tavily_api_key: str | None
    data_dir: Path
    port: int
    cookie_secure: bool
    app_origin: str
    free_only: bool
    storage_persistent: bool
    google_client_id: str | None
    google_client_secret: str | None
    google_redirect_uri: str
    google_gmail_redirect_uri: str
    credential_encryption_key: str | None
    personal_data_mode: str
    scheduler_enabled: bool
    scheduler_tick_seconds: int
    build_id: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_local_env()
        port = int(os.getenv("PORT", "8000"))
        tick = int(os.getenv("SCHEDULER_TICK_SECONDS", "60"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if tick < 1:
            raise ValueError("SCHEDULER_TICK_SECONDS must be positive")
        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
            data_dir=Path(os.getenv("DATA_DIR", "./data")).expanduser(),
            port=port,
            cookie_secure=_bool("COOKIE_SECURE", True),
            app_origin=os.getenv("APP_ORIGIN", "http://127.0.0.1:8000").rstrip("/"),
            free_only=_bool("FREE_ONLY", True),
            storage_persistent=_bool("STORAGE_PERSISTENT", False),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID") or None,
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET") or None,
            google_redirect_uri=os.getenv(
                "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback"
            ),
            google_gmail_redirect_uri=os.getenv(
                "GOOGLE_GMAIL_REDIRECT_URI",
                "http://127.0.0.1:8000/auth/google/gmail/callback",
            ),
            credential_encryption_key=os.getenv("CREDENTIAL_ENCRYPTION_KEY") or None,
            personal_data_mode=os.getenv("PERSONAL_DATA_MODE", "synthetic"),
            scheduler_enabled=_bool("SCHEDULER_ENABLED", True),
            scheduler_tick_seconds=tick,
            build_id=os.getenv("BUILD_ID", "local"),
        )

    @property
    def google_oidc_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


