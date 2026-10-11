-- Reversible lifecycle for storage roots and nested locations.
-- `active` already exists in the initial schema; archived_at records when a
-- location left the active graph without deleting inventory or history.

alter table public.locations
  add column if not exists archived_at timestamptz;

create index if not exists locations_workspace_active_idx
  on public.locations(workspace_id, active, parent_id);

update public.locations
set archived_at = coalesce(archived_at, updated_at, now())
where not active and archived_at is null;

update public.locations
set archived_at = null
where active and archived_at is not null;

alter table public.locations drop constraint if exists locations_archive_state_check;
alter table public.locations
  add constraint locations_archive_state_check check (
    (active and archived_at is null) or (not active and archived_at is not null)
  );
