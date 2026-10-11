from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import binascii
from collections import Counter
from contextlib import asynccontextmanager
import csv
from difflib import SequenceMatcher
import hashlib
import html
import hmac
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Annotated, Callable, Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field
from importlib import resources

from .assistant import (
    alias_candidates,
    assistant_decision,
    infer_category,
    normalized_query,
    search_inventory,
    title_item_name,
)
from .agent_credentials import (
    AgentCredentialService,
    create_agent_credential_service,
)
from .build import build_info
from .config import LedConfig, LlmEndpoint
from .controller import LedController, PixelUpdate, WorkspaceLedController
from .edge import (
    EDGE_CREDENTIAL_SCOPES,
    EdgeCommand,
    EdgeRepository,
    QueuedEdgeWriter,
    WorkspaceQueuedEdgeWriter,
    create_edge_repository,
    create_workspace_edge_repository,
    normalize_credential_scopes,
)
from .auth import AuthError, AuthIdentity, AuthService, create_auth_service
from .agent import execute_plan, normalize_plan, run_agent, slugify
from .layout import StorageLayout, normalize_bin_id
from .llm_client import (
    LLM_USAGE_FIELDS,
    LlmClient,
    LlmError,
    _is_local,
    add_llm_usage,
)
from .locations import (
    LOCATION_TYPES,
    Location,
    LocationNameConflictError,
    backfill_from_layout,
    default_accepts_items,
    generate_cells,
    location_path,
    match_locations,
    normalize_binding_kind,
    normalize_location_type,
    pixels_from_binding,
    normalize_internal_dimensions,
)
from .measurement import measure_calibrated_item, render_calibration_grid_pdf
from .magpie_tools import MagpieToolService
from .mcp_server import create_mcp_server
from .openai_assistant import (
    OpenAIAssistantError,
    enrich_item_draft,
    llm_assistant_decision,
    photo_data_url,
)
from .image_search import search_item_image
from .interaction_contract import VOICE_SCOPE_MESSAGE, interaction_contract
from .item_identity import identities_conflict, inferred_dimensions, resolve_item_identity
from .labels import (
    InventoryReportRow,
    ItemLabelPayload,
    LabelPayload,
    query_zebra_status,
    render_item_label_pdf,
    render_item_label_sheet_pdf,
    render_inventory_report_pdf,
    render_label_sheet_pdf,
    render_location_zpl,
    render_qr_svg,
    send_zpl,
)
from .placement import (
    CAPACITY_RANK,
    append_placement_memory,
    infer_item_capacity,
    item_capacity_for,
    measured_capacity_for,
    physical_fit,
    requested_capacity,
    suggest_locations,
)
from .repositories import (
    InventoryRepository,
    StorageRepositories,
    WorkspaceRepositoryRouter,
)
from .responses_client import OpenAIResponsesClient
from .store import (
    InventoryAuditConflictError,
    InventoryCatalogPage,
    SearchResult,
    _placement_rule_conflicts,
    _size_rule_conflicts,
)
from .words import pick_unused
from .workspace_admin import (
    WorkspaceCatalogService,
    WorkspaceMemberService,
    WorkspaceMemberServiceRouter,
    can_manage_members,
    create_workspace_catalog_service,
    create_workspace_member_service,
    create_workspace_member_service_router,
)


EDGE_CONNECTED_SECONDS = 180
EDGE_SERVICE_REQUIREMENTS = {
    "lights": ("commands", "set_pixels"),
    "voice": ("voice", "voice_assistant"),
    "screen": ("screen", "kiosk_display"),
    "printer": ("printer", "label_printer"),
    "check_station": ("check_station", "check_station"),
}
PENDING_CONFIRMATION_TTL_SECONDS = 5 * 60
KIOSK_IDLE_CACHE_SECONDS = 30.0
WORKSPACE_MANAGEMENT_EVENTS = {
    "visual_multi_root_setup": "workspace_visual_multi_root_setup",
    "cross_root_item_move": "workspace_cross_root_item_move",
    "location_archive_restore": "workspace_location_archive_restore",
}
AGENT_STORAGE_EVENTS = {
    "root_discovery": "workspace_agent_root_discovery",
    "cross_root_retrieval": "workspace_agent_cross_root_retrieval",
    "label_preview": "workspace_agent_label_preview",
}
AGENT_CORRECTION_EVENTS = {
    "explained_proposal": "workspace_agent_explained_proposal",
    "correction_applied": "workspace_agent_correction_applied",
    "correction_retained": "workspace_agent_correction_retained",
}
VOICE_AGENT_TOOLS = frozenset(
    {
        "attach_photo",
        "clear_lights",
        "explain_location_choice",
        "get_location",
        "light_location",
        "list_locations",
        "move_item",
        "put_item",
        "search_items",
        "suggest_locations",
    }
)
APP_SHELL_PATHS = (
    "/",
    "/mobile",
    "/cabinet",
    "/check-station",
    "/storage",
    "/review",
    "/account",
    "/theme.css",
    "/product-shell.js",
    "/auth.js",
    "/location-display.js",
    "/confirmation.js",
    "/photo-utils.js",
    "/jsqr.min.js",
    "/manifest.webmanifest",
    "/app-icon.svg",
    "/app-icon-192.png",
    "/app-icon-512.png",
    "/apple-touch-icon.png",
)
NETWORK_FIRST_SHELL_PATHS = (
    "/theme.css",
    "/product-shell.js",
    "/auth.js",
    "/location-display.js",
    "/confirmation.js",
    "/photo-utils.js",
)
PUBLIC_GET_PATHS = frozenset(
    (
        *APP_SHELL_PATHS,
        "/browse",
        "/favicon.ico",
        "/service-worker.js",
        "/auth/config",
        "/health",
        "/ready",
        "/build",
    )
)


def _edge_device_age_seconds(device: dict[str, object]) -> float | None:
    last_seen = device.get("last_seen_at")
    if isinstance(last_seen, (int, float)):
        if float(last_seen) <= 0:
            return None
        return max(0.0, time.time() - float(last_seen))
    if isinstance(last_seen, str) and last_seen.strip():
        try:
            observed = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - observed).total_seconds(),
        )
    return None


def _edge_device_connection(device: dict[str, object]) -> dict[str, object]:
    enrolled = bool(device.get("enrolled"))
    pairing_pending = bool(device.get("pairing_pending"))
    age = _edge_device_age_seconds(device)
    connected = bool(enrolled and age is not None and age <= EDGE_CONNECTED_SECONDS)
    scopes = device.get("credential_scopes")
    capabilities = device.get("capabilities")
    scope_set = {str(scope) for scope in scopes} if isinstance(scopes, list) else set()
    capability_set = (
        {str(capability) for capability in capabilities}
        if isinstance(capabilities, list)
        else set()
    )
    service_readiness = {
        service: {
            "scope": scope,
            "capability": capability,
            "granted": scope in scope_set,
            "reported": capability in capability_set,
            "ready": bool(
                connected
                and scope in scope_set
                and capability in capability_set
            ),
        }
        for service, (scope, capability) in EDGE_SERVICE_REQUIREMENTS.items()
    }
    command_ready = bool(
        service_readiness["lights"]["ready"]
    )
    if not enrolled:
        connection_state = "pairing" if pairing_pending else "unpaired"
        connection_message = (
            "Run the setup command on this device."
            if pairing_pending
            else "Pair this device to connect it."
        )
    elif age is None:
        connection_state = "waiting"
        connection_message = "Paired; waiting for the first heartbeat."
    elif not connected:
        connection_state = "offline"
        connection_message = "Offline; check the device service and network."
    elif isinstance(scopes, list) and "commands" in scopes and not command_ready:
        connection_state = "connected"
        connection_message = "Connected; waiting for light-controller capability."
    else:
        connection_state = "connected"
        connection_message = "Connected."
    return {
        "connected": connected,
        "command_ready": command_ready,
        "connection_state": connection_state,
        "connection_message": connection_message,
        "last_seen_age_seconds": round(age, 1) if age is not None else None,
        "service_readiness": service_readiness,
    }


DEFAULT_LIGHT_SECONDS = 180.0
SAVE_ACK_SECONDS = 2.0
MAX_BACKUP_PHOTO_BYTES = 18 * 1024 * 1024
# Cap how many bins a single "find" lights at once, so a broad query does not
# blanket the whole cabinet.
LIGHT_BIN_LIMIT = 12
ACTIVE_FIND_COLORS = [
    (255, 0, 0),
    (0, 70, 255),
    (255, 150, 0),
    (0, 180, 70),
    (180, 0, 255),
    (0, 210, 255),
    (255, 40, 180),
]
CONFIRMATION_COLOR_NAMES = (
    "Red",
    "Blue",
    "Amber",
    "Green",
    "Purple",
    "Cyan",
    "Pink",
)


def _llm_ready_endpoint(cfg: LedConfig) -> LlmEndpoint | None:
    """Whether the configured LLM can be reached: an API key is set, or the
    endpoint is local (Ollama/llama.cpp) and needs none."""
    endpoints = [
        LlmEndpoint(
            cfg.llm.base_url,
            cfg.llm.model,
            cfg.llm.api_key_env,
            cfg.llm.provider,
        )
    ]
    if cfg.llm.fallback is not None:
        endpoints.append(cfg.llm.fallback)
    endpoints.extend(cfg.llm.fallbacks)
    for endpoint in endpoints:
        base_url = Template(endpoint.base_url).safe_substitute(os.environ)
        model = Template(endpoint.model).safe_substitute(os.environ)
        if endpoint.api_key_env and os.environ.get(endpoint.api_key_env):
            return LlmEndpoint(
                base_url,
                model,
                endpoint.api_key_env,
                endpoint.provider,
            )
        if not endpoint.api_key_env and _is_local(base_url):
            return LlmEndpoint(base_url, model, "", endpoint.provider)
    return None


def _llm_key_present(cfg: LedConfig) -> bool:
    return _llm_ready_endpoint(cfg) is not None


def _operational_state_probe(state_dir: Path) -> dict[str, object]:
    descriptor: int | None = None
    probe_path: Path | None = None
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".magpie-ready-",
            dir=state_dir,
        )
        probe_path = Path(raw_path)
        os.write(descriptor, b"ready\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        probe_path.unlink()
        probe_path = None
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True}


def _runtime_profile(
    cfg: LedConfig,
    *,
    storage: dict[str, object],
    edge_enabled: bool,
) -> dict[str, object]:
    hardware_owner = cfg.backend != "none"
    if cfg.auth.multi_workspace and cfg.edge.mode == "workspace":
        role = "central_service"
    elif cfg.edge.mode == "central":
        role = "single_workspace_central"
    elif hardware_owner:
        role = "cabinet_service"
    else:
        role = "standalone_service"
    return {
        "schema": "magpie.runtime.v1",
        "role": role,
        "hardware_owner": hardware_owner,
        "storage_authority": str(storage.get("backend") or ""),
        "workspace_scope": "request" if cfg.auth.multi_workspace else "fixed",
        "edge_execution": (
            "workspace_queue"
            if cfg.edge.mode == "workspace"
            else "fixed_queue"
            if cfg.edge.mode == "central"
            else "local"
        ),
        "edge_queue_enabled": edge_enabled,
    }


class ColorRequest(BaseModel):
    color: tuple[int, int, int] = Field(..., description="RGB color, 0-255 per channel")
    brightness: float | None = Field(None, ge=0, le=1)


class PixelRequest(ColorRequest):
    clear_others: bool = False


class PixelBatchRequest(BaseModel):
    pixels: list[tuple[int, tuple[int, int, int]]]
    clear_others: bool = False
    brightness: float | None = Field(None, ge=0, le=1)


class EdgeHeartbeatRequest(BaseModel):
    version: str = ""
    capabilities: list[str] = Field(default_factory=list)


class EdgeCompletionRequest(BaseModel):
    succeeded: bool
    error: str = Field("", max_length=1000)
    retryable: bool = False


class EdgePrintZplRequest(BaseModel):
    zpl: str = Field(..., min_length=8, max_length=256 * 1024)


class EdgeDeviceEnrollRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=63)
    scopes: list[str] = Field(default_factory=lambda: ["commands"])


class EdgeDevicePairRequest(BaseModel):
    pairing_code: str = Field(..., min_length=16, max_length=128)


class FlashRequest(ColorRequest):
    seconds: Annotated[float, Field(gt=0, le=30)] = 1.0


class BinFlashRequest(BaseModel):
    color: tuple[int, int, int] = (255, 255, 255)
    brightness: float | None = Field(1.0, ge=0, le=1)
    seconds: Annotated[float, Field(gt=0, le=300)] = DEFAULT_LIGHT_SECONDS


class LocationLightRequest(BinFlashRequest):
    pass


class LocationLightEntry(BaseModel):
    location_id: str
    color: tuple[int, int, int]


class LocationLightBatchRequest(BaseModel):
    locations: list[LocationLightEntry]
    brightness: float | None = Field(1.0, ge=0, le=1)
    seconds: Annotated[float, Field(gt=0, le=300)] = DEFAULT_LIGHT_SECONDS


class InventoryPutRequest(BaseModel):
    name: str
    bin_id: str
    quantity: float = 1
    unit: str = "each"
    description: str = ""
    category: str = ""
    aliases: list[str] = []
    photo_ids: list[int] = []
    size_class: str = ""
    size_source: str = "manual"
    size_confidence: float = Field(1.0, ge=0, le=1)
    dimensions: dict[str, object] | None = None


class InventoryMoveRequest(BaseModel):
    item_id: int
    from_bin: str
    to_bin: str


class InventoryBulkMoveEntry(BaseModel):
    item_id: int
    from_bin: str


class InventoryBulkMoveRequest(BaseModel):
    entries: list[InventoryBulkMoveEntry]
    to_bin: str


class InventoryLifecycleRequest(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=100)
    action: str


class InventoryHomeRequest(BaseModel):
    location_id: str


class InventoryUpdateRequest(BaseModel):
    name: str
    bin_id: str
    quantity: float = 1
    unit: str = "each"
    description: str = ""
    category: str = ""
    aliases: list[str] = []
    size_class: str = ""
    size_source: str = "manual"
    size_confidence: float = Field(1.0, ge=0, le=1)
    dimensions: dict[str, object] | None = None


class InventoryMergeRequest(BaseModel):
    target_item_id: int = Field(..., gt=0)


class PhotoUploadRequest(BaseModel):
    filename: str = "photo.jpg"
    content_type: str = "image/jpeg"
    data_base64: str


class PhotoMeasurementRequest(BaseModel):
    item: str = ""
    photo_ids: list[int] = []


class AssistantCommandRequest(BaseModel):
    text: str
    photo_ids: list[int] = []


class InventoryReviewRequest(BaseModel):
    item_id: int
    bin_id: str


class UndoRequest(BaseModel):
    expected_interaction_id: int | None = Field(default=None, gt=0)


class AgentCommandRequest(BaseModel):
    text: str
    photo_ids: list[int] = []


class AssistantChatRequest(BaseModel):
    text: str
    photo_ids: list[int] = []
    current_location_id: str | None = None
    destination_locked: bool = False
    mode: str = "auto"
    source: str = "ui"
    voice_transcript: str = ""
    voice_command: str = ""
    voice_query: str = ""
    stt_model: str = ""
    client_request_id: str = ""


class AssistantRevisionRequest(BaseModel):
    text: str
    source: str = "ui"
    location_id: str = ""
    step_index: int | None = Field(default=None, ge=0)
    voice_transcript: str = ""
    voice_command: str = ""
    voice_query: str = ""
    stt_model: str = ""


class KioskRevisionRequest(BaseModel):
    prompt_id: str = Field(min_length=1, max_length=64)


class AssistantFollowupRequest(BaseModel):
    text: str
    context: dict[str, object] = Field(default_factory=dict)


class PlanStep(BaseModel):
    tool: str
    arguments: dict = {}


class AgentConfirmRequest(BaseModel):
    plan: list[PlanStep]
    command: str = ""
    photo_ids: list[int] = []
    source_interaction_id: int | None = None


class AgentCancelRequest(BaseModel):
    command: str = ""
    photo_ids: list[int] = []
    source_interaction_id: int | None = None


class PhotoAttachmentRetryRequest(BaseModel):
    photo_ids: list[int] = []
    item_ids: list[int] = []
    location_id: str | None = None


class LearningBulkApproveRequest(BaseModel):
    example_ids: list[int] = []


class AgentEvalReplayRequest(BaseModel):
    limit: int = 100
    kind: str | None = None
    run_model: bool = False
    minimum_cases: int = Field(1, ge=1, le=25)
    required_scenarios: list[
        Literal["putaway", "rules", "correction", "photo", "organization"]
    ] = Field(default_factory=list)


class FeedbackCorrectionRequest(BaseModel):
    query: str
    item_id: int


class FindNotHereRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    location_id: str = Field(..., min_length=1)
    query: str = ""
    active_find_id: int | None = Field(None, gt=0)
    source: str = "ui"


class AssistantFindRequest(BaseModel):
    query: str = ""
    photo_ids: list[int] = []
    current_location_id: str | None = None
    destination_locked: bool = False


class GenerateImageRequest(BaseModel):
    item_id: int
    name: str


class VoiceAttemptRequest(BaseModel):
    rms: float | None = None
    activity_rms: float | None = None
    transcript: str = ""
    wake_word: str = ""
    wake_heard: bool = False
    awaiting_command: bool = False
    command: str = ""
    route: str = ""
    query: str = ""
    stt_model: str = ""
    audio_threshold: float = 0.0
    device_id: str = ""
    runtime_version: str = ""
    backend_response: dict[str, object] | None = None
    error: str = ""


class VoiceStateRequest(BaseModel):
    state: str
    message: str = ""
    transcript: str = ""
    command: str = ""
    device_id: str = ""
    runtime_version: str = ""
    capabilities: dict[str, object] = Field(default_factory=dict)


class ScreenHeartbeatRequest(BaseModel):
    screen_id: str = "kiosk"
    url: str = ""
    build_label: str = ""
    last_seen_interaction_id: int = 0
    status: str = "active"


class LocationCreateRequest(BaseModel):
    id: str | None = None
    type: str
    label: str
    parent_id: str | None = None
    cols: int | None = None  # this node's own grid (a drawer's internal space)
    rows: int | None = None
    col: int | None = None  # placement within the parent's grid
    row: int | None = None
    w: int = 1
    h: int = 1
    capacity_class: str = "medium"
    purpose_tags: list[str] = []
    placement_notes: str = ""
    internal_dimensions: dict[str, object] = {}
    accepts_items: bool | None = None


class StorageSetupChildRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(default="area", min_length=1, max_length=40)


class StorageSetupDestinationRequest(StorageSetupChildRequest):
    parent_label: str | None = Field(default=None, max_length=120)
    capacity_class: str = Field(default="medium", min_length=1, max_length=40)


class StorageSetupRequest(BaseModel):
    root_name: str = Field(min_length=1, max_length=120)
    root_type: str = Field(default="home", min_length=1, max_length=40)
    children: list[StorageSetupChildRequest] = Field(
        default_factory=list,
        max_length=20,
    )
    destination: StorageSetupDestinationRequest | None = None


class GridFillRequest(BaseModel):
    cols: int | None = None
    rows: int | None = None
    cell_type: str = "bin"
    naming: str = "words"


class CellCreateRequest(BaseModel):
    col: int
    row: int
    cell_type: str = "bin"
    naming: str = "words"


class LocationUpdateRequest(BaseModel):
    label: str | None = None
    type: str | None = None
    parent_id: str | None = None
    cols: int | None = None
    rows: int | None = None
    w: int | None = None
    h: int | None = None
    light_target: str | None = None
    light_indexes: list[int] | None = None
    capacity_class: str | None = None
    purpose_tags: list[str] | None = None
    placement_notes: str | None = None
    internal_dimensions: dict[str, object] | None = None
    accepts_items: bool | None = None


class LocationAuditPlacementRequest(BaseModel):
    item_id: int = Field(..., gt=0)
    location_id: str = Field(..., min_length=1, max_length=160)
    status: Literal["present", "missing"]


class LocationVerificationRequest(BaseModel):
    revision: str = Field(..., min_length=64, max_length=64)
    placements: list[LocationAuditPlacementRequest] = Field(
        default_factory=list,
        max_length=100,
    )


class HardwareBindingRequest(BaseModel):
    kind: str = "wled_pixels"
    controller_id: str = "main"
    address: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)
    active: bool = True


class HardwareCommissionRequest(BaseModel):
    controller_id: str = Field(default="main", min_length=1, max_length=120)
    start_pixel: int = Field(default=0, ge=0, le=9999)
    direction: Literal["forward", "reverse"] = "forward"
    pixel_allocation: Literal["existing_layout", "one", "place_width"] = (
        "existing_layout"
    )
    replace_existing: bool = False


class LocationMoveRequest(BaseModel):
    new_parent_id: str | None = None


class LocationBatchCreateRequest(BaseModel):
    count: int = Field(1, ge=1, le=100)
    labels: list[str] = Field(default_factory=list, max_length=100)
    type: str = "bin"
    label_prefix: str = "Bin"
    capacity_class: str = "medium"
    purpose_tags: list[str] = []
    placement_notes: str = ""
    internal_dimensions: dict[str, object] = {}
    accepts_items: bool | None = None


class LocationStructureCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(min_length=1, max_length=40)
    parent_id: str | None = None
    capacity_class: str = Field(default="large", min_length=1, max_length=40)
    child_count: int = Field(default=4, ge=1, le=100)
    child_type: str = Field(min_length=1, max_length=40)
    child_label_prefix: str = Field(min_length=1, max_length=120)
    child_capacity_class: str = Field(
        default="medium",
        min_length=1,
        max_length=40,
    )
    accepts_items: bool | None = None
    child_accepts_items: bool | None = None


class LocationQuickPutRequest(BaseModel):
    text: str = ""
    photo_ids: list[int] = []
    quantity: float = 1
    unit: str = "each"
    client_request_id: str = ""


class RuleUpdateRequest(BaseModel):
    rule_text: str
    trigger_terms: list[str] = Field(default_factory=list)
    target_location_id: str | None = None
    size_class: str | None = None


class LocationItemRequest(BaseModel):
    name: str
    quantity: float = 1
    unit: str = "each"


class PlacementSuggestRequest(BaseModel):
    item: str
    limit: int = Field(8, ge=1, le=20)
    dimensions: dict[str, object] | None = None


class InventoryCsvImportRequest(BaseModel):
    csv_text: str
    default_location_id: str | None = None
    create_missing_locations: bool = False
    dry_run: bool = False


class InventoryJsonImportRequest(BaseModel):
    snapshot: dict[str, object]
    dry_run: bool = False


class WorkspaceInviteRequest(BaseModel):
    email: str
    role: str = "member"
    redirect_url: str = ""


class WorkspaceRoleRequest(BaseModel):
    role: str


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class WorkspaceRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class WorkspaceDeleteRequest(BaseModel):
    confirmation: str = Field(..., min_length=1, max_length=120)


class AgentCredentialCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


def create_app(
    config: LedConfig | None = None,
    *,
    repositories: StorageRepositories | None = None,
    repository_router: WorkspaceRepositoryRouter | None = None,
    edge_repository: EdgeRepository | None = None,
    auth_service: AuthService | None = None,
    workspace_member_service: WorkspaceMemberService | None = None,
    workspace_member_router: WorkspaceMemberServiceRouter | None = None,
    workspace_catalog_service: WorkspaceCatalogService | None = None,
    agent_credential_service: AgentCredentialService | None = None,
    llm: LlmClient | None = None,
) -> FastAPI:
    cfg = config or LedConfig.from_path(os.environ.get("LED_CONFIG"))
    layout = StorageLayout.from_path(cfg.layout_path)
    if repository_router is not None:
        repository_bundle = repository_router
    elif repositories is not None:
        repository_bundle = WorkspaceRepositoryRouter.fixed(repositories)
    elif cfg.auth.multi_workspace:
        repository_bundle = WorkspaceRepositoryRouter.from_config(
            cfg.storage,
            sqlite_path=cfg.database_path,
        )
    else:
        repository_bundle = WorkspaceRepositoryRouter.fixed(
            StorageRepositories.from_config(
                cfg.storage,
                sqlite_path=cfg.database_path,
            )
        )
    store = repository_bundle.inventory
    locations = repository_bundle.locations
    edge_commands = edge_repository
    edge_command_generation = 0
    edge_command_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
    edge_command_waiters_lock = threading.Lock()

    def _notify_edge_command(_command: object) -> None:
        nonlocal edge_command_generation
        edge_command_generation += 1
        with edge_command_waiters_lock:
            waiters = list(edge_command_waiters)
        for loop, waiter in waiters:
            try:
                loop.call_soon_threadsafe(waiter.set)
            except RuntimeError:
                continue

    if cfg.auth.multi_workspace and edge_commands is None:
        edge_commands = create_workspace_edge_repository(
            cfg.storage,
            workspace_id=repository_bundle.current_workspace_id,
        )
    if cfg.edge.mode == "workspace":
        if edge_commands is None:
            raise ValueError("workspace edge mode requires workspace-routed edge storage")
        controller = WorkspaceLedController(
            cfg,
            writer=WorkspaceQueuedEdgeWriter(
                edge_commands,
                workspace_id=repository_bundle.current_workspace_id,
                ttl_seconds=cfg.edge.command_ttl_seconds,
                on_enqueue=_notify_edge_command,
            ),
        )
    elif cfg.edge.mode == "central":
        edge_commands = edge_commands or create_edge_repository(
            cfg.storage,
            sqlite_path=cfg.database_path,
        )
        controller = LedController(
            cfg,
            writer=QueuedEdgeWriter(
                edge_commands,
                device_id=cfg.edge.target_device_id,
                ttl_seconds=cfg.edge.command_ttl_seconds,
                on_enqueue=_notify_edge_command,
            ),
        )
    else:
        controller = LedController(cfg)

    def _ready_edge_service_devices(service: str) -> list[dict[str, object]]:
        if edge_commands is None or service not in EDGE_SERVICE_REQUIREMENTS:
            return []
        try:
            status = edge_commands.status()
        except RuntimeError:
            return []
        ready: list[dict[str, object]] = []
        for raw_device in status.get("devices", []):
            if not isinstance(raw_device, dict):
                continue
            device = dict(raw_device)
            device.update(_edge_device_connection(device))
            readiness = device.get("service_readiness")
            service_status = (
                readiness.get(service) if isinstance(readiness, dict) else None
            )
            if isinstance(service_status, dict) and service_status.get("ready") is True:
                ready.append(device)
        return sorted(ready, key=lambda device: str(device.get("device_id") or ""))

    def _label_printer_status() -> dict[str, object]:
        if cfg.zebra.host.strip():
            return query_zebra_status(cfg.zebra)
        devices = _ready_edge_service_devices("printer")
        return {
            "ok": bool(devices),
            "edge": True,
            "configured": {
                "mode": "edge",
                "device_ids": [
                    str(device.get("device_id") or "") for device in devices
                ],
            },
            "values": {},
            "error": "" if devices else "no connected edge label printer is ready",
        }

    def _enqueue_edge_command(
        device_id: str,
        kind: str,
        payload: dict[str, object],
    ) -> EdgeCommand:
        if edge_commands is None:
            raise ValueError("edge command routing is not enabled")
        command = edge_commands.enqueue(
            device_id,
            kind,
            payload,
            ttl_seconds=cfg.edge.command_ttl_seconds,
        )
        _notify_edge_command(command)
        return command

    human_auth = auth_service
    if cfg.auth.mode == "supabase":
        human_auth = human_auth or create_auth_service(cfg.auth)
    member_service = workspace_member_service
    member_router = workspace_member_router
    catalog_service = workspace_catalog_service
    if cfg.auth.mode == "supabase" and cfg.auth.multi_workspace:
        member_router = member_router or create_workspace_member_service_router(cfg.auth)
        catalog_service = catalog_service or create_workspace_catalog_service(
            cfg.auth,
            photo_bucket=cfg.storage.photo_bucket,
        )
    elif cfg.auth.mode == "supabase":
        member_service = member_service or create_workspace_member_service(cfg.auth)
    agent_access = agent_credential_service
    if (
        agent_access is None
        and auth_service is None
        and cfg.auth.mode == "supabase"
        and cfg.auth.multi_workspace
    ):
        agent_access = create_agent_credential_service(cfg.auth)
    if cfg.backend != "none":
        backfill_from_layout(locations, layout)
    llm_state_dir = Path(cfg.database_path).parent
    llm_client = llm or LlmClient(
        cfg.llm,
        failure_state_path=llm_state_dir / "llm-failures-compatible.json",
    )
    use_openai_agent = llm is None and (
        cfg.assistant_backend == "openai"
        or (
            cfg.assistant_backend == "auto"
            and not _llm_key_present(cfg)
            and bool(os.environ.get("OPENAI_API_KEY"))
        )
    )
    agent_llm_client = (
        llm
        if llm is not None
        else OpenAIResponsesClient(
            model=cfg.openai_model,
            reasoning_effort="low",
            fallback=llm_client,
            failure_state_path=llm_state_dir / "llm-failures-openai.json",
        )
        if use_openai_agent
        else llm_client
    )
    agent_provider = "openai" if use_openai_agent else cfg.llm.provider
    agent_model = cfg.openai_model if use_openai_agent else cfg.llm.model
    agent_api = "responses" if use_openai_agent else "chat_completions"
    active_find_states: dict[str, dict[str, object]] = {}
    active_find_states_needing_restore: set[str] = set()
    active_find_lock = threading.RLock()
    tool_service = MagpieToolService(
        store=store,
        locations=locations,
        layout=layout,
        light_handler=lambda pixels, color, seconds, target: _light_pixels(
            pixels,
            seconds=seconds,
            color=color,
            target=target,
        ),
        clear_lights_handler=lambda: _clear_active_finds(),
        printer_status_handler=_label_printer_status,
    )
    mcp_server = create_mcp_server(tool_service)
    mcp_http_app = mcp_server.streamable_http_app()
    build = build_info()
    screen_states: dict[str, dict[str, object]] = {}
    voice_states: dict[str, dict[str, object]] = {}
    voice_last_results: dict[str, dict[str, object]] = {}
    kiosk_state_generations: dict[str, int] = {}
    kiosk_state_events: dict[str, asyncio.Event] = {}
    kiosk_storage_readiness_cache: dict[
        str, tuple[float, dict[str, object]]
    ] = {}
    kiosk_interaction_cache: dict[
        tuple[str, int], tuple[float, list[dict[str, object]]]
    ] = {}
    kiosk_pending_plan_cache: dict[
        tuple[str, int], tuple[float, dict[str, object] | None]
    ] = {}

    def _state_workspace_id() -> str:
        return repository_bundle.current_workspace_id() or "service"

    def _interaction_created_at(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            created_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc)

    def _confirmation_metadata(
        interaction: dict[str, object],
        *,
        expire: bool = True,
    ) -> dict[str, object]:
        result = dict(interaction)
        if result.get("status") != "pending":
            result["confirmable"] = False
            return result
        created_at = _interaction_created_at(result.get("created_at"))
        if created_at is None:
            result["confirmable"] = False
            return result
        now = datetime.now(timezone.utc)
        age_seconds = max(0.0, (now - created_at).total_seconds())
        expires_at = created_at + timedelta(
            seconds=PENDING_CONFIRMATION_TTL_SECONDS
        )
        expired = age_seconds > PENDING_CONFIRMATION_TTL_SECONDS
        result.update(
            {
                "age_seconds": age_seconds,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "confirmable": not expired,
            }
        )
        if expired and expire:
            active_find_removed, active_find_errors = _remove_pending_put_away_find(
                result,
            )
            store.set_assistant_interaction_status(
                int(result["id"]),
                "expired",
            )
            _invalidate_kiosk_interactions()
            result["status"] = "expired"
            result["confirmable"] = False
            result["active_find_removed"] = active_find_removed
            result["active_find_light_ok"] = not active_find_errors
            result["active_find_light_errors"] = active_find_errors
        return result

    def _confirmation_interactions(
        interactions: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        results = [
            _confirmation_metadata(interaction)
            for interaction in interactions
        ]

        def referenced_item_ids(value: object) -> set[int]:
            item_ids: set[int] = set()
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "item_id":
                        try:
                            item_id = int(nested)
                        except (TypeError, ValueError):
                            item_id = 0
                        if item_id > 0:
                            item_ids.add(item_id)
                    else:
                        item_ids.update(referenced_item_ids(nested))
            elif isinstance(value, list):
                for nested in value:
                    item_ids.update(referenced_item_ids(nested))
            return item_ids

        def referenced_item_names(value: object) -> dict[int, str]:
            names: dict[int, str] = {}
            if isinstance(value, dict):
                try:
                    item_id = int(value.get("item_id") or 0)
                except (TypeError, ValueError):
                    item_id = 0
                item_name = str(value.get("name") or "").strip()
                if item_id > 0 and item_name:
                    names[item_id] = item_name
                for nested in value.values():
                    names.update(referenced_item_names(nested))
            elif isinstance(value, list):
                for nested in value:
                    names.update(referenced_item_names(nested))
            return names

        for result in results:
            result["voice_confirmable"] = _voice_confirmable_plan(result)
            response = result.get("response")
            item_ids = referenced_item_ids(response)
            item_names = referenced_item_names(response)
            current_items: list[dict[str, object]] = []
            for item_id in sorted(item_ids)[:25]:
                rows = store.item_inventory(item_id)
                placements: list[dict[str, object]] = []
                for row in rows:
                    path = _path_for(row.bin_id)
                    placements.append(
                        {
                            "location_id": row.bin_id,
                            "path": path,
                            "path_label": " › ".join(
                                str(node.get("label") or node.get("id") or "")
                                for node in path
                                if isinstance(node, dict)
                            )
                            or row.bin_id,
                        }
                    )
                current_items.append(
                    {
                        "item_id": item_id,
                        "name": rows[0].name if rows else item_names.get(item_id, ""),
                        "active": bool(rows),
                        "placements": placements,
                    }
                )
            result["current_items"] = current_items
        return results

    def _confirmation_presentation(
        interaction: dict[str, object],
    ) -> dict[str, object] | None:
        response = interaction.get("response")
        if not isinstance(response, dict):
            return None
        plan = response.get("plan")
        if not isinstance(plan, list) or not plan:
            return None
        placement = response.get("placement")
        placement_reason = (
            str(placement.get("reason") or "").strip()
            if isinstance(placement, dict)
            else ""
        )

        def color_payload(raw_color: object) -> dict[str, object] | None:
            if not isinstance(raw_color, (list, tuple)) or len(raw_color) != 3:
                return None
            try:
                rgb = [max(0, min(255, int(channel))) for channel in raw_color]
            except (TypeError, ValueError):
                return None
            color_name = "Custom"
            for color_index, candidate in enumerate(ACTIVE_FIND_COLORS):
                if tuple(rgb) == candidate:
                    color_name = CONFIRMATION_COLOR_NAMES[color_index]
                    break
            return {"name": color_name, "rgb": rgb}

        actual_color_by_light: dict[str, dict[str, object]] = {}
        raw_assignments = response.get("light_assignments")
        if isinstance(raw_assignments, list):
            for raw_assignment in raw_assignments:
                if not isinstance(raw_assignment, dict):
                    continue
                light_location_id = str(
                    raw_assignment.get("lit_location")
                    or raw_assignment.get("location_id")
                    or ""
                ).strip()
                color = color_payload(raw_assignment.get("color"))
                if not light_location_id or color is None:
                    continue
                actual_color_by_light[light_location_id] = color

        session_color_by_destination: dict[str, dict[str, object]] = {}
        raw_active_finds = response.get("active_finds")
        if isinstance(raw_active_finds, list):
            for raw_find in raw_active_finds:
                if not isinstance(raw_find, dict):
                    continue
                color = color_payload(raw_find.get("color"))
                if color is None:
                    continue
                for raw_match in raw_find.get("matches") or []:
                    if not isinstance(raw_match, dict):
                        continue
                    location_id = str(raw_match.get("location_id") or "").strip()
                    if location_id:
                        session_color_by_destination[location_id] = color

        color_by_light: dict[str, dict[str, object]] = {}
        rows: list[dict[str, object]] = []
        placement_tools: set[str] = set()
        for index, raw_step in enumerate(plan):
            if not isinstance(raw_step, dict):
                continue
            tool = str(raw_step.get("tool") or "")
            if tool in {"put_item", "move_item"}:
                placement_tools.add(tool)
            raw_arguments = raw_step.get("arguments")
            arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
            destination_id = str(
                arguments.get(
                    "location_id" if tool == "put_item" else "to_location_id"
                )
                or (
                    arguments.get("new_parent_id")
                    if tool == "move_container"
                    else ""
                )
                or ""
            ).strip()
            destination_path = (
                [
                    {"id": node.id, "label": node.label or node.id}
                    for node in location_path(locations, destination_id)
                ]
                if destination_id
                else []
            )
            destination_label = " › ".join(
                str(node["label"]) for node in destination_path
            )
            nearest_lit = locations.nearest_lit(destination_id) if destination_id else None
            color: dict[str, object] | None = None
            if tool in {"put_item", "move_item"} and nearest_lit is not None:
                color_key = nearest_lit.id
                if color_key in actual_color_by_light:
                    color_by_light[color_key] = actual_color_by_light[color_key]
                elif color_key not in color_by_light:
                    color_index = len(color_by_light) % len(ACTIVE_FIND_COLORS)
                    color_by_light[color_key] = {
                        "name": CONFIRMATION_COLOR_NAMES[color_index],
                        "rgb": list(ACTIVE_FIND_COLORS[color_index]),
                    }
                color = color_by_light[color_key]
            elif tool in {"put_item", "move_item"}:
                color = session_color_by_destination.get(destination_id)

            if tool == "put_item":
                quantity = arguments.get("quantity") or 1
                try:
                    quantity_label = f"{float(quantity):g}"
                except (TypeError, ValueError):
                    quantity_label = str(quantity)
                item = str(arguments.get("item") or "item")
                verb = "Add" if arguments.get("quantity_mode") == "add" else "Store"
                summary = f"{verb} {quantity_label} x {item}"
            elif tool == "move_item":
                item = str(
                    arguments.get("item")
                    or (
                        f"item #{arguments.get('item_id')}"
                        if arguments.get("item_id")
                        else "item"
                    )
                )
                summary = f"Move {item}"
            elif tool == "remove_item":
                item = str(
                    arguments.get("item")
                    or (
                        f"item #{arguments.get('item_id')}"
                        if arguments.get("item_id")
                        else "item"
                    )
                )
                summary = f"Remove {item}"
            elif tool == "move_container":
                summary = f"Move {arguments.get('location_id') or 'location'}"
            else:
                summary = tool.replace("_", " ").strip().capitalize() or "Storage change"

            rows.append(
                {
                    "index": index,
                    "tool": tool,
                    "summary": summary,
                    "destination": (
                        {
                            "id": destination_id,
                            "path": destination_path,
                            "label": destination_label or destination_id,
                            "light_location_id": nearest_lit.id if nearest_lit else None,
                        }
                        if destination_id
                        else None
                    ),
                    "color": color,
                    "light": (
                        {
                            "location_id": nearest_lit.id,
                            "status": (
                                "lit"
                                if nearest_lit.id in actual_color_by_light
                                and bool(response.get("lit"))
                                else (
                                    "unavailable"
                                    if nearest_lit.id in actual_color_by_light
                                    else "available"
                                )
                            ),
                        }
                        if nearest_lit is not None
                        else {"location_id": None, "status": "unavailable"}
                    ),
                }
            )

        return {
            "schema": "magpie.confirmation.v1",
            "title": "Ready to confirm",
            "instruction": "Review the destination, then confirm or change it.",
            "voice_instruction": (
                "Say yes to confirm, no to cancel, or say what to change."
            ),
            "actions": [
                {"id": "confirm", "label": "Confirm"},
                {"id": "change", "label": "Change"},
                {"id": "cancel", "label": "Cancel"},
            ],
            "change_prompts": (
                [
                    {
                        "id": "different_place",
                        "label": "Different place",
                        "text": "Use a different storage place.",
                    },
                    {
                        "id": "bigger_container",
                        "label": "Bigger container",
                        "text": "Use a bigger bin or container.",
                    },
                    {
                        "id": "smaller_container",
                        "label": "Smaller container",
                        "text": "Use a smaller bin or container.",
                    },
                    {
                        "id": "wrong_item",
                        "label": "Wrong item",
                        "text": "The item identity is wrong.",
                    },
                ]
                if "put_item" in placement_tools
                else [
                    {
                        "id": "different_place",
                        "label": "Different place",
                        "text": "Use a different storage place.",
                    }
                ]
                if placement_tools
                else []
            ),
            "rows": rows,
            "explanation": (
                {
                    "label": "Why this location?",
                    "summary": placement_reason,
                }
                if placement_reason
                else None
            ),
        }

    def _cached_pending_agent_plan(cursor: int) -> dict[str, object] | None:
        workspace_id = _state_workspace_id()
        now = time.monotonic()
        cache_key = (workspace_id, max(0, int(cursor)))
        cached = kiosk_pending_plan_cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return dict(cached[1]) if cached[1] is not None else None
        for key, (expires_at, _) in tuple(kiosk_pending_plan_cache.items()):
            if expires_at <= now:
                kiosk_pending_plan_cache.pop(key, None)
        pending = store.latest_pending_agent_plan()
        if pending is None:
            kiosk_pending_plan_cache[cache_key] = (
                now + KIOSK_IDLE_CACHE_SECONDS,
                None,
            )
        else:
            kiosk_pending_plan_cache.pop(cache_key, None)
        return dict(pending) if pending is not None else None

    def _confirmable_agent_plan(
        pending: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if pending is None:
            return None
        candidate = _confirmation_metadata(pending)
        if not candidate.get("confirmable"):
            return None
        result = dict(candidate)
        response = candidate.get("response")
        result["response"] = dict(response) if isinstance(response, dict) else {}
        result["response"]["confirmation"] = _confirmation_presentation(candidate)
        return result

    def _latest_confirmable_agent_plan() -> dict[str, object] | None:
        return _confirmable_agent_plan(store.latest_pending_agent_plan())

    def _voice_confirmable_plan(pending: dict[str, object] | None) -> bool:
        if pending is None:
            return False
        response = pending.get("response")
        plan = response.get("plan") if isinstance(response, dict) else None
        if not isinstance(plan, list) or not plan:
            return False
        return not any(
            not isinstance(step, dict) or step.get("tool") != "put_item"
            for step in plan
        )

    def _latest_voice_confirmable_plan() -> dict[str, object] | None:
        pending = _latest_confirmable_agent_plan()
        return pending if _voice_confirmable_plan(pending) else None

    def _supersede_pending_agent_plans() -> None:
        store.supersede_pending_agent_plans()

    def _target_pixel_count(target: str) -> int:
        if not isinstance(controller, WorkspaceLedController):
            return controller.config.pixel_count
        indexes = [
            index
            for location in locations.all()
            if location.light_target == target
            for index in location.light_indexes
            if index >= 0
        ]
        return max(indexes, default=-1) + 1

    def _light_index_limit() -> int:
        return 10000 if isinstance(controller, WorkspaceLedController) else cfg.pixel_count

    def _require_local_light_controller() -> LedController:
        if isinstance(controller, WorkspaceLedController):
            raise HTTPException(
                status_code=400,
                detail="raw pixel commands require a location-selected device target",
            )
        return controller

    def _controller_set_pixels(
        updates: list[PixelUpdate],
        *,
        target: str,
        clear_others: bool,
        brightness: float | None,
    ) -> None:
        if isinstance(controller, WorkspaceLedController):
            controller.set_pixels(
                updates,
                target=target,
                pixel_count=_target_pixel_count(target),
                workspace_id=_state_workspace_id(),
                clear_others=clear_others,
                brightness=brightness,
            )
            return
        controller.set_pixels(
            updates,
            clear_others=clear_others,
            brightness=brightness,
        )

    def _controller_set_pixels_for(
        updates: list[PixelUpdate],
        *,
        target: str,
        clear_others: bool,
        brightness: float | None,
        seconds: float,
    ) -> None:
        if isinstance(controller, WorkspaceLedController):
            controller.set_pixels_for(
                updates,
                target=target,
                pixel_count=_target_pixel_count(target),
                workspace_id=_state_workspace_id(),
                clear_others=clear_others,
                brightness=brightness,
                seconds=seconds,
            )
            return
        controller.set_pixels_for(
            updates,
            clear_others=clear_others,
            brightness=brightness,
            seconds=seconds,
        )

    def _controller_clear(target: str) -> None:
        if isinstance(controller, WorkspaceLedController):
            controller.clear(
                target=target,
                pixel_count=_target_pixel_count(target),
                workspace_id=_state_workspace_id(),
            )
            return
        controller.clear()

    def _workspace_light_target_ready(target: str) -> bool:
        if not isinstance(controller, WorkspaceLedController):
            return True
        clean_target = target.strip()
        if not clean_target or edge_commands is None:
            return False
        try:
            devices = edge_commands.status().get("devices", [])
        except Exception:
            return False
        return any(
            isinstance(device, dict)
            and str(device.get("device_id") or "") == clean_target
            and _edge_device_connection(device).get("command_ready") is True
            for device in devices
        )

    def _screen_state() -> dict[str, object]:
        return screen_states.setdefault(
            _state_workspace_id(),
            {
                "screen_id": "",
                "url": "",
                "build_label": "",
                "last_seen_interaction_id": 0,
                "status": "unknown",
                "updated_at": 0.0,
            },
        )

    def _voice_state() -> dict[str, object]:
        return voice_states.setdefault(
            _state_workspace_id(),
            {
                "state": "starting",
                "message": "Voice starting",
                "transcript": "",
                "command": "",
                "device_id": "",
                "runtime_version": "",
                "capabilities": {},
                "updated_at": 0.0,
            },
        )

    def _voice_last_result() -> dict[str, object]:
        return voice_last_results.setdefault(_state_workspace_id(), {})

    def _active_find_state() -> dict[str, object]:
        workspace_id = _state_workspace_id()
        existing = active_find_states.get(workspace_id)
        if existing is not None:
            return existing
        persisted = store.load_active_guidance_state()
        raw_entries = persisted.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        highest_id = max(
            (
                int(entry.get("id") or 0)
                for entry in entries
                if isinstance(entry, dict)
            ),
            default=0,
        )
        try:
            next_id = int(persisted.get("next_id") or 1)
        except (TypeError, ValueError):
            next_id = 1
        state = {
            "entries": entries,
            "next_id": max(1, next_id, highest_id + 1),
        }
        active_find_states[workspace_id] = state
        if entries:
            active_find_states_needing_restore.add(workspace_id)
        return state

    def _persist_active_find_state() -> None:
        state = _active_find_state()
        store.save_active_guidance_state(
            {
                "entries": state["entries"],
                "next_id": state["next_id"],
            }
        )

    def _notify_kiosk_state(workspace_id: str = "") -> None:
        workspace_id = workspace_id or _state_workspace_id()
        kiosk_state_generations[workspace_id] = (
            kiosk_state_generations.get(workspace_id, 0) + 1
        )
        kiosk_state_events.setdefault(workspace_id, asyncio.Event()).set()

    def _invalidate_kiosk_interactions(workspace_id: str = "") -> None:
        workspace_id = workspace_id or _state_workspace_id()
        for cache_key in tuple(kiosk_pending_plan_cache):
            if cache_key[0] == workspace_id:
                kiosk_pending_plan_cache.pop(cache_key, None)
        for cache_key in tuple(kiosk_interaction_cache):
            if cache_key[0] == workspace_id:
                kiosk_interaction_cache.pop(cache_key, None)

    def _invalidate_kiosk_storage_readiness(workspace_id: str = "") -> None:
        workspace_id = workspace_id or _state_workspace_id()
        kiosk_storage_readiness_cache.pop(workspace_id, None)

    def _raw_active_find_snapshot() -> list[dict[str, object]]:
        with active_find_lock:
            entries = _active_find_state()["entries"]
            return [
                {
                    **entry,
                    "matches": [
                        {
                            key: value
                            for key, value in match.items()
                            if not str(key).startswith("_")
                        }
                        for match in entry.get("matches", [])
                    ],
                }
                for entry in entries  # type: ignore[union-attr]
            ]

    def _active_find_snapshot() -> list[dict[str, object]]:
        _reconcile_active_finds()
        workspace_id = _state_workspace_id()
        if workspace_id in active_find_states_needing_restore:
            active_find_states_needing_restore.discard(workspace_id)
            _restore_active_lights()
        return _raw_active_find_snapshot()

    def _clear_active_finds() -> list[str]:
        hardware_errors: list[str] = []
        with active_find_lock:
            state = _active_find_state()
            targets = {
                str(location.light_target or "")
                for active in state["entries"]  # type: ignore[union-attr]
                for match in active.get("matches", [])
                if (location := locations.get(str(match.get("lit_location") or "")))
            }
            state["entries"] = []
            state["next_id"] = 1
            _persist_active_find_state()
            if isinstance(controller, WorkspaceLedController):
                targets.update(
                    str(location.light_target or "")
                    for location in locations.all()
                    if location.light_target and location.light_indexes
                )
                for target in sorted(target for target in targets if target):
                    try:
                        _controller_clear(target)
                    except Exception as exc:
                        hardware_errors.append(
                            f"{target}: {str(exc).strip()[:200] or type(exc).__name__}"
                        )
            else:
                try:
                    _controller_clear("")
                except Exception as exc:
                    hardware_errors.append(
                        str(exc).strip()[:200] or type(exc).__name__
                    )
        _notify_kiosk_state()
        return hardware_errors

    def _restore_active_lights() -> bool:
        updates_by_target: dict[str, dict[int, PixelUpdate]] = {}
        with active_find_lock:
            for active in _active_find_state()["entries"]:  # type: ignore[union-attr]
                active_color = tuple(active.get("color") or (255, 255, 255))
                for match in active.get("matches", []):
                    lit = locations.get(str(match.get("lit_location") or ""))
                    if lit is None:
                        continue
                    target = str(lit.light_target or "")
                    target_updates = updates_by_target.setdefault(target, {})
                    for pixel in lit.light_indexes:
                        if 0 <= pixel < _target_pixel_count(target):
                            target_updates[pixel] = PixelUpdate(
                                index=pixel,
                                color=active_color,
                            )
            if not any(updates_by_target.values()):
                return False
            if not all(
                _workspace_light_target_ready(target)
                for target, updates in updates_by_target.items()
                if updates
            ):
                return False
            try:
                for target, updates_by_pixel in updates_by_target.items():
                    _controller_set_pixels(
                        list(updates_by_pixel.values()),
                        target=target,
                        clear_others=True,
                        brightness=cfg.default_brightness,
                    )
                return True
            except Exception:
                return False

    def _restore_active_lights_after(seconds: float) -> None:
        workspace_id = _state_workspace_id()

        def restore() -> None:
            token = None
            try:
                if repository_bundle.current_workspace_id() != workspace_id:
                    token = repository_bundle.bind(workspace_id)
                _restore_active_lights()
            except Exception:
                return
            finally:
                if token is not None:
                    repository_bundle.reset(token)

        timer = threading.Timer(seconds + 0.05, restore)
        timer.daemon = True
        timer.start()

    def _reconcile_active_finds() -> tuple[bool, list[str]]:
        """Keep long-lived colored finds aligned with the current storage graph."""
        with active_find_lock:
            state = _active_find_state()
            current_entries = list(state["entries"])  # type: ignore[arg-type]
            if not current_entries:
                return False, []

            old_targets = {
                str(location.light_target or "")
                for entry in current_entries
                for match in entry.get("matches", [])
                if (
                    location := locations.get_any(
                        str(match.get("lit_location") or "")
                    )
                )
                is not None
            }
            resolved_items: dict[int, int | None] = {}
            inventory_by_item: dict[int, list] = {}
            seen_matches: set[tuple[str, str, int, str, tuple[int, ...]]] = set()
            retained_reversed: list[dict[str, object]] = []
            for entry in reversed(current_entries):
                retained_matches: list[dict[str, object]] = []
                entry_kind = str(entry.get("kind") or "")
                active_color = tuple(
                    int(component) for component in entry.get("color", [])
                )
                for match in entry.get("matches", []):
                    location_id = str(match.get("location_id") or "")
                    location = locations.get(location_id)
                    if location is None:
                        continue
                    item_id = int(match.get("item_id") or 0)
                    canonical_item_id = item_id
                    canonical_name = str(match.get("name") or "")
                    if item_id:
                        if item_id not in resolved_items:
                            resolved_items[item_id] = store.resolve_item_id(
                                item_id
                            )
                        resolved_item_id = resolved_items[item_id]
                        repository_backed = match.get("_repository_backed")
                        if repository_backed is True:
                            if resolved_item_id is None:
                                continue
                            if resolved_item_id not in inventory_by_item:
                                inventory_by_item[resolved_item_id] = (
                                    store.item_inventory(resolved_item_id)
                                )
                            current_inventory = inventory_by_item[
                                resolved_item_id
                            ]
                            current_placement = next(
                                (
                                    row
                                    for row in current_inventory
                                    if str(row.bin_id) == location_id
                                ),
                                None,
                            )
                            if (
                                current_placement is None
                                and entry_kind == "put_away"
                                and len(current_inventory) == 1
                            ):
                                current_placement = current_inventory[0]
                                location_id = str(current_placement.bin_id)
                                location = locations.get(location_id)
                                if location is None:
                                    continue
                            if current_placement is None:
                                continue
                            canonical_item_id = resolved_item_id
                            canonical_name = str(current_placement.name)
                        elif (
                            repository_backed is None
                            and resolved_item_id is not None
                        ):
                            if resolved_item_id not in inventory_by_item:
                                inventory_by_item[resolved_item_id] = (
                                    store.item_inventory(resolved_item_id)
                                )
                            current_inventory = inventory_by_item[
                                resolved_item_id
                            ]
                            match_name = re.sub(
                                r"\s+",
                                " ",
                                str(match.get("name") or ""),
                            ).strip().casefold()
                            known_names = {
                                re.sub(r"\s+", " ", candidate)
                                .strip()
                                .casefold()
                                for row in current_inventory
                                for candidate in [
                                    str(row.name),
                                    *(str(alias) for alias in row.aliases),
                                ]
                                if candidate.strip()
                            }
                            current_locations = {
                                str(row.bin_id) for row in current_inventory
                            }
                            if (
                                match_name in known_names
                                and location_id not in current_locations
                            ):
                                continue
                    lit = locations.nearest_lit(location_id)
                    guidance = (
                        ("light", lit.id)
                        if lit is not None
                        else ("location", location_id)
                    )
                    match_key = (
                        guidance[0],
                        guidance[1],
                        canonical_item_id,
                        canonical_name.strip().casefold(),
                        active_color,
                    )
                    if match_key in seen_matches:
                        continue
                    seen_matches.add(match_key)
                    retained_matches.append(
                        {
                            **match,
                            "item_id": canonical_item_id,
                            "name": canonical_name,
                            "location_id": location_id,
                            "lit_location": lit.id if lit is not None else None,
                            "path": _path_for(location_id),
                        }
                    )
                if retained_matches:
                    retained_reversed.append(
                        {**entry, "matches": retained_matches}
                    )
            retained_entries = list(reversed(retained_reversed))
            if retained_entries == current_entries:
                return False, []
            state["entries"] = retained_entries
            _persist_active_find_state()

        hardware_errors: list[str] = []
        for target in sorted(old_targets):
            if isinstance(controller, WorkspaceLedController) and not target:
                continue
            try:
                _controller_clear(target)
            except Exception as exc:
                hardware_errors.append(
                    (
                        f"{target}: " if target else ""
                    )
                    + (str(exc).strip()[:200] or type(exc).__name__)
                )
        has_remaining_lights = any(
            match.get("lit_location")
            for entry in retained_entries
            for match in entry.get("matches", [])
        )
        if has_remaining_lights and not _restore_active_lights():
            hardware_errors.append(
                "remaining active-find lights could not be restored"
            )
        _notify_kiosk_state()
        return True, hardware_errors

    def _activate_find(
        query: str,
        results: list[dict[str, object]],
        *,
        kind: str = "find",
        replace_query: str = "",
    ) -> dict[str, object]:
        def guidance_key(match: dict[str, object]) -> tuple[str, str]:
            lit_location_id = str(match.get("lit_location") or "")
            if lit_location_id:
                return ("light", lit_location_id)
            return ("location", str(match.get("location_id") or ""))

        inventory_by_item: dict[int, list] = {}

        def repository_backed(
            item_id: int,
            location_id: str,
            name: str,
        ) -> bool:
            if item_id <= 0:
                return False
            resolved_item_id = store.resolve_item_id(item_id)
            if resolved_item_id is None:
                return False
            if resolved_item_id not in inventory_by_item:
                inventory_by_item[resolved_item_id] = store.item_inventory(
                    resolved_item_id
                )
            clean_name = re.sub(r"\s+", " ", name).strip().casefold()
            return any(
                str(row.bin_id) == location_id
                and clean_name
                in {
                    re.sub(r"\s+", " ", candidate).strip().casefold()
                    for candidate in [
                        str(row.name),
                        *(str(alias) for alias in row.aliases),
                    ]
                    if candidate.strip()
                }
                for row in inventory_by_item[resolved_item_id]
            )

        matches: list[dict[str, object]] = []
        seen_guidance: set[tuple[str, str]] = set()
        for result in results:
            location_id = str(result.get("location_id") or result.get("bin_id") or "")
            lit = locations.nearest_lit(location_id) if location_id else None
            current_key = ("light", lit.id) if lit is not None else ("location", location_id)
            if not location_id or current_key in seen_guidance:
                continue
            seen_guidance.add(current_key)
            path = result.get("path") if isinstance(result.get("path"), list) else []
            item_id = int(result.get("item_id") or 0)
            name = str(result.get("name") or query)
            matches.append(
                {
                    "item_id": item_id,
                    "name": name,
                    "location_id": location_id,
                    "lit_location": lit.id if lit is not None else None,
                    "path": path,
                    "_repository_backed": repository_backed(
                        item_id,
                        location_id,
                        name,
                    ),
                }
            )
        if not matches:
            return {"lit": False, "locations": [], "active_finds": _active_find_snapshot()}

        with active_find_lock:
            state = _active_find_state()
            entries = state["entries"]
            entry_id = int(state["next_id"])
            state["next_id"] = entry_id + 1
            current_guidance = {guidance_key(match) for match in matches}
            replacement_colors = [
                tuple(int(component) for component in prior.get("color", []))
                for prior in entries  # type: ignore[union-attr]
                if replace_query
                and str(prior.get("kind") or "") == kind
                and str(prior.get("query") or "").casefold()
                == replace_query.casefold()
            ]
            overlapping_colors = [
                tuple(int(component) for component in prior.get("color", []))
                for prior in entries  # type: ignore[union-attr]
                if any(
                    guidance_key(match) in current_guidance
                    for match in prior.get("matches", [])
                )
            ]
            color = (
                replacement_colors[0]
                if replacement_colors
                else (
                    overlapping_colors[0]
                    if overlapping_colors
                    else ACTIVE_FIND_COLORS[
                        (entry_id - 1) % len(ACTIVE_FIND_COLORS)
                    ]
                )
            )
            retained: list[dict[str, object]] = []
            for prior in entries:  # type: ignore[union-attr]
                if (
                    replace_query
                    and str(prior.get("kind") or "") == kind
                    and str(prior.get("query") or "").casefold()
                    == replace_query.casefold()
                ):
                    continue
                prior_color = tuple(
                    int(component) for component in prior.get("color", [])
                )
                prior_matches = [
                    match
                    for match in prior.get("matches", [])
                    if (
                        guidance_key(match) not in current_guidance
                        or prior_color == color
                    )
                ]
                if prior_matches:
                    retained.append({**prior, "matches": prior_matches})
            entry = {
                "id": entry_id,
                "kind": kind,
                "query": query.strip(),
                "color": color,
                "matches": matches,
                "created_at": time.time(),
            }
            state["entries"] = [*retained, entry]
            _persist_active_find_state()
            lit_ok = _restore_active_lights()
            assignments = [
                {
                    "location_id": match["location_id"],
                    "lit_location": match["lit_location"],
                    "color": color,
                }
                for match in matches
                if match.get("lit_location")
            ]
            return {
                "lit": lit_ok,
                "locations": assignments,
                "active_finds": _active_find_snapshot(),
            }

    def _remove_active_find_match(
        *,
        item_id: int,
        location_id: str,
        active_find_id: int | None = None,
    ) -> tuple[bool, list[str]]:
        removed_targets: set[str] = set()
        remaining_targets: set[str] = set()
        removed = False
        with active_find_lock:
            state = _active_find_state()
            retained_entries: list[dict[str, object]] = []
            for entry in state["entries"]:  # type: ignore[union-attr]
                entry_id = int(entry.get("id") or 0)
                retained_matches: list[dict[str, object]] = []
                for match in entry.get("matches", []):
                    match_location_id = str(match.get("location_id") or "")
                    same_match = (
                        int(match.get("item_id") or 0) == item_id
                        and match_location_id == location_id
                        and (active_find_id is None or entry_id == active_find_id)
                    )
                    lit = locations.get(str(match.get("lit_location") or ""))
                    target = str(lit.light_target or "") if lit is not None else ""
                    if same_match:
                        removed = True
                        if target:
                            removed_targets.add(target)
                        continue
                    retained_matches.append(match)
                    if target:
                        remaining_targets.add(target)
                if retained_matches:
                    retained_entries.append({**entry, "matches": retained_matches})
            state["entries"] = retained_entries
            _persist_active_find_state()
        if not removed:
            return False, []
        hardware_errors: list[str] = []
        for target in sorted(removed_targets - remaining_targets):
            try:
                _controller_clear(target)
            except Exception as exc:
                hardware_errors.append(
                    f"{target}: {str(exc).strip()[:200] or type(exc).__name__}"
                )
        if remaining_targets and not _restore_active_lights():
            hardware_errors.append("remaining active-find lights could not be restored")
        _notify_kiosk_state()
        return True, hardware_errors

    def _remove_pending_put_away_find(
        source: dict[str, object] | None,
        *,
        fallback_command: str = "",
    ) -> tuple[bool, list[str]]:
        if source is None:
            return False, []
        response = source.get("response")
        response = response if isinstance(response, dict) else {}
        queries = {
            re.sub(r"\s+", " ", str(value)).strip().casefold()
            for value in (
                response.get("original_command"),
                source.get("command"),
                fallback_command,
            )
            if str(value or "").strip()
        }
        if not queries:
            return False, []
        response_active_finds = response.get("active_finds")
        response_active_finds = (
            response_active_finds if isinstance(response_active_finds, list) else []
        )
        candidate_entry_ids = [
            int(entry.get("id") or 0)
            for entry in response_active_finds
            if isinstance(entry, dict)
            and str(entry.get("kind") or "") == "put_away"
            and re.sub(
                r"\s+",
                " ",
                str(entry.get("query") or ""),
            ).strip().casefold()
            in queries
            and int(entry.get("id") or 0) > 0
        ]
        target_entry_id = max(candidate_entry_ids, default=0)

        removed_targets: set[str] = set()
        remaining_targets: set[str] = set()
        removed = False
        with active_find_lock:
            state = _active_find_state()
            retained_entries: list[dict[str, object]] = []
            for entry in state["entries"]:  # type: ignore[union-attr]
                entry_query = re.sub(
                    r"\s+",
                    " ",
                    str(entry.get("query") or ""),
                ).strip().casefold()
                remove_entry = (
                    str(entry.get("kind") or "") == "put_away"
                    and (
                        int(entry.get("id") or 0) == target_entry_id
                        if target_entry_id
                        else entry_query in queries
                    )
                )
                for match in entry.get("matches", []):
                    lit = locations.get(str(match.get("lit_location") or ""))
                    target = str(lit.light_target or "") if lit is not None else ""
                    if not target:
                        continue
                    if remove_entry:
                        removed_targets.add(target)
                    else:
                        remaining_targets.add(target)
                if remove_entry:
                    removed = True
                else:
                    retained_entries.append(entry)
            state["entries"] = retained_entries
            _persist_active_find_state()
        if not removed:
            return False, []

        hardware_errors: list[str] = []
        for target in sorted(removed_targets - remaining_targets):
            try:
                _controller_clear(target)
            except Exception as exc:
                hardware_errors.append(
                    f"{target}: {str(exc).strip()[:200] or type(exc).__name__}"
                )
        if remaining_targets and not _restore_active_lights():
            hardware_errors.append("remaining active-find lights could not be restored")
        _notify_kiosk_state()
        return True, hardware_errors

    def _bind_confirmed_put_away_find(
        *,
        source_interaction_id: int | None,
        applied: list[dict[str, object]],
    ) -> bool:
        """Connect provisional put-away guidance to the item that was saved."""
        if source_interaction_id is None:
            return False
        source = store.assistant_interaction(source_interaction_id)
        if source is None:
            return False
        response = source.get("response")
        response = response if isinstance(response, dict) else {}
        queries = {
            re.sub(r"\s+", " ", str(value)).strip().casefold()
            for value in (
                response.get("original_command"),
                source.get("command"),
            )
            if str(value or "").strip()
        }
        response_active_finds = response.get("active_finds")
        response_active_finds = (
            response_active_finds if isinstance(response_active_finds, list) else []
        )
        candidate_entry_ids = {
            int(entry.get("id") or 0)
            for entry in response_active_finds
            if isinstance(entry, dict)
            and str(entry.get("kind") or "") == "put_away"
            and re.sub(
                r"\s+",
                " ",
                str(entry.get("query") or ""),
            ).strip().casefold()
            in queries
            and int(entry.get("id") or 0) > 0
        }
        if not candidate_entry_ids:
            return False

        targets: list[dict[str, object]] = []
        for result in applied:
            if not isinstance(result, dict):
                continue
            raw_item_id = result.get("item_id") or result.get("moved_item")
            location_id = str(
                result.get("location_id") or result.get("to_location_id") or ""
            )
            if not raw_item_id or not location_id:
                continue
            item_id = int(raw_item_id)
            current = next(
                (
                    row
                    for row in store.item_inventory(item_id)
                    if str(row.bin_id) == location_id
                ),
                None,
            )
            if current is None:
                continue
            targets.append(
                {
                    "item_id": item_id,
                    "name": str(current.name),
                    "location_id": location_id,
                }
            )
        if not targets:
            return False

        changed = False
        with active_find_lock:
            state = _active_find_state()
            for entry in state["entries"]:  # type: ignore[union-attr]
                if int(entry.get("id") or 0) not in candidate_entry_ids:
                    continue
                matches = entry.get("matches", [])
                if not isinstance(matches, list):
                    continue
                unmatched = list(targets)
                for match in matches:
                    if not isinstance(match, dict):
                        continue
                    location_id = str(match.get("location_id") or "")
                    target_index = next(
                        (
                            index
                            for index, target in enumerate(unmatched)
                            if str(target["location_id"]) == location_id
                        ),
                        None,
                    )
                    if target_index is None and len(unmatched) == 1:
                        target_index = 0
                    if target_index is None:
                        continue
                    target = unmatched.pop(target_index)
                    match.update(
                        {
                            **target,
                            "_repository_backed": True,
                            "path": _path_for(str(target["location_id"])),
                        }
                    )
                    changed = True
        if changed:
            _persist_active_find_state()
            _notify_kiosk_state()
        return changed

    def _report_find_not_here(
        *,
        item_id: int,
        location_id: str,
        query: str = "",
        active_find_id: int | None = None,
        source: str = "ui",
    ) -> dict[str, object]:
        target = _canonical_inventory_location(location_id)
        if target is None:
            raise HTTPException(status_code=404, detail="unknown bin or location")
        current = next(
            (
                row
                for row in store.item_inventory(item_id)
                if row.bin_id == target
            ),
            None,
        )
        if current is None:
            raise HTTPException(
                status_code=404,
                detail="item is not recorded in that location",
            )
        reason = "Reported missing during retrieval."
        already_uncertain = bool(
            current.needs_review and current.review_reason == reason
        )
        if not already_uncertain:
            store.mark_needs_review(
                item_id=item_id,
                bin_id=target,
                reason=reason,
            )
            store.record_example(
                command=(query.strip() or f"{current.name} was not in {target}"),
                action=json.dumps(
                    {
                        "find_not_here": {
                            "item_id": item_id,
                            "item": current.name,
                            "location_id": target,
                        }
                    },
                    sort_keys=True,
                ),
                kind="correction",
                context={
                    "source": source.strip() or "ui",
                    "active_find_id": active_find_id,
                },
            )
        removed, hardware_errors = _remove_active_find_match(
            item_id=item_id,
            location_id=target,
            active_find_id=active_find_id,
        )
        return {
            "ok": True,
            "action": "find_not_here",
            "item_id": item_id,
            "item": current.name,
            "location_id": target,
            "needs_review": True,
            "already_uncertain": already_uncertain,
            "active_find_removed": removed,
            "active_finds": _active_find_snapshot(),
            "light_ok": not hardware_errors,
            "light_errors": hardware_errors,
            "message": (
                f"Marked {current.name}'s recorded location as uncertain. "
                "Move it when you find it."
            ),
        }
    atexit.register(controller.close)
    if human_auth is not None:
        close_auth = getattr(human_auth, "close", None)
        if callable(close_auth):
            atexit.register(close_auth)
    if member_service is not None:
        close_members = getattr(member_service, "close", None)
        if callable(close_members):
            atexit.register(close_members)
    if member_router is not None:
        atexit.register(member_router.close)
    if catalog_service is not None:
        close_catalog = getattr(catalog_service, "close", None)
        if callable(close_catalog):
            atexit.register(close_catalog)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(
        title="Searchable Storage LED API",
        version=str(build.version),
        lifespan=lifespan,
    )
    # Local development can serve standalone browser tools from another origin.
    # Hosted Magpie is same-origin and must not inherit that LAN-wide browser access.
    browser_origins = [cfg.public_origin] if cfg.public_origin else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=browser_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/mcp", mcp_http_app, name="magpie-mcp")

    @app.exception_handler(LocationNameConflictError)
    async def location_name_conflict_handler(
        _request: Request,
        exc: LocationNameConflictError,
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    edge_device_paths = {
        "/assistant/chat": "voice",
        "/assistant/revise": "voice",
        "/assistant/voice/confirm": "voice",
        "/assistant/voice/cancel": "voice",
        "/assistant/undo": "voice",
        "/voice/attempts": "voice",
        "/voice/state": "voice",
        "/kiosk/state": "screen",
        "/kiosk/actions/confirm": "screen",
        "/kiosk/actions/revise": "screen",
        "/kiosk/actions/cancel": "screen",
        "/kiosk/actions/undo": "screen",
        "/kiosk/actions/clear": "screen",
        "/screen/heartbeat": "screen",
    }

    @app.middleware("http")
    async def human_authentication(request: Request, call_next):
        if human_auth is None or cfg.auth.mode == "none":
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.method in {"GET", "HEAD"} and (
            request.url.path in PUBLIC_GET_PATHS
            or request.url.path.startswith("/scan/")
        ):
            return await call_next(request)
        if request.url.path.startswith("/edge/devices/"):
            workspace_token = None
            if repository_bundle.dynamic:
                requested_workspace = request.headers.get(
                    "x-magpie-workspace", ""
                ).strip()
                if not requested_workspace:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "X-Magpie-Workspace is required"},
                    )
                try:
                    workspace_token = repository_bundle.bind(requested_workspace)
                except ValueError as exc:
                    return JSONResponse(status_code=400, content={"detail": str(exc)})
            try:
                return await call_next(request)
            finally:
                if workspace_token is not None:
                    repository_bundle.reset(workspace_token)
        authorization = request.headers.get("authorization", "")
        device_id = request.headers.get("x-magpie-device-id", "").strip().lower()
        required_scope = edge_device_paths.get(request.url.path)
        if device_id and required_scope:
            workspace_token = None
            try:
                if repository_bundle.dynamic:
                    requested_workspace = request.headers.get(
                        "x-magpie-workspace", ""
                    ).strip()
                    if not requested_workspace:
                        raise HTTPException(
                            status_code=400,
                            detail="X-Magpie-Workspace is required",
                        )
                    workspace_token = repository_bundle.bind(requested_workspace)
                device_id = _edge_device_id(device_id)
                _authorize_edge(device_id, authorization, required_scope=required_scope)
            except (HTTPException, ValueError) as exc:
                if workspace_token is not None:
                    repository_bundle.reset(workspace_token)
                status_code = exc.status_code if isinstance(exc, HTTPException) else 400
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                return JSONResponse(
                    status_code=status_code,
                    content={"detail": detail},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.magpie_device_id = device_id
            request.state.magpie_workspace_id = (
                repository_bundle.current_workspace_id()
            )
            try:
                return await call_next(request)
            finally:
                if workspace_token is not None:
                    repository_bundle.reset(workspace_token)
        token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if request.url.path.startswith("/mcp") and token.startswith("magpie_mcp_"):
            if agent_access is None:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "agent connections are not configured"},
                )
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            try:
                credential = agent_access.authenticate(token_hash)
            except RuntimeError:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "agent authentication is unavailable"},
                )
            if credential is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "invalid agent credentials"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            credential_workspace = str(credential.get("workspace_id") or "")
            requested_workspace = request.headers.get("x-magpie-workspace", "").strip()
            if requested_workspace and requested_workspace != credential_workspace:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "agent credential is bound to a different workspace"},
                )
            identity = AuthIdentity(
                user_id=f"agent:{credential.get('id')}",
                email="",
                workspace_id=credential_workspace,
                role="member",
            )
            request.state.magpie_identity = identity
            request.state.magpie_workspace_id = identity.workspace_id
            workspace_token = None
            try:
                if repository_bundle.dynamic:
                    workspace_token = repository_bundle.bind(identity.workspace_id)
                return await call_next(request)
            finally:
                if workspace_token is not None:
                    repository_bundle.reset(workspace_token)
        requested_workspace = (
            request.headers.get("x-magpie-workspace", "")
            if cfg.auth.multi_workspace
            else ""
        )
        allow_unassigned = bool(
            cfg.auth.multi_workspace
            and request.url.path
            in {"/auth/session", "/account", "/workspaces", "/client/bootstrap"}
        )
        try:
            identity = human_auth.authenticate(
                token,
                requested_workspace,
                allow_unassigned=allow_unassigned,
            )
        except AuthError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": str(exc)},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path
            not in {"/account", "/workspaces", "/workspace/membership"}
            and identity.role not in {"owner", "admin", "member"}
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "this workspace role has read-only access"},
            )
        request.state.magpie_identity = identity
        request.state.magpie_workspace_id = identity.workspace_id
        workspace_token = None
        if repository_bundle.dynamic and identity.workspace_id:
            workspace_token = repository_bundle.bind(identity.workspace_id)
        try:
            return await call_next(request)
        finally:
            if workspace_token is not None:
                repository_bundle.reset(workspace_token)

    @app.middleware("http")
    async def kiosk_state_notifications(request: Request, call_next):
        response = await call_next(request)
        successful_write = (
            response.status_code < 400
            and request.method not in {"GET", "HEAD", "OPTIONS"}
        )
        if successful_write:
            identity = getattr(request.state, "magpie_identity", None)
            workspace_id = str(
                getattr(request.state, "magpie_workspace_id", "")
                or getattr(identity, "workspace_id", "")
                or ""
            )
            if not request.url.path.startswith(("/voice/", "/screen/")):
                _invalidate_kiosk_storage_readiness(workspace_id)
                _invalidate_kiosk_interactions(workspace_id)
            if request.url.path not in {"/screen/heartbeat", "/voice/attempts"}:
                _notify_kiosk_state(workspace_id)
        return response

    def _authorize_edge(
        device_id: str,
        authorization: str | None,
        *,
        required_scope: str | None = None,
    ) -> None:
        try:
            expected = cfg.edge.tokens().get(device_id, "")
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail="edge authentication is misconfigured") from exc
        provided = ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[7:].strip()
        if (
            not cfg.auth.multi_workspace
            and expected
            and provided
            and hmac.compare_digest(expected, provided)
        ):
            return
        repository = edge_commands
        if provided and repository is not None:
            token_hash = hashlib.sha256(provided.encode("utf-8")).hexdigest()
            credential_matches = getattr(repository, "credential_matches", None)
            if required_scope is not None:
                credential_authorizes = getattr(
                    repository, "credential_authorizes", None
                )
                if credential_authorizes and credential_authorizes(
                    device_id, token_hash, required_scope
                ):
                    return
                if credential_matches and credential_matches(device_id, token_hash):
                    raise HTTPException(
                        status_code=403,
                        detail=f"edge credential does not allow {required_scope}",
                    )
            elif credential_matches and credential_matches(device_id, token_hash):
                return
        raise HTTPException(status_code=401, detail="invalid edge device credentials")

    def _require_edge_repository() -> EdgeRepository:
        if edge_commands is None:
            raise HTTPException(status_code=503, detail="edge command routing is not enabled")
        return edge_commands

    shell_asset_version = (build.commit or build.version or "dev")[:16]

    def _version_shell_assets(content: str) -> str:
        for path in NETWORK_FIRST_SHELL_PATHS:
            for attribute in ("href", "src"):
                content = content.replace(
                    f'{attribute}="{path}"',
                    f'{attribute}="{path}?v={shell_asset_version}"',
                )
        return content

    def _app_shell(name: str) -> str:
        content = (
            resources.files("searchable_storage_led")
            .joinpath("static", name)
            .read_text(encoding="utf-8")
        )
        return _version_shell_assets(content)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/cabinet", response_class=HTMLResponse)
    def cabinet_console() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/kiosk", response_class=HTMLResponse)
    def kiosk() -> HTMLResponse:
        public_origin = (
            cfg.public_origin.strip()
            or cfg.zebra.base_url.strip()
            or f"http://127.0.0.1:{cfg.bind_port}"
        ).rstrip("/")
        places_url = f"{public_origin}/storage?view=places"
        qr_svg = render_qr_svg(places_url)
        qr_data_url = (
            "data:image/svg+xml;base64,"
            + base64.b64encode(qr_svg.encode("utf-8")).decode("ascii")
        )
        kiosk_html = (
            _app_shell("kiosk.html")
            .replace("__MAGPIE_STORAGE_HANDOFF_QR__", qr_data_url)
            .replace(
                "__MAGPIE_STORAGE_HANDOFF_URL__",
                html.escape(places_url, quote=True),
            )
        )
        return HTMLResponse(
            content=kiosk_html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/mobile", response_class=HTMLResponse)
    def mobile() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("mobile.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/check-station", response_class=HTMLResponse)
    def check_station() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("check-station.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/app-icon.svg")
    def app_icon() -> Response:
        return _static_asset("app-icon.svg", "image/svg+xml")

    @app.get("/theme.css")
    def theme_stylesheet() -> Response:
        content = (
            resources.files("searchable_storage_led")
            .joinpath("static", "theme.css")
            .read_bytes()
        )
        return Response(
            content=content,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/product-shell.js")
    def product_shell_script() -> Response:
        return _static_asset("product-shell.js", "text/javascript; charset=utf-8")

    @app.get("/auth.js")
    def auth_script() -> Response:
        return _static_asset("auth.js", "text/javascript; charset=utf-8")

    @app.get("/location-display.js")
    def location_display_script() -> Response:
        return _static_asset("location-display.js", "text/javascript; charset=utf-8")

    @app.get("/confirmation.js")
    def confirmation_script() -> Response:
        return _static_asset("confirmation.js", "text/javascript; charset=utf-8")

    @app.get("/photo-utils.js")
    def photo_utils_script() -> Response:
        return _static_asset("photo-utils.js", "text/javascript; charset=utf-8")

    @app.get("/jsqr.min.js")
    def qr_decoder_script() -> Response:
        return _static_asset("jsqr.min.js", "text/javascript; charset=utf-8")

    @app.get("/app-icon-192.png")
    def app_icon_192() -> Response:
        return _static_asset("app-icon-192.png", "image/png")

    @app.get("/app-icon-512.png")
    def app_icon_512() -> Response:
        return _static_asset("app-icon-512.png", "image/png")

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon() -> Response:
        return _static_asset("apple-touch-icon.png", "image/png")

    def _static_asset(name: str, media_type: str) -> Response:
        content = (
            resources.files("searchable_storage_led")
            .joinpath("static", name)
            .read_bytes()
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=604800"},
        )

    @app.get("/manifest.webmanifest")
    def web_manifest() -> dict[str, object]:
        return {
            "name": "Magpie Storage",
            "short_name": "Magpie",
            "description": "Find, put away, label, and review searchable storage.",
            "id": "/",
            "start_url": "/mobile?source=pwa",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui"],
            "background_color": "#f5f6f8",
            "theme_color": "#0f766e",
            "categories": ["productivity", "utilities"],
            "launch_handler": {"client_mode": "navigate-existing"},
            "icons": [
                {
                    "src": "/app-icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/app-icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/app-icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
            "shortcuts": [
                {
                    "name": "Find an item",
                    "short_name": "Find",
                    "url": "/mobile?action=find&source=shortcut",
                    "icons": [{"src": "/app-icon-192.png", "sizes": "192x192"}],
                },
                {
                    "name": "Put an item away",
                    "short_name": "Put away",
                    "url": "/mobile?action=put-away&source=shortcut",
                    "icons": [{"src": "/app-icon-192.png", "sizes": "192x192"}],
                },
                {
                    "name": "Browse inventory",
                    "short_name": "Items",
                    "url": "/storage?view=inventory&source=shortcut",
                    "icons": [{"src": "/app-icon-192.png", "sizes": "192x192"}],
                },
                {
                    "name": "Add a storage place",
                    "short_name": "Add place",
                    "url": "/storage?view=places&create=top&source=shortcut",
                    "icons": [{"src": "/app-icon-192.png", "sizes": "192x192"}],
                },
            ],
        }

    @app.get("/service-worker.js")
    def service_worker() -> Response:
        cache_version = shell_asset_version.replace('"', "")
        app_shell = json.dumps(
            [
                f"{path}?v={shell_asset_version}"
                if path in NETWORK_FIRST_SHELL_PATHS
                else path
                for path in APP_SHELL_PATHS
            ]
        )
        network_first_shell = json.dumps(list(NETWORK_FIRST_SHELL_PATHS))
        script = (
            f'const CACHE_NAME = "magpie-shell-{cache_version}";\n'
            f"const APP_SHELL = {app_shell};\n"
            f"const NETWORK_FIRST_SHELL = new Set({network_first_shell});\n"
        ) + """
const SCAN_CACHE_NAME = "magpie-scan-shells-v2";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => ![CACHE_NAME, SCAN_CACHE_NAME].includes(key)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "CACHE_SCAN_PAGE") {
    const path = String(event.data.path || "");
    if (path.startsWith("/scan/")) {
      event.waitUntil(caches.open(SCAN_CACHE_NAME).then((cache) => cache.add(path)));
    }
  }
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (event.request.mode === "navigate") {
    const cachedPath = url.pathname;
    if (url.pathname.startsWith("/scan/")) {
      event.respondWith(
        fetch(event.request)
          .then((response) => {
            const copy = response.clone();
            caches.open(SCAN_CACHE_NAME).then((cache) => cache.put(cachedPath, copy));
            return response;
          })
          .catch(() => caches.match(cachedPath).then((cached) => cached || caches.match("/")))
      );
      return;
    }
    event.respondWith(
      fetch(event.request, {cache: "no-store"})
        .catch(() => caches.match(cachedPath).then((cached) => cached || caches.match("/")))
    );
    return;
  }
  if (NETWORK_FIRST_SHELL.has(url.pathname)) {
    event.respondWith(
      fetch(event.request, {cache: "no-store"})
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  event.respondWith(
    caches.match(event.request)
      .then((cached) => cached || fetch(event.request))
  );
});
"""
        return Response(
            content=script,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/browse", response_class=RedirectResponse)
    def browse() -> RedirectResponse:
        return RedirectResponse(
            url="/storage?view=places",
            status_code=307,
        )

    @app.get("/storage", response_class=HTMLResponse)
    def storage_manager() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("storage.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/review", response_class=HTMLResponse)
    def review_manager() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("review.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/account", response_class=HTMLResponse)
    def account_manager() -> HTMLResponse:
        return HTMLResponse(
            content=_app_shell("account.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    def _scan_shell(workspace_id: str, target_path: str) -> HTMLResponse:
        workspace = workspace_id.strip()
        if not workspace:
            raise HTTPException(status_code=404, detail="workspace label is invalid")
        if cfg.auth.multi_workspace:
            try:
                workspace = str(UUID(workspace))
            except ValueError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="workspace label is invalid",
                ) from exc
        safe_target = json.dumps(target_path)
        safe_workspace = json.dumps(workspace)
        return HTMLResponse(
            content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f766e">
  <title>Open storage - Magpie</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#f5f6f8; color:#171a1f; font-family:system-ui,-apple-system,sans-serif; }}
    main {{ text-align:center; padding:24px; }}
    strong {{ display:block; margin-bottom:8px; font-size:24px; }}
    span {{ color:#626a75; }}
  </style>
  <script>sessionStorage.setItem("magpie.auth.scan.workspace.v1", {safe_workspace});</script>
  <script src="/auth.js?v={shell_asset_version}"></script>
</head>
<body>
  <main><strong>Magpie</strong><span id="scanStatus">Opening storage</span></main>
  <script>
    (window.magpieAuthReady || Promise.resolve()).then(async (identity) => {{
      if (
        window.magpieAuthConfig?.mode === "supabase"
        && String(identity?.workspace_id || "") !== {safe_workspace}
      ) {{
        sessionStorage.removeItem("magpie.auth.scan.workspace.v1");
        document.getElementById("scanStatus").textContent = "You do not have access to this storage workspace";
        return;
      }}
      const response = await window.fetch({safe_target}, {{cache:"no-store"}});
      if (!response.ok) {{
        const body = await response.json().catch(() => ({{}}));
        sessionStorage.removeItem("magpie.auth.scan.workspace.v1");
        document.getElementById("scanStatus").textContent = body.detail || "Storage label not found";
        return;
      }}
      localStorage.setItem("magpie.auth.workspace.v1", {safe_workspace});
      sessionStorage.removeItem("magpie.auth.scan.workspace.v1");
      const page = await response.text();
      document.open();
      document.write(page);
      document.close();
    }}).catch((error) => {{
      sessionStorage.removeItem("magpie.auth.scan.workspace.v1");
      document.getElementById("scanStatus").textContent = error.message || "Could not open storage";
    }});
  </script>
</body>
</html>""",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/scan/{workspace_id}/l/{location_id}", response_class=HTMLResponse)
    def scan_location_shell(workspace_id: str, location_id: str) -> HTMLResponse:
        return _scan_shell(
            workspace_id,
            f"/l/{quote(location_id, safe='')}",
        )

    @app.get("/scan/{workspace_id}/i/{item_id}", response_class=HTMLResponse)
    def scan_item_shell(workspace_id: str, item_id: int) -> HTMLResponse:
        return _scan_shell(workspace_id, f"/i/{item_id}")

    def _assistant_status_payload() -> dict[str, object]:
        openai_key_present = bool(os.environ.get("OPENAI_API_KEY"))
        ready_llm_endpoint = _llm_ready_endpoint(cfg)
        llm_key_present = ready_llm_endpoint is not None
        has_key = openai_key_present or llm_key_present
        if cfg.assistant_backend == "openai":
            if openai_key_present:
                effective_backend = "openai"
            elif llm_key_present:
                effective_backend = ready_llm_endpoint.provider
            else:
                effective_backend = "openai_unconfigured"
        elif cfg.assistant_backend == "auto":
            if llm_key_present:
                effective_backend = ready_llm_endpoint.provider
            elif openai_key_present:
                effective_backend = "openai"
            else:
                effective_backend = "deterministic"
        else:
            effective_backend = "deterministic"
        reported_openai = effective_backend == "openai"
        reported_endpoint = ready_llm_endpoint or LlmEndpoint(
            cfg.llm.base_url,
            cfg.llm.model,
            cfg.llm.api_key_env,
            cfg.llm.provider,
        )
        provider_failures = (
            agent_llm_client.provider_failures()
            if hasattr(agent_llm_client, "provider_failures")
            else []
        )
        return {
            "configured_backend": cfg.assistant_backend,
            "effective_backend": effective_backend,
            "provider": "openai" if reported_openai else reported_endpoint.provider,
            "model": cfg.openai_model if reported_openai else reported_endpoint.model,
            "base_url": (
                "https://api.openai.com/v1"
                if reported_openai
                else reported_endpoint.base_url
            ),
            "key_env": (
                "OPENAI_API_KEY" if reported_openai else reported_endpoint.api_key_env
            ),
            "openai_model": cfg.openai_model,
            "openai_key_present": openai_key_present,
            "reasoning_effort": "low" if agent_api == "responses" else "",
            "max_steps": cfg.agent_max_steps,
            "fallback_provider": cfg.llm.provider,
            "fallback_model": cfg.llm.model,
            "agent_provider": agent_provider,
            "agent_model": agent_model,
            "agent_api": agent_api,
            "agent_ready": has_key,
            "degraded": bool(provider_failures),
            "provider_failures": provider_failures,
            "ready": effective_backend != "openai_unconfigured",
            "build": build.as_dict(),
            "build_label": build.label(),
        }

    @app.get("/health")
    def health() -> dict[str, object]:
        assistant = _assistant_status_payload()
        storage = repository_bundle.info()
        return {
            "ok": True,
            "build": build.as_dict(),
            "version": build.version,
            "commit": build.commit,
            "build_label": build.label(),
            "pixel_count": cfg.pixel_count,
            "gpio_pin": cfg.gpio_pin,
            "backend": cfg.backend,
            "wled_url": cfg.wled_url,
            "wled_start": cfg.wled_start,
            "color_order": cfg.color_order,
            "layout": layout.name,
            "bin_count": len(layout.bins),
            "agent": {
                "ready": assistant["agent_ready"],
                "provider": assistant["agent_provider"],
                "model": assistant["agent_model"],
                "api": assistant["agent_api"],
                "effective_backend": assistant["effective_backend"],
            },
            "storage": storage,
            "runtime": _runtime_profile(
                cfg,
                storage=storage,
                edge_enabled=edge_commands is not None,
            ),
            "edge": {
                "mode": cfg.edge.mode,
                "target_device_id": cfg.edge.target_device_id,
                "machine_url": cfg.edge.machine_url,
                "routing": (
                    "location_light_target"
                    if cfg.edge.mode == "workspace"
                    else "configured_target"
                    if cfg.edge.mode == "central"
                    else "local"
                ),
                "enabled": edge_commands is not None,
            },
            "auth": {
                "mode": cfg.auth.mode,
                "enabled": human_auth is not None,
                "workspace_id": (
                    "request-scoped"
                    if cfg.auth.multi_workspace
                    else cfg.auth.workspace_id or storage["workspace_id"]
                ),
                "multi_workspace": cfg.auth.multi_workspace,
            },
        }

    @app.get("/ready")
    def ready() -> JSONResponse:
        payload = health()
        ready_state = True
        try:
            storage_probe = repository_bundle.health_probe()
        except Exception as exc:
            ready_state = False
            storage_probe = {
                **repository_bundle.info(),
                "ok": False,
                "error": type(exc).__name__,
            }
        operational_state_probe = _operational_state_probe(llm_state_dir)
        ready_state = ready_state and operational_state_probe["ok"] is True
        agent = payload.get("agent") or {}
        runtime = payload.get("runtime") or {}
        agent_required = runtime.get("role") == "central_service"
        agent_probe = {
            "ok": bool(
                not agent_required
                or (
                    agent.get("ready") is True
                    and agent.get("effective_backend")
                    not in {"deterministic", "openai_unconfigured"}
                )
            ),
            "required": agent_required,
            "provider": str(agent.get("provider") or ""),
            "model": str(agent.get("model") or ""),
        }
        ready_state = ready_state and agent_probe["ok"] is True
        return JSONResponse(
            status_code=200 if ready_state else 503,
            content={
                **payload,
                "ok": ready_state,
                "schema": "magpie.readiness.v2",
                "ready": ready_state,
                "storage_probe": storage_probe,
                "operational_state_probe": operational_state_probe,
                "agent_probe": agent_probe,
            }
        )

    @app.get("/auth/config")
    def auth_config() -> dict[str, object]:
        return {
            "schema": "magpie.auth.config.v1",
            "mode": cfg.auth.mode,
            "multi_workspace": cfg.auth.multi_workspace,
            "supabase_url": cfg.auth.supabase_url if cfg.auth.mode == "supabase" else "",
            "publishable_key": (
                cfg.auth.publishable_key() if cfg.auth.mode == "supabase" else ""
            ),
            "edge_url": cfg.edge.machine_url,
        }

    @app.get("/auth/session")
    def auth_session(request: Request) -> dict[str, object]:
        identity = getattr(request.state, "magpie_identity", None)
        if not isinstance(identity, AuthIdentity):
            raise HTTPException(status_code=401, detail="no authenticated session")
        return {"ok": True, "identity": identity.as_dict()}

    def _request_identity(request: Request) -> AuthIdentity:
        identity = getattr(request.state, "magpie_identity", None)
        if not isinstance(identity, AuthIdentity):
            raise HTTPException(status_code=401, detail="no authenticated session")
        return identity

    def _member_service() -> WorkspaceMemberService:
        if member_router is not None:
            try:
                return member_router.for_workspace(
                    repository_bundle.current_workspace_id()
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if member_service is None:
            raise HTTPException(
                status_code=404,
                detail="workspace membership management is not enabled",
            )
        return member_service

    def _workspace_catalog() -> WorkspaceCatalogService:
        if catalog_service is None:
            raise HTTPException(
                status_code=404,
                detail="workspace catalog is not enabled",
            )
        return catalog_service

    def _invalidate_user_identity(user_id: str) -> None:
        invalidate = getattr(human_auth, "invalidate_user", None)
        if callable(invalidate):
            invalidate(user_id)

    @app.get("/workspace")
    def workspace_detail(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        return {
            "schema": "magpie.workspace.v1",
            "workspace": _member_service().workspace(),
            "identity": identity.as_dict(),
        }

    @app.get("/workspaces")
    def list_workspaces(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        rows = _workspace_catalog().workspaces(identity.user_id)
        return {
            "schema": "magpie.workspaces.v1",
            "workspaces": rows,
            "selected_workspace_id": identity.workspace_id,
        }

    @app.post("/workspaces", status_code=201)
    def create_workspace(
        body: WorkspaceCreateRequest,
        request: Request,
    ) -> dict[str, object]:
        identity = _request_identity(request)
        try:
            result = _workspace_catalog().create(identity.user_id, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_user_identity(identity.user_id)
        return {"ok": True, **result}

    @app.delete("/account")
    def delete_account(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        if identity.workspace_id:
            raise HTTPException(
                status_code=400,
                detail="leave or delete every workspace before deleting your account",
            )
        try:
            account = _workspace_catalog().delete_account(identity.user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_user_identity(identity.user_id)
        return {"ok": True, "account": account}

    @app.delete("/workspace")
    def delete_workspace(
        body: WorkspaceDeleteRequest,
        request: Request,
    ) -> dict[str, object]:
        identity = _request_identity(request)
        if identity.role != "owner":
            raise HTTPException(
                status_code=403,
                detail="only the workspace owner can delete this workspace",
            )
        try:
            result = _workspace_catalog().delete(
                identity.user_id,
                identity.workspace_id,
                body.confirmation,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for user_id in result.get("member_user_ids", []):
            _invalidate_user_identity(str(user_id))
        return {"ok": True, **result}

    @app.patch("/workspace")
    def rename_workspace(
        body: WorkspaceRenameRequest,
        request: Request,
    ) -> dict[str, object]:
        identity = _request_identity(request)
        if identity.role != "owner":
            raise HTTPException(
                status_code=403,
                detail="only the workspace owner can rename this workspace",
            )
        try:
            workspace = _workspace_catalog().rename(
                identity.user_id,
                identity.workspace_id,
                body.name,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "workspace": workspace}

    @app.delete("/workspace/membership")
    def leave_workspace(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        if identity.role == "owner":
            raise HTTPException(
                status_code=400,
                detail="workspace owners must delete the workspace instead of leaving it",
            )
        try:
            member = _member_service().remove(identity.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_user_identity(identity.user_id)
        return {"ok": True, "member": member}

    @app.get("/workspace/members")
    def workspace_members(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        return {
            "schema": "magpie.workspace_members.v1",
            "workspace_id": identity.workspace_id,
            "members": _member_service().members(),
            "can_manage": can_manage_members(identity),
        }

    @app.post("/workspace/invitations")
    def invite_workspace_member(
        body: WorkspaceInviteRequest,
        request: Request,
    ) -> dict[str, object]:
        identity = _request_identity(request)
        if not can_manage_members(identity, requested_role=body.role):
            raise HTTPException(
                status_code=403,
                detail="this workspace role cannot invite that member role",
            )
        try:
            member = _member_service().invite(
                body.email,
                body.role,
                redirect_url=body.redirect_url,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_user_identity(str(member.get("user_id") or ""))
        return {"ok": True, "member": member}

    @app.patch("/workspace/members/{user_id}")
    def update_workspace_member(
        user_id: str,
        body: WorkspaceRoleRequest,
        request: Request,
    ) -> dict[str, object]:
        identity = _request_identity(request)
        if user_id == identity.user_id:
            raise HTTPException(status_code=400, detail="you cannot change your own role")
        try:
            current_member = _member_service().member(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if identity.role == "admin" and current_member.get("role") == "admin":
            raise HTTPException(
                status_code=403,
                detail="only the workspace owner can manage admins",
            )
        if not can_manage_members(identity, requested_role=body.role):
            raise HTTPException(
                status_code=403,
                detail="this workspace role cannot assign that member role",
            )
        try:
            member = _member_service().set_role(user_id, body.role)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_user_identity(user_id)
        return {"ok": True, "member": member}

    @app.delete("/workspace/members/{user_id}")
    def remove_workspace_member(user_id: str, request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        if user_id == identity.user_id:
            raise HTTPException(status_code=400, detail="you cannot remove yourself")
        try:
            current_member = _member_service().member(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if identity.role == "admin" and current_member.get("role") == "admin":
            raise HTTPException(
                status_code=403,
                detail="only the workspace owner can manage admins",
            )
        if not can_manage_members(identity):
            raise HTTPException(
                status_code=403,
                detail="this workspace role cannot remove members",
            )
        try:
            member = _member_service().remove(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _invalidate_user_identity(user_id)
        return {"ok": True, "member": member}

    def _require_agent_manager(request: Request) -> AuthIdentity:
        identity = _request_identity(request)
        if not can_manage_members(identity):
            raise HTTPException(
                status_code=403,
                detail="this workspace role cannot manage agent connections",
            )
        if agent_access is None:
            raise HTTPException(
                status_code=503,
                detail="agent connections are not configured",
            )
        return identity

    @app.get("/workspace/agent-credentials")
    def workspace_agent_credentials(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        credentials = agent_access.list(identity.workspace_id) if agent_access else []
        return {
            "schema": "magpie.agent_credentials.v1",
            "workspace_id": identity.workspace_id,
            "mcp_url": f"{cfg.public_origin.rstrip('/')}/mcp/",
            "can_manage": can_manage_members(identity) and agent_access is not None,
            "credentials": credentials,
        }

    @app.post("/workspace/agent-credentials")
    def create_workspace_agent_credential(
        body: AgentCredentialCreateRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        identity = _require_agent_manager(request)
        token = f"magpie_mcp_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            credential = agent_access.issue(
                identity.workspace_id,
                identity.user_id,
                body.name,
                token_hash,
                token[-6:],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "ok": True,
            "credential": credential,
            "mcp_url": f"{cfg.public_origin.rstrip('/')}/mcp/",
            "token": token,
            "shown_once": True,
        }

    @app.delete("/workspace/agent-credentials/{credential_id}")
    def revoke_workspace_agent_credential(
        credential_id: UUID,
        request: Request,
    ) -> dict[str, object]:
        identity = _require_agent_manager(request)
        credential = agent_access.revoke(identity.workspace_id, str(credential_id))
        if credential is None:
            raise HTTPException(status_code=404, detail="unknown active agent connection")
        return {"ok": True, "credential": credential}

    def _edge_device_id(value: str) -> str:
        clean = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", clean):
            raise HTTPException(
                status_code=400,
                detail="device id must use 1-63 lowercase letters, numbers, or hyphens",
            )
        return clean

    def _edge_environment_devices() -> set[str]:
        if cfg.auth.multi_workspace:
            return set()
        try:
            return set(cfg.edge.tokens())
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="edge authentication is misconfigured",
            ) from exc

    def _runtime_capabilities_by_device() -> dict[str, set[str]]:
        now = time.time()
        capabilities: dict[str, set[str]] = {}
        for state, max_age, capability in (
            (_voice_state(), 30.0, "voice_assistant"),
            (_screen_state(), 45.0, "kiosk_display"),
        ):
            device_id = str(state.get("device_id") or "").strip().lower()
            updated_at = float(state.get("updated_at") or 0.0)
            if (
                device_id
                and updated_at > 0
                and max(0.0, now - updated_at) <= max_age
            ):
                capabilities.setdefault(device_id, set()).add(capability)
        return capabilities

    def _workspace_device_status() -> dict[str, object]:
        status = _require_edge_repository().status()
        static_devices = _edge_environment_devices()
        runtime_capabilities = _runtime_capabilities_by_device()
        devices_by_id = {
            str(device.get("device_id") or ""): dict(device)
            for device in status.get("devices", [])
            if isinstance(device, dict) and device.get("device_id")
        }
        for device_id in static_devices:
            devices_by_id.setdefault(
                device_id,
                {
                    "device_id": device_id,
                    "version": "",
                    "capabilities": [],
                    "credential_scopes": sorted(EDGE_CREDENTIAL_SCOPES),
                    "last_seen_at": None,
                    "enrolled": False,
                },
            )
        bound_by_device: dict[str, list[dict[str, str]]] = {}
        for location in locations.all():
            device_id = str(location.light_target or "").strip()
            if not device_id or not location.light_indexes:
                continue
            bound_by_device.setdefault(device_id, []).append(
                {"id": location.id, "label": location.label or location.id}
            )
            devices_by_id.setdefault(
                device_id,
                {
                    "device_id": device_id,
                    "version": "",
                    "capabilities": [],
                    "credential_scopes": [],
                    "last_seen_at": None,
                    "enrolled": False,
                },
            )
        devices = []
        for device_id, device in sorted(devices_by_id.items()):
            reported = sorted(
                {
                    str(capability)
                    for capability in device.get("capabilities", [])
                }
            )
            runtime = sorted(runtime_capabilities.get(device_id, set()))
            device["reported_capabilities"] = reported
            device["runtime_capabilities"] = runtime
            device["capabilities"] = sorted({*reported, *runtime})
            device["environment_fallback"] = device_id in static_devices
            device["bound_locations"] = bound_by_device.get(device_id, [])
            device.update(_edge_device_connection(device))
            devices.append(device)
        return {**status, "devices": devices}

    def _require_device_manager(request: Request) -> AuthIdentity:
        identity = _request_identity(request)
        if not can_manage_members(identity):
            raise HTTPException(
                status_code=403,
                detail="this workspace role cannot manage edge devices",
            )
        return identity

    def _issue_edge_credential(
        device_id: str,
        scopes: list[str] | None = None,
    ) -> dict[str, object]:
        try:
            credential_scopes = normalize_credential_scopes(scopes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        hint = token[-6:]
        device = _require_edge_repository().set_credential(
            device_id,
            token_hash=token_hash,
            token_hint=hint,
            scopes=credential_scopes,
        )
        return {
            "ok": True,
            "device": {
                **device,
                "environment_fallback": device_id in _edge_environment_devices(),
            },
            "token": token,
            "shown_once": True,
        }

    def _issue_edge_pairing(
        device_id: str,
        scopes: list[str],
        *,
        ttl_seconds: float = 600,
    ) -> dict[str, object]:
        try:
            credential_scopes = normalize_credential_scopes(scopes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        pairing_code = secrets.token_urlsafe(24)
        pairing_hash = hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()
        expires_at = time.time() + ttl_seconds
        device = _require_edge_repository().set_pairing(
            device_id,
            pairing_hash=pairing_hash,
            pairing_hint=pairing_code[-6:],
            scopes=credential_scopes,
            expires_at=expires_at,
        )
        return {
            "ok": True,
            "device": {
                **device,
                "environment_fallback": device_id in _edge_environment_devices(),
            },
            "pairing_code": pairing_code,
            "expires_at": expires_at,
            "expires_in_seconds": int(ttl_seconds),
            "shown_once": True,
        }

    def _edge_pairing_status() -> dict[str, object]:
        machine_url = cfg.edge.machine_url.strip()
        requires_machine_origin = bool(cfg.auth.multi_workspace)
        available = bool(machine_url) or not requires_machine_origin
        return {
            "pairing_available": available,
            "pairing_url": machine_url,
            "pairing_message": (
                ""
                if available
                else "Device pairing requires a configured machine-only edge origin."
            ),
        }

    def _require_edge_pairing_origin() -> None:
        status = _edge_pairing_status()
        if not status["pairing_available"]:
            raise HTTPException(status_code=503, detail=status["pairing_message"])

    @app.get("/workspace/devices")
    def workspace_devices(request: Request) -> dict[str, object]:
        identity = _request_identity(request)
        return {
            "schema": "magpie.workspace_devices.v1",
            "workspace_id": identity.workspace_id,
            "can_manage": can_manage_members(identity),
            **_edge_pairing_status(),
            **_workspace_device_status(),
        }

    @app.post("/workspace/devices")
    def enroll_workspace_device(
        body: EdgeDeviceEnrollRequest,
        request: Request,
    ) -> dict[str, object]:
        _require_device_manager(request)
        device_id = _edge_device_id(body.device_id)
        existing = next(
            (
                device
                for device in _workspace_device_status()["devices"]
                if isinstance(device, dict) and device.get("device_id") == device_id
            ),
            None,
        )
        if existing and existing.get("enrolled"):
            raise HTTPException(
                status_code=409,
                detail="device is already enrolled; rotate its token instead",
            )
        return _issue_edge_credential(device_id, body.scopes)

    @app.post("/workspace/devices/pairing")
    def create_workspace_device_pairing(
        body: EdgeDeviceEnrollRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        identity = _require_device_manager(request)
        _require_edge_pairing_origin()
        device_id = _edge_device_id(body.device_id)
        response.headers["Cache-Control"] = "no-store"
        return {
            **_issue_edge_pairing(device_id, body.scopes),
            "workspace_id": identity.workspace_id,
            "machine_url": cfg.edge.machine_url,
        }

    @app.post("/workspace/devices/{device_id}/rotate")
    def rotate_workspace_device(device_id: str, request: Request) -> dict[str, object]:
        _require_device_manager(request)
        clean_id = _edge_device_id(device_id)
        existing = next(
            (
                device
                for device in _workspace_device_status()["devices"]
                if isinstance(device, dict) and device.get("device_id") == clean_id
            ),
            None,
        )
        if not existing or not existing.get("enrolled"):
            raise HTTPException(status_code=404, detail="unknown enrolled edge device")
        return _issue_edge_credential(
            clean_id,
            [str(scope) for scope in existing.get("credential_scopes", [])],
        )

    @app.post("/workspace/devices/{device_id}/pairing")
    def replace_workspace_device_pairing(
        device_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        identity = _require_device_manager(request)
        _require_edge_pairing_origin()
        clean_id = _edge_device_id(device_id)
        existing = next(
            (
                device
                for device in _workspace_device_status()["devices"]
                if isinstance(device, dict) and device.get("device_id") == clean_id
            ),
            None,
        )
        if not existing or not existing.get("enrolled"):
            raise HTTPException(status_code=404, detail="unknown enrolled edge device")
        response.headers["Cache-Control"] = "no-store"
        return {
            **_issue_edge_pairing(
                clean_id,
                [str(scope) for scope in existing.get("credential_scopes", [])],
            ),
            "workspace_id": identity.workspace_id,
            "machine_url": cfg.edge.machine_url,
        }

    @app.delete("/workspace/devices/{device_id}")
    def revoke_workspace_device(device_id: str, request: Request) -> dict[str, object]:
        _require_device_manager(request)
        clean_id = _edge_device_id(device_id)
        device = _require_edge_repository().revoke_credential(clean_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown edge device")
        fallback = clean_id in _edge_environment_devices()
        return {
            "ok": True,
            "device": {**device, "environment_fallback": fallback},
            "warning": (
                "The static environment token remains active until it is removed from the service configuration."
                if fallback
                else ""
            ),
        }

    @app.get("/edge/status")
    def edge_status() -> dict[str, object]:
        repository = _require_edge_repository()
        return {
            "schema": "magpie.edge.status.v1",
            "mode": cfg.edge.mode,
            "target_device_id": cfg.edge.target_device_id,
            **repository.status(),
        }

    @app.get("/edge/devices/{device_id}/status")
    def edge_device_status(
        device_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        clean_id = _edge_device_id(device_id)
        _authorize_edge(clean_id, authorization)
        device = next(
            (
                candidate
                for candidate in _workspace_device_status()["devices"]
                if isinstance(candidate, dict)
                and candidate.get("device_id") == clean_id
            ),
            None,
        )
        if device is None:
            raise HTTPException(status_code=404, detail="unknown edge device")
        return {
            "ok": True,
            "schema": "magpie.edge.device_status.v1",
            "device": device,
        }

    @app.post("/edge/devices/{device_id}/pair")
    def pair_edge_device(
        device_id: str,
        body: EdgeDevicePairRequest,
        response: Response,
    ) -> dict[str, object]:
        clean_id = _edge_device_id(device_id)
        pairing_hash = hashlib.sha256(
            body.pairing_code.strip().encode("utf-8")
        ).hexdigest()
        response.headers["Cache-Control"] = "no-store"
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        device = _require_edge_repository().consume_pairing(
            clean_id,
            pairing_hash=pairing_hash,
            token_hash=token_hash,
            token_hint=token[-6:],
        )
        if device is None:
            raise HTTPException(status_code=401, detail="invalid or expired pairing code")
        return {
            "ok": True,
            "device": device,
            "token": token,
            "shown_once": True,
        }

    @app.post("/edge/devices/{device_id}/heartbeat")
    def edge_heartbeat(
        device_id: str,
        request: EdgeHeartbeatRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_edge(device_id, authorization)
        device = _require_edge_repository().heartbeat(
            device_id,
            version=request.version.strip(),
            capabilities=sorted(set(request.capabilities)),
        )
        return {"ok": True, "device": device}

    @app.post("/edge/devices/{device_id}/commands/claim")
    async def claim_edge_command(
        device_id: str,
        response: Response,
        wait_seconds: float = 0,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_edge(device_id, authorization, required_scope="commands")
        response.headers["Cache-Control"] = "no-store"
        clean_wait = max(0.0, min(float(wait_seconds), 25.0))
        repository = _require_edge_repository()

        def claim():
            return repository.claim(
                device_id,
                claim_timeout=cfg.edge.claim_timeout_seconds,
                max_attempts=cfg.edge.max_attempts,
            )

        observed_generation = edge_command_generation
        command = await asyncio.to_thread(claim)
        if command is None and clean_wait > 0:
            loop = asyncio.get_running_loop()
            waiter = asyncio.Event()
            waiter_key = (loop, waiter)
            timed_out = False
            with edge_command_waiters_lock:
                edge_command_waiters.add(waiter_key)
            try:
                if edge_command_generation == observed_generation:
                    try:
                        await asyncio.wait_for(waiter.wait(), timeout=clean_wait)
                    except TimeoutError:
                        timed_out = True
                command = await asyncio.to_thread(claim)
            finally:
                with edge_command_waiters_lock:
                    edge_command_waiters.discard(waiter_key)
            if command is None and timed_out:
                return {"ok": True, "command": None, "timed_out": True}
        return {
            "ok": True,
            "command": command.as_dict() if command is not None else None,
            "timed_out": False,
        }

    @app.post("/edge/devices/{device_id}/commands/{command_id}/complete")
    def complete_edge_command(
        device_id: str,
        command_id: str,
        request: EdgeCompletionRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        _authorize_edge(device_id, authorization, required_scope="commands")
        command = _require_edge_repository().complete(
            device_id,
            command_id,
            succeeded=request.succeeded,
            error=request.error,
            retryable=request.retryable,
            max_attempts=cfg.edge.max_attempts,
        )
        if command is None:
            raise HTTPException(status_code=404, detail="claimed edge command not found")
        return {"ok": True, "command": command.as_dict()}

    @app.get("/build")
    def build_status() -> dict[str, object]:
        return build.as_dict() | {"label": build.label()}

    def _capability_payload() -> dict[str, object]:
        """Machine-readable discovery for web, mobile, and Pi clients."""
        has_lights = cfg.backend != "none" or cfg.edge.mode in {"central", "workspace"}
        return {
            "schema": "magpie.capabilities.v1",
            "build": build.as_dict() | {"label": build.label()},
            "storage": repository_bundle.info(),
            "interaction": interaction_contract(),
            "features": {
                "agent_chat": True,
                "agent_responses_api": True,
                "agent_conversation_history": True,
                "agent_confirm": True,
                "agent_undo": True,
                "agent_location_inspection": True,
                "agent_storage_roots": True,
                "agent_label_batch_preview": True,
                "agent_inventory_review": True,
                "find_not_here_correction": True,
                "agent_attach_photo_undo": True,
                "agent_bulk_reorganization": True,
                "agent_remove_item": True,
                "agent_remove_item_undo": True,
                "agent_update_item_undo": True,
                "agent_update_item_size_undo": True,
                "agent_update_item_home_undo": True,
                "workspace_edge_routing": cfg.edge.mode == "workspace",
                "agent_update_location_undo": True,
                "agent_update_rule_undo": True,
                "agent_update_size_rule_undo": True,
                "learned_rules": True,
                "learned_aliases": True,
                "learned_rule_conflicts": True,
                "item_homes": True,
                "manual_item_home_updates": True,
                "item_metadata_updates": True,
                "learning_examples_review": True,
                "learning_examples_summary": True,
                "learning_examples_approve": True,
                "learning_examples_bulk_approve": True,
                "learning_examples_reject": True,
                "learning_examples_promote": True,
                "learning_rule_adherence": True,
                "learning_undo": True,
                "size_rules": True,
                "item_size_explain": True,
                "item_qr_scan": True,
                "item_label_pdfs": True,
                "item_label_batch_pdfs": True,
                "location_label_batch_pdfs": True,
                "item_history": True,
                "location_history": True,
                "natural_size_corrections": True,
                "photos": True,
                "photo_upload": True,
                "browser_photo_normalization": True,
                "photo_attachment_retry": True,
                "calibrated_photo_measurement": True,
                "check_station": True,
                "measured_size_provenance": True,
                "photo_sort_review_flags": True,
                "qr_scan": True,
                "qr_scan_local_fallback": True,
                "qr_scan_page_cache": True,
                "scan_quick_put": True,
                "scan_quick_put_retry_queue": True,
                "scan_quick_put_photo_payload_queue": True,
                "scan_quick_put_workspace_queue": True,
                "scan_quick_put_idempotency": True,
                "scan_quick_put_inline_edit": True,
                "scan_quick_put_interaction": True,
                "manual_inventory_interactions": True,
                "inventory_catalog_pagination": True,
                "inventory_move_preview": True,
                "inventory_lifecycle_preview": True,
                "inventory_lifecycle_bulk": True,
                "container_move": True,
                "location_archiving": True,
                "atomic_location_creation": True,
                "atomic_location_lifecycle": True,
                "archive_group_provenance": True,
                "guided_location_audits": True,
                "location_content_revisions": True,
                "printable_labels": True,
                "printable_label_pdfs": True,
                "zpl_labels": True,
                "inventory_json_export": True,
                "inventory_csv_export": True,
                "inventory_pdf_export": True,
                "inventory_json_import": True,
                "inventory_csv_import": True,
                "atomic_inventory_import": True,
                "supabase_schema": True,
                "storage_repository_contract": True,
                "runtime_readiness": True,
                "hardware_bindings": True,
                "hardware_binding_commissioning": True,
                "atomic_hardware_bindings": True,
                "hardware_binding_validation": True,
                "hardware_diagnostics": True,
                "edge_command_queue": edge_commands is not None,
                "edge_long_poll": edge_commands is not None,
                "edge_voice_bridge": edge_commands is not None,
                "edge_local_capability_discovery": True,
                "human_auth": cfg.auth.mode == "supabase",
                "workspace_authorization": cfg.auth.mode == "supabase",
                "multi_workspace": cfg.auth.multi_workspace,
                "workspace_self_service": cfg.auth.multi_workspace,
                "workspace_readiness": True,
                "account_deletion": cfg.auth.multi_workspace,
                "workspace_rename": cfg.auth.multi_workspace,
                "workspace_leave": cfg.auth.multi_workspace,
                "workspace_deletion": cfg.auth.multi_workspace,
                "client_bootstrap": True,
                "mcp_server": True,
                "workspace_agent_credentials": agent_access is not None,
                "pwa_manifest": True,
                "service_worker_shell_cache": True,
                "voice_attempt_log": True,
                "voice_interaction_link": True,
                "voice_tuning_summary": True,
                "voice_acceptance": True,
                "voice_threshold_calibration": True,
                "voice_activity_detection": True,
                "voice_local_wake": True,
                "voice_live_status": True,
                "screen_heartbeat": True,
                "kiosk_display": True,
                "kiosk_touch_actions": True,
                "mobile_client": True,
                "canonical_ask_surface": True,
                "cabinet_console": True,
                "mobile_offline_mutation_queue": True,
                "voice_putaway_ui_handoff": True,
                "assistant_chat_idempotency": True,
                "agent_usage_metering": True,
                "storage_usage_metering": True,
                "lights": has_lights,
                "wled": cfg.backend == "wled",
            },
            "limits": {
                "light_bin_limit": LIGHT_BIN_LIMIT,
                "default_light_seconds": DEFAULT_LIGHT_SECONDS,
                "voice_attempt_limit_max": 100,
                "edge_claim_wait_seconds_max": 25,
                "photo_upload_bytes_max": 10 * 1024 * 1024,
                "browser_photo_edge_pixels_max": 2048,
                "offline_photo_queue_bytes_max": 18 * 1024 * 1024,
                "location_audit_placements_max": 100,
            },
            "endpoints": {
                "app": "/",
                "cabinet_console": "/cabinet",
                "health": "/health",
                "readiness": "/ready",
                "build": "/build",
                "assistant_chat": "/assistant/chat",
                "agent_usage": "/usage/agent",
                "storage_usage": "/usage/storage",
                "mcp": "/mcp/",
                "workspace_agent_credentials": "/workspace/agent-credentials",
                "assistant_followup": "/assistant/followup",
                "assistant_find_not_here": "/assistant/find/not-here",
                "assistant_handoff": "/assistant/handoffs/{interaction_id}",
                "assistant_confirm": "/assistant/confirm",
                "assistant_undo": "/assistant/undo",
                "assistant_learning_examples": "/assistant/learning/examples",
                "assistant_learning_summary": "/assistant/learning/summary",
                "approve_learning_example": "/assistant/learning/examples/{example_id}/approve",
                "bulk_approve_learning_examples": "/assistant/learning/examples/bulk-approve",
                "reject_learning_example": "/assistant/learning/examples/{example_id}",
                "promote_learning_example": "/assistant/learning/examples/{example_id}/promote",
                "learning_rule_adherence": "/assistant/learning/adherence",
                "placement_size": "/placement/size",
                "photos": "/photos",
                "photo_attachment_retry": "/photos/attach-retry",
                "photo_measurement": "/measurement/photo",
                "measurement_grid_pdf": "/measurement/calibration-grid.pdf",
                "check_station": "/check-station",
                "locations": "/locations",
                "create_location_structure": "/locations/structure",
                "create_location_children_batch": (
                    "/locations/{location_id}/children/batch"
                ),
                "location_contents": "/locations/{location_id}/contents",
                "location_audit": "/locations/{location_id}/audit",
                "verify_location_contents": "/locations/{location_id}/verify",
                "inventory_catalog": "/inventory/catalog",
                "preview_inventory_move": "/inventory/move-preview",
                "preview_inventory_lifecycle": "/inventory/lifecycle/preview",
                "apply_inventory_lifecycle": "/inventory/lifecycle/apply",
                "move_location": "/locations/{location_id}/move",
                "preview_location_move": "/locations/{location_id}/move-preview",
                "archive_location": "/locations/{location_id}/archive",
                "restore_location": "/locations/{location_id}/restore",
                "location_hardware_bindings": "/locations/{location_id}/hardware-bindings",
                "preview_hardware_commissioning": (
                    "/locations/{location_id}/hardware-bindings/batch-preview"
                ),
                "apply_hardware_commissioning": (
                    "/locations/{location_id}/hardware-bindings/batch"
                ),
                "hardware_diagnostics": "/hardware/diagnostics",
                "edge_status": "/edge/status",
                "edge_device_status": "/edge/devices/{device_id}/status",
                "edge_local_capabilities": "/edge/local-capabilities",
                "auth_config": "/auth/config",
                "auth_session": "/auth/session",
                "client_bootstrap": "/client/bootstrap",
                "delete_account": "/account",
                "workspaces": "/workspaces",
                "workspace": "/workspace",
                "rename_workspace": "/workspace",
                "leave_workspace": "/workspace/membership",
                "delete_workspace": "/workspace",
                "workspace_location_scan": "/scan/{workspace_id}/l/{location_id}",
                "workspace_item_scan": "/scan/{workspace_id}/i/{item_id}",
                "location_scan": "/l/{location_id}",
                "item_scan": "/i/{item_id}",
                "item_label_pdf": "/items/{item_id}/label.pdf",
                "item_label_batch_pdf": "/items/labels.pdf",
                "location_label_batch_pdf": "/locations/labels.pdf",
                "location_qr_svg": "/locations/{location_id}/qr.svg",
                "location_label_pdf": "/locations/{location_id}/label.pdf",
                "item_history": "/items/{item_id}/history",
                "update_item_home": "/inventory/{item_id}/home",
                "inventory_review": "/inventory/review",
                "mark_inventory_reviewed": "/inventory/reviewed",
                "location_history": "/locations/{location_id}/history",
                "scan_quick_put": "/locations/{location_id}/quick-put",
                "label_sheet": "/locations/{location_id}/labels.html",
                "label_sheet_pdf": "/locations/{location_id}/labels.pdf",
                "export_inventory_json": "/export/inventory.json",
                "export_inventory_csv": "/export/inventory.csv",
                "export_inventory_pdf": "/export/inventory.pdf",
                "import_inventory_json": "/import/inventory.json",
                "import_inventory_csv": "/import/inventory.csv",
                "manifest": "/manifest.webmanifest",
                "service_worker": "/service-worker.js",
                "qr_decoder": "/jsqr.min.js",
                "voice_recent": "/voice/recent",
                "voice_tuning": "/voice/tuning",
                "voice_acceptance": "/voice/acceptance",
                "voice_calibration": "/voice/calibration",
                "voice_status": "/voice/status",
                "voice_state": "/voice/state",
                "kiosk_state": "/kiosk/state",
                "kiosk_display": "/kiosk",
                "kiosk_confirm": "/kiosk/actions/confirm",
                "kiosk_revise": "/kiosk/actions/revise",
                "kiosk_cancel": "/kiosk/actions/cancel",
                "kiosk_undo": "/kiosk/actions/undo",
                "kiosk_clear": "/kiosk/actions/clear",
                "mobile_client": "/mobile",
                "screen_heartbeat": "/screen/heartbeat",
                "screen_status": "/screen/status",
                "demo_readiness": "/demo/readiness",
                "workspace_readiness": "/workspace/readiness",
                "product_readiness": "/product/readiness",
            },
            "compatibility_endpoints": {
                "assistant_ask": {
                    "path": "/assistant/ask",
                    "replacement": "/assistant/chat",
                    "mode": "ask",
                },
                "assistant_find": {
                    "path": "/assistant/find",
                    "replacement": "/assistant/chat",
                    "mode": "find",
                },
                "assistant_agent": {
                    "path": "/assistant/agent",
                    "replacement": "/assistant/chat",
                    "mode": "agent",
                },
                "assistant_command": {
                    "path": "/assistant/command",
                    "replacement": "/assistant/chat",
                    "mode": "command",
                },
            },
        }

    @app.get("/capabilities")
    def capabilities() -> dict[str, object]:
        return _capability_payload()

    @app.get("/client/bootstrap")
    def client_bootstrap(request: Request) -> dict[str, object]:
        """Return the shared startup contract for web and native clients."""
        identity = getattr(request.state, "magpie_identity", None)
        identity_payload = (
            identity.as_dict() if isinstance(identity, AuthIdentity) else None
        )
        workspace = None
        workspaces: list[dict[str, object]] = []

        if isinstance(identity, AuthIdentity):
            if cfg.auth.multi_workspace:
                workspaces = _workspace_catalog().workspaces(identity.user_id)
                workspace = next(
                    (
                        row
                        for row in workspaces
                        if str(row.get("id") or "") == identity.workspace_id
                    ),
                    None,
                )
            elif identity.workspace_id:
                workspace = _member_service().workspace()
                workspaces = [workspace | {"role": identity.role}]

        return {
            "schema": "magpie.client_bootstrap.v1",
            "auth": {
                "mode": cfg.auth.mode,
                "multi_workspace": cfg.auth.multi_workspace,
            },
            "identity": identity_payload,
            "workspace": workspace,
            "workspaces": workspaces,
            "capabilities": _capability_payload(),
        }

    def _screen_status_payload() -> dict[str, object]:
        screen_state = _screen_state()
        updated_at = float(screen_state.get("updated_at") or 0.0)
        age_seconds = max(0.0, time.time() - updated_at) if updated_at else None
        active = age_seconds is not None and age_seconds <= 45
        return {
            "ok": True,
            "screen": {
                "screen_id": str(screen_state.get("screen_id") or ""),
                "device_id": str(screen_state.get("device_id") or ""),
                "url": str(screen_state.get("url") or ""),
                "build_label": str(screen_state.get("build_label") or ""),
                "last_seen_interaction_id": int(screen_state.get("last_seen_interaction_id") or 0),
                "status": str(screen_state.get("status") or "unknown"),
                "updated_at": updated_at or None,
                "age_seconds": age_seconds,
                "active": active,
            },
        }

    @app.post("/screen/heartbeat")
    def screen_heartbeat(
        body: ScreenHeartbeatRequest,
        request: Request,
    ) -> dict[str, object]:
        screen_state = _screen_state()
        previous_interaction_id = int(
            screen_state.get("last_seen_interaction_id") or 0
        )
        authenticated_device_id = str(
            getattr(request.state, "magpie_device_id", "") or ""
        ).strip()
        update: dict[str, object] = {
            "screen_id": body.screen_id.strip() or "kiosk",
            "url": body.url.strip(),
            "build_label": body.build_label.strip(),
            "last_seen_interaction_id": max(
                0, int(body.last_seen_interaction_id or 0)
            ),
            "status": body.status.strip() or "active",
            "updated_at": time.time(),
        }
        if authenticated_device_id:
            update["device_id"] = authenticated_device_id
        elif cfg.auth.mode != "none":
            update["device_id"] = ""
        screen_state.update(update)
        observed_interaction_id = int(update["last_seen_interaction_id"])
        if (
            observed_interaction_id > 0
            and observed_interaction_id != previous_interaction_id
        ):
            store.update_assistant_interaction_response(
                observed_interaction_id,
                {
                    "screen_observed": {
                        "screen_id": str(update["screen_id"]),
                        "device_id": str(update.get("device_id") or ""),
                        "build_label": str(update["build_label"]),
                        "observed_at": float(update["updated_at"]),
                    }
                },
            )
        return _screen_status_payload()

    @app.get("/screen/status")
    def screen_status() -> dict[str, object]:
        return _screen_status_payload()

    def _voice_status_payload() -> dict[str, object]:
        voice_state = _voice_state()
        updated_at = float(voice_state.get("updated_at") or 0.0)
        age_seconds = max(0.0, time.time() - updated_at) if updated_at else None
        last_result = dict(_voice_last_result())
        last_result_updated_at = float(last_result.get("updated_at") or 0.0)
        if last_result_updated_at:
            last_result["age_seconds"] = max(
                0.0,
                time.time() - last_result_updated_at,
            )
        return {
            "ok": True,
            "voice": {
                "state": str(voice_state.get("state") or "unknown"),
                "message": str(voice_state.get("message") or ""),
                "transcript": str(voice_state.get("transcript") or ""),
                "command": str(voice_state.get("command") or ""),
                "device_id": str(voice_state.get("device_id") or ""),
                "runtime_version": str(voice_state.get("runtime_version") or ""),
                "capabilities": dict(voice_state.get("capabilities") or {}),
                "updated_at": updated_at or None,
                "age_seconds": age_seconds,
                "active": age_seconds is not None and age_seconds <= 30,
                "last_result": last_result or None,
                "active_finds": _active_find_snapshot(),
            },
        }

    @app.post("/voice/state")
    def update_voice_state(
        body: VoiceStateRequest,
        request: Request,
    ) -> dict[str, object]:
        state = body.state.strip().lower()
        if state not in {"starting", "waiting", "listening", "processing", "heard", "done", "error"}:
            raise HTTPException(status_code=400, detail="invalid voice state")
        voice_state = _voice_state()
        update: dict[str, object] = {
            "state": state,
            "message": body.message.strip(),
            "transcript": body.transcript.strip(),
            "command": body.command.strip(),
            "updated_at": time.time(),
        }
        authenticated_device_id = str(
            getattr(request.state, "magpie_device_id", "") or ""
        ).strip()
        if authenticated_device_id:
            update["device_id"] = authenticated_device_id[:120]
        elif cfg.auth.mode == "none" and body.device_id.strip():
            update["device_id"] = body.device_id.strip()[:120]
        elif cfg.auth.mode != "none":
            update["device_id"] = ""
        if body.runtime_version.strip():
            update["runtime_version"] = body.runtime_version.strip()[:240]
        if body.capabilities:
            allowed_capabilities = {
                "wake_gate",
                "audio_before_wake_uploaded",
                "no_wake_follow_up",
                "metrics_since",
                "speech_clips",
                "passive_speech_ignored",
                "wake_detections",
                "cloud_stt_calls",
                "local_stt_calls",
                "assistant_requests",
                "agent_requests",
                "cloud_tts_calls",
                "local_tts_calls",
                "cloud_audio_available",
                "cloud_audio_failure",
                "cloud_audio_retry_seconds",
                "local_stt_enabled",
                "local_stt_available",
                "local_tts_enabled",
                "local_tts_available",
                "stt_model",
                "stt_fallback_model",
                "output_device",
            }
            update["capabilities"] = {
                key: value
                for key, value in body.capabilities.items()
                if key in allowed_capabilities
                and isinstance(value, (bool, int, float, str))
            }
        voice_state.update(update)
        if state in {"done", "error"}:
            last_result = _voice_last_result()
            last_result.clear()
            last_result.update(voice_state)
        return _voice_status_payload()

    @app.get("/voice/status")
    def voice_status() -> dict[str, object]:
        return _voice_status_payload()

    @app.get("/edge/local-capabilities")
    def local_edge_capabilities() -> dict[str, object]:
        voice = _voice_status_payload()["voice"]
        screen = _screen_status_payload()["screen"]
        capabilities: list[str] = []
        if cfg.backend != "none" and cfg.pixel_count > 0:
            capabilities.append("set_pixels")
        if isinstance(voice, dict) and voice.get("active") is True:
            capabilities.append("voice_assistant")
        if isinstance(screen, dict) and screen.get("active") is True:
            capabilities.append("kiosk_display")
        if cfg.zebra.host.strip():
            capabilities.append("label_printer")
        return {
            "ok": True,
            "schema": "magpie.edge.local_capabilities.v1",
            "capabilities": capabilities,
            "services": {
                "lights": "set_pixels" in capabilities,
                "voice": "voice_assistant" in capabilities,
                "screen": "kiosk_display" in capabilities,
                "printer": "label_printer" in capabilities,
                "check_station": False,
            },
        }

    @app.post("/edge/local/print-zpl")
    def local_edge_print_zpl(
        body: EdgePrintZplRequest,
        request: Request,
    ) -> dict[str, object]:
        client_host = str(request.client.host if request.client else "")
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(
                status_code=403,
                detail="local edge commands are restricted to loopback",
            )
        zpl = body.zpl.strip()
        if not zpl.startswith("^XA") or not zpl.endswith("^XZ"):
            raise HTTPException(status_code=400, detail="invalid ZPL document")
        try:
            send_zpl(f"{zpl}\n", cfg.zebra)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "printer": cfg.zebra.host}

    def _demo_check(
        key: str,
        label: str,
        passed: bool,
        evidence: str,
        next_step: str = "",
        action: dict[str, str] | None = None,
    ) -> dict[str, object]:
        is_passed = bool(passed)
        payload: dict[str, object] = {
            "key": key,
            "label": label,
            "passed": is_passed,
            "evidence": evidence,
            "next_step": "" if is_passed else next_step,
        }
        if action and not is_passed:
            payload["action"] = action
        return payload

    def _hardware_diagnostics_payload() -> dict[str, object]:
        all_locations = locations.all()
        children_by_parent: dict[str, int] = {}
        owners: dict[tuple[str, int], list[str]] = {}
        target_pixel_counts: dict[str, int] = {}
        invalid: list[dict[str, object]] = []
        configured_locations = 0
        for loc in all_locations:
            if loc.parent_id:
                children_by_parent[loc.parent_id] = children_by_parent.get(loc.parent_id, 0) + 1
            if loc.light_indexes:
                configured_locations += 1
            controller_id = loc.light_target or "main"
            for pixel in sorted(set(loc.light_indexes)):
                limit = _light_index_limit()
                if not 0 <= pixel < limit:
                    invalid.append(
                        {"location_id": loc.id, "controller_id": controller_id, "pixel": pixel}
                    )
                    continue
                owners.setdefault((controller_id, pixel), []).append(loc.id)
                target_pixel_counts[controller_id] = max(
                    target_pixel_counts.get(controller_id, 0), pixel + 1
                )
        conflicts = [
            {
                "controller_id": controller_id,
                "pixel": pixel,
                "location_ids": sorted(location_ids),
            }
            for (controller_id, pixel), location_ids in sorted(owners.items())
            if len(set(location_ids)) > 1
        ]
        unbound_lit_leaves = sorted(
            loc.id
            for loc in all_locations
            if loc.light_target and not loc.light_indexes and not children_by_parent.get(loc.id)
        )
        assigned_pixels = len(owners)
        ready = bool(
            (cfg.pixel_count > 0 or target_pixel_counts)
            and configured_locations > 0
            and not invalid
            and not conflicts
            and not unbound_lit_leaves
        )
        controller_diagnostics: dict[str, object] = {
            "backend": cfg.backend,
            "pixel_count": cfg.pixel_count,
            "currently_lit_pixels": sum(
                1 for pixel in controller.pixels if any(pixel)
            ),
        }
        if isinstance(controller, WorkspaceLedController):
            controller_diagnostics["targets"] = target_pixel_counts
        return {
            "ok": True,
            "schema": "magpie.hardware_diagnostics.v1",
            "ready": ready,
            "controller": controller_diagnostics,
            "configured_locations": configured_locations,
            "assigned_pixels": assigned_pixels,
            "unassigned_pixels": max(
                0,
                (
                    cfg.pixel_count
                    if not isinstance(controller, WorkspaceLedController)
                    else sum(target_pixel_counts.values())
                )
                - assigned_pixels,
            ),
            "invalid_bindings": invalid,
            "conflicts": conflicts,
            "unbound_lit_leaves": unbound_lit_leaves,
        }

    @app.get("/hardware/diagnostics")
    def hardware_diagnostics() -> dict[str, object]:
        return _hardware_diagnostics_payload()

    def _voice_acceptance_payload() -> dict[str, object]:
        window_size = 5
        required_passes = 4
        recent_attempts = _voice_attempts_with_tuning(
            store.recent_voice_command_attempts(limit=50)
        )
        current_commit = build.commit.strip()
        current_runtime_attempts = [
            attempt
            for attempt in recent_attempts
            if current_commit
            and current_commit[:7] in str(attempt.get("runtime_version") or "")
        ]
        attempts = current_runtime_attempts[-window_size:]
        legacy_attempt_count = len(recent_attempts) - len(current_runtime_attempts)
        passed = sum(1 for attempt in attempts if bool(attempt["tuning"]["passed"]))
        lit_find = any(
            bool(attempt["tuning"]["passed"])
            and bool(attempt["tuning"].get("lit"))
            and re.search(
                r"\b(find|where|locate|show)\b",
                f"{attempt.get('command', '')} {attempt.get('query', '')}",
                re.I,
            )
            for attempt in attempts
        )
        ready = len(attempts) == window_size and passed >= required_passes and lit_find
        hands_free_checks = {
            "putaway_proposed": False,
            "proposal_lit": False,
            "proposal_observed": False,
            "voice_confirmed": False,
            "inventory_saved": False,
            "completion_observed": False,
        }
        hands_free_proposal_id = 0
        hands_free_confirmation_id = 0
        current_attempts_by_interaction = {
            int(attempt["tuning"]["linked_interaction_id"]): attempt
            for attempt in current_runtime_attempts[-20:]
            if isinstance(attempt.get("tuning"), dict)
            and isinstance(attempt["tuning"].get("linked_interaction_id"), int)
        }
        for proposal_attempt in reversed(current_runtime_attempts[-20:]):
            tuning = proposal_attempt.get("tuning")
            if not isinstance(tuning, dict) or not re.search(
                r"\b(put(?:\s+away)?|store|file|stash|stow)\b",
                f"{proposal_attempt.get('command', '')} {proposal_attempt.get('query', '')}",
                re.I,
            ):
                continue
            proposal_id = tuning.get("linked_interaction_id")
            if not isinstance(proposal_id, int):
                continue
            proposal = store.assistant_interaction(proposal_id)
            proposal_response = (
                proposal.get("response") if isinstance(proposal, dict) else None
            )
            confirmation_response = (
                proposal_response.get("confirmation_response")
                if isinstance(proposal_response, dict)
                else None
            )
            confirmation_id = (
                confirmation_response.get("confirmation_interaction_id")
                if isinstance(confirmation_response, dict)
                else None
            )
            confirmation_attempt = (
                current_attempts_by_interaction.get(confirmation_id)
                if isinstance(confirmation_id, int)
                else None
            )
            confirmation_tuning = (
                confirmation_attempt.get("tuning")
                if isinstance(confirmation_attempt, dict)
                else None
            )
            applied = (
                confirmation_response.get("applied")
                if isinstance(confirmation_response, dict)
                else None
            )
            hands_free_proposal_id = proposal_id
            hands_free_confirmation_id = (
                confirmation_id if isinstance(confirmation_id, int) else 0
            )
            hands_free_checks = {
                "putaway_proposed": True,
                "proposal_lit": bool(tuning.get("lit")),
                "proposal_observed": bool(tuning.get("screen_seen_interaction")),
                "voice_confirmed": bool(
                    isinstance(confirmation_attempt, dict)
                    and str(confirmation_attempt.get("route") or "")
                    == "/assistant/voice/confirm"
                    and isinstance(confirmation_tuning, dict)
                    and confirmation_tuning.get("checks", {}).get("backend") is True
                ),
                "inventory_saved": bool(
                    isinstance(proposal, dict)
                    and proposal.get("status") == "confirmed"
                    and isinstance(applied, list)
                    and any(
                        isinstance(entry, dict) and entry.get("stored")
                        for entry in applied
                    )
                ),
                "completion_observed": bool(
                    isinstance(confirmation_tuning, dict)
                    and confirmation_tuning.get("screen_seen_interaction")
                ),
            }
            break
        hands_free_ready = all(hands_free_checks.values())

        check_labels = {
            "audio": "microphone did not capture clear speech",
            "wake_or_armed": "wake word was not accepted",
            "command": "no command followed the wake word",
            "routed": "command was not routed",
            "backend": "agent request failed",
            "screen_linked": "result was not linked to the kiosk",
            "screen_active": "kiosk was not active",
            "screen_observed": "kiosk did not display the result",
            "light": "cabinet light did not activate",
        }
        failure_counts: dict[str, int] = {}
        for attempt in attempts:
            tuning = attempt["tuning"]
            if bool(tuning["passed"]):
                continue
            checks = tuning.get("checks") if isinstance(tuning.get("checks"), dict) else {}
            reason = next(
                (label for key, label in check_labels.items() if checks.get(key) is False),
                str(attempt.get("error") or "voice request did not complete"),
            )
            failure_counts[reason] = failure_counts.get(reason, 0) + 1

        lit_item = next(
            (
                item.name
                for item in store.list_inventory()
                if locations.nearest_lit(item.bin_id) is not None
            ),
            "",
        )
        root = next((loc for loc in locations.all() if not loc.parent_id), None)
        if ready and not hands_free_ready:
            prompt = "Magpie, put away a test item"
            instruction = (
                "Run one hands-free put-away on this build. The proposal must light and appear "
                "on the kiosk; then say yes and wait for the completion receipt."
            )
        elif ready:
            prompt = "Voice check passed"
            instruction = "Magpie heard, completed, displayed, and lit recent requests reliably."
        elif not attempts:
            if lit_item:
                prompt = f"Magpie, find {lit_item}"
                instruction = (
                    "No spoken requests have been verified on this build. "
                    "Say the displayed request near the cabinet to start a fresh five-command check."
                )
            else:
                prompt = "Store an item in a lit location"
                instruction = (
                    "This build has no voice evidence, and a voice find cannot be verified "
                    "until at least one stored item has a connected light."
                )
        elif not lit_find:
            if lit_item:
                prompt = f"Magpie, find {lit_item}"
                instruction = "Say this near the cabinet. The matching location must light and appear on the kiosk."
            else:
                prompt = "Store an item in a lit location"
                instruction = "A voice find cannot be verified until at least one stored item has a connected light."
        else:
            prompt = (
                f"Magpie, what is in {root.label or root.id}"
                if root is not None
                else "Magpie, list my storage spaces"
            )
            instruction = "Repeat the displayed request until at least four of the latest five attempts pass."
        if failure_counts and "kiosk was not active" in failure_counts:
            instruction = "Keep the cabinet kiosk open, then say the displayed request near the microphone."

        return {
            "ok": True,
            "schema": "magpie.voice_acceptance.v1",
            "ready": ready,
            "window_size": window_size,
            "required_passes": required_passes,
            "runtime_scope": current_commit[:7] or "unknown",
            "evidence_state": "current" if attempts else "missing_current",
            "legacy_attempt_count": max(0, legacy_attempt_count),
            "attempt_count": len(attempts),
            "passed": passed,
            "lit_find": lit_find,
            "hands_free_confirmation": {
                "ready": hands_free_ready,
                "checks": hands_free_checks,
                "proposal_interaction_id": hands_free_proposal_id,
                "confirmation_interaction_id": hands_free_confirmation_id,
            },
            "remaining": max(0, required_passes - passed),
            "prompt": prompt,
            "instruction": instruction,
            "failures": [
                {"reason": reason, "count": count}
                for reason, count in sorted(
                    failure_counts.items(), key=lambda entry: (-entry[1], entry[0])
                )
            ],
            "attempts": attempts,
        }

    @app.get("/voice/acceptance")
    def voice_acceptance() -> dict[str, object]:
        return _voice_acceptance_payload()

    @app.get("/demo/readiness")
    def demo_readiness() -> dict[str, object]:
        capabilities_payload = _capability_payload()
        features = capabilities_payload["features"]
        endpoints = capabilities_payload["endpoints"]
        attempts = _voice_attempts_with_tuning(store.recent_voice_attempts(limit=50))
        calibration = _voice_calibration_summary(attempts)
        acceptance = _voice_acceptance_payload()
        recent_voice_window = acceptance["attempts"]
        recent_voice_passes = int(acceptance["passed"])
        recent_voice_rate = (
            recent_voice_passes / len(recent_voice_window)
            if recent_voice_window
            else 0.0
        )
        voice_find_passed = bool(acceptance["lit_find"])
        voice_passed = bool(acceptance["ready"])
        hands_free_confirmation = acceptance["hands_free_confirmation"]
        hands_free_confirmation_passed = bool(hands_free_confirmation["ready"])
        voice_evidence = (
            f"{recent_voice_passes}/{len(recent_voice_window)} current-build activations passed; "
            f"lit find={'yes' if voice_find_passed else 'no'}"
        )
        voice_next_step = ""
        if not voice_passed:
            voice_next_step = (
                str(acceptance["instruction"])
                if not recent_voice_window
                else "Pass at least 4 of the latest 5 voice activations, including one find that lights and appears on the kiosk."
            )
        hardware = _hardware_diagnostics_payload()
        checks = [
            _demo_check(
                "qr_scan",
                "QR scan opens item/location and quick put-away",
                bool(
                    features["qr_scan"]
                    and features["qr_scan_local_fallback"]
                    and features["item_qr_scan"]
                    and features["scan_quick_put"]
                ),
                f"{endpoints['item_scan']}, {endpoints['location_scan']}, {endpoints['scan_quick_put']}, {endpoints['qr_decoder']}",
            ),
            _demo_check(
                "text_photo_putaway",
                "Text and photo put-away use the canonical assistant",
                bool(features["agent_chat"] and features["photo_upload"] and features["photo_sort_review_flags"]),
                f"{endpoints['assistant_chat']}, {endpoints['photos']}",
            ),
            _demo_check(
                "mobile_pwa",
                "The installable mobile app supports resilient capture and put-away",
                bool(
                    features["mobile_client"]
                    and features["pwa_manifest"]
                    and features["service_worker_shell_cache"]
                    and features["mobile_offline_mutation_queue"]
                    and features["scan_quick_put_retry_queue"]
                    and features["scan_quick_put_photo_payload_queue"]
                    and features["photo_attachment_retry"]
                ),
                (
                    f"{endpoints['mobile_client']}, {endpoints['manifest']}, "
                    f"{endpoints['service_worker']}, {endpoints['scan_quick_put']}, "
                    f"{endpoints['photo_attachment_retry']}"
                ),
                "Restore the mobile PWA shell, offline put-away queue, and photo retry path.",
            ),
            _demo_check(
                "voice_find",
                "Recent voice interactions are reliable end to end",
                voice_passed,
                f"{voice_evidence}; {endpoints['voice_acceptance']}",
                voice_next_step,
            ),
            _demo_check(
                "voice_putaway_confirmation",
                "A spoken put-away and confirmation complete hands free",
                hands_free_confirmation_passed,
                ", ".join(
                    f"{name.replace('_', ' ')}={'yes' if passed else 'no'}"
                    for name, passed in hands_free_confirmation["checks"].items()
                ),
                (
                    "Say a put-away request near the cabinet, wait for the lit proposal, "
                    "then say yes and verify the completion receipt appears."
                    if not hands_free_confirmation_passed
                    else ""
                ),
            ),
            _demo_check(
                "learned_rule",
                "Learned placement and size rules can be confirmed and undone",
                bool(features["learned_rules"] and features["size_rules"] and features["agent_update_rule_undo"]),
                f"{endpoints['assistant_chat']}, {endpoints['assistant_undo']}",
            ),
            _demo_check(
                "review_approval",
                "Uncertain captures can be reviewed and approved through Magpie",
                bool(
                    features["agent_inventory_review"]
                    and features["learning_examples_review"]
                    and features["learning_examples_approve"]
                ),
                f"{endpoints['assistant_chat']}, {endpoints['inventory_review']}",
            ),
            _demo_check(
                "labels",
                "Labels can be previewed/exported as PDFs",
                bool(
                    features["printable_label_pdfs"]
                    and features["item_label_pdfs"]
                    and features["item_label_batch_pdfs"]
                ),
                (
                    f"{endpoints['label_sheet_pdf']}, {endpoints['item_label_pdf']}, "
                    f"{endpoints['item_label_batch_pdf']}"
                ),
            ),
            _demo_check(
                "export",
                "Inventory can be exported for trust and portability",
                bool(
                    features["inventory_json_export"]
                    and features["inventory_csv_export"]
                    and features["inventory_pdf_export"]
                ),
                (
                    f"{endpoints['export_inventory_json']}, "
                    f"{endpoints['export_inventory_csv']}, "
                    f"{endpoints['export_inventory_pdf']}"
                ),
            ),
            _demo_check(
                "build_visibility",
                "Build version is visible and queryable",
                bool(build.version and build.commit),
                endpoints["build"],
            ),
            _demo_check(
                "cabinet_hardware",
                "Cabinet light bindings are valid and conflict-free",
                bool(hardware["ready"]),
                (
                    f"{hardware['configured_locations']} locations, "
                    f"{hardware['assigned_pixels']}/{hardware['controller']['pixel_count']} pixels, "
                    f"{len(hardware['conflicts'])} conflicts"
                ),
                "Open cabinet hardware diagnostics and repair invalid, conflicting, or unbound lights."
                if not hardware["ready"]
                else "",
            ),
        ]
        passed = sum(1 for check in checks if check["passed"])
        return {
            "ok": True,
            "schema": "magpie.demo_readiness.v1",
            "build": build.as_dict() | {"label": build.label()},
            "ready": passed == len(checks),
            "passed": passed,
            "failed": len(checks) - passed,
            "checks": checks,
            "voice_window": {
                "size": len(recent_voice_window),
                "passed": recent_voice_passes,
                "rate": round(recent_voice_rate, 3),
                "lit_find": voice_find_passed,
                "runtime_scope": str(acceptance["runtime_scope"]),
                "evidence_state": str(acceptance["evidence_state"]),
                "legacy_attempt_count": int(acceptance["legacy_attempt_count"]),
                "required_size": int(acceptance["window_size"]),
                "required_passes": int(acceptance["required_passes"]),
                "required_rate": 0.8,
            },
            "voice_calibration": calibration,
            "hardware": hardware,
        }

    def _ambiguous_active_location_groups() -> list[list[str]]:
        buckets: dict[tuple[str, str], list[str]] = {}
        for location in locations.all():
            normalized_name = " ".join(
                str(location.label or location.id).split()
            ).casefold()
            key = (str(location.parent_id or ""), normalized_name)
            buckets.setdefault(key, []).append(location.id)
        return [
            location_ids
            for location_ids in buckets.values()
            if len(location_ids) > 1
        ]

    @app.get("/workspace/readiness")
    def workspace_readiness() -> dict[str, object]:
        active_locations = locations.all()
        roots = [
            location
            for location in active_locations
            if location.parent_id is None
        ]
        destinations = [
            location
            for location in active_locations
            if location.accepts_items
        ]
        inventory = store.list_inventory()
        item_ids = {int(row.item_id) for row in inventory}
        ambiguous_name_groups = _ambiguous_active_location_groups()

        checks = [
            {
                "key": "storage_map",
                "label": "Create a storage place",
                "description": "Add the home, workshop, room, or other top-level place where storage lives.",
                "passed": bool(roots),
                "required": True,
                "action": {
                    "label": "Create storage",
                    "href": "/storage?view=places&onboarding=1",
                },
            },
            {
                "key": "item_destination",
                "label": "Add an item destination",
                "description": "Create a bin, drawer, shelf, box, or other place that can hold items.",
                "passed": bool(destinations),
                "required": True,
                "action": {
                    "label": "Add a destination",
                    "href": "/storage?view=places&onboarding=destination",
                },
            },
            {
                "key": "unambiguous_paths",
                "label": "Resolve duplicate place names",
                "description": (
                    "Places at the same level need distinct names so Magpie can "
                    "identify exact paths without guessing."
                ),
                "passed": not ambiguous_name_groups,
                "required": True,
                "action": {
                    "label": "Review duplicates",
                    "href": "/storage?view=places&conflicts=1",
                },
            },
            {
                "key": "first_item",
                "label": "Put away your first item",
                "description": "Ask Magpie to put something away and start your searchable inventory.",
                "passed": bool(item_ids),
                "required": False,
                "action": {
                    "label": "Open Magpie",
                    "href": "/",
                },
            },
        ]
        required_checks = [check for check in checks if check["required"]]
        recommended_checks = [check for check in checks if not check["required"]]
        required_passed = sum(1 for check in required_checks if check["passed"])
        recommended_passed = sum(
            1 for check in recommended_checks if check["passed"]
        )
        ready = required_passed == len(required_checks)
        if ambiguous_name_groups:
            stage = "needs_path_repair"
            summary = (
                "Resolve duplicate place names before Magpie uses these storage paths."
            )
        elif ready and item_ids:
            stage = "inventory_started"
            summary = "Magpie is ready for find and put-away."
        elif ready:
            stage = "storage_ready"
            summary = "Your storage is ready. Put away your first item to start the catalog."
        elif roots:
            stage = "needs_destination"
            summary = "Add an item destination to make Magpie ready for put-away."
        else:
            stage = "needs_storage"
            summary = "Create your first storage place to begin."
        return {
            "ok": True,
            "schema": "magpie.workspace_readiness.v2",
            "ready": ready,
            "stage": stage,
            "summary": summary,
            "required_passed": required_passed,
            "required_total": len(required_checks),
            "recommended_passed": recommended_passed,
            "recommended_total": len(recommended_checks),
            "checks": checks,
            "counts": {
                "roots": len(roots),
                "locations": len(active_locations),
                "destinations": len(destinations),
                "items": len(item_ids),
                "placements": len(inventory),
                "ambiguous_name_groups": len(ambiguous_name_groups),
            },
        }

    @app.get("/product/readiness")
    def product_readiness() -> dict[str, object]:
        demo = demo_readiness()
        demo_checks = {
            str(check.get("key") or ""): check
            for check in demo["checks"]
            if isinstance(check, dict)
        }
        capabilities_payload = _capability_payload()
        features = capabilities_payload["features"]
        endpoints = capabilities_payload["endpoints"]
        agent = assistant_status()
        storage = repository_bundle.info()
        public_origin = cfg.public_origin or cfg.zebra.base_url.strip()
        secure_origin = urlsplit(public_origin).scheme.lower() == "https"
        shared_storage = str(storage.get("backend") or "").lower() == "supabase"
        runtime = _runtime_profile(
            cfg,
            storage=storage,
            edge_enabled=edge_commands is not None,
        )
        central_service = bool(
            runtime.get("role") == "central_service"
            and runtime.get("hardware_owner") is False
            and runtime.get("workspace_scope") == "request"
            and runtime.get("edge_execution") == "workspace_queue"
            and runtime.get("edge_queue_enabled") is True
            and shared_storage
            and cfg.auth.mode == "supabase"
            and cfg.auth.multi_workspace
        )
        edge_status_payload = edge_commands.status() if edge_commands is not None else {}
        runtime_capabilities = _runtime_capabilities_by_device()
        edge_devices: list[dict[str, object]] = []
        for raw_device in edge_status_payload.get("devices") or []:
            if not isinstance(raw_device, dict):
                continue
            device = dict(raw_device)
            device_id = str(device.get("device_id") or "")
            device["capabilities"] = sorted(
                {
                    *(str(value) for value in device.get("capabilities") or []),
                    *runtime_capabilities.get(device_id, set()),
                }
            )
            device.update(_edge_device_connection(device))
            edge_devices.append(device)
        all_locations = locations.all(include_archived=True)
        active_locations = [location for location in all_locations if location.active]
        configured_light_targets = sorted(
            {
                str(location.light_target or "").strip()
                for location in active_locations
                if location.light_indexes and str(location.light_target or "").strip()
            }
        )
        selected_target_ids = (
            [cfg.edge.target_device_id]
            if cfg.edge.target_device_id
            else configured_light_targets
        )
        devices_by_id = {
            str(device.get("device_id") or ""): device
            for device in edge_devices
            if isinstance(device, dict) and device.get("device_id")
        }
        configured_edge_targets: dict[str, list[str]] = {
            "lights": (
                selected_target_ids
                if cfg.edge.mode in {"central", "workspace"} or central_service
                else []
            ),
        }
        for service, (scope, _) in EDGE_SERVICE_REQUIREMENTS.items():
            if service == "lights":
                continue
            configured_edge_targets[service] = sorted(
                {
                    str(device.get("device_id") or "")
                    for device in edge_devices
                    if scope in {
                        str(value) for value in device.get("credential_scopes") or []
                    }
                    and str(device.get("device_id") or "")
                }
            )

        def _edge_service_ready(service: str, target_ids: list[str]) -> bool:
            if not target_ids:
                return False
            target_devices = [
                devices_by_id[target]
                for target in target_ids
                if target in devices_by_id
            ]
            if len(target_devices) != len(target_ids):
                return False
            return all(
                bool(
                    (
                        device.get("service_readiness")
                        if isinstance(device.get("service_readiness"), dict)
                        else {}
                    ).get(service, {}).get("ready")
                )
                for device in target_devices
            )

        secure_edge_services = {
            service: _edge_service_ready(service, target_ids)
            for service, target_ids in configured_edge_targets.items()
        }
        managed_roots = [
            location for location in active_locations if not location.parent_id
        ]
        active_location_by_id = {location.id: location for location in active_locations}
        all_location_by_id = {location.id: location for location in all_locations}
        ambiguous_location_names = _ambiguous_active_location_groups()
        inventory = store.list_inventory()
        orphaned_inventory = [
            item for item in inventory if item.bin_id not in all_location_by_id
        ]
        organizer_only_inventory = [
            item
            for item in inventory
            if (
                (destination := active_location_by_id.get(item.bin_id)) is not None
                and not destination.accepts_items
            )
        ]
        unavailable_rule_targets = [
            rule
            for rule in store.rules(active_only=True, limit=500)
            if (
                (target := all_location_by_id.get(rule.target_location_id)) is None
                or not target.active
            )
        ]
        whole_storage_agent = bool(
            len(managed_roots) >= 2
            and not orphaned_inventory
            and not organizer_only_inventory
            and not unavailable_rule_targets
            and not ambiguous_location_names
            and features.get("agent_storage_roots")
            and features.get("agent_location_inspection")
            and features.get("agent_label_batch_preview")
        )
        whole_storage_next_steps: list[str] = []
        if len(managed_roots) < 2:
            whole_storage_next_steps.append(
                "Create another top-level storage space in Places."
            )
        if orphaned_inventory:
            count = len(orphaned_inventory)
            whole_storage_next_steps.append(
                f"Repair {count} inventory record{'s' if count != 1 else ''} "
                "whose place no longer exists."
            )
        if organizer_only_inventory:
            count = len(organizer_only_inventory)
            whole_storage_next_steps.append(
                f"Move {count} inventory placement{'s' if count != 1 else ''} "
                "out of organizer-only places and into item destinations."
            )
        if unavailable_rule_targets:
            count = len(unavailable_rule_targets)
            whole_storage_next_steps.append(
                f"Repair or disable {count} active placement rule"
                f"{'s' if count != 1 else ''} whose target is unavailable."
            )
        if ambiguous_location_names:
            count = len(ambiguous_location_names)
            whole_storage_next_steps.append(
                f"Review {count} duplicate place-name group"
                f"{'s' if count != 1 else ''} in Places and rename, move, or "
                "archive one place in each group."
            )
        missing_agent_tools = [
            label
            for feature, label in (
                ("agent_storage_roots", "storage-root discovery"),
                ("agent_location_inspection", "place inspection"),
                ("agent_label_batch_preview", "label preview"),
            )
            if not features.get(feature)
        ]
        if missing_agent_tools:
            whole_storage_next_steps.append(
                f"Enable the agent's {', '.join(missing_agent_tools)} "
                f"tool{'s' if len(missing_agent_tools) != 1 else ''}."
            )
        if organizer_only_inventory or orphaned_inventory:
            whole_storage_action = {
                "label": "Review placement issues",
                "href": "/storage?view=inventory&scope=placement",
            }
        elif unavailable_rule_targets:
            whole_storage_action = {
                "label": "Review rule issues",
                "href": "/storage?view=rules&scope=issues",
            }
        elif ambiguous_location_names:
            whole_storage_action = {
                "label": "Review duplicate places",
                "href": "/storage?view=places&conflicts=1",
            }
        elif len(managed_roots) < 2:
            whole_storage_action = {
                "label": "Add storage space",
                "href": "/storage?view=places&onboarding=root",
            }
        elif missing_agent_tools:
            whole_storage_action = {
                "label": "Open agent connections",
                "href": "/account#agentConnectionsSection",
            }
        else:
            whole_storage_action = None
        managed_root_ids = {location.id for location in managed_roots}
        non_main_root_ids = managed_root_ids - {"main-cabinet"}
        journey_evidence = {
            key: locations.has_event(event_type)
            for key, event_type in AGENT_STORAGE_EVENTS.items()
        }
        correction_evidence = {
            key: locations.has_event(event_type)
            for key, event_type in AGENT_CORRECTION_EVENTS.items()
        }
        management_evidence = {
            key: locations.has_event(event_type)
            for key, event_type in WORKSPACE_MANAGEMENT_EVENTS.items()
        }

        def root_id_for_location(location_id: object) -> str:
            path = location_path(locations, str(location_id or ""))
            return path[0].id if path else ""

        agent_storage_journeys = all(journey_evidence.values())
        agent_correction_journey = all(correction_evidence.values())
        workspace_management_journey = all(management_evidence.values())

        def assistant_prefill_action(
            label: str,
            prompt: str = "",
            *,
            action: str = "",
        ) -> dict[str, str]:
            query = []
            if action:
                query.append(f"action={quote(action, safe='')}")
            if prompt:
                query.append(f"prompt={quote(prompt, safe='')}")
            return {
                "label": label,
                "href": f"/?{'&'.join(query)}" if query else "/",
            }

        outside_item = next(
            (
                row
                for row in inventory
                if root_id_for_location(row.bin_id) in non_main_root_ids
            ),
            None,
        )
        representative_item = str(inventory[0].name).strip() if inventory else ""
        representative_root = next(
            (
                root
                for root in managed_roots
                if root.id in non_main_root_ids
            ),
            managed_roots[0] if managed_roots else None,
        )
        if len(managed_roots) < 2:
            agent_journey_action = {
                "label": "Add storage space",
                "href": "/storage?view=places&onboarding=root",
            }
        elif not journey_evidence["root_discovery"]:
            agent_journey_action = assistant_prefill_action(
                "List storage spaces",
                "List my storage spaces",
            )
        elif not journey_evidence["cross_root_retrieval"]:
            agent_journey_action = (
                assistant_prefill_action(
                    f"Find {outside_item.name}",
                    f"Find {outside_item.name}",
                    action="find",
                )
                if outside_item is not None
                else assistant_prefill_action(
                    "Put away outside cabinet",
                    action="put-away",
                )
            )
        elif not journey_evidence["label_preview"] and representative_root is not None:
            root_name = representative_root.label or representative_root.id
            agent_journey_action = assistant_prefill_action(
                f"Preview {root_name} labels",
                f"Preview labels for {root_name}",
            )
        else:
            agent_journey_action = None

        if not correction_evidence["explained_proposal"]:
            correction_action = assistant_prefill_action(
                "Start explained put-away",
                f"Put away {representative_item}" if representative_item else "",
                action="put-away",
            )
        elif not correction_evidence["correction_applied"]:
            correction_action = assistant_prefill_action(
                "Start correction test",
                f"Put away {representative_item}" if representative_item else "",
                action="put-away",
            )
        elif not correction_evidence["correction_retained"]:
            correction_action = assistant_prefill_action(
                "Verify learned correction",
                f"Put away {representative_item}" if representative_item else "",
                action="put-away",
            )
        else:
            correction_action = None

        if not management_evidence["visual_multi_root_setup"]:
            management_action = {
                "label": "Set up storage space",
                "href": "/storage?view=places&onboarding=1",
            }
        elif not management_evidence["cross_root_item_move"]:
            management_action = {
                "label": "Move inventory",
                "href": "/storage?view=inventory",
            }
        elif not management_evidence["location_archive_restore"]:
            management_action = {
                "label": "Test place recovery",
                "href": "/storage?view=places",
            }
        else:
            management_action = None

        due_place_checks, more_due_place_checks = _review_place_checks(limit=50)
        due_place_check_states: dict[str, int] = {}
        for place_check in due_place_checks:
            confidence = place_check.get("confidence")
            state = str(
                confidence.get("state")
                if isinstance(confidence, dict)
                else "unknown"
            )
            due_place_check_states[state] = due_place_check_states.get(state, 0) + 1
        physical_storage_current = not due_place_checks and not more_due_place_checks

        agent_control_plane = bool(
            agent.get("agent_ready")
            and agent.get("agent_api") in {"responses", "chat_completions"}
            and features.get("mcp_server")
        )
        edge_service_metadata = {
            "lights": (
                "secure_edge_controller",
                "Configured cabinet light controllers are securely connected",
                "Enroll a commands-scoped cabinet token and verify a recent set_pixels heartbeat.",
            ),
            "voice": (
                "secure_edge_voice",
                "Configured voice stations are securely connected",
                "Connect each voice-scoped device and verify a recent voice assistant heartbeat.",
            ),
            "screen": (
                "secure_edge_screen",
                "Configured kiosk screens are securely connected",
                "Connect each screen-scoped device and verify a recent kiosk display heartbeat.",
            ),
            "printer": (
                "secure_edge_printer",
                "Configured label printers are securely connected",
                "Connect each printer-scoped device and verify a recent label printer heartbeat.",
            ),
            "check_station": (
                "secure_edge_check_station",
                "Configured check stations are securely connected",
                "Connect each check-station-scoped device and verify a recent check station heartbeat.",
            ),
        }

        def edge_service_check(service: str) -> dict[str, object]:
            key, label, next_step = edge_service_metadata[service]
            scope, capability = EDGE_SERVICE_REQUIREMENTS[service]
            target_ids = configured_edge_targets[service]
            ready_ids = []
            for target_id in target_ids:
                device = devices_by_id.get(target_id) or {}
                readiness = device.get("service_readiness")
                service_status = (
                    readiness.get(service) if isinstance(readiness, dict) else None
                )
                if isinstance(service_status, dict) and service_status.get("ready"):
                    ready_ids.append(target_id)
            return _demo_check(
                key,
                label,
                secure_edge_services[service],
                (
                    f"configured={str(bool(target_ids)).lower()}, "
                    f"targets={','.join(target_ids) or 'none'}, "
                    f"scope={scope}, capability={capability}, "
                    f"enrolled={sum(1 for target_id in target_ids if (devices_by_id.get(target_id) or {}).get('enrolled'))}/{len(target_ids)}, "
                    f"ready={len(ready_ids)}/{len(target_ids)}"
                ),
                next_step if not secure_edge_services[service] else "",
            )

        edge_service_checks = [
            edge_service_check(service) for service in EDGE_SERVICE_REQUIREMENTS
        ]
        product_checks = [
            _demo_check(
                "agent_control_plane",
                "Storage agent and guarded MCP control plane are ready",
                agent_control_plane,
                (
                    f"provider={agent.get('provider', 'unknown')}, "
                    f"model={agent.get('model', 'unknown')}, "
                    f"api={agent.get('agent_api', 'unknown')}, mcp={endpoints['mcp']}"
                ),
                "Configure an available storage-agent provider and verify the guarded MCP endpoint."
                if not agent_control_plane
                else "",
            ),
            _demo_check(
                "mobile_https_origin",
                "An HTTPS mobile/PWA origin is configured",
                secure_origin,
                public_origin or "No public origin configured",
                "Configure the public HTTPS origin used by sign-in, QR codes, and mobile installs."
                if not secure_origin
                else "",
            ),
            _demo_check(
                "shared_storage",
                "Inventory uses the shared Supabase backend",
                shared_storage,
                f"backend={storage.get('backend', 'unknown')}, workspace={storage.get('workspace_id', '')}",
                "Provision Supabase, migrate the live workspace, and switch the Pi repository backend."
                if not shared_storage
                else "",
            ),
            _demo_check(
                "central_service",
                "Hardware-neutral authenticated central service is configured",
                central_service,
                (
                    f"backend={cfg.backend}, storage={storage.get('backend', 'unknown')}, "
                    f"auth={cfg.auth.mode}, multi_workspace={str(cfg.auth.multi_workspace).lower()}, "
                    f"runtime={runtime.get('role', 'unknown')}, edge={runtime.get('edge_execution', 'unknown')}"
                ),
                (
                    "Deploy the container with no local hardware, Supabase storage and auth, "
                    "request-scoped workspaces, and workspace-routed edge commands."
                    if not central_service
                    else ""
                ),
            ),
            *edge_service_checks,
            _demo_check(
                "whole_storage_agent",
                "Magpie manages multiple storage spaces through one agent",
                whole_storage_agent,
                (
                    f"roots={', '.join(root.label or root.id for root in managed_roots) or 'none'}; "
                    f"inventory_records={len(inventory)}; orphaned={len(orphaned_inventory)}; "
                    f"organizer_only={len(organizer_only_inventory)}; "
                    f"unavailable_rule_targets={len(unavailable_rule_targets)}; "
                    f"ambiguous_names={len(ambiguous_location_names)}"
                ),
                (
                    " ".join(whole_storage_next_steps)
                    if not whole_storage_agent
                    else ""
                ),
                whole_storage_action if not whole_storage_agent else None,
            ),
            _demo_check(
                "physical_storage_integrity",
                "Occupied storage places have current physical checks",
                physical_storage_current,
                (
                    f"due={len(due_place_checks)}"
                    + ("+" if more_due_place_checks else "")
                    + "; "
                    + (
                        ", ".join(
                            f"{state}={count}"
                            for state, count in sorted(due_place_check_states.items())
                        )
                        or "all occupied places current"
                    )
                ),
                (
                    "Check every occupied place that is uncertain, overdue, newly added, "
                    "or changed since its last physical check."
                    if not physical_storage_current
                    else ""
                ),
                (
                    {"label": "Review storage checks", "href": "/review"}
                    if not physical_storage_current
                    else None
                ),
            ),
            _demo_check(
                "agent_storage_journeys",
                "Recent agent journeys prove multi-storage discovery, retrieval, and labels",
                agent_storage_journeys,
                ", ".join(
                    f"{name.replace('_', ' ')}={'yes' if passed else 'no'}"
                    for name, passed in journey_evidence.items()
                ),
                (
                    "Ask Magpie to list storage spaces, retrieve an item outside the main cabinet, "
                    "and create a label preview for a storage space."
                    if not agent_storage_journeys
                    else ""
                ),
                agent_journey_action if not agent_storage_journeys else None,
            ),
            _demo_check(
                "agent_correction_journey",
                "Recent agent use proves explained and retained corrections",
                agent_correction_journey,
                ", ".join(
                    f"{name.replace('_', ' ')}={'yes' if passed else 'no'}"
                    for name, passed in correction_evidence.items()
                ),
                (
                    "Review a location explanation, correct a filed item's destination, "
                    "and verify Magpie retains the correction for future decisions."
                    if not agent_correction_journey
                    else ""
                ),
                correction_action if not agent_correction_journey else None,
            ),
            _demo_check(
                "workspace_management_journey",
                "Recent use proves visual setup, cross-root movement, and lifecycle recovery",
                workspace_management_journey,
                ", ".join(
                    f"{name.replace('_', ' ')}={'yes' if passed else 'no'}"
                    for name, passed in management_evidence.items()
                ),
                (
                    "Create two storage spaces in Places, move an item between them, "
                    "then archive and restore the same test location."
                    if not workspace_management_journey
                    else ""
                ),
                management_action if not workspace_management_journey else None,
            ),
        ]

        product_checks_by_key = {
            str(check.get("key") or ""): check for check in product_checks
        }
        core_keys = (
            "qr_scan",
            "text_photo_putaway",
            "mobile_pwa",
            "learned_rule",
            "review_approval",
            "labels",
            "export",
            "build_visibility",
            "agent_control_plane",
            "mobile_https_origin",
            "shared_storage",
            "central_service",
        )
        workspace_keys = (
            "whole_storage_agent",
            "physical_storage_integrity",
        )
        enhancement_keys = (
            "voice_find",
            "voice_putaway_confirmation",
            "cabinet_hardware",
        )
        validation_keys = (
            "agent_storage_journeys",
            "agent_correction_journey",
            "workspace_management_journey",
        )

        def grouped_checks(
            keys: tuple[str, ...], *, group: str, required: bool
        ) -> list[dict[str, object]]:
            rows: list[dict[str, object]] = []
            for key in keys:
                check = product_checks_by_key.get(key) or demo_checks.get(key)
                if check is not None:
                    rows.append({**check, "group": group, "required": required})
            return rows

        core_checks = grouped_checks(core_keys, group="software", required=True)
        workspace_checks = grouped_checks(
            workspace_keys, group="workspace", required=True
        )
        edge_controller_checks = [
            {
                **product_checks_by_key[str(edge_service_metadata[service][0])],
                "group": "hardware",
                "required": bool(configured_edge_targets[service]),
            }
            for service in EDGE_SERVICE_REQUIREMENTS
        ]
        optional_hardware_checks = grouped_checks(
            enhancement_keys,
            group="hardware",
            required=False,
        )
        enhancement_checks = [
            *edge_controller_checks,
            *optional_hardware_checks,
        ]
        validation_checks = grouped_checks(
            validation_keys, group="validation", required=True
        )
        checks = [
            *core_checks,
            *workspace_checks,
            *enhancement_checks,
            *validation_checks,
        ]

        def check_summary(
            key: str, label: str, rows: list[dict[str, object]], *, required: bool
        ) -> dict[str, object]:
            passed = sum(1 for check in rows if check["passed"])
            return {
                "key": key,
                "label": label,
                "required": required,
                "ready": passed == len(rows),
                "passed": passed,
                "failed": len(rows) - passed,
                "total": len(rows),
            }

        groups = [
            check_summary("software", "Software", core_checks, required=True),
            check_summary(
                "workspace", "Storage setup", workspace_checks, required=True
            ),
            check_summary(
                "hardware",
                "Hardware enhancements",
                enhancement_checks,
                required=any(check["required"] for check in edge_controller_checks),
            ),
            check_summary(
                "validation", "Agent proof", validation_checks, required=True
            ),
        ]
        required_checks = [
            *core_checks,
            *workspace_checks,
            *validation_checks,
            *[check for check in enhancement_checks if check["required"]],
        ]
        passed = sum(1 for check in required_checks if check["passed"])
        return {
            **demo,
            "schema": "magpie.product_readiness.v8",
            "ready": passed == len(required_checks),
            "passed": passed,
            "failed": len(required_checks) - passed,
            "required_total": len(required_checks),
            "all_passed": sum(1 for check in checks if check["passed"]),
            "all_failed": sum(1 for check in checks if not check["passed"]),
            "checks": checks,
            "groups": groups,
            "agent": agent,
            "storage": storage,
            "public_origin": public_origin,
            "agent_storage_journeys": journey_evidence,
            "agent_correction_journey": correction_evidence,
            "workspace_management_journey": management_evidence,
            "physical_storage_checks": {
                "due": len(due_place_checks),
                "has_more": more_due_place_checks,
                "states": due_place_check_states,
            },
        }

    @app.get("/state")
    def state() -> dict[str, object]:
        return {"pixels": controller.pixels}

    @app.get("/assistant/status")
    def assistant_status() -> dict[str, object]:
        return _assistant_status_payload()

    @app.get("/assistant/interactions")
    def assistant_interactions(after_id: int = 0, limit: int = 20) -> dict[str, object]:
        interactions = _confirmation_interactions(
            store.assistant_interactions_after(after_id, limit=limit)
        )
        return {
            "ok": True,
            "interactions": interactions,
            "latest_id": interactions[-1]["id"] if interactions else after_id,
        }

    @app.get("/assistant/handoffs/{interaction_id}")
    def assistant_handoff(interaction_id: int) -> dict[str, object]:
        interaction = store.assistant_interaction(interaction_id)
        if interaction is None:
            raise HTTPException(status_code=404, detail="put-away handoff not found")
        response = interaction.get("response")
        decision = response.get("decision") if isinstance(response, dict) else None
        if (
            not isinstance(decision, dict)
            and isinstance(response, dict)
            and isinstance(response.get("handoff"), dict)
        ):
            decision = response
        handoff = decision.get("handoff") if isinstance(decision, dict) else None
        if (
            interaction.get("route") != "/assistant/chat"
            or not isinstance(handoff, dict)
            or handoff.get("kind") != "put_away_destination"
        ):
            raise HTTPException(
                status_code=409,
                detail="interaction is not a resumable put-away handoff",
            )
        photo_ids = [
            int(photo_id)
            for photo_id in interaction.get("photo_ids", [])
            if isinstance(photo_id, int)
        ]
        return {
            "ok": True,
            "interaction_id": interaction_id,
            "kind": "put_away_destination",
            "command": str(handoff.get("command") or interaction.get("command") or ""),
            "resume_command": str(handoff.get("resume_command") or ""),
            "photo_ids": photo_ids,
            "current_location_id": handoff.get("current_location_id"),
            "suggested_container": handoff.get("suggested_container"),
        }

    @app.get("/usage/agent")
    def agent_usage(response: Response, limit: int = 100) -> dict[str, object]:
        def recorded_value(value: object) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        response.headers["Cache-Control"] = "no-store"
        clean_limit = max(1, min(int(limit), 100))
        interactions = store.assistant_interactions_after(limit=clean_limit)
        totals = {field_name: 0 for field_name in LLM_USAGE_FIELDS}
        by_model: dict[tuple[str, str, str], dict[str, object]] = {}
        by_failure: dict[tuple[str, str, str], dict[str, object]] = {}
        metered_interactions = 0
        successful_interactions = 0
        failed_interactions = 0
        model_interactions = 0
        for interaction in interactions:
            response = interaction.get("response")
            response = response if isinstance(response, dict) else {}
            failure_category = str(response.get("failure_category") or "").strip()
            agent = response.get("agent")
            model_routed = bool(failure_category or isinstance(agent, dict))
            if model_routed:
                model_interactions += 1
            if failure_category:
                failed_interactions += 1
                failure_key = (
                    str(response.get("provider") or "unknown"),
                    str(response.get("model") or "unknown"),
                    failure_category,
                )
                failure = by_failure.setdefault(
                    failure_key,
                    {
                        "provider": failure_key[0],
                        "model": failure_key[1],
                        "category": failure_key[2],
                        "count": 0,
                        "latest_at": None,
                        "status_codes": set(),
                    },
                )
                failure["count"] = int(failure["count"]) + 1
                created_at = str(interaction.get("created_at") or "")
                if created_at:
                    failure["latest_at"] = created_at
                status_code = recorded_value(response.get("provider_status"))
                if status_code:
                    failure["status_codes"].add(status_code)
            if not isinstance(agent, dict):
                continue
            successful_interactions += 1
            usage = agent.get("usage")
            if not isinstance(usage, dict):
                continue
            metered_interactions += 1
            normalized = {
                field_name: recorded_value(usage.get(field_name))
                for field_name in LLM_USAGE_FIELDS
            }
            add_llm_usage(totals, normalized)
            key = (
                str(agent.get("provider") or "unknown"),
                str(agent.get("model") or "unknown"),
                str(agent.get("api") or "unknown"),
            )
            model = by_model.setdefault(
                key,
                {
                    "provider": key[0],
                    "model": key[1],
                    "api": key[2],
                    "interactions": 0,
                    **{field_name: 0 for field_name in LLM_USAGE_FIELDS},
                },
            )
            model["interactions"] = int(model["interactions"]) + 1
            for field_name in LLM_USAGE_FIELDS:
                model[field_name] = int(model[field_name]) + normalized[field_name]
        timestamps = [
            str(interaction.get("created_at") or "")
            for interaction in interactions
            if interaction.get("created_at")
        ]
        recorded_outcomes = successful_interactions + failed_interactions
        deterministic_interactions = max(0, len(interactions) - model_interactions)
        assistant_status = _assistant_status_payload()
        current_provider_failures = []
        for failure in assistant_status.get("provider_failures", []):
            if not isinstance(failure, dict):
                continue
            current_provider_failures.append(
                {
                    "provider": str(failure.get("provider") or "unknown"),
                    "model": str(failure.get("model") or "unknown"),
                    "category": str(failure.get("category") or "unknown"),
                    "status_code": recorded_value(failure.get("status_code")) or None,
                    "retry_in_seconds": recorded_value(failure.get("retry_in_seconds")),
                }
            )
        failures = []
        for failure in by_failure.values():
            failures.append(
                {
                    **failure,
                    "status_codes": sorted(failure["status_codes"]),
                }
            )
        return {
            "ok": True,
            "schema": "magpie.agent_usage.v2",
            "window": {
                "interaction_limit": clean_limit,
                "interaction_count": len(interactions),
                "metered_interactions": metered_interactions,
                "may_be_truncated": len(interactions) == clean_limit,
                "from": timestamps[0] if timestamps else None,
                "to": timestamps[-1] if timestamps else None,
            },
            "usage": totals,
            "routing": {
                "assistant_interactions": len(interactions),
                "deterministic_interactions": deterministic_interactions,
                "model_interactions": model_interactions,
                "model_interaction_rate": (
                    round(model_interactions / len(interactions), 3)
                    if interactions
                    else None
                ),
            },
            "models": sorted(
                by_model.values(), key=lambda model: int(model["requests"]), reverse=True
            ),
            "reliability": {
                "successful_interactions": successful_interactions,
                "failed_interactions": failed_interactions,
                "recorded_outcomes": recorded_outcomes,
                "failure_rate": (
                    round(failed_interactions / recorded_outcomes, 3)
                    if recorded_outcomes
                    else None
                ),
                "degraded": bool(assistant_status.get("degraded")),
                "current_provider_failures": current_provider_failures,
            },
            "failures": sorted(
                failures,
                key=lambda failure: (
                    int(failure["count"]),
                    str(failure["latest_at"] or ""),
                ),
                reverse=True,
            ),
        }

    @app.get("/usage/storage")
    def storage_usage(response: Response) -> dict[str, object]:
        """Report process-local storage traffic without making a storage read."""

        response.headers["Cache-Control"] = "no-store"
        backend = repository_bundle.backend
        snapshot: dict[str, object] | None = None
        if backend == "supabase":
            inventory_repository = repository_bundle.current().inventory
            client = getattr(inventory_repository, "client", None)
            snapshot_fn = getattr(client, "usage_snapshot", None)
            if callable(snapshot_fn):
                candidate = snapshot_fn()
                if isinstance(candidate, dict):
                    snapshot = candidate
        return {
            "ok": True,
            "schema": "magpie.storage_usage.v1",
            "backend": backend,
            "available": snapshot is not None,
            "observed": snapshot,
            "measurement": (
                "Logical Magpie requests and final response-body bytes observed "
                "since this service process started."
            ),
            "billing_authority": (
                "Supabase Usage is authoritative for billed egress, retries, and "
                "protocol overhead."
            ),
        }

    @app.get("/kiosk/state")
    async def kiosk_state(
        response: Response,
        after_id: int = 0,
        confirmation_interaction_id: int = 0,
        voice_updated_at: float = -1,
        wait_seconds: float = 20,
        screen_id: str = "",
        screen_url: str = "",
        build_label: str = "",
        status: str = "active",
        last_seen_interaction_id: int = 0,
    ) -> dict[str, object]:
        """Return voice and assistant changes, waiting until either changes.

        The kiosk keeps one request open instead of polling voice and interaction
        endpoints independently. A zero wait is useful for diagnostics and tests.
        """

        response.headers["Cache-Control"] = "no-store"
        clean_after_id = max(0, int(after_id))
        clean_confirmation_id = max(0, int(confirmation_interaction_id))
        clean_wait = max(0.0, min(float(wait_seconds), 25.0))
        if screen_id.strip():
            _screen_state().update(
                {
                    "screen_id": screen_id.strip(),
                    "url": screen_url.strip(),
                    "build_label": build_label.strip(),
                    "last_seen_interaction_id": max(
                        0, int(last_seen_interaction_id or 0)
                    ),
                    "status": status.strip() or "active",
                    "updated_at": time.time(),
                }
            )

        workspace_id = _state_workspace_id()

        def interaction_rows() -> list[dict[str, object]]:
            now = time.monotonic()
            cache_key = (workspace_id, clean_after_id)
            cached = kiosk_interaction_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return [dict(interaction) for interaction in cached[1]]
            for key, (expires_at, _) in tuple(kiosk_interaction_cache.items()):
                if expires_at <= now:
                    kiosk_interaction_cache.pop(key, None)
            interactions = store.assistant_interactions_after(
                clean_after_id,
                limit=10,
            )
            kiosk_interaction_cache[cache_key] = (
                now + KIOSK_IDLE_CACHE_SECONDS,
                [dict(interaction) for interaction in interactions],
            )
            return [dict(interaction) for interaction in interactions]

        def storage_readiness() -> dict[str, object]:
            now = time.monotonic()
            cached = kiosk_storage_readiness_cache.get(workspace_id)
            if cached is not None and cached[0] > now:
                return dict(cached[1])
            for cached_workspace_id, (expires_at, _) in tuple(
                kiosk_storage_readiness_cache.items()
            ):
                if expires_at <= now:
                    kiosk_storage_readiness_cache.pop(cached_workspace_id, None)
            active_locations = locations.all()
            destination_count = sum(
                1 for location in active_locations if location.accepts_items
            )
            active_ids = {location.id for location in active_locations}
            root_count = sum(
                1
                for location in active_locations
                if not location.parent_id or location.parent_id not in active_ids
            )
            readiness = {
                "schema": "magpie.kiosk_storage.v1",
                "configured": bool(active_locations),
                "put_away_ready": destination_count > 0,
                "location_count": len(active_locations),
                "destination_count": destination_count,
                "root_count": root_count,
                "setup_path": (
                    "/storage?view=places&onboarding=destination"
                    if active_locations
                    else "/storage?onboarding=1"
                ),
            }
            kiosk_storage_readiness_cache[workspace_id] = (
                now + 30.0,
                readiness,
            )
            return dict(readiness)

        def snapshot(*, timed_out: bool = False) -> dict[str, object]:
            interactions = _confirmation_interactions(
                interaction_rows()
            )
            voice = _voice_status_payload()["voice"]
            service_build_label = build.label()
            pending_agent_plan = _confirmable_agent_plan(
                _cached_pending_agent_plan(clean_after_id)
            )
            pending_confirmation = (
                pending_agent_plan
                if _voice_confirmable_plan(pending_agent_plan)
                else None
            )
            confirmation_state: dict[str, object] | None = None
            if clean_confirmation_id:
                if (
                    pending_agent_plan is not None
                    and int(pending_agent_plan.get("id") or 0)
                    == clean_confirmation_id
                ):
                    confirmation_state = {
                        "interaction_id": clean_confirmation_id,
                        "status": "pending",
                        "confirmable": True,
                    }
                else:
                    requested = store.assistant_interaction(clean_confirmation_id)
                    if requested is None:
                        confirmation_state = {
                            "interaction_id": clean_confirmation_id,
                            "status": "unknown",
                            "confirmable": False,
                        }
                    else:
                        requested = _confirmation_metadata(requested)
                        confirmation_state = {
                            "interaction_id": clean_confirmation_id,
                            "status": str(requested.get("status") or "unknown"),
                            "confirmable": bool(requested.get("confirmable")),
                        }
                    if pending_agent_plan is not None:
                        confirmation_state["replaced_by_interaction_id"] = int(
                            pending_agent_plan.get("id") or 0
                        )
            return {
                "ok": True,
                "voice": voice,
                "storage": storage_readiness(),
                "interactions": interactions,
                "pending_agent_plan": pending_agent_plan,
                "pending_confirmation": pending_confirmation,
                "confirmation_state": confirmation_state,
                "latest_id": (
                    interactions[-1]["id"] if interactions else clean_after_id
                ),
                "timed_out": timed_out,
                "service_build_label": service_build_label,
                "reload_required": bool(
                    build_label.strip()
                    and build_label.strip() != service_build_label
                ),
            }

        observed_generation = kiosk_state_generations.get(workspace_id, 0)
        payload = snapshot()
        current_voice_updated_at = float(payload["voice"].get("updated_at") or 0.0)
        confirmation_state = payload.get("confirmation_state")
        confirmation_changed = bool(
            isinstance(confirmation_state, dict)
            and (
                confirmation_state.get("status")
                not in {"pending", "confirming", "unknown"}
                or confirmation_state.get("replaced_by_interaction_id")
            )
        )
        if (
            clean_wait == 0
            or voice_updated_at < 0
            or current_voice_updated_at != float(voice_updated_at)
            or payload["interactions"]
            or confirmation_changed
        ):
            return payload

        kiosk_state_event = kiosk_state_events.setdefault(
            workspace_id,
            asyncio.Event(),
        )
        kiosk_state_event.clear()
        if kiosk_state_generations.get(workspace_id, 0) == observed_generation:
            try:
                await asyncio.wait_for(kiosk_state_event.wait(), timeout=clean_wait)
            except TimeoutError:
                return snapshot(timed_out=True)
        return snapshot()

    def _learning_example_scenarios(
        *,
        action: object,
        kind: str,
        context: dict[str, object],
    ) -> list[str]:
        tools = set(_plan_tools(action)) if isinstance(action, list) else set()
        scenarios: list[str] = []
        if tools & {"put_item", "move_item", "remove_item"}:
            scenarios.append("putaway")
        if tools & {
            "learn_alias",
            "learn_rule",
            "learn_size_rule",
            "update_rule",
            "update_size_rule",
            "forget_rule",
        }:
            scenarios.append("rules")
        if kind in {
            "correction",
            "item_correction",
            "review_correction",
            "edited_photo_sort",
        }:
            scenarios.append("correction")
        action_photo_ids = (
            action.get("photo_ids") if isinstance(action, dict) else None
        )
        if context.get("photo_ids") or action_photo_ids:
            scenarios.append("photo")
        if tools & {
            "create_location",
            "move_container",
            "update_location",
            "archive_location",
            "restore_location",
            "print_label",
        }:
            scenarios.append("organization")
        return scenarios

    def _learning_examples_payload(
        limit: int = 25,
        kind: str | None = None,
        review_status: str | None = None,
        include_rejected: bool = False,
        scenario: str | None = None,
    ) -> dict[str, object]:
        clean_scenario = str(scenario or "").strip().lower()
        allowed_scenarios = {
            "putaway",
            "rules",
            "correction",
            "photo",
            "organization",
        }
        if clean_scenario and clean_scenario not in allowed_scenarios:
            raise HTTPException(status_code=400, detail="invalid learning scenario")
        requested_limit = _eval_limit(limit)
        rows = store.examples_for_eval(
            limit=500 if clean_scenario else min(requested_limit + 1, 500),
            kind=kind,
            review_status=review_status,
            include_rejected=include_rejected,
        )
        examples: list[dict[str, object]] = []
        for row in rows:
            raw_action = str(row.get("action") or "")
            parsed_action: object | None = None
            parse_error: str | None = None
            raw_context = row.get("context_json")
            parsed_context: object | None = None
            context_parse_error: str | None = None
            try:
                parsed_action = json.loads(raw_action)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
            if isinstance(raw_context, dict):
                parsed_context = raw_context
            else:
                try:
                    parsed_context = json.loads(str(raw_context or "{}"))
                except json.JSONDecodeError as exc:
                    context_parse_error = str(exc)
            example: dict[str, object] = {
                "id": row["id"],
                "command": row["command"],
                "kind": row["kind"],
                "created_at": row["created_at"],
                "review_status": row.get("review_status") or "pending",
                "reviewed_at": row.get("reviewed_at"),
                "action": raw_action,
            }
            if isinstance(parsed_context, dict):
                example["context"] = parsed_context
            if parsed_action is not None:
                example["parsed_action"] = parsed_action
                scenarios = _learning_example_scenarios(
                    action=parsed_action,
                    kind=str(row["kind"]),
                    context=parsed_context if isinstance(parsed_context, dict) else {},
                )
                if clean_scenario and clean_scenario not in scenarios:
                    continue
                example["scenarios"] = scenarios
                example["promotable"] = (
                    example["review_status"] != "promoted"
                    and bool(_promotable_learning_steps(parsed_action))
                )
                example["forgettable"] = (
                    example["review_status"] == "promoted"
                    and isinstance(parsed_action, list)
                    and any(
                        isinstance(step, dict)
                        and isinstance(step.get("arguments"), dict)
                        and bool(step["arguments"].get("provisional"))
                        and step["arguments"].get("rule_id") is not None
                        for step in parsed_action
                    )
                )
            elif clean_scenario:
                continue
            if parse_error:
                example["parse_error"] = parse_error
            if context_parse_error:
                example["context_parse_error"] = context_parse_error
            examples.append(example)
            if len(examples) > requested_limit:
                break
        has_more = len(examples) > requested_limit
        examples = examples[:requested_limit]
        return {
            "ok": True,
            "count": len(examples),
            "scenario": clean_scenario or None,
            "examples": examples,
            "has_more": has_more,
        }

    @app.get("/assistant/learning/examples")
    def assistant_learning_examples(
        limit: int = 25,
        kind: str | None = None,
        review_status: str | None = None,
        include_rejected: bool = False,
        scenario: str | None = None,
    ) -> dict[str, object]:
        return _learning_examples_payload(
            limit=limit,
            kind=kind,
            review_status=review_status,
            include_rejected=include_rejected,
            scenario=scenario,
        )

    @app.get("/assistant/learning/summary")
    def assistant_learning_summary() -> dict[str, object]:
        return {
            "ok": True,
            "review_counts": store.example_review_counts(),
            "kind_counts": store.example_kind_counts(),
        }

    @app.get("/assistant/learning/adherence")
    def assistant_learning_adherence(limit: int = 100) -> dict[str, object]:
        evaluations: list[dict[str, object]] = []
        for interaction in store.assistant_interactions_after(limit=max(1, min(limit, 100))):
            response = interaction.get("response")
            if not isinstance(response, dict):
                continue
            learning = response.get("learning")
            if not isinstance(learning, dict):
                continue
            for evaluation in learning.get("rule_adherence", []):
                if not isinstance(evaluation, dict):
                    continue
                evaluations.append(
                    {
                        **evaluation,
                        "interaction_id": interaction.get("id"),
                        "command": interaction.get("command"),
                        "created_at": interaction.get("created_at"),
                    }
                )
        followed = sum(1 for evaluation in evaluations if evaluation.get("followed") is True)
        return {
            "ok": True,
            "count": len(evaluations),
            "followed": followed,
            "not_followed": len(evaluations) - followed,
            "rate": followed / len(evaluations) if evaluations else None,
            "evaluations": evaluations,
        }

    def _promotable_learning_steps(action: object) -> list[dict[str, object]]:
        if not isinstance(action, list):
            return []
        steps: list[dict[str, object]] = []
        for step in action:
            if not isinstance(step, dict):
                continue
            if step.get("tool") not in {"learn_alias", "learn_rule", "learn_size_rule"}:
                continue
            args = step.get("arguments")
            if not isinstance(args, dict):
                continue
            steps.append({"tool": step["tool"], "arguments": dict(args)})
        return steps

    def _record_applied_learning_example(
        *,
        command: str,
        action: list[dict[str, object]],
        kind: str,
        context: dict[str, object] | None = None,
    ) -> tuple[int | None, str]:
        example_id = store.record_example(
            command=command,
            action=json.dumps(action, sort_keys=True),
            kind=kind,
            context=context,
        )
        if example_id is not None and store.mark_example_promoted(example_id):
            return example_id, "promoted"
        return example_id, "pending"

    @app.post("/assistant/learning/examples/{example_id}/promote")
    def promote_learning_example(example_id: int) -> dict[str, object]:
        row = store.example(example_id)
        if row is None:
            raise HTTPException(status_code=404, detail="learning example not found")
        if row.get("review_status") == "promoted":
            raise HTTPException(status_code=409, detail="learning example is already promoted")
        if row.get("review_status") == "rejected":
            raise HTTPException(status_code=409, detail="learning example is rejected")
        try:
            action = json.loads(str(row.get("action") or ""))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="learning example action is not valid JSON") from exc
        steps = _promotable_learning_steps(action)
        if not steps:
            raise HTTPException(status_code=409, detail="learning example has no explicit learn action")
        command = str(row.get("command") or "")
        for step in steps:
            args = step.setdefault("arguments", {})
            if isinstance(args, dict) and not args.get("source_command"):
                args["source_command"] = command
        try:
            plan = normalize_plan(
                steps,
                locations=locations,
                store=store,
                layout=layout,
                command=command,
            )
            applied = execute_plan(plan, store=store, locations=locations)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.mark_example_promoted(example_id)
        return {
            "ok": True,
            "promoted": example_id,
            "applied": applied,
        }

    @app.delete("/assistant/learning/examples/{example_id}")
    def reject_learning_example(example_id: int) -> dict[str, object]:
        row = store.example(example_id)
        if row is None:
            raise HTTPException(status_code=404, detail="learning example not found")
        if not store.reject_example(example_id):
            raise HTTPException(status_code=404, detail="learning example not found")
        rolled_back: list[dict[str, object]] = []
        try:
            action = json.loads(str(row.get("action") or ""))
        except json.JSONDecodeError:
            action = []
        for step in action if isinstance(action, list) else []:
            if not isinstance(step, dict):
                continue
            args = step.get("arguments")
            if not isinstance(args, dict) or not args.get("provisional"):
                continue
            rule_type = {
                "learn_rule": "placement",
                "learn_size_rule": "size",
            }.get(str(step.get("tool") or ""))
            try:
                rule_id = int(args.get("rule_id"))
            except (TypeError, ValueError):
                continue
            if rule_type and store.deactivate_rule(rule_id=rule_id, rule_type=rule_type):
                rolled_back.append({"rule_id": rule_id, "rule_type": rule_type})
        return {"ok": True, "rejected": example_id, "rolled_back": rolled_back}

    @app.post("/assistant/learning/examples/{example_id}/approve")
    def approve_learning_example(example_id: int) -> dict[str, object]:
        row = store.example(example_id)
        if row is None:
            raise HTTPException(status_code=404, detail="learning example not found")
        if row.get("review_status") == "rejected":
            raise HTTPException(status_code=409, detail="learning example is rejected")
        if row.get("review_status") == "promoted":
            raise HTTPException(status_code=409, detail="learning example is promoted")
        if not store.approve_example(example_id):
            raise HTTPException(status_code=404, detail="learning example not found")
        return {"ok": True, "approved": example_id}

    @app.post("/assistant/learning/examples/bulk-approve")
    def bulk_approve_learning_examples(request: LearningBulkApproveRequest) -> dict[str, object]:
        seen: set[int] = set()
        approved: list[int] = []
        skipped: list[dict[str, object]] = []
        for raw_id in request.example_ids[:100]:
            example_id = int(raw_id)
            if example_id in seen:
                continue
            seen.add(example_id)
            row = store.example(example_id)
            if row is None:
                skipped.append({"id": example_id, "reason": "not_found"})
                continue
            status = str(row.get("review_status") or "pending")
            if status != "pending":
                skipped.append({"id": example_id, "reason": status})
                continue
            if store.approve_example(example_id):
                approved.append(example_id)
            else:
                skipped.append({"id": example_id, "reason": "not_found"})
        return {
            "ok": True,
            "approved": approved,
            "approved_count": len(approved),
            "skipped": skipped,
        }

    def _voice_backend_interaction_id(response: dict[str, object]) -> int | None:
        for key in (
            "confirmation_interaction_id",
            "interaction_id",
            "source_interaction_id",
        ):
            raw_id = response.get(key)
            if isinstance(raw_id, int) and raw_id > 0:
                return raw_id
        return None

    @app.post("/voice/attempts")
    def record_voice_attempt(request: VoiceAttemptRequest) -> dict[str, object]:
        backend_response = request.backend_response or {}
        attempt_id = store.record_voice_attempt(
            rms=request.rms,
            activity_rms=request.activity_rms,
            transcript=request.transcript,
            wake_word=request.wake_word,
            wake_heard=request.wake_heard,
            awaiting_command=request.awaiting_command,
            command=request.command,
            route=request.route,
            query=request.query,
            stt_model=request.stt_model,
            audio_threshold=request.audio_threshold,
            device_id=request.device_id,
            runtime_version=request.runtime_version,
            backend_response=backend_response,
            error=request.error,
        )
        linked_interaction_id = _voice_backend_interaction_id(backend_response)
        linked = False
        if linked_interaction_id is not None:
            linked = store.update_assistant_interaction_response(
                linked_interaction_id,
                {
                    "voice_attempt": {
                        "id": attempt_id,
                        "transcript": request.transcript.strip(),
                        "wake_word": request.wake_word.strip(),
                        "wake_heard": request.wake_heard,
                        "awaiting_command": request.awaiting_command,
                        "command": request.command.strip(),
                        "route": request.route.strip(),
                        "query": request.query.strip(),
                        "stt_model": request.stt_model.strip(),
                        "rms": request.rms,
                        "activity_rms": request.activity_rms,
                        "audio_threshold": request.audio_threshold,
                        "device_id": request.device_id.strip(),
                        "runtime_version": request.runtime_version.strip(),
                        "error": request.error.strip(),
                    }
                },
            )
        return {
            "ok": True,
            "attempt_id": attempt_id,
            "linked_interaction_id": linked_interaction_id,
            "linked": linked,
        }

    @app.get("/voice/recent")
    def recent_voice_attempts(limit: int = 20) -> dict[str, object]:
        attempts = store.recent_voice_attempts(limit=limit)
        return {"ok": True, "attempts": _voice_attempts_with_tuning(attempts)}

    def _voice_response_value(response: dict[str, object], *path: str) -> object:
        current: object = response
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _voice_lit(response: dict[str, object]) -> bool:
        return any(
            bool(_voice_response_value(response, *path))
            for path in (
                ("lit",),
                ("automation", "lit"),
                ("decision", "lit"),
                ("raw", "lit"),
                ("raw", "automation", "lit"),
            )
        )

    def _voice_action(response: dict[str, object]) -> str:
        for path in (
            ("action",),
            ("mode",),
            ("decision", "action"),
            ("raw", "action"),
            ("raw", "mode"),
        ):
            value = _voice_response_value(response, *path)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _voice_attempt_tuning(
        attempt: dict[str, object],
        interactions: dict[int, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        backend_response = attempt.get("backend_response")
        if not isinstance(backend_response, dict):
            backend_response = {}
        error = str(attempt.get("error") or "").strip()
        route = str(attempt.get("route") or "").strip()
        query = str(attempt.get("query") or "").strip()
        command = str(attempt.get("command") or "").strip()
        transcript = str(attempt.get("transcript") or "").strip()
        threshold = float(attempt.get("audio_threshold") or 0)
        rms_raw = attempt.get("rms")
        rms = float(rms_raw) if isinstance(rms_raw, (int, float)) else None
        activity_raw = attempt.get("activity_rms")
        activity_rms = (
            float(activity_raw)
            if isinstance(activity_raw, (int, float))
            else rms
        )
        audio_ok = bool(transcript) or (
            activity_rms is not None and (not threshold or activity_rms >= threshold)
        )
        wake_heard = bool(attempt.get("wake_heard"))
        awaiting_command = bool(attempt.get("awaiting_command"))
        routed = bool(route and (query or command))
        backend_ok = bool(backend_response) and not error and backend_response.get("ok", True) is not False
        linked_interaction_id = _voice_backend_interaction_id(backend_response)
        lit = _voice_lit(backend_response)
        action = _voice_action(backend_response)
        screen = _screen_status_payload()["screen"]
        linked_interaction = (
            interactions.get(linked_interaction_id)
            if interactions is not None and linked_interaction_id is not None
            else store.assistant_interaction(linked_interaction_id)
            if linked_interaction_id is not None
            else None
        )
        linked_response = (
            linked_interaction.get("response")
            if isinstance(linked_interaction, dict)
            else None
        )
        observation = (
            linked_response.get("screen_observed")
            if isinstance(linked_response, dict)
            else None
        )
        screen_seen_interaction = isinstance(observation, dict)
        checks = {
            "audio": audio_ok,
            "wake_or_armed": wake_heard or awaiting_command,
            "command": bool(command or query),
            "routed": routed,
            "backend": backend_ok,
            "screen_linked": linked_interaction_id is not None,
            "screen_active": screen_seen_interaction,
            "screen_observed": screen_seen_interaction,
        }
        if route or action:
            checks["light"] = lit if re.search(r"\b(find|put|store|file|stash|stow)\b", f"{route} {action}", re.I) else True
        passed = all(checks.values()) and not error
        return {
            "passed": passed,
            "checks": checks,
            "rms": rms,
            "activity_rms": activity_rms,
            "audio_threshold": threshold,
            "transcript": transcript,
            "command": command,
            "route": route,
            "query": query,
            "action": action,
            "lit": lit,
            "linked_interaction_id": linked_interaction_id,
            "screen": screen,
            "screen_observation": observation if screen_seen_interaction else None,
            "screen_seen_interaction": screen_seen_interaction,
            "message": str(
                _voice_response_value(backend_response, "message")
                or _voice_response_value(backend_response, "raw", "message")
                or ""
            ),
        }

    def _voice_attempt_with_tuning(
        attempt: dict[str, object],
        interactions: dict[int, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        enriched = dict(attempt)
        enriched["tuning"] = _voice_attempt_tuning(enriched, interactions)
        return enriched

    def _voice_attempts_with_tuning(
        attempts: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        interaction_ids = sorted(
            {
                interaction_id
                for attempt in attempts
                for interaction_id in [
                    _voice_backend_interaction_id(
                        attempt.get("backend_response")
                        if isinstance(attempt.get("backend_response"), dict)
                        else {}
                    )
                ]
                if interaction_id is not None
            }
        )
        interactions = {
            int(interaction["id"]): interaction
            for interaction in store.assistant_interactions_by_ids(interaction_ids)
        }
        return [
            _voice_attempt_with_tuning(attempt, interactions) for attempt in attempts
        ]

    @app.get("/voice/tuning")
    def voice_tuning(limit: int = 20) -> dict[str, object]:
        attempts = _voice_attempts_with_tuning(
            store.recent_voice_command_attempts(limit=limit)
        )
        passed = sum(1 for attempt in attempts if attempt["tuning"]["passed"])
        failed = len(attempts) - passed
        routed = sum(
            1 for attempt in attempts if attempt["tuning"]["checks"].get("routed")
        )
        backend_succeeded = sum(
            1 for attempt in attempts if attempt["tuning"]["checks"].get("backend")
        )
        wake_only = sum(
            1
            for attempt in attempts
            if attempt.get("wake_heard")
            and not (attempt.get("command") or attempt.get("query"))
        )
        errors = sum(1 for attempt in attempts if str(attempt.get("error") or "").strip())

        def rate(numerator: int, denominator: int) -> float | None:
            return round(numerator / denominator, 3) if denominator else None

        return {
            "ok": True,
            "count": len(attempts),
            "passed": passed,
            "failed": failed,
            "summary": {
                "activations": len(attempts),
                "routed": routed,
                "routing_rate": rate(routed, len(attempts)),
                "backend_succeeded": backend_succeeded,
                "backend_success_rate": rate(backend_succeeded, routed),
                "passed_end_to_end": passed,
                "end_to_end_rate": rate(passed, len(attempts)),
                "wake_only": wake_only,
                "errors": errors,
            },
            "attempts": attempts,
        }

    def _voice_calibration_summary(attempts: list[dict[str, object]]) -> dict[str, object]:
        quiet_rms: list[float] = []
        speech_rms: list[float] = []
        unclear_rms: list[float] = []
        ambient_rms: list[float] = []
        thresholds: list[float] = []
        for attempt in attempts:
            tuning = attempt.get("tuning")
            if not isinstance(tuning, dict):
                tuning = _voice_attempt_tuning(attempt)
            rms_raw = tuning.get("activity_rms")
            if not isinstance(rms_raw, (int, float)):
                continue
            rms = float(rms_raw)
            threshold = tuning.get("audio_threshold")
            if not isinstance(threshold, (int, float)) or float(threshold) <= 0:
                continue
            thresholds.append(float(threshold))
            checks = tuning.get("checks") if isinstance(tuning.get("checks"), dict) else {}
            has_command = bool(tuning.get("command") or tuning.get("query"))
            error = str(attempt.get("error") or "").strip()
            if error == "quiet" or checks.get("audio") is False:
                quiet_rms.append(rms)
            elif error == "no_transcript" or (
                checks.get("audio") is True and not has_command and not tuning.get("transcript")
            ):
                unclear_rms.append(rms)
            elif checks.get("audio") is True and not has_command:
                ambient_rms.append(rms)
            elif has_command:
                speech_rms.append(rms)

        current_threshold = thresholds[-1] if thresholds else 0.0
        quiet_floor = max(quiet_rms) if quiet_rms else None
        speech_floor = min(speech_rms) if speech_rms else None
        suggested_threshold = None
        next_step = "Capture a quiet clip and a spoken command."
        ready = False
        if quiet_floor is None:
            next_step = "Capture a quiet-room clip so Magpie knows the noise floor."
        elif speech_floor is None:
            if unclear_rms:
                next_step = "Audio is crossing the threshold but STT is not returning speech; move closer, reduce noise, or compare STT models."
            else:
                next_step = "Say a real command near the cabinet to capture speech RMS."
        elif speech_floor <= quiet_floor:
            next_step = "Speech is not clearly above the quiet floor; adjust mic gain or placement."
        else:
            suggested_threshold = round((quiet_floor + speech_floor) / 2)
            ready = True
            if quiet_floor < current_threshold < speech_floor:
                next_step = "Say 'Magpie, find M4 T-nuts' near the cabinet to run the live acceptance test."
            else:
                next_step = "Set MAGPIE_VOICE_AUDIO_THRESHOLD near the suggested threshold and retest."
        return {
            "ready": ready,
            "current_threshold": current_threshold,
            "suggested_threshold": suggested_threshold,
            "quiet_samples": len(quiet_rms),
            "speech_samples": len(speech_rms),
            "unclear_samples": len(unclear_rms),
            "ambient_samples": len(ambient_rms),
            "quiet_floor_rms": quiet_floor,
            "speech_floor_rms": speech_floor,
            "unclear_floor_rms": min(unclear_rms) if unclear_rms else None,
            "ambient_floor_rms": min(ambient_rms) if ambient_rms else None,
            "next_step": next_step,
        }

    @app.get("/voice/calibration")
    def voice_calibration(limit: int = 50) -> dict[str, object]:
        attempts = _voice_attempts_with_tuning(store.recent_voice_attempts(limit=limit))
        return {
            "ok": True,
            "count": len(attempts),
            "calibration": _voice_calibration_summary(attempts),
        }

    @app.post("/photos")
    def upload_photo(request: PhotoUploadRequest) -> dict[str, object]:
        try:
            encoded = request.data_base64.split(",", 1)[-1]
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid base64 photo data") from exc
        if not data:
            raise HTTPException(status_code=400, detail="photo data is required")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="photo must be 10 MB or smaller")
        photo = store.add_photo(
            filename=request.filename,
            content_type=request.content_type,
            data=data,
        )
        return {"ok": True, "photo": photo.as_dict()}

    @app.get("/measurement/calibration-grid.pdf")
    def calibration_grid_pdf() -> Response:
        return Response(
            content=render_calibration_grid_pdf(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'inline; filename="magpie-measurement-grid.pdf"',
                "Cache-Control": "public, max-age=86400",
            },
        )

    @app.post("/measurement/photo")
    def measure_photo(request: PhotoMeasurementRequest) -> dict[str, object]:
        if not request.photo_ids:
            raise HTTPException(status_code=400, detail="at least one photo is required")
        photos = []
        for photo_id in list(dict.fromkeys(request.photo_ids))[:2]:
            photo = store.photo(photo_id)
            if photo is None:
                raise HTTPException(status_code=404, detail=f"photo {photo_id} not found")
            photos.append(photo)
        try:
            measurement = measure_calibrated_item(
                item=request.item,
                photos=photos,
                client=agent_llm_client,
                photo_loader=store.photo_content,
            )
        except LlmError as exc:
            failure_messages = {
                "authentication": "The measurement model credentials need attention.",
                "quota_exhausted": "The measurement model has no available credits.",
                "rate_limited": "The measurement model is temporarily rate limited.",
                "provider_unavailable": "The measurement model is temporarily unavailable.",
            }
            raise HTTPException(
                status_code=503,
                detail=failure_messages.get(
                    exc.category,
                    "Magpie could not measure that photo.",
                ),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        measured_size = measured_capacity_for(
            measurement.get("dimensions"),
            locations=locations,
        )
        if measured_size is None:
            measured_size = item_capacity_for(request.item, store=store)
        return {
            "ok": bool(measurement["reference_detected"]),
            **measurement,
            "size_class": measured_size[0],
            "size_source": (
                "measurement" if measured_size[1].startswith("measured") else "inference"
            ),
        }

    @app.get("/photos/{photo_id}")
    def get_photo(photo_id: int) -> Response:
        photo = store.photo(photo_id)
        if photo is None:
            raise HTTPException(status_code=404, detail="photo not found")
        if Path(photo.path).exists():
            return FileResponse(
                photo.path,
                media_type=photo.content_type,
                filename=photo.filename,
            )
        try:
            content = store.photo_content(photo_id)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=404, detail="photo not found") from exc
        return Response(
            content=content,
            media_type=photo.content_type,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(photo.filename)}"
            },
        )

    @app.delete("/photos/{photo_id}")
    def remove_photo(photo_id: int) -> dict[str, object]:
        store.delete_photo(photo_id)
        return {"ok": True}

    @app.post("/clear")
    def clear() -> dict[str, object]:
        hardware_errors = _clear_active_finds()
        return {
            "ok": True,
            "active_finds": [],
            "light_ok": not hardware_errors,
            "light_errors": hardware_errors,
            "message": (
                "Current finds and lights cleared."
                if not hardware_errors
                else (
                    "Current finds cleared. The light controller did not "
                    "confirm the update."
                )
            ),
        }

    @app.get("/lights/active")
    def active_lights() -> dict[str, object]:
        return {"ok": True, "active_finds": _active_find_snapshot()}

    @app.post("/strip")
    def fill_strip(request: ColorRequest) -> dict[str, object]:
        _require_local_light_controller().fill(
            request.color, brightness=request.brightness
        )
        return {"ok": True}

    @app.post("/pixels/{index}")
    def set_pixel(index: int, request: PixelRequest) -> dict[str, object]:
        try:
            _require_local_light_controller().set_pixel(
                index,
                request.color,
                clear_others=request.clear_others,
                brightness=request.brightness,
            )
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "index": index}

    @app.post("/pixels/{index}/flash")
    def flash_pixel(index: int, request: FlashRequest) -> dict[str, object]:
        try:
            _require_local_light_controller().flash_pixel(
                index,
                request.color,
                seconds=request.seconds,
                brightness=request.brightness,
            )
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "index": index}

    @app.post("/pixels")
    def set_pixels(request: PixelBatchRequest) -> dict[str, object]:
        try:
            _require_local_light_controller().set_pixels(
                [
                    PixelUpdate(index=index, color=color)
                    for index, color in request.pixels
                ],
                clear_others=request.clear_others,
                brightness=request.brightness,
            )
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "count": len(request.pixels)}

    @app.get("/layout")
    def get_layout() -> dict[str, object]:
        return layout.as_dict()

    @app.get("/bins")
    def get_bins() -> dict[str, object]:
        return {"bins": [bin.as_dict() for bin in layout.bins]}

    @app.get("/bins/{bin_id}")
    def get_bin(bin_id: str) -> dict[str, object]:
        bin = layout.get(bin_id)
        if bin is None:
            raise HTTPException(status_code=404, detail="bin not found")
        return {
            "bin": bin.as_dict(),
            "inventory": [item.as_dict() for item in store.bin_inventory(bin.id)],
        }

    @app.post("/bins/{bin_id}/flash")
    def flash_bin(bin_id: str, request: BinFlashRequest) -> dict[str, object]:
        bin = layout.get(bin_id)
        if bin is None:
            raise HTTPException(status_code=404, detail="bin not found")
        lit_location = locations.nearest_lit(bin_id)
        lit = bool(
            lit_location
            and _light_pixels(
                lit_location.light_indexes,
                seconds=request.seconds,
                color=request.color,
                brightness=request.brightness,
                target=str(lit_location.light_target or ""),
            )
        )
        return {
            "ok": True,
            "bin_id": bin.id,
            "lit": lit,
            "lit_location": lit_location.id if lit_location else None,
            "light_target": (
                str(lit_location.light_target or "") if lit_location else None
            ),
            "led_indexes": lit_location.light_indexes if lit_location else [],
            "seconds": request.seconds,
        }

    @app.post("/inventory")
    def put_inventory(request: InventoryPutRequest) -> dict[str, object]:
        target = _require_inventory_destination(request.bin_id)
        try:
            item_id = store.upsert_item(
                name=request.name,
                description=request.description,
                category=request.category,
                aliases=request.aliases,
                photo_ids=request.photo_ids,
            )
            previous = next(
                (row for row in store.item_inventory(item_id) if row.bin_id == target),
                None,
            )
            store.put_inventory(
                item_id=item_id,
                bin_id=target,
                quantity=request.quantity,
                unit=request.unit,
                normalize_bin=False,
            )
            if request.size_class:
                store.update_item_size(
                    item_id=item_id,
                    size_class=request.size_class,
                    source=request.size_source,
                    confidence=request.size_confidence,
                    dimensions=request.dimensions,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        applied = [{
            "stored": request.name,
            "item_id": item_id,
            "location_id": target,
            "previous_quantity": previous.quantity if previous is not None else None,
            "previous_unit": previous.unit if previous is not None else None,
            "quantity": request.quantity,
        }]
        lit_location = locations.nearest_lit(target)
        lit = (
            _save_light_effect(
                lit_location.light_indexes,
                seconds=SAVE_ACK_SECONDS,
                target=str(lit_location.light_target or ""),
            )
            if lit_location
            else False
        )
        response = {
            "ok": True,
            "item_id": item_id,
            "bin_id": target,
            "applied": applied,
            "lit": lit,
            "lit_location": lit_location.id if lit_location else None,
            "light_effect": "save-pulse" if lit else None,
            "light_seconds": SAVE_ACK_SECONDS if lit else None,
        }
        if lit and _raw_active_find_snapshot():
            _restore_active_lights_after(SAVE_ACK_SECONDS)
        interaction_id = _record_manual_inventory_interaction(
            command=f"manual put {request.name} in {target}",
            response={
                **response,
                "action": "manual_put_inventory",
                "item": request.name,
                "quantity": request.quantity,
                "unit": request.unit,
            },
            photo_ids=request.photo_ids,
        )
        _record_manual_example(
            command=f"manual put {request.name} in {target}",
            plan=[
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": request.name,
                        "location_id": target,
                        "quantity": request.quantity,
                        "unit": request.unit,
                    },
                }
            ],
        )
        response["interaction_id"] = interaction_id
        return response

    def _inventory_move_plan(
        entries: list[InventoryBulkMoveEntry], to_bin: str
    ) -> tuple[str, list[dict[str, object]], int]:
        if not entries:
            raise HTTPException(
                status_code=400,
                detail="select at least one inventory placement",
            )
        if len(entries) > 100:
            raise HTTPException(
                status_code=400,
                detail="bulk moves are limited to 100 placements",
            )
        dst = _require_inventory_destination(
            to_bin,
            unknown_detail="unknown destination bin",
        )

        seen: set[tuple[int, str]] = set()
        snapshots: list[dict[str, object]] = []
        inventory_by_item = {}
        destination_state_by_item: dict[int, dict[str, object] | None] = {}
        already_there_count = 0
        for requested in entries:
            current_rows = inventory_by_item.setdefault(
                requested.item_id,
                list(store.item_inventory(requested.item_id)),
            )
            src = _canonical_inventory_location(requested.from_bin)
            if src is None:
                exact_source = requested.from_bin.strip()
                if any(row.bin_id == exact_source for row in current_rows):
                    src = exact_source
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"unknown source location: {requested.from_bin}",
                    )
            key = (requested.item_id, src)
            if key in seen:
                raise HTTPException(
                    status_code=400,
                    detail="the same inventory placement was selected more than once",
                )
            seen.add(key)
            if src == dst:
                already_there_count += 1
                continue
            source_before = next((row for row in current_rows if row.bin_id == src), None)
            if source_before is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"inventory changed before the move; nothing remains in {src}",
                )
            if requested.item_id not in destination_state_by_item:
                destination_before = next(
                    (row for row in current_rows if row.bin_id == dst),
                    None,
                )
                destination_state_by_item[requested.item_id] = (
                    {
                        "quantity": float(destination_before.quantity),
                        "unit": str(destination_before.unit),
                        "needs_review": bool(destination_before.needs_review),
                        "review_reason": str(destination_before.review_reason),
                    }
                    if destination_before is not None
                    else None
                )
            destination_before_state = destination_state_by_item[requested.item_id]
            snapshots.append(
                {
                    "moved_item": requested.item_id,
                    "item": source_before.name,
                    "from_location_id": src,
                    "to_location_id": dst,
                    "location_id": dst,
                    "previous_from_quantity": source_before.quantity,
                    "previous_from_unit": source_before.unit,
                    "previous_from_needs_review": bool(source_before.needs_review),
                    "previous_from_review_reason": str(source_before.review_reason),
                    "previous_to_quantity": (
                        destination_before_state["quantity"]
                        if destination_before_state is not None
                        else None
                    ),
                    "previous_to_unit": (
                        destination_before_state["unit"]
                        if destination_before_state is not None
                        else None
                    ),
                    "previous_to_needs_review": (
                        bool(destination_before_state["needs_review"])
                        if destination_before_state is not None
                        else False
                    ),
                    "previous_to_review_reason": (
                        str(destination_before_state["review_reason"])
                        if destination_before_state is not None
                        else ""
                    ),
                }
            )
            destination_state_by_item[requested.item_id] = (
                {
                    **destination_before_state,
                    "quantity": float(destination_before_state["quantity"])
                    + float(source_before.quantity),
                }
                if destination_before_state is not None
                else {
                    "quantity": float(source_before.quantity),
                    "unit": str(source_before.unit),
                    "needs_review": bool(source_before.needs_review),
                    "review_reason": str(source_before.review_reason),
                }
            )
        if not snapshots:
            raise HTTPException(
                status_code=400,
                detail="all selected inventory is already in that location",
            )
        return dst, snapshots, already_there_count

    def _inventory_move_preview(
        request: InventoryBulkMoveRequest,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        dst, snapshots, already_there_count = _inventory_move_plan(
            request.entries,
            request.to_bin,
        )
        destination = locations.get(dst)
        selected_item_ids = {int(entry.item_id) for entry in request.entries}
        selected_source_ids = {
            str(_canonical_inventory_location(entry.from_bin) or entry.from_bin)
            for entry in request.entries
        }
        merge_count = sum(
            1
            for snapshot in snapshots
            if snapshot.get("previous_to_quantity") is not None
        )
        payload = {
            "ok": True,
            "to_bin": dst,
            "destination": {
                "id": dst,
                "label": destination.label if destination is not None else dst,
                "path": _path_for(dst),
            },
            "selected_count": len(request.entries),
            "move_count": len(snapshots),
            "already_there_count": already_there_count,
            "item_count": len(selected_item_ids),
            "source_location_count": len(selected_source_ids),
            "merge_count": merge_count,
            "review_resolution_count": sum(
                1
                for snapshot in snapshots
                if snapshot.get("previous_from_needs_review")
            ),
            "entries": [
                {
                    "item_id": int(snapshot["moved_item"]),
                    "name": str(snapshot["item"]),
                    "from_location_id": str(snapshot["from_location_id"]),
                    "quantity": float(snapshot["previous_from_quantity"]),
                    "unit": str(snapshot["previous_from_unit"]),
                    "combines_with_existing": (
                        snapshot.get("previous_to_quantity") is not None
                    ),
                    "resolves_review": bool(
                        snapshot.get("previous_from_needs_review")
                    ),
                }
                for snapshot in snapshots
            ],
        }
        return payload, snapshots

    def _resolve_moved_review(
        snapshot: dict[str, object],
        destination_id: str,
    ) -> bool:
        if not snapshot.get("previous_from_needs_review"):
            return False
        item_id = int(snapshot["moved_item"])
        if not snapshot.get("previous_to_needs_review"):
            store.mark_reviewed(item_id=item_id, bin_id=destination_id)
        previous_to_quantity = snapshot.get("previous_to_quantity")
        after_quantity = float(snapshot["previous_from_quantity"]) + (
            float(previous_to_quantity)
            if previous_to_quantity is not None
            else 0.0
        )
        before = {
            "item_id": item_id,
            "name": str(snapshot["item"]),
            "bin_id": str(snapshot["from_location_id"]),
            "quantity": float(snapshot["previous_from_quantity"]),
            "unit": str(snapshot.get("previous_from_unit") or "each"),
            "needs_review": True,
            "review_reason": str(
                snapshot.get("previous_from_review_reason") or ""
            ),
        }
        after = {
            "item_id": item_id,
            "name": str(snapshot["item"]),
            "bin_id": destination_id,
            "quantity": after_quantity,
            "unit": str(
                snapshot.get("previous_to_unit")
                or snapshot.get("previous_from_unit")
                or "each"
            ),
            "needs_review": bool(snapshot.get("previous_to_needs_review")),
            "review_reason": str(
                snapshot.get("previous_to_review_reason") or ""
            ),
        }
        _record_review_correction_example(
            before=before,
            after=after,
            command=(
                f"review corrected {snapshot['item']} from "
                f"{snapshot['from_location_id']} to {destination_id}"
            ),
        )
        return True

    @app.post("/inventory/move-preview")
    def preview_inventory_move(
        request: InventoryBulkMoveRequest,
    ) -> dict[str, object]:
        preview, _snapshots = _inventory_move_preview(request)
        return preview

    @app.post("/inventory/move")
    def move_inventory(request: InventoryMoveRequest) -> dict[str, object]:
        preview_request = InventoryBulkMoveRequest(
            entries=[
                InventoryBulkMoveEntry(
                    item_id=request.item_id,
                    from_bin=request.from_bin,
                )
            ],
            to_bin=request.to_bin,
        )
        preview, snapshots = _inventory_move_preview(preview_request)
        dst = str(preview["to_bin"])
        snapshot = snapshots[0]
        moved = store.move_inventory(
            item_id=request.item_id,
            from_bin=str(snapshot["from_location_id"]),
            to_bin=dst,
            normalize_bin=False,
        )
        if not moved:
            raise HTTPException(
                status_code=409,
                detail="inventory changed before the move; nothing was moved",
            )
        source_root = _storage_root_id(str(snapshot["from_location_id"]))
        destination_root = _storage_root_id(dst)
        if source_root and destination_root and source_root != destination_root:
            _record_workspace_management_event(
                "cross_root_item_move",
                location_id=dst,
                note=f"moved inventory from {source_root} to {destination_root}",
            )
        resolved_review_count = int(_resolve_moved_review(snapshot, dst))
        _, active_find_errors = _reconcile_active_finds()
        lit = locations.nearest_lit(dst)
        response = {
            "ok": True,
            "to_bin": dst,
            "lit": (
                _light_pixels(
                    lit.light_indexes,
                    seconds=120.0,
                    target=str(lit.light_target or ""),
                )
                if lit
                else False
            ),
            "applied": [snapshot],
            "resolved_review_count": resolved_review_count,
            "active_finds": _raw_active_find_snapshot(),
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
        }
        src = str(snapshot["from_location_id"])
        command = f"manual move item {request.item_id} from {src} to {dst}"
        interaction_id = _record_manual_inventory_interaction(
            command=command,
            response={
                **response,
                "action": "manual_move_inventory",
                "item_id": request.item_id,
                "from_bin": src,
            },
        )
        _record_manual_example(
            command=command,
            plan=[
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": request.item_id,
                        "from_location_id": src,
                        "to_location_id": dst,
                    },
                }
            ],
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/inventory/move-bulk")
    def move_inventory_bulk(request: InventoryBulkMoveRequest) -> dict[str, object]:
        preview, snapshots = _inventory_move_preview(request)
        dst = str(preview["to_bin"])

        applied: list[dict[str, object]] = []
        try:
            for snapshot in snapshots:
                moved = store.move_inventory(
                    item_id=int(snapshot["moved_item"]),
                    from_bin=str(snapshot["from_location_id"]),
                    to_bin=dst,
                    normalize_bin=False,
                )
                if not moved:
                    raise RuntimeError(
                        f"inventory changed before the move from {snapshot['from_location_id']}"
                    )
                applied.append(snapshot)
        except Exception as exc:
            rollback_failed = False
            for snapshot in reversed(applied):
                try:
                    item_id = int(snapshot["moved_item"])
                    store.remove_inventory(item_id=item_id, bin_id=dst, normalize_bin=False)
                    previous_to_quantity = snapshot.get("previous_to_quantity")
                    if previous_to_quantity is not None:
                        store.put_inventory(
                            item_id=item_id,
                            bin_id=dst,
                            quantity=float(previous_to_quantity),
                            unit=str(snapshot.get("previous_to_unit") or "each"),
                            needs_review=bool(
                                snapshot.get("previous_to_needs_review")
                            ),
                            review_reason=str(
                                snapshot.get("previous_to_review_reason") or ""
                            ),
                            normalize_bin=False,
                        )
                    store.put_inventory(
                        item_id=item_id,
                        bin_id=str(snapshot["from_location_id"]),
                        quantity=float(snapshot["previous_from_quantity"]),
                        unit=str(snapshot.get("previous_from_unit") or "each"),
                        needs_review=bool(
                            snapshot.get("previous_from_needs_review")
                        ),
                        review_reason=str(
                            snapshot.get("previous_from_review_reason") or ""
                        ),
                        normalize_bin=False,
                    )
                except Exception:
                    rollback_failed = True
            if rollback_failed:
                raise HTTPException(
                    status_code=503,
                    detail="bulk move failed and Magpie could not fully restore the prior inventory",
                ) from exc
            raise HTTPException(
                status_code=409,
                detail="inventory changed during the bulk move; no items were moved",
            ) from exc

        resolved_review_count = sum(
            int(_resolve_moved_review(snapshot, dst))
            for snapshot in applied
        )
        destination_root = _storage_root_id(dst)
        source_roots = {
            _storage_root_id(str(snapshot["from_location_id"]))
            for snapshot in applied
        }
        if destination_root and any(
            source_root and source_root != destination_root
            for source_root in source_roots
        ):
            _record_workspace_management_event(
                "cross_root_item_move",
                location_id=dst,
                note=f"bulk moved inventory to {destination_root}",
            )
        _, active_find_errors = _reconcile_active_finds()
        lit = locations.nearest_lit(dst)
        response = {
            "ok": True,
            "to_bin": dst,
            "moved_count": len(applied),
            "lit": (
                _light_pixels(
                    lit.light_indexes,
                    seconds=120.0,
                    target=str(lit.light_target or ""),
                )
                if lit
                else False
            ),
            "applied": applied,
            "resolved_review_count": resolved_review_count,
            "active_finds": _raw_active_find_snapshot(),
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
        }
        command = f"manual bulk move {len(applied)} inventory placements to {dst}"
        interaction_id = _record_manual_inventory_interaction(
            command=command,
            response={
                **response,
                "action": "manual_bulk_move_inventory",
            },
        )
        _record_manual_example(
            command=command,
            plan=[
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": snapshot["moved_item"],
                        "from_location_id": snapshot["from_location_id"],
                        "to_location_id": dst,
                    },
                }
                for snapshot in applied
            ],
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/inventory/{item_id}/home")
    def update_item_home(item_id: int, request: InventoryHomeRequest) -> dict[str, object]:
        target = _canonical_inventory_location(request.location_id)
        if target is None:
            raise HTTPException(status_code=400, detail="unknown home location")
        rows = store.item_inventory(item_id)
        current = rows[0] if rows else None
        if current is None:
            raise HTTPException(status_code=404, detail="unknown item")
        try:
            store.update_item_home(item_id=item_id, location_id=target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        applied = [
            {
                "updated_item_home": item_id,
                "item": current.name,
                "location_id": target,
                "previous_home_location_id": current.home_location_id,
                "previous_last_seen_location_id": current.last_seen_location_id,
            }
        ]
        response = {
            "ok": True,
            "item_id": item_id,
            "item": current.name,
            "home_location_id": target,
            "applied": applied,
        }
        interaction_id = store.record_assistant_interaction(
            route="/inventory/home",
            command=f"manual set home for {current.name} to {target}",
            response=response,
            status="confirmed",
        )
        store.update_assistant_interaction_response(
            interaction_id,
            {"interaction_id": interaction_id},
        )
        _record_manual_example(
            command=f"manual set home for {current.name} to {target}",
            plan=[
                {
                    "tool": "update_item_home",
                    "arguments": {
                        "item_id": item_id,
                        "location_id": target,
                    },
                }
            ],
        )
        response["interaction_id"] = interaction_id
        return response

    def _record_manual_inventory_interaction(
        *,
        command: str,
        response: dict[str, object],
        photo_ids: list[int] | None = None,
    ) -> int:
        interaction_id = store.record_assistant_interaction(
            route="/inventory/manual",
            command=command,
            photo_ids=photo_ids or [],
            response=response,
            status="manual",
        )
        store.update_assistant_interaction_response(
            interaction_id,
            {"interaction_id": interaction_id},
        )
        return interaction_id

    def _record_manual_example(*, command: str, plan: list[dict[str, object]]) -> None:
        store.record_example(
            command=command,
            action=json.dumps(plan, sort_keys=True),
            kind="manual",
        )

    def _canonical_inventory_location(raw: str) -> str | None:
        raw = raw.strip()
        if locations.get(raw) is not None:
            return raw
        normalized = normalize_bin_id(raw)
        if layout.get(normalized) is not None or locations.get(normalized) is not None:
            return normalized
        return None

    def _storage_root_id(location_id: str) -> str:
        path = location_path(locations, location_id)
        return path[0].id if path else ""

    def _record_workspace_management_event(
        evidence_key: str,
        *,
        location_id: str,
        note: str,
    ) -> None:
        _record_workspace_evidence(
            WORKSPACE_MANAGEMENT_EVENTS[evidence_key],
            location_id=location_id,
            note=note,
        )

    def _record_workspace_evidence(
        event_type: str,
        *,
        location_id: str,
        note: str,
    ) -> None:
        if locations.has_event(event_type):
            return
        locations.record_event(
            event_type=event_type,
            location_id=location_id,
            note=note,
        )

    def _agent_response_payloads(
        response: dict[str, object],
    ) -> list[dict[str, object]]:
        payloads = [response]
        payloads.extend(
            nested
            for key in ("decision", "raw")
            if isinstance((nested := response.get(key)), dict)
        )
        return payloads

    def _agent_evidence_location(response: dict[str, object]) -> str:
        payloads = _agent_response_payloads(response)
        for payload in payloads:
            destination = payload.get("destination")
            if isinstance(destination, dict):
                location_id = str(destination.get("id") or "")
                if location_id:
                    return location_id
            for key in ("to_location_id", "location_id"):
                location_id = str(payload.get(key) or "")
                if location_id:
                    return location_id
            results = payload.get("results")
            if isinstance(results, list):
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    path = result.get("path")
                    if isinstance(path, list) and path:
                        leaf = path[-1]
                        location_id = (
                            str(leaf.get("id") or "")
                            if isinstance(leaf, dict)
                            else ""
                        )
                        if location_id:
                            return location_id
            trace = payload.get("trace")
            if isinstance(trace, list):
                for entry in trace:
                    if not isinstance(entry, dict):
                        continue
                    arguments = entry.get("arguments")
                    if isinstance(arguments, dict):
                        location_id = str(arguments.get("location_id") or "")
                        if location_id:
                            return location_id
                    result = entry.get("result")
                    if isinstance(result, list):
                        for row in result:
                            location_id = (
                                str(row.get("id") or "")
                                if isinstance(row, dict)
                                else ""
                            )
                            if location_id:
                                return location_id
            try:
                item_id = int(payload.get("item_id") or 0)
            except (TypeError, ValueError):
                item_id = 0
            if item_id:
                rows = store.item_inventory(item_id)
                if rows:
                    return rows[0].bin_id
        return next(
            (
                location.id
                for location in locations.all()
                if location.active and not location.parent_id
            ),
            "",
        )

    def _correction_rule_event(rule_type: object, rule_id: object) -> str:
        clean_type = str(rule_type or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", clean_type):
            return ""
        try:
            clean_id = int(rule_id or 0)
        except (TypeError, ValueError):
            return ""
        return (
            f"workspace_agent_correction_rule_{clean_type}_{clean_id}"
            if clean_id > 0
            else ""
        )

    def _record_agent_correction_application(response: dict[str, object]) -> None:
        location_id = _agent_evidence_location(response)
        if not location_id:
            return
        _record_workspace_evidence(
            AGENT_CORRECTION_EVENTS["correction_applied"],
            location_id=location_id,
            note="Agent correction was applied successfully.",
        )
        proposed_rule = response.get("proposed_rule")
        if not isinstance(proposed_rule, dict):
            proposed_rule = response
        rule_event = _correction_rule_event(
            proposed_rule.get("rule_type"),
            proposed_rule.get("rule_id"),
        )
        if rule_event:
            _record_workspace_evidence(
                rule_event,
                location_id=location_id,
                note="Correction rule was created for later adherence checks.",
            )

    def _record_agent_response_evidence(response: dict[str, object]) -> None:
        payloads = _agent_response_payloads(response)
        evidence: dict[str, str] = {}

        for payload in payloads:
            confirmation = payload.get("confirmation")
            explanation = (
                confirmation.get("explanation")
                if isinstance(confirmation, dict)
                else None
            )
            if isinstance(explanation, dict) and str(
                explanation.get("summary") or ""
            ).strip():
                evidence[AGENT_CORRECTION_EVENTS["explained_proposal"]] = (
                    "Agent presented an explained storage proposal."
                )

            result_sets: list[object] = [payload.get("results")]
            retrieval_response = bool(
                payload.get("action") == "found" or payload.get("mode") == "find"
            )
            trace = payload.get("trace")
            if isinstance(trace, list):
                for entry in trace:
                    if not isinstance(entry, dict):
                        continue
                    tool = str(entry.get("tool") or "")
                    result = entry.get("result")
                    if tool == "list_storage_roots" and isinstance(result, list):
                        returned_ids = {
                            str(root.get("id") or "")
                            for root in result
                            if isinstance(root, dict)
                        }
                        if len(returned_ids) >= 2:
                            evidence[AGENT_STORAGE_EVENTS["root_discovery"]] = (
                                "Agent listed multiple storage roots."
                            )
                    elif tool == "search_items":
                        result_sets.append(result)
                        retrieval_response = True
                    elif tool in {"preview_label_sheet", "preview_label_batch"}:
                        if isinstance(result, dict) and bool(
                            result.get("found") or result.get("label_count")
                        ):
                            evidence[AGENT_STORAGE_EVENTS["label_preview"]] = (
                                "Agent prepared a storage label preview."
                            )

            if retrieval_response:
                for results in result_sets:
                    if not isinstance(results, list):
                        continue
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        path = result.get("path")
                        root = path[0] if isinstance(path, list) and path else None
                        root_id = (
                            str(root.get("id") or "")
                            if isinstance(root, dict)
                            else ""
                        )
                        if root_id and root_id != "main-cabinet":
                            evidence[AGENT_STORAGE_EVENTS["cross_root_retrieval"]] = (
                                "Agent retrieved inventory outside the main cabinet."
                            )

            learning = payload.get("learning")
            evaluations = (
                learning.get("rule_adherence")
                if isinstance(learning, dict)
                else None
            )
            if not isinstance(evaluations, list):
                continue
            for evaluation in evaluations:
                if (
                    not isinstance(evaluation, dict)
                    or evaluation.get("followed") is not True
                ):
                    continue
                rule_event = _correction_rule_event(
                    evaluation.get("rule_type"),
                    evaluation.get("rule_id"),
                )
                if rule_event and locations.has_event(rule_event):
                    evidence[AGENT_CORRECTION_EVENTS["correction_retained"]] = (
                        "Agent followed a previously corrected storage rule."
                    )

        if not evidence:
            return
        location_id = _agent_evidence_location(response)
        if not location_id:
            return
        for event_type, note in evidence.items():
            _record_workspace_evidence(
                event_type,
                location_id=location_id,
                note=note,
            )

    def _inventory_destination_error(
        location_id: str,
        *,
        location: Location | None = None,
    ) -> str:
        destination = location if location is not None else locations.get(location_id)
        if destination is not None and not destination.accepts_items:
            return "that place organizes storage but does not hold items directly"
        return ""

    def _require_inventory_destination(
        raw: str,
        *,
        unknown_detail: str = "unknown bin or location",
    ) -> str:
        target = _canonical_inventory_location(raw)
        if target is None:
            raise HTTPException(status_code=400, detail=unknown_detail)
        error = _inventory_destination_error(target)
        if error:
            raise HTTPException(status_code=400, detail=error)
        return target

    def _csv_inventory_location(raw: str) -> tuple[str | None, str]:
        """Resolve a CSV location without guessing between duplicate labels."""
        target = _canonical_inventory_location(raw)
        if target is not None:
            return target, _inventory_destination_error(target)
        matches = match_locations(raw, locations)
        if len(matches) == 1:
            return matches[0].id, _inventory_destination_error(
                matches[0].id,
                location=matches[0],
            )
        if len(matches) > 1:
            paths = [
                " > ".join(
                    node.label or node.id
                    for node in location_path(locations, row.id)
                )
                for row in matches[:5]
            ]
            return None, (
                f"ambiguous location {raw}; use a qualified path: "
                + "; ".join(paths)
            )
        return None, f"unknown location {raw or '(missing)'}"

    def _csv_value(row: dict[str, str], *names: str) -> str:
        lowered = {str(key).strip().lower(): value for key, value in row.items()}
        for name in names:
            value = lowered.get(name)
            if value is not None:
                return str(value).strip()
        return ""

    def _csv_aliases(value: str) -> list[str]:
        return [
            alias.strip()
            for alias in re.split(r"[|;]", value or "")
            if alias.strip()
        ]

    def _csv_quantity(value: str, *, row_number: int) -> float:
        if not value:
            return 1.0
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"row {row_number}: quantity must be a number") from exc

    def _csv_path_parts(value: str, *, row_number: int) -> list[str]:
        raw = str(value or "").strip()
        if not raw:
            return []
        parts = [
            " ".join(part.split())
            for part in re.split(r"\s*(?:>|/)\s*", raw)
        ]
        if any(not part for part in parts):
            raise ValueError(f"row {row_number}: location_path contains a blank place")
        if len(parts) > 12:
            raise ValueError(f"row {row_number}: location_path cannot exceed 12 levels")
        if any(len(part) > 120 for part in parts):
            raise ValueError(
                f"row {row_number}: storage place names cannot exceed 120 characters"
            )
        return parts

    def _csv_path_types(
        value: str,
        *,
        part_count: int,
        row_number: int,
    ) -> list[str]:
        raw = str(value or "").strip()
        if not raw:
            if part_count == 1:
                return ["bin"]
            return ["area"] * (part_count - 1) + ["bin"]
        parts = [
            " ".join(part.split())
            for part in re.split(r"\s*(?:>|/)\s*", raw)
        ]
        if len(parts) != part_count:
            raise ValueError(
                f"row {row_number}: location_path_types must have one type per place"
            )
        try:
            return [normalize_location_type(part) for part in parts]
        except ValueError as exc:
            raise ValueError(f"row {row_number}: {exc}") from exc

    def _snapshot_list(snapshot: dict[str, object], key: str) -> list[dict[str, object]]:
        value = snapshot.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        rows: list[dict[str, object]] = []
        for index, row in enumerate(value, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"{key}[{index}] must be an object")
            rows.append(row)
        return rows

    def _snapshot_photo_ids(
        row: dict[str, object], *, field: str, errors: list[str]
    ) -> list[int]:
        raw_ids = row.get("photo_ids") or []
        if not isinstance(raw_ids, list):
            errors.append(f"{field}: photo_ids must be a list")
            return []
        photo_ids: list[int] = []
        for raw_id in raw_ids:
            try:
                photo_id = int(raw_id)
            except (TypeError, ValueError):
                errors.append(f"{field}: invalid photo id {raw_id}")
                continue
            if photo_id <= 0:
                errors.append(f"{field}: invalid photo id {raw_id}")
                continue
            photo_ids.append(photo_id)
        return list(dict.fromkeys(photo_ids))

    def _location_from_snapshot(row: dict[str, object]) -> Location:
        loc_id = str(row.get("id", "")).strip()
        loc_type = str(row.get("type", "")).strip()
        if not loc_id:
            raise ValueError("location id is required")
        if not loc_type:
            raise ValueError(f"location {loc_id}: type is required")
        parent = row.get("parent_id")
        parent_id = str(parent).strip() if parent is not None else None
        grid = row.get("grid")
        if grid is not None and not isinstance(grid, dict):
            raise ValueError(f"location {loc_id}: grid must be an object")
        indexes_raw = row.get("light_indexes") or []
        if not isinstance(indexes_raw, list):
            raise ValueError(f"location {loc_id}: light_indexes must be a list")
        tags_raw = row.get("purpose_tags") or []
        if not isinstance(tags_raw, list):
            raise ValueError(f"location {loc_id}: purpose_tags must be a list")
        dimensions_raw = row.get("internal_dimensions") or {}
        if not isinstance(dimensions_raw, dict):
            raise ValueError(f"location {loc_id}: internal_dimensions must be an object")
        active_raw = row.get("active", True)
        if not isinstance(active_raw, bool):
            raise ValueError(f"location {loc_id}: active must be true or false")
        accepts_items_raw = row.get("accepts_items", True)
        if not isinstance(accepts_items_raw, bool):
            raise ValueError(f"location {loc_id}: accepts_items must be true or false")
        archived_raw = row.get("archived_at")
        if archived_raw is not None and not isinstance(archived_raw, str):
            raise ValueError(f"location {loc_id}: archived_at must be text or null")
        archive_group_raw = row.get("archive_group")
        if archive_group_raw is not None and not isinstance(archive_group_raw, str):
            raise ValueError(f"location {loc_id}: archive_group must be text or null")
        if active_raw and archive_group_raw:
            raise ValueError(f"location {loc_id}: active location cannot have archive_group")
        try:
            return Location(
                id=loc_id,
                type=loc_type,
                parent_id=parent_id,
                label=str(row.get("label") or loc_id),
                grid=grid,
                col=int(row["col"]) if row.get("col") is not None else None,
                row=int(row["row"]) if row.get("row") is not None else None,
                w=int(row.get("w") or 1),
                h=int(row.get("h") or 1),
                light_target=str(row["light_target"]).strip()
                if row.get("light_target") is not None
                else None,
                light_indexes=[int(index) for index in indexes_raw],
                capacity_class=str(row.get("capacity_class") or "medium"),
                purpose_tags=[str(tag).strip() for tag in tags_raw if str(tag).strip()],
                placement_notes=str(row.get("placement_notes") or ""),
                internal_dimensions=normalize_internal_dimensions(dimensions_raw),
                accepts_items=accepts_items_raw,
                active=active_raw,
                archived_at=archived_raw,
                archive_group=(
                    str(archive_group_raw).strip()
                    if archive_group_raw
                    else (loc_id if not active_raw else None)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"location {loc_id}: invalid numeric field") from exc

    def _validate_inventory_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
        if snapshot.get("schema") != "magpie.inventory.v1":
            raise ValueError("snapshot schema must be magpie.inventory.v1")
        location_rows = _snapshot_list(snapshot, "locations")
        inventory_rows = _snapshot_list(snapshot, "inventory")
        placement_rule_rows = _snapshot_list(snapshot, "placement_rules")
        size_rule_rows = _snapshot_list(snapshot, "size_rules")
        photo_rows = _snapshot_list(snapshot, "photos")
        snapshot_location_id_list = [
            str(row.get("id", "")).strip()
            for row in location_rows
            if str(row.get("id", "")).strip()
        ]
        snapshot_location_ids = set(snapshot_location_id_list)
        existing_locations = locations.all(include_archived=True)
        known_locations = {loc.id for loc in existing_locations} | snapshot_location_ids
        prepared_locations = [_location_from_snapshot(row) for row in location_rows]
        location_by_id = {loc.id: loc for loc in existing_locations}
        location_by_id.update({loc.id: loc for loc in prepared_locations})
        errors: list[str] = []
        prepared_photos: list[dict[str, object]] = []
        photo_ids: set[int] = set()
        total_photo_bytes = 0
        for index, row in enumerate(photo_rows, start=1):
            try:
                photo_id = int(row.get("id"))
            except (TypeError, ValueError):
                errors.append(f"photos[{index}]: id must be a positive integer")
                continue
            if photo_id <= 0:
                errors.append(f"photos[{index}]: id must be a positive integer")
                continue
            if photo_id in photo_ids:
                errors.append("photos contains duplicate ids")
                continue
            photo_ids.add(photo_id)
            encoded = row.get("data_base64")
            if not isinstance(encoded, str) or not encoded:
                errors.append(f"photos[{index}]: data_base64 is required")
                continue
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                errors.append(f"photos[{index}]: data_base64 is invalid")
                continue
            if not data:
                errors.append(f"photos[{index}]: photo data is required")
                continue
            if len(data) > 10 * 1024 * 1024:
                errors.append(f"photos[{index}]: photo must be 10 MB or smaller")
                continue
            total_photo_bytes += len(data)
            prepared_photos.append(
                {
                    "id": photo_id,
                    "filename": str(row.get("filename") or "photo"),
                    "content_type": str(
                        row.get("content_type") or "application/octet-stream"
                    ),
                    "data": data,
                }
            )
        if total_photo_bytes > MAX_BACKUP_PHOTO_BYTES:
            errors.append("photos: combined photo data must be 18 MB or smaller")
        if len(snapshot_location_id_list) != len(snapshot_location_ids):
            errors.append("locations contains duplicate ids")
        for loc in prepared_locations:
            if loc.parent_id and loc.parent_id not in known_locations:
                errors.append(f"location {loc.id}: unknown parent {loc.parent_id}")
        parent_by_id = {loc.id: loc.parent_id for loc in existing_locations}
        active_by_id = {loc.id: loc.active for loc in existing_locations}
        parent_by_id.update({loc.id: loc.parent_id for loc in prepared_locations})
        active_by_id.update({loc.id: loc.active for loc in prepared_locations})
        reported_cycles: set[tuple[str, ...]] = set()
        for loc in prepared_locations:
            trail: list[str] = []
            current: str | None = loc.id
            while current is not None and current in parent_by_id:
                if current in trail:
                    cycle = trail[trail.index(current) :]
                    canonical = tuple(sorted(cycle))
                    if canonical not in reported_cycles:
                        reported_cycles.add(canonical)
                        errors.append(f"location graph contains a cycle: {' > '.join(cycle + [current])}")
                    break
                trail.append(current)
                current = parent_by_id.get(current)
            if loc.active and loc.parent_id and active_by_id.get(loc.parent_id) is False:
                errors.append(
                    f"location {loc.id}: active location cannot have archived parent {loc.parent_id}"
                )
        for index, row in enumerate(location_rows, start=1):
            for photo_id in _snapshot_photo_ids(
                row, field=f"locations[{index}]", errors=errors
            ):
                if photo_id not in photo_ids:
                    errors.append(
                        f"locations[{index}]: unknown photo {photo_id}"
                    )
        for index, row in enumerate(inventory_rows, start=1):
            name = str(row.get("name") or "").strip()
            raw_target = str(row.get("bin_id") or "").strip()
            target = _canonical_inventory_location(raw_target)
            if target is None and raw_target in snapshot_location_ids:
                target = raw_target
            if not name:
                errors.append(f"inventory[{index}]: name is required")
            if target is None:
                errors.append(f"inventory[{index}]: unknown location {row.get('bin_id') or '(missing)'}")
            elif error := _inventory_destination_error(
                target,
                location=location_by_id.get(target),
            ):
                errors.append(f"inventory[{index}]: {error}")
            try:
                float(row.get("quantity", 1))
            except (TypeError, ValueError):
                errors.append(f"inventory[{index}]: quantity must be a number")
            size_class = str(row.get("size_class") or "").strip().lower()
            if size_class and size_class not in CAPACITY_RANK:
                errors.append(
                    f"inventory[{index}]: invalid size_class "
                    f"{row.get('size_class')}"
                )
            dimensions = row.get("dimensions")
            if dimensions is not None and not isinstance(dimensions, dict):
                errors.append(f"inventory[{index}]: dimensions must be an object or null")
            try:
                size_confidence = float(row.get("size_confidence", 0))
                if not 0 <= size_confidence <= 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(
                    f"inventory[{index}]: size_confidence must be between 0 and 1"
                )
            for field in ("home_location_id", "last_seen_location_id"):
                location_id = str(row.get(field) or "").strip()
                if location_id and location_id not in known_locations:
                    errors.append(
                        f"inventory[{index}]: unknown {field} {location_id}"
                    )
            if "active" in row and not isinstance(row["active"], bool):
                errors.append(f"inventory[{index}]: active must be true or false")
            archived_at = row.get("archived_at")
            if archived_at is not None and not isinstance(archived_at, str):
                errors.append(
                    f"inventory[{index}]: archived_at must be text or null"
                )
            for photo_id in _snapshot_photo_ids(
                row, field=f"inventory[{index}]", errors=errors
            ):
                if photo_id not in photo_ids:
                    errors.append(f"inventory[{index}]: unknown photo {photo_id}")
        for index, row in enumerate(placement_rule_rows, start=1):
            target = str(row.get("target_location_id") or "").strip()
            if not str(row.get("rule_text") or "").strip():
                errors.append(f"placement_rules[{index}]: rule_text is required")
            if target not in known_locations:
                errors.append(f"placement_rules[{index}]: unknown target {target or '(missing)'}")
            if "active" in row and not isinstance(row["active"], bool):
                errors.append(
                    f"placement_rules[{index}]: active must be true or false"
                )
        for index, row in enumerate(size_rule_rows, start=1):
            size_class = str(row.get("size_class") or "").strip().lower()
            if not str(row.get("rule_text") or "").strip():
                errors.append(f"size_rules[{index}]: rule_text is required")
            if size_class not in CAPACITY_RANK:
                errors.append(f"size_rules[{index}]: invalid size_class {row.get('size_class') or '(missing)'}")
            if "active" in row and not isinstance(row["active"], bool):
                errors.append(f"size_rules[{index}]: active must be true or false")
        if errors:
            raise ValueError({"errors": errors})
        ordered_locations: list[Location] = []
        available_location_ids = {loc.id for loc in existing_locations}
        remaining_locations = list(prepared_locations)
        while remaining_locations:
            ready = [
                loc
                for loc in remaining_locations
                if loc.parent_id is None or loc.parent_id in available_location_ids
            ]
            if not ready:
                raise ValueError(
                    {"errors": ["location graph could not be ordered parent-first"]}
                )
            ordered_locations.extend(ready)
            available_location_ids.update(loc.id for loc in ready)
            ready_ids = {loc.id for loc in ready}
            remaining_locations = [
                loc for loc in remaining_locations if loc.id not in ready_ids
            ]
        return {
            "locations": ordered_locations,
            "inventory": inventory_rows,
            "placement_rules": placement_rule_rows,
            "size_rules": size_rule_rows,
            "photos": prepared_photos,
        }

    @app.post("/import/inventory.csv")
    def import_inventory_csv(request: InventoryCsvImportRequest) -> dict[str, object]:
        try:
            rows = list(csv.DictReader(io.StringIO(request.csv_text)))
        except csv.Error as exc:
            raise HTTPException(status_code=400, detail=f"invalid CSV: {exc}") from exc
        if not rows:
            raise HTTPException(status_code=400, detail="CSV has no inventory rows")

        prepared: list[dict[str, object]] = []
        errors: list[str] = []
        missing_location_rows: list[int] = []
        planned_locations: list[Location] = []
        reserved_location_ids = {
            location.id for location in locations.all(include_archived=True)
        }

        def planned_location_id(label: str, parent_id: str | None) -> str:
            raw = f"{parent_id}-{label}" if parent_id else label
            base = slugify(raw) or "location"
            candidate = base
            suffix = 2
            while candidate in reserved_location_ids:
                candidate = f"{base}-{suffix}"
                suffix += 1
            reserved_location_ids.add(candidate)
            return candidate

        def resolve_or_plan_path(
            path_text: str,
            type_text: str,
            *,
            row_number: int,
        ) -> Location:
            parts = _csv_path_parts(path_text, row_number=row_number)
            types = _csv_path_types(
                type_text,
                part_count=len(parts),
                row_number=row_number,
            )
            available = [*locations.all(), *planned_locations]
            parent_id: str | None = None
            current: Location | None = None
            for label, location_type in zip(parts, types, strict=True):
                matches = [
                    location
                    for location in available
                    if location.parent_id == parent_id
                    and _normalized_location_name(location.label or location.id)
                    == _normalized_location_name(label)
                ]
                if len(matches) > 1:
                    raise ValueError(
                        f"row {row_number}: ambiguous place in location_path: {label}"
                    )
                if matches:
                    current = matches[0]
                else:
                    current = Location(
                        id=planned_location_id(label, parent_id),
                        type=location_type,
                        parent_id=parent_id,
                        label=label,
                        accepts_items=default_accepts_items(location_type),
                    )
                    planned_locations.append(current)
                    available.append(current)
                parent_id = current.id
            if current is None:
                raise ValueError(f"row {row_number}: location_path is required")
            if not current.accepts_items:
                raise ValueError(
                    f"row {row_number}: the last place in location_path must hold items"
                )
            return current

        for index, row in enumerate(rows, start=2):
            name = _csv_value(row, "name", "item", "item_name")
            row_location_id = _csv_value(
                row,
                "location_id",
                "bin_id",
                "location",
                "bin",
            )
            row_location_path = _csv_value(row, "location_path", "place_path")
            row_location_types = _csv_value(
                row,
                "location_path_types",
                "place_path_types",
            )
            raw_location = row_location_id or row_location_path or (
                request.default_location_id or ""
            )
            if not raw_location and request.dry_run:
                target, location_error = None, None
                missing_location_rows.append(index)
            else:
                target, location_error = _csv_inventory_location(raw_location)
                if location_error and row_location_id and row_location_path:
                    target, location_error = _csv_inventory_location(
                        row_location_path
                    )
                if (
                    location_error
                    and row_location_path
                    and not location_error.startswith("ambiguous location")
                ):
                    try:
                        planned = resolve_or_plan_path(
                            row_location_path,
                            row_location_types,
                            row_number=index,
                        )
                        target, location_error = planned.id, ""
                    except ValueError as exc:
                        target, location_error = None, str(exc)
            try:
                quantity = _csv_quantity(_csv_value(row, "quantity", "qty"), row_number=index)
            except ValueError as exc:
                errors.append(str(exc))
                quantity = 1.0
            if not name:
                errors.append(f"row {index}: name is required")
            if location_error:
                errors.append(f"row {index}: {location_error}")
            prepared.append(
                {
                    "name": name,
                    "location_id": target or raw_location,
                    "location_path": row_location_path,
                    "quantity": quantity,
                    "unit": _csv_value(row, "unit") or "each",
                    "category": _csv_value(row, "category"),
                    "description": _csv_value(row, "description", "notes"),
                    "aliases": _csv_aliases(_csv_value(row, "aliases", "alias")),
                }
            )
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})

        planned_by_id = {location.id: location for location in planned_locations}

        def planned_path_label(location_id: str) -> str:
            labels: list[str] = []
            seen: set[str] = set()
            current = planned_by_id.get(location_id) or locations.get(location_id)
            while current is not None and current.id not in seen:
                labels.append(current.label or current.id)
                seen.add(current.id)
                current = (
                    planned_by_id.get(current.parent_id)
                    or locations.get(current.parent_id)
                    if current.parent_id
                    else None
                )
            return " > ".join(reversed(labels))

        missing_locations = [
            {
                "id": location.id,
                "label": location.label,
                "type": location.type,
                "parent_id": location.parent_id,
                "path": planned_path_label(location.id),
                "accepts_items": location.accepts_items,
            }
            for location in planned_locations
        ]
        if request.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "imported": 0,
                "rows": prepared,
                "requires_default_location": bool(missing_location_rows),
                "missing_location_rows": missing_location_rows,
                "requires_location_creation": bool(planned_locations),
                "missing_locations": missing_locations,
            }

        if planned_locations and not request.create_missing_locations:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "review and approve the missing storage places first",
                    "missing_locations": missing_locations,
                },
            )

        applied: list[dict[str, object]] = []
        snapshots: list[dict[str, object]] = []
        for row in prepared:
            previous_item = next(
                (
                    match
                    for match in store.exact_match(str(row["name"]))
                    if match.name.casefold() == str(row["name"]).casefold()
                ),
                None,
            )
            previous = (
                next(
                    (
                        entry
                        for entry in store.item_inventory(previous_item.item_id)
                        if entry.bin_id == str(row["location_id"])
                    ),
                    None,
                )
                if previous_item is not None
                else None
            )
            snapshots.append(
                {"previous_item": previous_item, "previous_placement": previous}
            )
        try:
            imported = store.import_inventory_rows_atomic(
                new_locations=planned_locations,
                rows=prepared,
            )
            locations.invalidate_cache()
            applied.extend(
                {"created_location": location.id}
                for location in planned_locations
            )
            for row, imported_row, snapshot in zip(
                prepared, imported, snapshots, strict=True
            ):
                item_id = int(imported_row["item_id"])
                previous_item = snapshot["previous_item"]
                previous = snapshot["previous_placement"]
                if previous_item is not None:
                    applied.append(
                        {
                            "updated_item": item_id,
                            "previous_name": previous_item.name,
                            "previous_description": previous_item.description,
                            "previous_category": previous_item.category,
                            "previous_aliases": list(previous_item.aliases),
                        }
                    )
                applied.append(
                    {
                        "stored": row["name"],
                        "item_id": item_id,
                        "location_id": row["location_id"],
                        "quantity": row["quantity"],
                        "previous_quantity": previous.quantity if previous else None,
                        "previous_unit": previous.unit if previous else None,
                        "previous_needs_review": (
                            previous.needs_review if previous else None
                        ),
                        "previous_review_reason": (
                            previous.review_reason if previous else None
                        ),
                    }
                )
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise HTTPException(
                status_code=503,
                detail="inventory import could not be completed; no rows were imported",
            ) from exc
        response = {
            "ok": True,
            "dry_run": False,
            "imported": len(imported),
            "rows": imported,
            "created_locations": missing_locations,
            "applied": applied,
            "action": "inventory_csv_imported",
        }
        response["interaction_id"] = _record_manual_inventory_interaction(
            command=f"import {len(imported)} inventory rows from CSV",
            response=response,
        )
        return response

    @app.post("/import/inventory.json")
    def import_inventory_json(request: InventoryJsonImportRequest) -> dict[str, object]:
        try:
            prepared = _validate_inventory_snapshot(request.snapshot)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=exc.args[0]) from exc

        if request.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "imported": 0,
                "locations": len(prepared["locations"]),
                "inventory": len(prepared["inventory"]),
                "placement_rules": len(prepared["placement_rules"]),
                "size_rules": len(prepared["size_rules"]),
                "photos": len(prepared["photos"]),
            }

        location_count = 0
        for loc in prepared["locations"]:
            locations.upsert(loc)
            location_count += 1

        photo_id_map: dict[int, int] = {}
        for row in prepared["photos"]:
            photo = store.add_photo(
                filename=str(row["filename"]),
                content_type=str(row["content_type"]),
                data=bytes(row["data"]),
            )
            photo_id_map[int(row["id"])] = photo.id
        source_locations = {
            str(row.get("id") or "").strip(): row
            for row in request.snapshot.get("locations", [])
            if isinstance(row, dict)
        }
        for loc in prepared["locations"]:
            source = source_locations.get(loc.id, {})
            for source_photo_id in _snapshot_photo_ids(
                source, field=f"location {loc.id}", errors=[]
            ):
                store.attach_photo_to_location(
                    location_id=loc.id,
                    photo_id=photo_id_map[source_photo_id],
                )

        inventory_count = 0
        restored_item_state: dict[int, dict[str, object]] = {}
        for row in prepared["inventory"]:
            try:
                item_id = store.upsert_item(
                    name=str(row.get("name") or ""),
                    description=str(row.get("description") or ""),
                    category=str(row.get("category") or ""),
                    aliases=[str(alias) for alias in row.get("aliases") or []],
                )
                for source_photo_id in row.get("photo_ids") or []:
                    store.attach_photo(
                        item_id=item_id,
                        photo_id=photo_id_map[int(source_photo_id)],
                    )
                store.put_inventory(
                    item_id=item_id,
                    bin_id=str(row.get("bin_id") or ""),
                    quantity=float(row.get("quantity", 1)),
                    unit=str(row.get("unit") or "each"),
                    needs_review=bool(row.get("needs_review", False)),
                    review_reason=str(row.get("review_reason") or ""),
                    normalize_bin=False,
                )
                item_state = restored_item_state.setdefault(item_id, {})
                for field in (
                    "size_class",
                    "size_source",
                    "size_confidence",
                    "dimensions",
                    "home_location_id",
                    "last_seen_location_id",
                    "active",
                    "archived_at",
                ):
                    if field in row:
                        item_state[field] = row[field]
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            inventory_count += 1

        for item_id, row in restored_item_state.items():
            if any(
                field in row
                for field in (
                    "size_class",
                    "size_source",
                    "size_confidence",
                    "dimensions",
                )
            ):
                store.restore_item_size_metadata(
                    item_id=item_id,
                    size_class=str(row.get("size_class") or ""),
                    source=str(row.get("size_source") or ""),
                    confidence=float(row.get("size_confidence") or 0),
                    dimensions=(
                        dict(row["dimensions"])
                        if isinstance(row.get("dimensions"), dict)
                        else None
                    ),
                )
            if any(
                field in row
                for field in (
                    "home_location_id",
                    "last_seen_location_id",
                    "active",
                    "archived_at",
                )
            ):
                store.restore_item_location_metadata(
                    item_id=item_id,
                    home_location_id=str(row.get("home_location_id") or ""),
                    last_seen_location_id=str(
                        row.get("last_seen_location_id") or ""
                    ),
                    active=bool(row.get("active", True)),
                    archived_at=(
                        str(row["archived_at"])
                        if row.get("archived_at") is not None
                        else None
                    ),
                )

        placement_count = 0
        for row in prepared["placement_rules"]:
            rule_id = store.learn_rule(
                rule_text=str(row.get("rule_text") or ""),
                trigger_terms=[str(term) for term in row.get("trigger_terms") or []],
                target_location_id=str(row.get("target_location_id") or ""),
                source_command=str(row.get("source_command") or "json import"),
                confidence=float(row.get("confidence", 1.0)),
            )
            if row.get("active") is False:
                store.set_rule_active(
                    rule_id=rule_id, rule_type="placement", active=False
                )
            placement_count += 1

        size_count = 0
        for row in prepared["size_rules"]:
            rule_id = store.learn_size_rule(
                rule_text=str(row.get("rule_text") or ""),
                trigger_terms=[str(term) for term in row.get("trigger_terms") or []],
                size_class=str(row.get("size_class") or ""),
                source_command=str(row.get("source_command") or "json import"),
                confidence=float(row.get("confidence", 1.0)),
            )
            if row.get("active") is False:
                store.set_rule_active(
                    rule_id=rule_id, rule_type="size", active=False
                )
            size_count += 1

        return {
            "ok": True,
            "dry_run": False,
            "imported": inventory_count,
            "locations": location_count,
            "inventory": inventory_count,
            "placement_rules": placement_count,
            "size_rules": size_count,
            "photos": len(photo_id_map),
        }

    @app.get("/inventory/review")
    def review_inventory(limit: int = 100) -> dict[str, object]:
        clean_limit = max(1, min(int(limit), 500))
        items: list[dict[str, object]] = []
        rows = store.review_queue(limit=clean_limit + 1)
        for item in rows[:clean_limit]:
            path = _path_for(item.bin_id)
            path_label = " › ".join(
                str(node.get("display_label") or node.get("label") or node.get("id") or "")
                for node in path
                if isinstance(node, dict)
            )
            items.append(
                {
                    **item.as_dict(),
                    "location_path": path,
                    "location_path_label": path_label or item.bin_id,
                }
            )
        return {"items": items, "has_more": len(rows) > clean_limit}

    @app.post("/inventory/reviewed")
    def mark_inventory_reviewed(request: InventoryReviewRequest) -> dict[str, object]:
        target = _canonical_inventory_location(request.bin_id)
        if target is None:
            raise HTTPException(status_code=400, detail="unknown bin or location")
        before = next(
            (
                row
                for row in store.item_inventory(request.item_id)
                if row.bin_id == target
            ),
            None,
        )
        if before is None:
            raise HTTPException(status_code=404, detail="item not found in that location")
        approved = before.as_dict() if before is not None else None
        if not before.needs_review:
            return {
                "ok": True,
                "item_id": request.item_id,
                "bin_id": target,
                "approved": approved,
                "already_reviewed": True,
            }
        store.mark_reviewed(item_id=request.item_id, bin_id=target)
        review_example_id: int | None = None
        if approved is not None:
            review_example_id = store.record_example(
                command=f"review approved {before.name} in {before.bin_id}",
                action=json.dumps({"approved_review": approved}, sort_keys=True),
                kind="review_approved",
            )
            if review_example_id is not None:
                store.approve_example(review_example_id)
        applied = [
            {
                "approved_review": request.item_id,
                "item": before.name,
                "location_id": target,
                "previous_review_reason": before.review_reason,
                "review_example_id": review_example_id,
            }
        ]
        response = {
            "ok": True,
            "item_id": request.item_id,
            "bin_id": target,
            "approved": approved,
            "applied": applied,
        }
        interaction_id = _record_manual_inventory_interaction(
            command=f"manual approve review for {before.name} in {target}",
            response={**response, "action": "manual_approve_review"},
        )
        response["interaction_id"] = interaction_id
        return response

    @app.delete("/bins/{bin_id}/inventory")
    def clear_bin_inventory(bin_id: str) -> dict[str, object]:
        normalized_bin = normalize_bin_id(bin_id)
        if layout.get(normalized_bin) is None:
            raise HTTPException(status_code=404, detail="bin not found")
        removed = store.clear_bin_inventory(normalized_bin)
        response = {"ok": True, "bin_id": normalized_bin, "removed": removed}
        interaction_id = _record_manual_inventory_interaction(
            command=f"manual clear inventory in {normalized_bin}",
            response={**response, "action": "manual_clear_inventory"},
        )
        response["interaction_id"] = interaction_id
        return response

    @app.delete("/inventory/{item_id}/locations/{location_id}")
    def remove_inventory_item(item_id: int, location_id: str) -> dict[str, object]:
        target = _canonical_inventory_location(location_id)
        if target is None:
            raise HTTPException(status_code=404, detail="unknown bin or location")
        current_rows = store.item_inventory(item_id)
        current = next((row for row in current_rows if row.bin_id == target), None)
        resolved_review = bool(current and current.needs_review)
        if resolved_review and len(current_rows) == 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    "this is the item's only recorded placement; archive the item "
                    "instead of leaving an active item without a location"
                ),
            )
        removal_note = (
            "Review resolved: recorded placement removed after the item could not be found."
            if resolved_review
            else ""
        )
        removed = store.remove_inventory(
            item_id=item_id,
            bin_id=target,
            normalize_bin=False,
            note=removal_note,
        )
        if not removed:
            raise HTTPException(status_code=404, detail="item not found in that location")
        applied = [{
            "removed_item": item_id,
            "item": current.name if current is not None else "",
            "location_id": target,
            "quantity": current.quantity if current is not None else 1,
            "unit": current.unit if current is not None else "each",
            "previous_needs_review": resolved_review,
            "previous_review_reason": (
                current.review_reason if current is not None else ""
            ),
            "review_resolution": (
                "removed_missing_placement" if resolved_review else ""
            ),
        }]
        _, active_find_errors = _reconcile_active_finds()
        response = {
            "ok": True,
            "item_id": item_id,
            "bin_id": target,
            "applied": applied,
            "active_finds": _raw_active_find_snapshot(),
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
            "resolved_review_count": int(resolved_review),
        }
        interaction_id = _record_manual_inventory_interaction(
            command=f"manual remove item {item_id} from {target}",
            response={**response, "action": "manual_remove_inventory"},
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/inventory/{item_id}")
    def update_inventory(item_id: int, request: InventoryUpdateRequest) -> dict[str, object]:
        target = _require_inventory_destination(
            request.bin_id,
            unknown_detail="bin not found",
        )
        before_rows = store.item_inventory(item_id)
        before_target = next((row for row in before_rows if row.bin_id == target), None)
        before_review = next(
            (
                row
                for row in before_rows
                if row.bin_id == target and row.needs_review
            ),
            None,
        )
        try:
            store.update_item(
                item_id=item_id,
                name=request.name,
                description=request.description,
                category=request.category,
                aliases=request.aliases,
            )
            store.put_inventory(
                item_id=item_id,
                bin_id=target,
                quantity=request.quantity,
                unit=request.unit,
                normalize_bin=False,
            )
            if request.size_class:
                store.update_item_size(
                    item_id=item_id,
                    size_class=request.size_class,
                    source=request.size_source,
                    confidence=request.size_confidence,
                    dimensions=request.dimensions,
                )
            elif "size_class" in request.model_fields_set:
                # Empty is an explicit clear; omission preserves existing metadata.
                store.restore_item_size_metadata(
                    item_id=item_id,
                    size_class="",
                    source="",
                    confidence=0.0,
                    dimensions=request.dimensions,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if before_review is not None:
            _record_review_correction_example(
                before=before_review.as_dict(),
                after={
                    "item_id": item_id,
                    "name": request.name,
                    "description": request.description,
                    "category": request.category,
                    "aliases": request.aliases,
                    "bin_id": target,
                    "quantity": request.quantity,
                    "unit": request.unit,
                    "size_class": request.size_class,
                    "dimensions": request.dimensions,
                },
            )
        elif before_target is not None:
            after = {
                "item_id": item_id,
                "name": request.name,
                "description": request.description,
                "category": request.category,
                "aliases": request.aliases,
                "bin_id": target,
                "quantity": request.quantity,
                "unit": request.unit,
                "size_class": request.size_class,
                "dimensions": request.dimensions,
            }
            before = before_target.as_dict()
            if any(before.get(key) != after.get(key) for key in ("name", "category", "quantity", "unit", "aliases", "description", "size_class", "dimensions")):
                _record_inventory_correction_example(before=before, after=after)
        current = before_rows[0] if before_rows else None
        applied: list[dict[str, object]] = []
        if current is not None:
            applied.append({
                "updated_item": item_id,
                "item": request.name,
                "previous_name": current.name,
                "previous_description": current.description,
                "previous_category": current.category,
                "previous_aliases": current.aliases,
            })
            if "size_class" in request.model_fields_set:
                applied.append({
                    "updated_item_size": item_id,
                    "item": request.name,
                    "previous_size_class": current.size_class,
                    "previous_size_source": current.size_source,
                    "previous_size_confidence": current.size_confidence,
                    "previous_dimensions": current.dimensions,
                })
        applied.append({
            "stored": request.name,
            "item_id": item_id,
            "location_id": target,
            "previous_quantity": before_target.quantity if before_target is not None else None,
            "previous_unit": before_target.unit if before_target is not None else None,
            "quantity": request.quantity,
        })
        _, active_find_errors = _reconcile_active_finds()
        response = {
            "ok": True,
            "item_id": item_id,
            "bin_id": target,
            "applied": applied,
            "active_finds": _raw_active_find_snapshot(),
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
        }
        interaction_id = _record_manual_inventory_interaction(
            command=f"manual update {request.name} in {target}",
            response={
                **response,
                "action": "manual_update_inventory",
                "item": request.name,
                "quantity": request.quantity,
                "unit": request.unit,
            },
        )
        response["interaction_id"] = interaction_id
        return response

    def _assistant_command_response(request: AssistantCommandRequest) -> dict[str, object]:
        photos = []
        for photo_id in request.photo_ids:
            photo = store.photo(photo_id)
            if photo is not None:
                photos.append(photo)
        assistant_source = "deterministic"
        should_try_llm = cfg.assistant_backend == "openai" or (
            cfg.assistant_backend == "auto" and _llm_key_present(cfg)
        )
        if should_try_llm:
            try:
                decision = llm_assistant_decision(
                    request.text,
                    layout=layout,
                    store=store,
                    photos=photos,
                    client=llm_client,
                    photo_loader=store.photo_content,
                )
                assistant_source = "openai"
            except OpenAIAssistantError as exc:
                if cfg.assistant_backend == "openai":
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                decision = assistant_decision(request.text, layout=layout, store=store)
                decision["message"] = f"{decision['message']} OpenAI fallback: {exc}"
        else:
            decision = assistant_decision(request.text, layout=layout, store=store)
        draft = decision.get("item_draft")
        if isinstance(draft, dict):
            draft["photo_ids"] = request.photo_ids
        automation = apply_automation(
            decision=decision,
            photo_ids=request.photo_ids,
            store=store,
            layout=layout,
            controller=controller,
        )
        return {
            "ok": True,
            "assistant_source": assistant_source,
            "decision": decision,
            "automation": automation,
        }

    @app.post("/assistant/command")
    def assistant_command(request: AssistantCommandRequest) -> dict[str, object]:
        return _assistant_command_response(request)

    def _assistant_agent_response(
        request: AgentCommandRequest,
        *,
        route: str = "/assistant/agent",
        recorded_command: str | None = None,
        replace_active_query: str = "",
        allowed_tools: set[str] | frozenset[str] | None = None,
    ) -> dict[str, object]:
        def plan_light_results(plan: list[dict[str, object]]) -> list[dict[str, object]]:
            found: list[dict[str, object]] = []
            for step in plan:
                arguments = step.get("arguments")
                if not isinstance(arguments, dict):
                    continue
                location_id = (
                    arguments.get("location_id")
                    or arguments.get("to_location_id")
                    or arguments.get("new_parent_id")
                )
                if location_id and locations.get(str(location_id)) is not None:
                    location_id = str(location_id)
                    ancestry = locations.ancestors(location_id)
                    path = (
                        [{"id": node.id, "label": node.label} for node in reversed(ancestry)]
                        if ancestry
                        else [{"id": location_id, "label": location_id}]
                    )
                    found.append(
                        {
                            "name": str(
                                arguments.get("item")
                                or arguments.get("label")
                                or request.text
                            ),
                            "location_id": location_id,
                            "path": path,
                        }
                    )
            return found

        def search_trace_results(trace: list[dict]) -> list[dict[str, object]]:
            found: list[dict[str, object]] = []
            seen: set[tuple[object, object]] = set()
            for entry in trace:
                if entry.get("tool") != "search_items":
                    continue
                raw_results = entry.get("result")
                if not isinstance(raw_results, list):
                    continue
                for raw in raw_results:
                    if not isinstance(raw, dict):
                        continue
                    result = dict(raw)
                    result["lit_location"] = result.get("nearest_lit_location_id")
                    key = (result.get("item_id"), result.get("location_id"))
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(result)
            return found[:LIGHT_BIN_LIMIT]

        def hardware_trace_state(trace: list[dict]) -> dict[str, object] | None:
            for entry in reversed(trace):
                tool = str(entry.get("tool") or "")
                if tool not in {"light_location", "clear_lights"}:
                    continue
                raw = entry.get("result")
                result = raw if isinstance(raw, dict) else {}
                if tool == "clear_lights":
                    return {
                        "action": (
                            "lights_cleared" if result.get("ok") else "light_unavailable"
                        ),
                        "message": str(
                            result.get("message")
                            or result.get("error")
                            or "Lights cleared."
                        ),
                        "lit": False,
                        "locations": [],
                    }
                location_id = str(result.get("location_id") or "")
                lit_location = str(result.get("lit_location") or "")
                color = result.get("color")
                assignment = (
                    [
                        {
                            "location_id": location_id,
                            "lit_location": lit_location,
                            "color": color,
                        }
                    ]
                    if location_id and lit_location
                    else []
                )
                lit = bool(result.get("lit"))
                return {
                    "action": "lit_location" if lit else "light_unavailable",
                    "message": str(
                        result.get("message")
                        or result.get("error")
                        or (
                            f"Lit {lit_location}."
                            if lit
                            else "That location could not be lit."
                        )
                    ),
                    "lit": lit,
                    "locations": assignment,
                }
            return None

        def fallback_plan_message(plan: list[dict[str, object]]) -> str:
            first = plan[0] if plan else {}
            arguments = first.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            tool = str(first.get("tool") or "")
            if tool == "put_item":
                item = str(arguments.get("item") or "this item")
                destination = str(arguments.get("location_id") or "the proposed location")
                return f"Put {item} in {destination}."
            if tool == "move_item":
                item = str(arguments.get("item") or "the item")
                destination = str(arguments.get("to_location_id") or "the proposed location")
                return f"Move {item} to {destination}."
            if tool == "learn_rule":
                rule = str(arguments.get("rule_text") or "this storage rule")
                return f"Remember: {rule}"
            if tool == "learn_size_rule":
                rule = str(arguments.get("rule_text") or "this size rule")
                return f"Remember: {rule}"
            if tool == "print_label":
                location_id = str(arguments.get("location_id") or "this location")
                location = locations.get(location_id)
                label = location.label if location is not None else location_id
                return f"Print the label for {label}."
            if tool == "create_location":
                label = str(arguments.get("label") or "the new location")
                parent_id = str(arguments.get("parent_id") or "")
                parent = locations.get(parent_id)
                parent_label = parent.label if parent is not None else parent_id
                move_count = sum(1 for step in plan if step.get("tool") == "move_item")
                move_text = (
                    f" and move {move_count} item{'s' if move_count != 1 else ''} there"
                    if move_count
                    else ""
                )
                place = f" in {parent_label}" if parent_label else ""
                return f"Create {label}{place}{move_text}."
            if tool == "archive_location":
                location_id = str(arguments.get("location_id") or "this location")
                return f"Archive {location_id} and preserve its records."
            if tool == "restore_location":
                location_id = str(arguments.get("location_id") or "this location")
                return f"Restore {location_id} to active storage."
            return "I have a storage plan ready."

        try:
            images: list[str] = []
            for photo_id in request.photo_ids:
                photo = store.photo(photo_id)
                if photo is None:
                    raise LlmError(f"photo {photo_id} was not found")
                if data_url := photo_data_url(photo, store.photo_content):
                    images.append(data_url)
            result = run_agent(
                request.text,
                store=store,
                locations=locations,
                client=agent_llm_client,
                images=images or None,
                examples=store.find_examples(request.text, limit=3),
                conversation_history=[
                    interaction
                    for interaction in store.assistant_interactions_after(limit=10)
                    if interaction.get("route")
                    in {
                        "/assistant/chat",
                        "/assistant/agent",
                        "/assistant/revise",
                        "/assistant/followup",
                        "/assistant/voice/confirm",
                        "/assistant/voice/cancel",
                    }
                ],
                read_tool_runner=tool_service.read,
                allowed_tools=allowed_tools,
                max_steps=cfg.agent_max_steps,
            )
        except LlmError as exc:
            failure_messages = {
                "authentication": (
                    "Magpie's planning model credentials need attention. "
                    "Find, clear, and ordinary put-away commands still use the fast path."
                ),
                "quota_exhausted": (
                    "Magpie's planning model has no available credits. "
                    "Find, clear, and ordinary put-away commands still use the fast path."
                ),
                "rate_limited": (
                    "Magpie's planning model is temporarily rate limited. "
                    "Find, clear, and ordinary put-away commands still use the fast path."
                ),
                "provider_unavailable": (
                    "Magpie's planning model is temporarily unavailable. "
                    "Find, clear, and ordinary put-away commands still use the fast path."
                ),
            }
            category = exc.category
            detail = failure_messages.get(
                category,
                "Magpie could not complete that planning request.",
            )
            action = {
                "authentication": "model_authentication_error",
                "quota_exhausted": "model_quota_exhausted",
                "rate_limited": "model_rate_limited",
                "provider_unavailable": "model_unavailable",
            }.get(category, "model_error")
            failure_interaction_id = store.record_assistant_interaction(
                route=route,
                command=request.text,
                response={
                    "ok": False,
                    "action": action,
                    "message": detail,
                    "provider": agent_provider,
                    "model": agent_model,
                    "failure_category": category,
                    "provider_status": exc.status_code,
                },
                status="failed",
            )
            raise HTTPException(
                status_code=503 if category != "model_error" else 502,
                detail=detail,
                headers={
                    "X-Magpie-Interaction-ID": str(failure_interaction_id),
                    "X-Magpie-Error-Action": action,
                    "X-Magpie-Failure-Category": category,
                },
            ) from exc
        plan = normalize_plan(
            result.plan,
            locations=locations,
            store=store,
            layout=layout,
            command=request.text,
        )
        results = search_trace_results(result.trace)
        location_candidates: list[dict[str, object]] = []
        for trace_entry in reversed(result.trace):
            if trace_entry.get("tool") != "plan_reorganization":
                continue
            trace_result = trace_entry.get("result")
            if not isinstance(trace_result, dict) or not trace_result.get(
                "needs_parent_selection"
            ):
                continue
            raw_choices = trace_result.get("parent_choices")
            if isinstance(raw_choices, list):
                location_candidates = [
                    dict(choice)
                    for choice in raw_choices
                    if isinstance(choice, dict)
                    and str(choice.get("selection_command") or "").strip()
                ]
            break
        hardware_state = hardware_trace_state(result.trace)
        retrieval_intent = bool(results) or bool(
            re.match(
                r"^(?:please\s+)?(?:finds?|fine|where|show me|locate|retrieve|fetch|get me|grab|bring me|i need)\b",
                request.text.strip(),
                re.I,
            )
        )
        light_state: dict[str, object] = {"lit": False, "locations": []}
        if plan:
            plan_kind = (
                "put_away"
                if any(
                    str(step.get("tool") or "")
                    in {"put_item", "move_item", "create_location"}
                    for step in plan
                )
                else "plan"
            )
            light_state = _activate_find(
                replace_active_query or request.text,
                plan_light_results(plan),
                kind=plan_kind,
                replace_query=replace_active_query,
            )
        elif hardware_state is not None:
            light_state = {
                "lit": bool(hardware_state.get("lit")),
                "locations": hardware_state.get("locations", []),
            }
        elif retrieval_intent:
            light_state = _activate_find(request.text, results)
        response = {
            "ok": True,
            "message": (
                str(hardware_state.get("message") or "")
                if hardware_state is not None
                else result.message.strip()
                or (fallback_plan_message(plan) if plan else "")
            ),
            "plan": plan,
            "trace": result.trace,
            "action": (
                "proposed"
                if plan
                else (
                    str(hardware_state.get("action") or "answered")
                    if hardware_state is not None
                    else ("found" if retrieval_intent and results else "answered")
                )
            ),
            "results": results,
            "location_candidates": location_candidates,
            "lit": bool(light_state.get("lit")),
            "light_assignments": light_state.get("locations", []),
            "active_finds": light_state.get("active_finds", _active_find_snapshot()),
            "agent": {
                "provider": agent_provider,
                "model": agent_model,
                "api": agent_api,
                "usage": getattr(result, "usage", {}),
            },
        }
        if plan:
            _supersede_pending_agent_plans()
        interaction_id = store.record_assistant_interaction(
            route=route,
            command=recorded_command or request.text,
            photo_ids=request.photo_ids,
            response=response,
            status="pending" if plan else "",
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/assistant/agent")
    def assistant_agent(request: AgentCommandRequest) -> dict[str, object]:
        return _assistant_agent_response(request)

    @app.post("/assistant/revise")
    def assistant_revise(
        request: AssistantRevisionRequest,
    ) -> dict[str, object]:
        correction = request.text.strip()
        if not correction:
            raise HTTPException(status_code=400, detail="describe what should change")
        pending = _latest_confirmable_agent_plan()
        if pending is None:
            raise HTTPException(
                status_code=409,
                detail="there is no pending Magpie proposal to change",
            )
        pending_response = pending.get("response")
        original_plan = (
            pending_response.get("plan")
            if isinstance(pending_response, dict)
            else None
        )
        if not isinstance(original_plan, list) or not original_plan:
            raise HTTPException(
                status_code=409,
                detail="the pending Magpie proposal has no plan",
            )
        original_command = str(
            (
                pending_response.get("original_command")
                if isinstance(pending_response, dict)
                else ""
            )
            or pending.get("command")
            or ""
        ).strip()
        photo_ids = [
            int(photo_id)
            for photo_id in pending.get("photo_ids", [])
            if isinstance(photo_id, int)
        ]
        recorded_command = f"{original_command}\nCorrection: {correction}".strip()
        quick_change = correction.casefold().strip().rstrip(".")
        quick_change_mode = {
            "use a different storage place": "different",
            "use a bigger bin or container": "bigger",
            "use a smaller bin or container": "smaller",
        }.get(quick_change)
        selected_location_id = request.location_id.strip()
        revised_plan = json.loads(json.dumps(original_plan))
        quick_trace: list[dict[str, object]] = []
        quick_change_supported = bool(quick_change_mode or selected_location_id)
        quick_change_applied = False
        if selected_location_id:
            selected_location = locations.get(selected_location_id)
            if (
                selected_location is None
                or not selected_location.active
                or not selected_location.accepts_items
            ):
                raise HTTPException(
                    status_code=400,
                    detail="choose an active storage place that accepts items",
                )
            placement_indexes = [
                index
                for index, step in enumerate(revised_plan)
                if isinstance(step, dict)
                and str(step.get("tool") or "") in {"put_item", "move_item"}
                and isinstance(step.get("arguments"), dict)
            ]
            if request.step_index is None:
                if len(placement_indexes) != 1:
                    raise HTTPException(
                        status_code=400,
                        detail="choose which proposed item should change destination",
                    )
                selected_step_index = placement_indexes[0]
            else:
                selected_step_index = request.step_index
                if selected_step_index not in placement_indexes:
                    raise HTTPException(
                        status_code=400,
                        detail="the selected proposal row cannot change destination",
                    )
            selected_step = revised_plan[selected_step_index]
            selected_arguments = selected_step["arguments"]
            destination_key = (
                "location_id"
                if selected_step["tool"] == "put_item"
                else "to_location_id"
            )
            if str(selected_arguments.get(destination_key) or "") == selected_location_id:
                raise HTTPException(
                    status_code=400,
                    detail="choose a different destination",
                )
            selected_arguments[destination_key] = selected_location_id
            selected_step["arguments"] = selected_arguments
            selected_path = location_path(locations, selected_location_id)
            quick_change_applied = True
            quick_trace.append(
                {
                    "tool": "select_location",
                    "arguments": {
                        "step_index": selected_step_index,
                        "location_id": selected_location_id,
                    },
                    "result": {
                        "id": selected_location.id,
                        "label": selected_location.label,
                        "type": selected_location.type,
                        "path": [
                            {"id": node.id, "label": node.label}
                            for node in selected_path
                        ],
                    },
                }
            )
        elif quick_change_mode in {"bigger", "smaller"}:
            for step in revised_plan:
                if not isinstance(step, dict):
                    quick_change_supported = False
                    break
                tool = str(step.get("tool") or "")
                arguments = step.get("arguments")
                if tool not in {"put_item", "move_item"} or not isinstance(
                    arguments, dict
                ):
                    quick_change_supported = False
                    break
                current_id = str(
                    arguments.get(
                        "location_id" if tool == "put_item" else "to_location_id"
                    )
                    or ""
                )
                current_location = locations.get(current_id)
                item_name = str(arguments.get("item") or "").strip()
                if not item_name and arguments.get("item_id"):
                    item_rows = store.item_inventory(int(arguments["item_id"]))
                    item_name = item_rows[0].name if item_rows else ""
                if current_location is None or not item_name:
                    quick_change_supported = False
                    break
                needed_capacity, _size_reason = item_capacity_for(
                    item_name,
                    store=store,
                )
                needed_rank = CAPACITY_RANK[needed_capacity]
                current_rank = CAPACITY_RANK.get(
                    current_location.capacity_class,
                    CAPACITY_RANK["medium"],
                )
                suggestions = suggest_locations(
                    item_name,
                    store=store,
                    locations=locations,
                    limit=100,
                )
                candidates = [
                    suggestion
                    for suggestion in suggestions
                    if suggestion.location.id != current_id
                    and CAPACITY_RANK.get(
                        suggestion.location.capacity_class,
                        CAPACITY_RANK["medium"],
                    )
                    >= needed_rank
                ]
                if quick_change_mode == "bigger":
                    candidates = [
                        suggestion
                        for suggestion in candidates
                        if CAPACITY_RANK.get(
                            suggestion.location.capacity_class,
                            CAPACITY_RANK["medium"],
                        )
                        > current_rank
                    ]
                    candidates.sort(
                        key=lambda suggestion: (
                            CAPACITY_RANK.get(
                                suggestion.location.capacity_class,
                                CAPACITY_RANK["medium"],
                            ),
                            -suggestion.score,
                        )
                    )
                elif quick_change_mode == "smaller":
                    candidates = [
                        suggestion
                        for suggestion in candidates
                        if CAPACITY_RANK.get(
                            suggestion.location.capacity_class,
                            CAPACITY_RANK["medium"],
                        )
                        < current_rank
                    ]
                    candidates.sort(
                        key=lambda suggestion: (
                            -CAPACITY_RANK.get(
                                suggestion.location.capacity_class,
                                CAPACITY_RANK["medium"],
                            ),
                            -suggestion.score,
                        )
                    )
                if not candidates:
                    quick_change_applied = False
                    break
                selected = candidates[0]
                destination_key = (
                    "location_id" if tool == "put_item" else "to_location_id"
                )
                arguments[destination_key] = selected.location.id
                quick_change_applied = True
                quick_trace.append(
                    {
                        "tool": "suggest_locations",
                        "arguments": {
                            "item": item_name,
                            "change": quick_change_mode,
                            "exclude": current_id,
                        },
                        "result": selected.as_dict(),
                    }
                )

        if quick_change_supported:
            if quick_change_applied:
                light_results: list[dict[str, object]] = []
                for step in revised_plan:
                    arguments = step.get("arguments")
                    if not isinstance(arguments, dict):
                        continue
                    location_id = str(
                        arguments.get(
                            "location_id"
                            if step.get("tool") == "put_item"
                            else "to_location_id"
                        )
                        or ""
                    )
                    ancestry = locations.ancestors(location_id)
                    light_results.append(
                        {
                            "name": str(arguments.get("item") or original_command),
                            "location_id": location_id,
                            "path": [
                                {"id": node.id, "label": node.label}
                                for node in reversed(ancestry)
                            ],
                        }
                    )
                light_state = _activate_find(
                    original_command,
                    light_results,
                    kind="put_away",
                    replace_query=original_command,
                )
                _supersede_pending_agent_plans()
                response = {
                    "ok": True,
                    "message": "Updated the destination.",
                    "action": "proposed",
                    "plan": revised_plan,
                    "trace": quick_trace,
                    "usage": {},
                    "lit": bool(light_state.get("lit")),
                    "light_assignments": light_state.get("locations", []),
                    "active_finds": light_state.get(
                        "active_finds",
                        _active_find_snapshot(),
                    ),
                }
                response["interaction_id"] = store.record_assistant_interaction(
                    route="/assistant/revise",
                    command=recorded_command,
                    photo_ids=photo_ids,
                    response=response,
                    status="pending",
                )
            else:
                response = {
                    "ok": True,
                    "message": (
                        "Choose the storage place you want me to use. "
                        "The original proposal still needs confirmation."
                        if quick_change_mode == "different"
                        else (
                            "I could not find another suitable destination for that change. "
                            "The original proposal still needs confirmation."
                        )
                    ),
                    "action": "clarify",
                    "plan": [],
                    "trace": quick_trace,
                    "usage": {},
                }
                response["interaction_id"] = store.record_assistant_interaction(
                    route="/assistant/revise",
                    command=recorded_command,
                    photo_ids=photo_ids,
                    response=response,
                    status="complete",
                )
        else:
            revision_prompt = "\n".join(
                [
                    "Revise the pending storage plan.",
                    "",
                    f"Original user request:\n{original_command}",
                    "",
                    f"Current proposed plan JSON:\n{json.dumps(original_plan)}",
                    "",
                    f"User follow-up:\n{correction}",
                    "",
                    "If the follow-up is a question, answer briefly and keep or revise the plan only when useful.",
                    "If it asks whether an item or bin already exists, call search_items or suggest_locations before answering.",
                    "Return the full corrected plan when the plan changes. If the plan should not change, return no write tools and explain.",
                    "Keep unchanged steps when returning a corrected plan, incorporate the follow-up as detail or corrections, and do not create an item named after the follow-up.",
                    "Do not create, rename, move, or archive storage locations unless that operation was already present in the pending plan.",
                ]
            )
            response = _assistant_agent_response(
                AgentCommandRequest(text=revision_prompt, photo_ids=photo_ids),
                route="/assistant/revise",
                recorded_command=recorded_command,
                replace_active_query=original_command,
                allowed_tools=(
                    VOICE_AGENT_TOOLS
                    if request.source.strip().lower() == "voice"
                    else None
                ),
            )
        response["revision_of"] = int(pending["id"])
        response["original_command"] = original_command
        response["correction"] = correction
        response["source"] = request.source.strip() or "ui"
        interaction_id = response.get("interaction_id")
        plan = response.get("plan")
        if isinstance(plan, list) and plan:
            presentation = _confirmation_presentation({"response": response})
            if presentation is not None:
                response["confirmation"] = presentation
        else:
            response["original_plan_still_pending"] = True
        if request.source.strip().lower() == "voice":
            response["voice"] = {
                "transcript": request.voice_transcript.strip(),
                "command": request.voice_command.strip() or correction,
                "query": request.voice_query.strip() or correction,
                "stt_model": request.stt_model.strip(),
            }
        if isinstance(interaction_id, int):
            store.update_assistant_interaction_response(
                interaction_id,
                {
                    key: response[key]
                    for key in (
                        "revision_of",
                        "original_command",
                        "correction",
                        "source",
                        "voice",
                        "confirmation",
                        "original_plan_still_pending",
                    )
                    if key in response
                },
            )
        return response

    def _chat_mode(text: str, requested_mode: str, photo_ids: list[int]) -> str:
        mode = requested_mode.strip().lower()
        if mode in {"agent", "ask", "find", "command"}:
            return mode
        cleaned = text.strip()
        if re.match(
            r"^(?:please\s+)?(?:finds?|fine|where(?:'s| is| are)?|show me|locate|"
            r"retrieve|fetch|get me|grab(?: me)?|bring me|i need)\b",
            cleaned,
            re.I,
        ):
            return "find"
        if re.match(r"^(?:put away|put|store|file|stash|stow)\b", cleaned, re.I):
            return "ask"
        if re.match(
            r"^(?:move|relocate|consolidate|organize|sort|learn|forget|why|create|make|rename|categorize|classify|describe|remove|delete|discard|trash)\b",
            cleaned,
            re.I,
        ):
            return "agent"
        if re.search(r"\b(?:light|illuminate|clear|turn off)\b", cleaned, re.I):
            return "agent"
        if re.search(r"\b(?:threw|throw|tossed|used up|gone|no longer have)\b", cleaned, re.I):
            return "agent"
        if re.search(r"\b(?:mark|file)\s+(?:it|that|this)?\s*as\b", cleaned, re.I):
            return "agent"
        if re.search(
            r"\b(?:also called|known as|aka|alias|nickname|i call|we call)\b",
            cleaned,
            re.I,
        ):
            return "agent"
        if re.search(
            r"\b(?:home|usual spot|lives?|belongs?|should live|should always go|should go)\b",
            cleaned,
            re.I,
        ):
            return "agent"
        if re.search(r"\b(rules?|why did|why is|why was|explain|forget that|forget the)\b", cleaned, re.I):
            return "agent"
        if re.search(r"\b(qr|labels?|label sheet|printable sheet|print labels?)\b", cleaned, re.I):
            return "agent"
        if photo_ids:
            return "agent"
        return "ask"

    def _strip_find_intent(text: str) -> str:
        match = re.match(
            r"^(?:please\s+)?(?:finds?|fine|where(?:'s| is| are)?|show me|locate|"
            r"retrieve|fetch|get me|grab(?: me)?|bring me|i need)\s+(.+)$",
            text.strip(),
            re.I,
        )
        return match.group(1).strip() if match else text.strip()

    def _put_away_requested(text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", text).strip()
        return bool(
            re.search(
                r"\b(?:put\s+away|store|file|stash|stow)\b",
                cleaned,
                re.I,
            )
            or re.search(
                r"\bput\s+(?:this|that|these|those|it|them)\s+away\b",
                cleaned,
                re.I,
            )
            or re.search(
                r"\bput\b.+\b(?:in|into|inside)\b",
                cleaned,
                re.I,
            )
        )

    def _strip_put_intent(text: str) -> str:
        match = re.match(
            r"^(?:(?:please|let(?:'s| us)|can you|could you|would you)\s+)*"
            r"(?:put away|put|store|file|stash|stow)\s+"
            r"(?:(?:this|that|these|those|the)\s+)?(.+)$",
            text.strip(),
            re.I,
        )
        return match.group(1).strip() if match else text.strip()

    def _canonical_voice_chat_text(text: str, source: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if source.strip().lower() != "voice":
            return cleaned
        wake_matches = list(re.finditer(r"\bmagpie\b", cleaned, re.I))
        if not wake_matches:
            return cleaned
        tail = cleaned[wake_matches[-1].end() :].strip(" \t\r\n,;:-.!?")
        return tail or "Magpie"

    def _direct_location_light_response(command: str) -> dict[str, object] | None:
        match = re.fullmatch(
            r"(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
            r"(?:light|illuminate)(?:\s+up)?\s+(?:drawer\s+|bin\s+)?(B?\d{1,4})[.!?]?",
            command.strip(),
            re.I,
        )
        if not match:
            return None
        location_id = normalize_bin_id(match.group(1))
        location = locations.get(location_id)
        if location is None:
            return _direct_chat_response(
                command=command,
                mode="light",
                action="not_found",
                message=f"I could not find {location_id}.",
            )
        light_state = _activate_find(
            command,
            [
                {
                    "name": location.label,
                    "location_id": location_id,
                    "path": _path_for(location_id),
                }
            ],
        )
        lit = bool(light_state.get("lit"))
        assignment = light_state.get("locations", [])
        message = (
            f"Lit {location.label}."
            if lit
            else f"{location.label} does not have a working light binding."
        )
        response: dict[str, object] = {
            "ok": True,
            "mode": "light",
            "route": "/assistant/chat",
            "action": "lit_location" if lit else "light_unavailable",
            "message": message,
            "results": [],
            "plan": [],
            "lit": lit,
            "light_assignments": assignment,
            "active_finds": light_state.get("active_finds", []),
            "automation": {"lit": lit, "locations": assignment},
        }
        response["interaction_id"] = store.record_assistant_interaction(
            route="/assistant/chat",
            command=command,
            response=response,
        )
        return response

    def _constraint_only_chat(text: str) -> str | None:
        cleaned = re.sub(r"\s+", " ", text).strip(" \t\r\n.!?")
        match = re.fullmatch(
            r"(?:no[, ]+)?(?:(?:it|they|these|those)\s+)?"
            r"(?:(?:has|have|needs?|should|can)\s+)?(?:to\s+)?"
            r"(?:(?:be|go|fit|use)\s+)?(?:in(?:to)?\s+)?"
            r"(?:an?\s+|the\s+)?"
            r"(tiny|small|medium|med|large|big|oversize|oversized)\s+"
            r"(?:bin|box|drawer|container)",
            cleaned,
            re.I,
        )
        if not match:
            return None
        size = match.group(1).lower()
        return {"med": "medium", "big": "large", "oversized": "oversize"}.get(size, size)

    def _inventory_status_requested(text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", text).strip().lower()
        return bool(
            re.search(r"\b(?:inventory|items?|storage)\b", cleaned)
            and re.search(r"\b(?:status|summary|overview|count|how many)\b", cleaned)
        )

    def _storage_roots_requested(text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", text).strip(" .?!").lower()
        return bool(
            re.fullmatch(
                r"(?:what|which) (?:storage(?: (?:spaces|places|roots|locations))?"
                r"|spaces|places|roots|locations) "
                r"(?:do (?:i|we) have|have (?:i|we) added|are there|are in magpie)",
                cleaned,
            )
            or re.fullmatch(
                r"(?:list|show|name) (?:me )?(?:all )?(?:(?:my|our|the) )?"
                r"(?:storage(?: (?:spaces|places|roots|locations))?"
                r"|spaces|places|roots|locations)",
                cleaned,
            )
            or re.fullmatch(
                r"where are (?:my|our|the) storage (?:spaces|places|roots|locations)",
                cleaned,
            )
        )

    def _storage_roots_chat_response(command: str) -> dict[str, object]:
        raw_roots = tool_service.read("list_storage_roots", {})
        roots = raw_roots if isinstance(raw_roots, list) else []
        if roots:
            labels = [str(root.get("label") or root.get("id") or "storage") for root in roots]
            if len(labels) == 1:
                names = labels[0]
            else:
                names = f"{', '.join(labels[:-1])}, and {labels[-1]}"
            message = f"You have {len(roots)} top-level storage spaces: {names}."
        else:
            message = "You do not have any storage spaces yet."
        trace = [{"tool": "list_storage_roots", "arguments": {}, "result": roots}]
        response: dict[str, object] = {
            "ok": True,
            "mode": "agent",
            "route": "/assistant/chat",
            "message": message,
            "action": "answered",
            "results": [],
            "plan": [],
            "trace": trace,
            "lit": False,
            "light_assignments": [],
            "active_finds": _active_find_snapshot(),
            "automation": {"lit": False, "results": []},
        }
        response["interaction_id"] = store.record_assistant_interaction(
            route="/assistant/chat",
            command=command,
            response=response,
        )
        return response

    def _label_preview_chat_response(command: str) -> dict[str, object] | None:
        cleaned = re.sub(r"\s+", " ", command).strip(" .?!")
        match = re.fullmatch(
            r"(?:(?:please\s+)?(?:preview|show|open|make|create|prepare|print)\s+)?"
            r"(?:the\s+)?(?:qr\s+)?(?:labels?|label sheet)\s+for\s+"
            r"(?P<location>.+)",
            cleaned,
            re.I,
        )
        if match is None:
            return None
        location_name = match.group("location").strip()
        candidates = match_locations(location_name, locations, active=True)
        if not candidates:
            return _direct_chat_response(
                command=command,
                mode="clarify",
                action="clarify",
                message=f"I could not find an active storage place named {location_name}.",
            )
        if len(candidates) > 1:
            options = [
                {
                    "location_id": location.id,
                    "label": location.label or location.id,
                    "path": " > ".join(
                        node.label or node.id
                        for node in location_path(locations, location.id)
                    ),
                }
                for location in candidates
            ]
            return _direct_chat_response(
                command=command,
                mode="clarify",
                action="clarify",
                message=f"I found more than one storage place named {location_name}.",
                details={"candidates": options},
            )
        location = candidates[0]
        raw_preview = tool_service.read(
            "preview_label_sheet", {"location_id": location.id}
        )
        preview = raw_preview if isinstance(raw_preview, dict) else {}
        if not preview.get("found"):
            return _direct_chat_response(
                command=command,
                mode="clarify",
                action="clarify",
                message=f"I could not prepare labels for {location.label or location.id}.",
            )
        trace = [
            {
                "tool": "preview_label_sheet",
                "arguments": {"location_id": location.id},
                "result": preview,
            }
        ]
        response: dict[str, object] = {
            "ok": True,
            "mode": "agent",
            "route": "/assistant/chat",
            "message": f"Label preview ready for {location.label or location.id}.",
            "action": "answered",
            "results": [],
            "plan": [],
            "trace": trace,
            "label_preview": preview,
            "lit": False,
            "light_assignments": [],
            "active_finds": _active_find_snapshot(),
            "automation": {"lit": False, "results": []},
        }
        response["interaction_id"] = store.record_assistant_interaction(
            route="/assistant/chat",
            command=command,
            response=response,
        )
        return response

    def _storage_plan_chat_response(
        *,
        command: str,
        message: str,
        plan: list[dict[str, object]],
        trace_tool: str,
        photo_ids: list[int] | None = None,
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": True,
            "mode": "agent",
            "route": "/assistant/chat",
            "message": message,
            "action": "proposed",
            "results": [],
            "plan": plan,
            "trace": [
                {
                    "tool": trace_tool,
                    "arguments": {"command": command},
                    "result": {"proposed_steps": plan},
                }
            ],
            "lit": False,
            "light_assignments": [],
            "active_finds": _active_find_snapshot(),
            "automation": {"lit": False, "results": []},
        }
        _supersede_pending_agent_plans()
        response["interaction_id"] = store.record_assistant_interaction(
            route="/assistant/chat",
            command=command,
            photo_ids=photo_ids or [],
            response=response,
            status="pending",
        )
        return response

    def _storage_setup_chat_response(
        command: str,
        current_location_id: str | None,
        *,
        create_root: bool = False,
    ) -> dict[str, object]:
        root_plan: list[dict[str, object]] = []
        setup_contents = ""
        if create_root:
            setup_match = re.fullmatch(
                r"Set up (?P<label>.+?) as (?:an?\s+)?(?P<type>[a-z_ ]+)\."
                r"(?:\s+Inside it:\s*(?P<contents>.*?))?\s*",
                command.strip(),
                re.I,
            )
            if setup_match is None:
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message="Name the new storage place and what kind of place it is.",
                )
            root_label = setup_match.group("label").strip(" .")
            root_type = setup_match.group("type").strip().lower().replace(" ", "_")
            if not root_label or root_type not in LOCATION_TYPES:
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message="Choose a name and a supported storage type for the new place.",
                )
            root = Location(
                id=slugify(root_label),
                type=root_type,
                label=root_label,
                accepts_items=default_accepts_items(root_type),
            )
            root_plan.append(
                {
                    "tool": "create_location",
                    "arguments": {"type": root_type, "label": root_label},
                }
            )
            setup_contents = (setup_match.group("contents") or "").strip(" .")
            if not setup_contents:
                return _storage_plan_chat_response(
                    command=command,
                    message=f"Create {root.label} as a top-level storage place.",
                    plan=root_plan,
                    trace_tool="plan_storage_setup",
                )
        else:
            root = locations.get(current_location_id or "")
            if root is None or not root.active:
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message="Choose the storage place you want to set up.",
                )
        if re.fullmatch(
            r"(?:nothing(?: else)?|none|no|skip|done|finished|that(?:'s| is) all)",
            command.strip(" .?!"),
            re.I,
        ):
            return _direct_chat_response(
                command=command,
                mode="setup",
                action="setup_complete",
                message=f"{root.label} is ready.",
                details={"location_id": root.id},
            )
        generated_match = re.search(
            r"\bit contains:\s*(?P<contents>.+?)\.\s*Propose\b",
            command,
            re.I,
        )
        setup_question = re.search(r"\bhelp me set up storage inside\b", command, re.I)
        if generated_match is None and setup_question:
            return _direct_chat_response(
                command=command,
                mode="clarify",
                action="clarify",
                message=f"What cabinets, shelves, drawers, totes, boxes, or other storage are inside {root.label}?",
                details={"location_id": root.id},
            )
        contents_text = (
            setup_contents
            if create_root
            else generated_match.group("contents") if generated_match else command
        )

        count_words = {
            "a": 1,
            "an": 1,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
        }
        type_aliases = {
            "cabinet": ("cabinet", "Cabinet"),
            "cabinets": ("cabinet", "Cabinet"),
            "closet": ("closet", "Closet"),
            "closets": ("closet", "Closet"),
            "drawer": ("drawer", "Drawer"),
            "drawers": ("drawer", "Drawer"),
            "tray": ("tray", "Tray"),
            "trays": ("tray", "Tray"),
            "bag": ("bag", "Bag"),
            "bags": ("bag", "Bag"),
            "organizer": ("organizer", "Organizer"),
            "organizers": ("organizer", "Organizer"),
            "shelf": ("shelf", "Shelf"),
            "shelves": ("shelf", "Shelf"),
            "tote": ("tote", "Tote"),
            "totes": ("tote", "Tote"),
            "box": ("box", "Box"),
            "boxes": ("box", "Box"),
            "bin": ("bin", "Bin"),
            "bins": ("bin", "Bin"),
            "pouch": ("pouch", "Pouch"),
            "pouches": ("pouch", "Pouch"),
            "rack": ("rack", "Rack"),
            "racks": ("rack", "Rack"),
            "locker": ("locker", "Locker"),
            "lockers": ("locker", "Locker"),
            "workbench": ("area", "Workbench"),
            "workbenches": ("area", "Workbench"),
        }

        def parse_location_phrase(raw: str) -> tuple[int, str, str] | None:
            phrase = re.sub(
                r"^(?:(?:and|also)\s+)?(?:the|my|our)?\s*",
                "",
                raw.strip(),
                flags=re.I,
            )
            quantity = 1
            count_match = re.match(r"^(?P<count>\d+|[a-z]+)\s+(?P<body>.+)$", phrase, re.I)
            if count_match:
                count_token = count_match.group("count").lower()
                parsed_count = int(count_token) if count_token.isdigit() else count_words.get(count_token)
                if parsed_count is not None:
                    quantity = parsed_count
                    phrase = count_match.group("body")
            type_match = re.search(r"(?P<kind>[a-z]+)$", phrase, re.I)
            if type_match is None:
                return None
            kind = type_match.group("kind").lower()
            location_type = type_aliases.get(kind)
            if location_type is None:
                return None
            prefix = phrase[: type_match.start()].strip()
            base_label = " ".join(part.capitalize() for part in prefix.split())
            base_label = f"{base_label} {location_type[1]}".strip()
            return max(1, min(quantity, 12)), base_label, location_type[0]

        def plural_label(label: str) -> str:
            if label.endswith("Shelf"):
                return f"{label[:-5]}Shelves"
            if label.endswith(("Box", "Pouch")):
                return f"{label}es"
            return f"{label}s"

        plan: list[dict[str, object]] = list(root_plan)
        summary: list[str] = []
        raw_parts = re.split(r"\s*(?:,|;)\s*|\s+and\s+", contents_text, flags=re.I)
        for raw_part in raw_parts:
            part = raw_part.strip(" ,;.")
            if not part:
                continue
            nested_parts = re.split(r"\s+with\s+", part, maxsplit=1, flags=re.I)
            parent_phrase = nested_parts[0]
            child_phrase = nested_parts[1] if len(nested_parts) == 2 else ""
            parsed_parent = parse_location_phrase(parent_phrase)
            if parsed_parent is None:
                continue
            parent_count, parent_label, parent_type = parsed_parent
            parent_labels = [parent_label] if parent_count == 1 else [f"{parent_label} {index}" for index in range(1, parent_count + 1)]
            for label in parent_labels:
                plan.append(
                    {
                        "tool": "create_location",
                        "arguments": {
                            "type": parent_type,
                            "label": label,
                            "parent_id": root.id,
                        },
                    }
                )
            summary.append(parent_label if parent_count == 1 else f"{parent_count} {plural_label(parent_label)}")
            if not child_phrase or parent_count != 1:
                continue
            parsed_child = parse_location_phrase(child_phrase)
            if parsed_child is None:
                continue
            child_count, child_label, child_type = parsed_child
            for index in range(1, child_count + 1):
                label = child_label if child_count == 1 else f"{child_label} {index}"
                plan.append(
                    {
                        "tool": "create_location",
                        "arguments": {
                            "type": child_type,
                            "label": label,
                            "parent_id": slugify(parent_label),
                        },
                    }
                )
            child_summary = child_label if child_count == 1 else plural_label(child_label)
            summary.append(f"{child_count} {child_summary} inside {parent_label}")
            if len(plan) >= 24:
                break

        if len(plan) == len(root_plan):
            return _direct_chat_response(
                command=command,
                mode="clarify",
                action="clarify",
                message=(
                    f"I could not identify the storage inside {root.label}. "
                    "Name cabinets, shelves, drawers, totes, boxes, bins, or racks."
                    if create_root
                    else f"What storage should I add inside {root.label} first?"
                ),
                details={"location_id": root.id},
            )
        return _storage_plan_chat_response(
            command=command,
            message=f"Set up {root.label} with {', '.join(summary)}.",
            plan=plan[:24],
            trace_tool="plan_storage_setup",
        )

    def _storage_creation_chat_response(command: str) -> dict[str, object] | None:
        cleaned = re.sub(r"\s+", " ", command).strip(" .?!")

        count_words = {
            "a": 1,
            "an": 1,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
        }
        child_types = {
            "cabinet": "cabinet",
            "cabinets": "cabinet",
            "closet": "closet",
            "closets": "closet",
            "drawer": "drawer",
            "drawers": "drawer",
            "tray": "tray",
            "trays": "tray",
            "bag": "bag",
            "bags": "bag",
            "organizer": "organizer",
            "organizers": "organizer",
            "shelf": "shelf",
            "shelves": "shelf",
            "tote": "tote",
            "totes": "tote",
            "box": "box",
            "boxes": "box",
            "bin": "bin",
            "bins": "bin",
            "pouch": "pouch",
            "pouches": "pouch",
            "rack": "rack",
            "racks": "rack",
            "locker": "locker",
            "lockers": "locker",
        }

        def display_label(raw: str) -> str:
            label = re.sub(r"\s+", " ", raw).strip(" ,;:.-")
            return label.title() if label and label == label.lower() else label

        def listed_root(raw: str) -> tuple[str, str] | None:
            phrase = re.sub(
                r"^(?:(?:and|also)\s+)?(?:an?|the|my|our)\s+",
                "",
                raw.strip(),
                flags=re.I,
            ).strip(" ,;:.-")
            if not phrase:
                return None
            generic = re.fullmatch(
                r"(?P<label>.+?)\s+(?:storage\s+)?(?:space|place|area)",
                phrase,
                re.I,
            )
            if generic:
                return display_label(generic.group("label")), "area"
            concrete = re.fullmatch(
                r".+\s+(?P<type>room|cabinet|closet|garage|shed|shelf|rack|storage unit)",
                phrase,
                re.I,
            )
            if concrete:
                root_type = {
                    "garage": "room",
                    "shed": "area",
                }.get(
                    concrete.group("type").lower(),
                    concrete.group("type").lower().replace(" ", "_"),
                )
                return display_label(phrase), root_type
            if phrase.lower() in {
                "attic",
                "basement",
                "garage",
                "shed",
                "closet",
                "workshop",
                "office",
            }:
                return display_label(phrase), (
                    "closet" if phrase.lower() == "closet" else "area"
                )
            return display_label(phrase), "area"

        def listed_child(raw: str) -> tuple[list[str], str] | None:
            phrase = re.sub(
                r"^(?:(?:and|also)\s+)?(?:the|my|our)\s+",
                "",
                raw.strip(),
                flags=re.I,
            ).strip(" ,;:.-")
            quantity = 1
            count_match = re.match(
                r"^(?P<count>\d+|[a-z]+)\s+(?P<body>.+)$", phrase, re.I
            )
            if count_match:
                token = count_match.group("count").lower()
                parsed_count = (
                    int(token) if token.isdigit() else count_words.get(token)
                )
                if parsed_count is not None:
                    quantity = max(1, min(parsed_count, 12))
                    phrase = count_match.group("body").strip()
            type_match = re.search(r"(?P<kind>[a-z]+)$", phrase, re.I)
            if type_match is None:
                return None
            location_type = child_types.get(type_match.group("kind").lower())
            if location_type is None:
                return None
            prefix = phrase[: type_match.start()].strip()
            base_label = display_label(f"{prefix} {location_type}".strip())
            labels = (
                [base_label]
                if quantity == 1
                else [f"{base_label} {index}" for index in range(1, quantity + 1)]
            )
            return labels, location_type

        setup_match = re.fullmatch(
            r"(?:please\s+)?set up (?:my|our|the) storage"
            r"(?:\s*[.:,-]?\s*(?:(?:i|we)\s+have\s*)?(?P<places>.*))?",
            cleaned,
            re.I,
        )
        if setup_match is not None:
            places_text = (setup_match.group("places") or "").strip()
            if not places_text:
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message="What rooms, cabinets, shelves, totes, or other storage places do you have?",
                )
            hierarchy_match = re.fullmatch(
                r"(?P<root>[^,;]+?)\s+with\s+(?P<children>.+)",
                places_text,
                re.I,
            )
            if hierarchy_match is not None:
                root = listed_root(hierarchy_match.group("root"))
                children = [
                    listed_child(raw_child)
                    for raw_child in re.split(
                        r"\s*(?:,|;)\s*|\s+and\s+",
                        hierarchy_match.group("children"),
                        flags=re.I,
                    )
                ]
                if root is not None and children and all(children):
                    root_label, root_type = root
                    plan: list[dict[str, object]] = [
                        {
                            "tool": "create_location",
                            "arguments": {"type": root_type, "label": root_label},
                        }
                    ]
                    child_summaries: list[str] = []
                    for parsed_child in children:
                        assert parsed_child is not None
                        labels, location_type = parsed_child
                        child_summaries.extend(labels)
                        plan.extend(
                            {
                                "tool": "create_location",
                                "arguments": {
                                    "type": location_type,
                                    "label": label,
                                    "parent_id": root_label,
                                },
                            }
                            for label in labels
                        )
                    names = (
                        child_summaries[0]
                        if len(child_summaries) == 1
                        else (
                            f"{', '.join(child_summaries[:-1])}, "
                            f"and {child_summaries[-1]}"
                        )
                    )
                    return _storage_plan_chat_response(
                        command=command,
                        message=f"Create {root_label} with {names} inside.",
                        plan=plan,
                        trace_tool="plan_storage_setup",
                    )
            roots: list[tuple[str, str]] = []
            seen_labels: set[str] = set()
            for raw_place in re.split(r"\s*(?:,|;)\s*|\s+and\s+", places_text, flags=re.I):
                root = listed_root(raw_place)
                if root is None or root[0].lower() in seen_labels:
                    continue
                seen_labels.add(root[0].lower())
                roots.append(root)
            if not roots:
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message="Name at least one storage place to begin.",
                )
            if len(roots) > 12:
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message="Start with up to 12 top-level storage places; we can add the rest next.",
                )
            plan = [
                {
                    "tool": "create_location",
                    "arguments": {"type": root_type, "label": label},
                }
                for label, root_type in roots
            ]
            names = [label for label, _ in roots]
            if len(names) == 1:
                name_list = names[0]
            else:
                name_list = f"{', '.join(names[:-1])}, and {names[-1]}"
            return _storage_plan_chat_response(
                command=command,
                message=f"Create {name_list} as top-level storage places.",
                plan=plan,
                trace_tool="plan_storage_setup",
            )

        match = re.fullmatch(
            r"(?:please\s+)?(?:create|add|make|set up)\s+"
            r"(?:an?\s+|the\s+|my\s+|our\s+)?"
            r"(?P<body>.+)",
            cleaned,
            re.I,
        )
        if match is None:
            return None

        body_parts = re.split(r"\s+with\s+", match.group("body"), maxsplit=1, flags=re.I)
        root_label = body_parts[0].strip()
        child_phrase = body_parts[1].strip() if len(body_parts) == 2 else ""
        root_type = ""
        generic_root = re.fullmatch(
            r"(?P<label>.+?)\s+(?:storage\s+)?(?:space|place|area)",
            root_label,
            re.I,
        )
        if generic_root:
            root_label = generic_root.group("label").strip()
            root_type = "area"
        else:
            concrete_root = re.fullmatch(
                r".+\s+(?P<type>room|cabinet|closet|garage|shed)",
                root_label,
                re.I,
            )
            if concrete_root:
                root_type = {
                    "garage": "room",
                    "shed": "area",
                }.get(
                    concrete_root.group("type").lower(),
                    concrete_root.group("type").lower(),
                )
            elif root_label.lower() in {"attic", "basement", "garage", "shed", "closet"}:
                root_type = "closet" if root_label.lower() == "closet" else "area"
        if not root_label or not root_type:
            return None

        plan: list[dict[str, object]] = [
            {
                "tool": "create_location",
                "arguments": {"type": root_type, "label": root_label},
            }
        ]
        child_label = ""
        child_type = ""
        if child_phrase:
            child_match = re.fullmatch(
                r"(?:an?\s+)?(?P<label>.+?\s+"
                r"(?P<type>bin|box|drawer|shelf|tray|bag|organizer|tote|pouch|container))"
                r"(?:\s+(?:inside|in it|within it))?",
                child_phrase,
                re.I,
            )
            if child_match is None:
                return None
            child_label = child_match.group("label").strip()
            child_type = child_match.group("type").lower()
            if child_type == "container":
                child_type = "box"
            plan.append(
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": child_type,
                        "label": child_label,
                        "parent_id": root_label,
                    },
                }
            )

        message = f"Create {root_label}"
        if child_label:
            message += f" with {child_label} inside"
        message += "."
        return _storage_plan_chat_response(
            command=command,
            message=message,
            plan=plan,
            trace_tool="plan_storage_setup",
        )

    def _storage_management_chat_response(
        command: str,
        *,
        photo_ids: list[int] | None = None,
    ) -> dict[str, object] | None:
        cleaned = re.sub(r"\s+", " ", command).strip(" .?!")
        proposal_photo_ids = list(dict.fromkeys(photo_ids or []))

        def clean_name(raw: str) -> str:
            return re.sub(
                r"^(?:(?:the|my|our)\s+)?(?:storage\s+(?:place|location)\s+)?",
                "",
                raw.strip(),
                flags=re.I,
            ).strip(" .?!")

        def matches(raw: str, *, active: bool) -> list[Location]:
            name = clean_name(raw)
            return match_locations(name, locations, active=active)

        def path_text(location: Location) -> str:
            return " > ".join(
                node.label or node.id for node in location_path(locations, location.id)
            )

        def clarify(
            message: str,
            candidates: list[Location] | None = None,
            command_for: Callable[[Location], str] | None = None,
        ) -> dict[str, object]:
            candidates = candidates or []
            if len(candidates) > 1:
                choices = [path_text(row) for row in candidates]
                message = f"{message.rstrip(' ?')}: {' or '.join(choices)}?"
            return _direct_chat_response(
                command=command,
                mode="clarify",
                action="clarify",
                message=message,
                details={
                    "location_candidates": [
                        {"id": location.id, "label": location.label, "type": location.type}
                        | {
                            "path": [
                                {"id": node.id, "label": node.label or node.id}
                                for node in location_path(locations, location.id)
                            ],
                            "selection_command": command_for(location) if command_for else "",
                        }
                        for location in candidates
                    ]
                },
            )

        archive_match = re.fullmatch(
            r"(?:please\s+)?(?:archive|deactivate|retire)\s+(?P<location>.+)",
            cleaned,
            re.I,
        )
        if archive_match:
            raw_location = archive_match.group("location")
            candidates = matches(raw_location, active=True)
            if len(candidates) > 1:
                return clarify(
                    "Which storage location should I archive?",
                    candidates,
                    lambda row: f"Archive {path_text(row)}",
                )
            if not candidates:
                archived = matches(raw_location, active=False)
                if archived:
                    return clarify(f"{archived[0].label} is already archived.")
                return clarify(f"I could not find an active storage location named {clean_name(raw_location)}.")
            location = candidates[0]
            plan = [
                {
                    "tool": "archive_location",
                    "arguments": {"location_id": location.id},
                }
            ]
            return _storage_plan_chat_response(
                command=command,
                message=f"Archive {location.label} and preserve its items, nested locations, and history.",
                plan=plan,
                trace_tool="plan_storage_management",
            )

        restore_match = re.fullmatch(
            r"(?:please\s+)?(?:restore|unarchive|reactivate)\s+(?P<location>.+)",
            cleaned,
            re.I,
        )
        if restore_match:
            raw_location = restore_match.group("location")
            candidates = matches(raw_location, active=False)
            if len(candidates) > 1:
                return clarify(
                    "Which archived storage location should I restore?",
                    candidates,
                    lambda row: f"Restore {path_text(row)}",
                )
            if not candidates:
                active = matches(raw_location, active=True)
                if active:
                    return clarify(f"{active[0].label} is already active.")
                return clarify(f"I could not find an archived storage location named {clean_name(raw_location)}.")
            location = candidates[0]
            plan = [
                {
                    "tool": "restore_location",
                    "arguments": {"location_id": location.id},
                }
            ]
            return _storage_plan_chat_response(
                command=command,
                message=f"Restore {location.label} to active storage.",
                plan=plan,
                trace_tool="plan_storage_management",
            )

        move_command = re.sub(
            r"\s+and\s+(?:keep|attach|save|include)\s+"
            r"(?:(?:the|this|that)\s+)?photos?"
            r"(?:\s+(?:with|to)\s+(?:the\s+)?"
            r"(?:item|inventory(?:\s+record)?))?\s*$",
            "",
            cleaned,
            flags=re.I,
        )
        move_match = re.fullmatch(
            r"(?:please\s+)?(?:move|relocate|put)\s+(?P<source>.+?)\s+"
            r"(?:in|into|inside|under|to)\s+(?P<destination>.+)",
            move_command,
            re.I,
        )
        if move_match is None:
            return None
        raw_destination = re.sub(
            r"^(?:the\s+)?existing\s+",
            "",
            clean_name(move_match.group("destination")),
            flags=re.I,
        )
        raw_source = clean_name(move_match.group("source"))
        source_candidates = matches(raw_source, active=True)
        if not source_candidates:
            item_rows = [
                row
                for row in store.exact_match(raw_source)
                if (stored_location := locations.get(row.bin_id)) is not None
                and stored_location.active
            ]
            item_ids = {row.item_id for row in item_rows}
            if not item_rows:
                destination_candidates = matches(raw_destination, active=True)
                if proposal_photo_ids and len(destination_candidates) == 1:
                    destination = destination_candidates[0]
                    return _assistant_ask_response(
                        AssistantFindRequest(
                            query=f"{raw_source} in {destination.id}",
                            photo_ids=proposal_photo_ids,
                            current_location_id=destination.id,
                        ),
                        route="/assistant/chat",
                        put_away_intent=True,
                        recorded_command=command,
                    )
                return None
            if len(item_ids) > 1:
                names = sorted({row.name for row in item_rows})
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message=f"Which item should I move: {' or '.join(names)}?",
                )
            if len(item_rows) > 1:
                source_paths = [
                    " > ".join(
                        node.label or node.id
                        for node in location_path(locations, row.bin_id)
                    )
                    for row in item_rows
                ]
                return _direct_chat_response(
                    command=command,
                    mode="clarify",
                    action="clarify",
                    message=(
                        f"{item_rows[0].name} is stored in more than one place. "
                        f"Move it from {' or '.join(source_paths)}?"
                    ),
                )
            destination_candidates = matches(raw_destination, active=True)
            if len(destination_candidates) > 1:
                return clarify(
                    "Which destination storage location do you mean?",
                    destination_candidates,
                    lambda row: f"Move {item_rows[0].name} to {path_text(row)}",
                )
            if not destination_candidates:
                return clarify(
                    f"I could not find an active storage location named {raw_destination}."
                )
            item_row = item_rows[0]
            destination = destination_candidates[0]
            if item_row.bin_id == destination.id:
                return clarify(f"{item_row.name} is already in {destination.label}.")
            plan = [
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": item_row.item_id,
                        "item": item_row.name,
                        "from_location_id": item_row.bin_id,
                        "to_location_id": destination.id,
                    },
                }
            ]
            source_location = locations.get(item_row.bin_id)
            source_label = (
                source_location.label or source_location.id
                if source_location is not None
                else item_row.bin_id
            )
            return _storage_plan_chat_response(
                command=command,
                message=(
                    f"Move {item_row.name} from {source_label} to {destination.label}."
                ),
                plan=plan,
                trace_tool="plan_item_move",
                photo_ids=proposal_photo_ids,
            )
        if len(source_candidates) > 1:
            return clarify(
                "Which storage location should I move?",
                source_candidates,
                lambda row: f"Move {path_text(row)} into {raw_destination}",
            )
        source = source_candidates[0]
        top_level = raw_destination.lower() in {"top", "top level", "root", "no parent"}
        destination = None
        if not top_level:
            destination_candidates = matches(raw_destination, active=True)
            if len(destination_candidates) > 1:
                return clarify(
                    "Which destination storage location do you mean?",
                    destination_candidates,
                    lambda row: f"Move {source.id} into {path_text(row)}",
                )
            if not destination_candidates:
                return clarify(f"I could not find an active storage location named {raw_destination}.")
            destination = destination_candidates[0]
            if destination.id in locations.subtree_ids(source.id):
                return clarify(f"{source.label} cannot be moved inside itself or one of its nested locations.")
        new_parent_id = destination.id if destination is not None else None
        if source.parent_id == new_parent_id:
            place = destination.label if destination is not None else "the top level"
            return clarify(f"{source.label} is already in {place}.")
        plan = [
            {
                "tool": "move_container",
                "arguments": {
                    "location_id": source.id,
                    "new_parent_id": new_parent_id,
                },
            }
        ]
        place = destination.label if destination is not None else "the top level"
        return _storage_plan_chat_response(
            command=command,
            message=f"Move {source.label} into {place}.",
            plan=plan,
            trace_tool="plan_storage_management",
        )

    def _direct_chat_response(
        *,
        command: str,
        mode: str,
        action: str,
        message: str,
        details: dict[str, object] | None = None,
        photo_ids: list[int] | None = None,
    ) -> dict[str, object]:
        decision: dict[str, object] = {
            "action": action,
            "message": message,
            "results": [],
            "plan": [],
        }
        if details:
            decision.update(details)
        response: dict[str, object] = {
            "ok": True,
            "mode": mode,
            "route": "/assistant/chat",
            "message": message,
            "decision": decision,
            "automation": {"lit": False, "results": []},
        }
        interaction_id = store.record_assistant_interaction(
            route="/assistant/chat",
            command=command,
            photo_ids=photo_ids or [],
            response=response,
        )
        response["interaction_id"] = interaction_id
        return response

    def _size_word(text: str) -> str | None:
        normalized = re.sub(r"[-_]+", " ", text.lower())
        aliases = {
            "extra small": "tiny",
            "extra tiny": "tiny",
            "very small": "tiny",
            "tiny": "tiny",
            "small": "small",
            "medium": "medium",
            "med": "medium",
            "big": "large",
            "large": "large",
            "extra large": "oversize",
            "oversized": "oversize",
            "oversize": "oversize",
        }
        for phrase, size in sorted(aliases.items(), key=lambda entry: len(entry[0]), reverse=True):
            if re.search(rf"\b{re.escape(phrase)}\b", normalized):
                return size
        return None

    def _context_item_name(context: dict[str, object]) -> str:
        item = context.get("item")
        if isinstance(item, dict):
            return str(item.get("name") or "").strip()
        return ""

    def _context_item_id(context: dict[str, object]) -> int | None:
        value = context.get("saved_item_id")
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            return None
        return item_id if item_id > 0 else None

    def _context_destination_id(context: dict[str, object]) -> str | None:
        destination = context.get("destination")
        if not isinstance(destination, dict):
            return None
        raw = str(destination.get("id") or "").strip()
        return raw or None

    def _size_rule_terms(item_name: str) -> list[str]:
        ignored = {"a", "an", "and", "are", "is", "the", "this", "that", "these", "those"}
        return [
            term
            for term in re.findall(r"[a-z0-9]+", item_name.lower())
            if len(term) > 1 and term not in ignored and term not in CAPACITY_RANK
        ]

    def _placement_destination_from_followup(
        text: str,
        *,
        exclude_item_id: int | None = None,
    ) -> tuple[str | None, list[dict[str, object]]]:
        cleaned = text.strip().rstrip(".!?")
        if dest := _existing_location_id(cleaned):
            return dest, []

        def search_destination(raw: str) -> tuple[str | None, list[dict[str, object]]]:
            candidates: dict[str, dict[str, object]] = {}
            for result in store.search(raw, limit=5):
                if exclude_item_id is not None and result.item_id == exclude_item_id:
                    continue
                if _existing_location_id(result.bin_id):
                    candidates.setdefault(
                        result.bin_id,
                        {
                            "location_id": result.bin_id,
                            "item": result.name,
                            "path": _path_for(result.bin_id),
                        },
                    )
            if len(candidates) == 1:
                return next(iter(candidates)), []
            if len(candidates) > 1:
                return None, list(candidates.values())
            return None, []

        patterns = [
            r"\b(?:in|into|to|with)\s+(.+)$",
            r"\b(?:belongs?|goes?|should go|should be)\s+(?:in|into|to|with)\s+(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, cleaned, re.I)
            if not match:
                continue
            raw = match.group(1).strip()
            raw = re.sub(r"^(?:the|a|an)\s+", "", raw, flags=re.I)
            raw = re.sub(r"\b(?:from now on|instead|please)$", "", raw, flags=re.I).strip()
            if dest := _existing_location_id(raw):
                return dest, []
            dest, candidates = search_destination(raw)
            if dest or candidates:
                return dest, candidates
        if _size_word(cleaned) is None and len(cleaned.split()) <= 5:
            return search_destination(cleaned)
        return None, []

    def _learned_placement_response(
        *,
        text: str,
        item_name: str,
        item_id: int,
        from_location_id: str,
        to_location_id: str,
    ) -> dict[str, object]:
        moved = store.move_inventory(
            item_id=item_id,
            from_bin=from_location_id,
            to_bin=to_location_id,
            normalize_bin=False,
        )
        if not moved:
            return {"ok": True, "handled": False, "reason": "item was not in source location"}
        trigger_terms = _size_rule_terms(item_name) or [item_name.lower()]
        place = " › ".join(str(p["label"]) for p in _path_for(to_location_id))
        rule_text = f"{item_name} belongs in {place}."
        existing_rule = next(
            (
                rule
                for rule in store.rules(active_only=True, limit=1000)
                if rule.rule_text.casefold() == rule_text.casefold()
            ),
            None,
        )
        rule_id = store.learn_rule(
            rule_text=rule_text,
            trigger_terms=trigger_terms,
            target_location_id=to_location_id,
            source_command=text,
        )
        superseded_rule_ids: list[int] = []
        learned_rule = store.rule(rule_id)
        trigger_set = set(learned_rule.trigger_terms if learned_rule else trigger_terms)
        for rule in store.rules(active_only=True, limit=1000):
            if (
                rule.id != rule_id
                and set(rule.trigger_terms) == trigger_set
                and rule.target_location_id != to_location_id
                and store.deactivate_rule(rule_id=rule.id, rule_type="placement")
            ):
                superseded_rule_ids.append(rule.id)
        action = {
            "tool": "learn_rule",
            "arguments": {
                "rule_text": rule_text,
                "trigger_terms": trigger_terms,
                "target_location_id": to_location_id,
                "rule_id": rule_id,
                "provisional": existing_rule is None,
            },
        }
        example_id, review_status = _record_applied_learning_example(
            command=text,
            action=[action],
            kind="correction",
            context={
                "rule_id": rule_id,
                "rule_type": "placement",
                "provisional": existing_rule is None,
            },
        )
        lit_location = locations.nearest_lit(to_location_id)
        lit = _light_pixels(
            lit_location.light_indexes if lit_location is not None else [],
            target=str(lit_location.light_target or "") if lit_location else "",
        )
        response = {
            "ok": True,
            "handled": True,
            "action": "learned_placement_correction",
            "message": f"Moved {item_name} to {place}. I learned that as a reusable rule.",
            "item": item_name,
            "item_id": item_id,
            "from_location_id": from_location_id,
            "to_location_id": to_location_id,
            "destination": {"id": to_location_id, "path": _path_for(to_location_id)},
            "rule_id": rule_id,
            "rule_text": rule_text,
            "trigger_terms": trigger_terms,
            "superseded_rule_ids": superseded_rule_ids,
            "proposed_rule": {
                "example_id": example_id,
                "rule_id": rule_id,
                "rule_type": "placement",
                "review_status": review_status,
                "provisional": existing_rule is None,
            },
            "lit": lit,
        }
        store.record_assistant_interaction(
            route="/assistant/followup",
            command=text,
            response=response,
        )
        _record_agent_correction_application(response)
        return response

    def _quantity_correction_from_followup(text: str) -> tuple[float, str | None] | None:
        cleaned = text.strip()
        if re.fullmatch(r"B\d{1,4}", cleaned, re.I):
            return None
        match = re.search(
            r"\b(?:actually|there\s+(?:are|is)|qty|quantity|count|make\s+it|should\s+be|it(?:'s| is))?\s*(\d+(?:\.\d+)?)\s*(each|pieces?|pcs?|pack|packs|sets?)?\b",
            cleaned,
            re.I,
        )
        if not match:
            return None
        quantity = float(match.group(1))
        if quantity <= 0:
            return None
        raw_unit = (match.group(2) or "").strip().lower()
        units = {
            "piece": "pieces",
            "pieces": "pieces",
            "pc": "pieces",
            "pcs": "pieces",
            "pack": "pack",
            "packs": "packs",
            "set": "set",
            "sets": "sets",
            "each": "each",
        }
        return quantity, units.get(raw_unit) if raw_unit else None

    def _identity_correction_from_followup(text: str) -> str | None:
        cleaned = text.strip().rstrip(".!?")
        patterns = [
            r"^(?:actually\s+)?(?:it|that|this)(?:'s|\s+is)\s+(.+)$",
            r"^(?:actually\s+)?(?:rename|call)\s+(?:it|that|this)\s+(?:to\s+)?(.+)$",
            r"^(?:actually\s+)?(?:it|that|this)\s+is\s+called\s+(.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, cleaned, re.I)
            if not match:
                continue
            name = match.group(1).strip()
            name = re.sub(r"^(?:a|an|the)\s+", "", name, flags=re.I).strip()
            if not name or _size_word(name) is not None:
                return None
            if re.fullmatch(r"\d+(?:\.\d+)?(?:\s+\w+)?", name):
                return None
            return title_item_name(name)
        return None

    def _category_correction_from_followup(text: str) -> str | None:
        cleaned = text.strip().rstrip(".!?")
        match = re.match(
            r"^(?:actually\s+)?(?:category|cat|group|type)\s+(?:is|=|should\s+be)\s+(.+)$",
            cleaned,
            re.I,
        ) or re.match(
            r"^(?:actually\s+)?(?:put|mark|classify|file)\s+(?:it|that|this)\s+as\s+(.+)$",
            cleaned,
            re.I,
        )
        if not match:
            return None
        category = match.group(1).strip()
        category = re.sub(r"^(?:a|an|the)\s+", "", category, flags=re.I).strip()
        if not category or _size_word(category) is not None:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?(?:\s+\w+)?", category):
            return None
        return category.lower()

    def _alias_correction_from_followup(text: str) -> str | None:
        cleaned = text.strip().rstrip(".!?")
        patterns = [
            r"^(?:also\s+)?(?:called|known\s+as|aka)\s+(.+)$",
            r"^(?:alias|nickname)\s+(?:is|=|should\s+be)\s+(.+)$",
            r"^(?:add\s+)?(?:alias|aka)\s+(.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, cleaned, re.I)
            if not match:
                continue
            alias = match.group(1).strip()
            alias = re.sub(r"^(?:a|an|the)\s+", "", alias, flags=re.I).strip()
            if not alias or _size_word(alias) is not None:
                return None
            if re.fullmatch(r"B\d{1,4}", alias, re.I):
                return None
            if re.fullmatch(r"\d+(?:\.\d+)?(?:\s+\w+)?", alias):
                return None
            return alias
        return None

    def _learned_category_response(
        *,
        text: str,
        item_name: str,
        item_id: int,
        category: str,
    ) -> dict[str, object]:
        rows = store.item_inventory(item_id)
        current = rows[0] if rows else None
        if current is None:
            return {"ok": True, "handled": False, "reason": "item was not found"}
        before = current.as_dict()
        try:
            store.update_item(
                item_id=item_id,
                name=current.name,
                description=current.description,
                category=category,
                aliases=current.aliases,
            )
        except ValueError as exc:
            return {"ok": True, "handled": False, "reason": str(exc)}
        after_rows = store.item_inventory(item_id)
        after = after_rows[0].as_dict() if after_rows else {**before, "category": category}
        _record_inventory_correction_example(before=before, after=after, command_prefix="follow-up corrected")
        response = {
            "ok": True,
            "handled": True,
            "action": "learned_category_correction",
            "message": f"Got it. I marked {item_name} as {category}.",
            "item": item_name,
            "item_id": item_id,
            "category": category,
        }
        store.record_assistant_interaction(
            route="/assistant/followup",
            command=text,
            response=response,
        )
        _record_agent_correction_application(response)
        return response

    def _learned_alias_response(
        *,
        text: str,
        item_name: str,
        item_id: int,
        alias: str,
    ) -> dict[str, object]:
        rows = store.item_inventory(item_id)
        current = rows[0] if rows else None
        if current is None:
            return {"ok": True, "handled": False, "reason": "item was not found"}
        aliases_lower = {known.lower() for known in current.aliases}
        if alias.lower() == current.name.lower() or alias.lower() in aliases_lower:
            response = {
                "ok": True,
                "handled": True,
                "action": "learned_alias_correction",
                "message": f"I already know {current.name} as {alias}.",
                "item": current.name,
                "item_id": item_id,
                "alias": alias,
                "aliases": current.aliases,
            }
            store.record_assistant_interaction(
                route="/assistant/followup",
                command=text,
                response=response,
            )
            _record_agent_correction_application(response)
            return response
        before = current.as_dict()
        store.learn_alias(item_id=item_id, alias=alias)
        after_rows = store.item_inventory(item_id)
        after = after_rows[0].as_dict() if after_rows else {**before, "aliases": [*current.aliases, alias]}
        _record_inventory_correction_example(before=before, after=after, command_prefix="follow-up corrected")
        response = {
            "ok": True,
            "handled": True,
            "action": "learned_alias_correction",
            "message": f"Got it. {current.name} is also called {alias}.",
            "item": current.name,
            "item_id": item_id,
            "alias": alias,
            "aliases": after.get("aliases", [*current.aliases, alias]),
        }
        store.record_assistant_interaction(
            route="/assistant/followup",
            command=text,
            response=response,
        )
        _record_agent_correction_application(response)
        return response

    def _learned_identity_response(
        *,
        text: str,
        item_name: str,
        item_id: int,
        new_name: str,
    ) -> dict[str, object]:
        rows = store.item_inventory(item_id)
        current = rows[0] if rows else None
        if current is None:
            return {"ok": True, "handled": False, "reason": "item was not found"}
        aliases = [
            alias
            for alias in [*current.aliases, item_name]
            if alias.strip() and alias.strip().lower() != new_name.lower()
        ]
        before = current.as_dict()
        try:
            store.update_item(
                item_id=item_id,
                name=new_name,
                description=current.description,
                category=current.category,
                aliases=aliases,
            )
        except ValueError as exc:
            return {"ok": True, "handled": False, "reason": str(exc)}
        after_rows = store.item_inventory(item_id)
        after = after_rows[0].as_dict() if after_rows else {**before, "name": new_name, "aliases": aliases}
        _record_inventory_correction_example(before=before, after=after, command_prefix="follow-up corrected")
        response = {
            "ok": True,
            "handled": True,
            "action": "learned_identity_correction",
            "message": f"Got it. I renamed {item_name} to {new_name}.",
            "item": new_name,
            "item_id": item_id,
            "previous_name": item_name,
            "aliases": aliases,
        }
        store.record_assistant_interaction(
            route="/assistant/followup",
            command=text,
            response=response,
        )
        _record_agent_correction_application(response)
        return response

    def _learned_quantity_response(
        *,
        text: str,
        item_name: str,
        item_id: int,
        location_id: str,
        quantity: float,
        unit: str | None,
    ) -> dict[str, object]:
        current = next(
            (item for item in store.items_in_location(location_id) if item.item_id == item_id),
            None,
        )
        if current is None:
            return {"ok": True, "handled": False, "reason": "item was not in source location"}
        clean_unit = unit or current.unit or "each"
        store.put_inventory(
            item_id=item_id,
            bin_id=location_id,
            quantity=quantity,
            unit=clean_unit,
            normalize_bin=False,
        )
        action = {
            "tool": "update_item_quantity",
            "arguments": {
                "item": item_name,
                "location_id": location_id,
                "quantity": quantity,
                "unit": clean_unit,
            },
        }
        store.record_example(command=text, action=json.dumps(action), kind="correction")
        response = {
            "ok": True,
            "handled": True,
            "action": "learned_quantity_correction",
            "message": f"Got it. {item_name} is now {quantity:g} {clean_unit}.",
            "item": item_name,
            "item_id": item_id,
            "location_id": location_id,
            "quantity": quantity,
            "unit": clean_unit,
        }
        store.record_assistant_interaction(
            route="/assistant/followup",
            command=text,
            response=response,
        )
        _record_agent_correction_application(response)
        return response

    @app.post("/assistant/followup")
    def assistant_followup(request: AssistantFollowupRequest) -> dict[str, object]:
        context = request.context or {}
        if context.get("kind") != "filed":
            return {"ok": True, "handled": False, "reason": "no filed context"}
        item_name = _context_item_name(context)
        item_id = _context_item_id(context)
        source_location_id = _context_destination_id(context)
        if item_name and item_id and source_location_id:
            dest, candidates = _placement_destination_from_followup(
                request.text,
                exclude_item_id=item_id,
            )
            if candidates:
                response = {
                    "ok": True,
                    "handled": True,
                    "action": "clarify_placement_destination",
                    "message": "I found more than one matching place. Which one should I use?",
                    "candidates": candidates,
                }
                store.record_assistant_interaction(
                    route="/assistant/followup",
                    command=request.text,
                    response=response,
                )
                return response
            if dest:
                if dest != source_location_id:
                    return _learned_placement_response(
                        text=request.text,
                        item_name=item_name,
                        item_id=item_id,
                        from_location_id=source_location_id,
                        to_location_id=dest,
                    )
            if quantity := _quantity_correction_from_followup(request.text):
                return _learned_quantity_response(
                    text=request.text,
                    item_name=item_name,
                    item_id=item_id,
                    location_id=source_location_id,
                    quantity=quantity[0],
                    unit=quantity[1],
                )
            if new_name := _identity_correction_from_followup(request.text):
                if new_name.lower() != item_name.lower():
                    return _learned_identity_response(
                        text=request.text,
                        item_name=item_name,
                        item_id=item_id,
                        new_name=new_name,
                    )
            if category := _category_correction_from_followup(request.text):
                return _learned_category_response(
                    text=request.text,
                    item_name=item_name,
                    item_id=item_id,
                    category=category,
                )
            if alias := _alias_correction_from_followup(request.text):
                return _learned_alias_response(
                    text=request.text,
                    item_name=item_name,
                    item_id=item_id,
                    alias=alias,
                )

        size_class = _size_word(request.text)
        if not item_name or size_class is None:
            return {"ok": True, "handled": False, "reason": "no deterministic correction"}

        rule_text = f"{item_name} should be {size_class}."
        trigger_terms = _size_rule_terms(item_name) or [item_name.lower()]
        existing_rule = next(
            (
                rule
                for rule in store.size_rules(active_only=True, limit=1000)
                if rule.rule_text.casefold() == rule_text.casefold()
            ),
            None,
        )
        rule_id = store.learn_size_rule(
            rule_text=rule_text,
            trigger_terms=trigger_terms,
            size_class=size_class,
            source_command=request.text,
        )
        if item_id := _context_item_id(context):
            try:
                store.update_item_size(
                    item_id=item_id,
                    size_class=size_class,
                    source="followup",
                    confidence=1.0,
                )
            except ValueError:
                pass
        action = {
            "tool": "learn_size_rule",
            "arguments": {
                "rule_text": rule_text,
                "trigger_terms": trigger_terms,
                "size_class": size_class,
                "rule_id": rule_id,
                "provisional": existing_rule is None,
            },
        }
        example_id, review_status = _record_applied_learning_example(
            command=request.text,
            action=[action],
            kind="correction",
            context={
                "rule_id": rule_id,
                "rule_type": "size",
                "provisional": existing_rule is None,
            },
        )
        response = {
            "ok": True,
            "handled": True,
            "action": "learned_size_correction",
            "message": f"Got it. I learned to treat {item_name} as {size_class}.",
            "item": item_name,
            "size_class": size_class,
            "rule_id": rule_id,
            "rule_text": rule_text,
            "trigger_terms": trigger_terms,
            "proposed_rule": {
                "example_id": example_id,
                "rule_id": rule_id,
                "rule_type": "size",
                "review_status": review_status,
                "provisional": existing_rule is None,
            },
        }
        store.record_assistant_interaction(
            route="/assistant/followup",
            command=request.text,
            response=response,
        )
        _record_agent_correction_application(response)
        return response

    @app.post("/assistant/chat")
    def assistant_chat(request: AssistantChatRequest) -> dict[str, object]:
        client_request_id = request.client_request_id.strip()
        if client_request_id and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", client_request_id):
            raise HTTPException(status_code=400, detail="client_request_id is invalid")
        if client_request_id:
            interaction = store.assistant_interaction_by_client_request_id(
                client_request_id,
                route="/assistant/chat",
            )
            if interaction is not None:
                stored = interaction.get("response")
                if isinstance(stored, dict):
                    cached = stored.get("client_response")
                    if isinstance(cached, dict):
                        return {**cached, "idempotent_replay": True}

        def finalize(response: dict[str, object]) -> dict[str, object]:
            if not client_request_id:
                return response
            response["client_request_id"] = client_request_id
            interaction_id = response.get("interaction_id")
            if not isinstance(interaction_id, int):
                raw = response.get("raw")
                if isinstance(raw, dict):
                    interaction_id = raw.get("interaction_id")
            if isinstance(interaction_id, int):
                store.update_assistant_interaction_response(
                    interaction_id,
                    {
                        "client_request_id": client_request_id,
                        "client_response": response,
                    },
                )
            return response

        def annotate_voice(response: dict[str, object]) -> dict[str, object]:
            proposal = response
            if not isinstance(proposal.get("plan"), list):
                for key in ("decision", "raw"):
                    nested = response.get(key)
                    if isinstance(nested, dict) and isinstance(nested.get("plan"), list):
                        proposal = nested
                        break
            presentation = _confirmation_presentation({"response": proposal})
            if presentation is not None:
                proposal["confirmation"] = presentation
                response["confirmation"] = presentation
                interaction_id = proposal.get("interaction_id") or response.get(
                    "interaction_id"
                )
                if isinstance(interaction_id, int):
                    store.update_assistant_interaction_response(
                        interaction_id,
                        {"confirmation": presentation},
                    )
            if request.source.strip().lower() == "voice":
                voice = {
                    "transcript": request.voice_transcript.strip(),
                    "command": request.voice_command.strip() or request.text.strip(),
                    "query": request.voice_query.strip() or request.text.strip(),
                    "stt_model": request.stt_model.strip(),
                }
                response["source"] = "voice"
                response["voice"] = voice
                raw = response.get("raw")
                if isinstance(raw, dict):
                    raw["source"] = "voice"
                    raw["voice"] = voice
                interaction_id = response.get("interaction_id")
                if not isinstance(interaction_id, int) and isinstance(raw, dict):
                    interaction_id = raw.get("interaction_id")
                if isinstance(interaction_id, int):
                    store.update_assistant_interaction_response(
                        interaction_id,
                        {"source": "voice", "voice": voice},
                    )
            _record_agent_response_evidence(response)
            return finalize(response)

        chat_text = _canonical_voice_chat_text(request.text, request.source)
        request_mode = request.mode.strip().lower()

        def voice_place_configuration_response() -> dict[str, object] | None:
            if request.source.strip().lower() != "voice":
                return None
            cleaned = re.sub(r"\s+", " ", chat_text).strip(" .?!")
            place_nouns = (
                r"(?:storage|place|location|home|room|area|garage|attic|basement|"
                r"shed|workshop|office|cabinet|closet|shelf|drawer|tray|bag|organizer|tote|box|bin|pouch|"
                r"rack|locker|storage unit)"
            )
            config_request = request_mode in {"setup", "setup_new"}
            config_request = config_request or bool(
                re.search(
                    rf"\b(?:set up|create|add|make|rename|delete|remove)\b.*\b{place_nouns}\b",
                    cleaned,
                    re.I,
                )
            )
            config_request = config_request or bool(
                re.fullmatch(
                    r"(?:please\s+)?(?:archive|deactivate|retire|restore|unarchive|"
                    r"reactivate)\s+.+",
                    cleaned,
                    re.I,
                )
            )
            move_match = re.fullmatch(
                r"(?:please\s+)?(?:move|relocate|put)\s+(?P<source>.+?)\s+"
                r"(?:in|into|inside|under|to)\s+.+",
                cleaned,
                re.I,
            )
            if move_match is not None:
                source_name = re.sub(
                    r"^(?:(?:the|my|our)\s+)?(?:storage\s+(?:place|location)\s+)?",
                    "",
                    move_match.group("source").strip(),
                    flags=re.I,
                )
                config_request = bool(
                    match_locations(source_name, locations, active=True)
                )
            filing_query = _strip_put_intent(cleaned)
            container_match = re.search(
                r"\s+(?:in|into|inside|to)\s+"
                r"(?:(?:a|an|the|their|its|my|our)\s+)*"
                r"(?:new|own|separate)\s+"
                r"(?P<label>(?:[a-z0-9-]+\s+){0,4}?"
                r"(?P<type>pouch|bin|box|tote|tray|bag|organizer|drawer|shelf|cabinet|closet|rack|locker))"
                r"(?:\s+(?:in|inside|under)\s+(?P<parent>.+))?$",
                filing_query,
                re.I,
            )
            filing_request = bool(
                container_match
                and (
                    _put_away_requested(cleaned)
                    or re.match(
                        r"^(?:(?:let(?:'s| us)|please)\s+)?"
                        r"(?:move|relocate)\s+(?:this|that|these|those)\b",
                        cleaned,
                        re.I,
                    )
                )
            )
            config_request = config_request or filing_request
            if not config_request:
                return None
            response = _direct_chat_response(
                command=chat_text,
                mode="clarify",
                action="place_configuration_ui_required",
                message=(
                    (
                        "Open Places to create or choose the destination. "
                        "I kept the item and photos ready so put-away can continue there."
                    )
                    if filing_request
                    else (
                        "Use Storage, then Places, to change storage locations. "
                        "By voice, I can find items or help put them away."
                    )
                ),
                details={"places_url": "/storage?view=places"},
                photo_ids=request.photo_ids,
            )
            if not filing_request or container_match is None:
                return response
            interaction_id = int(response["interaction_id"])
            item_query = filing_query[: container_match.start()].strip(" ,.;:-")
            requested_container_capacity = (
                requested_capacity(cleaned) or infer_item_capacity(item_query)
            )
            parent_hint = str(container_match.group("parent") or "").strip(" ,.;:-")
            parent_lookup = re.sub(
                r"^(?:(?:the|my|our)\s+)?(?:storage\s+(?:place|location)\s+)?",
                "",
                parent_hint,
                flags=re.I,
            ).strip()
            parent_candidates = (
                match_locations(parent_lookup, locations, active=True)
                if parent_lookup
                else []
            )
            suggested_parent_id = (
                parent_candidates[0].id if len(parent_candidates) == 1 else None
            )
            handoff = {
                "kind": "put_away_destination",
                "command": chat_text,
                "resume_command": f"Put away {item_query}".strip(),
                "photo_ids": list(request.photo_ids),
                "current_location_id": request.current_location_id,
                "suggested_container": {
                    "label": str(container_match.group("label") or "").strip(),
                    "type": str(container_match.group("type") or "").strip().lower(),
                    "capacity_class": requested_container_capacity,
                    "parent_hint": parent_hint,
                    "parent_id": suggested_parent_id,
                },
            }
            places_url = (
                f"/storage?view=places&resume={interaction_id}"
                + (
                    f"&location={quote(suggested_parent_id, safe='')}"
                    if suggested_parent_id
                    else ""
                )
            )
            decision = response["decision"]
            if isinstance(decision, dict):
                decision["places_url"] = places_url
                decision["handoff"] = handoff
                store.update_assistant_interaction_response(
                    interaction_id,
                    {"decision": decision},
                )
            return response

        if re.fullmatch(r"(?:hey\s+|ok(?:ay)?\s+)?magpie", chat_text, re.I):
            return annotate_voice(
                _direct_chat_response(
                    command=chat_text,
                    mode="clarify",
                    action="clarify",
                    message="What should I find or put away?",
                )
            )
        if re.fullmatch(
            r"(?:please\s+)?(?:clear(?:\s+(?:all\s+)?(?:the\s+)?lights?)?|"
            r"turn\s+off\s+(?:all\s+)?(?:the\s+)?lights?)",
            chat_text,
            re.I,
        ):
            hardware_errors = _clear_active_finds()
            return annotate_voice(
                _direct_chat_response(
                    command=chat_text,
                    mode="command",
                    action="lights_cleared",
                    message=(
                        "Active locations cleared."
                        if not hardware_errors
                        else (
                            "Active locations cleared. The light controller did not "
                            "confirm the update."
                        )
                    ),
                    details={
                        "active_finds": [],
                        "light_ok": not hardware_errors,
                        "light_errors": hardware_errors,
                    },
                )
            )
        not_here_match = re.fullmatch(
            r"(?:please\s+)?(?:(?P<item>.+?)\s+)?"
            r"(?:(?:is|are)\s+(?:not here|missing)|"
            r"(?:isn't|aren't)\s+here|not here|missing)",
            chat_text,
            re.I,
        )
        if not_here_match:
            item_hint = re.sub(
                r"^(?:the|my|our)\s+",
                "",
                str(not_here_match.group("item") or "").strip().lower(),
            )
            candidates: list[tuple[dict[str, object], dict[str, object]]] = []
            for entry in _active_find_snapshot():
                for match in entry.get("matches", []):
                    name = str(match.get("name") or "").lower()
                    query = str(entry.get("query") or "").lower()
                    if item_hint and item_hint not in name and item_hint not in query:
                        continue
                    candidates.append((entry, match))
            if len(candidates) != 1:
                return annotate_voice(
                    _direct_chat_response(
                        command=chat_text,
                        mode="clarify",
                        action="find_not_here_ambiguous",
                        message=(
                            "Which item is not there?"
                            if candidates
                            else "Start a find first, then tell me the item is not there."
                        ),
                    )
                )
            entry, match = candidates[0]
            correction = _report_find_not_here(
                item_id=int(match.get("item_id") or 0),
                location_id=str(match.get("location_id") or ""),
                query=str(entry.get("query") or chat_text),
                active_find_id=int(entry.get("id") or 0) or None,
                source=request.source.strip() or "ui",
            )
            response = _direct_chat_response(
                command=chat_text,
                mode="command",
                action="find_not_here",
                message=str(correction["message"]),
                details={
                    key: correction[key]
                    for key in (
                        "item_id",
                        "item",
                        "location_id",
                        "needs_review",
                        "active_find_removed",
                        "active_finds",
                        "light_ok",
                        "light_errors",
                    )
                },
            )
            return annotate_voice(response)
        if direct_light := _direct_location_light_response(chat_text):
            return annotate_voice(direct_light)
        if (
            request.source.strip().lower() != "voice"
            and _storage_roots_requested(chat_text)
        ):
            return annotate_voice(_storage_roots_chat_response(chat_text))
        if request.source.strip().lower() != "voice":
            if label_preview := _label_preview_chat_response(chat_text):
                return annotate_voice(label_preview)
        if place_configuration := voice_place_configuration_response():
            return annotate_voice(place_configuration)
        if request.source.strip().lower() == "voice":
            pending_put_away = _latest_voice_confirmable_plan()
            size_revision = re.fullmatch(
                r"(?:please\s+)?(?:use|try|choose)\s+"
                r"(?:(?:a|the)\s+)?(?P<size>bigger|larger|large|smaller|small)\s+"
                r"(?:bin|drawer|box|container)(?:\s+instead)?[.!]?",
                chat_text,
                re.I,
            )
            if pending_put_away is not None and size_revision is not None:
                requested_size = size_revision.group("size").lower()
                correction = (
                    "Use a smaller bin or container."
                    if requested_size in {"smaller", "small"}
                    else "Use a bigger bin or container."
                )
                return annotate_voice(
                    assistant_revise(
                        AssistantRevisionRequest(
                            text=correction,
                            source="voice",
                            voice_transcript=request.voice_transcript,
                            voice_command=request.voice_command or request.text,
                            voice_query=request.voice_query or request.text,
                            stt_model=request.stt_model,
                        )
                    )
                )
            voice_mode = _chat_mode(chat_text, request.mode, request.photo_ids)
            voice_put_away = _put_away_requested(chat_text)
            voice_retrieval = voice_mode == "find" or request_mode == "find"
            if not (voice_put_away or voice_retrieval):
                return annotate_voice(
                    _direct_chat_response(
                        command=chat_text,
                        mode="clarify",
                        action="voice_scope_ui_required",
                        message=VOICE_SCOPE_MESSAGE,
                        details={
                            "assistant_url": "/",
                            "storage_url": "/storage?view=places",
                        },
                    )
                )
        if request_mode == "setup_new":
            return annotate_voice(
                _storage_setup_chat_response(
                    chat_text,
                    request.current_location_id,
                    create_root=True,
                )
            )
        if request_mode == "setup":
            return annotate_voice(
                _storage_setup_chat_response(chat_text, request.current_location_id)
            )
        if storage_creation := _storage_creation_chat_response(chat_text):
            return annotate_voice(storage_creation)
        if storage_management := _storage_management_chat_response(
            chat_text,
            photo_ids=request.photo_ids,
        ):
            return annotate_voice(storage_management)
        if _inventory_status_requested(chat_text):
            inventory = store.list_inventory()
            item_count = len({item.item_id for item in inventory})
            location_count = len({item.bin_id for item in inventory})
            review_count = len(store.review_queue())
            review_text = (
                f" {review_count} entr{'y needs' if review_count == 1 else 'ies need'} review."
                if review_count
                else " Nothing currently needs review."
            )
            return annotate_voice(
                _direct_chat_response(
                    command=chat_text,
                    mode="status",
                    action="inventory_status",
                    message=(
                        f"Magpie has {item_count} item records across {location_count} storage locations."
                        f"{review_text}"
                    ),
                    details={
                        "item_count": item_count,
                        "location_count": location_count,
                        "review_count": review_count,
                    },
                )
            )
        if size := _constraint_only_chat(chat_text):
            return annotate_voice(
                _direct_chat_response(
                    command=chat_text,
                    mode="clarify",
                    action="clarify",
                    message=f"What item or container should use a {size} bin?",
                    details={"constraint": {"size_class": size}},
                )
            )

        mode = _chat_mode(chat_text, request.mode, request.photo_ids)
        if mode == "agent":
            response = _assistant_agent_response(
                AgentCommandRequest(text=chat_text, photo_ids=request.photo_ids),
                route="/assistant/chat",
                allowed_tools=(
                    VOICE_AGENT_TOOLS
                    if request.source.strip().lower() == "voice"
                    else None
                ),
            )
            response["mode"] = "agent"
            response["route"] = "/assistant/chat"
            return annotate_voice(response)
        if mode == "find":
            response = _assistant_find_response(
                AssistantFindRequest(
                    query=_strip_find_intent(chat_text),
                    photo_ids=request.photo_ids,
                    current_location_id=request.current_location_id,
                ),
                route="/assistant/chat",
            )
            return annotate_voice({
                "ok": True,
                "mode": "find",
                "route": "/assistant/chat",
                "message": response.get("message", ""),
                "results": response.get("results", []),
                "automation": {
                    "lit": response.get("lit", False),
                    "results": response.get("results", []),
                },
                "raw": response,
                "interaction_id": response.get("interaction_id"),
            })
        if mode == "command":
            response = _assistant_command_response(
                AssistantCommandRequest(text=chat_text, photo_ids=request.photo_ids)
            )
            response["mode"] = "command"
            response["route"] = "/assistant/chat"
            return annotate_voice(response)
        response = _assistant_ask_response(
            AssistantFindRequest(
                query=_strip_put_intent(chat_text),
                photo_ids=request.photo_ids,
                current_location_id=request.current_location_id,
                destination_locked=request.destination_locked,
            ),
            route="/assistant/chat",
            put_away_intent=_put_away_requested(chat_text),
        )
        return annotate_voice({
            "ok": True,
            "mode": "ask",
            "route": "/assistant/chat",
            "message": response.get("message", ""),
            "decision": response,
            "automation": {
                "lit": response.get("lit", False),
                "destination": response.get("destination"),
                "results": response.get("results", []),
            },
            "interaction_id": response.get("interaction_id"),
        })

    def _eval_limit(limit: int) -> int:
        return max(1, min(int(limit), 500))

    def _plan_tools(plan: list[dict[str, object]]) -> list[str]:
        tools: list[str] = []
        for step in plan:
            tool = step.get("tool")
            if isinstance(tool, str):
                tools.append(tool)
        return tools

    def _eval_terms(value: object) -> set[str]:
        terms: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            if token in {"a", "an", "and", "in", "of", "the", "to"}:
                continue
            terms.add(token[:-1] if len(token) > 3 and token.endswith("s") else token)
        return terms

    def _eval_item_matches(expected: object, actual: object) -> bool:
        expected_terms = _eval_terms(expected)
        actual_terms = _eval_terms(actual)
        if not expected_terms or not actual_terms:
            return str(expected or "").strip().lower() == str(actual or "").strip().lower()
        overlap = len(expected_terms & actual_terms)
        return overlap / min(len(expected_terms), len(actual_terms)) >= 0.6

    def _semantic_plan_match(
        expected: list[dict[str, object]],
        actual: list[dict[str, object]],
        *,
        command: str,
    ) -> tuple[bool, list[str]]:
        expected_tools = _plan_tools(expected)
        actual_tools = _plan_tools(actual)
        if expected_tools != actual_tools:
            return False, [f"tool sequence expected {expected_tools}, got {actual_tools}"]
        differences: list[str] = []
        exact_keys = {"type", "object_type", "size_class", "capacity_class"}
        location_keys = {
            "location_id",
            "target_location_id",
            "from_location_id",
            "to_location_id",
            "parent_id",
            "object_id",
        }
        for index, (expected_step, actual_step) in enumerate(zip(expected, actual, strict=True)):
            expected_args = expected_step.get("arguments")
            actual_args = actual_step.get("arguments")
            if not isinstance(expected_args, dict) or not isinstance(actual_args, dict):
                if expected_args != actual_args:
                    differences.append(f"step {index + 1} arguments differ")
                continue
            if "item" in expected_args and not _eval_item_matches(
                expected_args.get("item"),
                actual_args.get("item"),
            ):
                differences.append(
                    f"step {index + 1} item expected {expected_args.get('item')!r}, "
                    f"got {actual_args.get('item')!r}"
                )
            for key in exact_keys:
                if key in expected_args and expected_args.get(key) != actual_args.get(key):
                    differences.append(
                        f"step {index + 1} {key} expected {expected_args.get(key)!r}, "
                        f"got {actual_args.get(key)!r}"
                    )
            for key in location_keys:
                expected_value = str(expected_args.get(key) or "").strip()
                if not expected_value or expected_value.lower() not in command.lower():
                    continue
                if expected_value.lower() != str(actual_args.get(key) or "").strip().lower():
                    differences.append(
                        f"step {index + 1} explicit {key} expected {expected_value!r}, "
                        f"got {actual_args.get(key)!r}"
                    )
            expected_quantity = expected_args.get("quantity")
            if isinstance(expected_quantity, (int, float)) and float(expected_quantity) != 1.0:
                actual_quantity = actual_args.get("quantity")
                if not isinstance(actual_quantity, (int, float)) or float(actual_quantity) != float(
                    expected_quantity
                ):
                    differences.append(
                        f"step {index + 1} quantity expected {expected_quantity!r}, "
                        f"got {actual_quantity!r}"
                    )
        return not differences, differences

    def _eval_plan_already_satisfied(
        expected: list[dict[str, object]],
        trace: list[dict[str, object]],
        *,
        inventory_store: InventoryRepository,
    ) -> bool:
        if not expected:
            return False
        found_rows: list[dict[str, object]] = []
        for entry in trace:
            if entry.get("tool") != "search_items":
                continue
            result = entry.get("result")
            if isinstance(result, list):
                found_rows.extend(row for row in result if isinstance(row, dict))
        for step in expected:
            tool = step.get("tool")
            args = step.get("arguments")
            if not isinstance(args, dict):
                return False
            if tool == "create_location":
                location_id = str(args.get("id") or "").strip() or slugify(
                    str(args.get("label") or "")
                )
                if not location_id or locations.get(location_id) is None:
                    return False
                continue
            if tool != "put_item":
                return False
            expected_item = args.get("item")
            expected_location = str(args.get("location_id") or "").strip().lower()
            expected_quantity = args.get("quantity")
            matched = False
            direct_rows = inventory_store.exact_match(str(expected_item or ""))
            candidate_rows = [
                {
                    "name": row.name,
                    "location_id": row.bin_id,
                    "quantity": row.quantity,
                }
                for row in direct_rows
            ]
            for row in [*candidate_rows, *found_rows]:
                if not _eval_item_matches(expected_item, row.get("name")):
                    continue
                if expected_location and expected_location != str(
                    row.get("location_id") or ""
                ).strip().lower():
                    continue
                if isinstance(expected_quantity, (int, float)):
                    actual_quantity = row.get("quantity")
                    if not isinstance(actual_quantity, (int, float)) or float(
                        actual_quantity
                    ) < float(expected_quantity):
                        continue
                matched = True
                break
            if not matched:
                return False
        return True

    def _eval_missing_context_reason(example: dict[str, object]) -> str:
        context = example.get("context")
        if isinstance(context, dict) and context:
            return ""
        command = str(example.get("command") or "").strip()
        lowered = command.lower()
        if lowered.startswith("smoke "):
            return "historical synthetic smoke example is not a production replay fixture"
        if lowered in {"photo", "sort photo", "sort this photo"}:
            return "historical photo example has no replayable photo context"
        expected_tools = example.get("expected_tools")
        has_write_plan = isinstance(expected_tools, list) and bool(expected_tools)
        if has_write_plan and (
            re.match(r"^(?:some of\s+)?(?:these|those|they|this|that|it)\b", command, re.I)
            or re.search(r"\bmake out what you can\b", command, re.I)
        ):
            return "historical follow-up or photo example has no replayable context"
        if "\n" not in command or lowered.startswith("revise the pending storage plan"):
            return ""
        first_line = command.splitlines()[0].strip()
        starts_with_write_intent = bool(
            re.match(
                r"^(?:put|store|file|stash|stow|move|relocate|create|make|learn|rename|remove|delete)\b",
                first_line,
                re.I,
            )
        )
        if has_write_plan and not starts_with_write_intent:
            return "historical multi-turn example has no replayable interaction context"
        return ""

    def _eval_example(row: dict[str, object]) -> dict[str, object]:
        raw_action = str(row["action"])
        example: dict[str, object] = {
            "id": int(row["id"]),
            "command": str(row["command"]),
            "kind": str(row["kind"]),
            "review_status": str(row.get("review_status") or "pending"),
            "created_at": str(row["created_at"]),
            "raw_action": raw_action,
        }
        raw_context = str(row.get("context_json") or "{}")
        try:
            context = json.loads(raw_context)
        except json.JSONDecodeError as exc:
            example["context_parse_error"] = str(exc)
            context = {}
        if isinstance(context, dict):
            example["context"] = context
        try:
            action = json.loads(raw_action)
        except json.JSONDecodeError as exc:
            example["parse_error"] = str(exc)
            return example
        if isinstance(action, list):
            example["expected_plan"] = action
            example["expected_tools"] = _plan_tools(action)
        else:
            example["expected_action"] = action
        example["scenarios"] = _learning_example_scenarios(
            action=action,
            kind=str(example["kind"]),
            context=context if isinstance(context, dict) else {},
        )
        return example

    @app.get("/assistant/evals/export")
    def assistant_evals_export(
        limit: int = 100,
        kind: str | None = None,
    ) -> dict[str, object]:
        examples = [
            _eval_example(row)
            for row in store.examples_for_eval(limit=_eval_limit(limit), kind=kind)
        ]
        return {
            "ok": True,
            "count": len(examples),
            "examples": examples,
        }

    @app.post("/assistant/evals/replay")
    def assistant_evals_replay(
        body: AgentEvalReplayRequest,
        request: Request,
    ) -> dict[str, object]:
        if body.run_model and human_auth is not None:
            identity = _request_identity(request)
            if not can_manage_members(identity):
                raise HTTPException(
                    status_code=403,
                    detail="only workspace owners and admins can run live model evals",
                )
        requested_limit = _eval_limit(body.limit)
        effective_limit = min(requested_limit, 25) if body.run_model else requested_limit
        eligible_review_statuses = {"approved", "promoted"}
        candidate_rows = store.examples_for_eval(limit=500, kind=body.kind)
        eligible_rows = [
            row
            for row in candidate_rows
            if str(row.get("review_status") or "pending")
            in eligible_review_statuses
        ]
        examples = [
            _eval_example(row)
            for row in eligible_rows[:effective_limit]
        ]
        results: list[dict[str, object]] = []
        for example in examples:
            expected_plan = example.get("expected_plan")
            if not isinstance(expected_plan, list):
                results.append(
                    {
                        "id": example["id"],
                        "command": example["command"],
                        "status": "skipped",
                        "reason": "example is not a plan",
                        "scenarios": example.get("scenarios", []),
                    }
                )
                continue
            missing_context_reason = _eval_missing_context_reason(example)
            if missing_context_reason:
                results.append(
                    {
                        "id": example["id"],
                        "command": example["command"],
                        "status": "skipped",
                        "reason": missing_context_reason,
                        "scenarios": example.get("scenarios", []),
                    }
                )
                continue
            context = example.get("context")
            if not isinstance(context, dict):
                context = {}
            photo_ids = [
                int(photo_id)
                for photo_id in context.get("photo_ids", [])
                if isinstance(photo_id, int)
            ]
            images: list[str] = []
            missing_photo_ids: list[int] = []
            for photo_id in photo_ids:
                photo = store.photo(photo_id)
                try:
                    data_url = (
                        photo_data_url(photo, store.photo_content)
                        if photo is not None
                        else None
                    )
                except LlmError:
                    data_url = None
                if data_url is None:
                    missing_photo_ids.append(photo_id)
                else:
                    images.append(data_url)
            if missing_photo_ids:
                results.append(
                    {
                        "id": example["id"],
                        "command": example["command"],
                        "status": "skipped",
                        "reason": f"missing replay photos: {missing_photo_ids}",
                        "scenarios": example.get("scenarios", []),
                    }
                )
                continue
            if not body.run_model:
                results.append(
                    {
                        "id": example["id"],
                        "command": example["command"],
                        "status": "ready",
                        "expected_tools": example.get("expected_tools", []),
                        "scenarios": example.get("scenarios", []),
                    }
                )
                continue
            try:
                actual = run_agent(
                    str(example["command"]),
                    store=store,
                    locations=locations,
                    client=llm_client,
                    images=images or None,
                    examples=store.find_examples(str(example["command"]), limit=3),
                )
            except LlmError as exc:
                results.append(
                    {
                        "id": example["id"],
                        "command": example["command"],
                        "status": "error",
                        "error": str(exc),
                        "scenarios": example.get("scenarios", []),
                    }
                )
                continue
            actual_plan = normalize_plan(
                actual.plan,
                locations=locations,
                store=store,
                layout=layout,
                command=str(example["command"]),
            )
            satisfied_by_state = not actual_plan and _eval_plan_already_satisfied(
                expected_plan,
                actual.trace,
                inventory_store=store,
            )
            if satisfied_by_state:
                matched, differences = True, []
            else:
                matched, differences = _semantic_plan_match(
                    expected_plan,
                    actual_plan,
                    command=str(example["command"]),
                )
            results.append(
                {
                    "id": example["id"],
                    "command": example["command"],
                    "status": "passed" if matched else "failed",
                    "expected_tools": example.get("expected_tools", []),
                    "actual_tools": _plan_tools(actual_plan),
                    "expected_plan": expected_plan,
                    "actual_plan": actual_plan,
                    "differences": differences,
                    "satisfied_by_state": satisfied_by_state,
                    "scenarios": example.get("scenarios", []),
                    **(
                        {
                            "actual_message": actual.message,
                            "trace": actual.trace,
                        }
                        if not matched
                        else {}
                    ),
                }
            )
        passed = sum(1 for result in results if result["status"] == "passed")
        failed = sum(1 for result in results if result["status"] == "failed")
        ready = sum(1 for result in results if result["status"] == "ready")
        skipped = sum(1 for result in results if result["status"] == "skipped")
        errors = sum(1 for result in results if result["status"] == "error")
        evaluated = passed + failed + errors
        evidence_count = evaluated if body.run_model else ready
        evidence_statuses = (
            {"passed", "failed", "error"} if body.run_model else {"ready"}
        )
        scenario_coverage = Counter(
            str(scenario)
            for result in results
            if result["status"] in evidence_statuses
            for scenario in result.get("scenarios", [])
        )
        required_scenarios = list(dict.fromkeys(body.required_scenarios))
        missing_required_scenarios = [
            scenario
            for scenario in required_scenarios
            if scenario_coverage.get(scenario, 0) == 0
        ]
        gate_passed = (
            evidence_count >= body.minimum_cases
            and failed == 0
            and errors == 0
            and not missing_required_scenarios
        )
        return {
            "ok": True,
            "run_model": body.run_model,
            "requested_limit": requested_limit,
            "effective_limit": effective_limit,
            "limit_capped": effective_limit < requested_limit,
            "eligible_review_statuses": sorted(eligible_review_statuses),
            "eligible_count": len(eligible_rows),
            "excluded_unreviewed": len(candidate_rows) - len(eligible_rows),
            "minimum_cases": body.minimum_cases,
            "evidence_count": evidence_count,
            "evaluated": evaluated,
            "required_scenarios": required_scenarios,
            "missing_required_scenarios": missing_required_scenarios,
            "scenario_coverage": dict(sorted(scenario_coverage.items())),
            "count": len(results),
            "passed": passed,
            "failed": failed,
            "ready": ready,
            "skipped": skipped,
            "errors": errors,
            "gate_passed": gate_passed,
            "results": results,
        }

    def _attach_photos_with_retry(
        *,
        photo_ids: list[int],
        item_ids: list[int],
        location_id: str | None,
    ) -> dict[str, object]:
        item_links: list[dict[str, object]] = []
        location_links: list[dict[str, object]] = []
        pending: list[dict[str, object]] = []

        def attempt(operation) -> str | None:
            error = ""
            for retry in range(3):
                try:
                    operation()
                    return None
                except Exception as exc:
                    error = str(exc)
                    if retry < 2:
                        time.sleep(0.15 * (retry + 1))
            return error or "photo attachment failed"

        for photo_id in list(dict.fromkeys(photo_ids)):
            if item_ids:
                for item_id in list(dict.fromkeys(item_ids)):
                    error = attempt(
                        lambda photo_id=photo_id, item_id=item_id: store.attach_photo(
                            item_id=item_id,
                            photo_id=photo_id,
                        )
                    )
                    if error:
                        pending.append(
                            {"photo_id": photo_id, "item_id": item_id, "error": error}
                        )
                    else:
                        item_links.append({"photo_id": photo_id, "item_id": item_id})
            elif location_id:
                error = attempt(
                    lambda photo_id=photo_id: store.attach_photo_to_location(
                        location_id=location_id,
                        photo_id=photo_id,
                    )
                )
                if error:
                    pending.append(
                        {"photo_id": photo_id, "location_id": location_id, "error": error}
                    )
                else:
                    location_links.append(
                        {"photo_id": photo_id, "location_id": location_id}
                    )
                    locations.record_event(
                        event_type="location_photo_attached",
                        location_id=location_id,
                        note=f"photo {photo_id}",
                    )
        return {
            "photo_ids": list(dict.fromkeys(photo_ids)),
            "item_links": item_links,
            "location_links": location_links,
            "pending": pending,
            "retry_request": {
                "photo_ids": list(dict.fromkeys(photo_ids)),
                "item_ids": list(dict.fromkeys(item_ids)),
                "location_id": location_id,
            }
            if pending
            else None,
        }

    @app.post("/photos/attach-retry")
    def retry_photo_attachments(request: PhotoAttachmentRetryRequest) -> dict[str, object]:
        result = _attach_photos_with_retry(
            photo_ids=request.photo_ids,
            item_ids=request.item_ids,
            location_id=request.location_id,
        )
        return {"ok": not bool(result["pending"]), "photo_attachments": result}

    def _apply_confirmed_plan(
        *,
        plan_steps: list[PlanStep],
        command: str,
        photo_ids: list[int],
        source_interaction_id: int | None = None,
    ) -> dict[str, object]:
        plan = normalize_plan(
            [step.model_dump() for step in plan_steps],
            locations=locations,
            store=store,
            layout=layout,
            command=command,
        )
        for step in plan:
            if step.get("tool") in {"learn_alias", "learn_rule", "learn_size_rule"}:
                args = step.setdefault("arguments", {})
                if isinstance(args, dict) and not args.get("source_command"):
                    args["source_command"] = command
        explicit_review = _plan_requests_review(plan)
        photo_review = _photo_sort_needs_review(command=command, plan=plan, photo_ids=photo_ids)
        review_reason = _review_reason_for_plan(
            explicit_review=explicit_review,
            photo_review=photo_review,
        )
        atomic_photo_put = bool(
            repository_bundle.backend == "supabase"
            and photo_ids
            and len(plan) == 1
            and plan[0].get("tool") == "put_item"
            and callable(getattr(store, "put_item_with_photos_atomic", None))
        )
        try:
            if atomic_photo_put:
                args = plan[0].get("arguments", {}) or {}
                applied = [
                    store.put_item_with_photos_atomic(
                        item=str(args["item"]),
                        location_id=str(args["location_id"]),
                        quantity=float(args.get("quantity", 1)),
                        unit=str(args.get("unit", "each")),
                        quantity_mode=str(args.get("quantity_mode", "set")),
                        photo_ids=photo_ids,
                        needs_review=explicit_review or photo_review,
                        review_reason=review_reason,
                    )
                ]
            else:
                applied = execute_plan(plan, store=store, locations=locations)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for entry in applied:
            if not isinstance(entry, dict) or not entry.get("print_label"):
                continue
            try:
                printed = _print_location_label(str(entry["print_label"]))
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            entry.clear()
            entry.update(printed)
        item_targets = _photo_item_targets(applied=applied)
        if atomic_photo_put:
            photo_attachments = {
                "photo_ids": list(dict.fromkeys(photo_ids)),
                "item_links": [
                    {"photo_id": photo_id, "item_id": item_targets[0]}
                    for photo_id in list(dict.fromkeys(photo_ids))
                ],
                "location_links": [],
                "pending": [],
                "retry_request": None,
            }
        else:
            photo_targets = _photo_location_targets(plan=plan, applied=applied)
            photo_target = (
                _existing_location_id(photo_targets[0])
                if len(photo_targets) == 1
                else None
            )
            photo_attachments = _attach_photos_with_retry(
                photo_ids=photo_ids,
                item_ids=item_targets,
                location_id=photo_target,
            )
        review_item_ids: list[int] = []
        review_photo_links: list[dict[str, object]] = []
        if explicit_review or photo_review:
            for item_id, location_id in _photo_review_targets(
                applied=applied,
                explicit_only=explicit_review,
            ):
                if not atomic_photo_put:
                    store.mark_needs_review(
                        item_id=item_id,
                        bin_id=location_id,
                        reason=review_reason,
                    )
                review_item_ids.append(item_id)
                for photo_id in photo_ids:
                    if not atomic_photo_put:
                        store.link_review_photo(
                            item_id=item_id,
                            bin_id=location_id,
                            photo_id=photo_id,
                        )
                    review_photo_links.append(
                        {
                            "photo_id": photo_id,
                            "item_id": item_id,
                            "location_id": location_id,
                        }
                    )
        home_learning: list[dict[str, object]] = []
        for entry in list(applied):
            if not isinstance(entry, dict) or entry.get("needs_review"):
                continue
            if not (entry.get("stored") or entry.get("moved_item")):
                continue
            try:
                item_id = int(entry.get("item_id") or entry.get("moved_item"))
                location_id = str(
                    entry.get("location_id") or entry.get("to_location_id") or ""
                )
                if location_id:
                    home_learning.append(
                        store.observe_confirmed_home(
                            item_id=item_id,
                            location_id=location_id,
                        )
                    )
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                home_learning.append(
                    {
                        "item_id": entry.get("item_id") or entry.get("moved_item"),
                        "location_id": entry.get("location_id") or entry.get("to_location_id"),
                        "error": str(exc) or "home learning failed",
                    }
                )
        source_interaction = (
            store.assistant_interaction(source_interaction_id)
            if source_interaction_id is not None
            else None
        )
        active_find_removed, active_find_errors = _remove_pending_put_away_find(
            source_interaction,
            fallback_command=command,
        )
        lit_target = _last_applied_location(applied)
        lit_location = locations.nearest_lit(lit_target) if lit_target else None
        lit = _save_light_effect(
            lit_location.light_indexes,
            seconds=SAVE_ACK_SECONDS,
            target=str(lit_location.light_target or ""),
        ) if lit_location else False
        if lit and _raw_active_find_snapshot():
            _restore_active_lights_after(SAVE_ACK_SECONDS)
        # Every confirmed command becomes a reusable example (few-shot + eval set).
        example_context = {
            "photo_ids": photo_ids,
            "source_interaction_id": source_interaction_id,
        }
        example_kind = "confirmed"
        if source_interaction_id is not None:
            source_interaction = store.assistant_interaction(source_interaction_id)
            source_response = (
                source_interaction.get("response")
                if isinstance(source_interaction, dict)
                else None
            )
            if (
                isinstance(source_interaction, dict)
                and source_interaction.get("route") == "/assistant/revise"
                and isinstance(source_response, dict)
            ):
                example_kind = "correction"
                example_context.update(
                    {
                        "revision_of": source_response.get("revision_of"),
                        "original_command": source_response.get("original_command"),
                        "correction": source_response.get("correction"),
                    }
                )
                revision_of = source_response.get("revision_of")
                if isinstance(revision_of, int):
                    original_interaction = store.assistant_interaction(revision_of)
                    original_response = (
                        original_interaction.get("response")
                        if isinstance(original_interaction, dict)
                        else None
                    )
                    original_plan = (
                        original_response.get("plan")
                        if isinstance(original_response, dict)
                        else None
                    )
                    if isinstance(original_plan, list):
                        example_context["original_plan"] = original_plan
        if _promotable_learning_steps(plan):
            _record_applied_learning_example(
                command=command,
                action=plan,
                kind=example_kind,
                context=example_context,
            )
        else:
            store.record_example(
                command=command,
                action=json.dumps(plan),
                kind=example_kind,
                context=example_context,
            )
        _record_edited_photo_sort_example(
            command=command,
            photo_ids=photo_ids,
            confirmed_plan=plan,
            source_interaction_id=source_interaction_id,
        )
        append_placement_memory(
            command=command,
            applied=applied,
            path=Path(cfg.database_path).parent / "placement-memory.md",
        )
        response = {
            "ok": True,
            "applied": applied,
            "lit": lit,
            "lit_location": lit_location.id if lit_location else None,
            "light_effect": "save-pulse" if lit else None,
            "light_seconds": SAVE_ACK_SECONDS if lit else None,
            "active_find_removed": active_find_removed,
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
            "active_finds": _raw_active_find_snapshot(),
        }
        response["receipt"] = _confirmation_receipt(
            plan=plan,
            applied=applied,
            photo_ids=photo_ids,
            photo_attachments=photo_attachments,
            needs_review=bool(review_item_ids),
        )
        if home_learning:
            response["home_learning"] = home_learning
            promoted_homes = [
                observation
                for observation in home_learning
                if observation.get("promoted") is True
            ]
            if promoted_homes:
                learned = promoted_homes[-1]
                learned_location_id = str(learned.get("location_id") or "")
                learned_location_path = " › ".join(
                    node.label or node.id
                    for node in location_path(locations, learned_location_id)
                ) or learned_location_id
                response["learning_message"] = (
                    f"I learned the usual home for "
                    f"{learned.get('item') or 'this item'}: {learned_location_path}."
                )
        if photo_ids:
            attachment_response = {
                key: value
                for key, value in photo_attachments.items()
                if key not in {"pending", "retry_request"}
            }
            if photo_attachments["pending"]:
                attachment_response["pending"] = photo_attachments["pending"]
                attachment_response["retry_request"] = photo_attachments["retry_request"]
            response["photo_attachments"] = {
                **attachment_response,
                "review_links": review_photo_links,
            }
        if review_item_ids:
            response["needs_review"] = True
            response["review_item_ids"] = review_item_ids
        if source_interaction_id is not None:
            response["source_interaction_id"] = source_interaction_id
        return response

    def _confirm_agent_plan(
        *,
        plan_steps: list[PlanStep],
        command: str,
        photo_ids: list[int],
        source_interaction_id: int | None,
    ) -> tuple[dict[str, object], bool]:
        if source_interaction_id is None:
            return (
                _apply_confirmed_plan(
                    plan_steps=plan_steps,
                    command=command,
                    photo_ids=photo_ids,
                    source_interaction_id=None,
                ),
                False,
            )

        source = store.assistant_interaction(source_interaction_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source interaction not found")
        if source["route"] not in {
            "/assistant/agent",
            "/assistant/chat",
            "/assistant/revise",
            "/mcp/propose_plan",
        }:
            raise HTTPException(
                status_code=409,
                detail="source interaction is not a confirmable Magpie proposal",
            )

        def cached_response(interaction: dict[str, object]) -> dict[str, object] | None:
            source_response = interaction.get("response")
            cached = (
                source_response.get("confirmation_response")
                if isinstance(source_response, dict)
                else None
            )
            if not isinstance(cached, dict):
                return None
            return {**cached, "idempotent_replay": True}

        if cached := cached_response(source):
            return cached, True
        source = _confirmation_metadata(source)
        if source["status"] != "pending":
            status = str(source["status"] or "unknown")
            raise HTTPException(
                status_code=409,
                detail=f"proposal cannot be confirmed because it is {status}",
            )
        if not store.claim_assistant_interaction_confirmation(source_interaction_id):
            current = store.assistant_interaction(source_interaction_id)
            if current is not None and (cached := cached_response(current)):
                return cached, True
            status = str(current["status"] if current is not None else "missing")
            raise HTTPException(
                status_code=409,
                detail=f"proposal confirmation is already {status}",
            )

        try:
            response = _apply_confirmed_plan(
                plan_steps=plan_steps,
                command=command,
                photo_ids=photo_ids,
                source_interaction_id=source_interaction_id,
            )
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            store.update_assistant_interaction_response(
                source_interaction_id,
                {"confirmation_error": str(detail or "confirmation failed")},
            )
            store.set_assistant_interaction_status(
                source_interaction_id,
                "confirmation_failed",
            )
            raise
        if not store.update_assistant_interaction_response(
            source_interaction_id,
            {
                "confirmed_plan": [step.model_dump() for step in plan_steps],
                "confirmation_response": response,
            },
        ):
            store.set_assistant_interaction_status(
                source_interaction_id,
                "confirmation_failed",
            )
            raise HTTPException(
                status_code=503,
                detail="plan was applied but its confirmation receipt could not be saved",
            )
        store.set_assistant_interaction_status(source_interaction_id, "confirmed")
        source_response = source.get("response")
        if (
            source.get("route") == "/assistant/revise"
            and isinstance(source_response, dict)
            and bool(source_response.get("correction"))
        ):
            _record_agent_correction_application(source_response)
        return response, False

    @app.post("/assistant/confirm")
    def assistant_confirm(request: AgentConfirmRequest) -> dict[str, object]:
        source_photo_ids: list[int] = []
        if request.source_interaction_id is not None:
            source = store.assistant_interaction(request.source_interaction_id)
            if source is not None:
                source_photo_ids = [
                    int(photo_id)
                    for photo_id in source.get("photo_ids", [])
                    if isinstance(photo_id, int)
                ]
        photo_ids = list(dict.fromkeys([*source_photo_ids, *request.photo_ids]))
        response, replayed = _confirm_agent_plan(
            plan_steps=request.plan,
            command=request.command,
            photo_ids=photo_ids,
            source_interaction_id=request.source_interaction_id,
        )
        if not replayed:
            confirmation_interaction_id = store.record_assistant_interaction(
                route="/assistant/confirm",
                command=request.command,
                photo_ids=photo_ids,
                response=response,
                status="confirmed",
            )
            response["confirmation_interaction_id"] = confirmation_interaction_id
            store.update_assistant_interaction_response(
                confirmation_interaction_id,
                {"confirmation_interaction_id": confirmation_interaction_id},
            )
            if request.source_interaction_id is not None:
                store.update_assistant_interaction_response(
                    request.source_interaction_id,
                    {"confirmation_response": response},
                )
        return response

    @app.post("/assistant/cancel")
    def assistant_cancel(request: AgentCancelRequest) -> dict[str, object]:
        source = (
            store.assistant_interaction(request.source_interaction_id)
            if request.source_interaction_id is not None
            else None
        )
        active_find_removed, active_find_errors = _remove_pending_put_away_find(
            source,
            fallback_command=request.command,
        )
        if source is not None:
            store.set_assistant_interaction_status(int(source["id"]), "canceled")
            _record_rejected_photo_sort_example(source=source)
        response = {
            "ok": True,
            "message": "Canceled the Magpie plan.",
            "source_interaction_id": int(source["id"]) if source is not None else None,
            "active_find_removed": active_find_removed,
            "active_finds": _active_find_snapshot(),
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
        }
        cancellation_interaction_id = store.record_assistant_interaction(
            route="/assistant/cancel",
            command=request.command or "cancel",
            photo_ids=request.photo_ids,
            response=response,
            status="canceled",
        )
        response["cancellation_interaction_id"] = cancellation_interaction_id
        store.update_assistant_interaction_response(
            cancellation_interaction_id,
            {"cancellation_interaction_id": cancellation_interaction_id},
        )
        return response

    @app.post("/assistant/voice/confirm")
    def assistant_voice_confirm() -> dict[str, object]:
        pending = _latest_confirmable_agent_plan()
        if pending is None:
            response = {
                "ok": False,
                "message": "There is no pending Magpie plan to confirm.",
            }
            store.record_assistant_interaction(
                route="/assistant/voice/confirm",
                command="confirm",
                response=response,
            )
            return response
        if not _voice_confirmable_plan(pending):
            response = {"ok": False, "message": VOICE_SCOPE_MESSAGE}
            store.record_assistant_interaction(
                route="/assistant/voice/confirm",
                command="confirm",
                response=response,
            )
            return response
        plan = pending["response"].get("plan")
        if not isinstance(plan, list) or not plan:
            raise HTTPException(status_code=409, detail="pending interaction has no plan")
        response, replayed = _confirm_agent_plan(
            plan_steps=[PlanStep(**step) for step in plan],
            command=str(pending["command"]),
            photo_ids=[
                int(photo_id)
                for photo_id in pending.get("photo_ids", [])
                if isinstance(photo_id, int)
            ],
            source_interaction_id=int(pending["id"]),
        )
        stored = [
            entry
            for entry in response.get("applied", [])
            if isinstance(entry, dict) and entry.get("stored") and entry.get("location_id")
        ]
        if len(stored) == 1:
            stored_location_id = str(stored[0]["location_id"])
            stored_location_path = " › ".join(
                node.label or node.id
                for node in location_path(locations, stored_location_id)
            ) or stored_location_id
            response["message"] = (
                f"Confirmed. Stored {stored[0]['stored']} in {stored_location_path}."
            )
        elif stored:
            response["message"] = f"Confirmed. Applied {len(stored)} storage changes."
        else:
            response["message"] = "Confirmed. Applied the latest Magpie plan."
        if not replayed:
            confirmation_interaction_id = store.record_assistant_interaction(
                route="/assistant/voice/confirm",
                command="confirm",
                photo_ids=[
                    int(photo_id)
                    for photo_id in pending.get("photo_ids", [])
                    if isinstance(photo_id, int)
                ],
                response=response,
                status="confirmed",
            )
            response["confirmation_interaction_id"] = confirmation_interaction_id
            store.update_assistant_interaction_response(
                confirmation_interaction_id,
                {"confirmation_interaction_id": confirmation_interaction_id},
            )
            store.update_assistant_interaction_response(
                int(pending["id"]),
                {"confirmation_response": response},
            )
        return response

    @app.post("/assistant/voice/cancel")
    def assistant_voice_cancel() -> dict[str, object]:
        pending = _latest_confirmable_agent_plan()
        if pending is None:
            response = {
                "ok": False,
                "message": "There is no pending Magpie plan to cancel.",
            }
        elif not _voice_confirmable_plan(pending):
            response = {"ok": False, "message": VOICE_SCOPE_MESSAGE}
        else:
            active_find_removed, active_find_errors = _remove_pending_put_away_find(
                pending,
            )
            store.set_assistant_interaction_status(int(pending["id"]), "canceled")
            _record_rejected_photo_sort_example(source=pending)
            response = {
                "ok": True,
                "message": "Canceled the latest Magpie plan.",
                "source_interaction_id": int(pending["id"]),
                "active_find_removed": active_find_removed,
                "active_finds": _active_find_snapshot(),
                "active_find_light_ok": not active_find_errors,
                "active_find_light_errors": active_find_errors,
            }
        store.record_assistant_interaction(
            route="/assistant/voice/cancel",
            command="cancel",
            response=response,
        )
        return response

    def _undo_message(undone: list[dict[str, object]]) -> str:
        if len(undone) != 1:
            return "Undid the latest confirmed Magpie plan."
        entry = undone[0]
        if entry.get("moved_container"):
            location_id = str(entry["moved_container"])
            location = locations.get_any(location_id)
            label = (
                (location.label or location.id)
                if location is not None
                else location_id
            )
            parent_id = str(entry.get("to_parent_id") or "")
            if not parent_id:
                return f"Moved {label} back to the top level."
            parent_path = " › ".join(
                node.label or node.id for node in location_path(locations, parent_id)
            ) or parent_id
            return f"Moved {label} back inside {parent_path}."
        if entry.get("moved_item") and entry.get("to_location_id"):
            item_id = int(entry["moved_item"])
            rows = store.item_inventory(item_id)
            item_name = rows[0].name if rows else f"Item {item_id}"
            destination_id = str(entry["to_location_id"])
            destination_path = " › ".join(
                node.label or node.id
                for node in location_path(locations, destination_id)
            ) or destination_id
            return f"Moved {item_name} back to {destination_path}."
        if entry.get("restored_archived_location"):
            location_id = str(entry["restored_archived_location"])
            location = locations.get_any(location_id)
            label = (
                (location.label or location.id)
                if location is not None
                else location_id
            )
            return f"Restored {label} from the archive."
        if entry.get("rearchived_location"):
            location_id = str(entry["rearchived_location"])
            location = locations.get_any(location_id)
            label = (
                (location.label or location.id)
                if location is not None
                else location_id
            )
            return f"Returned {label} to the archive."
        return "Undid the latest confirmed Magpie plan."

    @app.post("/assistant/undo")
    def assistant_undo(request: UndoRequest | None = None) -> dict[str, object]:
        confirmed = store.latest_undoable_confirmed_plan()
        if confirmed is None:
            response = {
                "ok": False,
                "message": "There is no confirmed Magpie plan to undo.",
            }
            undo_interaction_id = store.record_assistant_interaction(
                route="/assistant/undo",
                command="undo",
                response=response,
            )
            response["undo_interaction_id"] = undo_interaction_id
            store.update_assistant_interaction_response(
                undo_interaction_id,
                {"undo_interaction_id": undo_interaction_id},
            )
            return response
        expected_interaction_id = (
            request.expected_interaction_id if request is not None else None
        )
        if (
            expected_interaction_id is not None
            and int(confirmed["id"]) != expected_interaction_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "A newer storage change happened after this receipt. "
                        "Refresh before choosing what to undo."
                    ),
                    "expected_interaction_id": expected_interaction_id,
                    "latest_interaction_id": int(confirmed["id"]),
                },
            )
        applied = confirmed["response"].get("applied")
        if not isinstance(applied, list) or not applied:
            raise HTTPException(status_code=409, detail="confirmed interaction has no applied plan")
        home_learning = confirmed["response"].get("home_learning")
        learning_entries = (
            [
                entry
                for entry in home_learning
                if isinstance(entry, dict) and entry.get("home_observation")
            ]
            if isinstance(home_learning, list)
            else []
        )
        undone: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        learning_undone: list[dict[str, object]] = []
        learning_skipped: list[dict[str, object]] = []
        for entry in reversed([*applied, *learning_entries]):
            if not isinstance(entry, dict):
                continue
            if entry.get("home_observation"):
                observation_id = int(entry["home_observation"])
                item_id = int(entry["item_id"])
                try:
                    invalidated = store.undo_confirmed_home_observation(observation_id)
                    if entry.get("promoted"):
                        store.update_item_home(
                            item_id=item_id,
                            location_id=str(entry.get("previous_home_location_id") or ""),
                        )
                    if invalidated:
                        learning_undone.append(
                            {
                                "home_observation": observation_id,
                                "item_id": item_id,
                                "restored_home_location_id": (
                                    str(entry.get("previous_home_location_id") or "")
                                    if entry.get("promoted")
                                    else None
                                ),
                            }
                        )
                    else:
                        learning_skipped.append(
                            {
                                "home_observation": observation_id,
                                "reason": "observation was not active",
                            }
                        )
                except ValueError as exc:
                    learning_skipped.append(
                        {"home_observation": observation_id, "reason": str(exc)}
                    )
                continue
            if entry.get("archived_location_id"):
                location_id = str(entry["archived_location_id"])
                try:
                    restored_ids = locations.restore_subtree(location_id)
                    locations.record_event(
                        event_type="location_restored",
                        location_id=location_id,
                        quantity=float(len(restored_ids)),
                        note="undo agent archive",
                    )
                    undone.append(
                        {"restored_archived_location": location_id, "restored_ids": restored_ids}
                    )
                except (KeyError, ValueError) as exc:
                    skipped.append({"location_id": location_id, "reason": str(exc)})
                continue
            if entry.get("restored_location_id"):
                location_id = str(entry["restored_location_id"])
                try:
                    archived_ids = locations.archive_subtree(location_id)
                    locations.record_event(
                        event_type="location_archived",
                        location_id=location_id,
                        quantity=float(len(archived_ids)),
                        note="undo agent restore",
                    )
                    undone.append(
                        {"rearchived_location": location_id, "archived_ids": archived_ids}
                    )
                except (KeyError, ValueError) as exc:
                    skipped.append({"location_id": location_id, "reason": str(exc)})
                continue
            if entry.get("approved_review") and entry.get("location_id"):
                item_id = int(entry["approved_review"])
                location_id = str(entry["location_id"])
                try:
                    store.mark_needs_review(
                        item_id=item_id,
                        bin_id=location_id,
                        reason=str(entry.get("previous_review_reason") or ""),
                    )
                    review_example_id = entry.get("review_example_id")
                    if review_example_id is not None:
                        store.reject_example(int(review_example_id))
                    undone.append(
                        {
                            "restored_review": item_id,
                            "item": str(entry.get("item") or ""),
                            "location_id": location_id,
                            "invalidated_review_example_id": review_example_id,
                        }
                    )
                except ValueError as exc:
                    skipped.append(
                        {
                            "item_id": item_id,
                            "location_id": location_id,
                            "reason": str(exc),
                        }
                    )
                continue
            if entry.get("moved_container") and "from_parent_id" in entry:
                location_id = str(entry["moved_container"])
                old_parent = entry.get("from_parent_id")
                try:
                    moved = locations.move(
                        location_id, str(old_parent) if old_parent is not None else None
                    )
                    undone.append(
                        {
                            "moved_container": moved.id,
                            "to_parent_id": moved.parent_id,
                        }
                    )
                except (KeyError, ValueError) as exc:
                    skipped.append({"location_id": location_id, "reason": str(exc)})
                continue
            if entry.get("attached_photo"):
                photo_id = int(entry["attached_photo"])
                try:
                    store.set_photo_target(
                        photo_id=photo_id,
                        item_id=(
                            int(entry["previous_item_id"])
                            if entry.get("previous_item_id") is not None
                            else None
                        ),
                        location_id=(
                            str(entry["previous_location_id"])
                            if entry.get("previous_location_id") is not None
                            else None
                        ),
                    )
                    undone.append({"restored_photo_target": photo_id})
                except ValueError as exc:
                    skipped.append({"photo_id": photo_id, "reason": str(exc)})
                continue
            if entry.get("updated_location") and entry.get("previous_label"):
                location_id = str(entry["updated_location"])
                loc = locations.get(location_id)
                if loc is None:
                    skipped.append({"location_id": location_id, "reason": "location was not found"})
                    continue
                locations.upsert(
                    replace(
                        loc,
                        label=str(entry["previous_label"]),
                        type=str(entry.get("previous_type") or loc.type),
                        capacity_class=str(
                            entry.get("previous_capacity_class") or loc.capacity_class
                        ),
                        purpose_tags=[
                            str(tag)
                            for tag in entry.get("previous_purpose_tags", [])
                            if str(tag).strip()
                        ],
                        placement_notes=str(entry.get("previous_placement_notes") or ""),
                        accepts_items=(
                            bool(entry["previous_accepts_items"])
                            if "previous_accepts_items" in entry
                            else loc.accepts_items
                        ),
                        grid=(
                            entry.get("previous_grid")
                            if "previous_grid" in entry
                            else loc.grid
                        ),
                        col=(
                            entry.get("previous_col")
                            if "previous_col" in entry
                            else loc.col
                        ),
                        row=(
                            entry.get("previous_row")
                            if "previous_row" in entry
                            else loc.row
                        ),
                        w=(
                            int(entry["previous_w"])
                            if entry.get("previous_w") is not None
                            else loc.w
                        ),
                        h=(
                            int(entry["previous_h"])
                            if entry.get("previous_h") is not None
                            else loc.h
                        ),
                        light_target=(
                            entry.get("previous_light_target")
                            if "previous_light_target" in entry
                            else loc.light_target
                        ),
                        light_indexes=(
                            [int(pixel) for pixel in entry.get("previous_light_indexes", [])]
                            if "previous_light_indexes" in entry
                            else loc.light_indexes
                        ),
                        internal_dimensions=(
                            entry.get("previous_internal_dimensions")
                            if "previous_internal_dimensions" in entry
                            else loc.internal_dimensions
                        ),
                    )
                )
                locations.record_event(
                    event_type="location_updated",
                    location_id=location_id,
                    note="undo metadata update",
                )
                undone.append({"restored_location": location_id})
                continue
            if entry.get("moved_item") and entry.get("from_location_id") and entry.get("to_location_id"):
                item_id = int(entry["moved_item"])
                src = str(entry["to_location_id"])
                dst = str(entry["from_location_id"])
                if entry.get("previous_from_quantity") is not None:
                    store.remove_inventory(item_id=item_id, bin_id=src, normalize_bin=False)
                    if entry.get("previous_to_quantity") is not None:
                        store.put_inventory(
                            item_id=item_id,
                            bin_id=src,
                            quantity=float(entry["previous_to_quantity"]),
                            unit=str(entry.get("previous_to_unit") or "each"),
                            needs_review=bool(
                                entry.get("previous_to_needs_review")
                            ),
                            review_reason=str(
                                entry.get("previous_to_review_reason") or ""
                            ),
                            normalize_bin=False,
                        )
                    store.put_inventory(
                        item_id=item_id,
                        bin_id=dst,
                        quantity=float(entry["previous_from_quantity"]),
                        unit=str(entry.get("previous_from_unit") or "each"),
                        needs_review=bool(
                            entry.get("previous_from_needs_review")
                        ),
                        review_reason=str(
                            entry.get("previous_from_review_reason") or ""
                        ),
                        normalize_bin=False,
                    )
                    undone.append({"moved_item": item_id, "from_location_id": src, "to_location_id": dst})
                    continue
                if store.move_inventory(item_id=item_id, from_bin=src, to_bin=dst, normalize_bin=False):
                    undone.append({"moved_item": item_id, "from_location_id": src, "to_location_id": dst})
                else:
                    skipped.append({"moved_item": item_id, "reason": "current location did not match"})
                continue
            if entry.get("learned_alias") and entry.get("item_id"):
                item_id = int(entry["item_id"])
                alias = str(entry["learned_alias"])
                if store.remove_alias(item_id=item_id, alias=alias):
                    undone.append(
                        {
                            "removed_alias": alias,
                            "item_id": item_id,
                            "item": str(entry.get("item") or ""),
                        }
                    )
                else:
                    skipped.append({"item_id": item_id, "alias": alias, "reason": "alias was not found"})
                continue
            if entry.get("disabled_rule") and entry.get("rule_type"):
                rule_id = int(entry["disabled_rule"])
                rule_type = str(entry["rule_type"])
                try:
                    if store.set_rule_active(
                        rule_id=rule_id, rule_type=rule_type, active=True
                    ):
                        undone.append(
                            {"restored_rule": rule_id, "rule_type": rule_type}
                        )
                    else:
                        skipped.append(
                            {
                                "rule_id": rule_id,
                                "rule_type": rule_type,
                                "reason": "rule was not found",
                            }
                        )
                except ValueError as exc:
                    skipped.append({"rule_id": rule_id, "reason": str(exc)})
                continue
            if entry.get("reactivated_rule") and entry.get("rule_type"):
                rule_id = int(entry["reactivated_rule"])
                rule_type = str(entry["rule_type"])
                try:
                    if store.set_rule_active(
                        rule_id=rule_id, rule_type=rule_type, active=False
                    ):
                        undone.append(
                            {"disabled_rule": rule_id, "rule_type": rule_type}
                        )
                    else:
                        skipped.append(
                            {
                                "rule_id": rule_id,
                                "rule_type": rule_type,
                                "reason": "rule was not found",
                            }
                        )
                except ValueError as exc:
                    skipped.append({"rule_id": rule_id, "reason": str(exc)})
                continue
            if entry.get("updated_rule") and entry.get("previous_rule_text"):
                rule_id = int(entry["updated_rule"])
                try:
                    if store.update_rule(
                        rule_id=rule_id,
                        rule_text=str(entry["previous_rule_text"]),
                        trigger_terms=[
                            str(term)
                            for term in entry.get("previous_trigger_terms", [])
                            if str(term).strip()
                        ],
                        target_location_id=str(entry.get("previous_target_location_id") or ""),
                        source_command=str(entry.get("previous_source_command") or "undo"),
                        confidence=float(entry.get("previous_confidence", 1.0)),
                    ):
                        undone.append({"restored_rule": rule_id, "rule_type": "placement"})
                    else:
                        skipped.append({"rule_id": rule_id, "rule_type": "placement", "reason": "active rule was not found"})
                except ValueError as exc:
                    skipped.append({"rule_id": rule_id, "reason": str(exc)})
                continue
            if entry.get("updated_size_rule") and entry.get("previous_rule_text"):
                rule_id = int(entry["updated_size_rule"])
                try:
                    if store.update_size_rule(
                        rule_id=rule_id,
                        rule_text=str(entry["previous_rule_text"]),
                        trigger_terms=[
                            str(term)
                            for term in entry.get("previous_trigger_terms", [])
                            if str(term).strip()
                        ],
                        size_class=str(entry.get("previous_size_class") or ""),
                        source_command=str(entry.get("previous_source_command") or "undo"),
                        confidence=float(entry.get("previous_confidence", 1.0)),
                    ):
                        undone.append({"restored_rule": rule_id, "rule_type": "size"})
                    else:
                        skipped.append({"rule_id": rule_id, "rule_type": "size", "reason": "active rule was not found"})
                except ValueError as exc:
                    skipped.append({"rule_id": rule_id, "reason": str(exc)})
                continue
            if entry.get("learned_rule"):
                rule_id = int(entry["learned_rule"])
                try:
                    if store.deactivate_rule(rule_id=rule_id, rule_type="placement"):
                        undone.append({"forgotten_rule": rule_id, "rule_type": "placement"})
                    else:
                        skipped.append({"rule_id": rule_id, "rule_type": "placement", "reason": "active rule was not found"})
                except ValueError as exc:
                    skipped.append({"rule_id": rule_id, "reason": str(exc)})
                continue
            if entry.get("learned_size_rule"):
                rule_id = int(entry["learned_size_rule"])
                try:
                    if store.deactivate_rule(rule_id=rule_id, rule_type="size"):
                        undone.append({"forgotten_rule": rule_id, "rule_type": "size"})
                    else:
                        skipped.append({"rule_id": rule_id, "rule_type": "size", "reason": "active rule was not found"})
                except ValueError as exc:
                    skipped.append({"rule_id": rule_id, "reason": str(exc)})
                continue
            if entry.get("archived_inventory_item"):
                item_id = int(entry["archived_inventory_item"])
                try:
                    if store.restore_item(item_id):
                        undone.append(
                            {
                                "restored_archived_inventory_item": item_id,
                                "item": str(entry.get("item") or ""),
                            }
                        )
                    else:
                        skipped.append(
                            {
                                "item_id": item_id,
                                "reason": "item was not archived",
                            }
                        )
                except (RuntimeError, ValueError) as exc:
                    skipped.append({"item_id": item_id, "reason": str(exc)})
                continue
            if entry.get("restored_inventory_item"):
                item_id = int(entry["restored_inventory_item"])
                try:
                    if store.archive_item(item_id):
                        undone.append(
                            {
                                "rearchived_inventory_item": item_id,
                                "item": str(entry.get("item") or ""),
                            }
                        )
                    else:
                        skipped.append(
                            {
                                "item_id": item_id,
                                "reason": "item was not active",
                            }
                        )
                except (RuntimeError, ValueError) as exc:
                    skipped.append({"item_id": item_id, "reason": str(exc)})
                continue
            if entry.get("removed_item") and entry.get("item") and entry.get("location_id"):
                item_id = int(entry["removed_item"])
                location_id = str(entry["location_id"])
                quantity = float(entry.get("quantity", 1))
                unit = str(entry.get("unit") or "each")
                try:
                    store.put_inventory(
                        item_id=item_id,
                        bin_id=location_id,
                        quantity=quantity,
                        unit=unit,
                        normalize_bin=False,
                        needs_review=bool(entry.get("previous_needs_review")),
                        review_reason=str(entry.get("previous_review_reason") or ""),
                    )
                    undone.append(
                        {
                            "restored_item": item_id,
                            "item": str(entry["item"]),
                            "location_id": location_id,
                            "quantity": quantity,
                            "unit": unit,
                        }
                    )
                except ValueError as exc:
                    skipped.append({"item_id": item_id, "location_id": location_id, "reason": str(exc)})
                continue
            if entry.get("updated_item") and entry.get("previous_name"):
                item_id = int(entry["updated_item"])
                try:
                    store.update_item(
                        item_id=item_id,
                        name=str(entry["previous_name"]),
                        description=str(entry.get("previous_description") or ""),
                        category=str(entry.get("previous_category") or ""),
                        aliases=[
                            str(alias)
                            for alias in entry.get("previous_aliases", [])
                            if str(alias).strip()
                        ],
                    )
                    undone.append(
                        {
                            "restored_item_metadata": item_id,
                            "item": str(entry["previous_name"]),
                        }
                    )
                except ValueError as exc:
                    skipped.append({"item_id": item_id, "reason": str(exc)})
                continue
            if entry.get("updated_item_size"):
                item_id = int(entry["updated_item_size"])
                previous_size = str(entry.get("previous_size_class") or "")
                try:
                    dimensions = entry.get("previous_dimensions")
                    store.restore_item_size_metadata(
                        item_id=item_id,
                        size_class=previous_size,
                        source=str(entry.get("previous_size_source") or ""),
                        confidence=float(entry.get("previous_size_confidence") or 0),
                        dimensions=dimensions if isinstance(dimensions, dict) else None,
                    )
                    undone.append(
                        {
                            "restored_item_size": item_id,
                            "item": str(entry.get("item") or ""),
                            "size_class": previous_size,
                        }
                    )
                except ValueError as exc:
                    skipped.append({"item_id": item_id, "reason": str(exc)})
                continue
            if entry.get("updated_item_home"):
                item_id = int(entry["updated_item_home"])
                previous_home = str(entry.get("previous_home_location_id") or "")
                try:
                    store.update_item_home(item_id=item_id, location_id=previous_home)
                    undone.append(
                        {
                            "restored_item_home": item_id,
                            "item": str(entry.get("item") or ""),
                            "home_location_id": previous_home,
                        }
                    )
                except ValueError as exc:
                    skipped.append({"item_id": item_id, "reason": str(exc)})
                continue
            if entry.get("stored") and entry.get("item_id") and entry.get("location_id"):
                item_id = int(entry["item_id"])
                location_id = str(entry["location_id"])
                if entry.get("previous_quantity") is not None:
                    store.put_inventory(
                        item_id=item_id,
                        bin_id=location_id,
                        quantity=float(entry["previous_quantity"]),
                        unit=str(entry.get("previous_unit") or "each"),
                        needs_review=bool(entry.get("previous_needs_review")),
                        review_reason=str(
                            entry.get("previous_review_reason") or ""
                        ),
                        normalize_bin=False,
                    )
                    undone.append(
                        {
                            "restored_item": item_id,
                            "location_id": location_id,
                            "quantity": float(entry["previous_quantity"]),
                        }
                    )
                    continue
                if store.remove_inventory(item_id=item_id, bin_id=location_id, normalize_bin=False):
                    undone.append({"removed_item": item_id, "location_id": location_id})
                else:
                    skipped.append({"item_id": item_id, "location_id": location_id, "reason": "item was not there"})
                continue
            if entry.get("created_location"):
                location_id = str(entry["created_location"])
                subtree = locations.subtree_ids(location_id) if locations.get(location_id) else []
                if subtree and not store.items_in_locations(subtree):
                    removed = locations.delete_subtree(location_id)
                    undone.append({"deleted_location": location_id, "removed": removed})
                elif subtree:
                    skipped.append({"location_id": location_id, "reason": "location is not empty"})
                continue
            skipped.append({"entry": entry, "reason": "unsupported undo entry"})
        undone.extend(learning_undone)
        skipped.extend(learning_skipped)
        store.set_assistant_interaction_status(int(confirmed["id"]), "undone")
        response = {
            "ok": True,
            "message": _undo_message(undone),
            "source_interaction_id": int(confirmed["id"]),
            "undone": undone,
            "skipped": skipped,
        }
        undo_interaction_id = store.record_assistant_interaction(
            route="/assistant/undo",
            command="undo",
            response=response,
            status="undone",
        )
        response["undo_interaction_id"] = undo_interaction_id
        store.update_assistant_interaction_response(
            undo_interaction_id,
            {"undo_interaction_id": undo_interaction_id},
        )
        return response

    @app.post("/kiosk/actions/confirm")
    def kiosk_confirm() -> dict[str, object]:
        return assistant_voice_confirm()

    @app.post("/kiosk/actions/revise")
    def kiosk_revise(request: KioskRevisionRequest) -> dict[str, object]:
        pending = _latest_confirmable_agent_plan()
        if pending is None:
            return {
                "ok": False,
                "message": "There is no pending Magpie plan to change.",
            }
        if not _voice_confirmable_plan(pending):
            return {"ok": False, "message": VOICE_SCOPE_MESSAGE}
        response = pending.get("response")
        presentation = (
            response.get("confirmation")
            if isinstance(response, dict)
            else None
        )
        if not isinstance(presentation, dict):
            presentation = _confirmation_presentation(pending)
        prompts = (
            presentation.get("change_prompts", [])
            if isinstance(presentation, dict)
            else []
        )
        selected = next(
            (
                prompt
                for prompt in prompts
                if isinstance(prompt, dict)
                and str(prompt.get("id") or "") == request.prompt_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=400,
                detail="that change is not available for this proposal",
            )
        result = assistant_revise(
            AssistantRevisionRequest(
                text=str(selected.get("text") or ""),
                source="kiosk",
            )
        )
        result["change_prompt_id"] = request.prompt_id
        return result

    @app.post("/kiosk/actions/cancel")
    def kiosk_cancel() -> dict[str, object]:
        return assistant_voice_cancel()

    @app.post("/kiosk/actions/undo")
    def kiosk_undo() -> dict[str, object]:
        return assistant_undo()

    @app.post("/kiosk/actions/clear")
    def kiosk_clear() -> dict[str, object]:
        return clear()

    def _photo_location_targets(*, plan: list[dict], applied: list[dict]) -> list[str]:
        targets: list[str] = []
        for entry in applied:
            if location_id := entry.get("created_location"):
                targets.append(str(location_id))
        if not targets:
            for step in plan:
                if step.get("tool") != "put_item":
                    continue
                args = step.get("arguments", {}) or {}
                if location_id := args.get("location_id"):
                    targets.append(str(location_id))
        return list(dict.fromkeys(targets))

    def _photo_item_targets(*, applied: list[dict]) -> list[int]:
        targets: list[int] = []
        for entry in applied:
            if entry.get("stored") and entry.get("item_id"):
                targets.append(int(entry["item_id"]))
            elif entry.get("moved_item"):
                targets.append(int(entry["moved_item"]))
        return list(dict.fromkeys(targets))

    def _plan_requests_review(plan: list[dict]) -> bool:
        return any(
            step.get("tool") == "put_item"
            and bool((step.get("arguments", {}) or {}).get("needs_review", False))
            for step in plan
        )

    def _photo_review_targets(
        *, applied: list[dict], explicit_only: bool = False
    ) -> list[tuple[int, str]]:
        targets: list[tuple[int, str]] = []
        seen: set[tuple[int, str]] = set()
        for entry in applied:
            if not entry.get("stored") or not entry.get("item_id") or not entry.get("location_id"):
                continue
            if explicit_only and not entry.get("needs_review"):
                continue
            target = (int(entry["item_id"]), str(entry["location_id"]))
            if target in seen:
                continue
            seen.add(target)
            targets.append(target)
        return targets

    def _photo_sort_needs_review(
        *,
        command: str,
        plan: list[dict],
        photo_ids: list[int],
    ) -> bool:
        if not photo_ids:
            return False
        put_steps = [step for step in plan if step.get("tool") == "put_item"]
        if len(put_steps) > 1:
            return True
        return bool(
            re.search(
                r"\b(photo|sort|separate|split|catalog|catalogue|group|groups)\b",
                command,
                flags=re.IGNORECASE,
            )
        )

    def _review_reason_for_plan(*, explicit_review: bool, photo_review: bool) -> str:
        if photo_review and explicit_review:
            return "Agent marked this photo-sort item for review."
        if photo_review:
            return "Photo sort created multiple item groups; confirm each item and destination."
        return "Agent marked this item for review."

    def _record_edited_photo_sort_example(
        *,
        command: str,
        photo_ids: list[int],
        confirmed_plan: list[dict],
        source_interaction_id: int | None,
    ) -> None:
        if source_interaction_id is None:
            return
        source = store.assistant_interaction(source_interaction_id)
        if source is None:
            return
        original_plan = source.get("response", {}).get("plan")
        if not isinstance(original_plan, list):
            return
        source_photo_ids = [
            int(photo_id)
            for photo_id in source.get("photo_ids", [])
            if isinstance(photo_id, int)
        ]
        if not _photo_sort_needs_review(
            command=str(source.get("command") or command),
            plan=original_plan,
            photo_ids=source_photo_ids or photo_ids,
        ):
            return
        if _canonical_plan(original_plan) == _canonical_plan(confirmed_plan):
            return
        store.record_example(
            command=str(source.get("command") or command),
            action=json.dumps(
                {
                    "original_plan": original_plan,
                    "corrected_plan": confirmed_plan,
                    "photo_ids": source_photo_ids or photo_ids,
                },
                sort_keys=True,
            ),
            kind="edited_photo_sort",
        )

    def _record_rejected_photo_sort_example(*, source: dict[str, object]) -> None:
        plan = source.get("response", {}).get("plan")
        if not isinstance(plan, list):
            return
        photo_ids = [
            int(photo_id)
            for photo_id in source.get("photo_ids", [])
            if isinstance(photo_id, int)
        ]
        command = str(source.get("command") or "")
        if not _photo_sort_needs_review(command=command, plan=plan, photo_ids=photo_ids):
            return
        store.record_example(
            command=command,
            action=json.dumps(
                {
                    "rejected_plan": plan,
                    "photo_ids": photo_ids,
                },
                sort_keys=True,
            ),
            kind="rejected_photo_sort",
        )

    def _record_review_correction_example(
        *,
        before: dict[str, object],
        after: dict[str, object],
        command: str = "",
    ) -> None:
        if command.strip():
            store.record_example(
                command=command,
                action=json.dumps(
                    {
                        "before": before,
                        "after": after,
                    },
                    sort_keys=True,
                ),
                kind="review_correction",
            )
            return
        _record_inventory_correction_example(
            before=before,
            after=after,
            kind="review_correction",
            command_prefix="review corrected",
        )

    def _record_inventory_correction_example(
        *,
        before: dict[str, object],
        after: dict[str, object],
        kind: str = "item_correction",
        command_prefix: str = "item corrected",
    ) -> None:
        before_name = str(before.get("name") or "")
        after_name = str(after.get("name") or "")
        if not before_name and not after_name:
            return
        store.record_example(
            command=f"{command_prefix} {before_name} to {after_name}".strip(),
            action=json.dumps(
                {
                    "before": before,
                    "after": after,
                },
                sort_keys=True,
            ),
            kind=kind,
        )

    def _canonical_plan(plan: list[dict]) -> str:
        return json.dumps(plan, sort_keys=True, separators=(",", ":"))

    def _last_applied_location(applied: list[dict]) -> str | None:
        for entry in reversed(applied):
            if location_id := entry.get("location_id"):
                return str(location_id)
            if location_id := entry.get("to_location_id"):
                return str(location_id)
            if location_id := entry.get("created_location"):
                return str(location_id)
        return None

    def _confirmation_receipt(
        *,
        plan: list[dict],
        applied: list[dict],
        photo_ids: list[int],
        photo_attachments: dict[str, object],
        needs_review: bool,
    ) -> dict[str, object]:
        def location_label(raw_location_id: object) -> str:
            location_id = str(raw_location_id or "")
            if not location_id:
                return ""
            return " › ".join(
                node.label or node.id
                for node in location_path(locations, location_id)
            ) or location_id

        focus_location_id = None
        for entry in reversed(applied):
            if location_id := entry.get("to_location_id") or entry.get("location_id"):
                focus_location_id = str(location_id)
                break
        if focus_location_id is None:
            created_ids = [
                str(entry["created_location"])
                for entry in applied
                if entry.get("created_location")
            ]
            for step in plan:
                if step.get("tool") != "create_location":
                    continue
                args = step.get("arguments", {}) or {}
                if args.get("parent_id"):
                    continue
                candidate = str(args.get("id") or slugify(str(args.get("label") or "")))
                if candidate in created_ids:
                    focus_location_id = candidate
                    break
            if focus_location_id is None and created_ids:
                focus_location_id = created_ids[0]

        linked_photo_ids = {
            int(link["photo_id"])
            for key in ("item_links", "location_links")
            for link in photo_attachments.get(key, [])
            if isinstance(link, dict) and link.get("photo_id") is not None
        }
        pending_photo_ids = {
            int(link["photo_id"])
            for link in photo_attachments.get("pending", [])
            if isinstance(link, dict) and link.get("photo_id") is not None
        }
        stored_entries = [entry for entry in applied if entry.get("stored")]
        moved_entries = [entry for entry in applied if entry.get("moved_item")]
        removed_entries = [entry for entry in applied if entry.get("removed_item")]
        created_entries = [entry for entry in applied if entry.get("created_location")]
        reviewed_entries = [entry for entry in applied if entry.get("approved_review")]
        if len(stored_entries) == 1:
            entry = stored_entries[0]
            summary = (
                f"Stored {entry['stored']} in {location_label(entry.get('location_id'))}."
            )
        elif stored_entries:
            summary = f"Stored {len(stored_entries)} items."
        elif len(moved_entries) == 1:
            entry = moved_entries[0]
            summary = (
                f"Moved {entry.get('item') or 'the item'} to "
                f"{location_label(entry.get('to_location_id') or entry.get('location_id'))}."
            )
        elif moved_entries:
            summary = (
                f"Moved {len(moved_entries)} inventory records to "
                f"{location_label(moved_entries[0].get('to_location_id') or moved_entries[0].get('location_id'))}."
            )
        elif len(removed_entries) == 1:
            entry = removed_entries[0]
            summary = (
                f"Removed {entry.get('item') or 'the item'} from "
                f"{location_label(entry.get('location_id'))}."
            )
        elif removed_entries:
            summary = f"Removed {len(removed_entries)} inventory records."
        elif len(created_entries) == 1:
            summary = (
                f"Created {location_label(created_entries[0].get('created_location'))}."
            )
        elif created_entries:
            summary = f"Created {len(created_entries)} storage places."
        elif len(reviewed_entries) == 1:
            entry = reviewed_entries[0]
            summary = (
                f"Approved {entry.get('item') or 'the item'} in "
                f"{location_label(entry.get('location_id'))}."
            )
        else:
            summary = f"Applied {len(applied)} change{'s' if len(applied) != 1 else ''}."
        return {
            "schema": "magpie.confirmation_receipt.v1",
            "change_count": len(applied),
            "focus_location_id": focus_location_id,
            "summary": summary,
            "photos": {
                "uploaded": len(set(photo_ids)),
                "attached": len(linked_photo_ids),
                "pending": len(pending_photo_ids),
            },
            "needs_review": needs_review,
            "undo_available": bool(applied),
        }

    def _existing_location_id(raw: str) -> str | None:
        if locations.get(raw) is not None:
            return raw
        slug = slugify(raw)
        if locations.get(slug) is not None:
            return slug
        upper = raw.upper()
        if locations.get(upper) is not None:
            return upper
        return None

    def _path_for(location_id: str) -> list[dict[str, object]]:
        ancestry = location_path(locations, location_id)
        if ancestry:
            all_locations = locations.all(include_archived=True)
            normalized = {
                node.id: re.sub(r"\s+", " ", node.label or node.id)
                .strip()
                .casefold()
                for node in all_locations
            }
            label_counts = Counter(
                (node.parent_id, node.active, normalized[node.id])
                for node in all_locations
            )
            ambiguous_ids = {
                node.id
                for node in all_locations
                if label_counts[(node.parent_id, node.active, normalized[node.id])] > 1
            }
            return [
                {
                    "id": node.id,
                    "label": node.label,
                    **(
                        {"display_label": f"{node.label or node.id} · {node.id}"}
                        if node.id in ambiguous_ids
                        else {}
                    ),
                }
                for node in ancestry
            ]
        return [{"id": location_id, "label": location_id}]

    def _resolve_results(results) -> tuple[list[dict[str, object]], list[int]]:
        out: list[dict[str, object]] = []
        pixels: list[int] = []
        for r in results:
            lit = locations.nearest_lit(r.bin_id)
            if lit is not None:
                pixels.extend(lit.light_indexes)
            payload = r.as_dict()
            payload["location_id"] = r.bin_id
            payload["path"] = _path_for(r.bin_id)
            payload["lit_location"] = lit.id if lit is not None else None
            stored_location = locations.get_any(r.bin_id)
            payload["archived"] = bool(
                stored_location is not None and not stored_location.active
            )
            out.append(payload)
        return out, pixels

    def _light_pixels(
        pixels: list[int],
        seconds: float = DEFAULT_LIGHT_SECONDS,
        *,
        color: tuple[int, int, int] = (255, 255, 255),
        brightness: float | None = None,
        target: str = "",
    ) -> bool:
        # Lights always time out (the controller clears them after `seconds`).
        pixel_count = _target_pixel_count(target)
        wanted = sorted({p for p in pixels if 0 <= p < pixel_count})
        if not wanted:
            return False
        try:
            _controller_set_pixels_for(
                [PixelUpdate(index=i, color=color) for i in wanted],
                target=target,
                clear_others=True,
                brightness=brightness if brightness is not None else cfg.default_brightness,
                seconds=seconds,
            )
            return True
        except Exception:  # lighting is best-effort; a dead strip must not 500
            return False

    def _save_light_effect(
        pixels: list[int], *, seconds: float, target: str = ""
    ) -> bool:
        pixel_count = _target_pixel_count(target)
        wanted = sorted({p for p in pixels if 0 <= p < pixel_count})
        if not wanted:
            return False
        try:
            if isinstance(controller, WorkspaceLedController):
                controller.pulse_pixels(
                    wanted,
                    (40, 255, 120),
                    target=target,
                    pixel_count=pixel_count,
                    workspace_id=_state_workspace_id(),
                    brightness=cfg.default_brightness,
                    seconds=seconds,
                )
            else:
                controller.pulse_pixels(
                    wanted,
                    (40, 255, 120),
                    brightness=cfg.default_brightness,
                    seconds=seconds,
                )
            return True
        except Exception:  # inventory confirmation must survive lighting failures
            return False

    @app.post("/locations/{location_id}/light")
    def light_location(location_id: str) -> dict[str, object]:
        """Light the nearest lit ancestor of a location (for select-to-light).
        Times out automatically."""
        lit = locations.nearest_lit(location_id)
        if lit is None:
            return {"ok": True, "lit": False}
        return {
            "ok": True,
            "lit": _light_pixels(
                lit.light_indexes, seconds=120.0, target=str(lit.light_target or "")
            ),
            "lit_location": lit.id,
        }

    @app.post("/locations/{location_id}/light-color")
    def light_location_color(location_id: str, request: LocationLightRequest) -> dict[str, object]:
        lit = locations.nearest_lit(location_id)
        if lit is None:
            return {"ok": True, "lit": False}
        return {
            "ok": True,
            "lit": _light_pixels(
                lit.light_indexes,
                seconds=request.seconds,
                color=request.color,
                brightness=request.brightness,
                target=str(lit.light_target or ""),
            ),
            "lit_location": lit.id,
            "color": request.color,
        }

    @app.post("/locations/light-colors")
    def light_locations_color(request: LocationLightBatchRequest) -> dict[str, object]:
        updates_by_target: dict[str, list[PixelUpdate]] = {}
        lit_locations: list[dict[str, object]] = []
        seen_pixels_by_target: dict[str, set[int]] = {}
        for entry in request.locations:
            lit = locations.nearest_lit(entry.location_id)
            if lit is None:
                continue
            target = str(lit.light_target or "")
            lit_locations.append(
                {
                    "location_id": entry.location_id,
                    "lit_location": lit.id,
                    "light_target": target,
                    "color": entry.color,
                }
            )
            seen_pixels = seen_pixels_by_target.setdefault(target, set())
            updates = updates_by_target.setdefault(target, [])
            for index in lit.light_indexes:
                if not 0 <= index < _target_pixel_count(target):
                    continue
                if index in seen_pixels:
                    continue
                seen_pixels.add(index)
                updates.append(PixelUpdate(index=index, color=entry.color))
        if not any(updates_by_target.values()):
            return {"ok": True, "lit": False, "locations": lit_locations}
        try:
            for target, updates in updates_by_target.items():
                _controller_set_pixels_for(
                    updates,
                    target=target,
                    clear_others=True,
                    brightness=(
                        request.brightness
                        if request.brightness is not None
                        else cfg.default_brightness
                    ),
                    seconds=request.seconds,
                )
            return {"ok": True, "lit": True, "locations": lit_locations}
        except Exception:
            return {"ok": True, "lit": False, "locations": lit_locations}

    def _assistant_find_response(
        request: AssistantFindRequest,
        *,
        route: str = "/assistant/find",
    ) -> dict[str, object]:
        """The resolver: find an item, light the nearest lit ancestor, and report
        the full path so words can carry the rest. Works with no LLM."""
        queries = _split_find_queries(request.query)
        out: list[dict[str, object]] = []
        matches_by_query: list[tuple[str, list[dict[str, object]]]] = []
        seen: set[tuple[object, object]] = set()
        per_query_limit = max(1, 8 // len(queries))
        for query in queries:
            query_results, _ = _resolve_results(
                search_inventory(store, query, limit=per_query_limit)
            )
            matches_by_query.append((query, query_results))
            for result in query_results:
                key = (result.get("item_id"), result.get("location_id"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(result)
        light_state = (
            _activate_find(request.query, out)
            if out
            else {"lit": False, "locations": [], "active_finds": _active_find_snapshot()}
        )
        response = {
            "ok": True,
            "action": "found" if out else "not_found",
            "query": request.query,
            "message": _compound_find_response_message(matches_by_query)
            if len(queries) > 1
            else _find_response_message(query=request.query, results=out),
            "results": out,
            "lit": bool(light_state.get("lit")),
            "light_assignments": light_state.get("locations", []),
            "active_finds": light_state.get("active_finds", []),
        }
        interaction_id = store.record_assistant_interaction(
            route=route,
            command=request.query,
            photo_ids=request.photo_ids,
            response=response,
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/assistant/find")
    def assistant_find(request: AssistantFindRequest) -> dict[str, object]:
        return _assistant_find_response(request)

    @app.post("/assistant/find/not-here")
    def assistant_find_not_here(
        request: FindNotHereRequest,
    ) -> dict[str, object]:
        response = _report_find_not_here(
            item_id=request.item_id,
            location_id=request.location_id,
            query=request.query,
            active_find_id=request.active_find_id,
            source=request.source,
        )
        interaction_id = _record_manual_inventory_interaction(
            command=(
                request.query.strip()
                or f"{response['item']} was not in {response['location_id']}"
            ),
            response={**response, "action": "find_not_here"},
        )
        response["interaction_id"] = interaction_id
        return response

    def _split_find_queries(query: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", query).strip(" ,;.")
        cleaned = re.sub(
            r"\s+from\s+(?:the\s+)?(?:top|bottom|middle|first|second|third|last)\s+"
            r"(?:drawer|shelf|bin|cabinet)\s*$",
            "",
            cleaned,
            flags=re.I,
        )
        parts = [part.strip(" ,;.") for part in re.split(r"\s+(?:and|plus)\s+", cleaned, flags=re.I)]
        parts = [re.sub(r"^(?:the|a|an)\s+", "", part, flags=re.I) for part in parts]
        useful = [part for part in parts if re.search(r"[A-Za-z0-9]", part)]
        return useful or [cleaned]

    def _compound_find_response_message(
        matches_by_query: list[tuple[str, list[dict[str, object]]]],
    ) -> str:
        messages: list[str] = []
        for query, results in matches_by_query:
            if not results:
                messages.append(f"No match for {query}")
                continue
            top = results[0]
            path = top.get("path") if isinstance(top, dict) else []
            labels = [
                str(node.get("label") or node.get("id"))
                for node in path
                if isinstance(node, dict) and (node.get("label") or node.get("id"))
            ]
            place = " > ".join(labels) or str(
                top.get("location_id") or top.get("bin_id") or ""
            )
            archived = " in archived storage" if top.get("archived") else ""
            name = str(top.get("name") or query)
            messages.append(
                (
                    f"{name} was last recorded in {place}, but that location needs review"
                    if top.get("needs_review")
                    else f"{name} is in {place}{archived}"
                )
            )
        return "; ".join(messages) + "."

    def _find_response_message(*, query: str, results: list[dict[str, object]]) -> str:
        if not results:
            return f"No match for {query}."
        top = results[0]
        path = top.get("path") if isinstance(top, dict) else []
        labels = [
            str(node.get("label") or node.get("id"))
            for node in path
            if isinstance(node, dict) and (node.get("label") or node.get("id"))
        ]
        place = " > ".join(labels) or str(top.get("location_id") or top.get("bin_id") or "")
        name = str(top.get("name") or query)
        suffix = "" if len(results) == 1 else f" plus {len(results) - 1} more match(es)"
        archived = " in archived storage" if top.get("archived") else ""
        if top.get("needs_review"):
            return (
                f"{name} was last recorded in {place}, but that location needs "
                f"review{suffix}."
            )
        return f"{name} is in {place}{archived}{suffix}."

    @app.post("/assistant/image")
    def assistant_image(request: GenerateImageRequest) -> dict[str, object]:
        """Search the web for a real photo of the item and attach it (no key)."""
        try:
            data, content_type, source = search_item_image(request.name)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"image search failed: {exc}") from exc
        photo = store.add_photo(
            filename=f"item-{request.item_id}.jpg", content_type=content_type, data=data
        )
        store.attach_photo(item_id=request.item_id, photo_id=photo.id)
        return {"ok": True, "photo": photo.as_dict(), "source": source}

    def _assistant_ask_response(
        request: AssistantFindRequest,
        *,
        route: str = "/assistant/ask",
        put_away_intent: bool = False,
        recorded_command: str | None = None,
    ) -> dict[str, object]:
        """One input, no verbs needed: if you already have the item, find + light
        it; if you don't, put it away — pick an existing empty bin and light it."""
        def explicit_destination(text: str) -> tuple[str, str | None]:
            candidates: list[tuple[str, str]] = []
            for location in locations.all():
                for alias in {location.id, location.label}:
                    cleaned_alias = str(alias or "").strip()
                    if cleaned_alias:
                        candidates.append((cleaned_alias, location.id))
            candidates.sort(key=lambda candidate: len(candidate[0]), reverse=True)
            for alias, location_id in candidates:
                match = re.fullmatch(
                    rf"(?P<item>.+?)\s+(?:in|into)\s+(?:the\s+)?{re.escape(alias)}",
                    text.strip(" .?!"),
                    re.I,
                )
                if match and match.group("item").strip():
                    return match.group("item").strip(), location_id
            return text, None

        def explicit_quantity(text: str) -> tuple[str, float, str | None]:
            match = re.fullmatch(
                r"(?:(?:add|adding)\s+)?(?:another\s+)?"
                r"(?P<quantity>\d+(?:\.\d+)?)\s+"
                r"(?:(?:more|additional|extra)\s+)?"
                r"(?:(?P<unit>each|pieces?|pcs?|packs?|sets?|boxes?|bags?)\s+(?:of\s+)?)?"
                r"(?P<item>.+)",
                text.strip(),
                re.I,
            )
            if not match:
                return text, 1.0, None
            item = re.sub(
                r"^(?:of\s+)?(?:the\s+)?",
                "",
                match.group("item").strip(),
                flags=re.I,
            )
            if re.match(
                r"^(?:mm|millimeters?|cm|centimeters?|m|meters?|in|inch(?:es)?|ft|feet|foot)\b",
                item,
                re.I,
            ):
                return text, 1.0, None
            unit_aliases = {
                "piece": "each",
                "pieces": "each",
                "pc": "each",
                "pcs": "each",
                "packs": "pack",
                "sets": "set",
                "boxes": "box",
                "bags": "bag",
            }
            raw_unit = (match.group("unit") or "").lower()
            unit = unit_aliases.get(raw_unit, raw_unit or None)
            return item, float(match.group("quantity")), unit

        item_query, requested_destination_id = explicit_destination(request.query)
        item_query, stated_quantity, stated_unit = explicit_quantity(item_query)
        name = normalized_query(item_query)
        locked_destination_id = (
            _existing_location_id(request.current_location_id or "")
            if request.destination_locked
            else None
        )
        locked_destination = (
            locations.get(locked_destination_id) if locked_destination_id else None
        )
        if locked_destination is None or not locked_destination.accepts_items:
            locked_destination_id = None
        photos = [p for pid in request.photo_ids if (p := store.photo(pid)) is not None]
        def logged(response: dict[str, object]) -> dict[str, object]:
            plan = response.get("plan")
            if isinstance(plan, list) and plan:
                _supersede_pending_agent_plans()
            interaction_id = store.record_assistant_interaction(
                route=route,
                command=recorded_command or request.query,
                photo_ids=request.photo_ids,
                response=response,
                status="pending" if isinstance(plan, list) and plan else "",
            )
            response["interaction_id"] = interaction_id
            return response

        def put_away_destination_handoff(
            *,
            item_name: str,
            size_class: str,
        ) -> dict[str, object]:
            active_locations = [
                location for location in locations.all() if location.active
            ]
            suggested_type = (
                "bin"
                if size_class in {"tiny", "small"}
                else "box"
                if size_class == "medium"
                else "tote"
            )
            suggested_label = (
                "General storage"
                if not active_locations
                else f"{item_name} {suggested_type}"
            )
            response = logged(
                {
                    "action": "place_configuration_ui_required",
                    "message": (
                        "Create or choose an item destination in Places. "
                        f"I kept {item_name} and any photos ready so put-away "
                        "can continue."
                    ),
                    "item": {"name": item_name, "size_class": size_class},
                    "results": [],
                    "plan": [],
                }
            )
            interaction_id = int(response["interaction_id"])
            handoff = {
                "kind": "put_away_destination",
                "command": recorded_command or request.query,
                "resume_command": f"Put away {item_name}",
                "photo_ids": list(request.photo_ids),
                "current_location_id": request.current_location_id,
                "suggested_container": {
                    "label": suggested_label[:120],
                    "type": suggested_type,
                    "capacity_class": size_class,
                    "parent_hint": "",
                    "parent_id": None,
                },
            }
            places_url = f"/storage?view=places&resume={interaction_id}"
            response["places_url"] = places_url
            response["handoff"] = handoff
            store.update_assistant_interaction_response(
                interaction_id,
                {"places_url": places_url, "handoff": handoff},
            )
            return response

        def user_placement_reason(
            raw_reasons: list[str],
            *,
            size_reason: str,
            destination_id: str,
        ) -> str:
            destination = locations.get(destination_id)
            destination_label = (
                str(destination.label or destination.id).strip()
                if destination is not None
                else "This location"
            )
            destination_type = (
                str(destination.type or "location").replace("_", " ").strip()
                if destination is not None
                else "location"
            )
            if "using the destination selected in Places" in raw_reasons:
                return f"you selected {destination_label} in Places"
            explicit_size = re.search(
                r"\b(tiny|small|medium|large|oversize)\b",
                request.query,
                re.I,
            )
            if explicit_size:
                requested = explicit_size.group(1).lower()
                scope = " in the main cabinet" if "main cabinet" in request.query.lower() else ""
                return f"you asked for a {requested} bin{scope}"
            if size_reason.startswith("learned size rule:"):
                return f"this follows your rule: {size_reason.removeprefix('learned size rule:').strip()}"
            for reason in raw_reasons:
                if reason.startswith("learned rule:"):
                    rule = reason.split(":", 1)[1].strip()
                    return f"this follows your rule: {rule}"
                if reason.startswith("inside learned rule target:"):
                    rule = reason.split(":", 1)[1].strip()
                    return f"{destination_label} is inside the place set by your rule: {rule}"
            if any(
                reason == "learned home for this exact item"
                for reason in raw_reasons
            ):
                return f"you previously chose {destination_label} for this item"
            if any(
                reason == "inside the learned home for this exact item"
                for reason in raw_reasons
            ):
                return (
                    f"{destination_label} is inside the home you chose for this item"
                )
            for reason in raw_reasons:
                if reason.startswith("learned home from "):
                    count = reason.removeprefix("learned home from ").split(" ", 1)[0]
                    return (
                        f"you previously chose {destination_label} for "
                        f"{count} similar items"
                    )
                if reason.startswith("inside a learned family home from "):
                    count = reason.removeprefix(
                        "inside a learned family home from "
                    ).split(" ", 1)[0]
                    return (
                        f"{destination_label} is inside the home learned from "
                        f"{count} similar items"
                    )
            for reason in raw_reasons:
                if reason.startswith("similar items already there:"):
                    related = reason.split(":", 1)[1].strip()
                    return f"similar {related} items are already there"
                if reason.startswith("matches purpose tags:"):
                    tags = reason.split(":", 1)[1].strip()
                    return f"{destination_label} is intended for {tags}"
                if reason.startswith("matches location name:"):
                    label = reason.split(":", 1)[1].strip()
                    return f"the location is labeled for {label}"
                if reason.startswith("matches placement notes:"):
                    notes = reason.split(":", 1)[1].strip()
                    return f"its placement notes match {notes}"
            if any(
                reason == "using the selected location because it fits"
                for reason in raw_reasons
            ):
                return f"you selected {destination_label}, and it fits this item"
            if any(
                reason.startswith("only empty storage location")
                for reason in raw_reasons
            ):
                return "it is the only empty storage location that fits"
            if any(
                reason.startswith("first empty")
                for reason in raw_reasons
            ):
                return f"it is the first available {destination_type} of the right size"
            if any(
                reason.startswith("measured item") and " fits " in reason
                for reason in raw_reasons
            ):
                return f"{destination_label}'s measured internal space fits this item"
            if any(
                reason == "capacity is not much larger than needed"
                for reason in raw_reasons
            ):
                return "it is the closest available size for this item"
            if raw_reasons:
                return f"{destination_label} fits this item's size"
            return ""

        def learning_adherence(
            *,
            item_name: str,
            destination_id: str,
            size_class: str,
        ) -> dict[str, object]:
            rule_text = " ".join(
                part for part in (item_name, request.query) if str(part).strip()
            )
            evaluations: list[dict[str, object]] = []
            for rule in store.matching_size_rules(rule_text, limit=5):
                evaluations.append(
                    {
                        "rule_id": rule.id,
                        "rule_type": "size",
                        "rule_text": rule.rule_text,
                        "expected": rule.size_class,
                        "actual": size_class,
                        "followed": size_class == rule.size_class,
                    }
                )
            for rule in store.matching_rules(rule_text, limit=5):
                evaluations.append(
                    {
                        "rule_id": rule.id,
                        "rule_type": "placement",
                        "rule_text": rule.rule_text,
                        "expected": rule.target_location_id,
                        "actual": destination_id,
                        "followed": destination_id == rule.target_location_id,
                    }
                )
            return {
                "rule_adherence": evaluations,
                "followed_rule_ids": [
                    evaluation["rule_id"]
                    for evaluation in evaluations
                    if evaluation["followed"]
                ],
                "not_followed_rule_ids": [
                    evaluation["rule_id"]
                    for evaluation in evaluations
                    if not evaluation["followed"]
                ],
            }

        if not name and not photos:
            return logged(
                {
                    "action": "unknown",
                    "message": "Tell me an item, or attach a photo.",
                    "results": [],
                    "plan": [],
                }
            )

        correction = _fit_correction(name)
        if correction and request.current_location_id:
            moved = _handle_fit_correction(request.query, request.current_location_id)
            if moved is not None:
                return logged(moved)

        def _found(results, message):
            out, _ = _resolve_results(results)
            light_state = _activate_find(request.query, out)
            return logged(
                {
                    "action": "found",
                    "message": message.format(
                        name=out[0]["name"],
                        place=" › ".join(str(p["label"]) for p in out[0]["path"]),
                    ),
                    "results": out,
                    "lit": bool(light_state.get("lit")),
                    "light_assignments": light_state.get("locations", []),
                    "active_finds": light_state.get("active_finds", []),
                    "plan": [],
                }
            )

        def _put_with_existing(top) -> dict[str, object]:
            out, _pixels = _resolve_results([top])
            resolved = out[0]
            destination_id = str(
                resolved.get("location_id") or resolved.get("bin_id") or top.bin_id
            )
            quantity = stated_quantity
            unit = stated_unit or top.unit or "each"
            existing_size, _ = item_capacity_for(
                top.name,
                store=store,
                context_text=request.query,
            )
            uses_locked_destination = bool(locked_destination_id)
            learned_rule_target = "" if uses_locked_destination else next(
                (
                    rule.target_location_id
                    for rule in store.matching_rules(
                        f"{top.name} {request.query}",
                        limit=5,
                    )
                    if _location_can_fit(
                        rule.target_location_id,
                        existing_size,
                        top.dimensions,
                    )
                ),
                "",
            )
            learned_home = str(top.home_location_id or "").strip()
            uses_learned_home = bool(
                not uses_locked_destination
                and not learned_rule_target
                and learned_home
                and learned_home != destination_id
                and _location_can_fit(learned_home, existing_size, top.dimensions)
            )
            uses_learned_rule = bool(learned_rule_target)
            if uses_locked_destination or uses_learned_rule or uses_learned_home:
                destination_id = locked_destination_id or learned_rule_target or learned_home
                path = _path_for(destination_id)
                resolved = {
                    **resolved,
                    "location_id": destination_id,
                    "bin_id": destination_id,
                    "path": path,
                }
            else:
                path = (
                    resolved.get("path")
                    if isinstance(resolved.get("path"), list)
                    else []
                )
            place = " › ".join(
                str(node.get("label") or node.get("id"))
                for node in path
                if isinstance(node, dict)
            ) or destination_id
            light_state = _activate_find(
                request.query,
                [resolved],
                kind="put_away",
            )
            plan = [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": top.name,
                        "location_id": destination_id,
                        "quantity": quantity,
                        "unit": unit,
                        "quantity_mode": "add",
                    },
                }
            ]
            placement_reasons = (
                ["using the destination selected in Places"]
                if uses_locked_destination
                else ["learned placement rule"]
                if uses_learned_rule
                else ["learned home for this exact item"]
                if uses_learned_home
                else ["matching items already stored there"]
            )
            display_reason = (
                f"you selected {path[-1]['label'] if path else destination_id} in Places"
                if uses_locked_destination
                else f"this follows your placement rule for {top.name}"
                if uses_learned_rule
                else f"you previously chose {path[-1]['label']} for this item"
                if uses_learned_home and path
                else f"matching {top.name} are already there"
            )
            message_prefix = (
                f"Put this {top.name} in {place}, the destination you selected. "
                if uses_locked_destination
                else f"Put this {top.name} in {place}, following your placement rule. "
                if uses_learned_rule
                else f"Put this {top.name} in {place}, the home you chose. "
                if uses_learned_home
                else f"Put them with the existing {top.name} in {place}. "
            )
            return logged(
                {
                    "action": "put_away",
                    "message": message_prefix + f"Confirm to add {quantity:g} {unit}.",
                    "item": {
                        "item_id": top.item_id,
                        "name": top.name,
                        "category": top.category,
                        "aliases": top.aliases,
                        "quantity": quantity,
                        "unit": unit,
                        "size_class": top.size_class,
                        "dimensions": top.dimensions or {},
                    },
                    "destination": {"id": destination_id, "path": path},
                    "placement": {
                        "reason": display_reason,
                        "reasons": placement_reasons,
                    },
                    "results": [resolved],
                    "lit": bool(light_state.get("lit")),
                    "light_assignments": light_state.get("locations", []),
                    "active_finds": light_state.get(
                        "active_finds",
                        _active_find_snapshot(),
                    ),
                    "plan": plan,
                    "existing_item": True,
                    "learning": learning_adherence(
                        item_name=top.name,
                        destination_id=destination_id,
                        size_class=top.size_class or existing_size,
                    ),
                }
            )

        def likely_typo_match(query: str):
            normalized = normalized_query(query)
            if len(normalized) < 6 or len(normalized.split()) < 2:
                return None
            scored: list[tuple[float, object]] = []
            for candidate in store.list_inventory():
                names = [
                    candidate_name
                    for candidate_name in [candidate.name, *candidate.aliases]
                    if candidate_name.strip()
                    and not identities_conflict(query, candidate_name)
                ]
                score = max(
                    (
                        SequenceMatcher(
                            None,
                            normalized,
                            normalized_query(candidate_name),
                        ).ratio()
                        for candidate_name in names
                    ),
                    default=0.0,
                )
                scored.append((score, candidate))
            scored.sort(key=lambda entry: entry[0], reverse=True)
            if not scored or scored[0][0] < 0.86:
                return None
            if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
                return None
            return scored[0][1]

        # Fast path: a text query you already have -> either find it or propose
        # adding another quantity beside the existing item.
        if name:
            found = store.search(name, limit=8)
            if found:
                if put_away_intent:
                    identity = resolve_item_identity(name, found)
                    if identity.status == "ambiguous":
                        candidates = [
                            match.candidate.as_dict()
                            for match in identity.matches[:5]
                        ]
                        return logged(
                            {
                                "action": "clarify_item_identity",
                                "message": (
                                    "I found multiple hardware variants. "
                                    "Which exact item are you putting away?"
                                ),
                                "query": name,
                                "results": candidates,
                                "identity_candidates": candidates,
                                "lit": False,
                                "plan": [],
                            }
                        )
                    top = identity.selected
                    if top is None:
                        found = []
                if found and put_away_intent:
                    assert top is not None
                    return _put_with_existing(top)
                if found:
                    return _found(found, "You have {name} — in {place}.")
            if put_away_intent and (typo_match := likely_typo_match(name)) is not None:
                return _put_with_existing(typo_match)

        # New text items stay local; photos use vision to identify and enrich.
        draft: dict[str, object] = {
            "name": title_item_name(name) or "Item",
            "category": infer_category(name),
            "aliases": alias_candidates(name) if name else [],
            "unit": stated_unit or "each", "quantity": stated_quantity,
        }
        if _llm_key_present(cfg) and photos:
            try:
                draft = enrich_item_draft(
                    request.query or "Identify this item.",
                    photos=photos,
                    client=llm_client,
                    photo_loader=store.photo_content,
                )
            except LlmError:
                pass
        if stated_quantity != 1.0 or stated_unit:
            draft["quantity"] = stated_quantity
            draft["unit"] = stated_unit or str(draft.get("unit") or "each")
        corrected = str(draft["name"])
        draft_dimensions = inferred_dimensions(corrected)
        if draft_dimensions:
            draft["dimensions"] = draft_dimensions
        needed_capacity, size_reason = item_capacity_for(
            corrected,
            store=store,
            context_text=request.query,
        )
        draft["size_class"] = needed_capacity

        # The cleaned / photo-identified name might be something already on hand.
        if corrected.lower() != name:
            refound = store.search(corrected, limit=8)
            if refound:
                if put_away_intent:
                    identity = resolve_item_identity(corrected, refound)
                    if identity.status == "ambiguous":
                        candidates = [
                            match.candidate.as_dict()
                            for match in identity.matches[:5]
                        ]
                        return logged(
                            {
                                "action": "clarify_item_identity",
                                "message": (
                                    "I found multiple hardware variants. "
                                    "Which exact item are you putting away?"
                                ),
                                "query": corrected,
                                "results": candidates,
                                "identity_candidates": candidates,
                                "lit": False,
                                "plan": [],
                            }
                        )
                    if identity.selected is not None:
                        return _put_with_existing(identity.selected)
                return _found(refound, "That looks like {name} — you have it, in {place}.")
        placement = (
            {
                "location_id": locked_destination_id,
                "score": None,
                "reasons": ["using the destination selected in Places"],
            }
            if locked_destination_id
            else _put_away_destination(
                corrected,
                request.query,
                current_location_id=requested_destination_id or request.current_location_id,
                needed_capacity=needed_capacity,
                item_dimensions=(
                    draft.get("dimensions")
                    if isinstance(draft.get("dimensions"), dict)
                    else None
                ),
            )
        )
        if placement is None:
            return put_away_destination_handoff(
                item_name=corrected,
                size_class=needed_capacity,
            )
        dest = str(placement["location_id"])
        placement_reasons = [
            str(reason)
            for reason in placement.get("reasons", [])
            if str(reason).strip()
        ]
        placement_reason = placement_reasons[0] if placement_reasons else ""
        display_reason = user_placement_reason(
            placement_reasons,
            size_reason=size_reason,
            destination_id=dest,
        )
        path = _path_for(dest)
        light_state = _activate_find(
            request.query,
            [
                {
                    "name": corrected,
                    "location_id": dest,
                    "path": path,
                }
            ],
            kind="put_away",
        )
        place = " › ".join(str(p["label"]) for p in path)
        plan = [
            {
                "tool": "put_item",
                "arguments": {
                    "item": corrected,
                    "location_id": dest,
                    "quantity": float(draft.get("quantity") or 1),
                    "unit": str(draft.get("unit") or "each"),
                    "size_class": needed_capacity,
                    **(
                        {"dimensions": draft["dimensions"]}
                        if isinstance(draft.get("dimensions"), dict)
                        else {}
                    ),
                },
            }
        ]
        return logged(
            {
                "action": "put_away",
                "message": f"File {corrected} in {place}. Confirm to save it.",
                "item": draft,
                "size": {
                    "class": needed_capacity,
                    "reason": size_reason,
                },
                "placement": {
                    "reason": display_reason,
                    "reasons": placement_reasons,
                    "raw_reason": placement_reason,
                    "score": placement.get("score"),
                },
                "destination": {"id": dest, "path": path},
                "lit": bool(light_state.get("lit")),
                "light_assignments": light_state.get("locations", []),
                "active_finds": light_state.get(
                    "active_finds",
                    _active_find_snapshot(),
                ),
                "plan": plan,
                "learning": learning_adherence(
                    item_name=corrected,
                    destination_id=dest,
                    size_class=needed_capacity,
                ),
            }
        )

    @app.post("/assistant/ask")
    def assistant_ask(request: AssistantFindRequest) -> dict[str, object]:
        return _assistant_ask_response(request)

    def _fit_correction(text: str) -> bool:
        return bool(
            re.search(
                r"\b(won'?t fit|doesn'?t fit|too big|too small|wrong spot|wrong bin|move it|put it)\b",
                text,
                re.I,
            )
        )

    def _handle_fit_correction(text: str, current_location_id: str) -> dict[str, object] | None:
        source = _existing_location_id(current_location_id)
        if source is None:
            return None
        items = store.items_in_location(source)
        if len(items) != 1:
            return {
                "action": "unknown",
                "message": "Select the exact item to move, or tell me its name.",
                "results": [],
                "plan": [],
            }
        item = items[0]
        dest = _correction_destination(text, item.name, source)
        if dest is None:
            return {
                "action": "unknown",
                "message": "I could not find a better location to move it to.",
                "results": [],
                "plan": [],
            }
        moved = store.move_inventory(
            item_id=item.item_id,
            from_bin=source,
            to_bin=dest,
            normalize_bin=False,
        )
        if not moved:
            return None
        path = _path_for(dest)
        dest_lit = locations.nearest_lit(dest)
        lit = _light_pixels(
            dest_lit.light_indexes if dest_lit is not None else [],
            target=str(dest_lit.light_target or "") if dest_lit else "",
        )
        return {
            "action": "moved",
            "message": f"Moved {item.name} to {' › '.join(str(p['label']) for p in path)}.",
            "item": item.as_dict(),
            "destination": {"id": dest, "path": path},
            "from": source,
            "lit": lit,
            "plan": [],
        }

    def _correction_destination(text: str, item_name: str, current_location_id: str) -> str | None:
        lowered = text.lower()
        needed = infer_item_capacity(f"{item_name} {text}")
        if "printer cabinet" in lowered or "printercab" in lowered:
            dest = _best_child_location("printercab", needed, exclude=current_location_id)
            if dest:
                return dest
        suggestions = suggest_locations(
            f"{item_name} {text}",
            store=store,
            locations=locations,
            limit=8,
        )
        for suggestion in suggestions:
            if suggestion.location.id != current_location_id:
                return suggestion.location.id
        return _best_child_location("printercab", needed, exclude=current_location_id)

    def _best_child_location(parent_id: str, needed_capacity: str, *, exclude: str | None = None) -> str | None:
        parent = _existing_location_id(parent_id)
        if parent is None:
            return None
        needed_rank = CAPACITY_RANK.get(needed_capacity, CAPACITY_RANK["small"])
        item_counts = store.location_item_counts()
        candidates = [
            child
            for child in locations.children(parent)
            if child.id != exclude
            and CAPACITY_RANK.get(child.capacity_class, CAPACITY_RANK["medium"]) >= needed_rank
        ]
        if not candidates:
            candidates = [child for child in locations.children(parent) if child.id != exclude]
        if not candidates:
            return parent
        candidates.sort(
            key=lambda loc: (
                item_counts.get(loc.id, 0),
                -CAPACITY_RANK.get(loc.capacity_class, CAPACITY_RANK["medium"]),
                loc.label.lower(),
            )
        )
        return candidates[0].id

    def _best_descendant_destination(
        parent_id: str,
        item_text: str,
        needed_capacity: str,
        item_dimensions: dict[str, object] | None,
    ) -> dict[str, object] | None:
        descendant_ids = set(locations.subtree_ids(parent_id))
        descendant_ids.discard(parent_id)
        if not descendant_ids:
            return None
        for suggestion in suggest_locations(
            item_text,
            store=store,
            locations=locations,
            limit=100,
            item_dimensions=item_dimensions,
        ):
            if (
                suggestion.location.id in descendant_ids
                and _placement_candidate_allowed(suggestion.location.id)
                and _location_can_fit(
                    suggestion.location.id,
                    needed_capacity,
                    item_dimensions,
                )
            ):
                return {
                    "location_id": suggestion.location.id,
                    "score": suggestion.score,
                    "reasons": [
                        f"inside {locations.get(parent_id).label}",
                        *suggestion.reasons,
                    ],
                }
        item_counts = store.location_item_counts()
        candidates = [
            location
            for location_id in descendant_ids
            if (location := locations.get(location_id)) is not None
            and location.accepts_items
            and item_counts.get(location.id, 0) == 0
            and _location_can_fit(location.id, needed_capacity, item_dimensions)
        ]
        candidates.sort(
            key=lambda location: (
                CAPACITY_RANK.get(
                    location.capacity_class,
                    CAPACITY_RANK["medium"],
                ),
                location.label.casefold(),
                location.id,
            )
        )
        if not candidates:
            return None
        return {
            "location_id": candidates[0].id,
            "score": None,
            "reasons": [
                f"first available place inside {locations.get(parent_id).label}"
            ],
        }

    def _put_away_destination(
        corrected: str,
        original_text: str,
        *,
        current_location_id: str | None = None,
        needed_capacity: str | None = None,
        item_dimensions: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        needed = needed_capacity or item_capacity_for(
            corrected,
            store=store,
            context_text=original_text,
        )[0]
        current = _existing_location_id(current_location_id) if current_location_id else None
        if current and _location_can_fit(current, needed, item_dimensions):
            return {
                "location_id": current,
                "score": None,
                "reasons": ["using the selected location because it fits"],
            }
        if current and locations.get(current) is not None:
            scoped = _best_descendant_destination(
                current,
                f"{corrected} {original_text}",
                needed,
                item_dimensions,
            )
            if scoped is not None:
                return scoped
        learned = store.suggest_home(corrected)
        if learned and _location_can_fit(learned, needed, item_dimensions):
            return {
                "location_id": learned,
                "score": None,
                "reasons": ["learned home for this exact item"],
            }
        if learned and (learned_location := locations.get(learned)) is not None:
            scoped = _best_descendant_destination(
                learned_location.id,
                f"{corrected} {original_text}",
                needed,
                item_dimensions,
            )
            if scoped is not None:
                scoped["reasons"] = [
                    "inside the learned home for this exact item",
                    *list(scoped.get("reasons", [])),
                ]
                return scoped
        for suggestion in suggest_locations(
            f"{corrected} {original_text}",
            store=store,
            locations=locations,
            limit=8,
            item_dimensions=item_dimensions,
        ):
            if _placement_candidate_allowed(suggestion.location.id) and _location_can_fit(
                suggestion.location.id,
                needed,
                item_dimensions,
            ):
                return {
                    "location_id": suggestion.location.id,
                    "score": suggestion.score,
                    "reasons": list(suggestion.reasons),
                }
        item_counts = store.location_item_counts()
        empty_graph_locations = [
            location
            for location in locations.all()
            if location.accepts_items
            and item_counts.get(location.id, 0) == 0
            and _location_can_fit(
                location.id,
                needed,
                item_dimensions,
            )
        ]
        if len(empty_graph_locations) == 1:
            return {
                "location_id": empty_graph_locations[0].id,
                "score": None,
                "reasons": ["only empty storage location that fits"],
            }
        min_size = needed if needed in {"medium", "large"} else "small"
        fallback = next(
            (
                bin.id
                for bin in layout.bins
                if bin.id not in store.occupied_bin_ids()
                and CAPACITY_RANK.get(bin.size, CAPACITY_RANK["small"])
                >= CAPACITY_RANK.get(min_size, CAPACITY_RANK["small"])
                and _location_can_fit(bin.id, needed, item_dimensions)
            ),
            None,
        )
        if fallback is None:
            return None
        return {
            "location_id": fallback,
            "score": None,
            "reasons": [f"first empty {min_size} bin"],
        }

    def _placement_candidate_allowed(location_id: str) -> bool:
        loc = locations.get(location_id)
        if loc is None:
            return True
        if re.fullmatch(r"B\d{3}", loc.id) and store.location_item_counts().get(loc.id, 0):
            return False
        return True

    def _location_can_fit(
        location_id: str,
        needed_capacity: str,
        item_dimensions: dict[str, object] | None = None,
    ) -> bool:
        loc = locations.get(location_id)
        if loc is None:
            normalized = normalize_bin_id(location_id)
            loc = locations.get(normalized)
        if loc is None:
            return needed_capacity in {"tiny", "small"}
        if not loc.accepts_items:
            return False
        measured_fit, _reason = physical_fit(item_dimensions, loc.internal_dimensions)
        if measured_fit is False:
            return False
        return CAPACITY_RANK.get(loc.capacity_class, CAPACITY_RANK["medium"]) >= CAPACITY_RANK.get(
            needed_capacity,
            CAPACITY_RANK["small"],
        )

    @app.post("/feedback/correction")
    def feedback_correction(request: FeedbackCorrectionRequest) -> dict[str, object]:
        """Self-heal: the user says "it's actually this item". The query becomes a
        learned alias so the same search resolves directly next time."""
        store.learn_alias(item_id=request.item_id, alias=request.query)
        store.record_example(
            command=request.query,
            action=json.dumps({"correct_item_id": request.item_id}),
            kind="correction",
        )
        return {"ok": True, "learned_alias": request.query}

    def _location_summary(
        loc,
        *,
        child_counts,
        inventory_metrics,
        verification_events,
        verification_coverage_complete,
        bindings_by_location,
        photos_by_location,
    ) -> dict[str, object]:
        verification = _verification_from_event(
            loc.id,
            verification_events.get(loc.id),
        )
        location_metrics = inventory_metrics.get(loc.id, {})
        item_count = int(location_metrics.get("item_count", 0))
        review_count = int(location_metrics.get("review_count", 0))
        content_revision = int(location_metrics.get("content_revision", 0))
        return {
            "id": loc.id,
            "type": loc.type,
            "label": loc.label,
            "parent_id": loc.parent_id,
            "is_lit": loc.is_lit,
            "grid": loc.grid,
            "col": loc.col,
            "row": loc.row,
            "w": loc.w,
            "h": loc.h,
            "light_target": loc.light_target,
            "light_indexes": loc.light_indexes,
            "hardware_bindings": [
                binding.as_dict()
                for binding in bindings_by_location.get(loc.id, [])
            ],
            "capacity_class": loc.capacity_class,
            "purpose_tags": loc.purpose_tags,
            "placement_notes": loc.placement_notes,
            "internal_dimensions": loc.internal_dimensions,
            "accepts_items": loc.accepts_items,
            "active": loc.active,
            "archived_at": loc.archived_at,
            "photos": [
                photo.as_dict() for photo in photos_by_location.get(loc.id, [])
            ],
            "child_count": child_counts.get(loc.id, 0),
            "item_count": item_count,
            "review_count": review_count,
            "verification": verification,
            "confidence": _location_confidence(
                item_count=item_count,
                review_count=review_count,
                content_revision=content_revision,
                verification=verification,
                verification_coverage_complete=verification_coverage_complete,
            ),
        }

    @app.post("/placement/suggest")
    def placement_suggest(request: PlacementSuggestRequest) -> dict[str, object]:
        return {
            "ok": True,
            "suggestions": [
                suggestion.as_dict()
                for suggestion in suggest_locations(
                    request.item,
                    store=store,
                    locations=locations,
                    limit=request.limit,
                    item_dimensions=request.dimensions,
                )
            ],
        }

    @app.post("/placement/size")
    def placement_size(request: PlacementSuggestRequest) -> dict[str, object]:
        measured_size = measured_capacity_for(
            request.dimensions,
            locations=locations,
        )
        size_class, reason = measured_size or item_capacity_for(
            request.item,
            store=store,
        )
        return {
            "ok": True,
            "item": request.item,
            "size_class": size_class,
            "reason": reason,
        }

    def _rule_management_payload() -> dict[str, object]:
        all_locations = locations.all(include_archived=True)
        location_by_id = {location.id: location for location in all_locations}

        def location_path(location_id: str) -> list[dict[str, str]]:
            path: list[dict[str, str]] = []
            seen: set[str] = set()
            current = location_by_id.get(location_id)
            while current is not None and current.id not in seen:
                path.append({"id": current.id, "label": current.label or current.id})
                seen.add(current.id)
                current = (
                    location_by_id.get(current.parent_id)
                    if current.parent_id
                    else None
                )
            return list(reversed(path))

        placement_rules = []
        for rule in store.rules(active_only=False, limit=500):
            target = location_by_id.get(rule.target_location_id)
            path = location_path(rule.target_location_id)
            target_active = bool(target and target.active)
            placement_rules.append(
                {
                    "rule_type": "placement",
                    **rule.as_dict(),
                    "target_path": path,
                    "target_path_label": " > ".join(
                        node["label"] for node in path
                    ),
                    "target_active": target_active,
                    "target_archived": bool(target and not target.active),
                    "target_missing": target is None,
                    "target_issue": (
                        ""
                        if target_active
                        else "archived_target"
                        if target is not None
                        else "missing_target"
                    ),
                }
            )
        return {
            "ok": True,
            "placement_rules": placement_rules,
            "size_rules": [
                {"rule_type": "size", **rule.as_dict()}
                for rule in store.size_rules(active_only=False, limit=500)
            ],
            "conflicts": store.rule_conflicts(limit=100),
        }

    def _review_rule_issues(*, limit: int = 50) -> tuple[list[dict[str, object]], bool]:
        clean_limit = max(1, min(int(limit), 100))
        placement_rules = store.rules(active_only=True, limit=101)
        size_rules = store.size_rules(active_only=True, limit=101)
        location_by_id = {
            location.id: location
            for location in locations.all(include_archived=True)
        }
        issues: list[dict[str, object]] = []
        for rule in placement_rules[:100]:
            target = location_by_id.get(rule.target_location_id)
            if target is not None and target.active:
                continue
            issues.append(
                {
                    "key": f"target-{rule.id}",
                    "title": rule.rule_text or f"Placement rule {rule.id}",
                    "detail": (
                        "This rule points to an archived place."
                        if target is not None
                        else "This rule points to a place that no longer exists."
                    ),
                }
            )
        conflicts = _placement_rule_conflicts(placement_rules[:100], limit=100)
        if len(conflicts) < 100:
            conflicts.extend(
                _size_rule_conflicts(size_rules[:100], limit=100 - len(conflicts))
            )
        for index, conflict in enumerate(conflicts):
            rule_type = "size" if conflict.get("rule_type") == "size" else "placement"
            shared_terms = conflict.get("shared_terms") or []
            issues.append(
                {
                    "key": f"conflict-{rule_type}-{index}",
                    "title": f"Conflicting {rule_type} rules",
                    "detail": (
                        f"Rules {' and '.join(str(value) for value in conflict.get('rule_ids') or [])} overlap"
                        + (f" on {', '.join(str(value) for value in shared_terms)}" if shared_terms else "")
                        + "."
                    ),
                }
            )
        truncated_source = len(placement_rules) > 100 or len(size_rules) > 100
        return issues[:clean_limit], truncated_source or len(issues) > clean_limit

    def _review_place_checks(*, limit: int = 50) -> tuple[list[dict[str, object]], bool]:
        clean_limit = max(1, min(int(limit), 50))
        active_locations = locations.all(include_archived=False)
        location_by_id = {location.id: location for location in active_locations}
        inventory_metrics = store.location_inventory_metrics()
        verification_events, verification_truncated = (
            locations.latest_events_by_location("location_contents_verified")
        )
        verification_events = _expanded_verification_events(verification_events)

        def path_for(location_id: str) -> list[dict[str, str]]:
            path: list[dict[str, str]] = []
            seen: set[str] = set()
            current = location_by_id.get(location_id)
            while current is not None and current.id not in seen:
                path.append({"id": current.id, "label": current.label or current.id})
                seen.add(current.id)
                current = (
                    location_by_id.get(current.parent_id)
                    if current.parent_id
                    else None
                )
            return list(reversed(path))

        priority = {
            "attention": 0,
            "changed": 1,
            "stale": 2,
            "unchecked": 3,
            "unknown": 4,
        }
        candidates: list[dict[str, object]] = []
        for location in active_locations:
            location_metrics = inventory_metrics.get(location.id, {})
            item_count = int(location_metrics.get("item_count", 0))
            if item_count <= 0:
                continue
            review_count = int(location_metrics.get("review_count", 0))
            content_revision = int(location_metrics.get("content_revision", 0))
            verification = _verification_from_event(
                location.id,
                verification_events.get(location.id),
            )
            confidence = _location_confidence(
                item_count=item_count,
                review_count=review_count,
                content_revision=content_revision,
                verification=verification,
                verification_coverage_complete=not verification_truncated,
            )
            state = str(confidence["state"])
            if state not in priority:
                continue
            path = path_for(location.id)
            candidates.append(
                {
                    "location_id": location.id,
                    "label": location.label or location.id,
                    "type": location.type,
                    "path": path,
                    "path_label": " › ".join(node["label"] for node in path),
                    "item_count": item_count,
                    "review_count": review_count,
                    "confidence": confidence,
                    "action_label": (
                        "Recheck" if state in {"changed", "stale"} else "Check contents"
                    ),
                }
            )
        candidates.sort(
            key=lambda row: (
                priority[str(row["confidence"]["state"])],
                str(row["path_label"]).casefold(),
                str(row["location_id"]),
            )
        )
        return candidates[:clean_limit], len(candidates) > clean_limit

    @app.get("/review/queue")
    def review_queue_endpoint(limit: int = 50) -> dict[str, object]:
        clean_limit = max(1, min(int(limit), 50))
        inventory = review_inventory(limit=clean_limit)
        learning = _learning_examples_payload(
            limit=clean_limit,
            review_status="pending",
        )
        rule_issues, more_rules = _review_rule_issues(limit=clean_limit)
        place_checks, more_place_checks = _review_place_checks(limit=clean_limit)
        return {
            "ok": True,
            "limit": clean_limit,
            "items": inventory["items"],
            "learning_examples": learning["examples"],
            "rule_issues": rule_issues,
            "place_checks": place_checks,
            "has_more": {
                "items": bool(inventory["has_more"]),
                "learning_examples": bool(learning["has_more"]),
                "rule_issues": more_rules,
                "place_checks": more_place_checks,
            },
        }

    @app.get("/rules")
    def list_rules_endpoint() -> dict[str, object]:
        return _rule_management_payload()

    @app.patch("/rules/{rule_type}/{rule_id}")
    def update_rule_endpoint(
        rule_type: str,
        rule_id: int,
        request: RuleUpdateRequest,
    ) -> dict[str, object]:
        try:
            if rule_type == "placement":
                previous = store.rule(rule_id)
                if previous is None:
                    raise HTTPException(status_code=404, detail="active placement rule not found")
                target_location_id = str(request.target_location_id or "").strip()
                if not target_location_id or locations.get(target_location_id) is None:
                    raise HTTPException(status_code=400, detail="choose an active target location")
                updated = store.update_rule(
                    rule_id=rule_id,
                    rule_text=request.rule_text,
                    trigger_terms=request.trigger_terms,
                    target_location_id=target_location_id,
                    source_command=previous.source_command,
                    confidence=previous.confidence,
                )
                applied = {
                    "updated_rule": rule_id,
                    "previous_rule_text": previous.rule_text,
                    "previous_trigger_terms": previous.trigger_terms,
                    "previous_target_location_id": previous.target_location_id,
                    "previous_source_command": previous.source_command,
                    "previous_confidence": previous.confidence,
                }
            elif rule_type == "size":
                previous = store.size_rule(rule_id)
                if previous is None:
                    raise HTTPException(status_code=404, detail="active size rule not found")
                updated = store.update_size_rule(
                    rule_id=rule_id,
                    rule_text=request.rule_text,
                    trigger_terms=request.trigger_terms,
                    size_class=str(request.size_class or ""),
                    source_command=previous.source_command,
                    confidence=previous.confidence,
                )
                applied = {
                    "updated_size_rule": rule_id,
                    "previous_rule_text": previous.rule_text,
                    "previous_trigger_terms": previous.trigger_terms,
                    "previous_size_class": previous.size_class,
                    "previous_source_command": previous.source_command,
                    "previous_confidence": previous.confidence,
                }
            else:
                raise HTTPException(
                    status_code=400, detail="rule_type must be placement or size"
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="active rule not found")
        response = {
            "ok": True,
            "action": "manual_update_rule",
            "applied": [applied],
        }
        response["interaction_id"] = store.record_assistant_interaction(
            route="/rules/update",
            command=f"manual update {rule_type} rule {rule_id}",
            response=response,
            status="manual",
        )
        return response

    @app.post("/rules/{rule_type}/{rule_id}/{action}")
    def set_rule_status_endpoint(
        rule_type: str,
        rule_id: int,
        action: str,
    ) -> dict[str, object]:
        if rule_type not in {"placement", "size"}:
            raise HTTPException(
                status_code=400, detail="rule_type must be placement or size"
            )
        if action not in {"disable", "restore"}:
            raise HTTPException(status_code=400, detail="action must be disable or restore")
        current = (
            store.rule(rule_id, active_only=False)
            if rule_type == "placement"
            else store.size_rule(rule_id, active_only=False)
        )
        if current is None:
            raise HTTPException(status_code=404, detail="rule not found")
        active = action == "restore"
        if current.active == active:
            raise HTTPException(
                status_code=409,
                detail=f"rule is already {'active' if active else 'disabled'}",
            )
        if rule_type == "placement" and active:
            target_location_id = str(current.target_location_id or "").strip()
            if not target_location_id or locations.get(target_location_id) is None:
                raise HTTPException(
                    status_code=400,
                    detail="choose an active target location before restoring this rule",
                )
        try:
            changed = store.set_rule_active(
                rule_id=rule_id,
                rule_type=rule_type,
                active=active,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            raise HTTPException(status_code=404, detail="rule not found")
        applied_key = "reactivated_rule" if active else "disabled_rule"
        response = {
            "ok": True,
            "action": f"manual_{action}_rule",
            "applied": [{applied_key: rule_id, "rule_type": rule_type}],
        }
        response["interaction_id"] = store.record_assistant_interaction(
            route=f"/rules/{action}",
            command=f"manual {action} {rule_type} rule {rule_id}",
            response=response,
            status="manual",
        )
        return response

    def _normalized_location_name(value: object) -> str:
        return " ".join(str(value or "").split()).casefold()

    def _matching_sibling_locations(
        *,
        label: object,
        parent_id: str | None,
        exclude_id: str = "",
        active_only: bool = True,
    ) -> list[Location]:
        normalized = _normalized_location_name(label)
        if not normalized:
            return []
        return [
            location
            for location in locations.all(include_archived=not active_only)
            if location.id != exclude_id
            and location.parent_id == parent_id
            and _normalized_location_name(location.label or location.id) == normalized
        ]

    def _require_unique_sibling_name(
        *,
        label: object,
        parent_id: str | None,
        exclude_id: str = "",
        active_only: bool = True,
    ) -> None:
        matches = _matching_sibling_locations(
            label=label,
            parent_id=parent_id,
            exclude_id=exclude_id,
            active_only=active_only,
        )
        if not matches:
            return
        clean_label = " ".join(str(label or "").split())
        parent = locations.get_any(parent_id) if parent_id else None
        level = (
            f'inside "{parent.label or parent.id}"'
            if parent is not None
            else "at the top level"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f'A place named "{clean_label}" already exists {level}. '
                "Rename it or use the existing place."
            ),
        )

    @app.get("/locations")
    def list_locations_tree(include_archived: bool = False) -> dict[str, object]:
        all_locs = locations.all(include_archived=include_archived)
        inventory_metrics = store.location_inventory_metrics()
        verification_events, verification_truncated = (
            locations.latest_events_by_location("location_contents_verified")
        )
        verification_events = _expanded_verification_events(verification_events)
        location_ids = [loc.id for loc in all_locs]
        bindings_by_location = locations.hardware_bindings_by_location(location_ids)
        photos_by_location = store.location_photos_by_location(location_ids)
        child_counts: dict[str, int] = {}
        for loc in all_locs:
            if loc.parent_id:
                child_counts[loc.parent_id] = child_counts.get(loc.parent_id, 0) + 1
        return {
            "roots": [loc.id for loc in all_locs if loc.parent_id is None],
            "locations": [
                _location_summary(
                    loc,
                    child_counts=child_counts,
                    inventory_metrics=inventory_metrics,
                    verification_events=verification_events,
                    verification_coverage_complete=not verification_truncated,
                    bindings_by_location=bindings_by_location,
                    photos_by_location=photos_by_location,
                )
                for loc in all_locs
            ],
        }

    @app.post("/storage/setup")
    def setup_storage_endpoint(request: StorageSetupRequest) -> dict[str, object]:
        root_name = " ".join(request.root_name.split())
        if _matching_sibling_locations(label=root_name, parent_id=None):
            raise HTTPException(
                status_code=409,
                detail="a top-level storage place with that name already exists",
            )
        child_rows = [
            {
                "label": " ".join(child.label.split()),
                "type": child.type.strip().lower(),
            }
            for child in request.children
        ]
        destination_row = (
            {
                "label": " ".join(request.destination.label.split()),
                "type": request.destination.type.strip().lower(),
                "parent_label": " ".join(
                    (request.destination.parent_label or "").split()
                ),
                "capacity_class": (
                    request.destination.capacity_class.strip().lower()
                ),
            }
            if request.destination is not None
            else None
        )
        if not root_name or any(not str(row["label"]) for row in child_rows):
            raise HTTPException(
                status_code=400,
                detail="storage place names cannot be blank",
            )
        if destination_row is not None and not destination_row["label"]:
            raise HTTPException(
                status_code=400,
                detail="first container name cannot be blank",
            )
        normalized_labels = [str(row["label"]).casefold() for row in child_rows]
        if len(normalized_labels) != len(set(normalized_labels)):
            raise HTTPException(
                status_code=400,
                detail="starter place names must be unique",
            )
        child_labels = {str(row["label"]).casefold() for row in child_rows}
        destination_parent_label = (
            str(destination_row["parent_label"]).casefold()
            if destination_row is not None and destination_row["parent_label"]
            else ""
        )
        if destination_parent_label and destination_parent_label not in child_labels:
            raise HTTPException(
                status_code=400,
                detail="first container parent must be one of the starter places",
            )
        if (
            destination_row is not None
            and not destination_parent_label
            and str(destination_row["label"]).casefold() in child_labels
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "first container name conflicts with a starter place "
                    "at the same level"
                ),
            )
        allowed_types = set(LOCATION_TYPES)
        root_type = request.root_type.strip().lower()
        if (
            root_type not in allowed_types
            or any(row["type"] not in allowed_types for row in child_rows)
            or (
                destination_row is not None
                and destination_row["type"] not in allowed_types
            )
        ):
            raise HTTPException(status_code=400, detail="unknown storage place type")

        root_id = _available_location_id(root_name)
        root = Location(
            id=root_id,
            type=root_type,
            parent_id=None,
            label=root_name,
            accepts_items=default_accepts_items(root_type),
        )
        created: list[Location] = []
        destination: Location | None = None
        try:
            created.append(root)
            reserved_ids = {root.id}
            for row in child_rows:
                label = str(row["label"])
                child = Location(
                    id=_available_location_id(
                        f"{root_id}-{label}", reserved=reserved_ids
                    ),
                    type=str(row["type"]),
                    parent_id=root_id,
                    label=label,
                    accepts_items=default_accepts_items(str(row["type"])),
                )
                created.append(child)
                reserved_ids.add(child.id)
            if destination_row is not None:
                parent = root
                if destination_parent_label:
                    parent = next(
                        child
                        for child in created[1:]
                        if child.label.casefold() == destination_parent_label
                    )
                destination = Location(
                    id=_available_location_id(
                        f"{parent.id}-{destination_row['label']}",
                        reserved=reserved_ids,
                    ),
                    type=str(destination_row["type"]),
                    parent_id=parent.id,
                    label=str(destination_row["label"]),
                    capacity_class=str(destination_row["capacity_class"]),
                    accepts_items=True,
                )
                created.append(destination)
                reserved_ids.add(destination.id)

            locations.create_many(created)
            locations.record_event(
                event_type="location_created",
                location_id=root_id,
                note="storage space setup",
            )
            for child in created[1:]:
                locations.record_event(
                    event_type="location_created",
                    location_id=child.id,
                    note=(
                        f"storage space container in {child.parent_id}"
                        if destination is not None and child.id == destination.id
                        else f"storage space setup in {root_id}"
                    ),
                )
            active_root_count = sum(
                1
                for location in locations.all()
                if location.parent_id is None
            )
            if active_root_count >= 2:
                _record_workspace_management_event(
                    "visual_multi_root_setup",
                    location_id=root_id,
                    note=f"visual setup now manages {active_root_count} storage spaces",
                )

            response = {
                "ok": True,
                "root": root.as_dict(),
                "children": [
                    child.as_dict()
                    for child in created[1:]
                    if destination is None or child.id != destination.id
                ],
                "destination": destination.as_dict() if destination else None,
                "created": [location.as_dict() for location in created],
                "action": "manual_storage_setup",
                "applied": [
                    {"created_location": location.id} for location in created
                ],
            }
            response["interaction_id"] = store.record_assistant_interaction(
                route="/storage/setup",
                command=f"storage space setup {root_name}",
                response=response,
                status="manual",
            )
            return response
        except Exception:
            if locations.get_any(root_id) is not None:
                locations.delete_subtree(root_id)
            raise

    @app.post("/locations")
    def create_location_endpoint(request: LocationCreateRequest) -> dict[str, object]:
        if request.parent_id and locations.get(request.parent_id) is None:
            raise HTTPException(status_code=400, detail="unknown parent location")
        clean_label = " ".join(request.label.split())
        try:
            location_type = normalize_location_type(request.type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        requested_id = slugify(request.id or request.label)
        if not requested_id or not clean_label:
            raise HTTPException(status_code=400, detail="location label is required")
        _require_unique_sibling_name(
            label=clean_label,
            parent_id=request.parent_id,
        )
        loc_id = _available_location_id(requested_id)
        grid = {"cols": request.cols, "rows": request.rows} if request.cols else None
        locations.upsert(
            Location(
                id=loc_id,
                type=location_type,
                parent_id=request.parent_id,
                label=clean_label,
                grid=grid,
                col=request.col,
                row=request.row,
                w=request.w,
                h=request.h,
                capacity_class=request.capacity_class,
                purpose_tags=request.purpose_tags,
                placement_notes=request.placement_notes,
                internal_dimensions=normalize_internal_dimensions(
                    request.internal_dimensions
                ),
                accepts_items=(
                    request.accepts_items
                    if request.accepts_items is not None
                    else default_accepts_items(location_type)
                ),
            )
        )
        locations.record_event(
            event_type="location_created",
            location_id=loc_id,
            note=f"{location_type} in {request.parent_id or ''}".strip(),
        )
        response = {
            "ok": True,
            "id": loc_id,
            "location": locations.get(loc_id).as_dict(),
            "action": "manual_create_location",
            "applied": [{"created_location": loc_id}],
        }
        interaction_id = store.record_assistant_interaction(
            route="/locations/create",
            command=f"manual create location {loc_id}",
            response=response,
            status="manual",
        )
        response["interaction_id"] = interaction_id
        return response

    def _available_location_id(
        raw: str, *, reserved: set[str] | None = None
    ) -> str:
        base = slugify(raw) or "location"
        unavailable = reserved or set()
        if base not in unavailable and locations.get_any(base) is None:
            return base
        suffix = 2
        while (
            f"{base}-{suffix}" in unavailable
            or locations.get_any(f"{base}-{suffix}") is not None
        ):
            suffix += 1
        return f"{base}-{suffix}"

    @app.post("/locations/structure")
    def create_location_structure(
        request: LocationStructureCreateRequest,
    ) -> dict[str, object]:
        allowed_types = set(LOCATION_TYPES)
        parent_id = request.parent_id.strip() if request.parent_id else None
        if parent_id and locations.get(parent_id) is None:
            raise HTTPException(status_code=400, detail="unknown parent location")
        label = " ".join(request.label.split())
        child_prefix = " ".join(request.child_label_prefix.split())
        location_type = request.type.strip().lower()
        child_type = request.child_type.strip().lower()
        if not label or not child_prefix:
            raise HTTPException(
                status_code=400,
                detail="structure and inside place names cannot be blank",
            )
        if len(f"{child_prefix} {request.child_count}") > 120:
            raise HTTPException(
                status_code=400,
                detail="inside place names must be 120 characters or fewer",
            )
        if location_type not in allowed_types or child_type not in allowed_types:
            raise HTTPException(status_code=400, detail="unknown storage place type")
        _require_unique_sibling_name(label=label, parent_id=parent_id)

        structure_id = _available_location_id(
            f"{parent_id}-{label}" if parent_id else label
        )
        structure = Location(
            id=structure_id,
            type=location_type,
            parent_id=parent_id,
            label=label,
            capacity_class=request.capacity_class.strip().lower(),
            accepts_items=(
                request.accepts_items
                if request.accepts_items is not None
                else default_accepts_items(location_type)
            ),
        )
        created: list[Location] = []
        try:
            created.append(structure)
            reserved_ids = {structure.id}
            for index in range(1, request.child_count + 1):
                child_label = f"{child_prefix} {index}"
                child = Location(
                    id=_available_location_id(
                        f"{structure.id}-{child_label}", reserved=reserved_ids
                    ),
                    type=child_type,
                    parent_id=structure.id,
                    label=child_label,
                    capacity_class=request.child_capacity_class.strip().lower(),
                    accepts_items=(
                        request.child_accepts_items
                        if request.child_accepts_items is not None
                        else default_accepts_items(child_type)
                    ),
                )
                created.append(child)
                reserved_ids.add(child.id)
            locations.create_many(created)
            locations.record_event(
                event_type="location_created",
                location_id=structure.id,
                note=f"structure in {parent_id or 'top level'}",
            )
            for child in created[1:]:
                locations.record_event(
                    event_type="location_created",
                    location_id=child.id,
                    note=f"structure child in {structure.id}",
                )
            locations.record_event(
                event_type="location_structure_created",
                location_id=structure.id,
                quantity=float(request.child_count),
                note=f"{request.child_count} {child_type}",
            )
            response = {
                "ok": True,
                "structure": structure.as_dict(),
                "children": [child.as_dict() for child in created[1:]],
                "created": [location.as_dict() for location in created],
                "action": "manual_create_location_structure",
                "applied": [
                    {"created_location": location.id} for location in created
                ],
            }
            response["interaction_id"] = store.record_assistant_interaction(
                route="/locations/create-structure",
                command=(
                    f"manual create {label} with "
                    f"{request.child_count} {child_type}"
                ),
                response=response,
                status="manual",
            )
            return response
        except Exception:
            if locations.get_any(structure_id) is not None:
                locations.delete_subtree(structure_id)
            raise

    @app.post("/locations/{location_id}/children/batch")
    def create_location_children_batch(
        location_id: str,
        request: LocationBatchCreateRequest,
    ) -> dict[str, object]:
        if locations.get(location_id) is None:
            raise HTTPException(status_code=404, detail="unknown location")
        try:
            location_type = normalize_location_type(request.type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        internal_dimensions = normalize_internal_dimensions(request.internal_dimensions)
        labels = [" ".join(label.split()) for label in request.labels]
        if any(not label for label in labels):
            raise HTTPException(status_code=400, detail="place names cannot be empty")
        if any(len(label) > 120 for label in labels):
            raise HTTPException(
                status_code=400, detail="place names must be 120 characters or fewer"
            )
        normalized_labels = [label.casefold() for label in labels]
        if len(normalized_labels) != len(set(normalized_labels)):
            raise HTTPException(
                status_code=400, detail="place names in one batch must be unique"
            )
        if not labels:
            prefix = request.label_prefix.strip() or location_type.title()
            labels = [f"{prefix} {index}" for index in range(1, request.count + 1)]
        conflicts = [
            label
            for label in labels
            if _matching_sibling_locations(label=label, parent_id=location_id)
        ]
        if conflicts:
            names = ", ".join(f'"{label}"' for label in conflicts[:3])
            suffix = " and more" if len(conflicts) > 3 else ""
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{names}{suffix} already exist inside this place. "
                    "Rename them or use the existing places."
                ),
            )
        created_locations: list[Location] = []
        reserved_ids: set[str] = set()
        for label in labels:
            child_id = _available_location_id(
                f"{location_id}-{label}", reserved=reserved_ids
            )
            child = Location(
                id=child_id,
                type=location_type,
                parent_id=location_id,
                label=label,
                capacity_class=request.capacity_class,
                purpose_tags=request.purpose_tags,
                placement_notes=request.placement_notes,
                internal_dimensions=internal_dimensions,
                accepts_items=(
                    request.accepts_items
                    if request.accepts_items is not None
                    else default_accepts_items(location_type)
                ),
            )
            created_locations.append(child)
            reserved_ids.add(child.id)
        locations.create_many(created_locations)
        for child in created_locations:
            locations.record_event(
                event_type="location_created",
                location_id=child.id,
                note=f"batch {location_type} in {location_id}",
            )
        created = [child.as_dict() for child in created_locations]
        locations.record_event(
            event_type="location_children_created",
            location_id=location_id,
            quantity=float(len(created)),
            note=(
                f"named places x {len(created)}"
                if request.labels
                else f"{request.label_prefix.strip() or location_type.title()} x {len(created)}"
            ),
        )
        response = {
            "ok": True,
            "parent_id": location_id,
            "created": created,
            "action": "manual_create_location_batch",
            "applied": [
                {"created_location": str(child["id"])} for child in created
            ],
        }
        interaction_id = store.record_assistant_interaction(
            route="/locations/create-batch",
            command=f"manual create {len(created)} locations in {location_id}",
            response=response,
            status="manual",
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/locations/{location_id}/fill")
    def fill_location_grid(
        location_id: str, request: GridFillRequest
    ) -> dict[str, object]:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        try:
            cell_type = normalize_location_type(request.cell_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cols = request.cols or (loc.grid or {}).get("cols")
        rows = request.rows or (loc.grid or {}).get("rows")
        if not cols or not rows:
            raise HTTPException(status_code=400, detail="need cols and rows")
        if loc.grid is None or request.cols:
            locations.upsert(replace(loc, grid={"cols": int(cols), "rows": int(rows)}))
        created = generate_cells(
            locations, location_id, int(cols), int(rows),
            cell_type=cell_type, naming=request.naming,
        )
        locations.record_event(
            event_type="location_grid_filled",
            location_id=location_id,
            quantity=float(len(created)),
            note=f"{int(cols)}x{int(rows)} {cell_type}",
        )
        return {"ok": True, "created": len(created)}

    @app.post("/locations/{location_id}/update")
    def update_location(
        location_id: str, request: LocationUpdateRequest
    ) -> dict[str, object]:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        try:
            updated_type = (
                normalize_location_type(request.type)
                if request.type is not None
                else loc.type
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        parent_requested = "parent_id" in request.model_fields_set
        new_parent_id = (
            request.parent_id.strip() if request.parent_id else None
        )
        parent_changed = parent_requested and new_parent_id != loc.parent_id
        if parent_changed:
            if new_parent_id and locations.get(new_parent_id) is None:
                raise HTTPException(status_code=400, detail="unknown parent location")
            if new_parent_id == location_id or new_parent_id in locations.subtree_ids(
                location_id
            ):
                raise HTTPException(
                    status_code=400,
                    detail="a location cannot move inside itself or its descendants",
                )
        updated_label = (
            " ".join(request.label.split())
            if request.label is not None
            else loc.label
        )
        if not updated_label:
            raise HTTPException(status_code=400, detail="location label is required")
        _require_unique_sibling_name(
            label=updated_label,
            parent_id=new_parent_id if parent_changed else loc.parent_id,
            exclude_id=location_id,
        )
        grid = loc.grid
        if request.cols:
            grid = {
                "cols": request.cols,
                "rows": request.rows or (loc.grid or {}).get("rows") or request.cols,
            }
        elif request.cols == 0:
            grid = None
        light_target = loc.light_target
        if request.light_target is not None:
            light_target = request.light_target.strip() or None
        light_indexes = loc.light_indexes
        if request.light_indexes is not None:
            light_index_limit = _light_index_limit()
            invalid = [
                pixel
                for pixel in request.light_indexes
                if not 0 <= pixel < light_index_limit
            ]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"WLED pixels {invalid} are outside the configured range "
                        f"0..{light_index_limit - 1}"
                    ),
                )
            light_indexes = sorted(set(request.light_indexes))
        internal_dimensions = (
            normalize_internal_dimensions(request.internal_dimensions)
            if request.internal_dimensions is not None
            else loc.internal_dimensions
        )
        if (
            request.accepts_items is False
            and loc.accepts_items
            and store.location_item_counts().get(location_id, 0) > 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "move this place's direct items before marking it "
                    "as organizer-only"
                ),
            )
        moved = replace(loc, parent_id=new_parent_id) if parent_changed else loc
        updated = replace(
            moved,
            label=updated_label,
            type=updated_type,
            grid=grid,
            w=request.w if request.w else loc.w,
            h=request.h if request.h else loc.h,
            light_target=light_target,
            light_indexes=light_indexes,
            capacity_class=request.capacity_class or loc.capacity_class,
            purpose_tags=request.purpose_tags if request.purpose_tags is not None else loc.purpose_tags,
            placement_notes=(
                request.placement_notes
                if request.placement_notes is not None
                else loc.placement_notes
            ),
            internal_dimensions=internal_dimensions,
            accepts_items=(
                request.accepts_items
                if request.accepts_items is not None
                else loc.accepts_items
            ),
        )
        locations.upsert(updated)
        light_configuration_requested = bool(
            {"light_target", "light_indexes"} & request.model_fields_set
        )
        if light_configuration_requested:
            for binding in locations.hardware_bindings(location_id):
                if binding.kind == "wled_pixels" and binding.active:
                    locations.set_hardware_binding(
                        location_id=location_id,
                        kind=binding.kind,
                        controller_id=binding.controller_id,
                        address=binding.address,
                        metadata=binding.metadata,
                        active=False,
                    )
            if updated.light_target and updated.light_indexes:
                locations.set_hardware_binding(
                    location_id=location_id,
                    kind="wled_pixels",
                    controller_id=updated.light_target,
                    address=",".join(str(index) for index in updated.light_indexes),
                    metadata={"pixels": list(updated.light_indexes)},
                    active=True,
                )
            else:
                updated = replace(updated, light_indexes=[])
                locations.upsert(updated)
        locations.record_event(
            event_type="location_updated",
            location_id=location_id,
            note="metadata updated",
        )
        applied: list[dict[str, object]] = [
            {
                "updated_location": location_id,
                "label": updated.label,
                "type": updated.type,
                "capacity_class": updated.capacity_class,
                "purpose_tags": updated.purpose_tags,
                "placement_notes": updated.placement_notes,
                "accepts_items": updated.accepts_items,
                "previous_label": loc.label,
                "previous_type": loc.type,
                "previous_capacity_class": loc.capacity_class,
                "previous_purpose_tags": loc.purpose_tags,
                "previous_placement_notes": loc.placement_notes,
                "previous_accepts_items": loc.accepts_items,
                "previous_grid": loc.grid,
                "previous_col": loc.col,
                "previous_row": loc.row,
                "previous_w": loc.w,
                "previous_h": loc.h,
                "previous_light_target": loc.light_target,
                "previous_light_indexes": loc.light_indexes,
                "previous_internal_dimensions": loc.internal_dimensions,
            }
        ]
        if parent_changed:
            locations.record_event(
                event_type="location_moved",
                location_id=location_id,
                note=f"from {loc.parent_id or ''} to {new_parent_id or ''}".strip(),
            )
            applied.append(
                {
                    "moved_container": location_id,
                    "from_parent_id": loc.parent_id,
                    "to_parent_id": new_parent_id,
                    "location_id": location_id,
                }
            )
        response = {
            "ok": True,
            "id": location_id,
            "action": "manual_update_location",
            "applied": applied,
        }
        interaction_id = store.record_assistant_interaction(
            route="/locations/update",
            command=f"manual update location {location_id}",
            response=response,
            status="manual",
        )
        response["interaction_id"] = interaction_id
        return response

    def _verification_event_details(event: dict[str, object]) -> dict[str, object]:
        note = str(event.get("note") or "")
        if note.startswith("{"):
            try:
                parsed = json.loads(note)
                if (
                    isinstance(parsed, dict)
                    and parsed.get("schema") == "magpie.location_verification.v2"
                ):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {}

    def _expanded_verification_events(
        events: dict[str, dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        expanded = dict(events)
        for event in sorted(events.values(), key=lambda row: int(row.get("id") or 0)):
            details = _verification_event_details(event)
            covered_ids = details.get("location_ids")
            if not isinstance(covered_ids, list):
                continue
            for covered_id in covered_ids[:5000]:
                clean_id = str(covered_id or "").strip()
                if not clean_id:
                    continue
                current = expanded.get(clean_id)
                if current is None or int(event.get("id") or 0) > int(current.get("id") or 0):
                    expanded[clean_id] = event
        return expanded

    def _verification_from_event(
        location_id: str,
        event: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if event is None:
            return None
        quantity = event.get("quantity")
        note = str(event.get("note") or "")
        details = _verification_event_details(event)
        response = {
            "location_id": location_id,
            "verified_at": event.get("created_at"),
            "placement_count": int(float(quantity or 0)),
            "note": str(details.get("summary") or note),
        }
        for key in (
            "schema",
            "item_count",
            "location_count",
            "reviewed_count",
            "present_count",
            "missing_count",
            "revision",
            "root_location_id",
            "location_ids",
        ):
            if key in details:
                response[key] = details[key]
        root_location_id = str(details.get("root_location_id") or location_id)
        content_revisions = details.get("content_revisions")
        if isinstance(content_revisions, dict) and location_id in content_revisions:
            response["content_revision"] = int(content_revisions[location_id])
        location_summaries = details.get("location_summaries")
        if location_id != root_location_id and isinstance(location_summaries, dict):
            location_summary = location_summaries.get(location_id)
            if isinstance(location_summary, dict):
                for key in (
                    "placement_count",
                    "item_count",
                    "reviewed_count",
                    "present_count",
                    "missing_count",
                ):
                    if key in location_summary:
                        response[key] = location_summary[key]
                reviewed_count = int(location_summary.get("reviewed_count") or 0)
                missing_count = int(location_summary.get("missing_count") or 0)
                response["note"] = (
                    f"{reviewed_count} placement{'s' if reviewed_count != 1 else ''} reviewed"
                    + (f"; {missing_count} missing" if missing_count else "; all present")
                )
        return response

    def _location_verification(location_id: str) -> dict[str, object] | None:
        return _verification_from_event(
            location_id,
            locations.latest_event(
                "location_contents_verified",
                location_id=location_id,
            ),
        )

    def _location_confidence(
        *,
        item_count: int,
        review_count: int,
        content_revision: int,
        verification: dict[str, object] | None,
        verification_coverage_complete: bool,
    ) -> dict[str, object]:
        if review_count:
            state = "attention"
            label = f"{review_count} uncertain item record{'s' if review_count != 1 else ''}"
        elif item_count <= 0:
            state = "empty"
            label = "No item records"
        elif verification is None:
            state = "unchecked" if verification_coverage_complete else "unknown"
            label = (
                "Contents not checked"
                if verification_coverage_complete
                else "Check history unavailable"
            )
        else:
            state = "checked"
            label = "Contents checked"
            verified_revision = verification.get("content_revision")
            if (
                verified_revision is not None
                and int(verified_revision) != int(content_revision)
            ):
                state = "changed"
                label = "Contents changed since last check"
            raw_timestamp = str(verification.get("verified_at") or "").strip()
            if state == "checked" and raw_timestamp:
                try:
                    checked_at = datetime.fromisoformat(
                        raw_timestamp.replace("Z", "+00:00")
                    )
                    if checked_at.tzinfo is None:
                        checked_at = checked_at.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - checked_at > timedelta(days=90):
                        state = "stale"
                        label = "Contents check is over 90 days old"
                except ValueError:
                    state = "unknown"
                    label = "Check date unavailable"
        return {
            "state": state,
            "label": label,
            "item_count": item_count,
            "review_count": review_count,
            "content_revision": int(content_revision),
            "verified_at": verification.get("verified_at") if verification else None,
        }

    def _location_audit_snapshot(location_id: str) -> dict[str, object]:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        location_ids = locations.subtree_ids(location_id)
        inventory_metrics = store.location_inventory_metrics()
        rows, total = store.items_in_locations_page(
            location_ids,
            limit=100,
            offset=0,
        )
        can_verify = total <= 100
        projection = [
            {
                "item_id": int(row.item_id),
                "location_id": row.bin_id,
                "quantity": float(row.quantity),
                "unit": row.unit,
                "needs_review": bool(row.needs_review),
            }
            for row in rows
        ]
        revision = (
            hashlib.sha256(
                json.dumps(
                    projection,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if can_verify
            else ""
        )
        paths = {
            row.bin_id: _path_for(row.bin_id)
            for row in rows
        }
        return {
            "ok": True,
            "schema": "magpie.location_audit.v1",
            "location": loc.as_dict(),
            "location_id": location_id,
            "location_ids": location_ids,
            "location_count": len(location_ids),
            "content_revisions": {
                candidate: int(
                    inventory_metrics.get(candidate, {}).get("content_revision", 0)
                )
                for candidate in location_ids
            },
            "placement_count": total,
            "item_count": len({int(row.item_id) for row in rows}) if can_verify else None,
            "limit": 100,
            "can_verify": can_verify,
            "revision": revision,
            "placements": [
                {
                    **row.as_dict(),
                    "location_id": row.bin_id,
                    "location_path": paths[row.bin_id],
                    "location_path_label": " › ".join(
                        str(node.get("display_label") or node.get("label") or node.get("id") or "")
                        for node in paths[row.bin_id]
                        if isinstance(node, dict)
                    ) or row.bin_id,
                }
                for row in rows
            ],
        }

    @app.get("/locations/{location_id}/audit")
    def location_contents_audit(location_id: str) -> dict[str, object]:
        return _location_audit_snapshot(location_id)

    @app.post("/locations/{location_id}/verify")
    def verify_location_contents(
        location_id: str,
        request: LocationVerificationRequest,
    ) -> dict[str, object]:
        snapshot = _location_audit_snapshot(location_id)
        if not snapshot["can_verify"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "contents_check_too_large",
                    "message": (
                        "This place has more than 100 placements. Check a smaller "
                        "drawer, bin, shelf, or other child place."
                    ),
                    "placement_count": snapshot["placement_count"],
                    "limit": snapshot["limit"],
                },
            )
        if request.revision != snapshot["revision"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "contents_check_stale",
                    "message": "Inventory changed while this contents check was open. Reload the list and check it again.",
                },
            )
        expected = {
            (int(row["item_id"]), str(row["location_id"]))
            for row in snapshot["placements"]
        }
        submitted: dict[tuple[int, str], LocationAuditPlacementRequest] = {}
        for placement in request.placements:
            key = (placement.item_id, placement.location_id)
            if key in submitted:
                raise HTTPException(
                    status_code=400,
                    detail="contents check contains a duplicate placement",
                )
            submitted[key] = placement
        if set(submitted) != expected:
            raise HTTPException(
                status_code=400,
                detail="account for every expected placement before finishing the contents check",
            )

        root_label = str(snapshot["location"].get("label") or location_id)
        missing_reason = f"Reported missing during contents check of {root_label}."
        audit_rows: list[dict[str, object]] = []
        missing: list[dict[str, object]] = []
        location_summaries: dict[str, dict[str, object]] = {
            str(value): {
                "placement_count": 0,
                "item_ids": set(),
                "missing_count": 0,
            }
            for value in snapshot["location_ids"]
        }
        for row in snapshot["placements"]:
            key = (int(row["item_id"]), str(row["location_id"]))
            status = submitted[key].status
            audit_row = {
                "item_id": key[0],
                "location_id": key[1],
                "expected_quantity": float(row["quantity"]),
                "expected_unit": str(row["unit"]),
                "expected_needs_review": bool(row["needs_review"]),
                "status": status,
                "review_reason": missing_reason if status == "missing" else "",
            }
            audit_rows.append(audit_row)
            location_summary = location_summaries[key[1]]
            location_summary["placement_count"] = (
                int(location_summary["placement_count"]) + 1
            )
            item_ids = location_summary["item_ids"]
            assert isinstance(item_ids, set)
            item_ids.add(key[0])
            if status == "missing":
                missing.append({"item_id": key[0], "location_id": key[1]})
                location_summary["missing_count"] = (
                    int(location_summary["missing_count"]) + 1
                )

        placement_count = int(snapshot["placement_count"])
        item_count = int(snapshot["item_count"] or 0)
        missing_count = len(missing)
        serialized_location_summaries = {
            summary_location_id: {
                "placement_count": int(summary["placement_count"]),
                "item_count": len(summary["item_ids"]),
                "reviewed_count": int(summary["placement_count"]),
                "present_count": (
                    int(summary["placement_count"])
                    - int(summary["missing_count"])
                ),
                "missing_count": int(summary["missing_count"]),
            }
            for summary_location_id, summary in location_summaries.items()
        }
        details = {
            "schema": "magpie.location_verification.v2",
            "summary": (
                f"{placement_count} placement{'s' if placement_count != 1 else ''} reviewed"
                + (f"; {missing_count} missing" if missing_count else "; all present")
            ),
            "item_count": item_count,
            "location_count": int(snapshot["location_count"]),
            "root_location_id": location_id,
            "location_ids": [str(value) for value in snapshot["location_ids"]],
            "content_revisions": dict(snapshot["content_revisions"]),
            "location_summaries": serialized_location_summaries,
            "reviewed_count": placement_count,
            "present_count": placement_count - missing_count,
            "missing_count": missing_count,
            "revision": request.revision,
            "missing": missing,
        }
        try:
            changes = store.apply_location_audit(
                location_id=location_id,
                location_ids=[str(value) for value in snapshot["location_ids"]],
                placements=audit_rows,
                verification_note=json.dumps(details, sort_keys=True, separators=(",", ":")),
            )
        except InventoryAuditConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "contents_check_stale",
                    "message": "Inventory changed while this contents check was open. Reload the list and check it again.",
                },
            ) from exc
        verification = _location_verification(location_id)
        assert verification is not None
        verification.update(
            {
                "item_count": item_count,
                "location_count": int(snapshot["location_count"]),
                "includes_nested": True,
            }
        )
        return {
            "ok": True,
            "schema": "magpie.location_verification.v2",
            "location": snapshot["location"],
            "verification": verification,
            "changes": changes,
            "missing_count": missing_count,
            "resolved_review_count": sum(
                1
                for change in changes
                if change.get("status") == "present"
                and change.get("previous_needs_review")
            ),
        }

    @app.get("/locations/{location_id}/history")
    def location_history(location_id: str, limit: int = 25) -> dict[str, object]:
        loc = locations.get_any(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        return {
            "ok": True,
            "location_id": location_id,
            "location": loc.as_dict(),
            "events": locations.events(location_id, limit=limit),
        }

    @app.get("/locations/{location_id}/hardware-bindings")
    def list_location_hardware_bindings(location_id: str) -> dict[str, object]:
        if locations.get(location_id) is None:
            raise HTTPException(status_code=404, detail="unknown location")
        return {
            "ok": True,
            "location_id": location_id,
            "bindings": [
                binding.as_dict()
                for binding in locations.hardware_bindings(location_id)
            ],
        }

    def _hardware_commissioning_preview(
        location_id: str,
        request: HardwareCommissionRequest,
    ) -> dict[str, object]:
        root = locations.get(location_id)
        if root is None:
            raise HTTPException(status_code=404, detail="unknown location")
        subtree_ids = locations.subtree_ids(location_id)
        subtree_locations = [
            location
            for child_id in subtree_ids
            if (location := locations.get(child_id)) is not None
        ]
        parent_ids = {
            str(location.parent_id)
            for location in subtree_locations
            if location.parent_id
        }
        candidates = [
            location
            for location in subtree_locations
            if location.id != location_id
            and location.id not in parent_ids
            and location.accepts_items
        ]

        def natural_key(location: Location) -> tuple[tuple[int, object], ...]:
            return tuple(
                (0, int(part)) if part.isdigit() else (1, part.casefold())
                for part in re.split(r"(\d+)", location.id)
                if part
            )

        candidates.sort(key=natural_key, reverse=request.direction == "reverse")
        if not candidates:
            raise HTTPException(
                status_code=409,
                detail="this place has no leaf destinations to commission",
            )
        controller_id = request.controller_id.strip()
        next_pixel = request.start_pixel
        assignments: list[dict[str, object]] = []
        requested_pixels: dict[int, str] = {}
        blockers: list[dict[str, object]] = []
        for location in candidates:
            if request.pixel_allocation == "existing_layout":
                pixels = sorted(set(location.light_indexes))
                if not pixels:
                    blockers.append(
                        {
                            "code": "missing_layout_pixels",
                            "location_id": location.id,
                            "message": (
                                f"{location.label or location.id} has no existing "
                                "layout pixels to preserve."
                            ),
                            "pixels": [],
                        }
                    )
            else:
                width = (
                    max(1, int(location.w or 1))
                    if request.pixel_allocation == "place_width"
                    else 1
                )
                pixels = list(range(next_pixel, next_pixel + width))
                next_pixel += width
            for pixel in pixels:
                owner = requested_pixels.get(pixel)
                if owner is not None and owner != location.id:
                    blockers.append(
                        {
                            "code": "layout_pixel_conflict",
                            "location_id": location.id,
                            "message": (
                                f"Pixel {pixel} is assigned to both {owner} and "
                                f"{location.id} in the current layout."
                            ),
                            "pixels": [pixel],
                        }
                    )
                requested_pixels[pixel] = location.id
            assignments.append(
                {
                    "location_id": location.id,
                    "label": location.label or location.id,
                    "path": _path_for(location.id),
                    "pixels": pixels,
                    "address": ",".join(str(pixel) for pixel in pixels),
                    "replaces": [],
                    "unchanged": False,
                }
            )

        pixel_limit = _light_index_limit()
        invalid_pixels = [pixel for pixel in requested_pixels if pixel >= pixel_limit]
        if invalid_pixels:
            blockers.append(
                {
                    "code": "pixel_range",
                    "message": (
                        f"Pixels {min(invalid_pixels)}-{max(invalid_pixels)} are outside "
                        f"the configured range 0-{max(0, pixel_limit - 1)}."
                    ),
                    "pixels": invalid_pixels,
                }
            )

        all_locations = locations.all()
        bindings_by_location = locations.hardware_bindings_by_location(
            [location.id for location in all_locations]
        )
        assignments_by_location = {
            str(assignment["location_id"]): assignment
            for assignment in assignments
        }
        replacement_count = 0
        for owner_id, bindings in bindings_by_location.items():
            for binding in bindings:
                if (
                    not binding.active
                    or binding.kind != "wled_pixels"
                    or binding.controller_id != controller_id
                ):
                    continue
                existing_pixels = pixels_from_binding(binding.address, binding.metadata)
                assignment = assignments_by_location.get(owner_id)
                if assignment is not None:
                    requested = list(assignment["pixels"])
                    if existing_pixels == requested:
                        assignment["unchanged"] = True
                        continue
                    assignment["replaces"] = existing_pixels
                    replacement_count += 1
                    if not request.replace_existing:
                        blockers.append(
                            {
                                "code": "existing_binding",
                                "location_id": owner_id,
                                "message": (
                                    f"{assignment['label']} already uses pixels "
                                    f"{','.join(str(pixel) for pixel in existing_pixels)}."
                                ),
                                "pixels": existing_pixels,
                            }
                        )
                    continue
                overlap = sorted(set(existing_pixels) & set(requested_pixels))
                if overlap:
                    owner = locations.get(owner_id)
                    blockers.append(
                        {
                            "code": "pixel_conflict",
                            "location_id": owner_id,
                            "message": (
                                f"Pixels {','.join(str(pixel) for pixel in overlap)} "
                                f"are already assigned to "
                                f"{(owner.label or owner.id) if owner else owner_id}."
                            ),
                            "pixels": overlap,
                        }
                    )

        return {
            "ok": True,
            "schema": "magpie.hardware_commissioning.v1",
            "root": {
                "id": root.id,
                "label": root.label or root.id,
                "path": _path_for(root.id),
            },
            "controller_id": controller_id,
            "start_pixel": request.start_pixel,
            "direction": request.direction,
            "pixel_allocation": request.pixel_allocation,
            "replace_existing": request.replace_existing,
            "assignments": assignments,
            "blockers": blockers,
            "can_apply": not blockers,
            "summary": {
                "location_count": len(assignments),
                "pixel_count": len(requested_pixels),
                "first_pixel": min(requested_pixels) if requested_pixels else None,
                "last_pixel": max(requested_pixels) if requested_pixels else None,
                "replacement_count": replacement_count,
                "unchanged_count": sum(
                    1 for assignment in assignments if assignment["unchanged"]
                ),
            },
        }

    @app.post("/locations/{location_id}/hardware-bindings/batch-preview")
    def preview_hardware_commissioning(
        location_id: str,
        request: HardwareCommissionRequest,
    ) -> dict[str, object]:
        return _hardware_commissioning_preview(location_id, request)

    @app.post("/locations/{location_id}/hardware-bindings/batch")
    def apply_hardware_commissioning(
        location_id: str,
        request: HardwareCommissionRequest,
    ) -> dict[str, object]:
        preview = _hardware_commissioning_preview(location_id, request)
        if not preview["can_apply"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Resolve commissioning conflicts before saving.",
                    "blockers": preview["blockers"],
                },
            )
        binding_rows = [
            {
                "location_id": assignment["location_id"],
                "kind": "wled_pixels",
                "controller_id": preview["controller_id"],
                "address": assignment["address"],
                "metadata": {"pixels": assignment["pixels"]},
                "active": True,
            }
            for assignment in preview["assignments"]
        ]
        try:
            bindings = locations.set_hardware_bindings(binding_rows)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        for binding in bindings:
            locations.record_event(
                event_type="hardware_binding_updated",
                location_id=binding.location_id,
                note=(
                    f"bulk:{binding.kind}:{binding.controller_id}:{binding.address}"
                ),
            )
        response = {
            **preview,
            "bindings": [binding.as_dict() for binding in bindings],
            "action": "manual_bulk_hardware_binding",
            "applied": [
                {
                    "updated_hardware_binding": binding.id,
                    "location_id": binding.location_id,
                }
                for binding in bindings
            ],
        }
        response["interaction_id"] = store.record_assistant_interaction(
            route="/hardware/commission",
            command=(
                f"commission {len(bindings)} locations in {location_id} "
                f"on {preview['controller_id']}"
            ),
            response=response,
            status="manual",
        )
        return response

    @app.post("/locations/{location_id}/hardware-bindings")
    def set_location_hardware_binding(
        location_id: str, request: HardwareBindingRequest
    ) -> dict[str, object]:
        kind = normalize_binding_kind(request.kind)
        if not kind:
            raise HTTPException(status_code=400, detail="unsupported hardware binding kind")
        metadata = dict(request.metadata or {})
        if kind == "wled_pixels":
            try:
                pixels = pixels_from_binding(request.address, metadata)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail="WLED pixels must be comma-separated integers"
                ) from exc
            light_index_limit = _light_index_limit()
            invalid = [pixel for pixel in pixels if not 0 <= pixel < light_index_limit]
            if request.active and invalid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"WLED pixels {invalid} are outside the configured range "
                        f"0..{light_index_limit - 1}"
                    ),
                )
            if request.active and not pixels:
                raise HTTPException(
                    status_code=400, detail="an active WLED pixel binding needs at least one pixel"
                )
            metadata["pixels"] = pixels
        try:
            binding = locations.set_hardware_binding(
                location_id=location_id,
                kind=kind,
                controller_id=request.controller_id,
                address=request.address,
                metadata=metadata,
                active=request.active,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        locations.record_event(
            event_type="hardware_binding_updated",
            location_id=location_id,
            note=f"{binding.kind}:{binding.controller_id}:{binding.address}",
        )
        return {"ok": True, "binding": binding.as_dict()}

    def _location_move_preview(
        location_id: str, new_parent_id: str | None
    ) -> dict[str, object]:
        location = locations.get(location_id)
        if location is None:
            raise HTTPException(status_code=404, detail="unknown location")
        new_parent = new_parent_id.strip() if new_parent_id else None
        if new_parent == "":
            new_parent = None
        if new_parent == location.parent_id:
            raise HTTPException(
                status_code=409,
                detail="location is already stored at that destination",
            )
        if new_parent and locations.get(new_parent) is None:
            raise HTTPException(status_code=400, detail="unknown parent location")
        subtree_ids = locations.subtree_ids(location_id)
        if new_parent == location_id or new_parent in subtree_ids:
            raise HTTPException(
                status_code=400,
                detail="a location cannot move inside its own subtree",
            )
        _require_unique_sibling_name(
            label=location.label or location.id,
            parent_id=new_parent,
            exclude_id=location_id,
        )
        inventory = store.items_in_locations(subtree_ids)
        home_item_ids = {
            int(item.item_id)
            for item in store.list_inventory()
            if str(getattr(item, "home_location_id", "") or "") in subtree_ids
        }
        rules = [
            rule
            for rule in store.rules(active_only=False, limit=1000)
            if str(rule.target_location_id) in subtree_ids
        ]
        hardware_bindings = [
            binding
            for child_id in subtree_ids
            for binding in locations.hardware_bindings(child_id)
            if binding.active
        ]
        destination = locations.get(new_parent) if new_parent else None
        return {
            "ok": True,
            "location_id": location.id,
            "new_parent_id": new_parent,
            "source": {
                "id": location.id,
                "label": location.label,
                "path": _path_for(location.id),
            },
            "destination": (
                {
                    "id": destination.id,
                    "label": destination.label,
                    "path": _path_for(destination.id),
                }
                if destination is not None
                else {"id": None, "label": "Top level", "path": []}
            ),
            "impact": {
                "location_count": len(subtree_ids),
                "inventory_count": len(inventory),
                "learned_home_count": len(home_item_ids),
                "placement_rule_count": len(rules),
                "hardware_binding_count": len(hardware_bindings),
            },
        }

    @app.get("/locations/{location_id}/move-preview")
    def location_move_preview(
        location_id: str, new_parent_id: str | None = None
    ) -> dict[str, object]:
        return _location_move_preview(location_id, new_parent_id)

    @app.post("/locations/{location_id}/move")
    def move_location(
        location_id: str, request: LocationMoveRequest
    ) -> dict[str, object]:
        preview = _location_move_preview(location_id, request.new_parent_id)
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        new_parent_value = preview["new_parent_id"]
        new_parent = str(new_parent_value) if new_parent_value else None
        try:
            moved = locations.move(location_id, new_parent)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        locations.record_event(
            event_type="location_moved",
            location_id=moved.id,
            note=f"from {loc.parent_id or ''} to {moved.parent_id or ''}".strip(),
        )
        applied = [
            {
                "moved_container": moved.id,
                "from_parent_id": loc.parent_id,
                "to_parent_id": moved.parent_id,
                "location_id": moved.id,
            }
        ]
        _, active_find_errors = _reconcile_active_finds()
        response = {
            "ok": True,
            "id": moved.id,
            "old_parent_id": loc.parent_id,
            "new_parent_id": moved.parent_id,
            "subtree_ids": locations.subtree_ids(moved.id),
            "applied": applied,
            "action": "manual_move_location",
            "active_finds": _raw_active_find_snapshot(),
            "light_ok": not active_find_errors,
            "light_errors": active_find_errors,
        }
        interaction_id = store.record_assistant_interaction(
            route="/locations/move",
            command=f"manual move location {moved.id} from {loc.parent_id or ''} to {moved.parent_id or ''}".strip(),
            response=response,
            status="confirmed",
        )
        response["interaction_id"] = interaction_id
        return {
            **response,
        }

    @app.post("/locations/{location_id}/archive")
    def archive_location(location_id: str) -> dict[str, object]:
        location = locations.get(location_id)
        if location is None:
            raise HTTPException(status_code=404, detail="unknown active location")
        archived_ids = locations.archive_subtree(location_id)
        locations.record_event(
            event_type="location_archived",
            location_id=location_id,
            quantity=float(len(archived_ids)),
            note=f"archived subtree with {len(archived_ids)} locations",
        )
        applied = [
            {
                "archived_location_id": location_id,
                "archived_ids": archived_ids,
            }
        ]
        _, active_find_errors = _reconcile_active_finds()
        response = {
            "ok": True,
            "location_id": location_id,
            "archived": len(archived_ids),
            "archived_ids": archived_ids,
            "action": "manual_archive_location",
            "applied": applied,
            "active_finds": _raw_active_find_snapshot(),
            "light_ok": not active_find_errors,
            "light_errors": active_find_errors,
        }
        interaction_id = store.record_assistant_interaction(
            route="/locations/archive",
            command=f"manual archive location {location_id}",
            response=response,
            status="manual",
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/locations/{location_id}/restore")
    def restore_location(location_id: str) -> dict[str, object]:
        location = locations.get_any(location_id)
        if location is None:
            raise HTTPException(status_code=404, detail="unknown location")
        was_archived_here = locations.has_event(
            "location_archived",
            location_id=location_id,
        )
        _require_unique_sibling_name(
            label=location.label or location.id,
            parent_id=location.parent_id,
            exclude_id=location_id,
            active_only=True,
        )
        try:
            restored_ids = locations.restore_subtree(location_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        locations.record_event(
            event_type="location_restored",
            location_id=location_id,
            quantity=float(len(restored_ids)),
            note=f"restored subtree with {len(restored_ids)} locations",
        )
        if was_archived_here:
            _record_workspace_management_event(
                "location_archive_restore",
                location_id=location_id,
                note="archived and restored through the visual storage lifecycle",
            )
        applied = [
            {
                "restored_location_id": location_id,
                "restored_ids": restored_ids,
            }
        ]
        _, active_find_errors = _reconcile_active_finds()
        response = {
            "ok": True,
            "location_id": location_id,
            "restored": len(restored_ids),
            "restored_ids": restored_ids,
            "action": "manual_restore_location",
            "applied": applied,
            "active_finds": _raw_active_find_snapshot(),
            "light_ok": not active_find_errors,
            "light_errors": active_find_errors,
        }
        interaction_id = store.record_assistant_interaction(
            route="/locations/restore",
            command=f"manual restore location {location_id}",
            response=response,
            status="manual",
        )
        response["interaction_id"] = interaction_id
        return response

    def _location_removal_preview(
        location_id: str, *, include_archived: bool = False
    ) -> dict[str, object]:
        location = (
            locations.get_any(location_id)
            if include_archived
            else locations.get(location_id)
        )
        if location is None:
            raise HTTPException(status_code=404, detail="unknown location")
        subtree_ids = locations.subtree_ids(
            location_id, include_archived=include_archived
        )
        if include_archived and not location.active:
            archive_group = location.archive_group or location.id
            subtree_ids = [
                child_id
                for child_id in subtree_ids
                if (
                    (child := locations.get_any(child_id)) is not None
                    and not child.active
                    and (child.archive_group or child.id) == archive_group
                )
            ]
        inventory = store.items_in_locations(subtree_ids)
        home_items = [
            item
            for item in store.list_inventory()
            if str(getattr(item, "home_location_id", "") or "") in subtree_ids
        ]
        rules = [
            rule
            for rule in store.rules(active_only=False, limit=1000)
            if str(rule.target_location_id) in subtree_ids
        ]
        bindings = [
            binding
            for child_id in subtree_ids
            for binding in locations.hardware_bindings(child_id)
            if binding.active
        ]
        blockers = []
        if inventory:
            blockers.append(f"{len(inventory)} inventory entr{'y' if len(inventory) == 1 else 'ies'}")
        if home_items:
            blockers.append(f"{len(home_items)} item home{'s' if len(home_items) != 1 else ''}")
        if rules:
            blockers.append(f"{len(rules)} placement rule{'s' if len(rules) != 1 else ''}")
        if bindings:
            blockers.append(f"{len(bindings)} active hardware binding{'s' if len(bindings) != 1 else ''}")
        return {
            "ok": True,
            "location_id": location_id,
            "subtree_ids": subtree_ids,
            "location_count": len(subtree_ids),
            "inventory_count": len(inventory),
            "home_item_count": len(home_items),
            "placement_rule_count": len(rules),
            "hardware_binding_count": len(bindings),
            "can_remove": not blockers,
            "blockers": blockers,
        }

    @app.get("/locations/{location_id}/removal-preview")
    def location_removal_preview(
        location_id: str, include_archived: bool = False
    ) -> dict[str, object]:
        return _location_removal_preview(
            location_id, include_archived=include_archived
        )

    @app.delete("/locations/{location_id}")
    def delete_location(location_id: str) -> dict[str, object]:
        preview = _location_removal_preview(location_id)
        if not preview["can_remove"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Move contents and disconnect references before removing this location.",
                    "removal": preview,
                },
            )
        removed = locations.delete_subtree(location_id)
        for removed_id in removed:
            locations.record_event(
                event_type="location_deleted",
                location_id=removed_id,
                note=f"subtree root {location_id}",
            )
        return {"ok": True, "removed": len(removed), "removed_ids": removed}

    @app.post("/locations/{location_id}/cell")
    def create_cell(location_id: str, request: CellCreateRequest) -> dict[str, object]:
        if locations.get(location_id) is None:
            raise HTTPException(status_code=404, detail="unknown location")
        try:
            cell_type = normalize_location_type(request.cell_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cell_id = f"{location_id}-c{request.col}-r{request.row}"
        used = {child.label for child in locations.children(location_id)}
        label = f"C{request.col}·R{request.row}" if request.naming == "coords" else pick_unused(used)
        locations.upsert(
            Location(
                id=cell_id,
                type=cell_type,
                parent_id=location_id,
                label=label,
                col=request.col,
                row=request.row,
                accepts_items=default_accepts_items(cell_type),
            )
        )
        return {"ok": True, "id": cell_id}

    @app.post("/locations/{location_id}/items")
    def add_item_to_location(
        location_id: str, request: LocationItemRequest
    ) -> dict[str, object]:
        location = locations.get(location_id)
        if location is None:
            raise HTTPException(status_code=404, detail="unknown location")
        if not location.accepts_items:
            raise HTTPException(
                status_code=400,
                detail="that place organizes storage but does not hold items directly",
            )
        item_id = store.upsert_item(name=request.name)
        store.put_inventory(
            item_id=item_id,
            bin_id=location_id,
            quantity=request.quantity,
            unit=request.unit,
            normalize_bin=False,
        )
        response = {"ok": True, "item_id": item_id, "location_id": location_id}
        interaction_id = _record_manual_inventory_interaction(
            command=f"manual add {request.name} to {location_id}",
            response={
                **response,
                "action": "manual_location_add_item",
                "item": request.name,
                "quantity": request.quantity,
                "unit": request.unit,
            },
        )
        _record_manual_example(
            command=f"manual add {request.name} to {location_id}",
            plan=[
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": request.name,
                        "location_id": location_id,
                        "quantity": request.quantity,
                        "unit": request.unit,
                    },
                }
            ],
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/locations/{location_id}/quick-put")
    def quick_put_location(
        location_id: str, request: LocationQuickPutRequest
    ) -> dict[str, object]:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        client_request_id = request.client_request_id.strip()
        if client_request_id and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", client_request_id):
            raise HTTPException(status_code=400, detail="client_request_id is invalid")
        route = f"/locations/{location_id}/quick-put"
        if client_request_id:
            interaction = store.assistant_interaction_by_client_request_id(
                client_request_id,
                route=route,
            )
            if interaction is not None:
                stored = interaction.get("response")
                if isinstance(stored, dict):
                    cached = stored.get("client_response")
                    if isinstance(cached, dict):
                        return {**cached, "idempotent_replay": True}
        if error := _inventory_destination_error(location_id, location=loc):
            raise HTTPException(status_code=400, detail=error)
        photos = [p for pid in request.photo_ids if (p := store.photo(pid)) is not None]
        text = request.text.strip()
        if not text and not photos:
            raise HTTPException(status_code=400, detail="text or photo is required")
        draft: dict[str, object] = {
            "name": title_item_name(normalized_query(text)) or "Item",
            "category": "",
            "aliases": [],
            "unit": request.unit or "each",
            "quantity": request.quantity or 1,
        }
        if _llm_key_present(cfg):
            try:
                draft = enrich_item_draft(
                    text or "Identify this item.",
                    photos=photos,
                    client=llm_client,
                    photo_loader=store.photo_content,
                )
            except LlmError:
                pass
        name = str(draft.get("name") or title_item_name(text) or "Item")
        item_id = store.upsert_item(
            name=name,
            description=str(draft.get("description") or ""),
            category=str(draft.get("category") or ""),
            aliases=[str(alias) for alias in draft.get("aliases") or []],
            photo_ids=request.photo_ids,
        )
        quantity = float(draft.get("quantity") or request.quantity or 1)
        unit = str(draft.get("unit") or request.unit or "each")
        store.put_inventory(
            item_id=item_id,
            bin_id=location_id,
            quantity=quantity,
            unit=unit,
            normalize_bin=False,
        )
        lit = locations.nearest_lit(location_id)
        destination_path = _path_for(location_id)
        destination_text = " › ".join(
            str(
                node.get("display_label")
                or node.get("label")
                or node.get("id")
                or ""
            )
            for node in destination_path
            if isinstance(node, dict)
        )
        response = {
            "ok": True,
            "action": "put_away",
            "message": f"Stored {name} in {destination_text or loc.label or loc.id}.",
            "item_id": item_id,
            "item": {
                "item_id": item_id,
                "name": name,
                "category": str(draft.get("category") or ""),
                "aliases": [str(alias) for alias in draft.get("aliases") or []],
                "quantity": quantity,
                "unit": unit,
                "photo_ids": request.photo_ids,
            },
            "destination": {
                "id": location_id,
                "label": loc.label or loc.id,
                "path": destination_path,
            },
            "lit": _light_pixels(
                lit.light_indexes,
                seconds=120.0,
                target=str(lit.light_target or ""),
            ) if lit else False,
        }
        interaction_id = store.record_assistant_interaction(
            route=route,
            command=text or "photo",
            photo_ids=request.photo_ids,
            response=response,
        )
        response["interaction_id"] = interaction_id
        interaction_update: dict[str, object] = {"interaction_id": interaction_id}
        if client_request_id:
            response["client_request_id"] = client_request_id
            interaction_update.update(
                {
                    "client_request_id": client_request_id,
                    "client_response": response,
                }
            )
        store.update_assistant_interaction_response(interaction_id, interaction_update)
        return response

    def _label_payload(location_id: str) -> LabelPayload:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        path = list(reversed(locations.ancestors(location_id)))
        contents = store.items_in_locations(locations.subtree_ids(location_id))
        return LabelPayload(
            location=loc,
            path=path,
            contents=contents,
            base_url=cfg.public_origin.strip() or cfg.zebra.base_url.strip(),
            workspace_id=repository_bundle.current_workspace_id(),
        )

    def _label_card_html(payload: LabelPayload) -> str:
        loc = payload.location
        title = loc.label or loc.id
        path_text = " > ".join((node.label or node.id) for node in payload.path)
        contents = payload.contents[:4]
        content_text = ", ".join(item.name for item in contents) if contents else "Empty"
        tags = ", ".join(loc.purpose_tags[:4])
        qr_svg = render_qr_svg(payload.url)
        return f"""
          <article class="label-card">
            <section class="label-copy">
              <strong>{html.escape(title)}</strong>
              <span>{html.escape(path_text)}</span>
              <em>{html.escape(content_text)}</em>
              {f'<small>{html.escape(tags)}</small>' if tags else ''}
              <code>{html.escape(loc.id)}</code>
            </section>
            <section class="qr" aria-label="QR code for {html.escape(title, quote=True)}">
              {qr_svg}
            </section>
          </article>
        """

    def _label_sheet_payloads(location_id: str) -> list[LabelPayload]:
        if locations.get(location_id) is None:
            raise HTTPException(status_code=404, detail="unknown location")
        payloads = [_label_payload(loc_id) for loc_id in locations.subtree_ids(location_id)]
        payloads.sort(key=lambda payload: (" > ".join(node.label or node.id for node in payload.path), payload.location.id))
        return payloads[:200]

    def _item_rows(item_id: int, *, include_archived: bool = False) -> tuple[int | None, list[SearchResult]]:
        resolved_item_id = store.resolve_item_id(item_id)
        rows = store.item_inventory(resolved_item_id) if resolved_item_id is not None else []
        if not rows and include_archived and resolved_item_id is not None:
            rows = [
                row
                for row in store.list_inventory(include_archived=True)
                if row.item_id == resolved_item_id
            ]
        return resolved_item_id, rows

    def _item_label_payload(item_id: int) -> ItemLabelPayload:
        _resolved_item_id, rows = _item_rows(item_id, include_archived=True)
        if not rows:
            raise HTTPException(status_code=404, detail="unknown item")
        return ItemLabelPayload(
            item=rows[0],
            rows=rows,
            base_url=cfg.public_origin.strip() or cfg.zebra.base_url.strip(),
            workspace_id=repository_bundle.current_workspace_id(),
            location_paths={
                row.bin_id: (
                    " > ".join(
                        node.label or node.id
                        for node in reversed(locations.ancestors(row.bin_id))
                    )
                    or row.bin_id
                )
                for row in rows
            },
        )

    @app.get("/items/{item_id}/history")
    def item_history(item_id: int, limit: int = 25) -> dict[str, object]:
        resolved_item_id, rows = _item_rows(item_id, include_archived=True)
        if not rows:
            raise HTTPException(status_code=404, detail="unknown item")
        return {
            "ok": True,
            "item_id": resolved_item_id,
            "requested_item_id": item_id,
            "item": rows[0].as_dict(),
            "events": store.item_events(resolved_item_id, limit=limit),
        }

    def _format_quantity(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else f"{value:g}"

    def _history_event_label(value: object) -> str:
        event_type = str(value or "event").strip()
        labels = {
            "inventory_put": "Stored",
            "inventory_move": "Moved",
            "inventory_removed": "Removed",
            "item_upsert": "Item updated",
            "location_created": "Location created",
            "location_moved": "Location moved",
            "location_updated": "Location updated",
            "location_archived": "Location archived",
            "location_restored": "Location restored",
            "location_contents_verified": "Contents checked",
        }
        if event_type in labels:
            return labels[event_type]
        raw = event_type.replace("_", " ")
        return raw[:1].upper() + raw[1:]

    def _history_detail(value: object) -> str:
        raw = str(value or "").strip()
        if raw and " " not in raw and "_" in raw:
            return raw.replace("_", " ")
        return raw

    def _is_browser_image(content_type: str) -> bool:
        clean = content_type.split(";", 1)[0].strip().lower()
        return clean in {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}

    @app.get("/l/{location_id}", response_class=HTMLResponse)
    def scan_location(location_id: str) -> str:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        path = list(reversed(locations.ancestors(location_id)))
        contents = store.items_in_locations(locations.subtree_ids(location_id))
        photos = store.location_photos(location_id)
        path_text = " > ".join((node.label or node.id) for node in path)
        tags = loc.purpose_tags[:8]
        title = loc.label or loc.id
        workspace_id = repository_bundle.current_workspace_id() or "local"
        verification = _location_verification(location_id)
        verification_text = (
            f"Last checked {verification.get('verified_at') or 'recently'} · "
            f"{verification.get('placement_count') or 0} item records"
            if verification
            else "Contents have not been checked yet."
        )
        content_rows = "\n".join(
            (
                "<li>"
                f"<span><a href=\"/scan/{quote(workspace_id, safe='')}/i/{item.item_id}\">"
                f"{html.escape(item.name)}</a></span>"
                f"<strong>{html.escape(_format_quantity(item.quantity))} "
                f"{html.escape(item.unit)}</strong>"
                "</li>"
            )
            for item in contents
        )
        if not content_rows:
            content_rows = "<li><span>Empty</span><strong></strong></li>"
        history_rows = []
        for event in locations.events(location_id, limit=8):
            quantity = event.get("quantity")
            qty_text = f" · {_format_quantity(float(quantity))}" if quantity is not None else ""
            note = _history_detail(event.get("note"))
            detail = note or str(event.get("created_at") or "")
            history_rows.append(
                "<li>"
                f"<span>{html.escape(_history_event_label(event.get('event_type')))}"
                f"{html.escape(qty_text)}</span>"
                f"<strong>{html.escape(detail)}</strong>"
                "</li>"
            )
        history_html = "\n".join(history_rows) or "<li><span>No history yet</span><strong></strong></li>"
        tag_html = "".join(f"<span>{html.escape(tag)}</span>" for tag in tags)
        photo_html = "".join(
            (
                f"<img src=\"{html.escape(photo.url, quote=True)}\" alt=\"\" loading=\"lazy\">"
                if _is_browser_image(photo.content_type)
                else f"<a href=\"{html.escape(photo.url, quote=True)}\">"
                f"{html.escape(photo.content_type.split('/')[-1].upper())}</a>"
            )
            for photo in photos
        )
        photo_section = (
            f"<section class=\"product-object-photos\">{photo_html}</section>"
            if photo_html
            else ""
        )
        notes = (
            f"<p class=\"product-object-notes\">{html.escape(loc.placement_notes)}</p>"
            if loc.placement_notes
            else ""
        )
        safe_id = html.escape(loc.id)
        url_id = quote(loc.id, safe="")
        workspace_json = json.dumps(workspace_id).replace("<", "\\u003c")
        location_json = json.dumps(loc.id).replace("<", "\\u003c")
        mobile_location_url = f"/?location={url_id}"
        mobile_put_url = (
            f"{mobile_location_url}&prompt={quote('Put away ', safe='')}&action=put-away"
        )
        storage_location_url = f"/storage?location={url_id}"
        storage_check_url = f"{storage_location_url}&check=1"
        light_action = (
            '<button class="secondary" id="light" type="button">Light spot</button>'
            if locations.nearest_lit(location_id) is not None
            else ""
        )
        label_action = (
            '<button class="secondary" id="print" type="button">Print label</button>'
            if cfg.zebra.host.strip()
            else (
                f'<a class="button secondary" href="/locations/{url_id}/label.pdf">'
                "Download label</a>"
            )
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Magpie Location</title>
  <link rel="stylesheet" href="/theme.css">
  <script src="/product-shell.js" defer></script>
  <script src="/photo-utils.js"></script>
</head>
<body class="product-app">
  <header class="product-header">
    <div class="product-brand">
      <span class="product-mark" aria-hidden="true">M</span>
      <div class="product-title"><h1>Magpie</h1><span class="product-context">Scanned location</span></div>
      <nav class="product-nav" data-product-nav aria-label="Product views"></nav>
    </div>
    <div class="product-header-actions"><span class="product-status">{html.escape(loc.type)}</span></div>
  </header>
  <main class="product-object-main">
    <section class="product-object-hero">
      <p class="product-object-kind">Location</p>
      <h1 class="product-object-title">{html.escape(title)}</h1>
      <p class="product-object-path">{html.escape(path_text)}</p>
      <div class="product-object-meta">
        <span>{html.escape(loc.type)}</span>
        <span>{html.escape(loc.capacity_class)}</span>
        {tag_html}
      </div>
      <div class="product-object-status" id="verificationStatus">{html.escape(verification_text)}</div>
      {notes}
      {photo_section}
      <nav class="product-object-actions" aria-label="Location actions">
        <a class="button" id="quickPutAction" href="#quickPut">Quick add here</a>
        <a class="button secondary" href="{html.escape(mobile_put_url, quote=True)}">Put away with Magpie</a>
        <a class="button secondary" href="{html.escape(storage_location_url, quote=True)}">Manage place</a>
        <a class="button secondary" id="verifyContents" href="{html.escape(storage_check_url, quote=True)}">Check contents</a>
        {light_action}
        {label_action}
      </nav>
    </section>
    <div class="product-object-grid">
      <div class="product-object-column">
        <section class="product-object-section">
          <form class="product-object-form" id="quickPut">
            <label for="quickText">Quick add to {html.escape(title)}</label>
            <textarea id="quickText" placeholder="M4 T-nuts, solder wick, blue pry tool"></textarea>
            <input id="quickPhoto" type="file" accept="image/*,.heic,.heif" capture="environment" multiple>
            <button id="quickSubmit" type="submit">Save here</button>
            <div class="product-object-status" id="quickStatus">Add text, a photo, or both.</div>
          </form>
          <form class="product-object-form product-object-hidden" id="quickEdit">
            <label for="editName">Fix last saved item</label>
            <input id="editName" type="text" placeholder="Item name">
            <input id="editCategory" type="text" placeholder="Category">
            <input id="editQuantity" type="number" min="0" step="any" placeholder="Qty">
            <input id="editUnit" type="text" placeholder="Unit">
            <button id="editSubmit" type="submit">Update item</button>
            <div class="product-object-status" id="editStatus">Correct the item if Magpie guessed wrong.</div>
          </form>
        </section>
        <section class="product-object-section">
          <h2>Contents</h2>
          <ul class="product-object-list">{content_rows}</ul>
        </section>
      </div>
      <div class="product-object-column">
        <section class="product-object-section">
          <h2>Recent history</h2>
          <ul class="product-object-list">{history_html}</ul>
        </section>
        <p class="product-object-id">Location ID: {html.escape(loc.id)}</p>
      </div>
    </div>
  </main>
  <nav class="product-bottom-nav" data-product-nav aria-label="Mobile product views"></nav>
  <script>
    if ("serviceWorker" in navigator) {{
      navigator.serviceWorker.register("/service-worker.js").then((registration) => {{
        const worker = registration.active || registration.waiting || registration.installing;
        if (worker) worker.postMessage({{type: "CACHE_SCAN_PAGE", path: window.location.pathname}});
      }}).catch(() => {{}});
    }}
    document.getElementById("quickPutAction").addEventListener("click", () => {{
      requestAnimationFrame(() => document.getElementById("quickText").focus());
    }});
    const lightButton = document.getElementById("light");
    if (lightButton) lightButton.addEventListener("click", async () => {{
      const button = lightButton;
      button.disabled = true;
      button.textContent = "Lighting...";
      try {{
        const response = await fetch("/locations/{url_id}/light", {{ method: "POST" }});
        button.textContent = response.ok ? "Lit" : "Light failed";
      }} catch (error) {{
        button.textContent = "Light failed";
      }} finally {{
        setTimeout(() => {{
          button.disabled = false;
          button.textContent = "Light spot";
        }}, 1800);
      }}
    }});
    const printButton = document.getElementById("print");
    if (printButton) printButton.addEventListener("click", async () => {{
      const button = printButton;
      button.disabled = true;
      button.textContent = "Printing...";
      try {{
        const response = await fetch("/locations/{url_id}/print-label", {{ method: "POST" }});
        button.textContent = response.ok ? "Sent" : "Print failed";
      }} catch (error) {{
        button.textContent = "Print failed";
      }} finally {{
        setTimeout(() => {{
          button.disabled = false;
          button.textContent = "Print label";
        }}, 1800);
      }}
    }});
    async function quickPhotoPayload(file) {{
      const prepared = await window.magpiePhotos.prepare(file);
      return {{
        filename: prepared.filename,
        content_type: prepared.contentType,
        data_base64: prepared.dataUrl
      }};
    }}
    async function uploadQuickPhotoPayload(payload) {{
      const response = await fetch("/photos", {{
        method: "POST",
        headers: {{"content-type": "application/json"}},
        body: JSON.stringify(payload)
      }});
      let data = {{}};
      try {{ data = await response.json(); }} catch (error) {{}}
      if (!response.ok) {{
        const failure = new Error(data.detail || "photo upload failed");
        failure.status = response.status;
        throw failure;
      }}
      return data.photo.id;
    }}
    const workspaceId = {workspace_json};
    const locationId = {location_json};
    const legacyQueueKey = "magpie.scanQuickPut.queue.v1";
    const queueKey = `magpie.scanQuickPut.queue.v2.${{workspaceId}}`;
    const queueLimit = 25;
    const queuePhotoBytesLimit = 18 * 1024 * 1024;
    function readQueue() {{
      try {{
        const raw = localStorage.getItem(queueKey);
        const queue = raw ? JSON.parse(raw) : [];
        return Array.isArray(queue) ? queue.filter((entry) => entry.workspace_id === workspaceId) : [];
      }} catch (error) {{
        return [];
      }}
    }}
    function migrateLegacyQueue() {{
      if (localStorage.getItem(queueKey) !== null) return;
      try {{
        const raw = localStorage.getItem(legacyQueueKey);
        const legacy = raw ? JSON.parse(raw) : [];
        if (!Array.isArray(legacy) || !legacy.length) return;
        const migrated = legacy.map((entry) => ({{...entry, workspace_id:workspaceId}}));
        writeQueue(migrated);
        localStorage.removeItem(legacyQueueKey);
      }} catch (error) {{}}
    }}
    function queuedPhotoBytes(queue) {{
      return queue.flatMap((entry) => Array.isArray(entry.photo_payloads) ? entry.photo_payloads : [])
        .reduce((total,payload) => total + window.magpiePhotos.dataUrlBytes(payload.data_base64),0);
    }}
    function writeQueue(queue) {{
      const scoped = queue.filter((entry) => entry.workspace_id === workspaceId).slice(-queueLimit);
      if (queuedPhotoBytes(scoped) > queuePhotoBytesLimit) {{
        throw new Error("Offline photos are full. Reconnect before saving more.");
      }}
      localStorage.setItem(queueKey, JSON.stringify(scoped));
    }}
    function queueEntry(entry) {{
      const queue = readQueue();
      queue.push({{...entry, workspace_id:workspaceId, queued_at:new Date().toISOString()}});
      writeQueue(queue);
      updateQueueStatus();
    }}
    function updateQueueStatus(message) {{
      const status = document.getElementById("quickStatus");
      const count = readQueue().filter((entry) => entry.location_id === locationId).length;
      if (message) status.textContent = message;
      else if (count) status.textContent = `${{count}} saved entr${{count === 1 ? "y" : "ies"}} waiting to retry.`;
    }}
    let lastSavedItem = null;
    function showQuickEdit(data) {{
      if (!data || !data.item || !data.item.item_id) return;
      lastSavedItem = data.item;
      document.getElementById("editName").value = data.item.name || "";
      document.getElementById("editCategory").value = data.item.category || "";
      document.getElementById("editQuantity").value = data.item.quantity || 1;
      document.getElementById("editUnit").value = data.item.unit || "each";
      document.getElementById("quickEdit").classList.remove("product-object-hidden");
    }}
    async function uploadQueuedPhotos(entry, persist = null) {{
      entry.photo_ids = Array.isArray(entry.photo_ids) ? entry.photo_ids : [];
      entry.photo_payloads = Array.isArray(entry.photo_payloads) ? entry.photo_payloads : [];
      while (entry.photo_payloads.length) {{
        const photoId = await uploadQuickPhotoPayload(entry.photo_payloads[0]);
        entry.photo_ids = [...new Set([...entry.photo_ids,photoId])];
        entry.photo_payloads.shift();
        if (persist) persist();
      }}
    }}
    async function submitQuickPut(entry, persist = null) {{
      await uploadQueuedPhotos(entry,persist);
      const response = await fetch("/locations/{url_id}/quick-put", {{
        method: "POST",
        headers: {{"content-type": "application/json"}},
        body: JSON.stringify({{text:entry.text, photo_ids:entry.photo_ids, client_request_id:entry.client_request_id}})
      }});
      let data = {{}};
      try {{ data = await response.json(); }} catch (error) {{}}
      if (!response.ok) {{
        const failure = new Error(data.detail || "save failed");
        failure.status = response.status;
        throw failure;
      }}
      return data;
    }}
    async function retryQueuedQuickPuts() {{
      const queue = readQueue();
      if (!queue.length) return;
      let saved = 0;
      let rejected = 0;
      for (let index = 0; index < queue.length; index += 1) {{
        const entry = queue[index];
        if (entry.location_id !== locationId) continue;
        try {{
          await submitQuickPut(entry,() => writeQueue(queue));
          queue.splice(index,1);
          index -= 1;
          writeQueue(queue);
          saved += 1;
        }} catch (error) {{
          if (error.status && error.status < 500) {{
            queue.splice(index,1);
            index -= 1;
            writeQueue(queue);
            rejected += 1;
          }}
        }}
      }}
      if (saved) updateQueueStatus(`Retried and saved ${{saved}} entr${{saved === 1 ? "y" : "ies"}}.`);
      else if (rejected) updateQueueStatus(`${{rejected}} saved entr${{rejected === 1 ? "y needs" : "ies need"}} to be entered again.`);
      else updateQueueStatus();
    }}
    migrateLegacyQueue();
    updateQueueStatus();
    window.addEventListener("online", retryQueuedQuickPuts);
    retryQueuedQuickPuts();
    document.getElementById("quickPut").addEventListener("submit", async (event) => {{
      event.preventDefault();
      const status = document.getElementById("quickStatus");
      const button = document.getElementById("quickSubmit");
      const text = document.getElementById("quickText").value.trim();
      const files = Array.from(document.getElementById("quickPhoto").files || []);
      if (!text && !files.length) {{
        status.textContent = "Add text or a photo first.";
        return;
      }}
      button.disabled = true;
      status.textContent = files.length ? "Uploading photo..." : "Saving...";
      const photoIds = [];
      const photoPayloads = [];
      const clientRequestId = globalThis.crypto?.randomUUID?.() || `${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
      try {{
        for (const file of files) {{
          const payload = await quickPhotoPayload(file);
          photoPayloads.push(payload);
          photoIds.push(await uploadQuickPhotoPayload(payload));
          photoPayloads.shift();
        }}
        status.textContent = "Saving to this spot...";
        const entry = {{workspace_id:workspaceId, location_id:locationId, text, photo_ids:photoIds, photo_payloads:photoPayloads, client_request_id:clientRequestId}};
        const data = await submitQuickPut(entry);
        status.textContent = `Saved ${{data.item.name}} here${{data.lit ? " and lit it" : ""}}${{data.interaction_id ? ` · interaction #${{data.interaction_id}}` : ""}}.`;
        showQuickEdit(data);
        document.getElementById("quickText").value = "";
        document.getElementById("quickPhoto").value = "";
      }} catch (error) {{
        if (!error.status && (text || photoIds.length || photoPayloads.length)) {{
          try {{
            queueEntry({{location_id:locationId, text, photo_ids:photoIds, photo_payloads:photoPayloads, client_request_id:clientRequestId}});
            status.textContent = "Could not reach Magpie. Entry queued for retry.";
          }} catch (queueError) {{
            status.textContent = queueError.message || "Could not save this entry offline.";
          }}
        }} else status.textContent = error.message || "Photo upload failed; nothing queued.";
      }} finally {{
        button.disabled = false;
      }}
    }});
    document.getElementById("quickEdit").addEventListener("submit", async (event) => {{
      event.preventDefault();
      if (!lastSavedItem || !lastSavedItem.item_id) return;
      const status = document.getElementById("editStatus");
      const button = document.getElementById("editSubmit");
      button.disabled = true;
      status.textContent = "Updating...";
      try {{
        const response = await fetch(`/inventory/${{lastSavedItem.item_id}}`, {{
          method: "POST",
          headers: {{"content-type": "application/json"}},
          body: JSON.stringify({{
            name: document.getElementById("editName").value.trim(),
            bin_id: "{safe_id}",
            quantity: Number(document.getElementById("editQuantity").value || 1),
            unit: document.getElementById("editUnit").value.trim() || "each",
            category: document.getElementById("editCategory").value.trim(),
            description: "",
            aliases: []
          }})
        }});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "update failed");
        lastSavedItem.name = document.getElementById("editName").value.trim();
        lastSavedItem.category = document.getElementById("editCategory").value.trim();
        lastSavedItem.quantity = Number(document.getElementById("editQuantity").value || 1);
        lastSavedItem.unit = document.getElementById("editUnit").value.trim() || "each";
        status.textContent = "Updated.";
      }} catch (error) {{
        status.textContent = error.message || "Update failed";
      }} finally {{
        button.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""

    @app.get("/i/{item_id}", response_class=HTMLResponse)
    def scan_item(item_id: int) -> str:
        payload = _item_label_payload(item_id)
        item = payload.item
        title = item.name
        alias_text = ", ".join(item.aliases[:8])
        photo_urls = list(dict.fromkeys(photo for row in payload.rows for photo in row.photos))
        photo_html = "".join(
            f"<img src=\"{html.escape(url, quote=True)}\" alt=\"\" loading=\"lazy\">"
            for url in photo_urls[:8]
        )
        photo_section = (
            f"<section class=\"product-object-photos\">{photo_html}</section>"
            if photo_html
            else ""
        )
        location_rows = []
        for row in payload.rows:
            loc = locations.get(row.bin_id)
            path = list(reversed(locations.ancestors(row.bin_id))) if loc else []
            path_text = " > ".join((node.label or node.id) for node in path) or row.bin_id
            location_url = (
                f"/scan/{quote(payload.workspace_id or 'local', safe='')}/l/"
                f"{quote(row.bin_id, safe='')}"
            )
            location_rows.append(
                "<li>"
                f"<span><a href=\"{html.escape(location_url, quote=True)}\">"
                f"{html.escape(path_text)}</a></span>"
                f"<strong>{html.escape(_format_quantity(row.quantity))} {html.escape(row.unit)}</strong>"
                "</li>"
            )
        location_html = "\n".join(location_rows)
        history_rows = []
        for event in store.item_events(item.item_id, limit=8):
            quantity = event.get("quantity")
            qty_text = f" · {_format_quantity(float(quantity))}" if quantity is not None else ""
            target = str(event.get("bin_id") or "")
            note = _history_detail(event.get("note"))
            detail = " · ".join(part for part in [target, note] if part)
            history_rows.append(
                "<li>"
                f"<span>{html.escape(_history_event_label(event.get('event_type')))}"
                f"{html.escape(qty_text)}</span>"
                f"<strong>{html.escape(detail or str(event.get('created_at') or ''))}</strong>"
                "</li>"
            )
        history_html = "\n".join(history_rows) or "<li><span>No history yet</span><strong></strong></li>"
        pdf_url = f"/items/{item.item_id}/label.pdf"
        find_url = (
            f"/?prompt={quote(f'Find {item.name}', safe='')}"
            "&action=find&auto=find"
        )
        manage_url = f"/storage?item={item.item_id}"
        item_status = item.category or ("Inventory item" if item.active else "Archived item")
        manage_class = "button secondary" if item.active else "button"
        find_action = (
            f'<a class="button" href="{html.escape(find_url, quote=True)}">Find with Magpie</a>'
            if item.active
            else ""
        )
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Magpie Item</title>
  <link rel="stylesheet" href="/theme.css">
  <script src="/product-shell.js" defer></script>
</head>
<body class="product-app">
  <header class="product-header">
    <div class="product-brand">
      <span class="product-mark" aria-hidden="true">M</span>
      <div class="product-title"><h1>Magpie</h1><span class="product-context">Scanned item</span></div>
      <nav class="product-nav" data-product-nav aria-label="Product views"></nav>
    </div>
    <div class="product-header-actions"><span class="product-status">{html.escape(item_status)}</span></div>
  </header>
  <main class="product-object-main">
    <section class="product-object-hero">
      <p class="product-object-kind">Item</p>
      <h1 class="product-object-title">{html.escape(title)}</h1>
      {f'<p class="product-object-notes">{html.escape(item.description)}</p>' if item.description else ''}
      <div class="product-object-meta">
        {'<span>Archived</span>' if not item.active else ''}
        {f'<span>{html.escape(item.category)}</span>' if item.category else ''}
        {f'<span>{html.escape(item.size_class)}</span>' if item.size_class else ''}
        {f'<span>{html.escape(alias_text)}</span>' if alias_text else ''}
      </div>
      {photo_section}
      <nav class="product-object-actions" aria-label="Item actions">
        {find_action}
        <a class="{manage_class}" href="{html.escape(manage_url, quote=True)}">Manage item</a>
        <a class="button secondary" href="{html.escape(pdf_url, quote=True)}">Download label</a>
      </nav>
    </section>
    <div class="product-object-grid">
      <div class="product-object-column">
        <section class="product-object-section">
          <h2>Stored at</h2>
          <ul class="product-object-list">{location_html}</ul>
        </section>
      </div>
      <div class="product-object-column">
        <section class="product-object-section">
          <h2>Recent history</h2>
          <ul class="product-object-list">{history_html}</ul>
        </section>
        <p class="product-object-id">Item ID: {item.item_id}</p>
      </div>
    </div>
  </main>
  <nav class="product-bottom-nav" data-product-nav aria-label="Mobile product views"></nav>
</body>
</html>
"""

    @app.get("/labels/printer/status")
    def label_printer_status() -> dict[str, object]:
        return _label_printer_status()

    @app.get("/locations/{location_id}/labels.html", response_class=HTMLResponse)
    def location_label_sheet(location_id: str) -> str:
        payloads = _label_sheet_payloads(location_id)
        root = payloads[0].location if payloads else locations.get(location_id)
        title = (root.label or root.id) if root else location_id
        pdf_url = f"/locations/{quote(location_id, safe='')}/labels.pdf"
        cards = "\n".join(_label_card_html(payload) for payload in payloads)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Labels - {html.escape(title)}</title>
  <style>
    :root {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #171717; background: #f6f6f2; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 18px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 auto 16px; max-width: 1060px; }}
    h1 {{ margin: 0; font-size: 1.35rem; letter-spacing: 0; }}
    button {{ min-height: 40px; border-radius: 8px; border: 1px solid #171717; background: #171717; color: #fff; padding: 0 14px; font: inherit; }}
    a {{ min-height: 40px; border-radius: 8px; border: 1px solid #171717; color: #171717; padding: 0 14px; font: inherit; display: grid; place-items: center; text-decoration: none; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    main {{ max-width: 1060px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 12px; }}
    .label-card {{ min-height: 190px; break-inside: avoid; page-break-inside: avoid; display: grid; grid-template-columns: minmax(0, 1fr) 116px; gap: 12px; padding: 12px; border: 1px solid #171717; border-radius: 8px; background: #fff; }}
    .label-copy {{ min-width: 0; display: grid; align-content: start; gap: 7px; }}
    strong {{ font-size: 1.35rem; line-height: 1.05; overflow-wrap: anywhere; }}
    span, em, small {{ color: #454944; line-height: 1.25; overflow-wrap: anywhere; }}
    em {{ font-style: normal; }}
    code {{ margin-top: 4px; color: #646963; font-size: .78rem; overflow-wrap: anywhere; }}
    .qr {{ display: grid; place-items: center; }}
    .qr svg {{ width: 108px; height: 108px; display: block; }}
    @media print {{
      :root, body {{ background: #fff; }}
      body {{ padding: 0; }}
      header {{ display: none; }}
      main {{ max-width: none; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0; }}
      .label-card {{ border-radius: 0; min-height: 1.85in; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)} labels</h1>
    <div class="actions">
      <a href="{html.escape(pdf_url, quote=True)}">Export PDF</a>
      <button type="button" onclick="window.print()">Print sheet</button>
    </div>
  </header>
  <main>{cards}</main>
</body>
</html>
"""

    @app.get("/locations/{location_id}/labels.pdf", response_class=Response)
    def location_label_sheet_pdf(location_id: str) -> Response:
        payloads = _label_sheet_payloads(location_id)
        pdf = render_label_sheet_pdf(payloads)
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", location_id).strip("-") or "labels"
        headers = {"content-disposition": f'attachment; filename="{safe_name}-labels.pdf"'}
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/locations/{location_id}/qr.svg", response_class=Response)
    def location_qr_svg(location_id: str) -> Response:
        svg = render_qr_svg(_label_payload(location_id).url)
        return Response(
            svg,
            media_type="image/svg+xml",
            headers={"cache-control": "private, no-store"},
        )

    @app.get("/locations/{location_id}/label.pdf", response_class=Response)
    def location_label_pdf(location_id: str) -> Response:
        payload = _label_payload(location_id)
        pdf = render_label_sheet_pdf([payload])
        safe_name = (
            re.sub(r"[^a-zA-Z0-9_.-]+", "-", payload.location.label or location_id)
            .strip("-")
            or "location"
        )
        headers = {
            "content-disposition": f'attachment; filename="{safe_name}-label.pdf"'
        }
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/locations/labels.pdf", response_class=Response)
    def location_label_batch_pdf(location_ids: str = "") -> Response:
        parsed_location_ids = list(
            dict.fromkeys(
                value.strip() for value in location_ids.split(",") if value.strip()
            )
        )
        if not parsed_location_ids:
            raise HTTPException(status_code=400, detail="select at least one location")
        if len(parsed_location_ids) > 100:
            raise HTTPException(
                status_code=400,
                detail="label batches are limited to 100 locations",
            )
        payloads: list[LabelPayload] = []
        missing: list[str] = []
        for location_id in parsed_location_ids:
            try:
                payloads.append(_label_payload(location_id))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                missing.append(location_id)
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "one or more locations no longer exist",
                    "location_ids": missing,
                },
            )
        pdf = render_label_sheet_pdf(payloads)
        headers = {
            "content-disposition": 'attachment; filename="magpie-location-labels.pdf"',
            "x-magpie-label-count": str(len(payloads)),
        }
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/items/{item_id}/label.pdf", response_class=Response)
    def item_label_pdf(item_id: int) -> Response:
        payload = _item_label_payload(item_id)
        pdf = render_item_label_pdf(payload)
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", payload.item.name).strip("-") or f"item-{item_id}"
        headers = {"content-disposition": f'attachment; filename="{safe_name}-label.pdf"'}
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/items/labels.pdf", response_class=Response)
    def item_label_batch_pdf(item_ids: str = "") -> Response:
        raw_item_ids = [value.strip() for value in item_ids.split(",") if value.strip()]
        if not raw_item_ids:
            raise HTTPException(status_code=400, detail="select at least one item")
        if len(raw_item_ids) > 100:
            raise HTTPException(status_code=400, detail="label batches are limited to 100 items")
        try:
            parsed_item_ids = [int(value) for value in raw_item_ids]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="item IDs must be integers") from exc
        if any(item_id <= 0 for item_id in parsed_item_ids):
            raise HTTPException(status_code=400, detail="item IDs must be positive integers")
        parsed_item_ids = list(dict.fromkeys(parsed_item_ids))
        payloads: list[ItemLabelPayload] = []
        missing: list[int] = []
        for item_id in parsed_item_ids:
            try:
                payloads.append(_item_label_payload(item_id))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                missing.append(item_id)
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "one or more items no longer exist",
                    "item_ids": missing,
                },
            )
        pdf = render_item_label_sheet_pdf(payloads)
        headers = {
            "content-disposition": 'attachment; filename="magpie-item-labels.pdf"',
            "x-magpie-label-count": str(len(payloads)),
        }
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/locations/{location_id}/label.zpl", response_class=PlainTextResponse)
    def location_label_zpl(location_id: str) -> str:
        return render_location_zpl(_label_payload(location_id), cfg.zebra)

    def _print_location_label(location_id: str) -> dict[str, object]:
        zpl = render_location_zpl(_label_payload(location_id), cfg.zebra)
        if cfg.zebra.host.strip():
            send_zpl(zpl, cfg.zebra)
            return {"printed_label": location_id, "printer": cfg.zebra.host}
        printers = _ready_edge_service_devices("printer")
        if not printers:
            raise ValueError("no connected edge label printer is ready")
        device_id = str(printers[0].get("device_id") or "")
        command = _enqueue_edge_command(
            device_id,
            "print_zpl",
            {"zpl": zpl, "location_id": location_id},
        )
        return {
            "queued_label": location_id,
            "printer": f"edge:{device_id}",
            "device_id": device_id,
            "command_id": command.id,
            "status": command.status,
        }

    @app.post("/locations/{location_id}/print-label")
    def print_location_label(location_id: str) -> dict[str, object]:
        try:
            printed = _print_location_label(location_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "location_id": location_id, **printed}

    @app.get("/locations/{location_id}/contents")
    def location_contents(
        location_id: str,
        q: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, object]:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        clean_query = re.sub(r"\s+", " ", q).strip().lower()[:200]
        clean_limit = max(1, min(int(limit), 100))
        clean_offset = max(0, int(offset))
        page, total = store.items_in_locations_page(
            locations.subtree_ids(location_id),
            query=clean_query,
            limit=clean_limit,
            offset=clean_offset,
        )
        return {
            "ok": True,
            "schema": "magpie.location_contents.v1",
            "location_id": loc.id,
            "query": clean_query,
            "items": [item.as_dict() for item in page],
            "total": total,
            "offset": clean_offset,
            "limit": clean_limit,
            "has_more": clean_offset + len(page) < total,
        }

    @app.get("/locations/{location_id}")
    def get_location(
        location_id: str,
        include_children: bool = True,
        include_contents: bool = True,
    ) -> dict[str, object]:
        loc = locations.get(location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="unknown location")
        ancestry = locations.ancestors(location_id)
        root = ancestry[-1] if ancestry else loc
        inventory_metrics = store.location_inventory_metrics()
        children = locations.children(location_id) if include_children else []
        subtree_location_ids = locations.subtree_ids(location_id)
        summary_location_ids = [loc.id, *(child.id for child in children)]
        selected_event = locations.latest_event(
            "location_contents_verified",
            location_id=location_id,
        )
        verification_events = {location_id: selected_event} if selected_event else {}
        bindings_by_location = locations.hardware_bindings_by_location(
            summary_location_ids
        )
        photos_by_location = store.location_photos_by_location(summary_location_ids)
        child_counts: dict[str, int] = {}
        for node in locations.all():
            if node.parent_id:
                child_counts[node.parent_id] = child_counts.get(node.parent_id, 0) + 1
        selected_verification = _verification_from_event(
            location_id,
            verification_events.get(location_id),
        )
        response: dict[str, object] = {
            "location": _location_summary(
                loc,
                child_counts=child_counts,
                inventory_metrics=inventory_metrics,
                verification_events=verification_events,
                verification_coverage_complete=True,
                bindings_by_location=bindings_by_location,
                photos_by_location=photos_by_location,
            ),
            "ancestors": [a.id for a in ancestry][1:],
            # Full place, root -> self, with ambiguity-aware display labels.
            "path": _path_for(location_id),
            # Lights are configured per top-level unit. A location only exposes
            # light config if its root cabinet/chest has lights enabled.
            "is_root": loc.parent_id is None,
            "lights_enabled": bool(root.light_target),
            "root_light_target": root.light_target,
            "items": [item.as_dict() for item in store.items_in_location(location_id)],
            "verification": selected_verification,
            "confidence": _location_confidence(
                item_count=sum(
                    int(inventory_metrics.get(child_id, {}).get("item_count", 0))
                    for child_id in subtree_location_ids
                ),
                review_count=sum(
                    int(inventory_metrics.get(child_id, {}).get("review_count", 0))
                    for child_id in subtree_location_ids
                ),
                content_revision=int(
                    inventory_metrics.get(location_id, {}).get("content_revision", 0)
                ),
                verification=selected_verification,
                verification_coverage_complete=True,
            ),
            "verification_coverage_complete": True,
        }
        if include_children:
            response["children"] = [
                _location_summary(
                    child,
                    child_counts=child_counts,
                    inventory_metrics=inventory_metrics,
                    verification_events=verification_events,
                    verification_coverage_complete=False,
                    bindings_by_location=bindings_by_location,
                    photos_by_location=photos_by_location,
                )
                for child in children
            ]
        if include_contents:
            # Everything stored anywhere inside this location (its whole subtree),
            # so a selected drawer shows what's in all its bins.
            response["contents"] = [
                item.as_dict()
                for item in store.items_in_locations(subtree_location_ids)
            ]
        return response

    @app.get("/export/inventory.json")
    def export_inventory_json() -> dict[str, object]:
        """Portable inventory snapshot for backups and external integrations."""
        photo_ids: set[int] = set()
        inventory_rows: list[dict[str, object]] = []
        for item in store.list_inventory(include_archived=True):
            row = item.as_dict()
            item_photo_ids = [
                int(match.group(1))
                for url in item.photos
                if (match := re.fullmatch(r"/photos/(\d+)", url)) is not None
            ]
            row["photo_ids"] = list(dict.fromkeys(item_photo_ids))
            photo_ids.update(item_photo_ids)
            inventory_rows.append(row)
        location_rows: list[dict[str, object]] = []
        for loc in locations.all(include_archived=True):
            row = loc.as_dict()
            location_photo_ids = [photo.id for photo in store.location_photos(loc.id)]
            row["photo_ids"] = location_photo_ids
            photo_ids.update(location_photo_ids)
            location_rows.append(row)
        photo_rows: list[dict[str, object]] = []
        total_photo_bytes = 0
        for photo_id in sorted(photo_ids):
            photo = store.photo(photo_id)
            if photo is None:
                raise HTTPException(
                    status_code=500, detail=f"backup photo {photo_id} is missing"
                )
            try:
                content = store.photo_content(photo_id)
            except (FileNotFoundError, OSError) as exc:
                raise HTTPException(
                    status_code=500, detail=f"backup photo {photo_id} content is unavailable"
                ) from exc
            total_photo_bytes += len(content)
            if total_photo_bytes > MAX_BACKUP_PHOTO_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="backup photos exceed the 18 MB portable backup limit",
                )
            photo_rows.append(
                {
                    "id": photo.id,
                    "filename": photo.filename,
                    "content_type": photo.content_type,
                    "data_base64": base64.b64encode(content).decode("ascii"),
                }
            )
        return {
            "schema": "magpie.inventory.v1",
            "build": build_info().as_dict(),
            "locations": location_rows,
            "inventory": inventory_rows,
            "photos": photo_rows,
            "placement_rules": [
                rule.as_dict() for rule in store.rules(active_only=False, limit=1000)
            ],
            "size_rules": [
                rule.as_dict() for rule in store.size_rules(active_only=False, limit=1000)
            ],
        }

    @app.get("/export/inventory.csv", response_class=Response)
    def export_inventory_csv() -> Response:
        """Human-editable inventory export that round-trips with CSV import."""
        output = io.StringIO()
        fieldnames = [
            "item_id",
            "name",
            "location_id",
            "location_path",
            "location_path_types",
            "quantity",
            "unit",
            "category",
            "description",
            "aliases",
            "needs_review",
            "size_class",
            "size_source",
            "size_confidence",
            "dimensions",
            "photos",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for item in store.list_inventory():
            path = location_path(locations, item.bin_id)
            writer.writerow(
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "location_id": item.bin_id,
                    "location_path": " > ".join(
                        node.label or node.id for node in path
                    ) or item.bin_id,
                    "location_path_types": " > ".join(node.type for node in path),
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "category": item.category,
                    "description": item.description,
                    "aliases": "; ".join(item.aliases),
                    "needs_review": "true" if item.needs_review else "false",
                    "size_class": item.size_class,
                    "size_source": item.size_source,
                    "size_confidence": item.size_confidence,
                    "dimensions": json.dumps(item.dimensions or {}, sort_keys=True),
                    "photos": "; ".join(item.photos),
                }
            )
        headers = {"content-disposition": 'attachment; filename="magpie-inventory.csv"'}
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)

    @app.get("/export/inventory.pdf", response_class=Response)
    def export_inventory_pdf() -> Response:
        """Printable inventory report grouped by physical location path."""
        location_rows = locations.all(include_archived=True)
        location_by_id = {location.id: location for location in location_rows}

        def report_path(location_id: str) -> str:
            path = location_path(locations, location_id)
            return " > ".join(node.label or node.id for node in path) or location_id

        rows: list[InventoryReportRow] = []
        for item in store.list_inventory():
            location = location_by_id.get(item.bin_id)
            rows.append(
                InventoryReportRow(
                    item_id=item.item_id,
                    name=item.name,
                    quantity=item.quantity,
                    unit=item.unit,
                    location_id=item.bin_id,
                    location_path=report_path(item.bin_id),
                    category=item.category,
                    location_active=(
                        location.active if location is not None else True
                    ),
                )
            )
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pdf = render_inventory_report_pdf(rows, generated_label=generated)
        headers = {"content-disposition": 'attachment; filename="magpie-inventory.pdf"'}
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.get("/locations/{location_id}/tree")
    def location_tree(location_id: str) -> dict[str, object]:
        root = locations.get(location_id)
        if root is None:
            raise HTTPException(status_code=404, detail="unknown location")
        item_counts = store.location_item_counts()
        # Loose items stored directly at any node (not just in cells), in one query.
        items_by_loc: dict[str, list[dict[str, object]]] = {}
        for it in store.items_in_locations(locations.subtree_ids(location_id)):
            items_by_loc.setdefault(it.bin_id, []).append(
                {"name": it.name, "bin_id": it.bin_id, "quantity": it.quantity, "unit": it.unit}
            )

        def node(loc) -> dict[str, object]:
            return {
                "id": loc.id,
                "type": loc.type,
                "label": loc.label,
                "grid": loc.grid,
                "col": loc.col,
                "row": loc.row,
                "w": loc.w,
                "h": loc.h,
                "is_lit": loc.is_lit,
                "item_count": item_counts.get(loc.id, 0),
                "items": items_by_loc.get(loc.id, []),
                "children": [node(child) for child in locations.children(loc.id)],
            }

        tree = node(root)
        ancestry = locations.ancestors(location_id)
        tree["ancestors"] = [a.id for a in ancestry][1:]
        tree["lights_enabled"] = bool((ancestry[-1] if ancestry else root).light_target)
        return tree

    @app.get("/inventory")
    def list_inventory(archived: bool = False) -> dict[str, object]:
        items = store.list_inventory(include_archived=archived)
        if archived:
            items = [item for item in items if not item.active]
        return {
            "archived": archived,
            "items": [item.as_dict() for item in items],
        }

    @app.get("/inventory/catalog")
    def inventory_catalog(
        archived: bool = False,
        item_id: int = 0,
        q: str = "",
        category: str = "",
        root_id: str = "",
        scope: str = "all",
        sort: str = "name",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        clean_scope = scope.strip().lower() or "all"
        if clean_scope not in {
            "all",
            "review",
            "placement",
            "photos",
            "no-photo",
            "uncategorized",
        }:
            raise HTTPException(status_code=400, detail="unknown inventory scope")
        clean_sort = sort.strip().lower() or "name"
        if clean_sort not in {"name", "category", "location", "quantity"}:
            raise HTTPException(status_code=400, detail="unknown inventory sort")
        clean_limit = max(1, min(int(limit), 100))
        clean_offset = max(0, int(offset))
        clean_root_id = root_id.strip()
        location_ids: set[str] | None = None
        root_label = ""
        if clean_root_id:
            root = locations.get(clean_root_id, include_archived=True)
            if root is None or root.parent_id is not None:
                raise HTTPException(
                    status_code=404,
                    detail="unknown top-level storage place",
                )
            location_ids = set(
                locations.subtree_ids(clean_root_id, include_archived=True)
            )
            root_label = root.label or root.id
        location_by_id = {
            location.id: location
            for location in locations.all(include_archived=True)
        }
        repository_scope = clean_scope
        if clean_scope == "placement":
            issue_location_ids = {
                location.id
                for location in location_by_id.values()
                if not location.accepts_items
            }
            issue_location_ids.update(
                row.bin_id
                for row in store.list_inventory(include_archived=True)
                if row.active != archived and row.bin_id not in location_by_id
            )
            location_ids = (
                issue_location_ids
                if location_ids is None
                else location_ids & issue_location_ids
            )
            repository_scope = "all"
        resolved_catalog_item_id = store.resolve_item_id(item_id) if item_id > 0 else None
        if item_id > 0:
            direct_rows = [
                row
                for row in store.list_inventory(include_archived=True)
                if row.item_id == resolved_catalog_item_id
                and row.active != archived
            ]
            page = InventoryCatalogPage(
                items=direct_rows,
                total=1 if direct_rows else 0,
                metrics={
                    "items": 1 if direct_rows else 0,
                    "placements": len(direct_rows),
                    "locations": len({row.bin_id for row in direct_rows}),
                    "need_review": 1 if any(row.needs_review for row in direct_rows) else 0,
                },
                categories=sorted(
                    {row.category for row in direct_rows if row.category},
                    key=str.casefold,
                ),
            )
        elif clean_scope == "placement" and not location_ids:
            page = InventoryCatalogPage(
                items=[],
                total=0,
                metrics={
                    "items": 0,
                    "placements": 0,
                    "locations": 0,
                    "need_review": 0,
                },
                categories=[],
            )
        else:
            page = store.inventory_catalog_page(
                archived=archived,
                query=q,
                category=category,
                location_ids=location_ids,
                scope=repository_scope,
                sort=clean_sort,
                limit=clean_limit,
                offset=clean_offset,
            )

        path_cache: dict[str, tuple[list[dict[str, object]], str]] = {}

        def location_path(location_id: str) -> tuple[list[dict[str, object]], str]:
            if location_id not in path_cache:
                path = _path_for(location_id)
                label = " › ".join(
                    str(node.get("label") or node.get("id") or "")
                    for node in path
                    if isinstance(node, dict)
                )
                path_cache[location_id] = (path, label or location_id)
            return path_cache[location_id]

        grouped: dict[int, dict[str, object]] = {}
        flattened: list[dict[str, object]] = []
        for row in page.items:
            item_id = int(row.item_id)
            path, path_label = location_path(str(row.bin_id))
            stored_location = location_by_id.get(str(row.bin_id))
            placement_issue = (
                "missing_location"
                if stored_location is None
                else (
                    "organizer_only"
                    if not stored_location.accepts_items
                    else ""
                )
            )
            placement = {
                **row.as_dict(),
                "location_path": path,
                "location_path_label": path_label,
                "location_active": bool(
                    stored_location is not None and stored_location.active
                ),
                "location_archived": bool(
                    stored_location is not None and not stored_location.active
                ),
                "location_missing": stored_location is None,
                "location_accepts_items": (
                    stored_location.accepts_items
                    if stored_location is not None
                    else None
                ),
                "placement_issue": placement_issue,
            }
            flattened.append(placement)
            group = grouped.setdefault(
                item_id,
                {
                    "item_id": item_id,
                    "name": row.name,
                    "category": row.category,
                    "description": row.description,
                    "aliases": list(row.aliases),
                    "photos": list(row.photos),
                    "home_location_id": row.home_location_id,
                    "active": row.active,
                    "archived_at": row.archived_at,
                    "placements": [],
                    "location_paths": [],
                    "placement_issue_count": 0,
                },
            )
            placements = group["placements"]
            assert isinstance(placements, list)
            placements.append(placement)
            if placement_issue:
                group["placement_issue_count"] = int(
                    group["placement_issue_count"]
                ) + 1
            location_paths = group["location_paths"]
            assert isinstance(location_paths, list)
            location_paths.append(path_label)
            if not group["photos"] and row.photos:
                group["photos"] = list(row.photos)
            if not group["category"] and row.category:
                group["category"] = row.category
            if not group["description"] and row.description:
                group["description"] = row.description
            if not group["home_location_id"] and row.home_location_id:
                group["home_location_id"] = row.home_location_id

        groups = list(grouped.values())
        return {
            "ok": True,
            "schema": "magpie.inventory_catalog.v1",
            "archived": archived,
            "item_id": resolved_catalog_item_id,
            "scope": clean_scope,
            "sort": clean_sort,
            "query": " ".join(q.split()),
            "category": category.strip(),
            "root_id": clean_root_id,
            "root_label": root_label,
            "limit": clean_limit,
            "offset": clean_offset,
            "total": page.total,
            "count": len(groups),
            "has_more": clean_offset + len(groups) < page.total,
            "metrics": page.metrics,
            "categories": page.categories,
            "groups": groups,
            "items": flattened,
        }

    def _inventory_lifecycle_preview(
        request: InventoryLifecycleRequest,
    ) -> tuple[dict[str, object], list[int]]:
        action = request.action.strip().lower()
        if action not in {"archive", "restore"}:
            raise HTTPException(
                status_code=400,
                detail="inventory lifecycle action must be archive or restore",
            )
        item_ids = list(dict.fromkeys(int(item_id) for item_id in request.item_ids))
        rows_by_item: dict[int, list] = {}
        for row in store.list_inventory(include_archived=True):
            rows_by_item.setdefault(int(row.item_id), []).append(row)
        missing = [item_id for item_id in item_ids if item_id not in rows_by_item]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "one or more items no longer exist",
                    "item_ids": missing,
                },
            )
        expected_active = action == "archive"
        stale = [
            item_id
            for item_id in item_ids
            if bool(rows_by_item[item_id][0].active) != expected_active
        ]
        if stale:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"one or more items cannot be {action}d from their current state",
                    "item_ids": stale,
                },
            )
        entries = []
        for item_id in item_ids:
            rows = rows_by_item[item_id]
            entries.append(
                {
                    "item_id": item_id,
                    "name": str(rows[0].name),
                    "placement_count": len(rows),
                    "location_count": len({str(row.bin_id) for row in rows}),
                    "photo_count": len(
                        {
                            str(photo)
                            for row in rows
                            for photo in (row.photos or [])
                            if str(photo)
                        }
                    ),
                }
            )
        return (
            {
                "ok": True,
                "action": action,
                "item_count": len(entries),
                "placement_count": sum(
                    int(entry["placement_count"]) for entry in entries
                ),
                "location_count": len(
                    {
                        str(row.bin_id)
                        for item_id in item_ids
                        for row in rows_by_item[item_id]
                    }
                ),
                "photo_count": sum(int(entry["photo_count"]) for entry in entries),
                "items": entries,
                "preserves": ["placements", "photos", "history", "learned item home"],
            },
            item_ids,
        )

    @app.post("/inventory/lifecycle/preview")
    def preview_inventory_lifecycle(
        request: InventoryLifecycleRequest,
    ) -> dict[str, object]:
        preview, _item_ids = _inventory_lifecycle_preview(request)
        return preview

    def _apply_inventory_lifecycle(
        request: InventoryLifecycleRequest,
    ) -> dict[str, object]:
        preview, item_ids = _inventory_lifecycle_preview(request)
        action = str(preview["action"])
        apply_action = store.archive_item if action == "archive" else store.restore_item
        rollback_action = store.restore_item if action == "archive" else store.archive_item
        completed: list[int] = []
        try:
            for item_id in item_ids:
                if not apply_action(item_id):
                    raise RuntimeError("inventory changed during the lifecycle action")
                completed.append(item_id)
        except (RuntimeError, ValueError) as exc:
            rollback_failed: list[int] = []
            for item_id in reversed(completed):
                try:
                    if not rollback_action(item_id):
                        rollback_failed.append(item_id)
                except (RuntimeError, ValueError):
                    rollback_failed.append(item_id)
            if rollback_failed:
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": "inventory lifecycle failed and Magpie could not fully restore the prior state",
                        "item_ids": rollback_failed,
                    },
                ) from exc
            raise HTTPException(
                status_code=409,
                detail="inventory changed during the lifecycle action; no items were changed",
            ) from exc
        applied = [
            {
                (
                    "archived_inventory_item"
                    if action == "archive"
                    else "restored_inventory_item"
                ): item_id,
                "item": next(
                    str(entry["name"])
                    for entry in preview["items"]
                    if int(entry["item_id"]) == item_id
                ),
            }
            for item_id in item_ids
        ]
        response = {
            **preview,
            "action": f"manual_{action}_inventory",
            "lifecycle_action": action,
            "active": action == "restore",
            "applied": applied,
        }
        _, active_find_errors = _reconcile_active_finds()
        response.update(
            {
                "active_finds": _raw_active_find_snapshot(),
                "active_find_light_ok": not active_find_errors,
                "active_find_light_errors": active_find_errors,
            }
        )
        command = f"manual {action} {len(item_ids)} inventory item"
        if len(item_ids) != 1:
            command += "s"
        interaction_id = _record_manual_inventory_interaction(
            command=command,
            response=response,
        )
        response["interaction_id"] = interaction_id
        return response

    @app.post("/inventory/lifecycle/apply")
    def apply_inventory_lifecycle(
        request: InventoryLifecycleRequest,
    ) -> dict[str, object]:
        return _apply_inventory_lifecycle(request)

    @app.post("/inventory/{item_id}/archive")
    def archive_inventory_item(item_id: int) -> dict[str, object]:
        response = _apply_inventory_lifecycle(
            InventoryLifecycleRequest(item_ids=[item_id], action="archive")
        )
        response["item_id"] = item_id
        response["name"] = str(response["items"][0]["name"])
        return response

    @app.post("/inventory/{item_id}/restore")
    def restore_inventory_item(item_id: int) -> dict[str, object]:
        response = _apply_inventory_lifecycle(
            InventoryLifecycleRequest(item_ids=[item_id], action="restore")
        )
        response["item_id"] = item_id
        response["name"] = str(response["items"][0]["name"])
        return response

    @app.get("/inventory/{item_id}/merge-preview")
    def preview_inventory_item_merge(
        item_id: int, target_item_id: int
    ) -> dict[str, object]:
        try:
            return store.item_merge_preview(
                source_item_id=item_id, target_item_id=target_item_id
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == "item not found" else 409
            raise HTTPException(status_code=status_code, detail=detail) from exc

    @app.post("/inventory/{item_id}/merge")
    def merge_inventory_item(
        item_id: int, request: InventoryMergeRequest
    ) -> dict[str, object]:
        try:
            preview = store.merge_item(
                source_item_id=item_id, target_item_id=request.target_item_id
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail == "item not found" else 409
            raise HTTPException(status_code=status_code, detail=detail) from exc
        _, active_find_errors = _reconcile_active_finds()
        return {
            "ok": True,
            **preview,
            "active_finds": _raw_active_find_snapshot(),
            "active_find_light_ok": not active_find_errors,
            "active_find_light_errors": active_find_errors,
        }

    @app.get("/storage/search")
    def search_storage(
        q: str,
        archived: bool = False,
        limit: int = 30,
    ) -> dict[str, object]:
        query = re.sub(r"\s+", " ", q).strip()
        if not query:
            return {
                "query": q,
                "archived": archived,
                "results": [],
                "location_count": 0,
                "item_count": 0,
            }
        clean_limit = max(1, min(int(limit), 100))
        all_locations = locations.all(include_archived=True)
        location_by_id = {location.id: location for location in all_locations}
        item_counts = store.location_item_counts()
        child_counts: dict[str, int] = {}
        for location in all_locations:
            if location.parent_id:
                child_counts[location.parent_id] = (
                    child_counts.get(location.parent_id, 0) + 1
                )

        def path_nodes(location: Location) -> list[Location]:
            path: list[Location] = []
            seen: set[str] = set()
            current: Location | None = location
            while current is not None and current.id not in seen:
                path.append(current)
                seen.add(current.id)
                current = (
                    location_by_id.get(current.parent_id)
                    if current.parent_id
                    else None
                )
            return list(reversed(path))

        lowered = query.casefold()
        tokens = [token for token in re.split(r"[^a-z0-9]+", lowered) if token]

        def location_score(location: Location) -> tuple[int, str, str] | None:
            if location.active == archived:
                return None
            path = path_nodes(location)
            label = location.label.casefold()
            location_id = location.id.casefold()
            path_text = " > ".join(node.label or node.id for node in path).casefold()
            searchable = " ".join(
                [
                    label,
                    location_id,
                    location.type.replace("_", " ").casefold(),
                    " ".join(location.purpose_tags).casefold(),
                    location.placement_notes.casefold(),
                    path_text,
                ]
            )
            if lowered in {label, location_id}:
                rank = 0
            elif label.startswith(lowered) or location_id.startswith(lowered):
                rank = 1
            elif lowered in label or lowered in location_id:
                rank = 2
            elif tokens and all(token in searchable for token in tokens):
                rank = 3
            else:
                return None
            return (rank, path_text, location.id)

        scored_locations = [
            (score, location)
            for location in all_locations
            if (score := location_score(location)) is not None
        ]
        scored_locations.sort(key=lambda row: row[0])
        location_results: list[dict[str, object]] = []
        for _score, location in scored_locations[:clean_limit]:
            path = path_nodes(location)
            location_results.append(
                {
                    "result_type": "location",
                    "location_id": location.id,
                    "name": location.label or location.id,
                    "type": location.type,
                    "path": [
                        {"id": node.id, "label": node.label or node.id}
                        for node in path
                    ],
                    "child_count": child_counts.get(location.id, 0),
                    "item_count": item_counts.get(location.id, 0),
                    "accepts_items": location.accepts_items,
                    "archived": not location.active,
                }
            )

        item_results: list[dict[str, object]] = []
        for result in search_inventory(store, query, limit=clean_limit):
            location = location_by_id.get(result.bin_id)
            result_is_archived = bool(location is not None and not location.active)
            if result_is_archived != archived:
                continue
            payload = result.as_dict()
            payload.update(
                {
                    "result_type": "item",
                    "location_id": result.bin_id,
                    "path": [
                        {"id": node.id, "label": node.label or node.id}
                        for node in (path_nodes(location) if location else [])
                    ]
                    or [{"id": result.bin_id, "label": result.bin_id}],
                    "archived": result_is_archived,
                }
            )
            item_results.append(payload)

        return {
            "query": q,
            "archived": archived,
            "results": [*location_results, *item_results],
            "location_count": len(location_results),
            "item_count": len(item_results),
        }

    @app.get("/search")
    def search(q: str) -> dict[str, object]:
        results, _ = _resolve_results(search_inventory(store, q))
        return {"query": q, "results": results}

    @app.post("/search/flash")
    def search_and_flash(q: str) -> dict[str, object]:
        results = search_inventory(store, q, limit=1)
        if not results:
            raise HTTPException(status_code=404, detail="no matching inventory found")
        result = results[0]
        lit = locations.nearest_lit(result.bin_id)
        if lit is None:
            raise HTTPException(status_code=404, detail="location has no light binding")
        lit_ok = _light_pixels(
            lit.light_indexes,
            seconds=DEFAULT_LIGHT_SECONDS,
            brightness=1.0,
            target=str(lit.light_target or ""),
        )
        return {
            "ok": True,
            "match": result.as_dict(),
            "location": lit.as_dict(),
            "lit": lit_ok,
            "seconds": DEFAULT_LIGHT_SECONDS,
        }

    return app


def light_bins(controller: LedController, bins: list) -> tuple[bool, str]:
    """Light every given bin's LEDs in one frame, clearing others. Returns (ok, error).

    Out-of-range (unwired) indexes are skipped so one unwired bin does not stop
    the rest of the matches from lighting.
    """
    pixel_count = controller.config.pixel_count
    updates = [
        PixelUpdate(index=index, color=(255, 255, 255))
        for bin in bins
        for index in bin.led_indexes
        if 0 <= index < pixel_count
    ]
    if not updates:
        return False, "no wired LEDs for these bins"
    try:
        controller.set_pixels_for(
            updates,
            clear_others=True,
            brightness=1.0,
            seconds=DEFAULT_LIGHT_SECONDS,
        )
    except Exception as exc:  # Hardware/network failures should not break the request.
        return False, str(exc)
    return True, ""


def light_bin(controller: LedController, bin) -> tuple[bool, str]:
    """Light a single bin's LEDs, clearing any other lit pixels."""
    return light_bins(controller, [bin])


def apply_automation(
    *,
    decision: dict[str, object],
    photo_ids: list[int],
    store: InventoryRepository,
    layout: StorageLayout,
    controller: LedController,
) -> dict[str, object]:
    action = decision.get("action")
    if action == "find_item":
        results = decision.get("results")
        bin_ids: list[str] = []
        seen: set[str] = set()
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            candidate = normalize_bin_id(str(result.get("bin_id") or ""))
            if candidate and candidate not in seen:
                seen.add(candidate)
                bin_ids.append(candidate)
        if not bin_ids:
            fallback = normalize_bin_id(str(decision.get("suggested_bin") or ""))
            if fallback:
                bin_ids.append(fallback)
        match_count = len(bin_ids)
        chosen = [layout.get(bin_id) for bin_id in bin_ids[:LIGHT_BIN_LIMIT]]
        chosen = [bin for bin in chosen if bin is not None]
        if not chosen:
            return {"auto_saved": False, "lit": False, "light_ok": False}
        light_ok, light_error = light_bins(controller, chosen)
        return {
            "auto_saved": False,
            "lit": True,
            "bin_id": chosen[0].id,
            "bin_ids": [bin.id for bin in chosen],
            "match_count": match_count,
            "truncated": match_count > len(chosen),
            "light_ok": light_ok,
            "light_error": light_error,
            "light_seconds": DEFAULT_LIGHT_SECONDS,
        }
    if action != "store_item":
        return {"auto_saved": False, "light_ok": False}
    draft = decision.get("item_draft")
    if not isinstance(draft, dict):
        return {"auto_saved": False, "light_ok": False}
    bin_id = normalize_bin_id(str(draft.get("bin_id") or decision.get("suggested_bin") or ""))
    bin = layout.get(bin_id)
    if bin is None:
        return {"auto_saved": False, "light_ok": False, "error": "bin not found"}
    try:
        item_id = store.upsert_item(
            name=str(draft.get("name") or decision.get("query") or "Unknown item"),
            description=str(draft.get("description") or ""),
            category=str(draft.get("category") or ""),
            aliases=[
                str(alias)
                for alias in draft.get("aliases", [])
                if str(alias).strip()
            ]
            if isinstance(draft.get("aliases"), list)
            else [],
            photo_ids=photo_ids,
        )
        store.put_inventory(
            item_id=item_id,
            bin_id=bin.id,
            quantity=float(draft.get("quantity") or 1),
            unit=str(draft.get("unit") or "each"),
            needs_review=True,
            review_reason="Agent marked this item for review.",
        )
    except (TypeError, ValueError) as exc:
        return {"auto_saved": False, "light_ok": False, "error": str(exc)}

    light_ok, light_error = light_bin(controller, bin)

    decision["suggested_bin"] = bin.id
    draft["bin_id"] = bin.id
    draft["photo_ids"] = photo_ids
    return {
        "auto_saved": True,
        "item_id": item_id,
        "bin_id": bin.id,
        "needs_review": True,
        "light_ok": light_ok,
        "light_error": light_error,
        "light_seconds": DEFAULT_LIGHT_SECONDS,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("LED_CONFIG"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    cfg = LedConfig.from_path(args.config)
    host = args.host or cfg.bind_host
    port = args.port or cfg.bind_port
    uvicorn.run(create_app(cfg), host=host, port=port)


if __name__ == "__main__":
    main()
