import json
import time
import uuid

import httpx
import pytest

from app.assessment import assess_opportunity
from app.db import connect, initialize
from app.main import create_app
from app.models import Preferences, SearchRun, SourceDocument
from app.providers.gemini import GeminiAdapter
from app.runner import MAX_LLM_ATTEMPTS, _reassess_saved_opportunities_for_preferences, _reserve
from test_app import make_settings, seed_user
from test_assessment import SOURCE_TEXT, base_data
from test_synthetic_flow import _drain


@pytest.fixture
def saved_result(tmp_path, base_data):
    profile, opportunity, previous, model_findings = base_data
    app = create_app(make_settings(tmp_path))
    initialize(app.state.db_path)
    user_id, _ = seed_user(app.state.db_path)
    previous = Preferences.model_validate({**previous.model_dump(mode="python"), "company_search_mode": "watchlist_plus_discovery"})
    current = Preferences.model_validate({**previous.model_dump(mode="python"), "version": 2, "role_queries": ["registered nurse"]})
    run_id = uuid.uuid4()
    opportunity = type(opportunity).model_validate({**opportunity.model_dump(mode="python"), "user_id": user_id, "run_id": run_id})
    now = profile.created_at
    document = SourceDocument(
        id=opportunity.source_id, user_id=user_id, run_id=run_id,
        url=opportunity.canonical_url, title=opportunity.title,
        source_kind="extracted_page", body_text=SOURCE_TEXT, truncated=False,
        retrieved_at=now, published_at=None, extraction_status="retrieved", provider_request_id=None,
    )
    prior = assess_opportunity(opportunity=opportunity, profile=profile, preferences=previous,
                              model_assessment=model_findings, source_text=SOURCE_TEXT)
    run = SearchRun(
        id=run_id, user_id=user_id, state="running", state_version=4,
        profile_id=profile.id, profile_version=profile.version,
        preference_id=current.id, preference_version=2,
        action_count=3, search_call_count=1, extract_call_count=1, llm_call_count=5, decision_count=1,
        trigger="manual", occurrence_id=None, approved_source_snapshot=[],
        source_progress={"reassess_from_preference_version": 1}, deferred_counts={},
        claim_lease_until=None, started_at=now, updated_at=now, last_error=None,
    )
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO profiles(id,version,user_id,confirmed_at,payload_json) VALUES(?,?,?,?,?)",
                     (str(profile.id), profile.version, user_id, profile.confirmed_at.isoformat(), profile.model_dump_json()))
        for preference in (previous, current):
            conn.execute("INSERT INTO preferences(id,version,user_id,payload_json) VALUES(?,?,?,?)",
                         (str(preference.id), preference.version, user_id, preference.model_dump_json()))
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at) VALUES(?,?,?,?,?,?)",
                     (str(run.id), user_id, run.state, run.state_version, run.model_dump_json(), now.isoformat()))
        conn.execute("INSERT INTO sources(id,user_id,run_id,url,payload_json) VALUES(?,?,?,?,?)",
                     (str(document.id), user_id, str(run.id), str(document.url), json.dumps({"document": document.model_dump(mode="json")})))
        conn.execute("INSERT INTO opportunities(id,user_id,stable_source_key,canonical_url,payload_json) VALUES(?,?,?,?,?)",
                     (str(opportunity.id), user_id, opportunity.stable_source_key, str(opportunity.canonical_url), opportunity.model_dump_json()))
        conn.execute("INSERT INTO run_opportunities(run_id,opportunity_id) VALUES(?,?)", (str(run.id), str(opportunity.id)))
        conn.execute("INSERT INTO assessments(id,opportunity_id,profile_id,profile_version,preference_id,preference_version,payload_json) VALUES(?,?,?,?,?,?,?)",
                     (str(prior.id), str(opportunity.id), str(profile.id), profile.version, str(previous.id), 1, prior.model_dump_json()))
    return app.state.db_path, run, profile, current, prior


def refresh(saved_result, adapter, deadline=None):
    db_path, run, profile, preferences, _ = saved_result
    _drain(_reassess_saved_opportunities_for_preferences(
        db_path, run, profile, preferences, gemini=adapter,
        deadline=time.monotonic() + 60 if deadline is None else deadline,
    ))
    with connect(db_path) as conn:
        assessment = conn.execute("SELECT payload_json FROM assessments WHERE preference_version=2").fetchone()
        state = json.loads(conn.execute("SELECT payload_json FROM runs WHERE id=?", (str(run.id),)).fetchone()["payload_json"])
    return json.loads(assessment["payload_json"]) if assessment else None, state


@pytest.mark.parametrize("response_kind", ["success", "quota", "invalid"])
def test_changed_targets_use_real_adapter_and_never_retain_direct_fit(saved_result, response_kind):
    db_path, run, _, _, prior = saved_result
    requests = []

    async def handler(request):
        sent = json.loads(request.content)
        payload = json.loads(sent["contents"][0]["parts"][0]["text"])
        requests.append(payload)
        if response_kind == "quota":
            return httpx.Response(429)
        reply = {"role_alignment": "unrelated", "role_explanation": "The saved analyst internship is unrelated to the nursing target."}
        if response_kind == "invalid":
            reply["role_alignment"] = "made_up_category"
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(reply)}]}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GeminiAdapter("fake-key", "fixture", client=client,
                            reserve_attempt=lambda: _reserve(db_path, str(run.id), "llm_call_count", MAX_LLM_ATTEMPTS))
    try:
        result, state = refresh(saved_result, adapter)
    finally:
        _drain(client.aclose())
    assert len(requests) == 1
    assert requests[0]["TARGET_ROLES"] == ["registered nurse"]
    assert requests[0]["SOURCE_TEXT"] == SOURCE_TEXT
    assert "CONFIRMED_PROFILE" not in requests[0]
    assert result["role_alignment"] == ("unrelated" if response_kind == "success" else "unknown")
    assert result["score_components"]["role"] == 0
    assert result["requirement_matches"] == prior.model_dump(mode="json")["requirement_matches"]
    assert result["experience_alignment"] == prior.experience_alignment
    assert result["score"] == sum(result["score_components"].values())
    assert result["warnings"] if response_kind != "success" else "unrelated" in result["role_explanation"]
    assert state["llm_call_count"] == 6
    assert state["search_call_count"] == state["extract_call_count"] == 1
    with connect(db_path) as conn:
        old = json.loads(conn.execute("SELECT payload_json FROM assessments WHERE preference_version=1").fetchone()["payload_json"])
    assert old["role_alignment"] == "direct" and old["score_components"]["role"] == 25


class NoRequests:
    async def generate_structured(self, **kwargs):
        raise AssertionError("This operation must not spend another AI request")


@pytest.mark.parametrize("change", ["work_arrangement", "equivalent_role_text"])
def test_unchanged_role_inputs_reuse_findings_without_requests(saved_result, change):
    db_path, run, profile, current, prior = saved_result
    fields = current.model_dump(mode="python")
    fields["role_queries"] = ["DATA   ANALYST"] if change == "equivalent_role_text" else ["data analyst"]
    if change == "work_arrangement":
        fields["work_arrangements"] = ["remote"]
    current = Preferences.model_validate(fields)
    result, state = refresh((db_path, run, profile, current, prior), NoRequests())
    assert result["role_alignment"] == "direct" and result["score_components"]["role"] == 25
    assert state["llm_call_count"] == 5
    if change == "work_arrangement":
        assert result["bucket"] == "excluded"


@pytest.mark.parametrize("limit", ["budget", "deadline"])
def test_limits_preserve_unknown_fit_without_provider_requests(saved_result, limit):
    db_path, run, *_ = saved_result
    if limit == "budget":
        run.llm_call_count = MAX_LLM_ATTEMPTS
        with connect(db_path) as conn:
            conn.execute("UPDATE runs SET payload_json=? WHERE id=?", (run.model_dump_json(), str(run.id)))
    result, state = refresh(saved_result, NoRequests(), deadline=time.monotonic() - 1 if limit == "deadline" else None)
    assert result["role_alignment"] == "unknown" and result["score_components"]["role"] == 0
    assert result["warnings"]
    assert state["llm_call_count"] == (MAX_LLM_ATTEMPTS if limit == "budget" else 5)
    assert state["search_call_count"] == state["extract_call_count"] == 1


def test_cancellation_during_refresh_does_not_publish_new_assessment(saved_result):
    from app.models import RoleAlignmentOutput
    db_path, run, *_ = saved_result

    class CancelDuringRequest:
        async def generate_structured(self, **kwargs):
            cancelled = run.model_copy(update={"state": "cancelled", "state_version": run.state_version + 1})
            with connect(db_path) as conn:
                conn.execute("UPDATE runs SET state='cancelled',payload_json=? WHERE id=?", (cancelled.model_dump_json(), str(run.id)))
            return RoleAlignmentOutput(role_alignment="unrelated", role_explanation="The target changed.")

    result, state = refresh(saved_result, CancelDuringRequest())
    assert result is None and state["state"] == "cancelled"


def test_restart_reuses_already_refreshed_snapshot_without_another_request(saved_result):
    from app.models import RoleAlignmentOutput
    db_path, run, *_ = saved_result

    class Refreshed:
        async def generate_structured(self, **kwargs):
            return RoleAlignmentOutput(role_alignment="unrelated", role_explanation="Analyst and nursing targets differ.")

    first, state = refresh(saved_result, Refreshed())
    state["source_progress"]["reassess_from_preference_version"] = 1
    with connect(db_path) as conn:
        conn.execute("UPDATE runs SET payload_json=? WHERE id=?", (json.dumps(state), str(run.id)))
    second, _ = refresh(saved_result, NoRequests())
    assert second == first
