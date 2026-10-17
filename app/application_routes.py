from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.db import connect
from app.models import ApplicationDraft, DraftSentence, Opportunity, Profile, SavedOpportunity
from app.sessions import current_session, require_csrf

router=APIRouter()


class DraftCreateInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    opportunity_id: uuid.UUID
    profile_id: uuid.UUID
    profile_version: int=Field(ge=1)
    kind: Literal["email"]="email"


class DraftEditInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_version: int=Field(ge=1)
    edited_text: str=Field(min_length=1,max_length=10000)


class DraftReviewInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_version: int=Field(ge=1)


class SavedCreateInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    opportunity_id: uuid.UUID
    status: Literal["interested","preparing","applied","interview","rejected","dismissed"]="interested"
    note: str|None=Field(default=None,max_length=1000)


class SavedEditInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    expected_updated_at: datetime
    status: Literal["interested","preparing","applied","interview","rejected","dismissed"]
    note: str|None=Field(max_length=1000)


def _now(): return datetime.now(timezone.utc)


def _draft_text(draft: ApplicationDraft) -> str:
    body="\n\n".join(sentence.text for sentence in draft.sentences)
    return f"Subject: {draft.subject or ''}\n\n{body}" if draft.subject else body


def _load_draft(conn, draft_id: str, user_id: str) -> ApplicationDraft:
    row=conn.execute("SELECT payload_json FROM drafts WHERE id=? AND user_id=? ORDER BY version DESC LIMIT 1",(draft_id,user_id)).fetchone()
    if row is None: raise HTTPException(status_code=404,detail="Draft not found")
    return ApplicationDraft.model_validate_json(row["payload_json"])


def _validate_draft(draft: ApplicationDraft, profile: Profile, opportunity: Opportunity) -> None:
    fact_ids={fact.id for fact in profile.facts if fact.confirmed}
    requirement_ids={req.id for req in opportunity.requirements}
    for sentence in draft.sentences:
        if sentence.is_personal_claim and not sentence.profile_fact_ids:
            raise HTTPException(status_code=422,detail="Personal claims need confirmed profile facts")
        if any(ref not in fact_ids for ref in sentence.profile_fact_ids):
            raise HTTPException(status_code=422,detail="Draft references an unconfirmed profile fact")
        if any(ref not in requirement_ids for ref in sentence.requirement_ids):
            raise HTTPException(status_code=422,detail="Draft references an unknown job requirement")


def _make_template(draft_id, user_id, opportunity, profile, assessment, now):
    role=opportunity.title or "[Role title]"
    employer=opportunity.employer or "[Employer name]"
    sentences=[
        DraftSentence(text="Dear Hiring Team,",profile_fact_ids=[],requirement_ids=[],is_personal_claim=False,needs_user_input=False),
        DraftSentence(text=f"I am writing to apply for {role} at {employer}.",profile_fact_ids=[],requirement_ids=[],is_personal_claim=False,needs_user_input=False),
    ]
    matching={str(item.requirement_id):item for item in (assessment.requirement_matches if assessment else [])}
    confirmed=[fact for fact in profile.facts if fact.confirmed][:5]
    for fact in confirmed:
        if fact.kind=="project":
            claim=f"A project in my profile is {fact.label}: {fact.value}"
        elif fact.kind=="skill":
            level=f" ({fact.level} level)" if fact.level and fact.level!="unknown" else ""
            claim=f"My profile lists {fact.label}{level}: {fact.value}"
        elif fact.kind=="employment":
            claim=f"My profile lists this employment experience: {fact.label} — {fact.value}"
        elif fact.kind=="education":
            claim=f"My profile lists this education: {fact.label} — {fact.value}"
        else:
            claim=f"My profile includes: {fact.label} — {fact.value}"
        if len(claim)>1950: continue
        requirement_refs=[uuid.UUID(req_id) for req_id,match in matching.items() if fact.id in match.profile_fact_ids and match.status in {"supported","partial"}]
        sentences.append(DraftSentence(text=claim,profile_fact_ids=[fact.id],requirement_ids=requirement_refs,is_personal_claim=True,needs_user_input=False))
    sentences.extend([
        DraftSentence(text="I would welcome the opportunity to discuss how my documented background relates to the responsibilities of the role.",profile_fact_ids=[],requirement_ids=[],is_personal_claim=False,needs_user_input=False),
        DraftSentence(text="[Add a specific reason you are interested in this employer and any requested details that are not included above.]",profile_fact_ids=[],requirement_ids=[],is_personal_claim=False,needs_user_input=True),
        DraftSentence(text="Sincerely,\n[Your name]",profile_fact_ids=[],requirement_ids=[],is_personal_claim=False,needs_user_input=True),
    ])
    draft=ApplicationDraft(id=draft_id,user_id=user_id,opportunity_id=opportunity.id,profile_version=profile.version,kind="email",
                           subject=f"Application for {role}"[:200],sentences=sentences,edited_text=None,version=1,reviewed_at=None,created_at=now,updated_at=now)
    _validate_draft(draft,profile,opportunity)
    return draft


@router.post("/api/drafts",status_code=201)
async def create_draft(body: DraftCreateInput,request: Request,idempotency_key: str|None=Header(default=None,alias="Idempotency-Key")):
    session=current_session(request);require_csrf(request,session)
    user_id=session["user_id"];db_path=request.app.state.db_path
    if idempotency_key is not None and not 8<=len(idempotency_key)<=200: raise HTTPException(status_code=422,detail="Idempotency-Key must contain 8 to 200 characters")
    body_hash=hashlib.sha256(json.dumps(body.model_dump(mode="json"),sort_keys=True,separators=(",",":")).encode()).hexdigest()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            prior=conn.execute("SELECT body_hash,resource_id FROM idempotency WHERE user_id=? AND route=? AND request_key=?",(user_id,"POST:/api/drafts",idempotency_key)).fetchone()
            if prior:
                if prior["body_hash"]!=body_hash: raise HTTPException(status_code=409,detail="Idempotency key was used with different draft settings")
                return _load_draft(conn,prior["resource_id"],user_id).model_dump(mode="json")
        profile_row=conn.execute("SELECT payload_json,confirmed_at FROM profiles WHERE id=? AND version=? AND user_id=?",(str(body.profile_id),body.profile_version,user_id)).fetchone()
        if profile_row is None: raise HTTPException(status_code=404,detail="Profile not found")
        profile=Profile.model_validate_json(profile_row["payload_json"])
        if profile_row["confirmed_at"] is None or profile.confirmed_at is None or any(not fact.confirmed for fact in profile.facts): raise HTTPException(status_code=409,detail="Confirm your profile before preparing an application")
        op_row=conn.execute("SELECT payload_json FROM opportunities WHERE id=? AND user_id=?",(str(body.opportunity_id),user_id)).fetchone()
        if op_row is None: raise HTTPException(status_code=404,detail="Opportunity not found")
        opportunity=Opportunity.model_validate_json(op_row["payload_json"])
        if opportunity.page_type!="individual_job": raise HTTPException(status_code=422,detail="Only a verified individual vacancy can have an application draft")
        source=conn.execute("SELECT 1 FROM sources WHERE id=? AND user_id=?",(str(opportunity.source_id),user_id)).fetchone()
        assessment_row=conn.execute("SELECT payload_json FROM assessments WHERE opportunity_id=? AND profile_id=? AND profile_version=? ORDER BY rowid DESC LIMIT 1",(str(opportunity.id),str(profile.id),profile.version)).fetchone()
        if source is None or assessment_row is None: raise HTTPException(status_code=409,detail="Select a source-backed, assessed vacancy first")
        from app.models import Assessment
        assessment=Assessment.model_validate_json(assessment_row["payload_json"])
        draft=_make_template(uuid.uuid4(),user_id,opportunity,profile,assessment,_now())
        conn.execute("INSERT INTO drafts(id,user_id,version,payload_json) VALUES(?,?,?,?)",(str(draft.id),user_id,draft.version,json.dumps(draft.model_dump(mode="json"))))
        if idempotency_key:
            conn.execute("INSERT INTO idempotency(user_id,route,request_key,body_hash,resource_id,response_status,payload_json) VALUES(?,?,?,?,?,?,?)",
                         (user_id,"POST:/api/drafts",idempotency_key,body_hash,str(draft.id),201,json.dumps(draft.model_dump(mode="json"))))
    return draft.model_dump(mode="json")


@router.get("/api/drafts/{draft_id}")
async def get_draft(draft_id: uuid.UUID,request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn: return _load_draft(conn,str(draft_id),session["user_id"]).model_dump(mode="json")


@router.get("/api/drafts")
async def list_drafts(request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn:
        rows=conn.execute("""SELECT d.payload_json,o.payload_json AS opportunity FROM drafts d
            JOIN (SELECT id,MAX(version) AS version FROM drafts WHERE user_id=? GROUP BY id) latest
            ON d.id=latest.id AND d.version=latest.version
            JOIN opportunities o ON o.id=json_extract(d.payload_json,'$.opportunity_id')
            WHERE d.user_id=? ORDER BY d.rowid DESC LIMIT 20""",(session["user_id"],session["user_id"])).fetchall()
    items=[]
    for row in rows:
        draft=ApplicationDraft.model_validate_json(row["payload_json"]); opportunity=Opportunity.model_validate_json(row["opportunity"])
        items.append({"id":str(draft.id),"version":draft.version,"subject":draft.subject,"reviewed_at":draft.reviewed_at,
                      "edited":draft.edited_text is not None,"opportunity_id":str(opportunity.id),"title":opportunity.title,"employer":opportunity.employer})
    return {"items":items}


@router.put("/api/drafts/{draft_id}")
async def edit_draft(draft_id: uuid.UUID,body: DraftEditInput,request: Request):
    session=current_session(request);require_csrf(request,session)
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current=_load_draft(conn,str(draft_id),session["user_id"])
        if current.version!=body.expected_version: raise HTTPException(status_code=409,detail="Draft version is stale")
        updated=current.model_copy(update={"version":current.version+1,"edited_text":body.edited_text,"reviewed_at":None,"updated_at":_now()})
        conn.execute("INSERT INTO drafts(id,user_id,version,payload_json) VALUES(?,?,?,?)",(str(updated.id),session["user_id"],updated.version,json.dumps(updated.model_dump(mode="json"))))
    return updated.model_dump(mode="json")


@router.post("/api/drafts/{draft_id}/review")
async def review_draft(draft_id: uuid.UUID,body: DraftReviewInput,request: Request):
    session=current_session(request);require_csrf(request,session)
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current=_load_draft(conn,str(draft_id),session["user_id"])
        if current.version!=body.expected_version: raise HTTPException(status_code=409,detail="Draft version is stale")
        placeholders=[sentence.text for sentence in current.sentences if sentence.needs_user_input]
        if placeholders and (current.edited_text is None or any(text in current.edited_text for text in placeholders)):
            raise HTTPException(status_code=409,detail="Complete the visible placeholders before marking the draft reviewed")
        reviewed=current.model_copy(update={"reviewed_at":_now(),"updated_at":_now()})
        conn.execute("UPDATE drafts SET payload_json=? WHERE id=? AND user_id=? AND version=?",(json.dumps(reviewed.model_dump(mode="json")),str(draft_id),session["user_id"],current.version))
    return reviewed.model_dump(mode="json")


@router.get("/api/drafts/{draft_id}/download")
async def download_draft(draft_id: uuid.UUID,request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn: draft=_load_draft(conn,str(draft_id),session["user_id"])
    label="reviewed" if draft.reviewed_at else "not reviewed"
    body=draft.edited_text if draft.edited_text is not None else "\n\n".join(sentence.text for sentence in draft.sentences)
    text=f"RoleLander application draft — {label}\nSubject: {draft.subject or ''}\n\n{body}"
    return PlainTextResponse(text,headers={"Content-Disposition":f'attachment; filename="application-draft-{draft.id}.txt"'})


@router.post("/api/saved",status_code=201)
async def create_saved(body: SavedCreateInput,request: Request):
    session=current_session(request);require_csrf(request,session);user_id=session["user_id"]
    with connect(request.app.state.db_path) as conn:
        exists=conn.execute("SELECT 1 FROM opportunities WHERE id=? AND user_id=?",(str(body.opportunity_id),user_id)).fetchone()
        if exists is None: raise HTTPException(status_code=404,detail="Opportunity not found")
        saved_id=uuid.uuid4();now=_now()
        record=SavedOpportunity(id=saved_id,user_id=uuid.UUID(user_id),opportunity_id=body.opportunity_id,status=body.status,note=body.note,created_at=now,updated_at=now)
        conn.execute("INSERT OR IGNORE INTO saved(id,user_id,opportunity_id,payload_json) VALUES(?,?,?,?)",(str(saved_id),user_id,str(body.opportunity_id),json.dumps(record.model_dump(mode="json"))))
        row=conn.execute("SELECT payload_json FROM saved WHERE user_id=? AND opportunity_id=?",(user_id,str(body.opportunity_id))).fetchone()
    return SavedOpportunity.model_validate_json(row["payload_json"]).model_dump(mode="json")


@router.get("/api/saved")
async def list_saved(request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn:
        rows=conn.execute("SELECT s.payload_json,o.payload_json AS opportunity FROM saved s JOIN opportunities o ON o.id=s.opportunity_id WHERE s.user_id=? ORDER BY s.rowid DESC",(session["user_id"],)).fetchall()
    return {"items":[{"saved":json.loads(row["payload_json"]),"opportunity":json.loads(row["opportunity"])} for row in rows]}


@router.patch("/api/saved/{saved_id}")
async def edit_saved(saved_id: uuid.UUID,body: SavedEditInput,request: Request):
    session=current_session(request);require_csrf(request,session)
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT payload_json FROM saved WHERE id=? AND user_id=?",(str(saved_id),session["user_id"])).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Saved opportunity not found")
        current=SavedOpportunity.model_validate_json(row["payload_json"])
        if current.updated_at!=body.expected_updated_at: raise HTTPException(status_code=409,detail="Tracker version is stale")
        updated=current.model_copy(update={"status":body.status,"note":body.note,"updated_at":_now()})
        conn.execute("UPDATE saved SET payload_json=? WHERE id=? AND user_id=?",(json.dumps(updated.model_dump(mode="json")),str(saved_id),session["user_id"]))
    return updated.model_dump(mode="json")
