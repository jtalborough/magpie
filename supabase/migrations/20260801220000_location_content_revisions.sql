alter table public.locations
  add column if not exists content_revision bigint not null default 0;

create or replace function public.magpie_bump_location_content_revisions(
  target_workspace_id uuid,
  starting_location_ids uuid[]
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  with recursive affected as (
    select location.id, location.parent_id
    from public.locations location
    where location.workspace_id = target_workspace_id
      and location.id = any(coalesce(starting_location_ids, '{}'::uuid[]))
    union
    select parent.id, parent.parent_id
    from public.locations parent
    join affected child on child.parent_id = parent.id
    where parent.workspace_id = target_workspace_id
  )
  update public.locations location
  set content_revision = location.content_revision + 1
  where location.workspace_id = target_workspace_id
    and location.id in (select id from affected);
end;
$$;

revoke all on function public.magpie_bump_location_content_revisions(uuid, uuid[])
  from public, anon, authenticated;

create or replace function public.magpie_inventory_content_revision_trigger()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  target_workspace_id uuid;
  target_location_ids uuid[];
begin
  if tg_op = 'INSERT' then
    target_workspace_id := new.workspace_id;
    target_location_ids := array[new.location_id];
  elsif tg_op = 'DELETE' then
    target_workspace_id := old.workspace_id;
    target_location_ids := array[old.location_id];
  else
    target_workspace_id := new.workspace_id;
    target_location_ids := array[old.location_id, new.location_id];
  end if;
  perform public.magpie_bump_location_content_revisions(
    target_workspace_id,
    target_location_ids
  );
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

drop trigger if exists inventory_content_revision_insert on public.inventory_entries;
create trigger inventory_content_revision_insert
after insert on public.inventory_entries
for each row execute function public.magpie_inventory_content_revision_trigger();

drop trigger if exists inventory_content_revision_delete on public.inventory_entries;
create trigger inventory_content_revision_delete
after delete on public.inventory_entries
for each row execute function public.magpie_inventory_content_revision_trigger();

drop trigger if exists inventory_content_revision_update on public.inventory_entries;
create trigger inventory_content_revision_update
after update of item_id, location_id, quantity, unit on public.inventory_entries
for each row
when (
  old.item_id is distinct from new.item_id
  or old.location_id is distinct from new.location_id
  or old.quantity is distinct from new.quantity
  or old.unit is distinct from new.unit
)
execute function public.magpie_inventory_content_revision_trigger();

create or replace function public.magpie_item_active_content_revision_trigger()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  target_location_ids uuid[];
begin
  select array_agg(distinct entry.location_id)
  into target_location_ids
  from public.inventory_entries entry
  where entry.workspace_id = new.workspace_id
    and entry.item_id = new.id;
  perform public.magpie_bump_location_content_revisions(
    new.workspace_id,
    target_location_ids
  );
  return new;
end;
$$;

drop trigger if exists item_active_content_revision_update on public.items;
create trigger item_active_content_revision_update
after update of active on public.items
for each row
when (old.active is distinct from new.active)
execute function public.magpie_item_active_content_revision_trigger();

create or replace function public.magpie_location_structure_revision_trigger()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  target_workspace_id uuid;
  target_location_ids uuid[];
begin
  if tg_op = 'INSERT' then
    target_workspace_id := new.workspace_id;
    target_location_ids := array[new.parent_id];
  elsif tg_op = 'DELETE' then
    target_workspace_id := old.workspace_id;
    target_location_ids := array[old.parent_id];
  else
    target_workspace_id := new.workspace_id;
    target_location_ids := array[new.id, old.parent_id, new.parent_id];
  end if;
  perform public.magpie_bump_location_content_revisions(
    target_workspace_id,
    array_remove(target_location_ids, null)
  );
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

drop trigger if exists location_structure_revision_insert on public.locations;
create trigger location_structure_revision_insert
after insert on public.locations
for each row
when (new.parent_id is not null)
execute function public.magpie_location_structure_revision_trigger();

drop trigger if exists location_structure_revision_delete on public.locations;
create trigger location_structure_revision_delete
after delete on public.locations
for each row
when (old.parent_id is not null)
execute function public.magpie_location_structure_revision_trigger();

drop trigger if exists location_structure_revision_update on public.locations;
create trigger location_structure_revision_update
after update of parent_id, active on public.locations
for each row
when (
  old.parent_id is distinct from new.parent_id
  or old.active is distinct from new.active
)
execute function public.magpie_location_structure_revision_trigger();

create or replace function public.magpie_location_inventory_metrics(
  target_workspace_id uuid
)
returns table (
  location_code text,
  item_count bigint,
  review_count bigint,
  content_revision bigint
)
language sql
stable
security invoker
set search_path = public
as $$
  select
    location.local_code,
    count(item.id)::bigint,
    count(item.id) filter (where entry.needs_review)::bigint,
    location.content_revision
  from public.locations location
  left join public.inventory_entries entry
    on entry.workspace_id = target_workspace_id
   and entry.location_id = location.id
  left join public.items item
    on item.workspace_id = target_workspace_id
   and item.id = entry.item_id
   and item.active
  where location.workspace_id = target_workspace_id
  group by location.id, location.local_code, location.content_revision
  order by location.local_code;
$$;

grant execute on function public.magpie_location_inventory_metrics(uuid)
  to authenticated, service_role;

create or replace function public.magpie_schema_capabilities()
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'schema', 'magpie.supabase_capabilities.v1',
    'features', jsonb_build_object(
      'atomic_location_lifecycle', true,
      'atomic_location_creation', true,
      'atomic_hardware_bindings', true,
      'atomic_inventory_import', true,
      'archive_group_provenance', true,
      'guided_location_audits', true,
      'location_content_revisions', true
    )
  );
$$;
