from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.db import connect
from app.models import Assessment, DecisionOption, HumanDecision, Issue, Opportunity, Preferences, Profile, ProfileFact, RunEvent, SearchRun
from app.sessions import current_session, require_csrf
from app.state import ALLOWED_TRANSITIONS

router = APIRouter()
MAX_ACTIVE_RUNS_PER_USER = 1
MAX_RUNS_PER_USER_PER_HOUR = 5
MAX_GLOBAL_ACTIVE_RUNS = 20


class RunCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: uuid.UUID
    profile_version: int = Field(ge=1)
    preference_id: uuid.UUID
    preference_version: int = Field(ge=1)


class RunStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_state_version: int = Field(ge=1)


class DecisionAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_state_version: int = Field(ge=1)
    option_id: str = Field(min_length=1, max_length=100)
    answer_note: str | None = Field(max_length=1000)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(conn, run_id: str, event_type: str, message: str, metadata: dict | None = None) -> None:
    row = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 AS next FROM events WHERE run_id=?", (run_id,)).fetchone()
    event = RunEvent(id=uuid.uuid4(), run_id=run_id, sequence=row["next"], type=event_type,
                     message=message, metadata=metadata or {}, created_at=_now())
    conn.execute("INSERT INTO events(id,run_id,sequence,payload_json) VALUES(?,?,?,?)",
                 (str(event.id), run_id, event.sequence, json.dumps(event.model_dump(mode="json"))))


def _save_run(conn, run: SearchRun) -> None:
    conn.execute("UPDATE runs SET state=?,state_version=?,payload_json=?,updated_at=? WHERE id=? AND state_version=?",
                 (run.state, run.state_version, json.dumps(run.model_dump(mode="json")), run.updated_at.isoformat(),
                  str(run.id), run.state_version - 1))


def _run_response(conn, run: SearchRun) -> dict:
    pending = conn.execute("SELECT id FROM decisions WHERE run_id=? AND state='pending' LIMIT 1", (str(run.id),)).fetchone()
    response = run.model_dump(mode="json")
    response["pending_decision_id"] = pending["id"] if pending else None
    response["opportunity_count"] = conn.execute("SELECT COUNT(*) AS n FROM run_opportunities WHERE run_id=?", (str(run.id),)).fetchone()["n"]
    response["source_count"] = conn.execute("SELECT COUNT(*) AS n FROM sources WHERE run_id=?", (str(run.id),)).fetchone()["n"]
    return response


def _owned_run(conn, run_id: str, user_id: str) -> SearchRun:
    row = conn.execute("SELECT payload_json FROM runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return SearchRun.model_validate_json(row["payload_json"])


@router.post("/api/runs", status_code=202)
async def create_run(
    body: RunCreateInput, request: Request, background: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    session = current_session(request)
    require_csrf(request, session)
    user_id = session["user_id"]
    db_path = request.app.state.db_path
    normalized_body = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    body_hash = hashlib.sha256(normalized_body.encode()).hexdigest()
    if idempotency_key is not None and not 8 <= len(idempotency_key) <= 200:
        raise HTTPException(status_code=422, detail="Idempotency-Key must contain 8 to 200 characters")
    run_id = uuid.uuid4()
    now = _now()
    run = SearchRun(
        id=run_id, user_id=uuid.UUID(user_id), state="queued", state_version=1,
        profile_id=body.profile_id, profile_version=body.profile_version,
        preference_id=body.preference_id, preference_version=body.preference_version,
        action_count=0, search_call_count=0, extract_call_count=0, llm_call_count=0, decision_count=0,
        trigger="manual", occurrence_id=None, approved_source_snapshot=[], source_progress={"stage":"queued"},
        deferred_counts={}, claim_lease_until=None, started_at=None, updated_at=now, last_error=None,
    )
    created = False
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key is not None:
            prior = conn.execute("SELECT body_hash,resource_id FROM idempotency WHERE user_id=? AND route=? AND request_key=?",
                                 (user_id, "POST:/api/runs", idempotency_key)).fetchone()
            if prior is not None:
                if prior["body_hash"] != body_hash:
                    raise HTTPException(status_code=409, detail="Idempotency key was already used with different run settings")
                run = _owned_run(conn, prior["resource_id"], user_id)
                result = _run_response(conn, run)
            else:
                result = None
        else:
            result = None
        if result is None:
            active_states = ("queued", "running", "awaiting_decision")
            active_sql = ",".join("?" for _ in active_states)
            active_user = conn.execute(f"SELECT COUNT(*) AS n FROM runs WHERE user_id=? AND state IN ({active_sql})", (user_id,*active_states)).fetchone()["n"]
            if active_user >= MAX_ACTIVE_RUNS_PER_USER:
                raise HTTPException(status_code=409, detail="A research run is already active for this account.")
            recent = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE user_id=? AND created_at>=?", (user_id,(now-timedelta(hours=1)).isoformat())).fetchone()["n"]
            if recent >= MAX_RUNS_PER_USER_PER_HOUR:
                raise HTTPException(status_code=429, detail="The hourly research run limit has been reached.")
            global_active = conn.execute(f"SELECT COUNT(*) AS n FROM runs WHERE state IN ({active_sql})", active_states).fetchone()["n"]
            if global_active >= MAX_GLOBAL_ACTIVE_RUNS:
                raise HTTPException(status_code=503, detail="Research capacity is full. Try again later.")
            profile = conn.execute("SELECT payload_json,confirmed_at FROM profiles WHERE id=? AND version=? AND user_id=?",
                                   (str(body.profile_id), body.profile_version, user_id)).fetchone()
            if profile is None:
                raise HTTPException(status_code=404, detail="Profile not found")
            profile_data = json.loads(profile["payload_json"])
            if profile["confirmed_at"] is None or profile_data.get("confirmed_at") is None or any(not item.get("confirmed") for item in profile_data.get("facts", [])):
                raise HTTPException(status_code=409, detail="Confirm your profile before searching.")
            preference = conn.execute("SELECT 1 FROM preferences WHERE id=? AND version=? AND user_id=?",
                                      (str(body.preference_id), body.preference_version, user_id)).fetchone()
            if preference is None:
                raise HTTPException(status_code=404, detail="Preferences not found")
            conn.execute("INSERT INTO runs(id,user_id,state,state_version,payload_json,updated_at,created_at) VALUES(?,?,?,?,?,?,?)",
                         (str(run.id), user_id, run.state, run.state_version, json.dumps(run.model_dump(mode="json")), now.isoformat(),now.isoformat()))
            _event(conn, str(run.id), "started", "Research run queued.", {"trigger":"manual"})
            if idempotency_key is not None:
                conn.execute("INSERT INTO idempotency(user_id,route,request_key,body_hash,resource_id,response_status,payload_json) VALUES(?,?,?,?,?,?,?)",
                             (user_id, "POST:/api/runs", idempotency_key, body_hash, str(run.id), 202, json.dumps(run.model_dump(mode="json"))))
            created = True
            result = _run_response(conn, run)
    if created:
        background.add_task(request.app.state.run_segment, str(run.id))
    return result


@router.get("/api/runs")
async def list_runs(request: Request, limit: int = Query(default=10, ge=1, le=20)):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn:
        rows=conn.execute("SELECT payload_json FROM runs WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",(session["user_id"],limit)).fetchall()
        items=[_run_response(conn,SearchRun.model_validate_json(row["payload_json"])) for row in rows]
    return {"items":items}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: uuid.UUID, request: Request):
    session = current_session(request)
    with connect(request.app.state.db_path) as conn:
        run = _owned_run(conn, str(run_id), session["user_id"])
        return _run_response(conn, run)


@router.get("/api/runs/{run_id}/events")
async def get_run_events(run_id: uuid.UUID, request: Request):
    session = current_session(request)
    with connect(request.app.state.db_path) as conn:
        _owned_run(conn, str(run_id), session["user_id"])
        rows = conn.execute("SELECT payload_json FROM events WHERE run_id=? ORDER BY sequence", (str(run_id),)).fetchall()
    return {"items":[json.loads(row["payload_json"]) for row in rows]}


@router.get("/api/runs/{run_id}/sources")
async def get_run_sources(run_id: uuid.UUID, request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn:
        _owned_run(conn,str(run_id),session["user_id"])
        rows=conn.execute("SELECT id,payload_json FROM sources WHERE run_id=? AND user_id=? ORDER BY rowid",(str(run_id),session["user_id"])).fetchall()
    items=[]
    for row in rows:
        try:
            payload=json.loads(row["payload_json"]);document=payload["document"]
            items.append({"id":row["id"],"title":document["title"],"url":document["url"],"source_status":document["extraction_status"],
                          "inspection_state":payload.get("inspection_state","preview"),"excerpt":document.get("body_text","")[:1200],"truncated":document.get("truncated",False)})
        except (KeyError,TypeError,ValueError):
            continue
    return {"items":items}


@router.get("/api/runs/{run_id}/opportunities")
async def get_run_opportunities(run_id: uuid.UUID, request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn:
        run=_owned_run(conn,str(run_id),session["user_id"])
        rows=conn.execute("""SELECT o.payload_json AS opportunity,a.payload_json AS assessment,s.payload_json AS source
            FROM run_opportunities ro JOIN opportunities o ON o.id=ro.opportunity_id
            LEFT JOIN assessments a ON a.opportunity_id=o.id AND a.profile_id=? AND a.profile_version=? AND a.preference_id=? AND a.preference_version=?
            LEFT JOIN run_opportunity_sources ros ON ros.run_id=ro.run_id AND ros.opportunity_id=ro.opportunity_id
            LEFT JOIN sources s ON s.id=ros.source_id AND s.run_id=ro.run_id
            WHERE ro.run_id=? AND o.user_id=? ORDER BY o.rowid""",(str(run.profile_id),run.profile_version,str(run.preference_id),run.preference_version,str(run_id),session["user_id"])).fetchall()
    items=[]
    for row in rows:
        source_payload=json.loads(row["source"]) if row["source"] else {}
        document=source_payload.get("document",{})
        opportunity_data=json.loads(row["opportunity"])
        assessment_data=json.loads(row["assessment"]) if row["assessment"] else None
        items.append({"opportunity":opportunity_data,"assessment":assessment_data,
                      "source_excerpt":document.get("body_text","")[:2400],"source_truncated":document.get("truncated",False)})
    from app.assessment import rank_assessed
    assessed=[(Opportunity.model_validate(item["opportunity"]),Assessment.model_validate(item["assessment"])) for item in items if item["assessment"]]
    ordered_ids=[str(opportunity.id) for opportunity,_assessment in rank_assessed(assessed)]
    order={item_id:index for index,item_id in enumerate(ordered_ids)}
    items.sort(key=lambda item:order.get(item["opportunity"]["id"],len(order)))
    return {"items":items}


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, body: RunStateInput, request: Request):
    session = current_session(request)
    require_csrf(request, session)
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _owned_run(conn, str(run_id), session["user_id"])
        if run.state_version != body.expected_state_version:
            raise HTTPException(status_code=409, detail="Run state is stale")
        if "cancelled" not in ALLOWED_TRANSITIONS[run.state]:
            raise HTTPException(status_code=409, detail="Run cannot be cancelled in its current state")
        cancelled = run.model_copy(update={"state":"cancelled", "state_version":run.state_version+1, "updated_at":_now(), "source_progress":{**run.source_progress, "stage":"cancelled"}})
        _save_run(conn, cancelled)
        _event(conn, str(run.id), "warning", "Run cancelled by the user.")
        return _run_response(conn, cancelled)


@router.post("/api/runs/{run_id}/resume", status_code=202)
async def resume_run(run_id: uuid.UUID, body: RunStateInput, request: Request, background: BackgroundTasks):
    session = current_session(request)
    require_csrf(request, session)
    with connect(request.app.state.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _owned_run(conn, str(run_id), session["user_id"])
        if run.state != "interrupted" or run.state_version != body.expected_state_version:
            raise HTTPException(status_code=409, detail="Only the current interrupted run can be resumed")
        resumed = run.model_copy(update={"state":"queued", "state_version":run.state_version+1, "updated_at":_now(), "source_progress":{**run.source_progress, "stage":"resume_queued"}})
        _save_run(conn, resumed)
        _event(conn, str(run.id), "resumed", "Run resumed with the saved counters.")
        response = _run_response(conn, resumed)
    background.add_task(request.app.state.run_segment, str(run_id))
    return response


@router.get("/api/decisions/{decision_id}")
async def get_decision(decision_id: uuid.UUID, request: Request):
    session=current_session(request)
    with connect(request.app.state.db_path) as conn:
        row=conn.execute("SELECT d.payload_json FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.id=? AND r.user_id=?",(str(decision_id),session["user_id"])).fetchone()
    if row is None: raise HTTPException(status_code=404,detail="Decision not found")
    return HumanDecision.model_validate_json(row["payload_json"]).model_dump(mode="json")


def _apply_preference_patch(current: Preferences, patch: dict) -> Preferences:
    field=patch.get("preference_field"); proposed=patch.get("proposed_value")
    allowed={"role_queries","work_arrangements","seniority","paid_policy","opportunity_types","preferred_cities_policy","proximity.radius_km","proximity.policy"}
    if field not in allowed: raise HTTPException(status_code=422,detail="This preference change is not allowed")
    data=current.model_dump(mode="python")
    if field.startswith("proximity."):
        if current.proximity is None: raise HTTPException(status_code=422,detail="A nearby reference is required before changing the radius")
        prox=current.proximity.model_dump(mode="python")
        key=field.split(".",1)[1]
        if key=="radius_km":
            if not isinstance(proposed,int) or isinstance(proposed,bool) or not 1<=proposed<=500: raise HTTPException(status_code=422,detail="Radius must be between 1 and 500 km")
        elif not isinstance(proposed,str) or proposed not in {"required","preferred"}:
            raise HTTPException(status_code=422,detail="Choose required or preferred radius")
        prox[key]=proposed; data["proximity"]=prox
    else:
        data[field]=proposed
    data["version"]=current.version+1
    try: return Preferences.model_validate(data)
    except Exception as exc: raise HTTPException(status_code=422,detail="The proposed preference change is invalid") from exc


@router.post("/api/decisions/{decision_id}/answer")
async def answer_decision(decision_id: uuid.UUID, body: DecisionAnswerInput, request: Request, background: BackgroundTasks, response: Response):
    session=current_session(request); require_csrf(request,session)
    user_id=session["user_id"]; db_path=request.app.state.db_path; queued_id=None
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT d.payload_json,d.state,r.payload_json AS run_json FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.id=? AND r.user_id=?",(str(decision_id),user_id)).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Decision not found")
        decision=HumanDecision.model_validate_json(row["payload_json"]); run=SearchRun.model_validate_json(row["run_json"])
        if decision.state=="answered":
            if decision.selected_option_id!=body.option_id: raise HTTPException(status_code=409,detail="This decision was already answered differently")
            return _run_response(conn,run)
        if decision.state!="pending" or run.state!="awaiting_decision" or run.state_version!=body.expected_state_version or decision.state_version!=body.expected_state_version:
            raise HTTPException(status_code=409,detail="Decision or run state is stale")
        option=next((item for item in decision.options if item.id==body.option_id),None)
        if option is None: raise HTTPException(status_code=422,detail="Selected option does not exist")
        now=_now(); next_profile_version=run.profile_version; next_preference_version=run.preference_version
        new_error=None; next_state="queued"
        if option.action=="apply_preferences":
            prefrow=conn.execute("SELECT payload_json FROM preferences WHERE id=? AND version=? AND user_id=?",(str(run.preference_id),run.preference_version,user_id)).fetchone()
            if prefrow is None: raise HTTPException(status_code=404,detail="Run preferences not found")
            updated_preferences=_apply_preference_patch(Preferences.model_validate_json(prefrow["payload_json"]),option.patch or {})
            conn.execute("INSERT INTO preferences(id,version,user_id,payload_json) VALUES(?,?,?,?)",(str(updated_preferences.id),updated_preferences.version,user_id,json.dumps(updated_preferences.model_dump(mode="json"))))
            next_preference_version=updated_preferences.version
        elif option.action=="add_candidate_fact":
            if not body.answer_note or not body.answer_note.strip(): raise HTTPException(status_code=422,detail="Enter the factual answer you want added to your profile")
            profrow=conn.execute("SELECT payload_json FROM profiles WHERE id=? AND version=? AND user_id=?",(str(run.profile_id),run.profile_version,user_id)).fetchone()
            if profrow is None: raise HTTPException(status_code=404,detail="Run profile not found")
            old=Profile.model_validate_json(profrow["payload_json"])
            question=(option.patch or {}).get("fact_question") or "Additional candidate fact"
            fact=ProfileFact(id=uuid.uuid4(),kind="other",label=str(question)[:160],value=body.answer_note.strip(),level=None,origin="user",source_quote=None,confirmed=True)
            updated_profile=old.model_copy(update={"version":old.version+1,"facts":[*old.facts,fact],"confirmed_at":now,"created_at":now})
            conn.execute("INSERT INTO profiles(id,version,user_id,confirmed_at,payload_json,cleaned_cv_text) VALUES(?,?,?,?,?,?)",
                         (str(updated_profile.id),updated_profile.version,user_id,now.isoformat(),json.dumps(updated_profile.model_dump(mode="json")),""))
            next_profile_version=updated_profile.version
        else:
            next_state="completed"
            opportunity_count=conn.execute("SELECT COUNT(*) AS n FROM run_opportunities WHERE run_id=?",(str(run.id),)).fetchone()["n"]
            if not opportunity_count:
                next_state="failed"; new_error=Issue(code="USER_FINISHED",message="The user chose to finish without changing settings; no verified vacancies are in the run.",field=None)
        answered=decision.model_copy(update={"state":"answered","selected_option_id":option.id,"answer_note":body.answer_note,"answered_at":now})
        next_progress={**run.source_progress,"stage":"decision_answered" if next_state=="queued" else next_state}
        if option.action=="apply_preferences" and next_preference_version!=run.preference_version:
            next_progress["reassess_from_preference_version"]=run.preference_version
        next_run=run.model_copy(update={"state":next_state,"state_version":run.state_version+1,"profile_version":next_profile_version,
                                        "preference_version":next_preference_version,"updated_at":now,"claim_lease_until":None,
                                        "last_error":new_error,"source_progress":next_progress})
        decision_update=conn.execute("UPDATE decisions SET state='answered',payload_json=? WHERE id=? AND state='pending'",(json.dumps(answered.model_dump(mode="json")),str(decision_id)))
        if decision_update.rowcount!=1: raise HTTPException(status_code=409,detail="Decision changed before the answer was saved")
        run_update=conn.execute("UPDATE runs SET state=?,state_version=?,payload_json=?,updated_at=? WHERE id=? AND state='awaiting_decision' AND state_version=?",
                                 (next_run.state,next_run.state_version,json.dumps(next_run.model_dump(mode="json")),now.isoformat(),str(run.id),run.state_version))
        if run_update.rowcount!=1: raise HTTPException(status_code=409,detail="Run changed before the answer was saved")
        _event(conn,str(run.id),"decision_answered","Decision answered; updated settings will be used on resume.",{"decision_id":str(decision_id),"option_id":option.id})
        if next_state=="failed": _event(conn,str(run.id),"error",new_error.message,{"code":new_error.code})
        response_body=_run_response(conn,next_run)
        if next_state=="queued": queued_id=str(run.id)
    if queued_id:
        response.status_code=202
        background.add_task(request.app.state.run_segment,queued_id)
    return response_body
