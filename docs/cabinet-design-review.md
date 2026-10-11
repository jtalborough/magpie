# Cabinet Workspace Review

Scope: `/cabinet`, served by `index.html`. Independent designer-agent review
of source and a follow-up review of the implementation. Rendered layout has
not been verified because the browser runtime could not start.

## Findings

- The assistant occupied almost a viewport before the selected inventory.
- The cabinet was assigned the narrower desktop column and came last on phones.
- Nested-location inventory lacked the Edit action available on physical bins.
- Numerous secondary item actions displaced the important item controls.

## Implemented Direction

- Following user feedback about the oversized cabinet and off-screen Ask area,
  a compact map sits above selected inventory on the left. Ask remains open
  on the right. The desktop workspace fits the viewport below the measured
  header, with independent scrolling and a sticky selection heading.
- Map cells retain compact dimensions instead of stretching to fill the screen;
  coarse-pointer devices retain larger touch targets. Legacy tool disclosures
  do not consume space in this focused workspace.
- At narrow widths, a bounded, pannable map precedes inventory and a collapsible
  assistant. Pending proposals automatically open the assistant. Editing in
  the selection pane hides the map until Save or Cancel restores the contents.
- Edit and Move remain directly available. Secondary item actions use More.
- Nested Edit identifies both the item and its source placement, preserving
  quantities when one item is stored in several bins.
- Selection failures show a retry; responses for an older selection are ignored.

## Independent Review Follow-up

- Clear cannot hide pending or in-flight assistant work; errors append without
  replacing the proposal. The conversation and status have live-region semantics.
- An active inventory edit requires Save or Cancel before navigation. Saving
  disables controls and duplicate submission; failed saves retain the draft.
  Delayed selection responses and background refreshes cannot replace an editor.
- Map cells and nested headers are native buttons, with visible keyboard focus.
  Editor labels are associated with their inputs.
- Explicit empty size clears stored size metadata and records the prior state
  for Undo. Clients that omit size retain existing metadata.
- Mobile results no longer truncate at eight; they include quantities and links
  to the canonical item detail view.
- Assistant-status failure does not block inventory startup. Separate assistant
  and storage-loading retry controls expose recovery.

Validation: 192 static UI checks, executable JavaScript interaction checks with
simulated dependencies, and the actual size-write branch against temporary SQLite
passed. Full HTTP/backend tests and rendered browser acceptance remain unavailable
in this task's runtime. These checks do not establish production acceptance.

## September 7 Designer And Product Owner Review

Both independent reviews recommend one conversation, one composer, one proposal,
and one confirmation set. Desktop cabinet context belongs on the left and chat
on the right. On smaller screens, conversation should remain primary with
context disclosed separately. The latter is a recommendation, not implemented
or rendered acceptance in this pass; the existing mobile layout remains.

Removed generic Find/Put-away launchers from both initial and restored empty
states and from composer chips. Commands still use the same composer; attachment,
voice, and sort actions remain. Existing desktop right-side layout is local, not
evidence that production has it.

Reproduced flat/lock washer matching at equal size. Added explicit washer kind
and lock-style guards, prevented broad aliases from bypassing primary-name
conflicts, and retained ambiguity when a generic exact name competes with a
specific variant. Photo guidance now requests clarification for unclear types.
This does not establish recognition accuracy on the user's actual photograph.

Physical color mismatch remains REPORTED, not diagnosed. Do not alter channel
order without verifying the active edge backend and observing a controlled
red/green/blue test. Command acknowledgement is not proof of illumination.
The tool palette and active-find palette also differ; route-specific traces
are needed before claiming the user's mismatch is a palette or hardware defect.

Release remains blocked on repository/deployment access. Production was observed
at c2950b1; prior local changes at 61b00b6 and this pass are not deployed.

Validation for this pass: 206 zero-argument identity and static UI checks passed
using the system Python runtime; git diff --check passed. Full backend, browser,
photo-recognition, and physical LED acceptance remain unverified.

## Desktop Implementation

The larger desktop pass applies above 900px. The homepage now serves the same
desktop workspace as /cabinet. Initial narrow homepage visits redirect to the
unchanged /mobile interface, preserving query parameters and fragments. Resizing
an already open workspace does not reload the page or discard a draft.

- Right: unframed cabinet map and selected contents with independent scrolling.
- Left: persistent conversation, active location assignments, and one docked
  composer. Attachment, voice, and Send use vendored Lucide icons with accessible
  labels and native hover titles. Busy and recording labels remain synchronized.
- More holds sort-photo, wake, lighting cleanup, conversation cleanup and rules.
  Location management actions use a disclosure; Light location stays available
  beside the selection title. Existing controls are moved, not duplicated.
- Breakpoint transitions preserve DOM identity, callbacks, draft selection,
  focus, attachments, pending proposals and conversation scroll. Selecting a
  different location resets the actual inventory scrolling element.
- Desktop handoffs restore the requested destination, request and photo IDs
  through the existing proposal workflow. Network failures preserve the draft
  and photos for retry. Confirmation/cancellation consume the handoff and clear
  its destination lock so it cannot leak into the next unrelated request.

The designer reviewed the root-routing, focus and scroll fixes and found no
remaining critical source issues in that scope. Local validation: 206 identity
and static UI checks, seven existing executable interaction groups, the new
jsdom desktop ownership/focus/resize/menu/handoff checks, and Python/JavaScript
syntax checks pass. These are not rendered-browser acceptance.

Local Chromium cannot launch because this runtime cannot load macOS
SystemAppearance. The Publish container workflow now runs pinned jsdom and
Playwright checks on Linux before building/publishing, retains screenshots and
fails closed on geometry/state failures. That gate has not run yet: GitHub's
connector returns 404 and 1Password SSH signing remains unavailable. No
deployment or physical LED result is claimed.

## Remaining Visual Checks

Review at 1440x900, 1024x768, 768x1024, and 390x844 with empty bins, long names,
many items, nested containers, and the phone keyboard. Verify selection,
placement-specific edits, Save/Cancel, Review editing, and pending assistant
confirmations. Static assertions are not a substitute for these browser checks.

## September 7 Navigation and Overlap Correction

Following the user's screenshot and approval of the UX review:

- Desktop navigation is Items, Places, Needs review, and Account. The existing
  homepage and /cabinet links remain valid. Chat stays on the left while Items
  and place management reuse the existing manager in the right pane. Mobile
  retains a dedicated Chat destination and the standalone editing screens.
- The map and selection occupy explicit grid rows. Both clip their own paint;
  the map stretches within its row and the selection header cannot shrink into
  the contents. The repeated selected-place heading is hidden on desktop.
- Review is one counted navigation entry, not another cabinet tab.
- Manager frames remain connected across responsive transitions. The outer
  workspace selector owns workspace changes; embedded account controls are
  hidden. Open dialogs block view switching, and clean Items views refresh
  after parent updates and when reopened.
- Assistant links and Continue here buttons use the existing conversation.
  Existing drafts, photos, pending plans, and in-flight requests block a new
  handoff. Preparation is serialized and restores disabled controls in finally.

Local validation: 192 static UI checks and jsdom ownership/navigation/handoff
checks pass. The independent source recheck found no remaining blockers in this
bounded change. Chromium remains blocked locally by SystemAppearance. The
pre-publication Linux browser gate now checks a 128-bin mixed-size fixture at
2672x1522, 1440x900, 1280x800, 1024x768, and 1024x600, map/header/content
boundaries, embedded Items navigation, and a loaded editor document and draft
surviving a narrow resize. This fixture gate is not authenticated production or
physical-light acceptance.
