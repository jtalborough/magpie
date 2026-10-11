from pathlib import Path


MIGRATION = Path("supabase/migrations/20260713031000_initial_magpie_schema.sql")
CONTRACT_MIGRATION = Path(
    "supabase/migrations/20260714113000_repository_contract.sql"
)
ATOMIC_PUTAWAY_MIGRATION = Path(
    "supabase/migrations/20260715120000_atomic_photo_putaway.sql"
)
DIMENSION_INFERENCE_MIGRATION = Path(
    "supabase/migrations/20260715160000_infer_item_dimensions.sql"
)
LOCATION_DIMENSIONS_MIGRATION = Path(
    "supabase/migrations/20260715170000_location_internal_dimensions.sql"
)
EDGE_MIGRATION = Path("supabase/migrations/20260719180000_edge_command_queue.sql")
EDGE_PRINT_MIGRATION = Path(
    "supabase/migrations/20260801170000_edge_print_commands.sql"
)
LOCATION_ARCHIVE_GROUP_MIGRATION = Path(
    "supabase/migrations/20260801180000_location_archive_groups.sql"
)
GUIDED_LOCATION_AUDIT_MIGRATION = Path(
    "supabase/migrations/20260801210000_guided_location_audits.sql"
)
LOCATION_CONTENT_REVISION_MIGRATION = Path(
    "supabase/migrations/20260801220000_location_content_revisions.sql"
)
ITEM_MERGE_MIGRATION = Path("supabase/migrations/20260728143000_item_merge.sql")
LOCATION_DESTINATION_MIGRATION = Path(
    "supabase/migrations/20260728173000_location_item_destinations.sql"
)
LOCATION_NAME_INTEGRITY_MIGRATION = Path(
    "supabase/migrations/20260728230000_location_name_integrity.sql"
)
LOCATION_NAME_UPSERT_REPAIR_MIGRATION = Path(
    "supabase/migrations/20260729113000_repair_location_name_upsert_guard.sql"
)
INVENTORY_CATALOG_MIGRATION = Path(
    "supabase/migrations/20260729180000_inventory_catalog_page.sql"
)
INVENTORY_CATALOG_ROOT_FILTER_MIGRATION = Path(
    "supabase/migrations/20260729234500_inventory_catalog_root_filter.sql"
)
EXPANDED_LOCATION_TYPES_MIGRATION = Path(
    "supabase/migrations/20260731170000_expand_location_types.sql"
)
ATOMIC_LOCATION_LIFECYCLE_MIGRATION = Path(
    "supabase/migrations/20260801004500_atomic_location_lifecycle.sql"
)
SCHEMA_CAPABILITIES_MIGRATION = Path(
    "supabase/migrations/20260801010000_schema_capabilities.sql"
)
ATOMIC_LOCATION_CREATION_MIGRATION = Path(
    "supabase/migrations/20260801020000_atomic_location_creation.sql"
)
ATOMIC_HARDWARE_BINDINGS_MIGRATION = Path(
    "supabase/migrations/20260801030000_atomic_hardware_bindings.sql"
)
ATOMIC_INVENTORY_IMPORT_MIGRATION = Path(
    "supabase/migrations/20260801040000_atomic_inventory_import.sql"
)
ACTIVE_GUIDANCE_MIGRATION = Path(
    "supabase/migrations/20260801050000_active_guidance_state.sql"
)
REPOSITORY_EVENT_TYPE_INDEX_MIGRATION = Path(
    "supabase/migrations/20260801120000_repository_event_type_index.sql"
)
ASSISTANT_CLIENT_REQUEST_INDEX_MIGRATION = Path(
    "supabase/migrations/20260801160000_assistant_client_request_index.sql"
)
EDGE_CREDENTIAL_MIGRATION = Path(
    "supabase/migrations/20260719193000_edge_device_credentials.sql"
)
EDGE_SCOPE_MIGRATION = Path(
    "supabase/migrations/20260719223000_edge_device_scopes.sql"
)
EDGE_PAIRING_MIGRATION = Path(
    "supabase/migrations/20260720221000_edge_device_pairing.sql"
)
VOICE_RUNTIME_MIGRATION = Path(
    "supabase/migrations/20260720190000_voice_runtime_telemetry.sql"
)
VOICE_RUNTIME_REPAIR_MIGRATION = Path(
    "supabase/migrations/20260729111500_repair_voice_runtime_telemetry.sql"
)
AGENT_CREDENTIAL_MIGRATION = Path(
    "supabase/migrations/20260721234500_agent_credentials.sql"
)
EDGE_SCOPE_REPAIR_MIGRATION = Path(
    "supabase/migrations/20260722005500_repair_edge_device_scopes.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _contract_sql() -> str:
    return CONTRACT_MIGRATION.read_text(encoding="utf-8").lower()


def _atomic_putaway_sql() -> str:
    return ATOMIC_PUTAWAY_MIGRATION.read_text(encoding="utf-8").lower()


def _dimension_inference_sql() -> str:
    return DIMENSION_INFERENCE_MIGRATION.read_text(encoding="utf-8").lower()


def _location_dimensions_sql() -> str:
    return LOCATION_DIMENSIONS_MIGRATION.read_text(encoding="utf-8").lower()


def test_location_type_constraint_matches_common_physical_storage() -> None:
    sql = EXPANDED_LOCATION_TYPES_MIGRATION.read_text(encoding="utf-8").lower()

    assert "drop constraint if exists locations_type_check" in sql
    for location_type in ("closet", "tray", "bag", "organizer"):
        assert f"'{location_type}'" in sql


def test_location_destination_migration_backfills_structural_places() -> None:
    sql = LOCATION_DESTINATION_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists accepts_items" in sql
    assert "set accepts_items = false" in sql
    for location_type in (
        "home",
        "room",
        "area",
        "closet",
        "cabinet",
        "shelf-unit",
        "rack",
        "storage_unit",
    ):
        assert f"'{location_type}'" in sql


def test_active_guidance_is_workspace_scoped_and_durable() -> None:
    sql = ACTIVE_GUIDANCE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table public.active_guidance_states" in sql
    assert "workspace_id uuid primary key references public.workspaces(id)" in sql
    assert "state jsonb not null" in sql
    assert "enable row level security" in sql
    assert "public.is_workspace_member(workspace_id)" in sql
    assert "to service_role" in sql
    assert "notify pgrst, 'reload schema'" in sql


def test_repository_management_evidence_has_a_targeted_workspace_index() -> None:
    sql = REPOSITORY_EVENT_TYPE_INDEX_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create index if not exists repository_events_workspace_type_idx" in sql
    assert "repository_events(workspace_id, event_type, local_id desc)" in sql


def test_assistant_request_replay_has_a_targeted_workspace_index() -> None:
    sql = ASSISTANT_CLIENT_REQUEST_INDEX_MIGRATION.read_text(
        encoding="utf-8"
    ).lower()

    assert (
        "create index if not exists "
        "assistant_interactions_workspace_client_request_idx"
    ) in sql
    assert "workspace_id" in sql
    assert "response ->> 'client_request_id'" in sql
    assert "route" in sql
    assert "local_id desc" in sql
    assert "where response ? 'client_request_id'" in sql


def test_edge_schema_has_durable_atomic_device_queue() -> None:
    sql = EDGE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table if not exists public.edge_devices" in sql
    assert "create table if not exists public.edge_commands" in sql
    assert "alter table public.edge_commands enable row level security" in sql
    assert "create or replace function public.magpie_enqueue_edge_command" in sql
    assert "create or replace function public.magpie_claim_edge_command" in sql
    assert "for update skip locked" in sql
    assert "create or replace function public.magpie_complete_edge_command" in sql
    assert "command_retryable boolean default false" in sql


def test_edge_print_commands_are_durable_fifo_jobs() -> None:
    sql = EDGE_PRINT_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_enqueue_edge_command" in sql
    assert "command_kind not in ('set_pixels', 'print_zpl')" in sql
    assert "if command_kind = 'set_pixels'" in sql
    assert "superseded by newer state" in sql
    assert "insert into public.edge_commands" in sql


def test_edge_credentials_store_only_token_digests() -> None:
    sql = EDGE_CREDENTIAL_MIGRATION.read_text(encoding="utf-8").lower()

    assert "credential_hash text not null default ''" in sql
    assert "credential_hint text not null default ''" in sql
    assert "credential_created_at timestamptz" in sql
    assert "revoked_at timestamptz" in sql
    assert "revoke all privileges on table public.edge_devices" in sql


def test_edge_credentials_have_server_assigned_least_privilege_scopes() -> None:
    sql = EDGE_SCOPE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "credential_scopes text[] not null" in sql
    assert "cardinality(credential_scopes) > 0" in sql
    for scope in ("commands", "voice", "screen", "printer", "check_station"):
        assert f"'{scope}'" in sql
    assert "never inferred from heartbeat capabilities" in sql


def test_edge_pairing_is_expiring_atomic_and_service_role_only() -> None:
    sql = EDGE_PAIRING_MIGRATION.read_text(encoding="utf-8").lower()

    assert "pairing_hash text not null default ''" in sql
    assert "pairing_scopes text[] not null default '{}'" in sql
    assert "pairing_expires_at timestamptz" in sql
    assert "pairing_hash <> ''" in sql
    assert "cardinality(pairing_scopes) > 0" in sql
    assert "create or replace function public.magpie_consume_edge_pairing" in sql
    assert "and pairing_expires_at > now()" in sql
    assert "pairing_hash = ''" in sql
    assert "credential_hash = new_credential_hash" in sql
    assert "revoke all on function public.magpie_consume_edge_pairing" in sql
    assert "from public, anon, authenticated" in sql
    assert "to service_role" in sql


def test_edge_scope_repair_is_idempotent_and_restores_pairing_contract() -> None:
    sql = EDGE_SCOPE_REPAIR_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists credential_scopes" in sql
    assert "edge_devices_credential_scopes_valid" in sql
    assert "create or replace function public.magpie_consume_edge_pairing" in sql
    assert "credential_scopes = pairing_scopes" in sql
    assert "to service_role" in sql
    assert "notify pgrst, 'reload schema'" in sql


def test_agent_credentials_are_workspace_scoped_hashed_and_service_role_only() -> None:
    sql = AGENT_CREDENTIAL_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table public.agent_credentials" in sql
    assert "workspace_id uuid not null references public.workspaces(id) on delete cascade" in sql
    assert "token_hash text not null unique" in sql
    assert "char_length(token_hash) = 64" in sql
    assert "alter table public.agent_credentials enable row level security" in sql
    assert "revoke all on table public.agent_credentials from anon, authenticated" in sql
    assert "to service_role" in sql
    assert "plaintext is returned once and never stored" in sql


def test_supabase_initial_schema_covers_poc_contract_objects() -> None:
    sql = _sql()

    for table in [
        "workspaces",
        "workspace_members",
        "locations",
        "items",
        "item_aliases",
        "inventory_entries",
        "photos",
        "labels",
        "hardware_bindings",
        "placement_rules",
        "size_rules",
        "agent_examples",
    ]:
        assert f"create table public.{table}" in sql

    for table in [
        "locations",
        "items",
        "inventory_entries",
        "photos",
        "labels",
        "hardware_bindings",
        "placement_rules",
        "size_rules",
        "agent_examples",
    ]:
        start = sql.index(f"create table public.{table}")
        end = sql.index(");", start)
        table_sql = sql[start:end]
        assert "workspace_id uuid not null references public.workspaces(id)" in table_sql


def test_supabase_schema_keeps_hardware_and_labels_as_addons() -> None:
    sql = _sql()

    assert "create table public.hardware_bindings" in sql
    assert "kind text not null check (kind in ('wled_segment', 'wled_pixels', 'eink_tag', 'nfc_tag'))" in sql
    assert "location_id uuid not null references public.locations(id)" in sql
    assert "create table public.labels" in sql
    assert "object_type text not null check (object_type in ('location', 'item'))" in sql
    assert "unique (workspace_id, code)" in sql


def test_location_destination_semantics_are_persisted() -> None:
    sql = LOCATION_DESTINATION_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists accepts_items boolean not null default true" in sql
    assert "whether inventory may be stored directly at this node" in sql


def test_location_names_are_guarded_without_breaking_legacy_rows() -> None:
    sql = LOCATION_NAME_INTEGRITY_MIGRATION.read_text(encoding="utf-8").lower()

    assert "magpie_guard_active_sibling_location_name" in sql
    assert "pg_advisory_xact_lock(hashtextextended(lock_scope, 0))" in sql
    assert "sibling.workspace_id = new.workspace_id" in sql
    assert "sibling.parent_id is not distinct from new.parent_id" in sql
    assert "sibling.active" in sql
    assert "sibling.id <> new.id" in sql
    assert "regexp_replace(btrim(sibling.label), '[[:space:]]+', ' ', 'g')" in sql
    assert "errcode = '23505'" in sql
    assert "new.active is not distinct from old.active" in sql
    assert "normalized_name = old_normalized_name" in sql
    assert (
        "before insert or update of workspace_id, parent_id, label, active" in sql
    )


def test_location_subtree_lifecycle_is_atomic_and_conflict_checked() -> None:
    sql = ATOMIC_LOCATION_LIFECYCLE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_set_location_subtree_active" in sql
    assert "with recursive subtree as" in sql
    assert "array_agg(subtree.id order by subtree.depth, subtree.sort_path)" in sql
    assert "restoring.id = any(target_ids)" in sql
    assert "sibling.id <> all(target_ids)" in sql
    assert "restore the parent location first" in sql
    assert "update public.locations location" in sql
    assert "return jsonb_build_object('location_ids', to_jsonb(target_codes))" in sql


def test_location_restore_preserves_independent_archive_groups() -> None:
    sql = LOCATION_ARCHIVE_GROUP_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists archive_group text" in sql
    assert "set archive_group = local_code" in sql
    assert "not child.active" in sql
    assert "child.archive_group = coalesce(" in sql
    assert "else root_location.local_code" in sql
    assert "'archive_group_provenance', true" in sql


def test_supabase_schema_advertises_behavioral_capabilities() -> None:
    sql = SCHEMA_CAPABILITIES_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_schema_capabilities()" in sql
    assert "magpie.supabase_capabilities.v1" in sql
    assert "'atomic_location_lifecycle', true" in sql
    assert "stable" in sql
    assert "security invoker" in sql


def test_location_structure_creation_is_atomic_and_parent_ordered() -> None:
    sql = ATOMIC_LOCATION_CREATION_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_create_locations" in sql
    assert "for location_row in" in sql
    assert "jsonb_array_elements(location_rows)" in sql
    assert "location.local_code = parent_code" in sql
    assert "insert into public.locations" in sql
    assert "array_append(created_codes, created_code)" in sql
    assert "'atomic_location_creation', true" in sql


def test_hardware_binding_commissioning_is_atomic() -> None:
    sql = ATOMIC_HARDWARE_BINDINGS_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_set_hardware_bindings" in sql
    assert "jsonb_array_elements(binding_rows)" in sql
    assert "update public.hardware_bindings binding" in sql
    assert "insert into public.hardware_bindings" in sql
    assert "on conflict (workspace_id, location_id, kind, controller_id, address)" in sql
    assert "update public.locations location" in sql
    assert "return jsonb_build_object('bindings', saved_rows)" in sql
    assert "'atomic_hardware_bindings', true" in sql


def test_inventory_path_creation_and_import_are_one_transaction() -> None:
    sql = ATOMIC_INVENTORY_IMPORT_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_import_inventory" in sql
    assert "perform public.magpie_create_locations" in sql
    assert "insert into public.inventory_entries" in sql
    assert "source = 'import'" in sql
    assert "'atomic_inventory_import', true" in sql


def test_supabase_schema_supports_learning_and_review_loop() -> None:
    sql = _sql()

    assert "create table public.placement_rules" in sql
    assert "trigger_terms text[] not null default '{}'" in sql
    assert "target_location_id uuid not null references public.locations(id)" in sql
    assert "create table public.size_rules" in sql
    assert "size_class text not null check" in sql
    assert "create table public.agent_examples" in sql
    assert "proposed_plan jsonb not null default '[]'::jsonb" in sql
    assert "confirmed_plan jsonb not null default '[]'::jsonb" in sql
    assert "review_status text not null default 'pending'" in sql


def test_supabase_schema_enables_workspace_rls() -> None:
    sql = _sql()

    assert "create or replace function public.is_workspace_member" in sql
    assert "create trigger add_workspace_owner_member" in sql
    for table in [
        "workspaces",
        "workspace_members",
        "locations",
        "items",
        "item_aliases",
        "inventory_entries",
        "photos",
        "labels",
        "hardware_bindings",
        "placement_rules",
        "size_rules",
        "agent_examples",
    ]:
        assert f"alter table public.{table} enable row level security;" in sql


def test_repository_contract_schema_preserves_existing_client_ids() -> None:
    sql = _contract_sql()

    for table in [
        "items",
        "photos",
        "hardware_bindings",
        "placement_rules",
        "size_rules",
        "agent_examples",
    ]:
        start = sql.index(f"alter table public.{table}")
        end = sql.index(";", start)
        assert "local_id bigint generated by default as identity" in sql[start:end]
        assert f"{table}_workspace_local_id_idx" in sql

    for table in ["repository_events", "assistant_interactions", "voice_attempts"]:
        start = sql.index(f"create table public.{table}")
        end = sql.index(");", start)
        assert "workspace_id uuid not null references public.workspaces(id)" in sql[start:end]
        assert "local_id bigint generated by default as identity" in sql[start:end]
        assert "unique (workspace_id, local_id)" in sql[start:end]


def test_repository_contract_schema_covers_agent_and_voice_history() -> None:
    sql = _contract_sql()

    assert "create table public.assistant_interactions" in sql
    assert "photo_local_ids bigint[] not null default '{}'" in sql
    assert "response jsonb not null default '{}'::jsonb" in sql
    assert "create table public.voice_attempts" in sql
    assert "activity_rms real" in sql
    assert "backend_response jsonb not null default '{}'::jsonb" in sql
    assert "create table public.repository_events" in sql
    assert "create table public.inventory_review_photos" in sql


def test_voice_runtime_migration_attributes_attempts_to_edge_builds() -> None:
    sql = VOICE_RUNTIME_MIGRATION.read_text(encoding="utf-8").lower()

    assert "alter table public.voice_attempts" in sql
    assert "device_id text not null default ''" in sql
    assert "runtime_version text not null default ''" in sql


def test_voice_runtime_repair_is_safe_for_databases_with_partial_history() -> None:
    sql = VOICE_RUNTIME_REPAIR_MIGRATION.read_text(encoding="utf-8").lower()

    assert "alter table public.voice_attempts" in sql
    assert sql.count("add column if not exists") == 2
    assert "device_id text not null default ''" in sql
    assert "runtime_version text not null default ''" in sql


def test_location_name_guard_allows_updating_the_same_logical_location() -> None:
    sql = LOCATION_NAME_UPSERT_REPAIR_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_guard_active_sibling_location_name()" in sql
    assert "sibling.id <> new.id" in sql
    assert "sibling.local_code <> new.local_code" in sql
    assert "before insert or update of workspace_id, parent_id, label, active" in sql


def test_inventory_catalog_pages_complete_item_groups_with_invoker_rls() -> None:
    sql = INVENTORY_CATALOG_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.magpie_inventory_catalog_page" in sql
    assert "security invoker" in sql
    assert "with recursive location_tree" in sql
    assert "row_number() over" in sql
    assert "where page_order > clean_offset" in sql
    assert "join public.inventory_entries entry" in sql
    assert "'metrics', jsonb_build_object" in sql
    assert "'categories'" in sql
    assert "to authenticated, service_role" in sql
    assert "security definer" not in sql


def test_inventory_catalog_root_filter_is_scoped_and_backward_compatible() -> None:
    sql = INVENTORY_CATALOG_ROOT_FILTER_MIGRATION.read_text(
        encoding="utf-8"
    ).lower()

    assert "create or replace function public.magpie_inventory_catalog_page_v2" in sql
    assert "location_filters text[]" in sql
    assert "location.local_code = any(clean_locations)" in sql
    assert "metric_location.local_code = any(clean_locations)" in sql
    assert "where page_order > clean_offset" in sql
    assert "join public.inventory_entries entry" in sql
    assert "security invoker" in sql
    assert "to authenticated, service_role" in sql
    assert "security definer" not in sql


def test_repository_contract_schema_supports_unattached_photos_and_location_geometry() -> None:
    sql = _contract_sql()

    assert "alter column object_type drop not null" in sql
    assert "alter column object_id drop not null" in sql
    assert "add column item_id uuid references public.items(id)" in sql
    assert "add column location_id uuid references public.locations(id)" in sql
    for column in [
        "grid jsonb",
        "cell_col integer",
        "cell_row integer",
        "cell_w integer",
        "cell_h integer",
        "light_target text",
        "light_indexes integer[]",
    ]:
        assert f"add column {column}" in sql
    assert "'shelf-unit'" in sql


def test_repository_contract_new_tables_enforce_workspace_rls() -> None:
    sql = _contract_sql()

    for table in [
        "inventory_review_photos",
        "repository_events",
        "assistant_interactions",
        "voice_attempts",
    ]:
        assert f"alter table public.{table} enable row level security;" in sql
        assert f"on public.{table} for all" in sql
        assert "public.is_workspace_member(workspace_id)" in sql


def test_repository_contract_keeps_photo_bucket_private() -> None:
    sql = _contract_sql()

    assert "values ('magpie-photos', 'magpie-photos', false)" in sql
    assert "public can read magpie photos" not in sql


def test_repository_contract_can_advance_imported_identity_sequences() -> None:
    sql = _contract_sql()

    assert "function public.advance_magpie_identity_sequences()" in sql
    for table in [
        "items",
        "photos",
        "hardware_bindings",
        "placement_rules",
        "size_rules",
        "agent_examples",
        "repository_events",
        "assistant_interactions",
        "voice_attempts",
    ]:
        assert f"'{table}'" in sql
    assert "grant execute on function public.advance_magpie_identity_sequences() to service_role" in sql
    assert "revoke all on function public.advance_magpie_identity_sequences() from authenticated" in sql


def test_repository_contract_exposes_service_role_migration_count_audit() -> None:
    sql = _contract_sql()

    assert "function public.magpie_workspace_counts(target_workspace_id uuid)" in sql
    assert "'voice_attempts', (select count(*)" in sql
    assert "join public.items i on i.id = a.item_id" in sql
    assert "grant execute on function public.magpie_workspace_counts(uuid) to service_role" in sql
    assert "revoke all on function public.magpie_workspace_counts(uuid) from authenticated" in sql


def test_atomic_photo_putaway_is_transactional_and_service_role_only() -> None:
    sql = _atomic_putaway_sql()

    assert "function public.magpie_put_item_with_photos(" in sql
    assert "for update;" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "one or more photos were not found" in sql
    assert "update public.photos" in sql
    assert "insert into public.inventory_review_photos" in sql
    assert "insert into public.repository_events" in sql
    assert "to service_role;" in sql
    assert ") from authenticated;" in sql


def test_metric_dimensions_are_inferred_without_overwriting_measurements() -> None:
    sql = _dimension_inference_sql()

    assert "function public.magpie_infer_item_dimensions(item_name text)" in sql
    assert "'thread_diameter_mm'" in sql
    assert "'length_mm'" in sql
    assert "new.dimensions is not distinct from old.dimensions" in sql
    assert "old.dimensions = public.magpie_infer_item_dimensions(old.name)" in sql
    assert "before insert or update of name on public.items" in sql
    assert "where dimensions = '{}'::jsonb" in sql
    assert "from public, anon, authenticated" in sql


def test_location_internal_dimensions_are_structured_json() -> None:
    sql = _location_dimensions_sql()

    assert "add column if not exists internal_dimensions jsonb" in sql
    assert "jsonb_typeof(internal_dimensions) = 'object'" in sql


def test_item_merge_is_atomic_and_preserves_source_identity() -> None:
    sql = ITEM_MERGE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists merged_into_item_id uuid" in sql
    assert "function public.magpie_merge_items(" in sql
    assert "for update;" in sql
    assert "items use different units in the same location" in sql
    assert "insert into public.inventory_review_photos" in sql
    assert "update public.photos" in sql
    assert "update public.repository_events" in sql
    assert "merged_into_item_id = canonical_item.id" in sql
    assert "to authenticated, service_role;" in sql


def test_guided_location_audit_is_atomic_and_rejects_stale_inventory() -> None:
    sql = GUIDED_LOCATION_AUDIT_MIGRATION.read_text(encoding="utf-8").lower()

    assert "function public.magpie_apply_location_audit(" in sql
    assert "jsonb_array_length(audit_rows) > 100" in sql
    assert "with recursive subtree as" in sql
    assert "from public.locations location" in sql
    assert "for update of entry, item" in sql
    assert sql.index("for update of entry, item") < sql.index(
        "order by location.id\n  for update"
    )
    assert "inventory changed while this contents check was open" in sql
    assert "update public.inventory_entries" in sql
    assert "'location_contents_verified'" in sql
    assert "where location.id = any(subtree_ids)" in sql
    assert "where row->>'location_id' = location.local_code" in sql
    assert "insert into public.repository_events" in sql
    assert "'guided_location_audits', true" in sql
    assert "security invoker" in sql


def test_location_content_revisions_track_physical_changes_with_bounded_metrics() -> None:
    sql = LOCATION_CONTENT_REVISION_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists content_revision bigint" in sql
    assert "function public.magpie_bump_location_content_revisions(" in sql
    assert "inventory_content_revision_insert" in sql
    assert "inventory_content_revision_delete" in sql
    assert "after update of item_id, location_id, quantity, unit" in sql
    assert "item_active_content_revision_update" in sql
    assert "location_structure_revision_update" in sql
    assert "function public.magpie_location_inventory_metrics(" in sql
    assert "count(item.id) filter (where entry.needs_review)" in sql
    assert "'location_content_revisions', true" in sql
    assert "security invoker" in sql
