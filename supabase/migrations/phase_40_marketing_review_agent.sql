-- Marketing Review Agent (R2): additive columns + indexes on existing tables.
-- Applies to public and ready isolated tenant schemas. Does not drop or rewrite R1 tables.

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

DO $$
DECLARE
    rec record;
    stmt text;
BEGIN
    FOR rec IN
        SELECT schema_name
        FROM public.organization_tenants
        WHERE isolation_mode = 'schema'
          AND status = 'ready'
          AND schema_name ~ '^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
    LOOP
        stmt := format($f$
            ALTER TABLE %I.marketing_contents
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
                ADD COLUMN IF NOT EXISTS checksum_sha256 varchar,
                ADD COLUMN IF NOT EXISTS file_size_bytes integer,
                ADD COLUMN IF NOT EXISTS updated_at timestamp without time zone DEFAULT now();
            ALTER TABLE %I.marketing_reviews
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
                ON %I.marketing_contents (org_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS marketing_contents_org_status_idx
                ON %I.marketing_contents (org_id, status);
            CREATE INDEX IF NOT EXISTS marketing_contents_tags_gin
                ON %I.marketing_contents USING GIN (content_tags);
            CREATE INDEX IF NOT EXISTS marketing_reviews_org_content_created_idx
                ON %I.marketing_reviews (org_id, content_id, created_at DESC);
        $f$, rec.schema_name, rec.schema_name, rec.schema_name, rec.schema_name,
           rec.schema_name, rec.schema_name);
        EXECUTE stmt;
    END LOOP;
END $$;
