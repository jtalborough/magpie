-- Apply a reviewed CSV import, including proposed storage paths, in one
-- transaction. The API performs semantic preflight; this RPC revalidates the
-- destination contract at the database boundary.
create or replace function public.magpie_import_inventory(
  target_workspace_id uuid,
  location_rows jsonb,
  inventory_rows jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  inventory_row jsonb;
  alias_value text;
  item_name text;
  item_uuid uuid;
  item_local_id bigint;
  merged_target uuid;
  location_uuid uuid;
  location_code text;
  requested_quantity numeric;
  requested_unit text;
  imported_rows jsonb := '[]'::jsonb;
begin
  if jsonb_typeof(coalesce(location_rows, '[]'::jsonb)) <> 'array'
    or jsonb_array_length(coalesce(location_rows, '[]'::jsonb)) > 121
  then
    raise exception using
      errcode = '22023',
      message = 'location_rows must be an array with at most 121 places';
  end if;
  if jsonb_typeof(inventory_rows) <> 'array'
    or jsonb_array_length(inventory_rows) < 1
    or jsonb_array_length(inventory_rows) > 5000
  then
    raise exception using
      errcode = '22023',
      message = 'inventory_rows must contain between 1 and 5000 rows';
  end if;

  if jsonb_array_length(coalesce(location_rows, '[]'::jsonb)) > 0 then
    perform public.magpie_create_locations(
      target_workspace_id,
      location_rows
    );
  end if;

  for inventory_row in
    select value from jsonb_array_elements(inventory_rows)
  loop
    item_name := btrim(inventory_row->>'name');
    location_code := btrim(inventory_row->>'location_code');
    requested_quantity := nullif(inventory_row->>'quantity', '')::numeric;
    requested_unit := coalesce(nullif(btrim(inventory_row->>'unit'), ''), 'each');
    if item_name = '' then
      raise exception using errcode = '22023', message = 'item name is required';
    end if;
    if requested_quantity is null then
      raise exception using errcode = '22023', message = 'quantity is required';
    end if;

    select location.id
    into location_uuid
    from public.locations location
    where location.workspace_id = target_workspace_id
      and location.local_code = location_code
      and location.active
      and location.accepts_items;
    if location_uuid is null then
      raise exception using
        errcode = '23503',
        message = 'unknown, archived, or organizer-only location: ' || location_code;
    end if;

    select item.id, item.merged_into_item_id
    into item_uuid, merged_target
    from public.items item
    where item.workspace_id = target_workspace_id
      and item.name = item_name;

    if merged_target is not null then
      item_uuid := merged_target;
      insert into public.item_aliases(item_id, alias)
      values (item_uuid, item_name)
      on conflict (item_id, alias) do nothing;
    else
      insert into public.items (
        workspace_id, name, description, category, active, archived_at
      ) values (
        target_workspace_id,
        item_name,
        coalesce(inventory_row->>'description', ''),
        coalesce(inventory_row->>'category', ''),
        true,
        null
      )
      on conflict (workspace_id, name) do update set
        description = excluded.description,
        category = excluded.category,
        active = true,
        archived_at = null,
        updated_at = now()
      returning id into item_uuid;
    end if;

    select item.local_id
    into item_local_id
    from public.items item
    where item.id = item_uuid
      and item.workspace_id = target_workspace_id;
    if item_local_id is null then
      raise exception using errcode = '23503', message = 'item target is missing';
    end if;

    for alias_value in
      select btrim(value)
      from jsonb_array_elements_text(
        coalesce(inventory_row->'aliases', '[]'::jsonb)
      )
    loop
      if alias_value <> '' then
        insert into public.item_aliases(item_id, alias)
        values (item_uuid, alias_value)
        on conflict (item_id, alias) do nothing;
      end if;
    end loop;

    insert into public.inventory_entries (
      workspace_id, item_id, location_id, quantity, unit, status,
      confidence, source, needs_review, review_reason
    ) values (
      target_workspace_id, item_uuid, location_uuid, requested_quantity,
      requested_unit, 'present', 1, 'import', false, ''
    )
    on conflict (workspace_id, item_id, location_id) do update set
      quantity = excluded.quantity,
      unit = excluded.unit,
      status = 'present',
      confidence = 1,
      source = 'import',
      needs_review = false,
      review_reason = '',
      updated_at = now();

    update public.items
    set last_seen_location_id = location_uuid,
        home_location_id = coalesce(home_location_id, location_uuid),
        active = true,
        archived_at = null,
        updated_at = now()
    where id = item_uuid;

    insert into public.repository_events (
      workspace_id, event_type, item_id, item_local_id,
      location_id, location_code, quantity, note
    ) values (
      target_workspace_id, 'inventory_put', item_uuid, item_local_id,
      location_uuid, location_code, requested_quantity, 'CSV import'
    );

    imported_rows := imported_rows || jsonb_build_array(jsonb_build_object(
      'item_id', item_local_id,
      'name', item_name,
      'location_id', location_code,
      'quantity', requested_quantity,
      'unit', requested_unit
    ));
  end loop;

  return jsonb_build_object(
    'location_ids', coalesce(
      (select jsonb_agg(value->>'local_code') from jsonb_array_elements(coalesce(location_rows, '[]'::jsonb))),
      '[]'::jsonb
    ),
    'rows', imported_rows
  );
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
      'atomic_inventory_import', true
    )
  );
$$;
