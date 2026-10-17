from __future__ import annotations

from fastapi import HTTPException

from app.db import connect

ALLOWED_TRANSITIONS = {
    "queued": {"running", "cancelled", "interrupted"},
    "running": {"awaiting_decision", "completed", "failed", "cancelled", "interrupted"},
    "awaiting_decision": {"queued", "completed", "failed", "cancelled"},
    "interrupted": {"queued", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def get_owned(conn, table: str, resource_id: str, user_id: str):
    # Query text is a closed host-side allowlist; request values are always parameters.
    queries = {
        "profiles": "SELECT * FROM profiles WHERE id=? AND user_id=? ORDER BY version DESC LIMIT 1",
        "preferences": "SELECT * FROM preferences WHERE id=? AND user_id=? ORDER BY version DESC LIMIT 1",
        "runs": "SELECT * FROM runs WHERE id=? AND user_id=?",
        "sources": "SELECT * FROM sources WHERE id=? AND user_id=?",
        "opportunities": "SELECT * FROM opportunities WHERE id=? AND user_id=?",
        "drafts": "SELECT * FROM drafts WHERE id=? AND user_id=? ORDER BY version DESC LIMIT 1",
        "saved": "SELECT * FROM saved WHERE id=? AND user_id=?",
        "private_leads": "SELECT * FROM private_leads WHERE id=? AND user_id=?",
        "connected_accounts": "SELECT * FROM connected_accounts WHERE id=? AND user_id=?",
        "gmail_sources": "SELECT * FROM gmail_sources WHERE id=? AND user_id=?",
        "schedule_occurrences": "SELECT * FROM schedule_occurrences WHERE id=? AND user_id=?",
        "notification_items": "SELECT * FROM notification_items WHERE id=? AND user_id=?",
        "notification_deliveries": "SELECT * FROM notification_deliveries WHERE id=? AND user_id=?",
        "company_recommendation_operations": "SELECT * FROM company_recommendation_operations WHERE id=? AND user_id=?",
        "company_recommendations": "SELECT * FROM company_recommendations WHERE id=? AND user_id=?",
        "assessments": "SELECT a.* FROM assessments a JOIN opportunities o ON o.id=a.opportunity_id WHERE a.id=? AND o.user_id=?",
        "decisions": "SELECT d.* FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.id=? AND r.user_id=?",
        "events": "SELECT e.* FROM events e JOIN runs r ON r.id=e.run_id WHERE e.id=? AND r.user_id=?",
    }
    query = queries.get(table)
    if query is None:
        raise ValueError("Unsupported ownership table")
    row = conn.execute(query, (resource_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    return row


def transition_run(db_path, run_id: str, expected_version: int, new_state: str) -> int:
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT state,state_version FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run["state_version"] != expected_version:
            raise HTTPException(status_code=409, detail="Stale run state")
        if new_state not in ALLOWED_TRANSITIONS[run["state"]]:
            raise HTTPException(status_code=409, detail="Illegal run transition")
        next_version = expected_version + 1
        updated = conn.execute(
            "UPDATE runs SET state=?,state_version=? WHERE id=? AND state_version=?",
            (new_state, next_version, run_id, expected_version),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="Stale run state")
        return next_version

