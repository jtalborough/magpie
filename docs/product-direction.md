# Product Direction

> Status: direction captured from market research on 2026-07-03.
> POC contract: [`poc-contract.md`](poc-contract.md).

## Thesis

The market already validates QR box inventory, home inventory, label printing,
AI cataloging, and industrial pick-to-light. The open product space is the
combination:

> A hierarchy-native storage map with printable QR labels, AI-assisted capture,
> and optional physical find lights.

The product should start as a web/mobile storage service and treat lit hardware
as an optional upgrade. A smart cabinet is visually stronger, but it is a worse
first wedge because hardware cost, installation variance, Wi-Fi/LED support, and
layout constraints narrow the market too early.

## Product Shape

### Agent-First Experience

The assistant is the product surface, not a helper bolted onto an inventory
database. The default interaction should be:

```text
I found this.
Where does this go?
Where is my solder wick?
This bin is wrong.
Move this pouch to the attic shelf.
Print labels for this drawer.
```

The app should translate those natural requests into storage operations, ask
for confirmation only when it is about to change durable state, and keep manual
forms available as an escape hatch. A user should not need to understand the
schema, choose the right screen, or know whether a target is a room, shelf,
container, bin, or lit location.

Agent responsibilities:

- Find items and light the nearest lit location when possible.
- Suggest where new items belong using learned homes, capacity, purpose, and
  similar contents.
- Recognize when put-away needs a new container or location, preserve the
  captured item evidence, and hand the user to the visual place manager to
  create or select that destination.
- Move containers and inherit child movement automatically.
- Sort photos into multiple candidate item groups instead of filing everything
  as one vague entry.
- Explain uncertainty briefly and keep low-confidence captures in review.
- Learn from corrections such as "not there," "too big," "wrong bin," and
  "put it with soldering."
- Offer label and hardware actions from the same conversational surface.

The product wins only if the inventory gets better with less effort than
digging through the physical storage. Every development slice should be judged
against that friction test.

### Self-Improvement Loop

The agent must get better as the user corrects it. Learning is not a background
analytics feature; it is part of the interaction model.

There are three levels of learning:

- **Vocabulary learning**: "I call these jumper wires" teaches aliases so future
  searches resolve without asking the model.
- **Home learning**: repeated confirmed placements teach where exact or similar
  items usually belong.
- **Rule learning**: explicit preferences become durable placement policy, such
  as "I always keep jewelry in the main cabinet" or "printer tools go in the
  printer cabinet unless they are tiny fasteners."

The agent should listen for rule-shaped corrections:

```text
I always keep jewelry in the main cabinet.
Put soldering stuff in the solder pouch.
Don't put large printer tools in the small-parts cabinet.
Christmas decorations live on the attic shelf.
```

When the user states a durable preference, the assistant should propose the
rule in plain language and ask for confirmation before saving it:

```text
Learn this rule? Jewelry should go in Main Cabinet.
```

After confirmation, the rule should influence every future placement suggestion
and appear in the explanation:

```text
I would put the bracelet clasp in Main Cabinet because you told me jewelry
belongs there.
```

Rules need provenance and reversibility. The user should be able to ask:

```text
Why did you pick this spot?
What rules do you know about jewelry?
Forget that jewelry rule.
```

Implementation direction:

- Store learned rules as structured records, not only free-text memories.
- Keep the original user wording, normalized trigger terms, target location,
  confidence, source command, and timestamps.
- Retrieve relevant rules during `suggest_locations` and agent planning.
- Weight explicit rules above inferred homes, and corrections above plain
  confirmations.
- Treat conflicts as agent-visible context instead of silently overwriting old
  behavior.
- Surface the reason when a rule affected a suggestion.

### 1. Storage Web Service

This is the broad product. It should work for ordinary shelves, rooms, drawers,
boxes, pouches, totes, and cabinets without any hardware.

Core capabilities:

- Create a storage graph: home, room, shelf, cabinet, drawer, bin, box, pouch,
  item.
- Generate QR/barcode IDs for every location, container, and item.
- Print labels as PDFs before direct printer integrations.
- Scan a label to open the object in a web/mobile view.
- Add inventory by text, photo, voice, bulk paste, and CSV import.
- Move a parent container and automatically move its children.
- Search by item name, alias, notes, category, photos, OCR/AI tags, and natural
  language.
- Show a physical path, such as "Garage > Shelf B > Blue Bin 03 > Cable pouch."
- Export data as CSV, JSON, and PDF so users trust the system.

The web version should be the serious setup and management surface: modeling
storage spaces, printing label batches, reviewing uncertain AI captures,
editing hierarchy, and managing hardware bindings.

### 2. Mobile Apps

The daily workflow belongs on the phone. Use a cross-platform app stack rather
than an iPhone-only stack so iOS, Android, and web can share product behavior.

Mobile should optimize for:

- QR scanning.
- Photo capture.
- Voice search and voice put-away.
- Fast "find this" and "put this away" flows.
- Offline-tolerant scans and updates.
- Push or local notifications for audits and reminders.

### 3. Optional Hardware

Hardware is an add-on to the storage graph, not the core data model.

Start with maker-friendly WLED/ESP32 support:

- Bind a location to an LED segment or index range.
- Test the light while labeling or editing a location.
- Trigger "find" lights from search results.
- Let a QR scan of a shelf/bin test or flash that location.

Later hardware tiers:

- Prebuilt LED kits for common shelves, parts cabinets, Gridfinity drawers, and
  storage cubes.
- E-ink shelf/container tags.
- NFC labels.
- Camera verification for high-value drawers.
- Prebuilt lit cabinet modules.

## Two-Tier Business Direction

### Bring Your Own Storage

The entry product is a subscription web/mobile service for people who already
have physical storage. This avoids forcing hardware adoption before the user
feels the retrieval pain.

Best initial customers:

- Makers and workshop owners.
- Electronics hobbyists.
- Craft businesses.
- Professional organizers.
- Small labs and classrooms.
- Garage/storage-heavy households.

Avoid leading with generic insurance inventory. That market is crowded and the
usage frequency is too low. Lead with "find real things in messy real spaces."

### Lit Cabinet / Add-On Product

The cabinet is the premium proof point and hardware attach product.

It should be positioned as:

- A prebuilt or kit cabinet registered into the same storage graph.
- A physical "find mode" for high-value, high-frequency storage.
- One optional hardware binding type among shelves, drawers, tags, and future
  devices.

The cabinet should not fork the product model. It is a location subtree with
light bindings.

## Differentiation

### Hierarchy First

Most lightweight competitors model boxes or item lists. The product should model
the physical world:

```text
Home
  Garage
    Shelf B
      Blue Bin 03
        Cable pouch
          USB-C right-angle adapters
```

Moving `Blue Bin 03` moves everything inside it. This is the core conceptual
advantage.

### Labels As Infrastructure

Labels should be universal and portable:

- QR readable by any camera.
- Human-readable short name and path.
- Short code for voice/manual lookup.
- Duplicate labels for multiple sides of a bin.
- Reprint damaged labels.
- Batch labels for a shelf, drawer, cabinet, or moving session.

Support order:

1. Plain paper PDF.
2. Avery-style sheets.
3. Brother/DYMO/Zebra thermal templates.
4. Direct printer integrations only after PDF templates are reliable.

### AI That Reduces Data Entry

AI should create useful rough inventory without pretending to be certain.

Useful AI jobs:

- Detect likely items from a bin photo.
- Propose names, categories, and aliases.
- Infer storage affinity and suggest a home.
- Extract text from packaging and labels.
- Find duplicates.
- Turn a shelf video sweep into candidate containers/items.
- Mark low-confidence entries for review.

Corrections are product data. "Not here," "rename this," and "move this" should
feed search aliases, placement memory, and future suggestions.

### Physical Find Mode

Lights should be framed as progressive enhancement:

- If an exact bin has lights, flash it.
- If an item is inside an unlit pouch in a lit drawer, flash the drawer and use
  words for the pouch.
- If nothing in the path is lit, show the path and label names.

This keeps the product useful in every storage setup.

## MVP

Build around three first-class objects:

- `Location`: room, shelf, drawer, cabinet, rack, locker, storage unit.
- `Container`: bin, box, pouch, tote, tray, bag, parts organizer.
- `Item`: physical object, consumable, tool, part, document, collection object.

Every object can have:

- QR/barcode ID.
- Human-readable label.
- Parent object.
- Photos.
- Notes.
- Custom fields.
- Quantity.
- Owner/shared users.
- Move history.
- Print label template.
- Optional hardware binding.

First shippable slice:

1. Web app for hierarchy creation, search, and label batch generation.
2. Mobile-friendly scan view for any QR code.
3. Add item/container/location by text and photo.
4. Printable QR label PDFs.
5. Search result path display.
6. Container move with inherited child movement.
7. WLED location binding and "find" flash for the current cabinet.

## Risks

- Setup burden: the system must work with incomplete inventory.
- Labeling burden: batch print and reprint workflows are core, not polish.
- Data-entry burden: photo, voice, and rough AI capture are necessary.
- Hardware support: keep LEDs optional and maker-first until the software loop
  is proven.
- Printer support: PDF first; direct thermal printer support later.
- Privacy: storage inventory can reveal valuables, documents, tools, and home
  layout. Export, deletion, sharing permissions, and a credible self-host/local
  story matter.
- Physical entropy: people move things without updating software. Scanning,
  quick moves, audit prompts, and correction flows must be nearly frictionless.

## Competitive Lessons

Closest product classes:

- QR storage apps validate low-cost labels and scan-to-contents workflows.
- Home inventory apps validate photos, categories, rooms, and household use.
- SMB inventory tools validate label printing, reports, and web/mobile sync.
- Open-source parts/home inventory tools validate self-hosting and maker demand.
- Industrial pick-to-light validates the interaction pattern: identify item,
  guide physically, confirm transaction, update state.

The product should not compete head-on with general inventory SaaS or warehouse
automation. The wedge is personal and workshop retrieval: help people find
physical things without digging.
