-- Widen marketing status CHECKs so Mode A/B review states can persist.
-- Original phase_25 allowed only draft/in_review/approved/rejected (contents)
-- and pending/approved/rejected (reviews).

DO $$
DECLARE
    drop_name text;
    tenant_schema text;
    table_name text;
BEGIN
    FOR drop_name IN
        SELECT pgc.conname
        FROM pg_constraint pgc
        JOIN pg_class rel ON rel.oid = pgc.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relname = 'marketing_contents'
          AND pgc.contype = 'c'
          AND pg_get_constraintdef(pgc.oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE public.marketing_contents DROP CONSTRAINT %I', drop_name);
    END LOOP;

    FOR drop_name IN
        SELECT pgc.conname
        FROM pg_constraint pgc
        JOIN pg_class rel ON rel.oid = pgc.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relname = 'marketing_reviews'
          AND pgc.contype = 'c'
          AND pg_get_constraintdef(pgc.oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE public.marketing_reviews DROP CONSTRAINT %I', drop_name);
    END LOOP;

    ALTER TABLE public.marketing_contents
        ADD CONSTRAINT marketing_contents_status_check
        CHECK (status IN (
            'draft',
            'queued',
            'in_review',
            'awaiting_answers',
            'reviewed',
            'failed',
            'approved',
            'rejected'
        ));

    ALTER TABLE public.marketing_reviews
        ADD CONSTRAINT marketing_reviews_status_check
        CHECK (status IN (
            'pending',
            'queued',
            'running',
            'awaiting_answers',
            'ready_for_cco',
            'signed_off',
            'failed',
            'approved',
            'rejected'
        ));

    FOR tenant_schema IN
        SELECT schema_name
        FROM public.organization_tenants
        WHERE isolation_mode = 'schema'
          AND status = 'ready'
          AND schema_name ~ '^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
    LOOP
        FOR table_name, drop_name IN
            SELECT rel.relname, pgc.conname
            FROM pg_constraint pgc
            JOIN pg_class rel ON rel.oid = pgc.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            WHERE nsp.nspname = tenant_schema
              AND rel.relname IN ('marketing_contents', 'marketing_reviews')
              AND pgc.contype = 'c'
              AND pg_get_constraintdef(pgc.oid) ILIKE '%status%'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I.%I DROP CONSTRAINT %I',
                tenant_schema,
                table_name,
                drop_name
            );
        END LOOP;

        EXECUTE format(
            $f$ALTER TABLE %I.marketing_contents
               ADD CONSTRAINT marketing_contents_status_check
               CHECK (status IN (
                   'draft','queued','in_review','awaiting_answers','reviewed','failed','approved','rejected'
               ))$f$,
            tenant_schema
        );
        EXECUTE format(
            $f$ALTER TABLE %I.marketing_reviews
               ADD CONSTRAINT marketing_reviews_status_check
               CHECK (status IN (
                   'pending','queued','running','awaiting_answers','ready_for_cco','signed_off','failed','approved','rejected'
               ))$f$,
            tenant_schema
        );
    END LOOP;
END $$;
