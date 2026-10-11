-- Return only the latest matching event for each physical location.
-- This keeps Storage confidence reads proportional to places, not audit history.
create or replace function public.magpie_latest_location_events(
  target_workspace_id uuid,
  target_event_type text
)
returns table (
  local_id bigint,
  event_type text,
  item_local_id bigint,
  location_code text,
  quantity numeric,
  note text,
  created_at timestamptz
)
language sql
stable
security invoker
set search_path = public
as $$
  select distinct on (event.location_code)
    event.local_id,
    event.event_type,
    event.item_local_id,
    event.location_code,
    event.quantity,
    event.note,
    event.created_at
  from public.repository_events event
  where event.workspace_id = target_workspace_id
    and event.event_type = target_event_type
    and event.location_code <> ''
  order by event.location_code, event.local_id desc;
$$;
