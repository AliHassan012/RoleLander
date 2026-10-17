import asyncio
import json
import uuid

import pytest
from datetime import datetime, timezone
from pathlib import Path

from app.assessment import normalize_url
from app.config import Settings
from app.db import connect, initialize
from app.main import create_app
from app.models import AgentAction, AssessOpportunityOutput, DecisionProposal, ExtractOpportunityOutput, OpportunityFields, RequirementMatch
from app.profile_routes import ConfirmInput, PreferencesInput
from app.run_routes import DecisionAnswerInput, RunCreateInput, RunStateInput
from app.sessions import create_session
from app.providers.tavily import ExtractOutcome, ExtractedPage, SearchOutcome, SearchResult
from fastapi import Response
from starlette.background import BackgroundTasks
from starlette.requests import Request


BASE = Path(__file__).parent / "fixtures"
SAMPLE_OPPORTUNITY = json.loads((BASE / "sample_opportunity.json").read_text(encoding="utf-8"))
SOURCE_TEXT = "SAMPLE Data Analyst Intern. Northstar Learning Cooperative (SAMPLE). Internship. Hybrid in Islamabad, Pakistan. Basic Python familiarity preferred. " + SAMPLE_OPPORTUNITY["salary_details"]["evidence_refs"][0]["quote"] + "."


def _drain(coro):
    while True:
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value


def _route(app, path, method):
    for route in app.routes:
        for item in getattr(getattr(route, "original_router", None), "routes", []):
            if item.path == path and method in item.methods:
                return item.endpoint
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(path)


@pytest.mark.parametrize("bad_source", [False, True])
@pytest.mark.parametrize("preference_change", ["work_arrangement", "target_roles"])
def test_sample_profile_to_edited_draft_with_human_resume(tmp_path, monkeypatch, preference_change, bad_source):
    settings = Settings(gemini_api_key="fake-free-key", gemini_model="fixture", tavily_api_key="fake-free-key",
        data_dir=tmp_path, port=8000, cookie_secure=False, app_origin="http://testserver", free_only=True,
        storage_persistent=False, google_client_id=None, google_client_secret=None,
        google_redirect_uri="http://testserver/auth/google/callback", google_gmail_redirect_uri="http://testserver/auth/google/gmail/callback",
        credential_encryption_key=None, personal_data_mode="synthetic", scheduler_enabled=False, scheduler_tick_seconds=60, build_id="flow-test")
    app = create_app(settings); initialize(app.state.db_path)
    user_id=str(uuid.uuid4())
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO users(id,google_issuer,google_subject,verified_email,created_at) VALUES(?,?,?,?,?)",
            (user_id,"https://accounts.google.com",uuid.uuid4().hex,"synthetic@example.invalid",datetime.now(timezone.utc).isoformat()))
    cookie,_=create_session(app.state.db_path,user_id)
    with connect(app.state.db_path) as conn:
        csrf=conn.execute("SELECT csrf_token FROM sessions WHERE user_id=?",(user_id,)).fetchone()["csrf_token"]
    def request(method="POST"):
        return Request({"type":"http","method":method,"path":"/api/test","headers":[(b"cookie",f"rolelander_session={cookie}".encode()),(b"origin",b"http://testserver"),(b"x-csrf-token",csrf.encode())],"app":app})

    sample=_drain(_route(app,"/api/profiles/sample","POST")(request()))
    confirmed=_drain(_route(app,"/api/profiles/{profile_id}/confirm","POST")(sample["id"],ConfirmInput(expected_version=1),request()))
    assert confirmed["confirmed_at"] and all(f["confirmed"] for f in confirmed["facts"])
    prefs_input=PreferencesInput(country_code="PK",country_scope="open_to_country",role_queries=["data analyst"],opportunity_types=["internship"],
        seniority=["any"],work_arrangements=["hybrid"],location_mode="anywhere",preferred_cities=[],preferred_cities_policy="preferred",excluded_cities=[],proximity=None,
        paid_policy="any",right_to_work="unknown",needs_sponsorship="unknown",availability_note=None,career_interests=[],preferred_industries=[],excluded_industries=[],
        employment_commitment="either",relocation_policy="unknown",company_search_mode="watchlist_plus_discovery",company_size_preferences=[],company_stage_preferences=[],
        compensation_preferences=[dict(opportunity_type="internship",preferred_amount=None,minimum_amount=None,currency="PKR",period="month",minimum_policy="preferred",include_undisclosed=True,allow_unpaid=True,basis="gross",component="base")])
    prefs=_drain(_route(app,"/api/preferences","POST")(prefs_input,request()))

    fixture=dict(SAMPLE_OPPORTUNITY); fixture.pop("sample_label")
    counters={"controller":0,"role_refresh":0}
    rejected_source_id = str(uuid.uuid4())
    class FakeGemini:
        def __init__(self,*args,**kwargs): self.reserve=kwargs.get("reserve_attempt")
        async def generate_structured(self,*,system_prompt,user_payload,output_model,max_attempts):
            if self.reserve and not self.reserve(): raise AssertionError("test LLM budget exhausted unexpectedly")
            if output_model is AgentAction:
                counters["controller"]+=1
                if counters["controller"]==1:
                    return AgentAction(action="search",query="data analyst internships Pakistan",opportunity_ids=[],decision_proposal=None,public_summary="Searching current listings.")
                if counters["controller"]==2:
                    ids=[item["id"] for item in user_payload["OBSERVATION"]["candidates"]]
                    return AgentAction(action="inspect",query=None,opportunity_ids=[uuid.UUID(value) for value in (ids if bad_source else ids[:1])],decision_proposal=None,public_summary="Inspecting a saved vacancy.")
                if counters["controller"]==3:
                    if preference_change == "target_roles":
                        proposal=DecisionProposal(kind="preference_tradeoff",question="Target registered nurse roles?",preference_field="role_queries",proposed_value=["registered nurse"],fact_question=None)
                    else:
                        proposal=DecisionProposal(kind="preference_tradeoff",question="Only show remote roles?",preference_field="work_arrangements",proposed_value=["remote"],fact_question=None)
                    return AgentAction(action="ask_user",query=None,opportunity_ids=[],decision_proposal=proposal,public_summary="A preference choice is needed.")
                return AgentAction(action="finish",query=None,opportunity_ids=[],decision_proposal=None,public_summary="Search complete with one assessed vacancy.")
            if output_model is ExtractOpportunityOutput:
                sid=user_payload["SOURCE_ID"]
                data={k:v for k,v in fixture.items() if k in OpportunityFields.model_fields}
                data["field_evidence"]=[{**x,"evidence":{**x["evidence"],"source_id":sid}} for x in data["field_evidence"]]
                data["field_evidence"].extend([{"field":field,"evidence":{"source_id":sid,"quote":quote}} for field,quote in [
                    ("employer","Northstar Learning Cooperative (SAMPLE)"),("opportunity_type","Internship"),("work_arrangement","Hybrid")]])
                data["salary_details"]["evidence_refs"]=[{**x,"source_id":sid} for x in data["salary_details"]["evidence_refs"]]
                data["requirements"]=[{**r,"evidence":{**r["evidence"],"source_id":sid}} for r in data["requirements"]]
                if sid == rejected_source_id:
                    data["field_evidence"][0]["evidence"]["quote"] = "This quote is fabricated and absent from the saved source"
                return ExtractOpportunityOutput(page_type="individual_job",opportunity=OpportunityFields.model_validate(data))
            if output_model.__name__ == "RoleAlignmentOutput":
                counters["role_refresh"] += 1
                assert user_payload["TARGET_ROLES"] == ["registered nurse"]
                assert user_payload["JOB"]["title"] == fixture["title"]
                assert "SOURCE_TEXT" in user_payload
                return output_model(role_alignment="unrelated",role_explanation="A data analyst internship is unrelated to the approved nursing target.")
            assert output_model is AssessOpportunityOutput
            req=fixture["requirements"][0]
            python_fact=next(f for f in confirmed["facts"] if f["label"]=="Python")
            return AssessOpportunityOutput(requirement_matches=[RequirementMatch(requirement_id=req["id"],status="supported",profile_fact_ids=[python_fact["id"]],explanation="Confirmed sample profile lists basic Python.")],
                role_alignment="direct",role_explanation="The title matches the confirmed target role.",experience_alignment="unknown")

    class FakeTavily:
        def __init__(self,*args,**kwargs): self.reserve_search=kwargs.get("reserve_search_attempt"); self.reserve_extract=kwargs.get("reserve_extract_attempt")
        async def search(self,query,max_results):
            assert self.reserve_search()
            results=[SearchResult(str(uuid.UUID(fixture["source_id"])),fixture["title"],fixture["canonical_url"],SOURCE_TEXT,1)]
            if bad_source:
                results.insert(0,SearchResult(rejected_source_id,"Unverifiable listing","https://example.invalid/bad-listing",SOURCE_TEXT,2))
            return SearchOutcome(results,"fixture-request",1)
        async def extract_saved(self,result_ids,saved_results):
            assert self.reserve_extract()
            return ExtractOutcome([ExtractedPage(saved_results[sid].id,saved_results[sid].url,SOURCE_TEXT) for sid in result_ids],[],"fixture-extract")

    monkeypatch.setattr("app.runner.GeminiAdapter",FakeGemini)
    monkeypatch.setattr("app.runner.TavilyAdapter",FakeTavily)
    body=RunCreateInput(profile_id=uuid.UUID(sample["id"]),profile_version=1,preference_id=uuid.UUID(prefs["id"]),preference_version=1)
    queued=_drain(_route(app,"/api/runs","POST")(body,request(),BackgroundTasks(),idempotency_key="synthetic-flow-001"))
    _drain(app.state.run_segment(queued["id"]))
    running=_drain(_route(app,"/api/runs/{run_id}","GET")(uuid.UUID(queued["id"]),request("GET")))
    assert running["state"]=="awaiting_decision" and running["search_call_count"]==1 and running["extract_call_count"]==1, running.get("last_error")
    opportunities=_drain(_route(app,"/api/runs/{run_id}/opportunities","GET")(uuid.UUID(queued["id"]),request("GET")))
    assert len(opportunities["items"])==1 and opportunities["items"][0]["assessment"]["score"] is not None
    if bad_source:
        with connect(app.state.db_path) as conn:
            rejected=json.loads(conn.execute("SELECT payload_json FROM sources WHERE id=?",(rejected_source_id,)).fetchone()["payload_json"])
        assert rejected["inspection_state"] == "evidence_rejected"
        assert running["source_progress"]["inspected_count"] == 2
    initial_bucket = opportunities["items"][0]["assessment"]["bucket"]
    assert "SAMPLE Data Analyst Intern" in opportunities["items"][0]["source_excerpt"]

    awaiting=_drain(_route(app,"/api/runs/{run_id}","GET")(uuid.UUID(queued["id"]),request("GET")))
    decision_id=uuid.UUID(awaiting["pending_decision_id"])
    answer_body=DecisionAnswerInput(expected_state_version=running["state_version"],option_id="apply",answer_note=None)
    resumed=_drain(_route(app,"/api/decisions/{decision_id}/answer","POST")(decision_id,answer_body,request(),BackgroundTasks(),Response()))
    assert resumed["state"]=="queued" and resumed["preference_version"]==2
    _drain(app.state.run_segment(queued["id"]))
    final=_drain(_route(app,"/api/runs/{run_id}","GET")(uuid.UUID(queued["id"]),request("GET")))
    assert final["state"]=="completed" and final["action_count"]==4
    assert final["search_call_count"]==running["search_call_count"]==1
    assert final["extract_call_count"]==running["extract_call_count"]==1

    result=_drain(_route(app,"/api/runs/{run_id}/opportunities","GET")(uuid.UUID(queued["id"]),request("GET")))
    assert result["items"], "resumed run must return its saved opportunity"
    for item in result["items"]:
        assessment=item["assessment"]
        assert assessment is not None, "every resumed result must be assessed under current preferences"
        assert assessment["profile_version"] == 1 and assessment["preference_version"] == 2
        if preference_change == "target_roles":
            assert assessment["role_alignment"] == "unrelated", "changed targets must not retain a cached direct match"
            assert assessment["score_components"]["role"] == 0
            assert assessment["bucket"] == initial_bucket, "changing role fit must not erase unrelated eligibility uncertainties"
        else:
            assert assessment["bucket"] == "excluded", "hybrid job must be excluded after remote-only preference approval"
            assert assessment["role_alignment"] == "direct" and assessment["score_components"]["role"] == 25
        assert assessment["score"] is not None and 0 <= assessment["score"] <= 100
        assert assessment["score"] == sum(assessment["score_components"].values())
        assert "SAMPLE Data Analyst Intern" in item["source_excerpt"], "reassessment must retain the saved source evidence"
    with connect(app.state.db_path) as conn:
        versions=conn.execute("SELECT preference_version FROM assessments WHERE opportunity_id=? ORDER BY preference_version",(result["items"][0]["opportunity"]["id"],)).fetchall()
    assert [row["preference_version"] for row in versions] == [1, 2], "the prior assessment is retained alongside its refreshed snapshot"
    expected_refreshes = 1 if preference_change == "target_roles" else 0
    assert counters["role_refresh"] == expected_refreshes
    assert final["llm_call_count"] == running["llm_call_count"] + 1 + expected_refreshes
    opportunity_id=uuid.UUID(result["items"][0]["opportunity"]["id"])
    from app.application_routes import DraftCreateInput, DraftEditInput
    draft=_drain(_route(app,"/api/drafts","POST")(DraftCreateInput(opportunity_id=opportunity_id,profile_id=uuid.UUID(sample["id"]),profile_version=1),request(),idempotency_key=None))
    edited=_drain(_route(app,"/api/drafts/{draft_id}","PUT")(uuid.UUID(draft["id"]),DraftEditInput(expected_version=1,edited_text="Edited application draft for the sample role."),request()))
    assert edited["version"]==2 and edited["edited_text"]=="Edited application draft for the sample role."
