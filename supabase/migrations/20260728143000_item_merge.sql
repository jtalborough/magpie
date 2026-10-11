alter table public.items
  add column if not exists merged_into_item_id uuid
    references public.items(id) on delete restrict;

create index if not exists items_workspace_merged_into_idx
  on public.items(workspace_id, merged_into_item_id);

create or replace function public.magpie_merge_items(
  target_workspace_id uuid,
  source_item_local_id bigint,
  canonical_item_local_id bigint
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  source_item public.items%rowtype;
  canonical_item public.items%rowtype;
  source_entry public.inventory_entries%rowtype;
  canonical_entry public.inventory_entries%rowtype;
begin
  if source_item_local_id = canonical_item_local_id then
    raise exception 'source and canonical item must be different';
  end if;

  select * into source_item
  from public.items
  where workspace_id = target_workspace_id
    and local_id = source_item_local_id
  for update;

  select * into canonical_item
  from public.items
  where workspace_id = target_workspace_id
    and local_id = canonical_item_local_id
  for update;

  if source_item.id is null or canonical_item.id is null then
    raise exception 'item not found';
  end if;
  if not source_item.active or source_item.merged_into_item_id is not null then
    raise exception 'source item is not active';
  end if;
  if not canonical_item.active or canonical_item.merged_into_item_id is not null then
    raise exception 'canonical item is not active';
  end if;
  if exists (
    select 1
    from public.inventory_entries source_inventory
    join public.inventory_entries canonical_inventory
      on canonical_inventory.workspace_id = source_inventory.workspace_id
     and canonical_inventory.location_id = source_inventory.location_id
     and canonical_inventory.item_id = canonical_item.id
    where source_inventory.workspace_id = target_workspace_id
      and source_inventory.item_id = source_item.id
      and lower(source_inventory.unit) <> lower(canonical_inventory.unit)
  ) then
    raise exception 'items use different units in the same location';
  end if;

  insert into public.item_aliases(item_id, alias)
  values (canonical_item.id, source_item.name)
  on conflict do nothing;

  insert into public.item_aliases(item_id, alias)
  select canonical_item.id, alias
  from public.item_aliases
  where item_id = source_item.id
  on conflict do nothing;

  for source_entry in
    select * from public.inventory_entries
    where workspace_id = target_workspace_id
      and item_id = source_item.id
    order by created_at, id
    for update
  loop
    select * into canonical_entry
    from public.inventory_entries
    where workspace_id = target_workspace_id
      and item_id = canonical_item.id
      and location_id = source_entry.location_id
    for update;

    if canonical_entry.id is null then
      update public.inventory_entries
      set item_id = canonical_item.id,
          updated_at = now()
      where id = source_entry.id;
    else
      insert into public.inventory_review_photos(
        workspace_id, inventory_entry_id, photo_id
      )
      select target_workspace_id, canonical_entry.id, photo_id
      from public.inventory_review_photos
      where inventory_entry_id = source_entry.id
      on conflict do nothing;

      update public.inventory_entries
      set quantity = canonical_entry.quantity + source_entry.quantity,
          needs_review = canonical_entry.needs_review or source_entry.needs_review,
          review_reason = case
            when canonical_entry.review_reason <> '' then canonical_entry.review_reason
            else source_entry.review_reason
          end,
          updated_at = now()
      where id = canonical_entry.id;

      delete from public.inventory_entries where id = source_entry.id;
    end if;
    canonical_entry := null;
  end loop;

  update public.photos
  set item_id = canonical_item.id,
      object_id = case
        when object_type = 'item' and object_id = source_item.id
          then canonical_item.id
        else object_id
      end
  where workspace_id = target_workspace_id
    and (
      item_id = source_item.id
      or (object_type = 'item' and object_id = source_item.id)
    );

  update public.items
  set description = case
        when description = '' then source_item.description else description
      end,
      category = case when category = '' then source_item.category else category end,
      size_class = case
        when size_class = '' then source_item.size_class else size_class
      end,
      size_source = case
        when size_class = '' then source_item.size_source else size_source
      end,
      size_confidence = case
        when size_class = '' then source_item.size_confidence else size_confidence
      end,
      dimensions = case
        when dimensions = '{}'::jsonb then source_item.dimensions else dimensions
      end,
      home_location_id = coalesce(home_location_id, source_item.home_location_id),
      last_seen_location_id = coalesce(
        last_seen_location_id, source_item.last_seen_location_id
      ),
      updated_at = now()
  where id = canonical_item.id;

  update public.repository_events
  set item_id = canonical_item.id,
      item_local_id = canonical_item.local_id
  where workspace_id = target_workspace_id
    and item_id = source_item.id;

  delete from public.item_aliases where item_id = source_item.id;

  update public.items
  set active = false,
      archived_at = now(),
      merged_into_item_id = canonical_item.id,
      updated_at = now()
  where id = source_item.id;

  insert into public.repository_events(
    workspace_id, event_type, item_id, item_local_id, note
  )
  values (
    target_workspace_id,
    'item_merged',
    canonical_item.id,
    canonical_item.local_id,
    source_item.name || ' merged into ' || canonical_item.name
  );

  return jsonb_build_object(
    'merged', true,
    'source_item_id', source_item.local_id,
    'canonical_item_id', canonical_item.local_id
  );
end;
$$;

revoke all on function public.magpie_merge_items(uuid, bigint, bigint)
  from public, anon;
grant execute on function public.magpie_merge_items(uuid, bigint, bigint)
  to authenticated, service_role;
