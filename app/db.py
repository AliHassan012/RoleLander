from __future__ import annotations

import hashlib
import secrets
import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                google_issuer TEXT NOT NULL,
                google_subject TEXT NOT NULL,
                verified_email TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (google_issuer, google_subject)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                cookie_hash TEXT PRIMARY KEY,
                session_key TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version >= 1),
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                confirmed_at TEXT,
                payload_json TEXT NOT NULL,
                cleaned_cv_text TEXT NOT NULL DEFAULT '',
                is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK(is_synthetic IN (0,1)),
                PRIMARY KEY(id, version),
                UNIQUE(id, version, user_id)
            );
            CREATE INDEX IF NOT EXISTS profiles_owner_idx ON profiles(id,user_id);
            CREATE TABLE IF NOT EXISTS preferences (
                id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version >= 1),
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(id, version),
                UNIQUE(id, version, user_id)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                state_version INTEGER NOT NULL CHECK(state_version >= 1),
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS runs_owner_idx ON runs(id,user_id);
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS companies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                canonical_domain TEXT NOT NULL,
                careers_url TEXT NOT NULL,
                adapter TEXT NOT NULL,
                adapter_identifier TEXT,
                adapter_region TEXT,
                evidence_refs_json TEXT NOT NULL,
                verified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_companies (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('suggested','approved','blocked')),
                preferred INTEGER NOT NULL DEFAULT 0 CHECK(preferred IN (0,1)),
                priority INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL,
                approved_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id,company_id)
            );
            CREATE TABLE IF NOT EXISTS company_recommendation_operations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                search_attempts INTEGER NOT NULL DEFAULT 0 CHECK(search_attempts BETWEEN 0 AND 4),
                extract_batches INTEGER NOT NULL DEFAULT 0 CHECK(extract_batches BETWEEN 0 AND 2),
                structured_ai_attempts INTEGER NOT NULL DEFAULT 0 CHECK(structured_ai_attempts BETWEEN 0 AND 2),
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS company_recommendations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                operation_id TEXT NOT NULL REFERENCES company_recommendation_operations(id) ON DELETE CASCADE,
                company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                why_it_fits TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                relevant_opening INTEGER,
                UNIQUE(operation_id,company_id)
            );
            CREATE TABLE IF NOT EXISTS company_feed_cache (
                company_id TEXT PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete','partial','failed')),
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS opportunities (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                stable_source_key TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(user_id,stable_source_key)
            );
            CREATE TABLE IF NOT EXISTS run_opportunities (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
                PRIMARY KEY(run_id,opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS run_opportunity_sources (
                run_id TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                PRIMARY KEY(run_id,opportunity_id,source_id),
                FOREIGN KEY(run_id,opportunity_id) REFERENCES run_opportunities(run_id,opportunity_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
                profile_id TEXT,
                profile_version INTEGER NOT NULL,
                preference_id TEXT,
                preference_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_decision_per_run
              ON decisions(run_id) WHERE state='pending';
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(run_id,sequence)
            );
            CREATE TABLE IF NOT EXISTS saved (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                UNIQUE(user_id,opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                version INTEGER NOT NULL CHECK(version >= 1),
                payload_json TEXT NOT NULL,
                PRIMARY KEY(id,version)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                route TEXT NOT NULL,
                request_key TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                resource_id TEXT,
                response_status INTEGER,
                payload_json TEXT,
                PRIMARY KEY(user_id,route,request_key)
            );
            CREATE TABLE IF NOT EXISTS exchange_rate_snapshots (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, base TEXT NOT NULL,
                rates_json TEXT NOT NULL, provider_updated_at TEXT NOT NULL,
                next_update_at TEXT NOT NULL, fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connected_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL, subject TEXT NOT NULL, verified_email TEXT NOT NULL,
                granted_scopes_json TEXT NOT NULL, encrypted_refresh_token BLOB,
                access_expires_at TEXT, read_enabled INTEGER NOT NULL DEFAULT 0,
                send_self_enabled INTEGER NOT NULL DEFAULT 0,
                connection_state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_id,provider,subject)
            );
            CREATE TABLE IF NOT EXISTS gmail_sources (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0, effective_query TEXT NOT NULL,
                labels_json TEXT NOT NULL, senders_json TEXT NOT NULL,
                backfill_started_at TEXT, completed_cursor TEXT,
                pending_page_token TEXT, last_success_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS private_leads (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL, safe_subject TEXT NOT NULL,
                public_urls_json TEXT NOT NULL, review_status TEXT NOT NULL,
                received_at TEXT NOT NULL, UNIQUE(account_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS inbox_seen (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL REFERENCES connected_accounts(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL, seen_at TEXT NOT NULL,
                PRIMARY KEY(user_id,account_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS monitoring_settings (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS schedule_occurrences (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                settings_version INTEGER NOT NULL, due_at TEXT NOT NULL,
                state TEXT NOT NULL, UNIQUE(user_id,settings_version,due_at)
            );
            CREATE TABLE IF NOT EXISTS source_checks (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_key TEXT NOT NULL, status TEXT NOT NULL, coverage TEXT NOT NULL,
                progress_json TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_items (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_key TEXT NOT NULL, payload_json TEXT NOT NULL, read_at TEXT,
                UNIQUE(user_id,event_key)
            );
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_key TEXT NOT NULL, channel TEXT NOT NULL, destination TEXT NOT NULL,
                state TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(user_id,event_key,channel,destination)
            );
            """
        )
        # Additive migrations keep existing local databases usable.
        for table, column, declaration in (
            ("profiles", "is_synthetic", "INTEGER NOT NULL DEFAULT 0 CHECK(is_synthetic IN (0,1))"),
            ("runs", "created_at", "TEXT NOT NULL DEFAULT ''"),
            ("assessments", "profile_id", "TEXT"),
            ("assessments", "preference_id", "TEXT"),
        ):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.execute("UPDATE runs SET created_at=updated_at WHERE created_at='' OR created_at IS NULL")
        assessment_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='assessments'").fetchone()["sql"] or ""
        if "UNIQUE(opportunity_id,profile_version,preference_version)" in assessment_sql.replace(" ", ""):
            conn.execute("""CREATE TABLE assessments_migrated (
                id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
                profile_id TEXT, profile_version INTEGER NOT NULL, preference_id TEXT,
                preference_version INTEGER NOT NULL, payload_json TEXT NOT NULL)""")
            conn.execute("INSERT INTO assessments_migrated SELECT id,opportunity_id,profile_id,profile_version,preference_id,preference_version,payload_json FROM assessments")
            conn.execute("DROP TABLE assessments")
            conn.execute("ALTER TABLE assessments_migrated RENAME TO assessments")
        conn.execute("""UPDATE assessments AS a SET
            profile_id=COALESCE(profile_id,(SELECT json_extract(r.payload_json,'$.profile_id') FROM run_opportunities ro JOIN runs r ON r.id=ro.run_id
                WHERE ro.opportunity_id=a.opportunity_id AND json_extract(r.payload_json,'$.profile_version')=a.profile_version AND json_extract(r.payload_json,'$.preference_version')=a.preference_version
                ORDER BY r.created_at DESC LIMIT 1)),
            preference_id=COALESCE(preference_id,(SELECT json_extract(r.payload_json,'$.preference_id') FROM run_opportunities ro JOIN runs r ON r.id=ro.run_id
                WHERE ro.opportunity_id=a.opportunity_id AND json_extract(r.payload_json,'$.profile_version')=a.profile_version AND json_extract(r.payload_json,'$.preference_version')=a.preference_version
                ORDER BY r.created_at DESC LIMIT 1))""")
        conn.execute("""INSERT OR IGNORE INTO run_opportunity_sources(run_id,opportunity_id,source_id)
            SELECT ro.run_id,ro.opportunity_id,s.id FROM run_opportunities ro JOIN opportunities o ON o.id=ro.opportunity_id
            JOIN sources s ON s.run_id=ro.run_id AND lower(s.url)=lower(o.canonical_url)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS assessment_identity_idx ON assessments(opportunity_id,profile_id,profile_version,preference_id,preference_version)")
        conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version','1') ON CONFLICT(key) DO NOTHING"
        )


def hash_cookie(cookie_value: str) -> str:
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


def new_opaque_cookie() -> str:
    return secrets.token_urlsafe(32)

