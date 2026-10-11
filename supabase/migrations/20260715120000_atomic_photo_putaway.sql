-- Keep the common one-item put-away and its photos in one transaction.
-- The Pi calls this with the service role; browser clients continue to use RLS.
create or replace function public.magpie_put_item_with_photos(
  target_workspace_id uuid,
  target_item_name text,
  target_location_code text,
  requested_quantity numeric default 1,
  requested_unit text default 'each',
  quantity_mode text default 'set',
  target_photo_local_ids bigint[] default '{}',
  mark_for_review boolean default false,
  target_review_reason text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  clean_name text := btrim(target_item_name);
  clean_unit text := coalesce(nullif(btrim(requested_unit), ''), 'each');
  clean_mode text := lower(coalesce(nullif(btrim(quantity_mode), ''), 'set'));
  clean_photo_ids bigint[];
  location_row public.locations%rowtype;
  item_row public.items%rowtype;
  inventory_row public.inventory_entries%rowtype;
  previous_quantity numeric;
  previous_unit text;
  final_quantity numeric;
  matched_photo_count integer;
begin
  if clean_name = '' then
    raise exception using errcode = '22023', message = 'item name is required';
  end if;
  if requested_quantity is null or requested_quantity < 0 then
    raise exception using errcode = '22023', message = 'quantity must be zero or greater';
  end if;
  if clean_mode not in ('set', 'add') then
    raise exception using errcode = '22023', message = 'quantity_mode must be set or add';
  end if;

  select * into location_row
  from public.locations
  where workspace_id = target_workspace_id
    and (
      local_code = target_location_code
      or local_code = upper(target_location_code)
    )
  order by (local_code = target_location_code) desc
  limit 1;
  if not found then
    raise exception using errcode = 'P0002', message = format('location %s not found', target_location_code);
  end if;

  select coalesce(array_agg(distinct photo_id order by photo_id), '{}'::bigint[])
  into clean_photo_ids
  from unnest(coalesce(target_photo_local_ids, '{}'::bigint[])) as photo_id;

  select count(*) into matched_photo_count
  from public.photos
  where workspace_id = target_workspace_id
    and local_id = any(clean_photo_ids);
  if matched_photo_count <> cardinality(clean_photo_ids) then
    raise exception using errcode = 'P0002', message = 'one or more photos were not found';
  end if;

  insert into public.items(workspace_id, name)
  values (target_workspace_id, clean_name)
  on conflict (workspace_id, name) do update set name = excluded.name
  returning * into item_row;

  -- Serialize additive writes even when no inventory row exists yet.
  perform pg_advisory_xact_lock(
    hashtext(target_workspace_id::text),
    hashtext(item_row.id::text || ':' || location_row.id::text)
  );

  select * into inventory_row
  from public.inventory_entries
  where workspace_id = target_workspace_id
    and item_id = item_row.id
    and location_id = location_row.id
  for update;

  if found then
    previous_quantity := inventory_row.quantity;
    previous_unit := inventory_row.unit;
  end if;
  final_quantity := case
    when clean_mode = 'add' and previous_quantity is not null
      then previous_quantity + requested_quantity
    else requested_quantity
  end;

  insert into public.inventory_entries(
    workspace_id, item_id, location_id, quantity, unit, status,
    source, needs_review, review_reason
  )
  values (
    target_workspace_id, item_row.id, location_row.id, final_quantity, clean_unit,
    case when mark_for_review then 'review' else 'present' end,
    'agent', mark_for_review,
    case when mark_for_review then btrim(target_review_reason) else '' end
  )
  on conflict (workspace_id, item_id, location_id) do update set
    quantity = excluded.quantity,
    unit = excluded.unit,
    status = excluded.status,
    source = excluded.source,
    needs_review = excluded.needs_review,
    review_reason = excluded.review_reason
  returning * into inventory_row;

  update public.items
  set
    last_seen_location_id = location_row.id,
    home_location_id = coalesce(home_location_id, location_row.id)
  where id = item_row.id;

  update public.photos
  set
    item_id = item_row.id,
    object_type = 'item',
    object_id = item_row.id
  where workspace_id = target_workspace_id
    and local_id = any(clean_photo_ids);

  if mark_for_review then
    insert into public.inventory_review_photos(
      workspace_id, inventory_entry_id, photo_id
    )
    select target_workspace_id, inventory_row.id, photo.id
    from public.photos as photo
    where photo.workspace_id = target_workspace_id
      and photo.local_id = any(clean_photo_ids)
    on conflict (inventory_entry_id, photo_id) do nothing;
  end if;

  insert into public.repository_events(
    workspace_id, event_type, item_id, item_local_id, location_id,
    location_code, quantity, note
  )
  values (
    target_workspace_id, 'inventory_put', item_row.id, item_row.local_id,
    location_row.id, location_row.local_code, final_quantity,
    case when mark_for_review then 'needs_review' else '' end
  );

  return jsonb_build_object(
    'stored', item_row.name,
    'item_id', item_row.local_id,
    'location_id', location_row.local_code,
    'needs_review', mark_for_review,
    'previous_quantity', previous_quantity,
    'previous_unit', previous_unit,
    'quantity', final_quantity,
    'photo_ids', clean_photo_ids
  );
end;
$$;

revoke all on function public.magpie_put_item_with_photos(
  uuid, text, text, numeric, text, text, bigint[], boolean, text
) from public;
revoke all on function public.magpie_put_item_with_photos(
  uuid, text, text, numeric, text, text, bigint[], boolean, text
) from anon;
revoke all on function public.magpie_put_item_with_photos(
  uuid, text, text, numeric, text, text, bigint[], boolean, text
) from authenticated;
grant execute on function public.magpie_put_item_with_photos(
  uuid, text, text, numeric, text, text, bigint[], boolean, text
) to service_role;
