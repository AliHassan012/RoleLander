from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.db import connect
from app.models import Company, EvidenceRef, UserCompany, normalize_evidence_text
from app.sessions import current_session, require_csrf

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if not value or len(value) > 253 or ":" in value or "/" in value or "@" in value:
        raise ValueError("Enter a domain name without a URL or credentials")
    if not re.fullmatch(r"(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
        raise ValueError("Enter a valid public domain name")
    return value


def _host(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in {None, 443}:
        raise HTTPException(status_code=422, detail="Company evidence and careers links must use HTTPS")
    return parts.hostname.lower().rstrip(".")


class CompanyCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    canonical_domain: str = Field(min_length=3, max_length=253)
    careers_url: HttpUrl
    evidence: EvidenceRef

    @field_validator("name")
    @classmethod
    def clean_name(cls,value):
        value=value.strip()
        if not value: raise ValueError("Company name cannot be blank")
        return value

    @field_validator("canonical_domain")
    @classmethod
    def valid_domain(cls, value):
        return _domain(value)


class CompanyUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    status: Literal["suggested", "blocked"] | None = None
    preferred: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


class CompanyApproveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    preferred: bool = False
    priority: int = Field(default=0, ge=0, le=100)


def _load_owned(conn, company_id: str, user_id: str):
    row = conn.execute("""SELECT c.*,uc.status,uc.preferred,uc.priority,uc.origin,uc.approved_at,uc.version
        FROM user_companies uc JOIN companies c ON c.id=uc.company_id
        WHERE uc.user_id=? AND uc.company_id=?""", (user_id, company_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Company not found")
    company = Company(id=uuid.UUID(row["id"]), name=row["name"], canonical_domain=row["canonical_domain"],
        careers_url=row["careers_url"], adapter=row["adapter"], adapter_identifier=row["adapter_identifier"], adapter_region=row["adapter_region"],
        evidence_refs=json.loads(row["evidence_refs_json"]), verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None)
    user_company = UserCompany(user_id=uuid.UUID(user_id), company_id=uuid.UUID(row["id"]), status=row["status"],
        preferred=bool(row["preferred"]), priority=row["priority"], origin=row["origin"],
        approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None, version=row["version"])
    return {"company":company.model_dump(mode="json"),"membership":user_company.model_dump(mode="json")}


def _validate_identity_evidence(conn, user_id: str, ref: EvidenceRef, domain: str, careers_url: str) -> None:
    row = conn.execute("SELECT payload_json FROM sources WHERE id=? AND user_id=?", (str(ref.source_id), user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail="Company identity evidence must come from one of your saved public sources")
    payload = json.loads(row["payload_json"])
    document = payload.get("document", {})
    body = document.get("body_text", "")
    if document.get("source_kind") not in {"extracted_page", "employer_api", "job_feed"} or document.get("extraction_status") != "retrieved" or document.get("truncated"):
        raise HTTPException(status_code=422, detail="Use a complete, successfully retrieved public company or careers page")
    if normalize_evidence_text(ref.quote) not in normalize_evidence_text(body):
        raise HTTPException(status_code=422, detail="Company identity quote was not found in the saved source")
    quote = normalize_evidence_text(ref.quote).casefold()
    source_url = str(document.get("url", ""))
    source_host = _host(source_url)
    career_host = _host(careers_url)
    if domain not in quote and domain not in source_host:
        raise HTTPException(status_code=422, detail="Evidence must connect the company to its canonical domain")
    same_company_host = career_host == domain or career_host.endswith("." + domain)
    ats_host = career_host in {"boards.greenhouse.io", "jobs.lever.co", "jobs.eu.lever.co"}
    source_is_official = source_host == domain or source_host.endswith("." + domain) or (ats_host and source_host == career_host)
    if not source_is_official:
        raise HTTPException(status_code=422, detail="Identity evidence must come from the official company site or its cited ATS board")
    if not same_company_host and not ats_host:
        raise HTTPException(status_code=422, detail="Careers URL must use the company domain or a known public ATS")
    if ats_host and (careers_url.casefold() not in quote or domain not in quote):
        raise HTTPException(status_code=422, detail="Evidence must link the company domain to its exact public ATS board")


@router.get("/api/companies")
async def list_companies(request: Request):
    session = current_session(request)
    with connect(request.app.state.db_path) as conn:
        rows = conn.execute("SELECT company_id FROM user_companies WHERE user_id=? ORDER BY priority DESC,company_id", (session["user_id"],)).fetchall()
        items = [_load_owned(conn, row["company_id"], session["user_id"]) for row in rows]
    return {"items":items}


@router.post("/api/companies", status_code=201)
async def add_company(body: CompanyCreateInput, request: Request):
    session=current_session(request); require_csrf(request, session); user_id=session["user_id"]
    careers_url=str(body.careers_url)
    company_host=_host("https://"+body.canonical_domain)
    career_host=_host(careers_url)
    if not (career_host==company_host or career_host.endswith("."+company_host) or career_host in {"boards.greenhouse.io","jobs.lever.co","jobs.eu.lever.co"}):
        raise HTTPException(status_code=422, detail="Careers URL must use the company domain or a known public ATS")
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _validate_identity_evidence(conn,user_id,body.evidence,company_host,careers_url)
        existing=conn.execute("""SELECT c.id FROM companies c JOIN user_companies uc ON uc.company_id=c.id
            WHERE c.canonical_domain=? AND uc.user_id=?""",(company_host,user_id)).fetchone()
        if existing:
            company_id=existing["id"]
        else:
            company_id=str(uuid.uuid4());now=_now().isoformat()
            careers_parts=urlsplit(careers_url)
            adapter="greenhouse" if careers_parts.hostname=="boards.greenhouse.io" else "lever" if careers_parts.hostname in {"jobs.lever.co","jobs.eu.lever.co"} else "public_search"
            path=careers_parts.path.strip("/").split("/")
            identifier=path[0] if adapter in {"greenhouse","lever"} and path and path[0] else None
            region="eu" if careers_parts.hostname=="jobs.eu.lever.co" else "global" if adapter in {"greenhouse","lever"} else None
            conn.execute("""INSERT INTO companies(id,name,canonical_domain,careers_url,adapter,adapter_identifier,adapter_region,evidence_refs_json,verified_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",(company_id,body.name.strip(),company_host,careers_url,adapter,identifier,region,json.dumps([body.evidence.model_dump(mode="json")]),now))
        conn.execute("""INSERT OR IGNORE INTO user_companies(user_id,company_id,status,preferred,priority,origin,approved_at,version)
            VALUES(?,?, 'suggested',0,0,'user',NULL,1)""",(user_id,company_id))
        row=conn.execute("SELECT 1 FROM user_companies WHERE user_id=? AND company_id=?",(user_id,company_id)).fetchone()
        if row is None: raise HTTPException(status_code=409,detail="Company could not be added to the watchlist")
        result=_load_owned(conn,company_id,user_id)
        return result


@router.post("/api/companies/{company_id}/approve")
async def approve_company(company_id: uuid.UUID, body: CompanyApproveInput, request: Request):
    session=current_session(request);require_csrf(request,session);user_id=session["user_id"]
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current=_load_owned(conn,str(company_id),user_id)
        if current["membership"]["version"]!=body.expected_version: raise HTTPException(status_code=409,detail="Company approval is stale")
        if current["company"]["verified_at"] is None: raise HTTPException(status_code=409,detail="Verify company identity before approval")
        count=conn.execute("SELECT COUNT(*) AS n FROM user_companies WHERE user_id=? AND status='approved'",(user_id,)).fetchone()["n"]
        if current["membership"]["status"]!="approved" and count>=10: raise HTTPException(status_code=409,detail="The active watchlist is limited to ten companies")
        now=_now().isoformat()
        conn.execute("UPDATE user_companies SET status='approved',preferred=?,priority=?,approved_at=?,version=version+1 WHERE user_id=? AND company_id=?",
            (int(body.preferred),body.priority,now,user_id,str(company_id)))
        return _load_owned(conn,str(company_id),user_id)


@router.patch("/api/companies/{company_id}")
async def update_company(company_id: uuid.UUID, body: CompanyUpdateInput, request: Request):
    session=current_session(request);require_csrf(request,session);user_id=session["user_id"]
    if body.status is None and body.preferred is None and body.priority is None:
        raise HTTPException(status_code=422,detail="Choose a company setting to update")
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current=_load_owned(conn,str(company_id),user_id); membership=current["membership"]
        if membership["version"]!=body.expected_version: raise HTTPException(status_code=409,detail="Company update is stale")
        new_status=body.status or membership["status"]
        new_preferred=membership["preferred"] if body.preferred is None else body.preferred
        if new_status!="approved": new_preferred=False
        new_approved_at=membership["approved_at"] if new_status=="approved" else None
        conn.execute("""UPDATE user_companies SET status=?,approved_at=?,preferred=?,priority=COALESCE(?,priority),version=version+1
            WHERE user_id=? AND company_id=?""",(new_status,new_approved_at,int(new_preferred),body.priority,user_id,str(company_id)))
        return _load_owned(conn,str(company_id),user_id)


@router.post("/api/companies/{company_id}/check")
async def check_company_board(company_id: uuid.UUID, request: Request, refresh: bool = False):
    session=current_session(request);require_csrf(request,session);user_id=session["user_id"];db_path=request.app.state.db_path
    with connect(db_path) as conn:
        owned=_load_owned(conn,str(company_id),user_id)
        if owned["membership"]["status"]!="approved": raise HTTPException(status_code=409,detail="Approve this company before checking its board")
        company=Company.model_validate(owned["company"])
        if company.adapter not in {"greenhouse","lever"}: raise HTTPException(status_code=409,detail="This verified company uses unsupported-page discovery, which is not available yet")
        cached=conn.execute("SELECT fetched_at,status,payload_json FROM company_feed_cache WHERE company_id=?",(str(company_id),)).fetchone()
        if cached and not refresh:
            fetched_at=datetime.fromisoformat(cached["fetched_at"])
            if (_now()-fetched_at).total_seconds()<3600:
                return {"company_id":str(company_id),"checked_at":cached["fetched_at"],"status":cached["status"],**json.loads(cached["payload_json"]),"cached":True}
    from app.company_sources import fetch_company_feed
    outcome=await fetch_company_feed(company)
    result={"postings":outcome.postings,"pages_fetched":outcome.pages_fetched,"error":outcome.error,"cached":False}
    with connect(db_path) as conn:
        conn.execute("""INSERT INTO company_feed_cache(company_id,fetched_at,status,payload_json) VALUES(?,?,?,?)
            ON CONFLICT(company_id) DO UPDATE SET fetched_at=excluded.fetched_at,status=excluded.status,payload_json=excluded.payload_json""",
            (str(company_id),_now().isoformat(),outcome.status,json.dumps(result)))
    return {"company_id":str(company_id),"checked_at":_now().isoformat(),"status":outcome.status,**result}
