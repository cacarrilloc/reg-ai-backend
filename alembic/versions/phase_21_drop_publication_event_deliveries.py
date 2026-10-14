"""Drop unused publication_event_deliveries outbox table.

Gap analysis for regulatory publications is owned by the regulatory compliance
workflow cron. Nothing enqueued or emitted publication.created deliveries, so
this table stayed empty in practice.
"""

from alembic import op

revision = '20260720_0021'
down_revision = '20260718_0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "drop policy if exists publication_deliveries_org_access "
        "on public.publication_event_deliveries"
    )
    op.execute(
        "drop policy if exists publication_deliveries_internal_access "
        "on public.publication_event_deliveries"
    )
    op.execute("drop index if exists publication_event_deliveries_pending_idx")
    op.execute("drop table if exists public.publication_event_deliveries")


def downgrade() -> None:
    op.execute(
        "create table if not exists public.publication_event_deliveries ("
        "id text primary key, "
        "publication_id text not null "
        "references public.regulatory_publications(id) on delete cascade, "
        "org_id text not null, "
        "event_id text, "
        "sent_at timestamptz, "
        "created_at timestamptz not null default now(), "
        "unique (publication_id, org_id)"
        ")"
    )
    op.execute(
        "create index if not exists publication_event_deliveries_pending_idx "
        "on public.publication_event_deliveries (created_at) where sent_at is null"
    )
    op.execute("alter table public.publication_event_deliveries enable row level security")
    op.execute(
        "drop policy if exists publication_deliveries_org_access "
        "on public.publication_event_deliveries"
    )
    op.execute(
        "create policy publication_deliveries_org_access on public.publication_event_deliveries "
        "for all using ("
        "public.is_internal_user() or org_id = public.current_clerk_org_id()"
        ") with check ("
        "public.is_internal_user() or org_id = public.current_clerk_org_id()"
        ")"
    )
