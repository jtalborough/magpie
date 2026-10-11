from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import (
    READ_TOOL_NAMES,
    TOOL_SCHEMAS,
    WRITE_TOOL_NAMES,
    normalize_plan,
    proposed_plan_rejection_reason,
    run_read_tool,
)
from .layout import StorageLayout, normalize_bin_id


LightHandler = Callable[[list[int], tuple[int, int, int], float, str], bool]
ClearLightsHandler = Callable[[], None]
PrinterStatusHandler = Callable[[], dict[str, object]]

LIGHT_COLORS: dict[str, tuple[int, int, int]] = {
    "white": (255, 255, 255),
    "red": (255, 30, 30),
    "green": (40, 255, 120),
    "blue": (40, 120, 255),
    "yellow": (255, 210, 30),
    "cyan": (30, 230, 255),
    "magenta": (255, 40, 180),
}


@dataclass
class MagpieToolService:
    """One guarded storage tool contract for the embedded agent and MCP clients."""

    store: Any
    locations: Any
    layout: StorageLayout
    light_handler: LightHandler | None = None
    clear_lights_handler: ClearLightsHandler | None = None
    printer_status_handler: PrinterStatusHandler | None = None
    read_tool_names: frozenset[str] = field(
        default_factory=lambda: frozenset(READ_TOOL_NAMES)
    )
    write_tool_names: frozenset[str] = field(
        default_factory=lambda: frozenset(WRITE_TOOL_NAMES)
    )

    def read(self, name: str, arguments: dict[str, Any]) -> object:
        if name not in self.read_tool_names:
            raise ValueError(f"unknown Magpie read tool: {name}")
        if name == "get_printer_status":
            if self.printer_status_handler is None:
                return {"ok": False, "error": "label printer is not connected"}
            return self.printer_status_handler()
        if name == "light_location":
            color_name = str(arguments.get("color") or "white").strip().lower()
            try:
                seconds = float(arguments.get("seconds") or 120.0)
            except (TypeError, ValueError):
                seconds = 120.0
            return self.light_location(
                str(arguments.get("location_id") or ""),
                color=LIGHT_COLORS.get(color_name, LIGHT_COLORS["white"]),
                seconds=max(1.0, min(seconds, 600.0)),
            )
        if name == "clear_lights":
            return self.clear_lights()
        return run_read_tool(
            name,
            arguments,
            store=self.store,
            locations=self.locations,
        )

    def status(self) -> dict[str, object]:
        inventory = self.store.list_inventory()
        return {
            "ok": True,
            "item_count": len({item.item_id for item in inventory}),
            "occupied_location_count": len({item.bin_id for item in inventory}),
            "location_count": self.locations.count(),
            "review_count": len(self.store.review_queue()),
            "pending_plan": self.pending_plan(),
        }

    def storage_contract(self) -> dict[str, object]:
        """Describe the guarded plan surface from the agent's canonical schemas."""
        write_schemas = []
        for schema in TOOL_SCHEMAS:
            function = schema.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            if name not in self.write_tool_names:
                continue
            write_schemas.append(
                {
                    "name": name,
                    "description": str(function.get("description") or ""),
                    "parameters": deepcopy(function.get("parameters") or {}),
                }
            )
        write_schemas.sort(key=lambda entry: str(entry["name"]))
        return {
            "schema": "magpie.storage-plan.v1",
            "confirmation_required": True,
            "proposal_tool": "propose_storage_plan",
            "plan_step": {
                "type": "object",
                "required": ["tool", "arguments"],
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": [entry["name"] for entry in write_schemas],
                    },
                    "arguments": {"type": "object"},
                },
            },
            "write_tools": write_schemas,
            "read_tools": sorted(self.read_tool_names),
            "immediate_hardware_tools": ["light_location", "clear_lights"],
            "rules": [
                "Inspect storage with read tools before proposing changes.",
                "Submit every inventory, location, rule, photo, or print change as a plan step.",
                "A proposal remains pending until the user confirms it in Magpie.",
                "Never claim a pending proposal was applied.",
            ],
        }

    def pending_plan(self) -> dict[str, object] | None:
        pending = self.store.latest_pending_agent_plan()
        if not isinstance(pending, dict):
            return None
        response = pending.get("response")
        plan = response.get("plan") if isinstance(response, dict) else None
        return {
            "interaction_id": pending.get("id"),
            "command": str(pending.get("command") or ""),
            "photo_ids": list(pending.get("photo_ids") or []),
            "message": str(response.get("message") or "") if isinstance(response, dict) else "",
            "plan": plan if isinstance(plan, list) else [],
        }

    def propose_plan(
        self,
        *,
        command: str,
        steps: list[dict[str, Any]],
        message: str = "",
        photo_ids: list[int] | None = None,
        route: str = "/mcp/propose_plan",
    ) -> dict[str, object]:
        if not command.strip():
            raise ValueError("command is required")
        if not steps:
            raise ValueError("at least one plan step is required")
        unknown = sorted(
            {
                str(step.get("tool") or "")
                for step in steps
                if str(step.get("tool") or "") not in self.write_tool_names
            }
        )
        if unknown:
            raise ValueError(f"unsupported write tools: {', '.join(unknown)}")
        if any(str(step.get("tool") or "") == "print_label" for step in steps):
            status = self.read("get_printer_status", {})
            if not isinstance(status, dict) or not status.get("ok"):
                detail = (
                    str(status.get("error") or "printer unavailable")
                    if isinstance(status, dict)
                    else "printer unavailable"
                )
                raise ValueError(f"physical label printer is unavailable: {detail}")
        normalized = normalize_plan(
            steps,
            locations=self.locations,
            store=self.store,
            layout=self.layout,
            command=command,
        )
        if not normalized:
            raise ValueError("the proposed plan contained no actionable changes")
        if reason := proposed_plan_rejection_reason(
            command,
            normalized,
            store=self.store,
            locations=self.locations,
        ):
            raise ValueError(reason)
        response: dict[str, object] = {
            "ok": True,
            "message": message.strip() or "I have a storage plan ready for confirmation.",
            "plan": normalized,
            "requires_confirmation": True,
        }
        self.store.supersede_pending_agent_plans()
        interaction_id = self.store.record_assistant_interaction(
            route=route,
            command=command.strip(),
            photo_ids=list(photo_ids or []),
            response=response,
            status="pending",
        )
        response["interaction_id"] = interaction_id
        return response

    def light_location(
        self,
        location_id: str,
        *,
        color: tuple[int, int, int] = (255, 255, 255),
        seconds: float = 120.0,
    ) -> dict[str, object]:
        location = self.locations.get(location_id)
        if location is None:
            location = self.locations.get(normalize_bin_id(location_id))
        if location is None:
            raise ValueError(f"unknown location: {location_id}")
        location_id = location.id
        lit = self.locations.nearest_lit(location_id)
        if lit is None or not lit.light_indexes:
            return {
                "ok": True,
                "lit": False,
                "location_id": location_id,
                "message": "This location has no light binding.",
            }
        if self.light_handler is None:
            return {
                "ok": False,
                "lit": False,
                "location_id": location_id,
                "lit_location": lit.id,
                "message": "Lighting is not connected to this Magpie runtime.",
            }
        success = self.light_handler(
            list(lit.light_indexes), color, seconds, str(lit.light_target or "")
        )
        return {
            "ok": True,
            "lit": success,
            "location_id": location_id,
            "lit_location": lit.id,
            "color": color,
            "seconds": seconds,
        }

    def clear_lights(self) -> dict[str, object]:
        if self.clear_lights_handler is None:
            return {"ok": False, "message": "Lighting is not connected."}
        self.clear_lights_handler()
        return {"ok": True, "message": "Lights cleared."}
