# POC Contract: Agent, Schema, and Actions

> Status: working contract for getting the current POC up to snuff.
> Companion docs: [`vision.md`](vision.md), [`product-direction.md`](product-direction.md).

## Goal

The POC should prove one thing: a user can organize and find real physical
stuff through conversation, with the app turning natural language into safe
storage operations.

The storage graph, agent actions, and tuning set are intertwined. The agent
cannot be useful unless it knows the same objects the product sells: storage
spaces, containers, items, labels, optional lights, and durable preferences.

## Core Model

### Workspace

A workspace is the user's storage world: home, shop, classroom, lab, or garage.

Fields:

- `id`
- `name`
- `owner_id`
- `created_at`
- `updated_at`

POC note: the local Pi app can treat the single SQLite database as one implicit
workspace. Supabase should make this explicit early.

### Location Node

Everything that can contain or describe a physical place is a location node.
This includes rooms, shelves, cabinets, drawers, boxes, pouches, bins, and
cells. A location may hold items and may have child locations.

Fields:

- `id`
- `workspace_id`
- `parent_id`
- `type`: `room`, `area`, `cabinet`, `drawer`, `shelf`, `box`, `pouch`, `bin`, `cell`
- `label`
- `path_label`
- `capacity_class`: `tiny`, `small`, `medium`, `large`, `oversize`
- `purpose_tags`
- `placement_notes`
- `sort_order`
- `active`
- `created_at`
- `updated_at`

Current POC mapping: `main-cabinet` and `B###` bins are already location nodes.
The next model work is making every future storage object use the same tree
contract instead of adding special cases.

### Item

An item is the thing the user cares about finding or putting away.

Fields:

- `id`
- `workspace_id`
- `name`
- `aliases`
- `category`
- `quantity`
- `unit`
- `size_class`
- `home_location_id`
- `last_seen_location_id`
- `notes`
- `created_at`
- `updated_at`

POC rule: exact item size beats learned size rules, learned size rules beat
keyword guesses.

### Inventory Entry

An inventory entry records item presence at a location. This keeps items and
locations separate enough to support future partial quantities, kits, and
multiple homes.

Fields:

- `id`
- `workspace_id`
- `item_id`
- `location_id`
- `quantity`
- `unit`
- `status`: `present`, `review`, `missing`, `moved`
- `confidence`
- `source`: `manual`, `agent`, `photo`, `scan`, `import`
- `created_at`
- `updated_at`

Current POC mapping: the existing item-to-bin records serve this role.

### Label

A label is the physical affordance for scanning or printing a location, item,
or container.

Fields:

- `id`
- `workspace_id`
- `object_type`: `location`, `item`
- `object_id`
- `code`
- `qr_url`
- `label_text`
- `template`
- `printed_at`
- `created_at`
- `updated_at`

POC rule: PDF label batches are enough before direct printer support.

### Hardware Binding

A hardware binding attaches optional physical output to a location. Hardware
does not define the storage model.

Fields:

- `id`
- `workspace_id`
- `location_id`
- `kind`: `wled_segment`, `wled_pixels`, `eink_tag`, `nfc_tag`
- `controller_id`
- `address`
- `metadata`
- `active`
- `created_at`
- `updated_at`

POC rule: when an item is inside an unlit child, guidance walks up to the
nearest lit ancestor and uses words for the rest.

### Placement Rule

A placement rule is a durable user preference that changes future suggestions.

Fields:

- `id`
- `workspace_id`
- `rule_text`
- `trigger_terms`
- `target_location_id`
- `confidence`
- `source_command`
- `active`
- `created_at`
- `updated_at`

Example: `I always keep jewelry in the main cabinet.`

### Size Rule

A size rule teaches the agent how big a class of items usually is.

Fields:

- `id`
- `workspace_id`
- `rule_text`
- `trigger_terms`
- `size_class`
- `confidence`
- `source_command`
- `active`
- `created_at`
- `updated_at`

Example: `Jewelry boxes are medium.`

### Agent Example

An agent example is a confirmed command, correction, or rejected plan that can
be replayed as eval data and retrieved as few-shot context.

Fields:

- `id`
- `workspace_id`
- `command`
- `context`
- `proposed_plan`
- `confirmed_plan`
- `outcome`: `confirmed`, `corrected`, `rejected`
- `correction`
- `created_at`

POC rule: corrections are the highest-value examples. Once the user corrects an
exact mistake, the system should stop making that exact mistake.

## Agent Action Contract

The agent can read freely but durable writes are proposals until confirmed.

### Read Actions

`search_items(query)`

- Use when the user asks where something is or whether it exists.
- Returns matching items, quantities, paths, and nearest lightable location.

`list_locations(type?, parent_id?)`

- Use when choosing or inspecting storage places.
- Returns location labels, types, item counts, capacity, purpose, and parentage.

`suggest_locations(item)`

- Use when putting something away and the target is not explicit.
- Ranks homes using exact homes, placement rules, size rules, capacity,
  affinity, notes, and existing contents.

Near-term reads:

- `list_rules(query?)`
- `explain_location_choice(item, location_id)`
- `get_location(location_id)`
- `preview_label_batch(location_ids)`

### Write Proposals

Location creation is not an agent write proposal. The visual Storage manager owns the validated
`create_location(type, label, parent_id?, capacity_class?, purpose_tags?, placement_notes?)`
operation for rooms, areas, cabinets, drawers, shelves, boxes, pouches, bins, and cells.

`put_item(item, location_id, quantity?, unit?)`

- Place an item into a location.
- For photo sorting, one visible item group should become one `put_item`.

`move_container(location_id, new_parent_id)`

- Move a location subtree.
- All child locations and contained items move with it.

`learn_rule(rule_text, trigger_terms, target_location_id, confidence?)`

- Save durable placement policy after confirmation.

`learn_size_rule(rule_text, trigger_terms, size_class, confidence?)`

- Save durable size policy after confirmation.

`update_item_size(item, size_class, confidence?)`

- Save size for a specific item after confirmation.

Near-term writes:

- `move_item(item_id, location_id, quantity?)`
- `remove_item(item_id, location_id?, quantity?)`
- `rename_location(location_id, label)`
- `update_location(location_id, capacity_class?, purpose_tags?, placement_notes?)`
- `forget_rule(rule_id)`
- `print_labels(object_ids, template?)`
- `attach_photo(object_type, object_id, photo_id)`
- `mark_reviewed(entry_id)`

## Confirmation Policy

No confirmation needed:

- Searching.
- Listing.
- Suggesting locations.
- Explaining why a location was chosen.
- Previewing labels.

Confirmation required:

- Creating, renaming, moving, or deleting locations.
- Adding, moving, removing, or changing item quantities.
- Learning rules or changing item size.
- Printing labels if it consumes physical media.
- Binding lights, displays, or tags.

Confirmation delivery guarantees:

- Every agent proposal carries a source interaction ID that acts as its
  idempotency key.
- The first confirmation atomically claims the pending proposal before any
  durable action runs.
- A network retry for the same proposal returns the saved confirmation receipt
  instead of applying quantities, moves, rules, photos, or labels again.
- Browser clients disable the confirm control immediately so a double tap does
  not create competing requests.
- A proposal that failed while applying is not silently retried because part of
  a multi-step plan may already have changed durable state.

Review queue instead of auto-write:

- Low-confidence photo capture.
- Mixed photos where item groups are unclear.
- Conflicting learned rules.
- Suggestions that do not fit known capacity.
- Corrections where the intended replacement is ambiguous.

## POC Flows To Tune Against

### Find

User: `Where is my solder wick?`

Expected behavior:

- Agent calls `search_items`.
- UI shows one answer-first location path.
- If there is a lightable ancestor, it flashes.
- The answer includes enough words to navigate unlit children.

### Put Away Known Item

User: `Put away solder wick.`

Expected behavior:

- Agent calls `suggest_locations`.
- It proposes the best existing related location.
- The plan is a confirmable `put_item`.
- The explanation mentions the strongest reason: learned home, rule, capacity,
  purpose tag, or similar contents.

### New Container Needed While Filing

User: `Put these M3 hex nuts in a new blue pouch in drawer 2.`

Expected behavior:

- Agent does not create a location from the conversational request.
- Agent asks the user to create or select the pouch in the visual Storage manager.
- Put-away resumes with the selected location and retains the captured item evidence.

### Learn Placement Preference

User: `I always keep jewelry in the main cabinet.`

Expected behavior:

- Agent proposes `learn_rule`.
- Future jewelry placement suggestions prefer `main-cabinet`.
- Explanations cite the learned rule.

### Learn Size

User: `Jewelry boxes are medium.`

Expected behavior:

- Agent proposes `learn_size_rule`.
- Future jewelry box placement avoids tiny/small locations.
- Exact item size still overrides the class rule.

### Correct Fit

User: `That does not fit, this solder extractor is large.`

Expected behavior:

- Agent proposes `update_item_size`.
- It re-runs placement against larger locations.
- The correction becomes eval/few-shot data.

### Photo Sort

User: `Sort this photo into separate bins.`

Expected behavior:

- Agent identifies distinct visible groups.
- It proposes one `put_item` per group.
- Unclear groups go to review instead of becoming vague inventory.

### Label Batch

User: `Print labels for this drawer.`

Expected behavior:

- Agent previews all child location labels.
- User confirms before printing/exporting PDF.
- Each label opens the location view on scan.

### Rule Inspection

User: `What rules do you know about jewelry?`

Expected behavior:

- Agent lists matching placement and size rules.
- User can keep, edit, or forget them.

## Current Gaps

- Rule inspection, placement explanations, direct rule-card edit/forget
  actions, inline trigger/target editing, and confirm-gated `update_rule` /
  `update_size_rule` plans are available in chat. Placement rule edits use
  known-location choices and show a before/after preview. Fast put-away
  responses now also surface the strongest placement reason in the API and UI.
- `/assistant/chat` is now the canonical typed/voice/photo entrypoint for
  normal commands. The browser UI and voice daemon route normal commands
  through it. Older assistant endpoints still exist as advertised
  compatibility wrappers for external callers and tests.
- Label printing is a confirm-gated agent action through `print_label`, with
  label sheet and label batch previews available before printing or PDF export.
- Photo sorting works as an instruction path. Uncertain groups can be marked
  `needs_review`, confirmed photo sorts link their source photo into review,
  and the UI can edit, merge, remove, or approve review rows.
- A scripted feature-complete eval smoke suite exists in `tests/test_agent.py`.
  Live confirmed examples can be exported through `/assistant/evals/export`
  and checked through `/assistant/evals/replay`. Automated live model replay
  should be added to CI once costs and fixtures are bounded.
- Supabase tables are defined in
  `supabase/migrations/20260713031000_initial_magpie_schema.sql`, and the app's
  repository layer can run against SQLite or Supabase without changing agent
  behavior. Live project provisioning, SQLite migration, and switching the Pi
  to the shared workspace remain deployment gates.
- Hardware bindings are now local model/API/UI concepts for WLED pixels and
  future devices. Active WLED pixel bindings are validated against the installed
  strip, replacement deactivates stale addresses, and the location editor can
  light a binding on the real device. `/hardware/diagnostics` reports coverage,
  conflicts, invalid bindings, unbound lit leaves, and current controller state;
  cabinet hardware is included in demo readiness. Bulk assignment remains the
  commissioning polish item.
- Pi voice and kiosk integration exist, and the kiosk now reports a heartbeat
  plus the latest assistant interaction it has displayed. Voice quality,
  wake/routing confidence, light output, and screen-observed interactions need
  passing live evidence before hands-free "Magpie, find X" can be considered
  feature complete.
- Native iOS/Android clients are not started. The web/PWA surface should remain
  the behavior source of truth while a shared API contract and offline-tolerant
  mobile flows are defined.

## Next Implementation Slice

Make the agent loop measurable and tunable before adding more product surface.

Minimum slice:

1. Keep `/assistant/chat` as the only normal UI/voice command route; legacy
   wrappers are compatibility-only and advertised that way in `/capabilities`.
2. Add a bounded live-model replay command for approved examples so regressions
   in find, put-away, rule learning, and photo sort can be caught deliberately.
3. Add a voice tuning checklist that records microphone level, transcript,
   routed command, backend response, light result, kiosk heartbeat, and whether
   the screen observed the routed interaction for every test phrase.
4. Add pass/fail criteria for the first mobile/web feature-complete demo:
   QR scan, text/photo put-away, voice find, learned rule, review approval,
   label PDF, export, and build/version visibility.

This directly supports the self-improvement loop: the system learns, explains
what it learned, and lets the user correct or remove it.
