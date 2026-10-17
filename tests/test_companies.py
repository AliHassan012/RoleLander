from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import httpx
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.db import connect, initialize
from app.main import create_app
from app.sessions import create_session


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("Route unexpectedly awaited external I/O")


class _AsyncClient:
    def __init__(self, handler):
        self.handler=handler
    async def get(self,url,params=None,headers=None):
        request=httpx.Request("GET",httpx.URL(url).copy_merge_params(params or {}),headers=headers)
        return await self.handler(request)


def _settings(tmp_path):
    return Settings(gemini_api_key=None,gemini_model="test",tavily_api_key=None,data_dir=tmp_path,port=8000,cookie_secure=False,
        app_origin="http://testserver",free_only=True,storage_persistent=False,google_client_id=None,google_client_secret=None,
        google_redirect_uri="http://testserver/callback",google_gmail_redirect_uri="http://testserver/gmail",credential_encryption_key=None,
        personal_data_mode="synthetic",scheduler_enabled=False,scheduler_tick_seconds=60,build_id="test")


def _endpoint(app,path,method):
    routes=[]
    for route in app.routes:
        routes.extend(getattr(getattr(route,"original_router",None),"routes",[]))
        routes.append(route)
    return next(route.endpoint for route in routes if getattr(route,"path",None)==path and method in getattr(route,"methods",set()))


def _context(app, user_id=None):
    user_id=user_id or str(uuid.uuid4());now=datetime.now(timezone.utc)
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO users(id,google_issuer,google_subject,verified_email,created_at) VALUES(?,?,?,?,?)",
            (user_id,"https://accounts.google.com",uuid.uuid4().hex,"synthetic@example.invalid",now.isoformat()))
        run_id=str(uuid.uuid4())
        conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at) VALUES(?,?,?,?,?,?)",
            (run_id,user_id,"completed",1,"{}",now.isoformat()))
    cookie,csrf=create_session(app.state.db_path,user_id)
    request=Request({"type":"http","method":"POST","path":"/api/companies","headers":[(b"cookie",f"rolelander_session={cookie}".encode()),(b"origin",b"http://testserver"),(b"x-csrf-token",csrf.encode())],"app":app})
    source_id=str(uuid.uuid4())
    payload={"document":{"id":source_id,"user_id":user_id,"run_id":run_id,"url":"https://acme.example.com/about","title":"Acme about","source_kind":"extracted_page","body_text":"Acme.example.com is the official company website. Careers: https://careers.acme.example.com/jobs","truncated":False,"retrieved_at":now.isoformat(),"published_at":None,"extraction_status":"retrieved","provider_request_id":None},"inspection_state":"inspected"}
    with connect(app.state.db_path) as conn:
        conn.execute("INSERT INTO sources(id,user_id,run_id,url,payload_json) VALUES(?,?,?,?,?)",
            (source_id,user_id,run_id,"https://acme.example.com/about",json.dumps(payload)))
    return user_id,request,source_id


def _create_input(name,domain,careers_url,source_id,quote=None):
    from app.company_routes import CompanyCreateInput
    return CompanyCreateInput(name=name,canonical_domain=domain,careers_url=careers_url,
        evidence={"source_id":source_id,"quote":quote or "Acme.example.com is the official company website."})


def test_company_requires_owned_exact_evidence_and_human_approval(tmp_path):
    app=create_app(_settings(tmp_path));initialize(app.state.db_path)
    user_id,request,source_id=_context(app)
    add=_endpoint(app,"/api/companies","POST")
    body=_create_input("Acme","acme.example.com","https://careers.acme.example.com/jobs",source_id)
    created=_run(add(body,request))
    assert created["membership"]["status"]=="suggested" and created["company"]["adapter"]=="public_search"
    approve=_endpoint(app,"/api/companies/{company_id}/approve","POST")
    from app.company_routes import CompanyApproveInput
    approved=_run(approve(created["company"]["id"],CompanyApproveInput(expected_version=1,preferred=True,priority=50),request))
    assert approved["membership"]["status"]=="approved" and approved["membership"]["preferred"] is True
    listing=_run(_endpoint(app,"/api/companies","GET")(request))
    assert listing["items"][0]["company"]["id"]==created["company"]["id"]
    outsider_request=_context(app)[1]
    with pytest.raises(HTTPException) as missing:
        _run(_endpoint(app,"/api/companies/{company_id}/approve","POST")(created["company"]["id"],CompanyApproveInput(expected_version=2),outsider_request))
    assert missing.value.status_code==404


def test_company_evidence_must_be_saved_owned_and_supported(tmp_path):
    app=create_app(_settings(tmp_path));initialize(app.state.db_path)
    user_id,request,source_id=_context(app)
    add=_endpoint(app,"/api/companies","POST")
    with pytest.raises(HTTPException) as unsupported:
        _run(add(_create_input("Acme","acme.example.com","https://careers.acme.example.com/jobs",source_id,"A quote unrelated to the company"),request))
    assert unsupported.value.status_code==422
    with pytest.raises(HTTPException) as insecure:
        _run(add(_create_input("Acme","acme.example.com","http://careers.acme.example.com/jobs",source_id),request))
    assert insecure.value.status_code==422


def test_active_watchlist_is_capped_at_ten_and_blocking_is_explicit(tmp_path):
    app=create_app(_settings(tmp_path));initialize(app.state.db_path)
    user_id,request,source_id=_context(app)
    add=_endpoint(app,"/api/companies","POST");approve=_endpoint(app,"/api/companies/{company_id}/approve","POST")
    from app.company_routes import CompanyApproveInput, CompanyUpdateInput
    ids=[]
    for n in range(10):
        domain="acme.example.com" if n==0 else f"acme{n}.example.com";careers=f"https://careers.{domain}/jobs"
        quote=("Acme.example.com" if n==0 else f"Acme{n}.example.com")+f" is the official company website. Careers: {careers}"
        actual_source=source_id
        if n:
            now=datetime.now(timezone.utc);src=str(uuid.uuid4())
            with connect(app.state.db_path) as conn:
                run_id=conn.execute("SELECT id FROM runs WHERE user_id=?",(user_id,)).fetchone()["id"]
                payload={"document":{"id":src,"user_id":user_id,"run_id":run_id,"url":f"https://{domain}/about","title":"Company","source_kind":"extracted_page","body_text":quote,"truncated":False,"retrieved_at":now.isoformat(),"published_at":None,"extraction_status":"retrieved","provider_request_id":None}}
                conn.execute("INSERT INTO sources(id,user_id,run_id,url,payload_json) VALUES(?,?,?,?,?)",(src,user_id,run_id,f"https://{domain}/about",json.dumps(payload)))
            actual_source=src
        created=_run(add(_create_input(f"Acme {n}",domain,careers,actual_source,quote),request))
        approved=_run(approve(created["company"]["id"],CompanyApproveInput(expected_version=1),request));ids.append(approved["company"]["id"])
    domain="extra.example.com";src=str(uuid.uuid4());now=datetime.now(timezone.utc);careers=f"https://careers.{domain}/jobs"
    body=f"{domain} is the official company website. Careers: {careers}"
    with connect(app.state.db_path) as conn:
        run_id=conn.execute("SELECT id FROM runs WHERE user_id=?",(user_id,)).fetchone()["id"]
        payload={"document":{"id":src,"user_id":user_id,"run_id":run_id,"url":f"https://{domain}/about","title":"Company","source_kind":"extracted_page","body_text":body,"truncated":False,"retrieved_at":now.isoformat(),"published_at":None,"extraction_status":"retrieved","provider_request_id":None}}
        conn.execute("INSERT INTO sources(id,user_id,run_id,url,payload_json) VALUES(?,?,?,?,?)",(src,user_id,run_id,f"https://{domain}/about",json.dumps(payload)))
    extra=_run(add(_create_input("Extra",domain,careers,src,body),request))
    with pytest.raises(HTTPException) as capped:
        _run(approve(extra["company"]["id"],CompanyApproveInput(expected_version=1),request))
    assert capped.value.status_code==409
    patch=_endpoint(app,"/api/companies/{company_id}","PATCH")
    blocked=_run(patch(ids[0],CompanyUpdateInput(expected_version=2,status="blocked"),request))
    assert blocked["membership"]["status"]=="blocked" and blocked["membership"]["approved_at"] is None


def test_public_greenhouse_and_lever_boards_use_fixed_hosts_and_safe_posting_links():
    from app.company_sources import fetch_company_feed
    from app.models import Company
    now=datetime.now(timezone.utc)
    greenhouse=Company(id=uuid.uuid4(),name="Example",canonical_domain="example.com",careers_url="https://boards.greenhouse.io/example",
        adapter="greenhouse",adapter_identifier="example",adapter_region="global",evidence_refs=[],verified_at=now)
    seen=[]
    async def greenhouse_handler(request):
        seen.append(str(request.url))
        return httpx.Response(200,json={"jobs":[
            {"id":1,"title":"Analyst","absolute_url":"https://boards.greenhouse.io/example/jobs/1","location":{"name":"Remote"},"updated_at":"2026-10-17T00:00:00Z","content":"<p>Role</p>"},
            {"id":2,"title":"Bad link","absolute_url":"http://evil.example/job"}],"meta":{"total":2}})
    gh=_run(fetch_company_feed(greenhouse,_AsyncClient(greenhouse_handler)))
    lever=greenhouse.model_copy(update={"adapter":"lever","adapter_identifier":"demo","adapter_region":"eu","careers_url":"https://jobs.eu.lever.co/demo"})
    async def lever_handler(request):
        seen.append(str(request.url))
        return httpx.Response(200,json=[{"id":"posting-1","text":"Data Intern","hostedUrl":"https://jobs.eu.lever.co/demo/posting-1","categories":{"location":"Lahore"},"createdAt":1792195200000,"descriptionPlain":"Details"}])
    lv=_run(fetch_company_feed(lever,_AsyncClient(lever_handler)))
    assert gh.status=="complete" and gh.pages_fetched==1 and len(gh.postings)==1
    assert gh.postings[0]["posting_id"]=="1" and gh.postings[0]["url"].endswith("/jobs/1")
    assert lv.status=="complete" and lv.postings[0]["title"]=="Data Intern"
    assert "api.eu.lever.co/v0/postings/demo" in seen[1]
    assert all(url.startswith("https://boards-api.greenhouse.io/") for url in seen[:1])


def test_public_board_truncation_failures_and_unverified_sources_are_visible():
    from app.company_sources import fetch_company_feed
    from app.models import Company
    company=Company(id=uuid.uuid4(),name="Example",canonical_domain="example.com",careers_url="https://boards.greenhouse.io/example",
        adapter="greenhouse",adapter_identifier="example",adapter_region="global",evidence_refs=[],verified_at=datetime.now(timezone.utc))
    requests=[]
    async def handler(request):
        requests.append(request)
        return httpx.Response(200,json={"jobs":[{"id":i,"title":f"Role {i}","absolute_url":f"https://boards.greenhouse.io/example/jobs/{i}"} for i in range(50)]})
    result=_run(fetch_company_feed(company,_AsyncClient(handler)))
    assert result.status=="partial" and result.pages_fetched==4 and len(result.postings)<=200
    assert len(requests)==4
    unverified=company.model_copy(update={"verified_at":None})
    failed=_run(fetch_company_feed(unverified,_AsyncClient(handler)))
    assert failed.status=="failed" and "verified" in failed.error.casefold()


def test_company_board_check_requires_approval_and_caches_one_hour(monkeypatch,tmp_path):
    app=create_app(_settings(tmp_path));initialize(app.state.db_path)
    user_id,request,source_id=_context(app)
    quote="Acme.example.com official careers board: https://boards.greenhouse.io/acme"
    with connect(app.state.db_path) as conn:
        row=conn.execute("SELECT payload_json FROM sources WHERE id=?",(source_id,)).fetchone();payload=json.loads(row["payload_json"])
        payload["document"]["body_text"]=quote
        conn.execute("UPDATE sources SET payload_json=? WHERE id=?",(json.dumps(payload),source_id))
    from app.company_routes import CompanyApproveInput, CompanyCreateInput
    company=_run(_endpoint(app,"/api/companies","POST")(CompanyCreateInput(name="Acme",canonical_domain="acme.example.com",careers_url="https://boards.greenhouse.io/acme",evidence={"source_id":source_id,"quote":quote}),request))
    check=_endpoint(app,"/api/companies/{company_id}/check","POST")
    with pytest.raises(HTTPException) as unapproved:
        _run(check(company["company"]["id"],request))
    assert unapproved.value.status_code==409
    _run(_endpoint(app,"/api/companies/{company_id}/approve","POST")(company["company"]["id"],CompanyApproveInput(expected_version=1),request))
    from app import company_sources
    calls=[]
    async def fake_fetch(_company):
        calls.append(_company.id)
        return company_sources.FeedOutcome("complete",[{"posting_id":"1","title":"Analyst","url":"https://boards.greenhouse.io/acme/jobs/1","location":None,"updated_at":None,"description":"Details"}],1)
    monkeypatch.setattr(company_sources,"fetch_company_feed",fake_fetch)
    first=_run(check(company["company"]["id"],request))
    second=_run(check(company["company"]["id"],request))
    assert first["status"]=="complete" and first["cached"] is False
    assert second["cached"] is True and second["postings"]==first["postings"] and len(calls)==1
