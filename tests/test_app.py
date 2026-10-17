from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from pathlib import Path


from app.config import Settings
from app.db import connect, initialize
from app.main import create_app
from app.models import (
    AgentAction,
    EvidenceRef,
    MonitoringSettings,
    PrivateLead,
    ProfileFact,
    Preferences,
    Opportunity,
    Profile,
    UserCompany,
    DecisionOption,
    HumanDecision,
    SearchRun,
    normalize_evidence_text,
    validate_evidence,
)
from app.sessions import create_session
from app.state import get_owned, transition_run
from fastapi import HTTPException, Response
from starlette.background import BackgroundTasks
from starlette.requests import Request
from pydantic import ValidationError
import pytest


def make_settings(tmp_path, **overrides):
    values = dict(
        gemini_api_key=None,
        gemini_model="gemini-3.5-flash-lite",
        tavily_api_key=None,
        data_dir=tmp_path,
        port=8000,
        cookie_secure=False,
        app_origin="http://testserver",
        free_only=True,
        storage_persistent=False,
        google_client_id=None,
        google_client_secret=None,
        google_redirect_uri="http://testserver/auth/google/callback",
        google_gmail_redirect_uri="http://testserver/auth/google/gmail/callback",
        credential_encryption_key=None,
        personal_data_mode="synthetic",
        scheduler_enabled=True,
        scheduler_tick_seconds=60,
        build_id="test-build",
    )
    values.update(overrides)
    return Settings(**values)


def seed_user(db_path, email="demo@example.invalid"):
    user_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users(id,google_issuer,google_subject,verified_email,created_at) VALUES(?,?,?,?,?)",
            (user_id, "https://accounts.google.com", uuid.uuid4().hex, email, datetime.now(timezone.utc).isoformat()),
        )
    cookie, _ = create_session(db_path, user_id)
    return user_id, cookie


def route_endpoint(app, path, method=None):
    routes = []
    for route in app.routes:
        routes.extend(getattr(getattr(route, "original_router", None), "routes", []))
        routes.append(route)
    return next(route.endpoint for route in routes if getattr(route, "path", None) == path and (method is None or method in getattr(route, "methods", set())))


def request_for(app, cookie=None):
    headers = []
    if cookie:
        headers.append((b"cookie", f"rolelander_session={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "app": app})


def mutation_request(app, cookie, csrf):
    return Request({"type": "http", "method": "POST", "path": "/api/test", "headers": [
        (b"cookie", f"rolelander_session={cookie}".encode()),
        (b"origin", b"http://testserver"), (b"x-csrf-token", csrf.encode()),
    ], "app": app})


def run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("Route performed unexpected asynchronous I/O")


def test_health_without_provider_credentials_and_capabilities_hide_secrets(tmp_path):
    app = create_app(make_settings(tmp_path, gemini_api_key="must-not-leak", tavily_api_key="also-secret"))
    initialize(app.state.db_path)
    health = run_immediate(route_endpoint(app, "/healthz")())
    assert health == {"status": "ok", "version": "test-build"}
    with pytest.raises(HTTPException) as unauthorized:
        run_immediate(route_endpoint(app, "/api/capabilities")(request_for(app)))
    assert unauthorized.value.status_code == 401

    user_id, cookie = seed_user(app.state.db_path)
    caps = run_immediate(route_endpoint(app, "/api/capabilities")(request_for(app, cookie)))
    assert caps["providers"] == {"gemini": True, "tavily": True}
    assert "must-not-leak" not in json.dumps(caps)
    assert "also-secret" not in json.dumps(caps)
    assert run_immediate(route_endpoint(app, "/api/me")(request_for(app, cookie)))["user"]["id"] == user_id


def test_session_cookie_does_not_authenticate_another_client(tmp_path):
    app = create_app(make_settings(tmp_path))
    initialize(app.state.db_path)
    _user_id, cookie = seed_user(app.state.db_path)
    assert run_immediate(route_endpoint(app, "/api/me")(request_for(app, cookie)))["user"]["id"] == _user_id
    with pytest.raises(HTTPException) as unauthorized:
        run_immediate(route_endpoint(app, "/api/me")(request_for(app)))
    assert unauthorized.value.status_code == 401


def test_unconfigured_oidc_fails_closed(tmp_path):
    app = create_app(make_settings(tmp_path))
    response = run_immediate(route_endpoint(app, "/auth/google/start")(request_for(app)))
    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "OAUTH_NOT_CONFIGURED"


def test_contract_rejects_unknown_fields_and_missing_cv_quote():
    values = dict(id=uuid.uuid4(), kind="skill", label="Python", value="Basic", level="basic", origin="cv", source_quote="Basic Python", confirmed=False)
    with pytest.raises(ValidationError):
        ProfileFact(**values, unexpected="blocked")
    with pytest.raises(ValidationError):
        ProfileFact(**{**values, "source_quote": None})


def test_preferences_require_explicit_country_scope_and_valid_country():
    base = dict(
        id=uuid.uuid4(), version=1, country_code="PK", country_scope="jobs_in_country",
        role_queries=["data analyst"], opportunity_types=["job"], seniority=["any"],
        work_arrangements=["remote"], location_mode="anywhere", preferred_cities=[],
        preferred_cities_policy="preferred", excluded_cities=[], proximity=None,
        paid_policy="any", right_to_work="unknown", needs_sponsorship="unknown",
        availability_note=None, career_interests=[], preferred_industries=[],
        excluded_industries=[], employment_commitment="either", relocation_policy="unknown",
        company_search_mode="watchlist_plus_discovery", company_size_preferences=[],
        company_stage_preferences=[], compensation_preferences=[
            dict(opportunity_type="job", preferred_amount=None, minimum_amount=None, currency="PKR", period="month", minimum_policy="preferred", include_undisclosed=True, allow_unpaid=False, basis="gross", component="base")
        ],
    )
    Preferences(**base)
    with pytest.raises(ValidationError):
        Preferences(**{k: v for k, v in base.items() if k != "country_scope"})
    with pytest.raises(ValidationError):
        Preferences(**{**base, "country_code": "XX"})


def test_evidence_normalization_and_source_binding():
    source_id = uuid.uuid4()
    ref = EvidenceRef(source_id=source_id, quote="Basic Python")
    validate_evidence(ref, source_id, "Candidate: Basic   Python experience")
    assert normalize_evidence_text(" e\u0301  text ") == "é text"
    with pytest.raises(ValueError):
        validate_evidence(ref, uuid.uuid4(), "Basic Python")
    with pytest.raises(ValueError):
        validate_evidence(ref, source_id, "Python only")


def test_agent_action_and_company_approval_contracts_are_closed():
    AgentAction(action="search", query="analyst roles in Pakistan", opportunity_ids=[], decision_proposal=None, public_summary="Searching roles.")
    with pytest.raises(ValidationError):
        AgentAction(action="search", query="analyst roles", opportunity_ids=[], decision_proposal=None, public_summary="Search.", approve_company=True)
    with pytest.raises(ValidationError):
        AgentAction(action="search", query="analyst roles", opportunity_ids=[uuid.uuid4()], decision_proposal=None, public_summary="Search.")
    with pytest.raises(ValidationError):
        UserCompany(user_id=uuid.uuid4(), company_id=uuid.uuid4(), status="approved", preferred=False, priority=0, origin="recommendation", approved_at=None, version=1)


def test_monitoring_and_private_lead_contracts_reject_unsafe_defaults():
    MonitoringSettings(user_id=uuid.uuid4(), enabled=False, frequency="manual", local_time=None, timezone=None, weekday=None, company_enabled=False, gmail_enabled=False, discovery_enabled=False, notify_in_app=True, notify_email=False, notification_account_id=None, next_run_at=None, version=1)
    with pytest.raises(ValidationError):
        MonitoringSettings(user_id=uuid.uuid4(), enabled=True, frequency="weekly", local_time="09:00", timezone="Asia/Karachi", weekday=None, company_enabled=True, gmail_enabled=False, discovery_enabled=False, notify_in_app=True, notify_email=False, notification_account_id=None, next_run_at=None, version=1)
    with pytest.raises(ValidationError):
        PrivateLead(id=uuid.uuid4(), user_id=uuid.uuid4(), account_id=uuid.uuid4(), message_id="m1", safe_subject="Lead", public_urls=[], review_status="new", received_at=datetime.now(timezone.utc), raw_body="must not be stored")


def test_synthetic_fixtures_are_labeled_and_contract_valid():
    fixture_dir = Path(__file__).parent / "fixtures"
    profile_data = json.loads((fixture_dir / "sample_profile.json").read_text(encoding="utf-8"))
    opportunity_data = json.loads((fixture_dir / "sample_opportunity.json").read_text(encoding="utf-8"))
    assert profile_data.pop("sample_label") == "SAMPLE DATA"
    assert opportunity_data.pop("sample_label") == "SAMPLE DATA"
    profile = Profile.model_validate(profile_data)
    opportunity = Opportunity.model_validate(opportunity_data)
    assert profile.professional_years is None
    assert any(fact.kind == "project" for fact in profile.facts)
    assert str(opportunity.canonical_url).endswith(".invalid/sample-employer/analyst-intern-001")


def test_owned_lookup_hides_foreign_resources_and_transition_is_versioned(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    initialize(db_path)
    owner_id, _ = seed_user(db_path)
    other_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at) VALUES(?,?,?,?,?,?)", (run_id, owner_id, "queued", 1, "{}", datetime.now(timezone.utc).isoformat()))
        assert get_owned(conn, "runs", run_id, owner_id)["id"] == run_id
        with pytest.raises(HTTPException) as error:
            get_owned(conn, "runs", run_id, other_id)
        assert error.value.status_code == 404
        with pytest.raises(ValueError):
            get_owned(conn, "runs WHERE 1=1 --", run_id, owner_id)
    assert transition_run(db_path, run_id, 1, "running") == 2
    with pytest.raises(HTTPException) as stale:
        transition_run(db_path, run_id, 1, "running")
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as illegal:
        transition_run(db_path, run_id, 2, "queued")
    assert illegal.value.status_code == 409


def test_profile_routes_version_edit_confirm_and_owner_scope(tmp_path):
    app = create_app(make_settings(tmp_path))
    initialize(app.state.db_path)
    user_id, cookie = seed_user(app.state.db_path)
    with connect(app.state.db_path) as conn:
        csrf = conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?", (user_id,)).fetchone()["csrf_token"]
    request = mutation_request(app, cookie, csrf)
    manual = route_endpoint(app, "/api/profiles/manual")
    profile = run_immediate(manual(
        __import__("app.profile_routes", fromlist=["ManualProfileInput"]).ManualProfileInput(
            display_name="A Candidate", professional_years=2, facts=[{"kind":"skill","label":"Python","value":"Basic","level":"basic"}]
        ), request,
    ))
    assert profile["version"] == 1 and profile["confirmed_at"] is None
    edit = route_endpoint(app, "/api/profiles/{profile_id}", "PUT")
    edit_body = __import__("app.profile_routes", fromlist=["ProfileEditInput"]).ProfileEditInput(
        expected_version=1, display_name="A Candidate", professional_years=3,
        facts=[{"kind":"skill","label":"Python","value":"Intermediate","level":"intermediate"}],
    )
    updated = run_immediate(edit(profile["id"], edit_body, request))
    assert updated["version"] == 2 and updated["confirmed_at"] is None
    confirm = route_endpoint(app, "/api/profiles/{profile_id}/confirm")
    confirm_body = __import__("app.profile_routes", fromlist=["ConfirmInput"]).ConfirmInput(expected_version=2)
    confirmed = run_immediate(confirm(profile["id"], confirm_body, request))
    assert confirmed["confirmed_at"] is not None and confirmed["facts"][0]["confirmed"] is True
    with pytest.raises(HTTPException) as stale:
        run_immediate(confirm(profile["id"], confirm_body.model_copy(update={"expected_version":1}), request))
    assert stale.value.status_code == 409
    other_id, other_cookie = seed_user(app.state.db_path, "other@example.invalid")
    with connect(app.state.db_path) as conn:
        other_csrf = conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?", (other_id,)).fetchone()["csrf_token"]
    other_req = mutation_request(app, other_cookie, other_csrf)
    get_profile = route_endpoint(app, "/api/profiles/{profile_id}", "GET")
    with pytest.raises(HTTPException) as hidden:
        run_immediate(get_profile(profile["id"], other_req, version=None))
    assert hidden.value.status_code == 404


def test_preferences_route_requires_explicit_location_and_rejects_conflicting_cities(tmp_path):
    app = create_app(make_settings(tmp_path))
    initialize(app.state.db_path)
    user_id, cookie = seed_user(app.state.db_path)
    with connect(app.state.db_path) as conn:
        csrf = conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?", (user_id,)).fetchone()["csrf_token"]
    request = mutation_request(app, cookie, csrf)
    from app.profile_routes import PreferencesInput

    fields = dict(
        country_code="PK", country_scope="open_to_country", role_queries=["data analyst"], opportunity_types=["job"],
        seniority=["any"], work_arrangements=["remote"], location_mode="anywhere", preferred_cities=[],
        preferred_cities_policy="preferred", excluded_cities=[], proximity=None, paid_policy="any", right_to_work="unknown",
        needs_sponsorship="unknown", availability_note=None, career_interests=[], preferred_industries=[], excluded_industries=[],
        employment_commitment="either", relocation_policy="unknown", company_search_mode="watchlist_only",
        company_size_preferences=[], company_stage_preferences=[], compensation_preferences=[dict(
            opportunity_type="job", preferred_amount=None, minimum_amount=None, currency="PKR", period="month",
            minimum_policy="preferred", include_undisclosed=True, allow_unpaid=False, basis="gross", component="base",
        )],
    )
    prefs = route_endpoint(app, "/api/preferences")
    saved = run_immediate(prefs(PreferencesInput(**fields), request))
    assert saved["country_code"] == "PK" and saved["country_scope"] == "open_to_country"
    with pytest.raises(ValidationError):
        PreferencesInput(**{k: v for k, v in fields.items() if k != "country_scope"})
    city = dict(id="pk-lahore", name="Lahore", country_code="PK", admin_area=None, latitude=None, longitude=None, coordinate_precision="unknown")
    with pytest.raises(HTTPException) as missing_city:
        run_immediate(prefs(PreferencesInput(**{**fields, "location_mode":"cities", "preferred_cities":[]}), request))
    assert missing_city.value.status_code == 422
    with pytest.raises(HTTPException) as conflict:
        run_immediate(prefs(PreferencesInput(**{**fields, "preferred_cities":[city], "excluded_cities":[city]}), request))
    assert conflict.value.status_code == 422


def test_run_creation_requires_confirmed_profile_and_is_durable_idempotent(tmp_path):
    app = create_app(make_settings(tmp_path))
    initialize(app.state.db_path)
    user_id, cookie = seed_user(app.state.db_path)
    with connect(app.state.db_path) as conn:
        csrf = conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?", (user_id,)).fetchone()["csrf_token"]
    request = mutation_request(app, cookie, csrf)
    profile_id, preference_id = uuid.uuid4(), uuid.uuid4()
    profile_data = dict(id=str(profile_id),version=1,display_name="Synthetic",professional_years=None,facts=[],confirmed_at=None,created_at=datetime.now(timezone.utc).isoformat())
    preferences = Preferences(
        id=preference_id,version=1,country_code="PK",country_scope="open_to_country",role_queries=["data analyst"],
        opportunity_types=["job"],seniority=["any"],work_arrangements=["remote"],location_mode="anywhere",
        preferred_cities=[],preferred_cities_policy="preferred",excluded_cities=[],proximity=None,paid_policy="any",
        right_to_work="unknown",needs_sponsorship="unknown",availability_note=None,career_interests=[],preferred_industries=[],
        excluded_industries=[],employment_commitment="either",relocation_policy="unknown",company_search_mode="watchlist_only",
        company_size_preferences=[],company_stage_preferences=[],compensation_preferences=[dict(
            opportunity_type="job",preferred_amount=None,minimum_amount=None,currency="PKR",period="month",minimum_policy="preferred",
            include_undisclosed=True,allow_unpaid=False,basis="gross",component="base")],
    )
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO profiles(id,version,user_id,confirmed_at,payload_json) VALUES(?,?,?,?,?)",(str(profile_id),1,user_id,None,json.dumps(profile_data)))
        conn.execute("INSERT INTO preferences(id,version,user_id,payload_json) VALUES(?,?,?,?)",(str(preference_id),1,user_id,json.dumps(preferences.model_dump(mode="json"))))
    from app.run_routes import RunCreateInput
    endpoint = route_endpoint(app,"/api/runs","POST")
    scheduled=[]
    app.state.run_segment=lambda run_id: scheduled.append(run_id)
    body = RunCreateInput(profile_id=profile_id,profile_version=1,preference_id=preference_id,preference_version=1)
    no_run = BackgroundTasks()
    with pytest.raises(HTTPException) as unconfirmed:
        run_immediate(endpoint(body,request,no_run,idempotency_key="run-request-0001"))
    assert unconfirmed.value.status_code == 409 and len(no_run.tasks) == 0
    profile_data["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    with connect(app.state.db_path) as conn:
        conn.execute("UPDATE profiles SET confirmed_at=?,payload_json=? WHERE id=? AND version=1",(profile_data["confirmed_at"],json.dumps(profile_data),str(profile_id)))
    background = BackgroundTasks()
    created = run_immediate(endpoint(body,request,background,idempotency_key="run-request-0001"))
    replay_background = BackgroundTasks()
    replay = run_immediate(endpoint(body,request,replay_background,idempotency_key="run-request-0001"))
    assert created["id"] == replay["id"] and created["state"] == "queued"
    assert len(background.tasks) == 1 and len(replay_background.tasks) == 0
    with pytest.raises(HTTPException) as active_limit:
        run_immediate(endpoint(body,request,BackgroundTasks(),idempotency_key="run-request-0002"))
    assert active_limit.value.status_code == 409
    with connect(app.state.db_path) as conn:
        conn.execute("UPDATE runs SET state='completed' WHERE id=?",(created["id"],))
    for index in range(4):
        extra=run_immediate(endpoint(body,request,BackgroundTasks(),idempotency_key=f"run-extra-{index:04d}"))
        with connect(app.state.db_path) as conn:
            conn.execute("UPDATE runs SET state='completed' WHERE id=?",(extra["id"],))
    with pytest.raises(HTTPException) as hourly_limit:
        run_immediate(endpoint(body,request,BackgroundTasks(),idempotency_key="run-hourly-0001"))
    assert hourly_limit.value.status_code == 429
    background.tasks[0].func(created["id"])
    assert scheduled == [created["id"]]
    get_run = route_endpoint(app,"/api/runs/{run_id}","GET")
    final = run_immediate(get_run(created["id"],request))
    assert final["state"] == "queued" and final["state_version"] == 1


def test_human_decision_answer_applies_one_validated_preference_version_once(tmp_path):
    app=create_app(make_settings(tmp_path)); initialize(app.state.db_path)
    user_id,cookie=seed_user(app.state.db_path)
    with connect(app.state.db_path) as conn:
        csrf=conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?",(user_id,)).fetchone()["csrf_token"]
    request=mutation_request(app,cookie,csrf)
    now=datetime.now(timezone.utc); run_id=uuid.uuid4(); profile_id=uuid.uuid4(); preference_id=uuid.uuid4(); decision_id=uuid.uuid4()
    preferences=Preferences(id=preference_id,version=1,country_code="PK",country_scope="open_to_country",role_queries=["data analyst"],
        opportunity_types=["job"],seniority=["any"],work_arrangements=["remote"],location_mode="anywhere",preferred_cities=[],
        preferred_cities_policy="preferred",excluded_cities=[],proximity=None,paid_policy="any",right_to_work="unknown",needs_sponsorship="unknown",
        availability_note=None,career_interests=[],preferred_industries=[],excluded_industries=[],employment_commitment="either",
        relocation_policy="unknown",company_search_mode="watchlist_only",company_size_preferences=[],company_stage_preferences=[],
        compensation_preferences=[dict(opportunity_type="job",preferred_amount=None,minimum_amount=None,currency="PKR",period="month",minimum_policy="preferred",include_undisclosed=True,allow_unpaid=False,basis="gross",component="base")])
    run=SearchRun(id=run_id,user_id=uuid.UUID(user_id),state="awaiting_decision",state_version=2,profile_id=profile_id,profile_version=1,
        preference_id=preference_id,preference_version=1,action_count=2,search_call_count=1,extract_call_count=0,llm_call_count=3,decision_count=1,
        trigger="manual",occurrence_id=None,approved_source_snapshot=[],source_progress={"stage":"awaiting_decision"},deferred_counts={},
        claim_lease_until=None,started_at=now,updated_at=now,last_error=None)
    decision=HumanDecision(id=decision_id,run_id=run_id,state_version=2,kind="preference_tradeoff",question="Would broader related roles help?",
        options=[DecisionOption(id="apply",label="Apply change",explanation="Try quality analyst roles",patch={"preference_field":"role_queries","proposed_value":["quality analyst"]},action="apply_preferences"),
                 DecisionOption(id="finish",label="Keep current settings",explanation="Finish without changing search",patch=None,action="finish")],
        state="pending",selected_option_id=None,answer_note=None,created_at=now,answered_at=None)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO preferences(id,version,user_id,payload_json) VALUES(?,?,?,?)",(str(preference_id),1,user_id,json.dumps(preferences.model_dump(mode="json"))))
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at) VALUES(?,?,?,?,?,?)",(str(run_id),user_id,run.state,run.state_version,json.dumps(run.model_dump(mode="json")),now.isoformat()))
        conn.execute("INSERT INTO decisions(id,run_id,state,payload_json) VALUES(?,?,?,?)",(str(decision_id),str(run_id),"pending",json.dumps(decision.model_dump(mode="json"))))
    scheduled=[]; app.state.run_segment=lambda value:scheduled.append(value)
    from app.run_routes import DecisionAnswerInput
    answer=route_endpoint(app,"/api/decisions/{decision_id}/answer","POST")
    body=DecisionAnswerInput(expected_state_version=2,option_id="apply",answer_note=None)
    background=BackgroundTasks(); response=Response()
    result=run_immediate(answer(decision_id,body,request,background,response))
    assert response.status_code==202 and result["state"]=="queued" and result["state_version"]==3
    assert result["preference_version"]==2 and len(background.tasks)==1
    with connect(app.state.db_path) as conn:
        updated=conn.execute("SELECT payload_json FROM preferences WHERE id=? AND version=2",(str(preference_id),)).fetchone()
    assert json.loads(updated["payload_json"])["role_queries"]==["quality analyst"]
    replay_bg=BackgroundTasks(); replay_response=Response()
    replay=run_immediate(answer(decision_id,body,request,replay_bg,replay_response))
    assert replay["id"]==result["id"] and len(replay_bg.tasks)==0


def test_results_endpoint_handles_owned_empty_run_without_source_sql_error(tmp_path):
    app=create_app(make_settings(tmp_path)); initialize(app.state.db_path)
    user_id,cookie=seed_user(app.state.db_path)
    now=datetime.now(timezone.utc); run_id=uuid.uuid4(); profile_id=uuid.uuid4(); preference_id=uuid.uuid4()
    run=SearchRun(id=run_id,user_id=uuid.UUID(user_id),state="completed",state_version=2,profile_id=profile_id,profile_version=1,
        preference_id=preference_id,preference_version=1,action_count=1,search_call_count=1,extract_call_count=0,llm_call_count=1,decision_count=0,
        trigger="manual",occurrence_id=None,approved_source_snapshot=[],source_progress={"stage":"completed"},deferred_counts={},
        claim_lease_until=None,started_at=now,updated_at=now,last_error=None)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(run_id),user_id,run.state,run.state_version,json.dumps(run.model_dump(mode="json")),now.isoformat(),now.isoformat()))
    result=run_immediate(route_endpoint(app,"/api/runs/{run_id}/opportunities","GET")(run_id,request_for(app,cookie)))
    assert result == {"items":[]}


def test_synthetic_sample_is_host_marked_but_still_requires_confirmation(tmp_path):
    app=create_app(make_settings(tmp_path)); initialize(app.state.db_path)
    user_id,cookie=seed_user(app.state.db_path)
    with connect(app.state.db_path) as conn:
        csrf=conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?",(user_id,)).fetchone()["csrf_token"]
    profile=run_immediate(route_endpoint(app,"/api/profiles/sample","POST")(mutation_request(app,cookie,csrf)))
    assert profile["confirmed_at"] is None and all(not item["confirmed"] for item in profile["facts"])
    with connect(app.state.db_path) as conn:
        row=conn.execute("SELECT is_synthetic FROM profiles WHERE id=?",(profile["id"],)).fetchone()
    assert row["is_synthetic"] == 1


def test_resume_at_action_limit_finishes_with_valid_persisted_counter(tmp_path):
    from app.runner import _read_run, _run_controller, MAX_AGENT_ACTIONS
    app=create_app(make_settings(tmp_path,gemini_api_key="test",tavily_api_key="test")); initialize(app.state.db_path)
    user_id,_cookie=seed_user(app.state.db_path)
    now=datetime.now(timezone.utc); run_id=uuid.uuid4()
    run=SearchRun(id=run_id,user_id=uuid.UUID(user_id),state="running",state_version=2,profile_id=uuid.uuid4(),profile_version=1,
        preference_id=uuid.uuid4(),preference_version=1,action_count=MAX_AGENT_ACTIONS,search_call_count=0,extract_call_count=0,llm_call_count=0,decision_count=0,
        trigger="manual",occurrence_id=None,approved_source_snapshot=[],source_progress={"stage":"running"},deferred_counts={},
        claim_lease_until=now,started_at=now,updated_at=now,last_error=None)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (str(run_id),user_id,run.state,run.state_version,json.dumps(run.model_dump(mode="json")),now.isoformat(),now.isoformat()))
    task=_run_controller(app,run,Profile.model_construct(),Preferences.model_construct())
    with pytest.raises(StopIteration): task.send(None)
    saved=_read_run(app.state.db_path,str(run_id))
    assert saved.action_count == MAX_AGENT_ACTIONS and saved.state == "failed"


def test_assessment_identity_keeps_distinct_profile_and_preference_records(tmp_path):
    app=create_app(make_settings(tmp_path)); initialize(app.state.db_path)
    user_id,_cookie=seed_user(app.state.db_path); opportunity_id=uuid.uuid4()
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO opportunities(id,user_id,stable_source_key,canonical_url,payload_json) VALUES(?,?,?,?,?)",
            (str(opportunity_id),user_id,"key","https://jobs.example.invalid/role","{}"))
        for index in range(2):
            conn.execute("INSERT INTO assessments(id,opportunity_id,profile_id,profile_version,preference_id,preference_version,payload_json) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),str(opportunity_id),str(uuid.uuid4()),1,str(uuid.uuid4()),1,json.dumps({"version":index})))
        count=conn.execute("SELECT COUNT(*) AS n FROM assessments WHERE opportunity_id=?",(str(opportunity_id),)).fetchone()["n"]
    assert count == 2


def test_global_active_run_cap_is_enforced_before_profile_reads(tmp_path):
    app=create_app(make_settings(tmp_path)); initialize(app.state.db_path)
    user_id,cookie=seed_user(app.state.db_path)
    now=datetime.now(timezone.utc).isoformat()
    with connect(app.state.db_path) as conn:
        for _ in range(20):
            other=str(uuid.uuid4())
            conn.execute("INSERT INTO users(id,google_issuer,google_subject,verified_email,created_at) VALUES(?,?,?,?,?)",
                (other,"https://accounts.google.com",uuid.uuid4().hex,f"{other}@example.invalid",now))
            conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),other,"queued",1,"{}",now,now))
    with connect(app.state.db_path) as conn:
        csrf=conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?",(user_id,)).fetchone()["csrf_token"]
    request=mutation_request(app,cookie,csrf)
    from app.run_routes import RunCreateInput
    body=RunCreateInput(profile_id=uuid.uuid4(),profile_version=1,preference_id=uuid.uuid4(),preference_version=1)
    with pytest.raises(HTTPException) as full:
        run_immediate(route_endpoint(app,"/api/runs","POST")(body,request,BackgroundTasks(),idempotency_key="global-cap-0001"))
    assert full.value.status_code == 503


def test_old_assessment_schema_migrates_without_losing_saved_identity(tmp_path):
    from app.db import connect, initialize
    db_path=tmp_path/"legacy.sqlite3"
    user_id=str(uuid.uuid4()); run_id=str(uuid.uuid4()); opportunity_id=str(uuid.uuid4())
    profile_id=str(uuid.uuid4()); preference_id=str(uuid.uuid4()); now=datetime.now(timezone.utc).isoformat()
    with connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY,google_issuer TEXT,google_subject TEXT,verified_email TEXT,created_at TEXT);
            CREATE TABLE runs(id TEXT PRIMARY KEY,user_id TEXT,state TEXT,state_version INTEGER,payload_json TEXT,updated_at TEXT);
            CREATE TABLE opportunities(id TEXT PRIMARY KEY,user_id TEXT,stable_source_key TEXT,canonical_url TEXT,payload_json TEXT);
            CREATE TABLE run_opportunities(run_id TEXT,opportunity_id TEXT,PRIMARY KEY(run_id,opportunity_id));
            CREATE TABLE assessments(id TEXT PRIMARY KEY,opportunity_id TEXT,profile_version INTEGER,preference_version INTEGER,payload_json TEXT,UNIQUE(opportunity_id,profile_version,preference_version));
        """)
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?)",(user_id,"issuer","subject","legacy@example.invalid",now))
        payload=json.dumps({"profile_id":profile_id,"profile_version":1,"preference_id":preference_id,"preference_version":1})
        conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",(run_id,user_id,"completed",2,payload,now))
        conn.execute("INSERT INTO opportunities VALUES(?,?,?,?,?)",(opportunity_id,user_id,"key","https://jobs.example.invalid/role","{}"))
        conn.execute("INSERT INTO run_opportunities VALUES(?,?)",(run_id,opportunity_id))
        conn.execute("INSERT INTO assessments VALUES(?,?,?,?,?)",(str(uuid.uuid4()),opportunity_id,1,1,"{}"))
    initialize(db_path)
    with connect(db_path) as conn:
        row=conn.execute("SELECT profile_id,preference_id FROM assessments WHERE opportunity_id=?",(opportunity_id,)).fetchone()
    assert row["profile_id"]==profile_id and row["preference_id"]==preference_id

