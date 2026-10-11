# Magpie Product Vision

> Status: active product direction.
> Detailed strategy: [`product-direction.md`](product-direction.md).
> Current product contract: [`poc-contract.md`](poc-contract.md).

## North star

> Magpie remembers where physical things live, tells people where they belong,
> and guides them there with the least possible effort.

Magpie is an agent-led storage manager for homes, workshops, studios, labs, and
small businesses. It is not primarily an inventory database. The inventory and
storage graph are the durable memory that let the agent answer practical
questions:

```text
Magpie, find my USB drives.
Put these M3 screws away.
These need a bigger bin.
I always keep jewelry in the main cabinet.
Clear the lights.
```

The product succeeds when using Magpie is faster and easier than searching the
physical storage yourself.

## Product shape

Magpie starts as software that works with storage people already own. QR labels,
lights, displays, printers, cameras, and dedicated cabinets progressively
enhance the same system. They do not create separate inventories or products.

The broad product is:

- A central, containerized Magpie service.
- One reliable physical storage graph per workspace.
- A conversational storage agent with validated tools.
- Shared web, mobile, and kiosk experiences.
- Secure local edge controllers for physical devices.
- Optional Magpie hardware registered into the same graph.

## One physical storage graph

Magpie models where things actually are:

```text
My Workshop
|-- Main Cabinet
|   |-- Drawer 01
|   |   `-- M4 Hardware
|   `-- Drawer 02
|-- Printer Cabinet
|   `-- Soldering Drawer
|       `-- Solder Pouch
|-- Garage
|   `-- Shelf B
|       `-- Blue Tote
`-- Attic
    `-- Holiday Storage
```

The core records are:

- **Locations:** roots, rooms, cabinets, shelves, drawers, bins, and trays.
- **Containers:** boxes, totes, pouches, bags, and organizers that can move.
- **Items:** tools, parts, materials, consumables, and collections.
- **Placements:** the current relationship between an item and a destination.
- **Bindings:** optional lights, displays, printers, cameras, or sensors.
- **Rules:** durable user preferences that affect placement.
- **Evidence:** photos, measurements, scans, notes, and interaction provenance.

Moving a container moves everything inside it. Lighting is optional. When an
exact destination is not lit, Magpie lights the nearest useful ancestor and
uses words for the rest of the path.

## The core loops

### Find

1. The user asks by voice or text, or scans a label.
2. Magpie searches names, aliases, notes, photos, categories, and context.
3. It returns complete physical paths, not just cabinet names.
4. Multiple results receive distinct colors.
5. The nearest available locations light in matching colors.
6. The active color key remains visible until the user clears it.

Find is a read operation. Magpie should answer and guide immediately without a
confirmation step.

### Put away

1. The user describes, photographs, or scans the item.
2. Magpie identifies it or asks one short clarifying question.
3. It recommends an existing destination using fit, related contents, learned
   homes, explicit rules, and available capacity.
4. The proposed destination lights immediately.
5. Every active surface shows the same item, path, light color, and action.
6. The user confirms, corrects, or cancels.
7. Magpie saves the item, placement, quantity, and attached photo together.

Put-away should normally be one request and one confirmation. A missing or
inappropriate destination is a handoff to the visual Storage manager, not an
invitation for voice to restructure the hierarchy.

### Correct and learn

Corrections are product data:

```text
Wrong item.
Use a smaller bin.
Put it with the soldering tools.
Jewelry always goes in the main cabinet.
```

Magpie distinguishes among:

- A correction to the current proposal.
- A vocabulary alias.
- A learned home for an item or item family.
- A durable placement rule.

Rules preserve the original wording, normalized triggers, target, source,
confidence, and timestamps. They remain inspectable and reversible:

```text
Why did you choose this?
What rules do you know about jewelry?
Forget the jewelry rule.
```

Explicit rules outrank inferred homes. Corrections outrank plain confirmations.
Conflicts are surfaced for review instead of silently overwriting policy.

## Product surfaces

Every surface shares the same storage graph, agent plans, confirmation state,
location paths, and light colors. They deliberately differ in density and input
priority.

### Kiosk

The kiosk is a persistent, hands-free cabinet display:

- Voice-first with no required keyboard or mouse.
- Large listening, processing, confirmation, completion, and error states.
- Shows exactly what Magpie heard.
- Shows active color-coded finds and physical paths.
- Supports spoken confirmation, correction, cancellation, undo, and clear.
- Keeps important states visible long enough to understand them.
- Offers touch as an optional future fallback.

The kiosk does not configure storage places.

### Mobile

Mobile is the everyday capture and retrieval tool:

- Find and put away by voice or text.
- Photo capture and attachment.
- QR scanning and label-driven actions.
- Shared confirmation and correction.
- Persistent active-light list.
- Offline-tolerant capture and retry.
- Focused item and location details.

The installable web app is the first mobile product for iPhone and Android.
Native apps are justified later by capabilities that the web cannot provide
well, such as dependable background behavior or ARKit/LiDAR measurement.

### Web

Web is the serious setup and management surface:

- Global inventory organized by what items are, with location as context.
- Search, filtering, review, bulk editing, import, and export.
- Visual creation and organization of storage roots and nested places.
- Move, archive, and restore operations with consequence previews.
- QR label generation, batch printing, and reprinting.
- Rule, evidence, and uncertain-capture review.
- Hardware binding, pairing, readiness, and diagnostics.

The shared product navigation is:

```text
Ask | Items | Storage | Review | Account
```

## Agent boundary

Magpie owns the conversation and storage decisions. The application remains the
system of record and validates every tool call.

- Reads, search, and physical guidance can happen immediately.
- Durable inventory changes require an authoritative pending plan and user
  confirmation.
- Pending plans expire and newer requests supersede stale proposals.
- Place creation and hierarchy management remain visual.
- Voice is for find, put away, correction, confirmation, undo, and clear.
- Low confidence produces a short question instead of a silent guess.
- Every write records provenance and remains correctable.

Fast, inexpensive paths handle find, light, clear, confirmation, and ordinary
put-away. A bounded deeper planner handles genuine ambiguity, reorganization,
cleanup, and multi-step requests. The planner receives narrow Magpie tools
rather than direct database or hardware access.

## Placement intelligence

Magpie should never fall back to "first empty bin" when better evidence exists.
It ranks destinations using:

- **Fit:** item dimensions and destination capacity.
- **Affinity:** purpose, labels, notes, and existing contents.
- **Memory:** previously confirmed homes and corrections.
- **Rules:** explicit user placement policy.
- **Availability:** active, item-accepting, non-conflicting destinations.
- **Guidance:** whether the user can identify or light the destination.

The recommendation should be concise. Detailed provenance is available when the
user asks why, but routine voice responses should not read a long reason aloud.

## Edge and hardware model

The central service owns workspaces, storage data, agent plans, confirmation,
and policy. Raspberry Pis and other local devices are secure edge controllers
for:

- Microphones and speakers.
- Kiosk displays.
- WLED or addressable lights.
- Label printers.
- Cameras and check stations.
- Future sensors and persistent displays.

Each edge device is paired to a workspace with only the scopes it needs. Device
health, runtime version, and build commit are visible and queryable. A software-
only workspace requires no hardware services.

## Hardware progression

Hardware strengthens an already useful software product:

1. Printable QR labels and PDFs.
2. Existing WLED and ESP32 lighting.
3. Secure Raspberry Pi edge controllers.
4. Label-printer and check-station integrations.
5. Camera plus reference-grid size assistance.
6. Native spatial measurement where it proves valuable.
7. Prebuilt lighting kits and the dedicated Magpie cabinet.

The main cabinet is a premium, preconfigured location subtree with bindings. It
is not a separate database, assistant, or product workflow.

## Build order

### 1. Dependable foundation

- Control database egress and model cost.
- Make deployments immutable, observable, and reversible.
- Make voice, photos, lighting, and confirmation reliable.
- Keep build versions visible across central and edge services.

### 2. Software anyone can use

- First-run workspace and storage onboarding.
- Reliable nested storage lifecycle.
- Global item inventory and review.
- Installable mobile web experience.
- QR labels, import, export, and backup.

### 3. Smarter Magpie

- Stronger fit and affinity scoring.
- Durable aliases, homes, rules, and conflict review.
- Better visual identification and evidence handling.
- Bounded deep planning for complex organization work.

### 4. Device platform

- Secure pairing and scoped command routing.
- Device setup, readiness, health, and diagnostics.
- Lights, displays, printers, microphones, and cameras as independent services.

### 5. Check station and native sensing

- Camera and known-reference sizing first.
- Weight and dimension evidence where useful.
- Native spatial measurement only after the capture loop proves its value.

### 6. Dedicated Magpie hardware

- Productized cabinet and lighting kits.
- Fast setup against the same workspace graph.
- No hardware-only fork of the software experience.

## Product measures

Product decisions should improve these outcomes:

- Time from request to useful find guidance.
- Time and interactions required to put an item away.
- Percentage of proposals confirmed without correction.
- Percentage of corrections that improve later decisions.
- Photo and evidence retention through confirmation.
- Search success across aliases and multiple roots.
- Setup completion without hardware.
- Edge-service reliability when hardware is configured.
- Database egress and model cost per active workspace.
- User trust in export, history, correction, and deletion.

## The reference experience

```text
User: Magpie, put away these M4 T-nuts.

Magpie identifies the hardware, finds the existing M4 T-nut home, verifies that
the destination fits, assigns a color, lights the drawer, shows the same proposal
on kiosk and phone, and asks for confirmation.

User: Yes.

Magpie stores the inventory and photo, confirms completion on screen and by
voice, and uses that placement as stronger evidence next time.
```

That is Magpie: a physical storage system that becomes easier to use and more
accurate every time the user interacts with it.
