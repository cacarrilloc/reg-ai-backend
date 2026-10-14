-- Isolated tenant projects (public schema only). Platform schema tenants are
-- healed at request time by ensure_marketing_review_schema.

CREATE TABLE IF NOT EXISTS public.marketing_contents (
    id text primary key,
    org_id text not null,
    name text not null,
    storage_path text,
    status text not null default 'draft',
    uploaded_by text,
    created_at timestamp without time zone not null default now()
);

CREATE TABLE IF NOT EXISTS public.marketing_reviews (
    id text primary key,
    org_id text not null,
    content_id text not null,
    status text not null default 'pending',
    findings jsonb not null default '[]'::jsonb,
    decided_by text,
    decided_at timestamp without time zone,
    created_at timestamp without time zone not null default now()
);

ALTER TABLE public.marketing_contents
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
    ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT now();

ALTER TABLE public.marketing_reviews
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
    ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT now();

CREATE INDEX IF NOT EXISTS marketing_contents_org_created_idx
    ON public.marketing_contents (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS marketing_contents_org_status_idx
    ON public.marketing_contents (org_id, status);
CREATE INDEX IF NOT EXISTS marketing_contents_org_format_idx
    ON public.marketing_contents (org_id, format);
CREATE INDEX IF NOT EXISTS marketing_contents_tags_gin
    ON public.marketing_contents USING GIN (content_tags);
CREATE INDEX IF NOT EXISTS marketing_reviews_org_content_created_idx
    ON public.marketing_reviews (org_id, content_id, created_at DESC);
CREATE INDEX IF NOT EXISTS marketing_reviews_org_status_idx
    ON public.marketing_reviews (org_id, status);
CREATE INDEX IF NOT EXISTS marketing_reviews_publication_idx
    ON public.marketing_reviews (org_id, publication_id);
