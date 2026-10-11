alter table public.items
  add column if not exists active boolean not null default true;

alter table public.items
  add column if not exists archived_at timestamptz;

create index if not exists items_workspace_active_name_idx
  on public.items(workspace_id, active, name);
