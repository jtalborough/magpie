-- Magpie cloud schema target.
--
-- The Raspberry Pi POC keeps one implicit SQLite workspace. Supabase makes the
-- workspace and ownership model explicit so web, iOS, Android, and hardware
-- clients can share the same storage graph.

create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table public.workspaces (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.workspace_members (
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'member'
    check (role in ('owner', 'admin', 'member', 'viewer')),
  created_at timestamptz not null default now(),
  primary key (workspace_id, user_id)
);

create table public.locations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  parent_id uuid references public.locations(id) on delete set null,
  local_code text not null,
  type text not null
    check (type in ('home', 'room', 'area', 'cabinet', 'drawer', 'shelf', 'box', 'pouch', 'bin', 'cell', 'tote', 'rack', 'locker', 'storage_unit')),
  label text not null,
  path_label text not null default '',
  capacity_class text not null default 'medium'
    check (capacity_class in ('tiny', 'small', 'medium', 'large', 'oversize')),
  purpose_tags text[] not null default '{}',
  placement_notes text not null default '',
  sort_order integer not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (workspace_id, local_code)
);

create table public.items (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  name text not null,
  description text not null default '',
  category text not null default '',
  size_class text not null default ''
    check (size_class in ('', 'tiny', 'small', 'medium', 'large', 'oversize')),
  size_source text not null default '',
  size_confidence real not null default 0 check (size_confidence >= 0 and size_confidence <= 1),
  dimensions jsonb not null default '{}'::jsonb,
  home_location_id uuid references public.locations(id) on delete set null,
  last_seen_location_id uuid references public.locations(id) on delete set null,
  notes text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (workspace_id, name)
);

create table public.item_aliases (
  item_id uuid not null references public.items(id) on delete cascade,
  alias text not null,
  created_at timestamptz not null default now(),
  primary key (item_id, alias)
);

create table public.inventory_entries (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  item_id uuid not null references public.items(id) on delete cascade,
  location_id uuid not null references public.locations(id) on delete cascade,
  quantity numeric not null default 1,
  unit text not null default 'each',
  status text not null default 'present'
    check (status in ('present', 'review', 'missing', 'moved')),
  confidence real not null default 1 check (confidence >= 0 and confidence <= 1),
  source text not null default 'manual'
    check (source in ('manual', 'agent', 'photo', 'scan', 'import', 'voice')),
  needs_review boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (workspace_id, item_id, location_id)
);

create table public.photos (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  object_type text not null check (object_type in ('item', 'location', 'inventory_entry', 'review')),
  object_id uuid not null,
  storage_path text not null,
  content_type text not null default 'image/jpeg',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.labels (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  object_type text not null check (object_type in ('location', 'item')),
  object_id uuid not null,
  code text not null,
  qr_url text not null default '',
  label_text text not null,
  template text not null default 'default',
  printed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (workspace_id, code)
);

create table public.hardware_bindings (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  location_id uuid not null references public.locations(id) on delete cascade,
  kind text not null check (kind in ('wled_segment', 'wled_pixels', 'eink_tag', 'nfc_tag')),
  controller_id text not null default '',
  address text not null default '',
  metadata jsonb not null default '{}'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.placement_rules (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  rule_text text not null,
  trigger_terms text[] not null default '{}',
  target_location_id uuid not null references public.locations(id) on delete cascade,
  source_command text not null default '',
  confidence real not null default 1 check (confidence >= 0 and confidence <= 1),
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.size_rules (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  rule_text text not null,
  trigger_terms text[] not null default '{}',
  size_class text not null check (size_class in ('tiny', 'small', 'medium', 'large', 'oversize')),
  source_command text not null default '',
  confidence real not null default 1 check (confidence >= 0 and confidence <= 1),
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.agent_examples (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references public.workspaces(id) on delete cascade,
  command text not null,
  context jsonb not null default '{}'::jsonb,
  proposed_plan jsonb not null default '[]'::jsonb,
  confirmed_plan jsonb not null default '[]'::jsonb,
  outcome text not null default 'confirmed'
    check (outcome in ('confirmed', 'corrected', 'rejected')),
  correction text not null default '',
  review_status text not null default 'pending'
    check (review_status in ('pending', 'approved', 'promoted', 'rejected')),
  reviewed_at timestamptz,
  created_at timestamptz not null default now()
);

create index locations_workspace_parent_idx on public.locations(workspace_id, parent_id);
create index locations_workspace_type_idx on public.locations(workspace_id, type);
create index items_workspace_category_idx on public.items(workspace_id, category);
create index inventory_entries_workspace_location_idx on public.inventory_entries(workspace_id, location_id);
create index photos_workspace_object_idx on public.photos(workspace_id, object_type, object_id);
create index placement_rules_workspace_active_idx on public.placement_rules(workspace_id, active);
create index size_rules_workspace_active_idx on public.size_rules(workspace_id, active);
create index agent_examples_workspace_review_idx on public.agent_examples(workspace_id, review_status);

create trigger set_workspaces_updated_at
before update on public.workspaces
for each row execute function public.set_updated_at();

create trigger set_locations_updated_at
before update on public.locations
for each row execute function public.set_updated_at();

create trigger set_items_updated_at
before update on public.items
for each row execute function public.set_updated_at();

create trigger set_inventory_entries_updated_at
before update on public.inventory_entries
for each row execute function public.set_updated_at();

create trigger set_labels_updated_at
before update on public.labels
for each row execute function public.set_updated_at();

create trigger set_hardware_bindings_updated_at
before update on public.hardware_bindings
for each row execute function public.set_updated_at();

create trigger set_placement_rules_updated_at
before update on public.placement_rules
for each row execute function public.set_updated_at();

create trigger set_size_rules_updated_at
before update on public.size_rules
for each row execute function public.set_updated_at();

create or replace function public.is_workspace_member(target_workspace_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.workspaces w
    where w.id = target_workspace_id
      and w.owner_id = auth.uid()
  )
  or exists (
    select 1 from public.workspace_members wm
    where wm.workspace_id = target_workspace_id
      and wm.user_id = auth.uid()
  );
$$;

create or replace function public.add_workspace_owner_member()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.workspace_members(workspace_id, user_id, role)
  values (new.id, new.owner_id, 'owner')
  on conflict (workspace_id, user_id) do nothing;
  return new;
end;
$$;

create trigger add_workspace_owner_member
after insert on public.workspaces
for each row execute function public.add_workspace_owner_member();

alter table public.workspaces enable row level security;
alter table public.workspace_members enable row level security;
alter table public.locations enable row level security;
alter table public.items enable row level security;
alter table public.item_aliases enable row level security;
alter table public.inventory_entries enable row level security;
alter table public.photos enable row level security;
alter table public.labels enable row level security;
alter table public.hardware_bindings enable row level security;
alter table public.placement_rules enable row level security;
alter table public.size_rules enable row level security;
alter table public.agent_examples enable row level security;

create policy "workspace members can read workspaces"
on public.workspaces for select
using (owner_id = auth.uid() or public.is_workspace_member(id));

create policy "workspace owners can manage workspaces"
on public.workspaces for all
using (owner_id = auth.uid())
with check (owner_id = auth.uid());

create policy "workspace members can read memberships"
on public.workspace_members for select
using (public.is_workspace_member(workspace_id));

create policy "workspace owners can manage memberships"
on public.workspace_members for all
using (
  exists (
    select 1 from public.workspaces w
    where w.id = workspace_members.workspace_id
      and w.owner_id = auth.uid()
  )
)
with check (
  exists (
    select 1 from public.workspaces w
    where w.id = workspace_members.workspace_id
      and w.owner_id = auth.uid()
  )
);

create policy "workspace members can use locations"
on public.locations for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use items"
on public.items for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use inventory entries"
on public.inventory_entries for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use photos"
on public.photos for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use labels"
on public.labels for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use hardware bindings"
on public.hardware_bindings for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use placement rules"
on public.placement_rules for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use size rules"
on public.size_rules for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use agent examples"
on public.agent_examples for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

create policy "workspace members can use aliases through items"
on public.item_aliases for all
using (
  exists (
    select 1 from public.items i
    where i.id = item_aliases.item_id
      and public.is_workspace_member(i.workspace_id)
  )
)
with check (
  exists (
    select 1 from public.items i
    where i.id = item_aliases.item_id
      and public.is_workspace_member(i.workspace_id)
  )
);
