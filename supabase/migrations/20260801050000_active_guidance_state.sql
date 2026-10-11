-- Persist the active find and put-away color list across service restarts.
create table public.active_guidance_states (
  workspace_id uuid primary key references public.workspaces(id) on delete cascade,
  state jsonb not null default '{"entries": [], "next_id": 1}'::jsonb,
  updated_at timestamptz not null default now()
);

create trigger set_active_guidance_states_updated_at
before update on public.active_guidance_states
for each row execute function public.set_updated_at();

alter table public.active_guidance_states enable row level security;

create policy "workspace members can use active guidance"
on public.active_guidance_states for all
using (public.is_workspace_member(workspace_id))
with check (public.is_workspace_member(workspace_id));

grant select, insert, update, delete
on public.active_guidance_states to service_role;

-- Local and hosted PostgREST instances can keep serving the pre-upgrade
-- schema after `supabase migration up`. Make the candidate contract available
-- without requiring an API restart.
notify pgrst, 'reload schema';
