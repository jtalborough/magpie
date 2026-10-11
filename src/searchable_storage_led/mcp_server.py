from __future__ import annotations

import atexit
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import LedConfig
from .controller import LedController, PixelUpdate
from .layout import StorageLayout
from .locations import backfill_from_layout
from .labels import query_zebra_status
from .magpie_tools import LIGHT_COLORS, MagpieToolService
from .repositories import StorageRepositories


MCP_INSTRUCTIONS = (
    "Magpie manages the user's complete physical storage system across rooms, "
    "cabinets, drawers, shelves, bins, boxes, and pouches. Inspect inventory and "
    "locations before deciding. Use retrieve_item when the user wants something "
    "physically located and lit. All inventory, location, rule, photo, and print "
    "changes must be submitted through propose_storage_plan and remain pending "
    "until the user confirms them in Magpie. Call get_storage_contract before "
    "drafting an unfamiliar or multi-step change. Never claim a proposed change was applied."
)

def create_mcp_server(service: MagpieToolService) -> FastMCP:
    mcp = FastMCP(
        name="Magpie Storage",
        instructions=MCP_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*",
                "localhost:*",
                "pi5:*",
                "testserver",
                "magpie.example.com",
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "https://magpie.example.com",
            ],
        ),
    )

    @mcp.tool()
    def storage_status() -> dict[str, object]:
        """Get inventory, location, review, and pending-plan counts."""
        return service.status()

    @mcp.tool()
    def get_storage_contract() -> dict[str, object]:
        """Get guarded write tools, argument schemas, and confirmation rules."""
        return service.storage_contract()

    @mcp.tool()
    def search_items(query: str) -> object:
        """Search inventory and return each item's full physical path and light target."""
        return service.read("search_items", {"query": query})

    @mcp.tool()
    def retrieve_item(
        query: str,
        color: str = "white",
        seconds: float = 120.0,
    ) -> dict[str, object]:
        """Find the best inventory match and light its nearest wired location."""
        results = service.read("search_items", {"query": query})
        if not isinstance(results, list) or not results:
            return {"ok": True, "found": False, "query": query, "results": []}
        first = results[0]
        location_id = str(first.get("location_id") or "") if isinstance(first, dict) else ""
        lighting = (
            service.light_location(
                location_id,
                color=LIGHT_COLORS.get(color.lower(), LIGHT_COLORS["white"]),
                seconds=seconds,
            )
            if location_id
            else {"ok": True, "lit": False}
        )
        return {
            "ok": True,
            "found": True,
            "query": query,
            "match": first,
            "results": results,
            "lighting": lighting,
        }

    @mcp.tool()
    def list_locations(
        location_type: str | None = None,
        parent_id: str | None = None,
        capacity_class: str | None = None,
        empty_only: bool = False,
        limit: int = 40,
    ) -> object:
        """List a bounded set of locations using exact placement filters."""
        arguments: dict[str, Any] = {
            "empty_only": empty_only,
            "limit": max(1, min(limit, 50)),
        }
        if location_type:
            arguments["type"] = location_type
        if parent_id:
            arguments["parent_id"] = parent_id
        if capacity_class:
            arguments["capacity_class"] = capacity_class
        return service.read("list_locations", arguments)

    @mcp.tool()
    def list_storage_roots() -> object:
        """List every active top-level storage place with subtree and inventory counts."""
        return service.read("list_storage_roots", {})

    @mcp.tool()
    def list_archived_locations() -> object:
        """List archived storage subtrees that can be restored through a guarded plan."""
        return service.read("list_archived_locations", {})

    @mcp.tool()
    def get_location(location_id: str) -> object:
        """Inspect a location, its path, children, contents, photos, and hardware."""
        return service.read("get_location", {"location_id": location_id})

    @mcp.tool()
    def suggest_locations(
        item: str,
        dimensions: dict[str, float] | None = None,
    ) -> object:
        """Rank destinations using millimeter dimensions, rules, and contents."""
        arguments: dict[str, Any] = {"item": item}
        if dimensions:
            arguments["dimensions"] = dimensions
        return service.read("suggest_locations", arguments)

    @mcp.tool()
    def list_rules(query: str = "") -> object:
        """List learned placement and size rules, optionally filtered by text."""
        return service.read("list_rules", {"query": query})

    @mcp.tool()
    def list_review_items() -> object:
        """List uncertain inventory records awaiting user review."""
        return service.read("list_review_items", {})

    @mcp.tool()
    def explain_location_choice(
        item: str,
        location_id: str,
        dimensions: dict[str, float] | None = None,
    ) -> object:
        """Explain the evidence for or against a proposed storage destination."""
        arguments: dict[str, Any] = {"item": item, "location_id": location_id}
        if dimensions:
            arguments["dimensions"] = dimensions
        return service.read(
            "explain_location_choice",
            arguments,
        )

    @mcp.tool()
    def plan_reorganization(
        queries: list[str],
        destination_id: str = "",
        destination_label: str = "Consolidated Bin",
        parent_id: str = "main-cabinet",
        capacity_class: str = "large",
    ) -> object:
        """Inspect matching item families and draft a multi-item reorganization plan."""
        return service.read(
            "plan_reorganization",
            {
                "queries": queries,
                "destination_id": destination_id,
                "destination_label": destination_label,
                "parent_id": parent_id,
                "capacity_class": capacity_class,
            },
        )

    @mcp.tool()
    def preview_label_sheet(location_id: str) -> object:
        """Preview printable QR labels for a location subtree without printing."""
        return service.read("preview_label_sheet", {"location_id": location_id})

    @mcp.tool()
    def preview_label_batch(location_ids: list[str]) -> object:
        """Preview labels for an explicit set of storage locations."""
        return service.read("preview_label_batch", {"location_ids": location_ids})

    @mcp.tool()
    def get_printer_status() -> object:
        """Check whether the configured physical label printer is available."""
        return service.read("get_printer_status", {})

    @mcp.tool()
    def light_location(
        location_id: str,
        color: str = "white",
        seconds: float = 120.0,
    ) -> dict[str, object]:
        """Light a location or its nearest wired ancestor immediately."""
        return service.light_location(
            location_id,
            color=LIGHT_COLORS.get(color.lower(), LIGHT_COLORS["white"]),
            seconds=seconds,
        )

    @mcp.tool()
    def clear_lights() -> dict[str, object]:
        """Turn off all storage indicator lights."""
        return service.clear_lights()

    @mcp.tool()
    def get_pending_plan() -> dict[str, object]:
        """Get the latest storage plan awaiting user confirmation."""
        return {"ok": True, "pending_plan": service.pending_plan()}

    @mcp.tool()
    def propose_storage_plan(
        command: str,
        steps: list[dict[str, Any]],
        message: str = "",
        photo_ids: list[int] | None = None,
    ) -> dict[str, object]:
        """Submit guarded storage changes for user confirmation; never applies them."""
        return service.propose_plan(
            command=command,
            steps=steps,
            message=message,
            photo_ids=photo_ids,
        )

    return mcp


def create_runtime_service(config: LedConfig | None = None) -> MagpieToolService:
    cfg = config or LedConfig.from_path(os.environ.get("LED_CONFIG"))
    layout = StorageLayout.from_path(cfg.layout_path)
    repositories = StorageRepositories.from_config(
        cfg.storage,
        sqlite_path=cfg.database_path,
    )
    backfill_from_layout(repositories.locations, layout)
    controller = LedController(cfg)
    atexit.register(controller.close)

    def light(
        pixels: list[int],
        color: tuple[int, int, int],
        seconds: float,
        _target: str,
    ) -> bool:
        wanted = sorted({index for index in pixels if 0 <= index < cfg.pixel_count})
        if not wanted:
            return False
        controller.set_pixels_for(
            [PixelUpdate(index=index, color=color) for index in wanted],
            clear_others=True,
            brightness=cfg.default_brightness,
            seconds=seconds,
        )
        return True

    return MagpieToolService(
        store=repositories.inventory,
        locations=repositories.locations,
        layout=layout,
        light_handler=light,
        clear_lights_handler=controller.clear,
        printer_status_handler=lambda: query_zebra_status(cfg.zebra),
    )


def main() -> None:
    create_mcp_server(create_runtime_service()).run(transport="stdio")


if __name__ == "__main__":
    main()
