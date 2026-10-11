# Epic: E-ink Location Tags

> Status: concept / major epic. This is not an implementation plan for the current LED prototype;
> it is the hardware/software track for dynamic physical labels.

## Outcome

Every important physical location can carry a persistent, readable label generated from Locus:
the memorable location word, a QR code, the path/bin ID, and a compact contents summary. The label
updates when inventory changes and still reads when the network, browser, or power is unavailable.

## Product position

E-ink tags are not a replacement for LEDs. They are the durable label layer.

- **LEDs answer "where now?"** They flash immediately during find and put-away.
- **E-ink answers "what is this place?"** It stays readable across days or months.
- **Printed labels remain the baseline.** E-ink is for locations where contents or status change
  often enough to justify the hardware.

## Hardware direction

Primary pilot: **OpenEPaperLink + electronic shelf labels**.

Why:

- one access point can manage many low-power tags;
- used ESL tags are cheap enough to consider per-bin or per-drawer coverage;
- the access point/API model fits the existing Pi service better than one ESP32 per label;
- tag persistence matches the storage use case.

Fallback / limited-use path: **ESPHome + Waveshare e-paper**.

Use it for a single bench/status display or proof-of-rendering experiment. Do not choose it for
128 bin labels unless the OpenEPaperLink path fails.

## Standard tag choice

Default standard: **thin 2.13" black/white/red tags** for bins.

Preferred candidate: **Hanshow Nebular 213**, when available pre-flashed or easily flashable for
OpenEPaperLink.

Why this size:

- physically closer to a real bin label than a dashboard;
- thin enough to mount on small drawers, bins, and box faces;
- large enough for a memorable word, QR code, and one compact contents line;
- cheap enough to imagine dozens of locations after the pilot.

Color standard:

- use **BWR only** as the first color family: black, white, red;
- treat red as an attention/status channel, not decoration;
- avoid mixing yellow and red tags in the same first system unless availability forces it.

Fallback tag: **2.9" BWR tags** such as Chroma 29 or Solum M3. They are better supported and easier
to source in bulk, but bulkier. Use them for drawers, rugged boxes, shelf labels, or pilots where
2.13" firmware/support is the risk.

Avoid for now:

- **Chroma 21** despite the attractive size: OpenEPaperLink support is not ready enough to standardize
  on it.
- random unflashed retail ESL lots unless the goal is specifically to build a flashing workflow.

## Data model

Add display bindings parallel to light bindings:

```text
location
  id
  label
  parent_id
  light_target?      # existing direction: controller/segment/pixels
  light_indexes?
  display_target?    # access point or display backend
  display_tag_id?    # tag MAC/id
  display_template?  # small-bin, drawer, box, shelf, status
  display_size?      # physical resolution/class
```

Keep the binding on `Location`, not on `Item`. Items move; labels belong to places.

## Tag templates

Minimum viable template:

- large memorable word;
- small system name: `Locus`;
- QR code linking to `/locations/{id}` or a future mobile detail page;
- compact path or bin ID;
- contents summary: top 1-3 items or `empty`.

Later templates:

- drawer overview with child cells;
- rugged-box label with box name and major categories;
- shelf label with contained boxes;
- review/low-stock flag view;
- temporary "put away here" destination view if refresh latency is acceptable.

## Update rules

Tag updates should be sparse and explainable:

- location created, renamed, moved, or deleted;
- display binding changed;
- item added, moved in, moved out, renamed, or quantity/status changed;
- manual `refresh label` action;
- batch `refresh all labels in this drawer` action.

Avoid using tags for search-time flashing, animations, or chatty status. E-ink refresh is slow and
visible; it should feel intentional.

## API shape

Initial local API:

```text
GET  /locations/{id}/label-preview
POST /locations/{id}/display-binding
POST /locations/{id}/label-refresh
POST /display/refresh
GET  /display/tags
```

Renderer boundary:

```text
Location + contents -> tag image -> backend upload
```

Backends:

- `openepaperlink`: render PNG/BMP payload and send to the OpenEPaperLink AP;
- `file`: write generated images to disk for visual review and tests;
- `noop`: test mode.

## Implementation slices

1. **Document and purchase pilot hardware** — choose one AP, 5-10 thin 2.13" BWR tags, and optionally
   1-2 larger 2.9" BWR tags for drawer/box labels.
2. **File renderer** — generate deterministic label PNGs from fake locations and inventory.
3. **Display binding table/fields** — persist tag ID, backend, template, and size on locations.
4. **Preview UI** — show the generated label image on the location detail/edit surface.
5. **OpenEPaperLink backend** — push one generated image to one real tag from the Pi service.
6. **Inventory-triggered refresh** — enqueue refresh when a bound location's visible contents change.
7. **Batch operations** — bind, preview, and refresh all labels under a drawer/cabinet.

## Acceptance criteria

- A location can be bound to a physical tag without affecting LEDs or search.
- A generated label is deterministic and testable without hardware.
- A QR code on the label resolves to the right local location page.
- Moving an item into a bound bin updates the label preview and can refresh the real tag.
- If the tag backend is offline, inventory writes still succeed and the label refresh is queued or
  clearly marked failed.

## Open questions

- Tag density: every small bin, every drawer, every box, or mixed?
- Confirmed standard tag: Hanshow Nebular 213, or a different 2.13" BWR model with stronger
  OpenEPaperLink supply/support?
- Should small-bin labels show item names, or only the location word + QR so contents stay mostly
  in the phone UI?
- Do we want QR codes to open the Pi UI directly, or a future stable local domain?
- Where should refresh failures live: events table, a display queue table, or both?
