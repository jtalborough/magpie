-- Create a visually planned location structure in one database transaction.
-- Parent rows must appear before their children in location_rows.
create or replace function public.magpie_create_locations(
  target_workspace_id uuid,
  location_rows jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  location_row jsonb;
  parent_code text;
  parent_uuid uuid;
  created_code text;
  created_codes text[] := array[]::text[];
begin
  if jsonb_typeof(location_rows) <> 'array'
    or jsonb_array_length(location_rows) < 1
    or jsonb_array_length(location_rows) > 121
  then
    raise exception using
      errcode = '22023',
      message = 'location_rows must contain between 1 and 121 places';
  end if;

  for location_row in
    select value from jsonb_array_elements(location_rows)
  loop
    created_code := btrim(location_row->>'local_code');
    if created_code = '' then
      raise exception using
        errcode = '22023',
        message = 'location local_code is required';
    end if;

    parent_code := nullif(btrim(location_row->>'parent_code'), '');
    parent_uuid := null;
    if parent_code is not null then
      select location.id
      into parent_uuid
      from public.locations location
      where location.workspace_id = target_workspace_id
        and location.local_code = parent_code
        and location.active;
      if parent_uuid is null then
        raise exception using
          errcode = '23503',
          message = 'unknown or archived parent location: ' || parent_code;
      end if;
    end if;

    insert into public.locations (
      workspace_id, local_code, parent_id, type, label, grid,
      cell_col, cell_row, cell_w, cell_h, light_target, light_indexes,
      capacity_class, purpose_tags, placement_notes, internal_dimensions,
      accepts_items, active, archived_at
    ) values (
      target_workspace_id,
      created_code,
      parent_uuid,
      location_row->>'type',
      location_row->>'label',
      coalesce(location_row->'grid', '{}'::jsonb),
      nullif(location_row->>'cell_col', '')::integer,
      nullif(location_row->>'cell_row', '')::integer,
      coalesce(nullif(location_row->>'cell_w', '')::integer, 1),
      coalesce(nullif(location_row->>'cell_h', '')::integer, 1),
      nullif(location_row->>'light_target', ''),
      array(
        select value::integer
        from jsonb_array_elements_text(
          coalesce(location_row->'light_indexes', '[]'::jsonb)
        )
      ),
      coalesce(nullif(location_row->>'capacity_class', ''), 'medium'),
      array(
        select value
        from jsonb_array_elements_text(
          coalesce(location_row->'purpose_tags', '[]'::jsonb)
        )
      ),
      coalesce(location_row->>'placement_notes', ''),
      coalesce(location_row->'internal_dimensions', '{}'::jsonb),
      coalesce((location_row->>'accepts_items')::boolean, true),
      true,
      null
    );
    created_codes := array_append(created_codes, created_code);
  end loop;

  return jsonb_build_object('location_ids', to_jsonb(created_codes));
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
      'atomic_location_creation', true
    )
  );
$$;
