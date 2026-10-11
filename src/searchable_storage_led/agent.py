"""The LLM as manager: an agent that calls tools instead of returning a draft.

Read tools (search_items, list_locations) execute live during the loop and feed
their results back to the model. Write tools (create_location, put_item,
move_container) are NOT executed — they are collected into a *plan* and returned
for the user to confirm, then applied by execute_plan. This keeps the
answer-first / confirm-before-write contract while letting the LLM orchestrate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Callable

from .assistant import bin_fits, bin_sort_key, first_empty_bin, search_inventory
from .layout import StorageLayout, normalize_bin_id
from .item_identity import identities_conflict, normalize_item_dimensions
from .llm_client import LLM_USAGE_FIELDS, LlmClient, add_llm_usage, normalize_llm_usage
from .locations import (
    LOCATION_TYPES,
    Location,
    LocationStore,
    default_accepts_items,
    location_path,
    match_locations,
    normalize_internal_dimensions,
    normalize_location_type,
)
from .placement import (
    item_capacity_for,
    measured_capacity_for,
    placement_context,
    suggest_locations,
    tokens,
)
from .store import InventoryStore


AGENT_INSTRUCTIONS = (
    "You manage a searchable storage inventory by calling tools. You are the "
    "steward of the whole storage space: rooms, cabinets, drawers, bins, "
    "pouches, boxes, and the relationships between them. Your job is not only "
    "to answer one-off put-away requests, but to keep the space organized over "
    "time. Propose moves, consolidation, clearer homes, or container changes "
    "when the current organization is wrong or fragmented. "
    "When the user asks what storage spaces, rooms, areas, or cabinets exist, "
    "call list_storage_roots so the answer covers every top-level storage space. "
    "Location labels may repeat under different roots. Use path from location tool "
    "results and pass a qualified reference such as 'Garage > Printer Cabinet' "
    "when a label is ambiguous; never guess between duplicate labels. "
    "When listing or explaining learned placement rules, describe target_path "
    "with its human-readable labels instead of exposing only a location id. "
    "If a learned placement rule reports target_active=false, say its target is "
    "archived and never use it for put-away unless the user restores that "
    "location; offer to change the rule when another active home is intended. "
    "To find an item, call search_items, then answer with the best matching "
    "item's path labels and mention nearest_lit_location_id when it differs "
    "from the final location so the user can follow the lit ancestor into "
    "unlit pouches, boxes, or shelves. If a search result has archived=true, "
    "say that its storage location is archived. To put something away, first call "
    "search_items for the item name or the closest cleaned-up name, then choose "
    "a location: prefer exact or near-exact existing item homes, prefer the "
    "ranked placement candidates in context, call suggest_locations when unsure, "
    "reuse an existing location (from search_items or list_locations), or "
    "call create_location for a new room, closet, cabinet, drawer, shelf, tray, "
    "bag, organizer, box, pouch, or bin, then "
    "put_item. To relocate an existing item, call move_item; do not use "
    "put_item for existing inventory moves because that duplicates records. "
    "A request phrased as 'put away X' means the user has an additional physical "
    "X to store. When an exact existing item is found, propose put_item in its "
    "current home with quantity_mode='add' unless the user requests a different "
    "home. Default an unspecified put-away quantity to 1 each. Do not ask how "
    "many for an ordinary singular or unspecified put-away; quantity should only "
    "block when the user explicitly describes a bulk group whose count or unit "
    "materially changes the record. "
    "When the user explicitly names a destination location id, use that exact "
    "destination even when the item already has a different existing home. "
    "If the user only describes an item already stored in the requested "
    "destination, do not propose put_item again. A put-away command is the "
    "explicit add-more case described above. If an "
    "item already occupies a location matching a generic requested parent and "
    "capacity, do not shuffle it to an equivalent location. "
    "If the user says an item is gone, discarded, thrown away, used up, or "
    "should be removed from inventory, call search_items to identify the exact "
    "stored row, then propose remove_item. "
    "To relocate a container, call move_container. "
    "For consolidation requests like 'move all Allen keys and wrenches to a "
    "big bin', call plan_reorganization with each named family, create or "
    "choose a suitable large destination, then propose one move_item call for "
    "each matching inventory row. Include parent_id when the user names a "
    "storage place. If it is omitted, plan_reorganization may infer one shared "
    "storage root from the matched items; when it returns "
    "needs_parent_selection=true, ask which returned parent choice to use and "
    "do not propose writes yet. "
    "If a request names a place that does not exist yet, such as Attic, create "
    "that parent location before creating children inside it. "
    "If the user asks to rename a location or change what a location is for, "
    "propose update_location with the full corrected location fields you know. "
    "When the user gives usable inside dimensions for a storage location, include "
    "internal_dimensions with width_mm, depth_mm, and height_mm on create_location "
    "or update_location. Convert inches to millimeters before calling the tool. "
    "When the user gives usable physical dimensions for an item, pass canonical "
    "millimeters as dimensions on suggest_locations and put_item. Use length_mm, "
    "width_mm, and height_mm, and include the best size_class when known. "
    "If the user asks to archive, retire, or stop using a location, identify it "
    "with list_locations and propose archive_location. Archiving preserves the "
    "location subtree, inventory, rules, history, and hardware references but "
    "removes it from active placement choices. If the user asks to restore an "
    "archived location, call list_archived_locations and propose restore_location. "
    "Use capacity_class to avoid putting large tools in small-parts bins. Use "
    "purpose_tags and existing contents to keep related things together; for "
    "example, solder wick belongs in a solder pouch if it fits. "
    "If the user asks for a generic small bin/drawer in the main cabinet, use "
    "list_locations with parent_id='main-cabinet', capacity_class='small', "
    "and empty_only=true, then use an existing empty small cabinet bin "
    "(a B### location with item_count 0); "
    "do not create a new drawer/bin and do not choose an occupied B### bin "
    "unless the user explicitly names that bin. "
    "Do not merge distinct hardware just because one word overlaps: T-nuts, "
    "lead screw nuts, hex nuts, wood screws, and machine screws are different "
    "families unless the user explicitly says they belong together. "
    "Flat/plain washers, split lock washers, and toothed lock washers are "
    "distinct variants. Preserve the user's washer type and size; a generic "
    "washer label does not mean flat. If a photo does not clearly show the "
    "washer type, ask whether it is flat, split, or toothed before proposing "
    "a put-away. Never invent a subtype or alias to force a match. "
    "If the destination is ambiguous, the photo is unclear, or you are choosing "
    "between two plausible existing homes, ask one short clarifying question "
    "and return no write tools. "
    "If the user attaches a photo and asks to sort, catalog, split, or file "
    "things separately, identify distinct visible groups and propose one "
    "put_item call per group. Use approximate quantities when visible, keep "
    "labels broad enough to be useful (e.g. tactile switches, LEDs, ceramic "
    "capacitors, electrolytic capacitors, diodes), and use search_items or "
    "suggest_locations to route each group to the best existing related bin. "
    "Set needs_review=true on any put_item group whose identity, quantity, or "
    "destination is uncertain, and do not lump mixed electronics into one "
    "assorted item when the user asked for separate sorting. "
    "If the user asks what needs review, call list_review_items. If the user "
    "explicitly accepts a reviewed inventory record, propose approve_review_item "
    "with its item_id and location_id. Approval changes durable review state and "
    "must be confirmed. Do not approve an ambiguous record. "
    "IMPORTANT: when a request needs several steps, emit ALL the tool calls in "
    "ONE response so they are confirmed together — e.g. create_location AND "
    "put_item in the same turn. A new location's id is its label lowercased with "
    "spaces as hyphens (e.g. 'Blue Pouch' -> 'blue-pouch'); use that id as the "
    "location_id when putting an item into a location you are creating in the "
    "same turn. If the user provides a 'Current proposed plan JSON' plus a "
    "follow-up, answer questions directly and return no write tools when the "
    "plan should not change. When the plan does change, return the full "
    "revised plan, not only the delta. Preserve unchanged steps, apply the "
    "follow-up as corrections or extra detail, and do not treat the follow-up "
    "text itself as a new inventory item. Never replace a move_item or put_item "
    "step with remove_item unless the user's follow-up explicitly asks to remove, "
    "discard, or delete that physical item. "
    "If the user says they call an existing item by another name, such as "
    "'I call M4 T-nuts tee nuts' or 'those are also called jumper wires', call "
    "search_items to identify the item, then propose learn_alias. "
    "If the user asks to attach an uploaded photo to an item or location, "
    "propose attach_photo with object_type item or location, the object id, "
    "and the photo_id. "
    "If the user asks what rules you know, call list_rules. If the user asks "
    "what is in a location, what a bin/drawer is for, or whether a location "
    "has children, photos, lights, or hardware, call get_location. If the user "
    "asks why a destination was chosen, call explain_location_choice. If the user "
    "asks to forget a learned rule, call list_rules first if the exact rule id "
    "and type are not already clear, then propose forget_rule. If the user "
    "asks to change an existing learned placement or size rule by id, propose "
    "update_rule or update_size_rule with the full corrected rule fields. "
    "If the user states a durable preference like 'I always keep jewelry in "
    "the main cabinet', propose learn_rule with clear trigger_terms and the "
    "target location. If the target place does not exist yet, include "
    "create_location before learn_rule. "
    "If the user wants to rename an item or correct its category/description, "
    "propose update_item. When renaming an item, keep the old name as an alias "
    "unless the user explicitly says not to. "
    "If the user states a durable size rule like 'jewelry boxes are medium' "
    "or 'filament spools are large', propose learn_size_rule. If the user "
    "corrects the size of a specific item, propose update_item_size. "
    "If the user says an exact item belongs somewhere, has a usual home, or "
    "should always go in one specific location, search_items to identify it "
    "and propose update_item_home. Use learn_rule for a whole category or "
    "family preference, and update_item_home for one exact item. "
    "If the user asks for a printable label sheet, QR labels, or labels for "
    "a whole cabinet/container, call preview_label_sheet so they get an "
    "immediate sheet link. If they ask for labels for a specific list of "
    "locations, call preview_label_batch with those location_ids. If the user "
    "asks to print one physical location "
    "label, call get_printer_status first. Only propose print_label with the "
    "location_id when the printer reports ok; otherwise say the printer is "
    "unavailable. Printing consumes "
    "physical media and must be confirmed. "
    "If the user asks to light, illuminate, show, or identify a specific storage "
    "location, call light_location. If the user asks to turn off or clear the "
    "indicator lights, call clear_lights. These temporary hardware actions happen "
    "immediately and do not require confirmation. Never say lighting is unavailable "
    "without calling the appropriate tool first. "
    "create_location, update_location, archive_location, restore_location, put_item, move_item, remove_item, move_container, attach_photo, approve_review_item, learn_alias, learn_rule, learn_size_rule, "
    "update_rule, update_size_rule, update_item, update_item_size, update_item_home, forget_rule, and "
    "print_label are PROPOSALS the user must confirm; preview_label_sheet and preview_label_batch are "
    "a read tool and does not need confirmation. "
    "never claim they already happened. Use plain text with no Markdown. For a "
    "find, say the item and physical path. For a proposed change, state the "
    "action and destination only. Do not narrate placement reasoning unless the "
    "user asks why. Keep replies short."
)

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_items",
            "description": "Find where an item is stored.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_locations",
            "description": "List a bounded set of storage locations using exact filters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "capacity_class": {"type": "string"},
                    "empty_only": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_storage_roots",
            "description": (
                "List every top-level room, area, cabinet, or other storage root, "
                "including descendant location and item counts."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_archived_locations",
            "description": (
                "List archived storage locations that can be restored, including "
                "their archived subtree and direct item counts."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_locations",
            "description": (
                "Rank likely storage locations for an item using learned homes, "
                "capacity, purpose tags, notes, and similar contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "dimensions": {
                        "type": "object",
                        "properties": {
                            "length_mm": {"type": "number"},
                            "width_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                            "thread_diameter_mm": {"type": "number"},
                        },
                    },
                },
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_location",
            "description": (
                "Inspect one storage location, including path, child locations, "
                "direct items, subtree contents, photos, nearest lit ancestor, "
                "and hardware bindings."
            ),
            "parameters": {
                "type": "object",
                "properties": {"location_id": {"type": "string"}},
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_rules",
            "description": "List learned placement and size rules, optionally filtered by a query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_location_choice",
            "description": "Explain why a location is or is not a good choice for an item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "location_id": {"type": "string"},
                    "dimensions": {
                        "type": "object",
                        "properties": {
                            "length_mm": {"type": "number"},
                            "width_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                            "thread_diameter_mm": {"type": "number"},
                        },
                    },
                },
                "required": ["item", "location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_location",
            "description": "Propose a new physical storage place or container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": list(LOCATION_TYPES),
                    },
                    "label": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "capacity_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                    "purpose_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "placement_notes": {"type": "string"},
                    "accepts_items": {
                        "type": "boolean",
                        "description": "Whether inventory can be stored directly at this node.",
                    },
                    "internal_dimensions": {
                        "type": "object",
                        "properties": {
                            "width_mm": {"type": "number"},
                            "depth_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                        },
                    },
                },
                "required": ["type", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "put_item",
            "description": "Propose storing an item in a location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "location_id": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string"},
                    "size_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                    "dimensions": {
                        "type": "object",
                        "properties": {
                            "length_mm": {"type": "number"},
                            "width_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                            "thread_diameter_mm": {"type": "number"},
                        },
                    },
                    "quantity_mode": {
                        "type": "string",
                        "enum": ["add", "set"],
                        "description": (
                            "Use add when putting away more of an item already "
                            "stored in the destination; otherwise omit it."
                        ),
                    },
                    "needs_review": {
                        "type": "boolean",
                        "description": "True when the item group is uncertain and should enter the review queue after confirmation.",
                    },
                },
                "required": ["item", "location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_location",
            "description": "Propose renaming a location or changing its capacity, purpose tags, or placement notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location_id": {"type": "string"},
                    "label": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": list(LOCATION_TYPES),
                    },
                    "capacity_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                    "purpose_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "placement_notes": {"type": "string"},
                    "accepts_items": {
                        "type": "boolean",
                        "description": "Whether inventory can be stored directly at this node.",
                    },
                    "internal_dimensions": {
                        "type": "object",
                        "properties": {
                            "width_mm": {"type": "number"},
                            "depth_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                        },
                    },
                },
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_location",
            "description": "Propose archiving a location subtree without deleting its records.",
            "parameters": {
                "type": "object",
                "properties": {"location_id": {"type": "string"}},
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_location",
            "description": "Propose restoring an archived location subtree to active use.",
            "parameters": {
                "type": "object",
                "properties": {"location_id": {"type": "string"}},
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_container",
            "description": "Propose moving a location to a new parent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location_id": {"type": "string"},
                    "new_parent_id": {"type": "string"},
                },
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_item",
            "description": "Propose moving an existing inventory item from one location to another.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "item": {"type": "string"},
                    "from_location_id": {"type": "string"},
                    "to_location_id": {"type": "string"},
                },
                "required": ["from_location_id", "to_location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_item",
            "description": "Propose removing an existing inventory item from a storage location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "item": {"type": "string"},
                    "location_id": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_photo",
            "description": "Propose attaching an uploaded photo to an existing item or location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "photo_id": {"type": "integer"},
                    "object_type": {
                        "type": "string",
                        "enum": ["item", "location"],
                    },
                    "object_id": {
                        "description": "Item id/name or location id/label.",
                    },
                },
                "required": ["photo_id", "object_type", "object_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_rule",
            "description": (
                "Propose a durable user placement rule, such as jewelry belongs "
                "in the main cabinet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_text": {"type": "string"},
                    "trigger_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "target_location_id": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["rule_text", "trigger_terms", "target_location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_alias",
            "description": "Propose teaching a new search alias for an existing item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "item": {"type": "string"},
                    "alias": {"type": "string"},
                },
                "required": ["alias"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_size_rule",
            "description": (
                "Propose a durable size rule, such as filament spools are large."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_text": {"type": "string"},
                    "trigger_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "size_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["rule_text", "trigger_terms", "size_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_rule",
            "description": "Propose editing an existing learned placement rule by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "rule_text": {"type": "string"},
                    "trigger_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "target_location_id": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["rule_id", "rule_text", "trigger_terms", "target_location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_size_rule",
            "description": "Propose editing an existing learned size rule by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "rule_text": {"type": "string"},
                    "trigger_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "size_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["rule_id", "rule_text", "trigger_terms", "size_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_item_size",
            "description": "Propose updating the size class for one specific item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "size_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["item", "size_class"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_item_home",
            "description": "Propose updating the durable home location for one exact item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "item": {"type": "string"},
                    "location_id": {"type": "string"},
                },
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_item",
            "description": "Propose editing an existing item's name, category, description, or aliases.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "item": {"type": "string"},
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_rule",
            "description": "Propose deactivating a learned placement or size rule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "rule_type": {
                        "type": "string",
                        "enum": ["placement", "size"],
                    },
                },
                "required": ["rule_id", "rule_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_review_items",
            "description": "List inventory records that need user review.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_review_item",
            "description": "Propose accepting one reviewed inventory record as correct.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "location_id": {"type": "string"},
                },
                "required": ["item_id", "location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_printer_status",
            "description": (
                "Check whether the configured physical label printer is available."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "light_location",
            "description": (
                "Immediately light a storage location or its nearest wired ancestor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location_id": {"type": "string"},
                    "color": {
                        "type": "string",
                        "enum": [
                            "white",
                            "red",
                            "green",
                            "blue",
                            "yellow",
                            "cyan",
                            "magenta",
                        ],
                    },
                    "seconds": {"type": "number"},
                },
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_lights",
            "description": "Immediately turn off all storage indicator lights.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "print_label",
            "description": "Propose printing a physical label for one storage location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location_id": {"type": "string"},
                },
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_label_sheet",
            "description": (
                "Return a printable QR label sheet link for a location and its "
                "subtree without sending anything to the physical printer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location_id": {"type": "string"},
                },
                "required": ["location_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_label_batch",
            "description": (
                "Preview printable QR labels for an explicit list of storage "
                "locations without sending anything to a physical printer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["location_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_reorganization",
            "description": (
                "Build a read-only brief for moving all matching inventory "
                "items into an existing or new destination. Use this before "
                "bulk move_item proposals. Omit parent_id only when it is safe "
                "to infer the storage root from the matching items."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "destination_label": {"type": "string"},
                    "destination_id": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "capacity_class": {
                        "type": "string",
                        "enum": ["tiny", "small", "medium", "large", "oversize"],
                    },
                },
                "required": ["queries"],
            },
        },
    },
]

READ_TOOL_NAMES = {
    "search_items",
    "list_locations",
    "list_storage_roots",
    "list_archived_locations",
    "suggest_locations",
    "get_location",
    "list_rules",
    "list_review_items",
    "explain_location_choice",
    "preview_label_sheet",
    "preview_label_batch",
    "get_printer_status",
    "light_location",
    "clear_lights",
    "plan_reorganization",
}
WRITE_TOOL_NAMES = {
    "create_location",
    "update_location",
    "archive_location",
    "restore_location",
    "put_item",
    "move_item",
    "remove_item",
    "move_container",
    "attach_photo",
    "approve_review_item",
    "learn_alias",
    "learn_rule",
    "learn_size_rule",
    "update_rule",
    "update_size_rule",
    "update_item",
    "update_item_size",
    "update_item_home",
    "forget_rule",
    "print_label",
}


@dataclass
class AgentResult:
    message: str
    plan: list[dict] = field(default_factory=list)  # proposed writes, await confirm
    trace: list[dict] = field(default_factory=list)  # read tool calls + results
    usage: dict[str, int] = field(default_factory=dict)


def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug or "location"


def _plan_reorganization(
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> dict[str, object]:
    raw_queries = args.get("queries") or []
    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]
    queries = [str(query).strip() for query in raw_queries if str(query).strip()]
    requested_destination_id = str(args.get("destination_id") or "").strip()
    destination_id = requested_destination_id
    destination_label = str(
        args.get("destination_label") or destination_id or "Consolidated Bin"
    ).strip()
    if not destination_id:
        destination_id = slugify(destination_label)
    requested_parent_id = str(args.get("parent_id") or "").strip()
    capacity_class = str(args.get("capacity_class") or "large").strip().lower()

    def query_variants(query: str) -> list[str]:
        variants = [query]
        words = [word for word in tokens(query) if len(word) >= 4]
        variants.extend(words)
        for word in words:
            if word.endswith("ies") and len(word) > 4:
                variants.append(word[:-3] + "y")
            elif word.endswith("es") and len(word) > 4:
                variants.append(word[:-2])
            elif word.endswith("s") and len(word) > 4:
                variants.append(word[:-1])
        return list(dict.fromkeys(variant for variant in variants if variant))

    matches: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for query in queries:
        for variant in query_variants(query):
            for result in store.search(variant, limit=20):
                key = (result.item_id, result.bin_id)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "query": query,
                        "matched_variant": variant,
                        "item_id": result.item_id,
                        "item": result.name,
                        "from_location_id": result.bin_id,
                        "quantity": result.quantity,
                        "unit": result.unit,
                    }
                )

    def root_id_for(location_id: str) -> str:
        current = locations.get_any(location_id)
        seen_locations: set[str] = set()
        while current is not None and current.id not in seen_locations:
            seen_locations.add(current.id)
            if not current.parent_id:
                return current.id
            current = locations.get_any(current.parent_id)
        return ""

    matching_root_ids = sorted(
        {
            root_id
            for match in matches
            if (root_id := root_id_for(str(match["from_location_id"])))
        }
    )
    active_roots = {
        location.id: location
        for location in locations.all()
        if not location.parent_id
    }
    parent_id = requested_parent_id
    parent_source = "explicit" if parent_id else ""
    existing_destination = locations.get(destination_id)
    if (
        not parent_id
        and requested_destination_id
        and existing_destination is not None
    ):
        parent_id = str(existing_destination.parent_id or "")
        parent_source = "existing_destination"
    elif not parent_id and len(matching_root_ids) == 1:
        parent_id = matching_root_ids[0]
        parent_source = "matching_items"
    elif not parent_id and matches and len(matching_root_ids) > 1:
        parent_choices = [
            {
                "id": root_id,
                "label": active_roots[root_id].label,
                "type": active_roots[root_id].type,
                "path": [
                    {
                        "id": root_id,
                        "label": active_roots[root_id].label,
                    }
                ],
                "selection_command": (
                    f"Use {active_roots[root_id].label} for the new "
                    f"{destination_label}"
                ),
            }
            for root_id in matching_root_ids
            if root_id in active_roots
        ]
        return {
            "queries": queries,
            "destination_id": destination_id,
            "destination_label": destination_label,
            "destination_exists": False,
            "parent_id": "",
            "parent_source": "",
            "parent_choices": parent_choices,
            "matching_root_ids": matching_root_ids,
            "needs_parent_selection": True,
            "capacity_class": capacity_class,
            "matches": matches,
            "proposed_steps": [],
            "move_count": 0,
            "needs_confirmation": False,
        }
    elif not parent_id and len(active_roots) == 1:
        parent_id = next(iter(active_roots))
        parent_source = "only_storage_root"

    existing_destination = locations.get(destination_id)
    if (
        existing_destination is not None
        and parent_id
        and existing_destination.parent_id != parent_id
    ):
        destination_id = slugify(f"{parent_id} {destination_label}")
        existing_destination = locations.get(destination_id)
    destination_exists = existing_destination is not None
    move_steps = [
        {
            "tool": "move_item",
            "arguments": {
                "item_id": match["item_id"],
                "item": match["item"],
                "from_location_id": match["from_location_id"],
                "to_location_id": destination_id,
            },
        }
        for match in matches
        if match["from_location_id"] != destination_id
    ]

    proposed_steps: list[dict[str, object]] = []
    if not destination_exists:
        proposed_steps.append(
            {
                "tool": "create_location",
                "arguments": {
                    "type": "bin",
                    "label": destination_label,
                    "parent_id": parent_id,
                    "capacity_class": capacity_class,
                },
            }
        )
    proposed_steps.extend(move_steps)
    return {
        "queries": queries,
        "destination_id": destination_id,
        "destination_label": destination_label,
        "destination_exists": destination_exists,
        "parent_id": parent_id,
        "parent_source": parent_source,
        "parent_choices": [],
        "matching_root_ids": matching_root_ids,
        "needs_parent_selection": False,
        "capacity_class": capacity_class,
        "matches": matches,
        "proposed_steps": proposed_steps,
        "move_count": len(move_steps),
        "needs_confirmation": True,
    }


def _location_path_for_prompt(
    location_id: str,
    *,
    locations: LocationStore,
) -> list[dict[str, str]]:
    ancestry = []
    seen: set[str] = set()
    current = locations.get_any(location_id)
    while current is not None and current.id not in seen:
        ancestry.append(current)
        seen.add(current.id)
        current = locations.get_any(current.parent_id) if current.parent_id else None
    if not ancestry:
        return [{"id": location_id, "label": location_id}]
    return [
        {"id": loc.id, "label": loc.label or loc.id}
        for loc in reversed(ancestry)
    ]


def run_read_tool(
    name: str,
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> object:
    if name == "search_items":
        results = search_inventory(store, str(args.get("query", "")), limit=8)
        out: list[dict[str, object]] = []
        for r in results:
            try:
                location_id = _resolve_any_location_id(r.bin_id, locations)
            except ValueError:
                location_id = r.bin_id
            nearest_lit = locations.nearest_lit(location_id)
            stored_location = locations.get_any(location_id)
            out.append(
                {
                    "name": r.name,
                    "item_id": r.item_id,
                    "location_id": location_id,
                    "path": _location_path_for_prompt(location_id, locations=locations),
                    "nearest_lit_location_id": nearest_lit.id if nearest_lit else None,
                    "archived": bool(stored_location and not stored_location.active),
                    "quantity": r.quantity,
                    "unit": r.unit,
                    "size_class": r.size_class,
                }
            )
        return out
    if name == "list_locations":
        wanted_type = args.get("type")
        wanted_parent = args.get("parent_id")
        wanted_capacity = args.get("capacity_class")
        empty_only = bool(args.get("empty_only", False))
        try:
            limit = max(1, min(int(args.get("limit", 40)), 50))
        except (TypeError, ValueError):
            limit = 40
        item_counts = store.location_item_counts()
        out = []
        for loc in locations.all():
            if wanted_type and loc.type != wanted_type:
                continue
            if wanted_parent and loc.parent_id != wanted_parent:
                continue
            if wanted_capacity and loc.capacity_class != wanted_capacity:
                continue
            if empty_only and item_counts.get(loc.id, 0):
                continue
            out.append(
                {
                    "id": loc.id,
                    "type": loc.type,
                    "label": loc.label,
                    "parent_id": loc.parent_id,
                    "is_lit": loc.is_lit,
                    "capacity_class": loc.capacity_class,
                    "purpose_tags": loc.purpose_tags,
                    "placement_notes": loc.placement_notes,
                    "item_count": item_counts.get(loc.id, 0),
                    "path": [
                        {"id": node.id, "label": node.label or node.id}
                        for node in location_path(locations, loc.id)
                    ],
                }
            )
        out.sort(key=lambda row: str(row["id"]))
        return out[:limit]
    if name == "list_storage_roots":
        roots = [loc for loc in locations.all() if not loc.parent_id]
        out = []
        for root in roots:
            subtree_ids = locations.subtree_ids(root.id)
            items = store.items_in_locations(subtree_ids)
            out.append(
                {
                    "id": root.id,
                    "type": root.type,
                    "label": root.label,
                    "location_count": len(subtree_ids),
                    "item_count": len({item.item_id for item in items}),
                    "occupied_location_count": len({item.bin_id for item in items}),
                    "nearest_lit_location_id": (
                        locations.nearest_lit(root.id).id
                        if locations.nearest_lit(root.id) is not None
                        else None
                    ),
                }
            )
        return out
    if name == "list_archived_locations":
        item_counts = store.location_item_counts()
        archived = [
            loc for loc in locations.all(include_archived=True) if not loc.active
        ]
        archived_ids = {loc.id for loc in archived}
        out = []
        for loc in archived:
            if loc.parent_id in archived_ids:
                continue
            subtree_ids = locations.subtree_ids(loc.id, include_archived=True)
            out.append(
                {
                    "id": loc.id,
                    "type": loc.type,
                    "label": loc.label,
                    "parent_id": loc.parent_id,
                    "archived_at": loc.archived_at,
                    "location_count": len(subtree_ids),
                    "direct_item_count": item_counts.get(loc.id, 0),
                }
            )
        return out
    if name == "suggest_locations":
        dimensions = normalize_item_dimensions(args.get("dimensions"))
        return [
            suggestion.as_dict()
            for suggestion in suggest_locations(
                str(args.get("item", "")),
                store=store,
                locations=locations,
                limit=8,
                item_dimensions=dimensions or None,
            )
        ]
    if name == "get_location":
        raw_location_id = str(args.get("location_id", ""))
        try:
            location_id = _resolve_location_id(raw_location_id, locations)
        except ValueError as exc:
            candidates = match_locations(raw_location_id, locations, active=True)
            return {
                "found": False,
                "location_id": raw_location_id,
                "error": str(exc),
                "candidates": [
                    {
                        "id": candidate.id,
                        "label": candidate.label or candidate.id,
                        "path": [
                            {"id": node.id, "label": node.label or node.id}
                            for node in location_path(locations, candidate.id)
                        ],
                    }
                    for candidate in candidates
                ],
            }
        loc = locations.get(location_id)
        if loc is None:
            return {
                "found": False,
                "location_id": raw_location_id,
                "error": "unknown location",
            }
        ancestry = list(reversed(locations.ancestors(location_id)))
        children = locations.children(location_id)
        subtree_ids = locations.subtree_ids(location_id)
        nearest_lit = locations.nearest_lit(location_id)
        return {
            "found": True,
            "location": {
                "id": loc.id,
                "type": loc.type,
                "label": loc.label,
                "parent_id": loc.parent_id,
                "capacity_class": loc.capacity_class,
                "purpose_tags": list(loc.purpose_tags),
                "placement_notes": loc.placement_notes,
                "is_lit": loc.is_lit,
            },
            "path": [{"id": node.id, "label": node.label} for node in ancestry],
            "children": [
                {
                    "id": child.id,
                    "type": child.type,
                    "label": child.label,
                    "capacity_class": child.capacity_class,
                    "is_lit": child.is_lit,
                }
                for child in children[:25]
            ],
            "child_count": len(children),
            "items": [item.as_dict() for item in store.items_in_location(location_id)],
            "contents": [
                item.as_dict() for item in store.items_in_locations(subtree_ids)
            ],
            "subtree_count": len(subtree_ids),
            "nearest_lit_location_id": nearest_lit.id if nearest_lit else None,
            "photos": [
                photo.as_dict() for photo in store.location_photos(location_id)
            ],
            "hardware_bindings": [
                binding.as_dict()
                for binding in locations.hardware_bindings(location_id)
            ],
        }
    if name == "plan_reorganization":
        return _plan_reorganization(args, store=store, locations=locations)
    if name == "list_rules":
        query = str(args.get("query", "")).strip()
        placement_rules = (
            store.matching_rules(query, limit=20)
            if query
            else store.rules(active_only=True, limit=20)
        )
        size_rules = (
            store.matching_size_rules(query, limit=20)
            if query
            else store.size_rules(active_only=True, limit=20)
        )
        placement_payloads = []
        for rule in placement_rules:
            target = locations.get_any(rule.target_location_id)
            target_path = _location_path_for_prompt(
                rule.target_location_id,
                locations=locations,
            )
            placement_payloads.append(
                {
                    "rule_type": "placement",
                    **rule.as_dict(),
                    "target_path": target_path,
                    "target_path_label": " > ".join(
                        node["label"] for node in target_path
                    ),
                    "target_active": bool(target and target.active),
                    "target_archived": bool(target and not target.active),
                }
            )
        conflicts = store.rule_conflicts(query, limit=20)
        for conflict in conflicts:
            if conflict.get("rule_type") != "placement":
                continue
            target_paths = [
                _location_path_for_prompt(str(target), locations=locations)
                for target in conflict.get("targets", [])
            ]
            conflict["target_paths"] = target_paths
            conflict["target_path_labels"] = [
                " > ".join(node["label"] for node in path)
                for path in target_paths
            ]
        return {
            "placement_rules": placement_payloads,
            "size_rules": [
                {"rule_type": "size", **rule.as_dict()}
                for rule in size_rules
            ],
            "conflicts": conflicts,
        }
    if name == "list_review_items":
        return {
            "count": len(store.review_queue()),
            "items": [item.as_dict() for item in store.review_queue()[:50]],
        }
    if name == "explain_location_choice":
        item = str(args.get("item", ""))
        location_id = str(args.get("location_id", ""))
        dimensions = normalize_item_dimensions(args.get("dimensions"))
        suggestions = suggest_locations(
            item,
            store=store,
            locations=locations,
            limit=20,
            item_dimensions=dimensions or None,
        )
        for suggestion in suggestions:
            if suggestion.location.id == location_id:
                return {
                    "item": item,
                    "location_id": location_id,
                    "path": _location_path_for_prompt(
                        location_id,
                        locations=locations,
                    ),
                    "score": suggestion.score,
                    "reasons": list(suggestion.reasons),
                    "contents": list(suggestion.contents),
                }
        loc = locations.get(location_id)
        return {
            "item": item,
            "location_id": location_id,
            "found": loc is not None,
            "reasons": ["not in the top ranked placement candidates"],
        }
    if name == "preview_label_sheet":
        raw_location_id = str(args.get("location_id", ""))
        try:
            location_id = _resolve_parent_id(raw_location_id, locations)
        except ValueError:
            location_id = None
        loc = locations.get(location_id) if location_id else None
        if loc is None:
            return {
                "found": False,
                "location_id": raw_location_id,
                "error": "unknown location",
            }
        subtree_ids = locations.subtree_ids(location_id)
        return {
            "found": True,
            "location_id": loc.id,
            "label": loc.label or loc.id,
            "url": f"/locations/{loc.id}/labels.html",
            "label_count": len(subtree_ids),
        }
    if name == "preview_label_batch":
        raw_ids = args.get("location_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        labels: list[dict[str, object]] = []
        missing: list[str] = []
        seen: set[str] = set()
        for raw_location_id in raw_ids:
            raw_text = str(raw_location_id or "").strip()
            if not raw_text:
                continue
            try:
                location_id = _resolve_location_id(raw_text, locations)
            except ValueError:
                missing.append(raw_text)
                continue
            if location_id in seen:
                continue
            seen.add(location_id)
            loc = locations.get(location_id)
            if loc is None:
                missing.append(raw_text)
                continue
            path = list(reversed(locations.ancestors(location_id)))
            subtree_ids = locations.subtree_ids(location_id)
            labels.append(
                {
                    "location_id": loc.id,
                    "label": loc.label or loc.id,
                    "type": loc.type,
                    "path": [
                        {"id": node.id, "label": node.label or node.id}
                        for node in path
                    ],
                    "url": f"/l/{loc.id}",
                    "label_sheet_url": f"/locations/{loc.id}/labels.html",
                    "pdf_url": f"/locations/{loc.id}/labels.pdf",
                    "subtree_count": len(subtree_ids),
                    "item_count": len(store.items_in_locations(subtree_ids)),
                }
            )
        return {
            "found": bool(labels),
            "label_count": len(labels),
            "labels": labels[:50],
            "missing_location_ids": missing,
            "needs_confirmation": False,
        }
    if name == "get_printer_status":
        return {
            "ok": False,
            "error": "printer status is not connected to this agent runtime",
        }
    if name in {"light_location", "clear_lights"}:
        return {
            "ok": False,
            "lit": False,
            "error": "lighting is not connected to this agent runtime",
        }
    return {"error": f"unknown read tool: {name}"}


# Backward-compatible internal alias while callers migrate to the shared tool service.
_run_read_tool = run_read_tool


def run_agent(
    text: str,
    *,
    store: InventoryStore,
    locations: LocationStore,
    client: LlmClient,
    images: list[str] | None = None,
    examples: list[dict] | None = None,
    conversation_history: list[dict] | None = None,
    read_tool_runner: Callable[[str, dict], object] | None = None,
    allowed_tools: set[str] | frozenset[str] | None = None,
    max_steps: int = 4,
) -> AgentResult:
    user_content: object
    if images:
        user_content = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in images
        ]
    else:
        user_content = text
    permitted_tools = (
        READ_TOOL_NAMES | WRITE_TOOL_NAMES
        if allowed_tools is None
        else (READ_TOOL_NAMES | WRITE_TOOL_NAMES).intersection(allowed_tools)
    )
    permitted_write_tools = WRITE_TOOL_NAMES.intersection(permitted_tools)
    tools = [
        schema
        for schema in TOOL_SCHEMAS
        if schema["function"]["name"] in permitted_tools
    ]
    messages: list[dict] = [{"role": "system", "content": AGENT_INSTRUCTIONS}]
    if allowed_tools is not None:
        messages.append(
            {
                "role": "system",
                "content": (
                    "This input adapter is restricted to daily retrieval and put-away. "
                    "You may only propose these writes: "
                    f"{', '.join(sorted(permitted_write_tools)) or 'none'}. "
                    "Never create, rename, move, archive, restore, or otherwise configure "
                    "storage places through this adapter. Use existing locations or tell "
                    "the user to open Places."
                ),
            }
        )
    if context := placement_context(text, store=store, locations=locations):
        messages.append({"role": "system", "content": context})
    if examples:
        lines = [_format_example_for_prompt(ex) for ex in examples]
        messages.append(
            {
                "role": "system",
                "content": "Learn from how the user handled similar requests "
                "before:\n" + "\n".join(lines),
            }
        )
    if conversation_history:
        history_lines: list[str] = []
        for interaction in conversation_history[-6:]:
            command = str(interaction.get("command") or "").strip()
            response = interaction.get("response")
            response = response if isinstance(response, dict) else {}
            message = str(response.get("message") or "").strip()
            action = str(response.get("action") or "").strip()
            plan = response.get("plan")
            status = str(interaction.get("status") or "").strip()
            if command:
                history_lines.append(f"User: {command[:500]}")
            if message:
                history_lines.append(f"Magpie: {message[:500]}")
            summary = {"action": action, "status": status}
            if isinstance(plan, list) and plan:
                summary["plan"] = plan[:8]
            if any(summary.values()):
                history_lines.append(
                    "State: " + json.dumps(summary, separators=(",", ":"))[:1200]
                )
        if history_lines:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Recent Magpie conversation, oldest to newest. Use it to "
                        "resolve pronouns and corrections, but inspect live tools "
                        "before assuming inventory state:\n" + "\n".join(history_lines)
                    ),
                }
            )
    messages.append({"role": "user", "content": user_content})
    trace: list[dict] = []
    usage = {field_name: 0 for field_name in LLM_USAGE_FIELDS}
    completed_reads: set[str] = set()

    for _ in range(max_steps):
        result = client.chat(messages=messages, tools=tools)
        add_llm_usage(usage, normalize_llm_usage(result.raw))
        if not result.tool_calls:
            if recovered := _recover_required_hardware_action(
                text,
                trace=trace,
                locations=locations,
                read_tool_runner=read_tool_runner,
            ):
                recovered.usage = usage
                return recovered
            if adopted_plan := _proposed_plan_from_trace(trace):
                return AgentResult(
                    message=result.text, plan=adopted_plan, trace=trace, usage=usage
                )
            return AgentResult(message=result.text, plan=[], trace=trace, usage=usage)

        assistant_message: dict[str, object] = {
            "role": "assistant",
            "content": result.text or None,
            "tool_calls": result.tool_calls,
        }
        if result.raw.get("object") == "response":
            assistant_message["responses_output"] = result.raw.get("output")
        messages.append(assistant_message)

        plan: list[dict] = []
        write_call_ids: list[str] = []
        read_signatures: list[str] = []
        blocked_write_tools: list[str] = []
        for call in result.tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name in WRITE_TOOL_NAMES:
                if name not in permitted_write_tools:
                    blocked_write_tools.append(name)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": json.dumps(
                                {
                                    "accepted": False,
                                    "error": (
                                        f"{name} is not available through this input adapter."
                                    ),
                                    "instruction": (
                                        "Use an existing storage destination or direct the "
                                        "user to the visual Places manager."
                                    ),
                                }
                            ),
                        }
                    )
                    continue
                plan.append({"tool": name, "arguments": args})
                write_call_ids.append(str(call.get("id") or ""))
            elif name in READ_TOOL_NAMES:
                if name not in permitted_tools:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "error": (
                                        f"{name} is not available through this input adapter."
                                    ),
                                }
                            ),
                        }
                    )
                    continue
                signature = json.dumps(
                    {"tool": name, "arguments": args},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                read_signatures.append(signature)
                if signature in completed_reads:
                    output = {
                        "ok": False,
                        "error": "This exact storage read was already completed in this turn.",
                    }
                else:
                    output = (
                        read_tool_runner(name, args)
                        if read_tool_runner is not None
                        else run_read_tool(name, args, store=store, locations=locations)
                    )
                trace.append({"tool": name, "arguments": args, "result": output})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(output),
                    }
                )

        if blocked_write_tools:
            reason = (
                "This input adapter cannot perform storage-place configuration: "
                + ", ".join(sorted(set(blocked_write_tools)))
            )
            trace.append(
                {
                    "tool": "input_policy_guard",
                    "arguments": {"blocked_tools": sorted(set(blocked_write_tools))},
                    "result": {"accepted": False, "reason": reason},
                }
            )
            for call_id in write_call_ids:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            {
                                "accepted": False,
                                "error": reason,
                                "instruction": (
                                    "Revise the entire plan using only existing locations."
                                ),
                            }
                        ),
                    }
                )
            continue

        if not plan and read_signatures and all(
            signature in completed_reads for signature in read_signatures
        ):
            return AgentResult(
                message=(
                    "I checked that storage information but could not make progress. "
                    "Please add one detail or rephrase the request."
                ),
                plan=[],
                trace=trace,
                usage=usage,
            )
        completed_reads.update(read_signatures)

        if plan:
            reason = _write_plan_rejection_reason(
                text,
                plan,
                store=store,
                locations=locations,
                conversation_history=conversation_history,
            ) or _printer_plan_rejection_reason(plan, trace)
            if reason:
                trace.append(
                    {
                        "tool": "plan_guard",
                        "arguments": {"plan": plan},
                        "result": {"accepted": False, "reason": reason},
                    }
                )
                rejection = json.dumps(
                    {
                        "accepted": False,
                        "error": reason,
                        "instruction": (
                            "Revise the plan using a non-destructive tool such as move_item, "
                            "or return no write tools and ask a clarifying question."
                        ),
                    }
                )
                for call_id in write_call_ids:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": rejection,
                        }
                    )
                continue
            return AgentResult(message=result.text, plan=plan, trace=trace, usage=usage)

    if adopted_plan := _proposed_plan_from_trace(trace):
        return AgentResult(
            message="I found a reorganization plan for you to confirm.",
            plan=adopted_plan,
            trace=trace,
            usage=usage,
        )
    return AgentResult(
        message="I could not complete that in a few steps.",
        plan=[],
        trace=trace,
        usage=usage,
    )


def _recover_required_hardware_action(
    text: str,
    *,
    trace: list[dict],
    locations: LocationStore,
    read_tool_runner: Callable[[str, dict], object] | None,
) -> AgentResult | None:
    """Enforce explicit hardware commands when the model stops after inspection."""
    if read_tool_runner is None:
        return None
    cleaned = re.sub(r"\s+", " ", text).strip()
    clear_requested = bool(
        re.match(
            r"^(?:please\s+)?(?:can|could|would)\s+you\s+(?:turn\s+off|clear)\b|"
            r"^(?:please\s+)?(?:turn\s+off|clear)\s+(?:all\s+)?(?:the\s+)?lights?\b",
            cleaned,
            re.I,
        )
    )
    if clear_requested and not any(entry.get("tool") == "clear_lights" for entry in trace):
        output = read_tool_runner("clear_lights", {})
        trace.append({"tool": "clear_lights", "arguments": {}, "result": output})
        result = output if isinstance(output, dict) else {}
        message = str(result.get("message") or result.get("error") or "Lights cleared.")
        return AgentResult(message=message, plan=[], trace=trace)

    light_requested = bool(
        re.match(
            r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:light|illuminate)\b|"
            r"^(?:please\s+)?show\s+me\s+(?:location\s+)?b[ -]?0*\d{1,3}\b",
            cleaned,
            re.I,
        )
    )
    if not light_requested or any(entry.get("tool") == "light_location" for entry in trace):
        return None

    location_id = ""
    for entry in reversed(trace):
        if entry.get("tool") != "get_location":
            continue
        result = entry.get("result")
        if not isinstance(result, dict) or not result.get("found"):
            continue
        location = result.get("location")
        if isinstance(location, dict):
            location_id = str(location.get("id") or "")
        location_id = location_id or str(result.get("location_id") or "")
        if location_id:
            break
    if not location_id:
        match = re.search(r"\bb[ -]?0*(\d{1,3})\b", cleaned, re.I)
        if match:
            candidate = normalize_bin_id(match.group(1))
            if locations.get(candidate) is not None:
                location_id = candidate
    if not location_id:
        return None

    color_match = re.search(
        r"\b(white|red|green|blue|yellow|cyan|magenta)\b", cleaned, re.I
    )
    arguments = {"location_id": location_id}
    if color_match:
        arguments["color"] = color_match.group(1).lower()
    output = read_tool_runner("light_location", arguments)
    trace.append({"tool": "light_location", "arguments": arguments, "result": output})
    result = output if isinstance(output, dict) else {}
    lit_location = str(result.get("lit_location") or location_id)
    message = str(
        result.get("message")
        or result.get("error")
        or (f"Lit {lit_location}." if result.get("lit") else f"Could not light {location_id}.")
    )
    return AgentResult(message=message, plan=[], trace=trace)


def proposed_plan_rejection_reason(
    text: str,
    plan: list[dict],
    *,
    store: InventoryStore,
    locations: LocationStore,
    conversation_history: list[dict] | None = None,
) -> str:
    explicit_put_destinations = {
        normalize_bin_id(match.group(1).replace(" ", ""))
        for match in re.finditer(
            r"\b(?:in|into|to)\s+(B\s*\d{1,4})\b",
            text,
            re.IGNORECASE,
        )
    }
    if len(explicit_put_destinations) == 1:
        expected_destination = next(iter(explicit_put_destinations))
        for step in plan:
            if step.get("tool") != "put_item":
                continue
            arguments = step.get("arguments")
            if not isinstance(arguments, dict):
                continue
            actual_destination = normalize_bin_id(
                str(arguments.get("location_id") or "")
            )
            if actual_destination and actual_destination != expected_destination:
                return (
                    f"the user explicitly requested {expected_destination}; "
                    f"do not replace it with {actual_destination}"
                )

    followup_text = _revision_followup_text(text)
    pending_items = _revision_plan_items(text) or _pending_plan_items(
        conversation_history
    )
    refers_to_pending_item = bool(
        pending_items
        and re.search(
            r"\b(?:it|that|them|those|these)\b",
            followup_text,
            re.IGNORECASE,
        )
    )
    if refers_to_pending_item:
        expected = {item.casefold() for item in pending_items}
        for step in plan:
            if step.get("tool") not in {"put_item", "move_item"}:
                continue
            arguments = step.get("arguments")
            if not isinstance(arguments, dict):
                continue
            proposed_item = str(arguments.get("item") or "").strip()
            if proposed_item and proposed_item.casefold() not in expected:
                return (
                    f"the follow-up refers to the pending item ({', '.join(pending_items)}); "
                    f"do not replace it with {proposed_item} from the destination wording"
                )

    if any(step.get("tool") == "remove_item" for step in plan):
        intent_text = text
        followup = re.search(
            r"User follow-up:\s*(.*?)(?:\n\n|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if followup:
            intent_text = followup.group(1)
        if not re.search(
            r"\b(?:remove|delete|discard|trash|throw|threw|toss|tossed|used up|gone|"
            r"no longer have)\b",
            intent_text,
            re.IGNORECASE,
        ):
            return "remove_item requires an explicit user request to remove or discard the item"

    additive_intent = bool(
        re.search(
            r"\b(?:add|adding|another|more|additional|extra|also|as well)\b",
            followup_text,
            re.IGNORECASE,
        )
    )
    for step in plan:
        tool = step.get("tool")
        args = step.get("arguments")
        if not isinstance(args, dict):
            continue
        if tool == "approve_review_item":
            try:
                review_item_id = int(args.get("item_id"))
                review_location_id = _resolve_location_id(
                    args.get("location_id"), locations
                )
            except (TypeError, ValueError):
                return "review approval requires a valid item_id and location_id"
            if not any(
                row.item_id == review_item_id
                and row.bin_id == review_location_id
                for row in store.review_queue()
            ):
                return (
                    f"item {review_item_id} in {review_location_id} is not awaiting review"
                )
            continue
        step_is_additive = str(args.get("quantity_mode") or "").lower() == "add"
        if tool == "put_item" and additive_intent and not step_is_additive:
            return (
                "the user is adding inventory; use quantity_mode='add' so the "
                "existing quantity is not overwritten"
            )
        if tool == "put_item" and not (additive_intent or step_is_additive):
            item = str(args.get("item") or "").strip()
            destination = str(args.get("location_id") or "").strip()
            if item and destination and any(
                row.bin_id.casefold() == destination.casefold()
                for row in store.exact_match(item)
            ):
                return f"{item} is already stored in {destination}; do not create a duplicate"
        if tool != "move_item":
            continue
        source_id = str(args.get("from_location_id") or "").strip()
        destination_id = str(args.get("to_location_id") or "").strip()
        if not source_id or not destination_id or destination_id.casefold() in text.casefold():
            continue
        source = locations.get(source_id)
        destination = locations.get(destination_id)
        if (
            source is not None
            and destination is not None
            and source.parent_id == destination.parent_id
            and source.capacity_class == destination.capacity_class
        ):
            return (
                f"{source_id} already matches the requested parent and capacity; "
                f"do not move the item to equivalent location {destination_id}"
            )
    return ""


_write_plan_rejection_reason = proposed_plan_rejection_reason


def _printer_plan_rejection_reason(plan: list[dict], trace: list[dict]) -> str:
    if not any(step.get("tool") == "print_label" for step in plan):
        return ""
    checks = [entry for entry in trace if entry.get("tool") == "get_printer_status"]
    if not checks:
        return "check get_printer_status before proposing a physical label print"
    status = checks[-1].get("result")
    if not isinstance(status, dict) or not status.get("ok"):
        detail = (
            str(status.get("error") or "printer unavailable")
            if isinstance(status, dict)
            else "printer unavailable"
        )
        return f"the physical label printer is unavailable: {detail}"
    return ""


def _pending_plan_items(conversation_history: list[dict] | None) -> list[str]:
    for interaction in reversed(conversation_history or []):
        if str(interaction.get("status") or "").strip().lower() != "pending":
            continue
        response = interaction.get("response")
        if not isinstance(response, dict):
            continue
        plan = response.get("plan")
        if not isinstance(plan, list):
            continue
        items: list[str] = []
        for step in plan:
            if not isinstance(step, dict):
                continue
            arguments = step.get("arguments")
            if not isinstance(arguments, dict):
                continue
            item = str(arguments.get("item") or "").strip()
            if item and item.casefold() not in {known.casefold() for known in items}:
                items.append(item)
        if items:
            return items
    return []


def _revision_followup_text(text: str) -> str:
    match = re.search(
        r"User follow-up:\s*(.*?)(?:\n\n|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else text


def _revision_plan_items(text: str) -> list[str]:
    match = re.search(
        r"Current proposed plan JSON:\s*(.*?)(?:\n\nUser follow-up:|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    try:
        plan = json.loads(match.group(1).strip())
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(plan, list):
        return []
    items: list[str] = []
    for step in plan:
        if not isinstance(step, dict):
            continue
        arguments = step.get("arguments")
        if not isinstance(arguments, dict):
            continue
        item = str(arguments.get("item") or "").strip()
        if item and item.casefold() not in {known.casefold() for known in items}:
            items.append(item)
    return items


def _proposed_plan_from_trace(trace: list[dict]) -> list[dict]:
    for entry in reversed(trace):
        if entry.get("tool") != "plan_reorganization":
            continue
        result = entry.get("result") or {}
        if not isinstance(result, dict):
            continue
        raw_steps = result.get("proposed_steps") or []
        if not isinstance(raw_steps, list):
            continue
        plan: list[dict] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, dict):
                continue
            tool = str(raw_step.get("tool") or "")
            args = raw_step.get("arguments") or {}
            if tool not in WRITE_TOOL_NAMES or not isinstance(args, dict):
                continue
            plan.append({"tool": tool, "arguments": dict(args)})
        if any(step["tool"] == "move_item" for step in plan):
            return plan
    return []


def _format_example_for_prompt(example: dict) -> str:
    command = example.get("command")
    action = example.get("action")
    kind = example.get("kind")
    if kind == "correction":
        return f"- command: {command} -> {action} (user correction - weight highly)"
    if kind == "review_correction":
        try:
            payload = json.loads(str(action or "{}"))
        except json.JSONDecodeError:
            payload = {}
        return (
            f"- review correction: {command}\n"
            f"  before: {json.dumps(payload.get('before', {}), sort_keys=True)}\n"
            f"  after: {json.dumps(payload.get('after', {}), sort_keys=True)}\n"
            "  For similar items, prefer the corrected name, quantity, category, aliases, and destination."
        )
    if kind == "review_approved":
        try:
            payload = json.loads(str(action or "{}"))
        except json.JSONDecodeError:
            payload = {}
        approved = payload.get("approved_review", {})
        if not isinstance(approved, dict):
            approved = {}
        return (
            f"- review approved: {command}\n"
            f"  accepted item: {json.dumps(approved, sort_keys=True)}\n"
            "  For similar items, this name, quantity, category, destination, and review reason were accepted by the user."
        )
    if kind in {"edited_photo_sort", "rejected_photo_sort"}:
        try:
            payload = json.loads(str(action or "{}"))
        except json.JSONDecodeError:
            payload = {}
        if kind == "edited_photo_sort":
            return (
                f"- photo-sort command: {command}\n"
                f"  model first proposed: {json.dumps(payload.get('original_plan', []), sort_keys=True)}\n"
                f"  user corrected to: {json.dumps(payload.get('corrected_plan', []), sort_keys=True)}\n"
                "  For similar photo sorts, prefer the corrected grouping, labels, quantities, and destinations."
            )
        return (
            f"- photo-sort command: {command}\n"
            f"  user rejected this plan: {json.dumps(payload.get('rejected_plan', []), sort_keys=True)}\n"
            "  For similar photo sorts, do not repeat that grouping or generic labeling."
        )
    return f"- command: {command} -> {action}"


def execute_plan(
    plan: list[dict],
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> list[dict]:
    """Apply a confirmed plan of write tool calls, in order."""
    normalized_plan = normalize_plan(plan, locations=locations)
    _preflight_plan(normalized_plan, store=store, locations=locations)
    applied: list[dict] = []
    created_location_aliases: dict[str, str] = {}

    def planned_location_id(value: object) -> object:
        if value is None:
            return None
        raw = str(value).strip()
        return created_location_aliases.get(raw, created_location_aliases.get(slugify(raw), value))

    for step in normalized_plan:
        tool = step.get("tool")
        args = dict(step.get("arguments", {}) or {})
        for key in (
            "location_id",
            "from_location_id",
            "to_location_id",
            "target_location_id",
            "new_parent_id",
        ):
            if key in args:
                args[key] = planned_location_id(args[key])
        if tool == "create_location":
            requested_id = slugify(str(args["label"]))
            loc_id = _available_location_id(requested_id, locations)
            parent_ref = planned_location_id(args.get("parent_id"))
            parent_id = _resolve_parent_id(parent_ref, locations)
            location_type = normalize_location_type(args.get("type", "bin"))
            locations.upsert(
                Location(
                    id=loc_id,
                    type=location_type,
                    parent_id=parent_id,
                    label=str(args["label"]),
                    capacity_class=str(args.get("capacity_class") or "medium"),
                    purpose_tags=[
                        str(tag)
                        for tag in args.get("purpose_tags", [])
                        if str(tag).strip()
                    ],
                    placement_notes=str(args.get("placement_notes") or ""),
                    internal_dimensions=normalize_internal_dimensions(
                        args.get("internal_dimensions")
                    ),
                    accepts_items=(
                        bool(args["accepts_items"])
                        if "accepts_items" in args
                        else default_accepts_items(location_type)
                    ),
                )
            )
            locations.record_event(
                event_type="location_created",
                location_id=loc_id,
                note=f"agent create {args.get('type', 'bin')}",
            )
            created_location_aliases[requested_id] = loc_id
            created_location_aliases[str(args["label"]).strip()] = loc_id
            applied.append({"created_location": loc_id})
        elif tool == "update_location":
            applied.append(_execute_update_location(args, locations=locations))
        elif tool == "archive_location":
            location_id = _resolve_location_id(args.get("location_id"), locations)
            archived_ids = locations.archive_subtree(location_id)
            locations.record_event(
                event_type="location_archived",
                location_id=location_id,
                quantity=float(len(archived_ids)),
                note="agent archive",
            )
            applied.append(
                {
                    "archived_location_id": location_id,
                    "archived_ids": archived_ids,
                }
            )
        elif tool == "restore_location":
            location_id = _resolve_any_location_id(args.get("location_id"), locations)
            current = locations.get_any(location_id)
            if current is None or current.active:
                raise ValueError(f"location is not archived: {location_id}")
            restored_ids = locations.restore_subtree(location_id)
            locations.record_event(
                event_type="location_restored",
                location_id=location_id,
                quantity=float(len(restored_ids)),
                note="agent restore",
            )
            applied.append(
                {
                    "restored_location_id": location_id,
                    "restored_ids": restored_ids,
                }
            )
        elif tool == "put_item":
            location_id = _resolve_location_id(args.get("location_id"), locations)
            destination = locations.get(location_id)
            if destination is not None and not destination.accepts_items:
                raise ValueError(f"location does not accept items directly: {location_id}")
            item_name = str(args["item"])
            existing_item = next(iter(store.exact_match(item_name)), None)
            item_id = store.upsert_item(name=item_name)
            previous = next(
                (
                    row
                    for row in store.exact_match(str(args["item"]))
                    if row.item_id == item_id
                    and row.bin_id.casefold() == location_id.casefold()
                ),
                None,
            )
            requested_quantity = float(args.get("quantity", 1))
            quantity = (
                float(previous.quantity) + requested_quantity
                if args.get("quantity_mode") == "add" and previous is not None
                else requested_quantity
            )
            store.put_inventory(
                item_id=item_id,
                bin_id=location_id,
                quantity=quantity,
                unit=str(args.get("unit", "each")),
                needs_review=bool(args.get("needs_review", False)),
                normalize_bin=False,
            )
            dimensions = normalize_item_dimensions(args.get("dimensions"))
            requested_size = str(args.get("size_class") or "").strip().lower()
            size_update: dict[str, object] | None = None
            if dimensions or requested_size:
                measured_size = measured_capacity_for(
                    dimensions or None, locations=locations
                )
                size_class = requested_size or (
                    measured_size[0]
                    if measured_size is not None
                    else item_capacity_for(item_name, store=store)[0]
                )
                store.update_item_size(
                    item_id=item_id,
                    size_class=size_class,
                    source="agent",
                    confidence=1.0,
                    dimensions=(
                        dimensions
                        or (existing_item.dimensions if existing_item is not None else None)
                    ),
                )
                size_update = {
                    "updated_item_size": item_id,
                    "item": item_name,
                    "size_class": size_class,
                    "dimensions": dimensions,
                    "previous_size_class": (
                        existing_item.size_class if existing_item is not None else ""
                    ),
                    "previous_size_source": (
                        existing_item.size_source if existing_item is not None else ""
                    ),
                    "previous_size_confidence": (
                        existing_item.size_confidence if existing_item is not None else 0.0
                    ),
                    "previous_dimensions": (
                        existing_item.dimensions if existing_item is not None else None
                    ),
                }
            applied.append(
                {
                    "stored": item_name,
                    "item_id": item_id,
                    "location_id": location_id,
                    "needs_review": bool(args.get("needs_review", False)),
                    "previous_quantity": previous.quantity if previous is not None else None,
                    "previous_unit": previous.unit if previous is not None else None,
                    "quantity": quantity,
                }
            )
            if size_update is not None:
                applied.append(size_update)
        elif tool == "move_item":
            item_id = _resolve_item_id(args, store=store)
            from_location_id = _resolve_location_id(args.get("from_location_id"), locations)
            to_location_id = _resolve_location_id(args.get("to_location_id"), locations)
            current_rows = store.item_inventory(item_id)
            source_before = next((row for row in current_rows if row.bin_id == from_location_id), None)
            destination_before = next((row for row in current_rows if row.bin_id == to_location_id), None)
            moved = store.move_inventory(
                item_id=item_id,
                from_bin=from_location_id,
                to_bin=to_location_id,
                normalize_bin=False,
            )
            if not moved:
                raise ValueError(
                    f"item {item_id} is not stored in {from_location_id}"
                )
            applied.append(
                {
                    "moved_item": item_id,
                    "item": str(args.get("item") or ""),
                    "from_location_id": from_location_id,
                    "to_location_id": to_location_id,
                    "location_id": to_location_id,
                    "previous_from_quantity": source_before.quantity if source_before is not None else None,
                    "previous_from_unit": source_before.unit if source_before is not None else None,
                    "previous_to_quantity": destination_before.quantity if destination_before is not None else None,
                    "previous_to_unit": destination_before.unit if destination_before is not None else None,
                }
            )
        elif tool == "remove_item":
            applied.append(_execute_remove_item(args, store=store, locations=locations))
        elif tool == "move_container":
            location_id = _resolve_location_id(args.get("location_id"), locations)
            old_parent = locations.get(location_id).parent_id
            new_parent_id = _resolve_parent_id(args.get("new_parent_id"), locations)
            moved = locations.move(location_id, new_parent_id)
            locations.record_event(
                event_type="location_moved",
                location_id=moved.id,
                note=f"from {old_parent or ''} to {moved.parent_id or ''}".strip(),
            )
            applied.append(
                {
                    "moved_container": moved.id,
                    "from_parent_id": old_parent,
                    "to_parent_id": moved.parent_id,
                    "location_id": moved.id,
                }
            )
        elif tool == "attach_photo":
            applied.append(_execute_attach_photo(args, store=store, locations=locations))
        elif tool == "approve_review_item":
            item_id = int(args["item_id"])
            location_id = _resolve_location_id(args.get("location_id"), locations)
            current = next(
                (
                    row
                    for row in store.review_queue()
                    if row.item_id == item_id and row.bin_id == location_id
                ),
                None,
            )
            if current is None:
                raise ValueError(
                    f"review item {item_id} was not found in {location_id}"
                )
            store.mark_reviewed(item_id=item_id, bin_id=location_id)
            approved = current.as_dict()
            store.record_example(
                command=f"review approved {current.name} in {current.bin_id}",
                action=json.dumps({"approved_review": approved}, sort_keys=True),
                kind="review_approved",
            )
            applied.append(
                {
                    "approved_review": item_id,
                    "item": current.name,
                    "location_id": location_id,
                    "previous_review_reason": current.review_reason,
                }
            )
        elif tool == "learn_alias":
            applied.append(_execute_learn_alias(args, store=store))
        elif tool == "learn_rule":
            applied.append(_execute_learn_rule(args, store=store, locations=locations))
        elif tool == "learn_size_rule":
            applied.append(_execute_learn_size_rule(args, store=store))
        elif tool == "update_rule":
            applied.append(_execute_update_rule(args, store=store, locations=locations))
        elif tool == "update_size_rule":
            applied.append(_execute_update_size_rule(args, store=store))
        elif tool == "update_item":
            applied.append(_execute_update_item(args, store=store))
        elif tool == "update_item_size":
            applied.append(_execute_update_item_size(args, store=store))
        elif tool == "update_item_home":
            applied.append(
                _execute_update_item_home(args, store=store, locations=locations)
            )
        elif tool == "forget_rule":
            applied.append(_execute_forget_rule(args, store=store))
        elif tool == "print_label":
            location_id = _resolve_location_id(args.get("location_id"), locations)
            applied.append({"print_label": location_id})
        else:
            raise ValueError(f"unknown plan tool: {tool}")
    return applied


class _VirtualLocations:
    """In-memory location graph used to reject an invalid plan before writes."""

    def __init__(self, locations: LocationStore) -> None:
        self.rows = {
            location.id: location
            for location in locations.all(include_archived=True)
        }

    def get(self, location_id: str, *, include_archived: bool = False) -> Location | None:
        location = self.rows.get(location_id)
        if location is None or (not include_archived and not location.active):
            return None
        return location

    def get_any(self, location_id: str) -> Location | None:
        return self.rows.get(location_id)

    def all(self, *, include_archived: bool = False) -> list[Location]:
        return [
            location
            for location in self.rows.values()
            if include_archived or location.active
        ]

    def subtree_ids(self, location_id: str, *, include_archived: bool = False) -> list[str]:
        children: dict[str, list[str]] = {}
        for location in self.all(include_archived=include_archived):
            if location.parent_id:
                children.setdefault(location.parent_id, []).append(location.id)
        found: list[str] = []
        queue = [location_id]
        while queue:
            current = queue.pop(0)
            if current in found:
                continue
            found.append(current)
            queue.extend(children.get(current, []))
        return found


def _preflight_plan(
    plan: list[dict],
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> None:
    """Validate a complete confirmed plan against a virtual graph before mutation."""
    virtual = _VirtualLocations(locations)
    aliases: dict[str, str] = {}
    inventory = {
        (row.item_id, row.bin_id)
        for row in store.list_inventory()
    }
    virtual_item_ids: dict[str, int] = {}
    next_virtual_item_id = -1
    review_items = {(row.item_id, row.bin_id) for row in store.review_queue()}
    placement_rule_ids = {rule.id for rule in store.rules()}
    size_rule_ids = {rule.id for rule in store.size_rules()}

    def planned_reference(value: object) -> object:
        if value is None:
            return None
        raw = str(value).strip()
        return aliases.get(raw, aliases.get(slugify(raw), value))

    def active_location(value: object) -> str:
        return _resolve_location_id(planned_reference(value), virtual)

    def any_location(value: object) -> str:
        return _resolve_any_location_id(planned_reference(value), virtual)

    def parent_location(value: object) -> str | None:
        return _resolve_parent_id(planned_reference(value), virtual)

    def available_id(label: object) -> str:
        base = slugify(str(label))
        candidate = base
        suffix = 2
        while virtual.get_any(candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def require_unique_sibling_name(
        label: object,
        parent_id: str | None,
        *,
        exclude_id: str = "",
        active_only: bool = True,
    ) -> None:
        normalized = " ".join(str(label or "").split()).casefold()
        if not normalized:
            raise ValueError("location label is required")
        duplicate = next(
            (
                location
                for location in virtual.all(include_archived=not active_only)
                if location.id != exclude_id
                and location.parent_id == parent_id
                and " ".join((location.label or location.id).split()).casefold()
                == normalized
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError(
                f'location "{str(label).strip()}" already exists at that level'
            )

    def resolve_item_id(arguments: dict) -> int:
        nonlocal next_virtual_item_id
        raw_id = arguments.get("item_id")
        if raw_id not in (None, ""):
            resolved = int(raw_id)
            if not any(candidate_id == resolved for candidate_id, _ in inventory):
                raise ValueError(f"item {resolved} was not found in inventory")
            return resolved
        item_name = str(arguments.get("item") or "").strip()
        if not item_name:
            raise ValueError("item_id or item is required")
        virtual_id = virtual_item_ids.get(item_name.casefold())
        if virtual_id is not None:
            return virtual_id
        try:
            return _resolve_item_id(arguments, store=store)
        except ValueError:
            virtual_item_ids[item_name.casefold()] = next_virtual_item_id
            next_virtual_item_id -= 1
            return virtual_item_ids[item_name.casefold()]

    for index, step in enumerate(plan, start=1):
        tool = str(step.get("tool") or "")
        args = dict(step.get("arguments", {}) or {})
        try:
            if tool not in WRITE_TOOL_NAMES:
                raise ValueError(f"unknown plan tool: {tool}")
            if tool == "create_location":
                label = " ".join(str(args.get("label") or "").split())
                if not label:
                    raise ValueError("location label is required")
                location_type = normalize_location_type(args.get("type") or "bin")
                parent_id = parent_location(args.get("parent_id"))
                require_unique_sibling_name(label, parent_id)
                location_id = available_id(label)
                virtual.rows[location_id] = Location(
                    id=location_id,
                    type=location_type,
                    parent_id=parent_id,
                    label=label,
                    capacity_class=str(args.get("capacity_class") or "medium"),
                    accepts_items=(
                        bool(args["accepts_items"])
                        if "accepts_items" in args
                        else default_accepts_items(location_type)
                    ),
                )
                aliases[slugify(label)] = location_id
                aliases[label] = location_id
                continue

            if tool == "update_location":
                location_id = active_location(args.get("location_id"))
                current = virtual.get(location_id)
                if current is None:
                    raise ValueError(f"unknown location: {location_id}")
                parent_id = (
                    parent_location(args.get("parent_id"))
                    if "parent_id" in args
                    else current.parent_id
                )
                if parent_id == location_id or parent_id in virtual.subtree_ids(
                    location_id
                ):
                    raise ValueError("location cannot move inside its own subtree")
                label = (
                    " ".join(str(args.get("label") or "").split())
                    if "label" in args
                    else current.label
                )
                location_type = (
                    normalize_location_type(args.get("type"))
                    if "type" in args
                    else current.type
                )
                require_unique_sibling_name(
                    label,
                    parent_id,
                    exclude_id=location_id,
                )
                virtual.rows[location_id] = replace(
                    current,
                    parent_id=parent_id,
                    label=label,
                    type=location_type,
                )
                continue

            if tool == "archive_location":
                location_id = active_location(args.get("location_id"))
                for child_id in virtual.subtree_ids(location_id):
                    virtual.rows[child_id] = replace(
                        virtual.rows[child_id], active=False, archived_at="preflight"
                    )
                continue

            if tool == "restore_location":
                location_id = any_location(args.get("location_id"))
                current = virtual.get_any(location_id)
                if current is None or current.active:
                    raise ValueError(f"location is not archived: {location_id}")
                if current.parent_id and virtual.get(current.parent_id) is None:
                    raise ValueError("restore the parent location first")
                require_unique_sibling_name(
                    current.label or current.id,
                    current.parent_id,
                    exclude_id=location_id,
                    active_only=True,
                )
                for child_id in virtual.subtree_ids(location_id, include_archived=True):
                    virtual.rows[child_id] = replace(
                        virtual.rows[child_id], active=True, archived_at=None
                    )
                continue

            if tool == "put_item":
                location_id = active_location(args.get("location_id"))
                destination = virtual.get(location_id)
                if destination is not None and not destination.accepts_items:
                    raise ValueError(f"location does not accept items directly: {location_id}")
                float(args.get("quantity", 1))
                inventory.add((resolve_item_id(args), location_id))
                continue

            if tool == "move_item":
                resolved_item_id = resolve_item_id(args)
                source = active_location(args.get("from_location_id"))
                destination = active_location(args.get("to_location_id"))
                destination_location = virtual.get(destination)
                if (
                    destination_location is not None
                    and not destination_location.accepts_items
                ):
                    raise ValueError(
                        f"location does not accept items directly: {destination}"
                    )
                if (resolved_item_id, source) not in inventory:
                    raise ValueError(f"item {resolved_item_id} is not stored in {source}")
                inventory.discard((resolved_item_id, source))
                inventory.add((resolved_item_id, destination))
                continue

            if tool == "remove_item":
                resolved_item_id = resolve_item_id(args)
                rows = {
                    location_id
                    for candidate_id, location_id in inventory
                    if candidate_id == resolved_item_id
                }
                if args.get("location_id"):
                    location_id = active_location(args.get("location_id"))
                    if location_id not in rows:
                        raise ValueError(
                            f"item {resolved_item_id} is not stored in {location_id}"
                        )
                elif len(rows) == 1:
                    location_id = next(iter(rows))
                elif not rows:
                    raise ValueError(
                        f"item {resolved_item_id} was not found in inventory"
                    )
                else:
                    raise ValueError(
                        f"remove_item needs a location_id for item {resolved_item_id}"
                    )
                inventory.discard((resolved_item_id, location_id))
                continue

            if tool == "move_container":
                location_id = active_location(args.get("location_id"))
                new_parent_id = parent_location(args.get("new_parent_id"))
                if new_parent_id == location_id:
                    raise ValueError("location cannot be its own parent")
                if new_parent_id in virtual.subtree_ids(location_id):
                    raise ValueError("location cannot move inside its own subtree")
                current = virtual.get(location_id)
                if current is None:
                    raise ValueError(f"unknown location: {location_id}")
                require_unique_sibling_name(
                    current.label or current.id,
                    new_parent_id,
                    exclude_id=location_id,
                )
                virtual.rows[location_id] = replace(
                    current, parent_id=new_parent_id
                )
                continue

            if tool == "attach_photo":
                photo_id = int(args["photo_id"])
                if store.photo(photo_id) is None:
                    raise ValueError(f"photo {photo_id} was not found")
                object_type = str(args.get("object_type") or "").strip().lower()
                if object_type == "location":
                    active_location(args.get("object_id"))
                elif object_type == "item":
                    object_id = args.get("object_id")
                    resolve_item_id(
                        {"item_id": object_id}
                        if str(object_id or "").strip().isdigit()
                        else {"item": object_id}
                    )
                else:
                    raise ValueError("object_type must be item or location")
                continue

            if tool == "approve_review_item":
                review_item_id = int(args["item_id"])
                location_id = active_location(args.get("location_id"))
                if (review_item_id, location_id) not in review_items:
                    raise ValueError(
                        f"review item {review_item_id} was not found in {location_id}"
                    )
                review_items.discard((review_item_id, location_id))
                continue

            if tool in {"learn_alias", "update_item", "update_item_home"}:
                resolved_item_id = resolve_item_id(args)
                if not any(
                    candidate_id == resolved_item_id for candidate_id, _ in inventory
                ):
                    raise ValueError(
                        f"item {resolved_item_id} was not found in inventory"
                    )
                if tool == "learn_alias" and not str(args.get("alias") or "").strip():
                    raise ValueError("alias is required")
                if tool == "update_item_home":
                    active_location(args.get("location_id"))
                continue

            if tool in {"learn_rule", "update_rule"}:
                target_location_id = parent_location(args.get("target_location_id"))
                if target_location_id is None:
                    raise ValueError("target_location_id is required")
                if not str(args.get("rule_text") or "").strip():
                    raise ValueError("rule_text is required")
                float(args.get("confidence", 1.0))
                if tool == "update_rule" and int(args["rule_id"]) not in placement_rule_ids:
                    raise ValueError(
                        f"active placement rule {int(args['rule_id'])} was not found"
                    )
                continue

            if tool in {"learn_size_rule", "update_size_rule"}:
                if not str(args.get("rule_text") or "").strip():
                    raise ValueError("rule_text is required")
                if not str(args.get("size_class") or "").strip():
                    raise ValueError("size_class is required")
                float(args.get("confidence", 1.0))
                if (
                    tool == "update_size_rule"
                    and int(args["rule_id"]) not in size_rule_ids
                ):
                    raise ValueError(
                        f"active size rule {int(args['rule_id'])} was not found"
                    )
                continue

            if tool == "update_item_size":
                if not str(args.get("item") or "").strip():
                    raise ValueError("item is required")
                if not str(args.get("size_class") or "").strip():
                    raise ValueError("size_class is required")
                float(args.get("confidence", 1.0))
                continue

            if tool == "forget_rule":
                rule_id = int(args["rule_id"])
                rule_type = str(args["rule_type"])
                active_rules = (
                    placement_rule_ids if rule_type == "placement" else size_rule_ids
                )
                if rule_id not in active_rules:
                    raise ValueError(f"active {rule_type} rule {rule_id} was not found")
                active_rules.discard(rule_id)
                continue

            if tool == "print_label":
                active_location(args.get("location_id"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"plan step {index} ({tool}) is invalid: {exc}") from exc


def _resolve_item_id(args: dict, *, store: InventoryStore) -> int:
    raw_id = args.get("item_id")
    if raw_id not in (None, ""):
        return int(raw_id)
    item = str(args.get("item") or "").strip()
    if not item:
        raise ValueError("item_id or item is required")
    matches = store.search(item, limit=2)
    if len(matches) != 1:
        raise ValueError(f"item lookup needs an unambiguous item_id for {item}")
    return matches[0].item_id


def _resolve_location_id(raw_location_id: object, locations: LocationStore) -> str:
    raw = str(raw_location_id or "").strip()
    if not raw:
        raise ValueError("location_id is required")
    matches = match_locations(raw, locations, active=True)
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        paths = [" > ".join(node.label or node.id for node in location_path(locations, row.id)) for row in matches]
        raise ValueError(f"ambiguous location: {raw}; use one of: {', '.join(paths)}")
    raise ValueError(f"unknown location: {raw}")


def _resolve_any_location_id(raw_location_id: object, locations: LocationStore) -> str:
    raw = str(raw_location_id or "").strip()
    if not raw:
        raise ValueError("location_id is required")
    matches = match_locations(raw, locations, active=None)
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        raise ValueError(f"ambiguous location: {raw}")
    raise ValueError(f"unknown location: {raw}")


def _execute_update_location(
    args: dict,
    *,
    locations: LocationStore,
) -> dict[str, object]:
    location_id = _resolve_location_id(args.get("location_id"), locations)
    current = locations.get(location_id)
    if current is None:
        raise ValueError(f"unknown location: {location_id}")
    label = (
        str(args["label"]).strip()
        if "label" in args and args.get("label") is not None
        else current.label
    )
    loc_type = (
        normalize_location_type(args["type"])
        if "type" in args and args.get("type") is not None
        else current.type
    )
    capacity_class = (
        str(args["capacity_class"]).strip()
        if "capacity_class" in args and args.get("capacity_class") is not None
        else current.capacity_class
    )
    if isinstance(args.get("purpose_tags"), list):
        purpose_tags = [
            str(tag).strip()
            for tag in args["purpose_tags"]
            if str(tag).strip()
        ]
    else:
        purpose_tags = list(current.purpose_tags)
    placement_notes = (
        str(args["placement_notes"]).strip()
        if "placement_notes" in args and args.get("placement_notes") is not None
        else current.placement_notes
    )
    internal_dimensions = (
        normalize_internal_dimensions(args.get("internal_dimensions"))
        if "internal_dimensions" in args
        else current.internal_dimensions
    )
    accepts_items = (
        bool(args["accepts_items"])
        if "accepts_items" in args
        else current.accepts_items
    )
    updated = replace(
        current,
        label=label,
        type=loc_type,
        capacity_class=capacity_class,
        purpose_tags=purpose_tags,
        placement_notes=placement_notes,
        internal_dimensions=internal_dimensions,
        accepts_items=accepts_items,
    )
    locations.upsert(updated)
    locations.record_event(
        event_type="location_updated",
        location_id=location_id,
        note="agent metadata update",
    )
    return {
        "updated_location": location_id,
        "label": label,
        "type": loc_type,
        "capacity_class": capacity_class,
        "purpose_tags": purpose_tags,
        "placement_notes": placement_notes,
        "internal_dimensions": internal_dimensions,
        "accepts_items": accepts_items,
        "previous_label": current.label,
        "previous_type": current.type,
        "previous_capacity_class": current.capacity_class,
        "previous_purpose_tags": current.purpose_tags,
        "previous_placement_notes": current.placement_notes,
        "previous_internal_dimensions": current.internal_dimensions,
        "previous_accepts_items": current.accepts_items,
    }


def _execute_remove_item(
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> dict[str, object]:
    item_id = _resolve_item_id(args, store=store)
    rows = store.item_inventory(item_id)
    if not rows:
        raise ValueError(f"item {item_id} was not found in inventory")
    if args.get("location_id"):
        location_id = _resolve_location_id(args.get("location_id"), locations)
        current = next((row for row in rows if row.bin_id == location_id), None)
        if current is None:
            raise ValueError(f"item {item_id} is not stored in {location_id}")
    elif len(rows) == 1:
        current = rows[0]
        location_id = current.bin_id
    else:
        raise ValueError(f"remove_item needs a location_id for item {item_id}")
    removed = store.remove_inventory(item_id=item_id, bin_id=location_id, normalize_bin=False)
    if not removed:
        raise ValueError(f"item {item_id} is not stored in {location_id}")
    return {
        "removed_item": item_id,
        "item": current.name,
        "location_id": location_id,
        "quantity": current.quantity,
        "unit": current.unit,
    }


def _execute_learn_alias(
    args: dict,
    *,
    store: InventoryStore,
) -> dict[str, object]:
    item_id = _resolve_item_id(args, store=store)
    alias = str(args["alias"]).strip()
    if not alias:
        raise ValueError("alias is required")
    store.learn_alias(item_id=item_id, alias=alias)
    rows = store.item_inventory(item_id)
    item_name = rows[0].name if rows else str(args.get("item") or "")
    return {
        "learned_alias": alias,
        "item_id": item_id,
        "item": item_name,
    }


def _execute_attach_photo(
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> dict[str, object]:
    photo_id = int(args["photo_id"])
    photo = store.photo(photo_id)
    if photo is None:
        raise ValueError(f"photo {photo_id} was not found")
    object_type = str(args["object_type"]).strip().lower()
    object_id = args.get("object_id")
    if object_type == "item":
        lookup_args = (
            {"item_id": object_id}
            if isinstance(object_id, int) or str(object_id or "").strip().isdigit()
            else {"item": object_id}
        )
        item_id = _resolve_item_id(lookup_args, store=store)
        store.set_photo_target(photo_id=photo_id, item_id=item_id)
        rows = store.item_inventory(item_id)
        label = rows[0].name if rows else f"item #{item_id}"
        return {
            "attached_photo": photo_id,
            "object_type": "item",
            "item_id": item_id,
            "label": label,
            "previous_item_id": photo.item_id,
            "previous_location_id": photo.location_id,
        }
    if object_type == "location":
        location_id = _resolve_location_id(object_id, locations)
        store.set_photo_target(photo_id=photo_id, location_id=location_id)
        loc = locations.get(location_id)
        locations.record_event(
            event_type="location_photo_attached",
            location_id=location_id,
            note=f"agent photo {photo_id}",
        )
        return {
            "attached_photo": photo_id,
            "object_type": "location",
            "location_id": location_id,
            "label": loc.label if loc else location_id,
            "previous_item_id": photo.item_id,
            "previous_location_id": photo.location_id,
        }
    raise ValueError("object_type must be item or location")


def _execute_learn_rule(
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> dict[str, object]:
    target_location_id = _resolve_parent_id(args.get("target_location_id"), locations)
    if target_location_id is None:
        raise ValueError("target_location_id is required")
    rule_text = str(args["rule_text"])
    rule_id = store.learn_rule(
        rule_text=rule_text,
        trigger_terms=[
            str(term)
            for term in args.get("trigger_terms", [])
            if str(term).strip()
        ],
        target_location_id=target_location_id,
        source_command=str(args.get("source_command") or ""),
        confidence=float(args.get("confidence", 1.0)),
    )
    return {
        "learned_rule": rule_id,
        "target_location_id": target_location_id,
        "rule_text": rule_text,
    }


def _execute_learn_size_rule(
    args: dict,
    *,
    store: InventoryStore,
) -> dict[str, object]:
    rule_text = str(args["rule_text"])
    size_class = str(args["size_class"])
    rule_id = store.learn_size_rule(
        rule_text=rule_text,
        trigger_terms=[
            str(term)
            for term in args.get("trigger_terms", [])
            if str(term).strip()
        ],
        size_class=size_class,
        source_command=str(args.get("source_command") or ""),
        confidence=float(args.get("confidence", 1.0)),
    )
    return {"learned_size_rule": rule_id, "size_class": size_class, "rule_text": rule_text}


def _execute_update_rule(
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> dict[str, object]:
    rule_id = int(args["rule_id"])
    previous = store.rule(rule_id)
    if previous is None:
        raise ValueError(f"active placement rule {rule_id} was not found")
    target_location_id = _resolve_parent_id(args.get("target_location_id"), locations)
    if target_location_id is None:
        raise ValueError("target_location_id is required")
    rule_text = str(args["rule_text"])
    updated = store.update_rule(
        rule_id=rule_id,
        rule_text=rule_text,
        trigger_terms=[
            str(term)
            for term in args.get("trigger_terms", [])
            if str(term).strip()
        ],
        target_location_id=target_location_id,
        source_command=str(args.get("source_command") or ""),
        confidence=float(args.get("confidence", 1.0)),
    )
    if not updated:
        raise ValueError(f"active placement rule {rule_id} was not found")
    return {
        "updated_rule": rule_id,
        "target_location_id": target_location_id,
        "rule_text": rule_text,
        "previous_rule_text": previous.rule_text,
        "previous_trigger_terms": previous.trigger_terms,
        "previous_target_location_id": previous.target_location_id,
        "previous_source_command": previous.source_command,
        "previous_confidence": previous.confidence,
    }


def _execute_update_size_rule(
    args: dict,
    *,
    store: InventoryStore,
) -> dict[str, object]:
    rule_id = int(args["rule_id"])
    previous = store.size_rule(rule_id)
    if previous is None:
        raise ValueError(f"active size rule {rule_id} was not found")
    rule_text = str(args["rule_text"])
    size_class = str(args["size_class"])
    updated = store.update_size_rule(
        rule_id=rule_id,
        rule_text=rule_text,
        trigger_terms=[
            str(term)
            for term in args.get("trigger_terms", [])
            if str(term).strip()
        ],
        size_class=size_class,
        source_command=str(args.get("source_command") or ""),
        confidence=float(args.get("confidence", 1.0)),
    )
    if not updated:
        raise ValueError(f"active size rule {rule_id} was not found")
    return {
        "updated_size_rule": rule_id,
        "size_class": size_class,
        "rule_text": rule_text,
        "previous_rule_text": previous.rule_text,
        "previous_trigger_terms": previous.trigger_terms,
        "previous_size_class": previous.size_class,
        "previous_source_command": previous.source_command,
        "previous_confidence": previous.confidence,
    }


def _execute_update_item(
    args: dict,
    *,
    store: InventoryStore,
) -> dict[str, object]:
    item_id = _resolve_item_id(args, store=store)
    rows = store.item_inventory(item_id)
    current = rows[0] if rows else None
    if current is None:
        raise ValueError(f"item {item_id} was not found in inventory")
    new_name = str(args.get("name") or current.name).strip()
    description = (
        str(args["description"]).strip()
        if "description" in args and args.get("description") is not None
        else current.description
    )
    category = (
        str(args["category"]).strip()
        if "category" in args and args.get("category") is not None
        else current.category
    )
    if isinstance(args.get("aliases"), list):
        aliases = [str(alias).strip() for alias in args["aliases"] if str(alias).strip()]
    else:
        aliases = list(current.aliases)
    if new_name.lower() != current.name.lower() and current.name.lower() not in {
        alias.lower() for alias in aliases
    }:
        aliases.append(current.name)
    store.update_item(
        item_id=item_id,
        name=new_name,
        description=description,
        category=category,
        aliases=aliases,
    )
    return {
        "updated_item": item_id,
        "item": new_name,
        "previous_name": current.name,
        "previous_description": current.description,
        "previous_category": current.category,
        "previous_aliases": current.aliases,
        "category": category,
        "description": description,
        "aliases": aliases,
    }


def _execute_update_item_size(
    args: dict,
    *,
    store: InventoryStore,
) -> dict[str, object]:
    item_name = str(args["item"])
    item_id = store.upsert_item(name=item_name)
    rows = store.item_inventory(item_id)
    current = rows[0] if rows else None
    size_class = str(args["size_class"])
    store.update_item_size(
        item_id=item_id,
        size_class=size_class,
        source="user",
        confidence=float(args.get("confidence", 1.0)),
    )
    return {
        "updated_item_size": item_id,
        "item": item_name,
        "size_class": size_class,
        "previous_size_class": current.size_class if current else "",
        "previous_size_source": current.size_source if current else "",
        "previous_size_confidence": current.size_confidence if current else 0.0,
        "previous_dimensions": current.dimensions if current else None,
    }


def _execute_update_item_home(
    args: dict,
    *,
    store: InventoryStore,
    locations: LocationStore,
) -> dict[str, object]:
    item_id = _resolve_item_id(args, store=store)
    rows = store.item_inventory(item_id)
    current = rows[0] if rows else None
    if current is None:
        raise ValueError(f"item {item_id} was not found in inventory")
    location_id = _resolve_location_id(args.get("location_id"), locations)
    store.update_item_home(item_id=item_id, location_id=location_id)
    return {
        "updated_item_home": item_id,
        "item": current.name,
        "location_id": location_id,
        "previous_home_location_id": current.home_location_id,
        "previous_last_seen_location_id": current.last_seen_location_id,
    }


def _execute_forget_rule(
    args: dict,
    *,
    store: InventoryStore,
) -> dict[str, object]:
    rule_id = int(args["rule_id"])
    rule_type = str(args["rule_type"])
    forgotten = store.deactivate_rule(rule_id=rule_id, rule_type=rule_type)
    if not forgotten:
        raise ValueError(f"active {rule_type} rule {rule_id} was not found")
    return {"forgotten_rule": rule_id, "rule_type": rule_type}


def normalize_plan(
    plan: list[dict],
    *,
    locations: LocationStore,
    store: InventoryStore | None = None,
    layout: StorageLayout | None = None,
    command: str = "",
) -> list[dict]:
    """Remove proposal steps that would recreate an existing location.

    The LLM often emits "create Attic" when Attic is just the named parent.
    Keeping that in the visible proposal is misleading, and executing it would
    upsert over the existing metadata. Child steps still resolve parent labels
    through `_resolve_parent_id`, so dropping this no-op is safe.
    """
    explicit_location_ids = {
        normalize_bin_id(match.group(0).replace(" ", ""))
        for match in re.finditer(r"\bB\s*\d{1,4}\b", command, re.IGNORECASE)
    }
    generic_main_cabinet_bin = _generic_main_cabinet_bin_request(command)
    replacement_bin = (
        first_empty_bin(layout, store, min_size="small")
        if generic_main_cabinet_bin and store is not None and layout is not None
        else None
    )
    photo_sort_existing_bins = _photo_sort_existing_bins_request(command)
    created_location_ids = {
        slugify(str((step.get("arguments", {}) or {}).get("label", "")))
        for step in plan
        if step.get("tool") == "create_location"
    }
    removed_generic_locations: set[str] = set()
    reserved_empty_bins: set[str] = set()
    normalized: list[dict] = []
    for step in plan:
        tool = step.get("tool")
        args = step.get("arguments", {}) or {}
        if tool == "create_location":
            loc_id = slugify(str(args.get("label", "")))
            existing = locations.get_any(loc_id)
            if existing is not None and existing.active and _create_matches_existing_parent(
                args.get("parent_id"), existing.parent_id, locations
            ):
                continue
            if photo_sort_existing_bins:
                removed_generic_locations.add(loc_id)
                continue
            if replacement_bin and _is_generic_main_cabinet_child(args, locations):
                removed_generic_locations.add(loc_id)
                continue
        if tool == "put_item" and replacement_bin:
            loc = str(args.get("location_id", "")).strip()
            if loc in removed_generic_locations or _occupied_cabinet_bin(loc, store, layout):
                step = {
                    **step,
                    "arguments": {
                        **args,
                        "location_id": replacement_bin,
                    },
                }
        if tool == "put_item" and photo_sort_existing_bins and store is not None and layout is not None:
            loc = str((step.get("arguments", {}) or {}).get("location_id", "")).strip()
            if (
                loc in removed_generic_locations
                or loc in created_location_ids
                or locations.get(loc) is None
            ):
                destination = _photo_sort_destination(
                    str(args.get("item", "")),
                    store=store,
                    locations=locations,
                    layout=layout,
                    reserved_empty_bins=reserved_empty_bins,
                )
                if destination:
                    if _is_empty_cabinet_bin(destination, store, layout):
                        reserved_empty_bins.add(destination)
                    step = {
                        **step,
                        "arguments": {
                            **args,
                            "location_id": destination,
                        },
                    }
        if tool == "put_item" and store is not None and layout is not None:
            loc = str((step.get("arguments", {}) or {}).get("location_id", "")).strip()
            related = _related_inventory_location(str(args.get("item", "")), store)
            normalized_location = normalize_bin_id(loc)
            if (
                related
                and normalized_location not in explicit_location_ids
                and _occupied_cabinet_bin(loc, store, layout)
                and related != normalized_location
            ):
                step = {
                    **step,
                    "arguments": {
                        **(step.get("arguments", {}) or {}),
                        "location_id": related,
                    },
                }
        normalized.append(step)
    return normalized


def _generic_main_cabinet_bin_request(command: str) -> bool:
    lowered = command.lower()
    if re.search(r"\bB\d{1,3}\b", command, re.I):
        return False
    return bool(
        "main cabinet" in lowered
        and re.search(r"\bsmall\b", lowered)
        and re.search(r"\b(bin|drawer)\b", lowered)
    )


def _photo_sort_existing_bins_request(command: str) -> bool:
    lowered = command.lower()
    if re.search(r"\b(new|create|make)\b", lowered):
        return False
    return bool(
        re.search(
            r"\b(sort|catalog|catalogue|separate|separately|split|group|groups|file these|put these away)\b",
            lowered,
        )
    )


def _is_generic_main_cabinet_child(args: dict, locations: LocationStore) -> bool:
    loc_type = str(args.get("type", "")).lower()
    if loc_type not in {"bin", "drawer", "cell"}:
        return False
    label = str(args.get("label", "")).lower()
    if "small" not in label:
        return False
    parent = _resolve_parent_id(args.get("parent_id"), locations)
    return parent == "main-cabinet"


def _occupied_cabinet_bin(
    location_id: str,
    store: InventoryStore | None,
    layout: StorageLayout | None,
) -> bool:
    if store is None or layout is None:
        return False
    normalized = normalize_bin_id(location_id)
    return layout.get(normalized) is not None and normalized in store.occupied_bin_ids()


def _photo_sort_destination(
    item_name: str,
    *,
    store: InventoryStore,
    locations: LocationStore,
    layout: StorageLayout,
    reserved_empty_bins: set[str],
) -> str | None:
    if destination := _related_inventory_location(item_name, store):
        return destination
    for suggestion in suggest_locations(item_name, store=store, locations=locations, limit=8):
        if (
            suggestion.location.id in reserved_empty_bins
            and _is_empty_cabinet_bin(suggestion.location.id, store, layout)
        ):
            continue
        if locations.get(suggestion.location.id) is not None:
            return suggestion.location.id
    return _first_unreserved_empty_bin(layout, store, reserved_empty_bins, min_size="small")


def _related_inventory_location(item_name: str, store: InventoryStore) -> str | None:
    item_terms = _expanded_terms(item_name)
    if not item_terms:
        return None
    clean_item = _clean_phrase(item_name)
    scored: list[tuple[int, str]] = []
    for result in store.list_inventory():
        if identities_conflict(item_name, result.name):
            continue
        existing_terms = _expanded_terms(
            " ".join([result.name, result.category, *result.aliases])
        )
        overlap = item_terms & existing_terms
        if not overlap:
            continue
        score = len(overlap)
        if item_terms <= existing_terms:
            score += 4
        if result.name.lower() in item_name.lower() or item_name.lower() in result.name.lower():
            score += 3
        aliases = {_clean_phrase(alias) for alias in result.aliases}
        if clean_item == _clean_phrase(result.name) or clean_item in aliases:
            score += 5
        scored.append((score, result.bin_id))
    if not scored:
        return None
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return scored[0][1]


def _expanded_terms(text: str) -> set[str]:
    out: set[str] = set()
    for term in tokens(text):
        if term.endswith("ies") and len(term) > 4:
            out.add(term[:-3] + "y")
        elif term.endswith("es") and len(term) > 4:
            out.add(term[:-2])
        elif term.endswith("s") and len(term) > 3:
            out.add(term[:-1])
        else:
            out.add(term)
    return out


def _clean_phrase(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _first_unreserved_empty_bin(
    layout: StorageLayout,
    store: InventoryStore,
    reserved: set[str],
    *,
    min_size: str,
) -> str | None:
    occupied = store.occupied_bin_ids() | reserved
    for bin in sorted(layout.bins, key=bin_sort_key):
        if bin.id not in occupied and bin_fits(bin.size, min_size):
            return bin.id
    return None


def _is_empty_cabinet_bin(
    location_id: str,
    store: InventoryStore,
    layout: StorageLayout,
) -> bool:
    normalized = normalize_bin_id(location_id)
    return layout.get(normalized) is not None and normalized not in store.occupied_bin_ids()


def _resolve_parent_id(parent_id: object, locations: LocationStore) -> str | None:
    if parent_id is None:
        return None
    raw = str(parent_id).strip()
    if not raw:
        return None
    try:
        return _resolve_location_id(raw, locations)
    except ValueError as exc:
        raise ValueError(str(exc).replace("unknown location", "unknown parent location")) from exc


def _create_matches_existing_parent(
    requested_parent: object,
    existing_parent_id: str | None,
    locations: LocationStore,
) -> bool:
    if requested_parent is None or not str(requested_parent).strip():
        return existing_parent_id is None
    raw = str(requested_parent).strip()
    candidates = {raw, slugify(raw), raw.upper()}
    resolved = next((candidate for candidate in candidates if locations.get(candidate)), None)
    return existing_parent_id == (resolved or slugify(raw))


def _available_location_id(requested_id: str, locations: LocationStore) -> str:
    base = requested_id or "location"
    if locations.get_any(base) is None:
        return base
    suffix = 2
    while locations.get_any(f"{base}-{suffix}") is not None:
        suffix += 1
    return f"{base}-{suffix}"
