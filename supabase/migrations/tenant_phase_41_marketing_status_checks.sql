-- Isolated tenant projects: allow Mode A/B marketing statuses including queued.

DO $$
DECLARE
    drop_name text;
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
END $$;
