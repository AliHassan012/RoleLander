from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.assessment import assess_opportunity
from app.config import Settings
from app.db import connect, initialize
from app.main import create_app
from app.models import AssessOpportunityOutput, Preferences, Profile, Opportunity, RequirementMatch
from app.sessions import create_session
from starlette.background import BackgroundTasks
from starlette.requests import Request
from pydantic import ValidationError


FIXTURES=Path(__file__).parent/"fixtures"
SOURCE_TEXT="SAMPLE Data Analyst Intern. Hybrid in Islamabad, Pakistan. Basic Python familiarity preferred. PKR 30,000–50,000 per month."


def _fixture_context(user_id):
    p=json.loads((FIXTURES/"sample_profile.json").read_text(encoding="utf-8"));p.pop("sample_label")
    profile=Profile.model_validate(p)
    o=json.loads((FIXTURES/"sample_opportunity.json").read_text(encoding="utf-8"));o.pop("sample_label")
    o.update({"user_id":str(user_id),"run_id":str(uuid.uuid4())})
    opportunity=Opportunity.model_validate(o)
    preferences=Preferences(id=uuid.uuid4(),version=1,country_code="PK",country_scope="open_to_country",role_queries=["data analyst"],
        opportunity_types=["internship"],seniority=["any"],work_arrangements=["hybrid"],location_mode="anywhere",preferred_cities=[],
        preferred_cities_policy="preferred",excluded_cities=[],proximity=None,paid_policy="any",right_to_work="unknown",needs_sponsorship="unknown",
        availability_note=None,career_interests=[],preferred_industries=[],excluded_industries=[],employment_commitment="either",relocation_policy="unknown",
        company_search_mode="watchlist_only",company_size_preferences=[],company_stage_preferences=[],compensation_preferences=[dict(opportunity_type="internship",preferred_amount=40000,minimum_amount=30000,currency="PKR",period="month",minimum_policy="preferred",include_undisclosed=True,allow_unpaid=True,basis="gross",component="base")])
    model=AssessOpportunityOutput(requirement_matches=[RequirementMatch(requirement_id=opportunity.requirements[0].id,status="supported",profile_fact_ids=[profile.facts[0].id],explanation="Basic Python appears in the profile.")],role_alignment="direct",role_explanation="Target role matches.",experience_alignment="unknown")
    assessment=assess_opportunity(opportunity=opportunity,profile=profile,preferences=preferences,model_assessment=model,source_text=SOURCE_TEXT)
    return profile,opportunity,assessment


def _settings(tmp_path):
    return Settings(gemini_api_key=None,gemini_model="test",tavily_api_key=None,data_dir=tmp_path,port=8000,cookie_secure=False,
        app_origin="http://testserver",free_only=True,storage_persistent=False,google_client_id=None,google_client_secret=None,
        google_redirect_uri="http://testserver/callback",google_gmail_redirect_uri="http://testserver/gmail",credential_encryption_key=None,
        personal_data_mode="synthetic",scheduler_enabled=False,scheduler_tick_seconds=60,build_id="test")


def _endpoint(app,path,method):
    routes=[]
    for route in app.routes:
        routes.extend(getattr(getattr(route,"original_router",None),"routes",[]));routes.append(route)
    return next(route.endpoint for route in routes if getattr(route,"path",None)==path and method in getattr(route,"methods",set()))


def _request(app,cookie,csrf,method="POST"):
    return Request({"type":"http","method":method,"path":"/api/test","headers":[(b"cookie",f"rolelander_session={cookie}".encode()),(b"origin",b"http://testserver"),(b"x-csrf-token",csrf.encode())],"app":app})


def _run(coro):
    try: coro.send(None)
    except StopIteration as done: return done.value
    raise AssertionError("Route unexpectedly awaited external I/O")


def test_template_draft_preserves_fact_kind_and_version_review_and_idempotency(tmp_path):
    app=create_app(_settings(tmp_path));initialize(app.state.db_path)
    user_id=str(uuid.uuid4());now=datetime.now(timezone.utc)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO users(id,google_issuer,google_subject,verified_email,created_at) VALUES(?,?,?,?,?)",(user_id,"https://accounts.google.com",uuid.uuid4().hex,"synthetic@example.invalid",now.isoformat()))
    cookie,csrf=create_session(app.state.db_path,user_id);request=_request(app,cookie,csrf)
    profile,opportunity,assessment=_fixture_context(user_id)
    run_id=opportunity.run_id
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO profiles(id,version,user_id,confirmed_at,payload_json) VALUES(?,?,?,?,?)",(str(profile.id),profile.version,user_id,profile.confirmed_at.isoformat(),json.dumps(profile.model_dump(mode="json"))))
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at) VALUES(?,?,?,?,?,?)",(str(run_id),user_id,"completed",1,"{}",now.isoformat()))
        conn.execute("INSERT INTO sources(id,user_id,run_id,url,payload_json) VALUES(?,?,?,?,?)",(str(opportunity.source_id),user_id,str(run_id),str(opportunity.canonical_url),"{}"))
        conn.execute("INSERT INTO opportunities(id,user_id,stable_source_key,canonical_url,payload_json) VALUES(?,?,?,?,?)",(str(opportunity.id),user_id,opportunity.stable_source_key,str(opportunity.canonical_url),json.dumps(opportunity.model_dump(mode="json"))))
        conn.execute("INSERT INTO assessments(id,opportunity_id,profile_id,profile_version,preference_id,preference_version,payload_json) VALUES(?,?,?,?,?,?,?)",(str(assessment.id),str(opportunity.id),str(profile.id),profile.version,str(uuid.uuid4()),1,json.dumps(assessment.model_dump(mode="json"))))
    from app.application_routes import DraftCreateInput,DraftEditInput,DraftReviewInput
    create=_endpoint(app,"/api/drafts","POST")
    body=DraftCreateInput(opportunity_id=opportunity.id,profile_id=profile.id,profile_version=profile.version)
    first=_run(create(body,request,idempotency_key="draft-request-0001"))
    replay=_run(create(body,request,idempotency_key="draft-request-0001"))
    assert first["id"]==replay["id"] and first["version"]==1
    listing=_run(_endpoint(app,"/api/drafts","GET")(_request(app,cookie,csrf,method="GET")))
    assert listing["items"][0]["id"]==first["id"] and listing["items"][0]["version"]==1
    draft_text="\n".join(sentence["text"] for sentence in first["sentences"])
    assert "project" in draft_text.casefold() and "professional employment" not in draft_text.casefold()
    assert all(sentence["profile_fact_ids"] for sentence in first["sentences"] if sentence["is_personal_claim"])
    assert any(sentence["needs_user_input"] for sentence in first["sentences"])
    edit=_endpoint(app,"/api/drafts/{draft_id}","PUT")
    changed=_run(edit(first["id"],DraftEditInput(expected_version=1,edited_text="Dear Team, I am applying. Regards, Sam."),request))
    assert changed["version"]==2 and changed["reviewed_at"] is None
    review=_endpoint(app,"/api/drafts/{draft_id}/review","POST")
    with pytest.raises(HTTPException) as stale:
        _run(review(first["id"],DraftReviewInput(expected_version=1),request))
    assert stale.value.status_code==409
    reviewed=_run(review(first["id"],DraftReviewInput(expected_version=2),request))
    assert reviewed["reviewed_at"] is not None
    download=_run(_endpoint(app,"/api/drafts/{draft_id}/download","GET")(first["id"],_request(app,cookie,csrf,method="GET")))
    assert "reviewed" in download.body.decode() and "Application email" not in download.body.decode()
    changed_again=_run(edit(first["id"],DraftEditInput(expected_version=2,edited_text="Revised version"),request))
    assert changed_again["version"]==3 and changed_again["reviewed_at"] is None


def test_tracker_requires_owner_and_only_explicit_patch_changes_applied_status(tmp_path):
    app=create_app(_settings(tmp_path));initialize(app.state.db_path)
    user_id=str(uuid.uuid4());now=datetime.now(timezone.utc)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO users(id,google_issuer,google_subject,verified_email,created_at) VALUES(?,?,?,?,?)",(user_id,"https://accounts.google.com",uuid.uuid4().hex,"synthetic@example.invalid",now.isoformat()))
    cookie,csrf=create_session(app.state.db_path,user_id);request=_request(app,cookie,csrf)
    _profile,opportunity,_assessment=_fixture_context(user_id)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO opportunities(id,user_id,stable_source_key,canonical_url,payload_json) VALUES(?,?,?,?,?)",(str(opportunity.id),user_id,opportunity.stable_source_key,str(opportunity.canonical_url),json.dumps(opportunity.model_dump(mode="json"))))
    from app.application_routes import SavedCreateInput,SavedEditInput
    create=_endpoint(app,"/api/saved","POST")
    saved=_run(create(SavedCreateInput(opportunity_id=opportunity.id),request))
    assert saved["status"]=="interested"
    edit=_endpoint(app,"/api/saved/{saved_id}","PATCH")
    applied=_run(edit(saved["id"],SavedEditInput(expected_updated_at=saved["updated_at"],status="applied",note="Submitted by me"),request))
    assert applied["status"]=="applied" and applied["note"]=="Submitted by me"
    with pytest.raises(HTTPException) as stale:
        _run(edit(saved["id"],SavedEditInput(expected_updated_at=saved["updated_at"],status="rejected",note=None),request))
    assert stale.value.status_code==409
