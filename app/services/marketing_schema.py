"""Keep marketing_contents / marketing_reviews in sync with the Review Agent schema.

Prod tenant schemas cloned before phase_40/41 reject ``queued`` and omit R2 columns,
which is the "Unable to persist the marketing content record" failure. These
statements are additive and safe to re-run on the current search_path.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

_HEALED_SCHEMAS: set[str] = set()

_CREATE_CONTENTS_TABLE = """
CREATE TABLE IF NOT EXISTS marketing_contents (
    id text primary key,
    org_id text not null,
    name text not null,
    storage_path text,
    status text not null default 'draft',
    uploaded_by text,
    created_at timestamp without time zone not null default now()
)
"""

_CREATE_REVIEWS_TABLE = """
CREATE TABLE IF NOT EXISTS marketing_reviews (
    id text primary key,
    org_id text not null,
    content_id text not null,
    status text not null default 'pending',
    findings jsonb not null default '[]'::jsonb,
    decided_by text,
    decided_at timestamp without time zone,
    created_at timestamp without time zone not null default now()
)
"""

_ADD_CONTENT_COLUMNS = """
ALTER TABLE marketing_contents
    ADD COLUMN IF NOT EXISTS title varchar,
    ADD COLUMN IF NOT EXISTS format varchar NOT NULL DEFAULT 'text',
    ADD COLUMN IF NOT EXISTS channel varchar,
    ADD COLUMN IF NOT EXISTS audience varchar NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS registrant_type varchar NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS source_url varchar,
    ADD COLUMN IF NOT EXISTS body_text text,
    ADD COLUMN IF NOT EXISTS html_preview text,
    ADD COLUMN IF NOT EXISTS content_tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS layout_notes jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS classification jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS published_locations jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS cadence_key varchar,
    ADD COLUMN IF NOT EXISTS cadence_step integer,
    ADD COLUMN IF NOT EXISTS file_content_type varchar,
    ADD COLUMN IF NOT EXISTS file_size_bytes integer,
    ADD COLUMN IF NOT EXISTS checksum_sha256 varchar,
    ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT now()
"""

_ADD_REVIEW_COLUMNS = """
ALTER TABLE marketing_reviews
    ADD COLUMN IF NOT EXISTS mode varchar NOT NULL DEFAULT 'A',
    ADD COLUMN IF NOT EXISTS trigger_type varchar,
    ADD COLUMN IF NOT EXISTS trigger_id varchar,
    ADD COLUMN IF NOT EXISTS publication_id varchar,
    ADD COLUMN IF NOT EXISTS questions jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS answers jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS layout_notes jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS classification jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS rule_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS change_list jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS selected_options jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS output_kind varchar,
    ADD COLUMN IF NOT EXISTS artifact_storage_path varchar,
    ADD COLUMN IF NOT EXISTS skipped_archive_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS error_message text,
    ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT now()
"""

_WIDEN_STATUS_CHECKS = """
DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT rel.relname AS table_name, pgc.conname
        FROM pg_constraint pgc
        JOIN pg_class rel ON rel.oid = pgc.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = current_schema()
          AND rel.relname IN ('marketing_contents', 'marketing_reviews')
          AND pgc.contype = 'c'
          AND pg_get_constraintdef(pgc.oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', rec.table_name, rec.conname);
    END LOOP;

    ALTER TABLE marketing_contents
        ADD CONSTRAINT marketing_contents_status_check
        CHECK (status IN (
            'draft', 'queued', 'in_review', 'awaiting_answers',
            'reviewed', 'failed', 'approved', 'rejected'
        ));

    ALTER TABLE marketing_reviews
        ADD CONSTRAINT marketing_reviews_status_check
        CHECK (status IN (
            'pending', 'queued', 'running', 'awaiting_answers',
            'ready_for_cco', 'signed_off', 'failed', 'approved', 'rejected'
        ));
END $$
"""

_INDEX_STATEMENTS = (
    'CREATE INDEX IF NOT EXISTS marketing_contents_org_created_idx'
    ' ON marketing_contents (org_id, created_at DESC)',
    'CREATE INDEX IF NOT EXISTS marketing_contents_org_status_idx'
    ' ON marketing_contents (org_id, status)',
    'CREATE INDEX IF NOT EXISTS marketing_contents_org_format_idx'
    ' ON marketing_contents (org_id, format)',
    'CREATE INDEX IF NOT EXISTS marketing_contents_tags_gin'
    ' ON marketing_contents USING GIN (content_tags)',
    'CREATE INDEX IF NOT EXISTS marketing_reviews_org_content_created_idx'
    ' ON marketing_reviews (org_id, content_id, created_at DESC)',
    'CREATE INDEX IF NOT EXISTS marketing_reviews_org_status_idx'
    ' ON marketing_reviews (org_id, status)',
    'CREATE INDEX IF NOT EXISTS marketing_reviews_publication_idx'
    ' ON marketing_reviews (org_id, publication_id)',
)


def _schema_cache_key(db: Session) -> str:
    info = getattr(db, 'info', None)
    if isinstance(info, dict):
        schema = str(info.get('tenant_schema') or '').strip()
        if schema:
            return schema
    return 'public'


def ensure_marketing_review_schema(db: Session | None, *, force: bool = False) -> bool:
    """Apply missing Review Agent columns and queued-status CHECKs. Returns True if DDL ran."""
    if db is None:
        return False
    cache_key = _schema_cache_key(db)
    if not force and cache_key in _HEALED_SCHEMAS:
        return False
    get_bind = getattr(db, 'get_bind', None)
    if not callable(get_bind):
        _HEALED_SCHEMAS.add(cache_key)
        return False
    try:
        bind = get_bind()
    except Exception:
        _HEALED_SCHEMAS.add(cache_key)
        return False
    if bind is None:
        _HEALED_SCHEMAS.add(cache_key)
        return False

    schema = cache_key
    try:
        with bind.connect() as conn:
            with conn.begin():
                if schema and schema != 'public':
                    from app.services.schema_tenant_provisioner import (
                        TENANT_SCHEMA_RE,
                        quote_pg_ident,
                    )

                    if TENANT_SCHEMA_RE.fullmatch(schema):
                        conn.execute(
                            text(f'SET LOCAL search_path TO {quote_pg_ident(schema)}, public')
                        )
                for statement in (
                    _CREATE_CONTENTS_TABLE,
                    _CREATE_REVIEWS_TABLE,
                    _ADD_CONTENT_COLUMNS,
                    _ADD_REVIEW_COLUMNS,
                    _WIDEN_STATUS_CHECKS,
                ):
                    conn.execute(text(statement))
                for statement in _INDEX_STATEMENTS:
                    conn.execute(text(statement))
    except Exception:
        logger.exception('marketing_schema_heal_failed', schema=schema)
        return False

    _HEALED_SCHEMAS.add(cache_key)
    logger.info('marketing_schema_healed', schema=schema)
    return True


def reset_marketing_schema_heal_cache() -> None:
    _HEALED_SCHEMAS.clear()
