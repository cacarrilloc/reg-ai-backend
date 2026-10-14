"""Add durable deduplication and feed indexes for regulatory monitoring."""

from alembic import op

revision = '20260622_0004'
down_revision = '20260622_0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'create unique index if not exists regulatory_publications_source_url_key '
        'on public.regulatory_publications (source, url)'
    )
    op.execute(
        'create index if not exists regulatory_publications_published_at_idx '
        'on public.regulatory_publications (published_at desc nulls last, created_at desc)'
    )
    op.execute(
        'create table if not exists public.publication_event_deliveries ('
        'id text primary key, '
        'publication_id text not null references public.regulatory_publications(id) on delete cascade, '
        'org_id text not null, '
        'event_id text, '
        'sent_at timestamptz, '
        'created_at timestamptz not null default now(), '
        'unique (publication_id, org_id)'
        ')'
    )
    op.execute(
        'create index if not exists publication_event_deliveries_pending_idx '
        'on public.publication_event_deliveries (created_at) where sent_at is null'
    )
    op.execute(
        'alter table public.publication_event_deliveries enable row level security')
    op.execute(
        'drop policy if exists publication_deliveries_internal_access on public.publication_event_deliveries')
    op.execute(
        'create policy publication_deliveries_internal_access on public.publication_event_deliveries '
        'for all using (public.is_internal_user()) with check (public.is_internal_user())'
    )


def downgrade() -> None:
    op.execute(
        'drop policy if exists publication_deliveries_internal_access on public.publication_event_deliveries')
    op.execute('drop table if exists public.publication_event_deliveries')
    op.execute(
        'drop index if exists public.regulatory_publications_published_at_idx')
    op.execute(
        'drop index if exists public.regulatory_publications_source_url_key')
