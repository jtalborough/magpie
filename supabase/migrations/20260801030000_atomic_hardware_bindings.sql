-- Apply a complete visual hardware commissioning map in one transaction.
create or replace function public.magpie_set_hardware_bindings(
  target_workspace_id uuid,
  binding_rows jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  binding_row jsonb;
  target_location public.locations%rowtype;
  clean_kind text;
  clean_controller text;
  clean_address text;
  clean_metadata jsonb;
  target_active boolean;
  saved_binding public.hardware_bindings%rowtype;
  saved_rows jsonb := '[]'::jsonb;
begin
  if jsonb_typeof(binding_rows) <> 'array'
    or jsonb_array_length(binding_rows) < 1
    or jsonb_array_length(binding_rows) > 500
  then
    raise exception using
      errcode = '22023',
      message = 'binding_rows must contain between 1 and 500 bindings';
  end if;

  perform pg_advisory_xact_lock(
    hashtext(target_workspace_id::text),
    hashtext('magpie_hardware_bindings')
  );

  if exists (
    select 1
    from (
      select
        coalesce(entry->>'controller_id', '') as controller_id,
        pixel.value::integer as pixel
      from jsonb_array_elements(binding_rows) entry
      cross join lateral jsonb_array_elements_text(
        coalesce(entry->'metadata'->'pixels', '[]'::jsonb)
      ) pixel
      where lower(replace(btrim(entry->>'kind'), '-', '_')) = 'wled_pixels'
        and coalesce((entry->>'active')::boolean, true)
    ) requested
    group by requested.controller_id, requested.pixel
    having count(*) > 1
  ) then
    raise exception using
      errcode = '23505',
      message = 'a WLED pixel is assigned more than once in this batch';
  end if;

  if exists (
    with target_locations as (
      select location.id
      from public.locations location
      join jsonb_array_elements(binding_rows) entry
        on location.local_code = btrim(entry->>'location_code')
      where location.workspace_id = target_workspace_id
    ),
    requested_pixels as (
      select
        coalesce(entry->>'controller_id', '') as controller_id,
        pixel.value::integer as pixel
      from jsonb_array_elements(binding_rows) entry
      cross join lateral jsonb_array_elements_text(
        coalesce(entry->'metadata'->'pixels', '[]'::jsonb)
      ) pixel
      where lower(replace(btrim(entry->>'kind'), '-', '_')) = 'wled_pixels'
        and coalesce((entry->>'active')::boolean, true)
    ),
    existing_pixels as (
      select binding.controller_id, pixel.value::integer as pixel
      from public.hardware_bindings binding
      cross join lateral jsonb_array_elements_text(
        coalesce(binding.metadata->'pixels', '[]'::jsonb)
      ) pixel
      where binding.workspace_id = target_workspace_id
        and binding.kind = 'wled_pixels'
        and binding.active
        and binding.location_id not in (select id from target_locations)
    )
    select 1
    from requested_pixels requested
    join existing_pixels existing
      on existing.controller_id = requested.controller_id
      and existing.pixel = requested.pixel
  ) then
    raise exception using
      errcode = '23505',
      message = 'a WLED pixel is already assigned outside this batch';
  end if;

  for binding_row in select value from jsonb_array_elements(binding_rows)
  loop
    select location.*
    into target_location
    from public.locations location
    where location.workspace_id = target_workspace_id
      and location.local_code = btrim(binding_row->>'location_code')
      and location.active;
    if not found then
      raise exception using
        errcode = '23503',
        message = 'unknown or archived location: ' || btrim(binding_row->>'location_code');
    end if;

    clean_kind := lower(replace(btrim(binding_row->>'kind'), '-', '_'));
    if clean_kind not in ('wled_segment', 'wled_pixels', 'eink_tag', 'nfc_tag') then
      raise exception using
        errcode = '22023',
        message = 'unsupported hardware binding kind';
    end if;
    clean_controller := coalesce(btrim(binding_row->>'controller_id'), '');
    clean_address := coalesce(btrim(binding_row->>'address'), '');
    clean_metadata := coalesce(binding_row->'metadata', '{}'::jsonb);
    target_active := coalesce((binding_row->>'active')::boolean, true);

    if target_active then
      update public.hardware_bindings binding
      set active = false
      where binding.workspace_id = target_workspace_id
        and binding.location_id = target_location.id
        and binding.kind = clean_kind
        and binding.controller_id = clean_controller;
    end if;

    insert into public.hardware_bindings (
      workspace_id, location_id, kind, controller_id, address, metadata, active
    ) values (
      target_workspace_id, target_location.id, clean_kind, clean_controller,
      clean_address, clean_metadata, target_active
    )
    on conflict (workspace_id, location_id, kind, controller_id, address)
    do update set
      metadata = excluded.metadata,
      active = excluded.active,
      updated_at = now()
    returning * into saved_binding;

    if clean_kind = 'wled_pixels' then
      update public.locations location
      set light_target = case when target_active then nullif(clean_controller, '') else null end,
          light_indexes = case
            when target_active then array(
              select value::integer
              from jsonb_array_elements_text(
                coalesce(clean_metadata->'pixels', '[]'::jsonb)
              )
            )
            else '{}'::integer[]
          end
      where location.id = target_location.id;
    end if;

    saved_rows := saved_rows || jsonb_build_array(jsonb_build_object(
      'local_id', saved_binding.local_id,
      'location_code', target_location.local_code,
      'kind', saved_binding.kind,
      'controller_id', saved_binding.controller_id,
      'address', saved_binding.address,
      'metadata', saved_binding.metadata,
      'active', saved_binding.active
    ));
  end loop;

  return jsonb_build_object('bindings', saved_rows);
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
      'atomic_hardware_bindings', true
    )
  );
$$;
