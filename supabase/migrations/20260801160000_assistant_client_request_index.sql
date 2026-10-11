create index if not exists assistant_interactions_workspace_client_request_idx
  on public.assistant_interactions(
    workspace_id,
    (response ->> 'client_request_id'),
    route,
    local_id desc
  )
  where response ? 'client_request_id';
