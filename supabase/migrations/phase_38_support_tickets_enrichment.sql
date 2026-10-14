-- phase_38: enrich support_tickets for ops email notifications
-- Columns: priority, sender, description; status open|closed|escalated

alter table public.support_tickets
  add column if not exists priority text not null default 'NORMAL';

alter table public.support_tickets
  add column if not exists sender text;

do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'support_tickets'
      and column_name = 'body'
  ) and not exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'support_tickets'
      and column_name = 'description'
  ) then
    alter table public.support_tickets rename column body to description;
  end if;
end $$;

alter table public.support_tickets
  add column if not exists description text;

update public.support_tickets
set description = coalesce(nullif(trim(description), ''), '(no description)')
where description is null or trim(description) = '';

alter table public.support_tickets
  alter column description set not null;

update public.support_tickets
set sender = coalesce(nullif(trim(sender), ''), nullif(trim(created_by), ''), 'unknown')
where sender is null or trim(sender) = '';

alter table public.support_tickets
  alter column sender set not null;

update public.support_tickets
set status = 'open'
where lower(status) in ('pending', 'in_progress', 'open');

update public.support_tickets
set status = 'closed'
where lower(status) in ('resolved', 'closed', 'close');

update public.support_tickets
set status = 'escalated'
where lower(status) = 'escalated';

update public.support_tickets
set status = 'open'
where lower(status) not in ('open', 'closed', 'escalated');

alter table public.support_tickets drop constraint if exists support_tickets_status_check;

alter table public.support_tickets
  add constraint support_tickets_status_check
  check (status in ('open', 'closed', 'escalated'));

alter table public.support_tickets drop constraint if exists support_tickets_priority_check;

update public.support_tickets
set priority = upper(priority);

update public.support_tickets
set priority = 'NORMAL'
where priority not in ('LOW', 'NORMAL', 'HIGH', 'URGENT');

alter table public.support_tickets
  add constraint support_tickets_priority_check
  check (priority in ('LOW', 'NORMAL', 'HIGH', 'URGENT'));

comment on column public.support_tickets.priority is
  'Ticket priority: LOW, NORMAL, HIGH, or URGENT';
comment on column public.support_tickets.sender is
  'Email (preferred) or auth user id of the person who opened the ticket';
comment on column public.support_tickets.description is
  'Full ticket description / message body';
comment on column public.support_tickets.status is
  'Lifecycle status: open, closed, or escalated';
