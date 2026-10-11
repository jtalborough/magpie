-- Commit a guided physical contents check and its review-state changes in one
-- transaction. The supplied rows are a complete snapshot of active placements
-- in the selected location subtree; any concurrent inventory change aborts the
-- check instead of recording stale confidence.
create or replace function public.magpie_apply_location_audit(
  target_workspace_id uuid,
  root_location_code text,
  audit_rows jsonb,
  verification_note text
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  root_location public.locations%rowtype;
  target_entry public.inventory_entries%rowtype;
  target_item public.items%rowtype;
  target_location public.locations%rowtype;
  audit_row jsonb;
  subtree_ids uuid[];
  current_count integer;
  item_local_id bigint;
  location_code text;
  observed_status text;
  next_needs_review boolean;
  next_review_reason text;
  updates jsonb := '[]'::jsonb;
begin
  if jsonb_typeof(audit_rows) <> 'array'
    or jsonb_array_length(audit_rows) > 100
  then
    raise exception using
      errcode = '22023',
      message = 'audit_rows must be an array with at most 100 placements';
  end if;

  select location.*
  into root_location
  from public.locations location
  where location.workspace_id = target_workspace_id
    and location.local_code = btrim(root_location_code)
    and location.active;
  if root_location.id is null then
    raise exception using errcode = '23503', message = 'unknown location';
  end if;

  with recursive subtree as (
    select root_location.id
    union all
    select child.id
    from public.locations child
    join subtree parent on child.parent_id = parent.id
    where child.workspace_id = target_workspace_id
      and child.active
  )
  select array_agg(id) into subtree_ids from subtree;

  -- Lock existing placements before their locations, matching inventory-write
  -- trigger order. The location lock then blocks new placements and topology
  -- changes until the verification event commits.
  perform 1
  from public.inventory_entries entry
  join public.items item on item.id = entry.item_id
  where entry.workspace_id = target_workspace_id
    and entry.location_id = any(subtree_ids)
    and item.active
  order by entry.id
  for update of entry, item;
  perform 1
  from public.locations location
  where location.id = any(subtree_ids)
  order by location.id
  for update;

  select count(*)
  into current_count
  from public.inventory_entries entry
  join public.items item on item.id = entry.item_id
  where entry.workspace_id = target_workspace_id
    and entry.location_id = any(subtree_ids)
    and item.active;

  if current_count <> jsonb_array_length(audit_rows) then
    raise exception using
      errcode = '40001',
      message = 'inventory changed while this contents check was open';
  end if;
  if jsonb_array_length(audit_rows) <> (
    select count(*)
    from (
      select distinct value->>'item_id' as item_id,
                      value->>'location_id' as location_id
      from jsonb_array_elements(audit_rows)
    ) unique_rows
  ) then
    raise exception using
      errcode = '22023',
      message = 'contents check contains a duplicate placement';
  end if;

  for audit_row in select value from jsonb_array_elements(audit_rows)
  loop
    item_local_id := nullif(audit_row->>'item_id', '')::bigint;
    location_code := btrim(audit_row->>'location_id');
    observed_status := audit_row->>'status';
    if observed_status not in ('present', 'missing') then
      raise exception using
        errcode = '22023',
        message = 'contents check status must be present or missing';
    end if;

    select entry.*
    into target_entry
    from public.inventory_entries entry
    join public.items item on item.id = entry.item_id
    join public.locations location on location.id = entry.location_id
    where entry.workspace_id = target_workspace_id
      and item.workspace_id = target_workspace_id
      and item.local_id = item_local_id
      and item.active
      and location.workspace_id = target_workspace_id
      and location.local_code = location_code
      and location.id = any(subtree_ids)
    for update of entry;
    if target_entry.id is null
      or target_entry.quantity <> (audit_row->>'expected_quantity')::numeric
      or target_entry.unit <> audit_row->>'expected_unit'
      or target_entry.needs_review <> (audit_row->>'expected_needs_review')::boolean
    then
      raise exception using
        errcode = '40001',
        message = 'inventory changed while this contents check was open';
    end if;

    select item.* into target_item
    from public.items item where item.id = target_entry.item_id;
    select location.* into target_location
    from public.locations location where location.id = target_entry.location_id;
    next_needs_review := observed_status = 'missing';
    next_review_reason := case
      when next_needs_review then btrim(coalesce(audit_row->>'review_reason', ''))
      else ''
    end;
    if target_entry.needs_review = next_needs_review
      and target_entry.review_reason = next_review_reason
    then
      continue;
    end if;

    update public.inventory_entries entry
    set needs_review = next_needs_review,
        review_reason = next_review_reason,
        status = case when next_needs_review then 'review' else 'present' end,
        updated_at = now()
    where entry.id = target_entry.id;

    insert into public.repository_events (
      workspace_id, event_type, item_id, item_local_id,
      location_id, location_code, note
    ) values (
      target_workspace_id,
      case when next_needs_review then 'inventory_needs_review' else 'inventory_reviewed' end,
      target_item.id,
      target_item.local_id,
      target_location.id,
      target_location.local_code,
      next_review_reason
    );
    updates := updates || jsonb_build_array(jsonb_build_object(
      'item_id', target_item.local_id,
      'location_id', target_location.local_code,
      'status', observed_status,
      'previous_needs_review', target_entry.needs_review,
      'previous_review_reason', target_entry.review_reason
    ));
  end loop;

  insert into public.repository_events (
    workspace_id, event_type, location_id, location_code, quantity, note
  )
  select
    target_workspace_id,
    'location_contents_verified',
    location.id,
    location.local_code,
    case
      when location.id = root_location.id then current_count
      else (
        select count(*)
        from jsonb_array_elements(audit_rows) row
        where row->>'location_id' = location.local_code
      )
    end,
    btrim(verification_note)
  from public.locations location
  where location.id = any(subtree_ids);

  return jsonb_build_object('updates', updates);
end;
$$;

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
      'guided_location_audits', true
    )
  );
$$;
