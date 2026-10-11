-- Product readiness checks milestone event types without reading interaction payloads.
create index if not exists repository_events_workspace_type_idx
  on public.repository_events(workspace_id, event_type, local_id desc);
