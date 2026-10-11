from pathlib import Path


INDEX_HTML = Path("src/searchable_storage_led/static/index.html")
STATIC_DIR = INDEX_HTML.parent
ACCOUNT_HTML = STATIC_DIR / "account.html"
KIOSK_HTML = STATIC_DIR / "kiosk.html"
MOBILE_HTML = STATIC_DIR / "mobile.html"
STORAGE_HTML = STATIC_DIR / "storage.html"
REVIEW_HTML = STATIC_DIR / "review.html"
CHECK_STATION_HTML = STATIC_DIR / "check-station.html"
PHOTO_UTILS_JS = STATIC_DIR / "photo-utils.js"
LOCATION_DISPLAY_JS = STATIC_DIR / "location-display.js"
CONFIRMATION_JS = STATIC_DIR / "confirmation.js"
PRODUCT_SHELL_JS = STATIC_DIR / "product-shell.js"
THEME_CSS = STATIC_DIR / "theme.css"
API_PY = Path("src/searchable_storage_led/api.py")


def test_cabinet_workspace_keeps_assistant_beside_map_and_inventory() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "--cabinet-header-height" in html
    assert "new ResizeObserver(resizeWorkspace).observe(header)" in html
    assert "grid-template-rows:minmax(180px,.9fr) minmax(240px,1.1fr)" in html
    assert ".cabinet-workspace .stack { grid-column:2; grid-row:1/-1;" in html
    assert ".cabinet-workspace #selectionPanel { grid-column:1; grid-row:2;" in html
    assert ".cabinet-workspace .stack > .advanced-only { display:none !important; }" in html
    assert "cabinet-ask" not in html
    assert "height:calc(100dvh - 112px)" not in html
    assert "overflow:auto; align-content:start;" in html
    assert ".cabinet-workspace #command { height:64px;" in html
    assert "@media (min-width:901px) and (pointer:coarse)" in html


def test_cabinet_edits_guard_navigation_and_serialize_save() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for name in ("selectBin", "selectChildLocation", "selectContainer", "showReviewView", "openStorageManager", "startEditItem", "clearBinInventory", "refreshCurrentView"):
        body = html.split(f"function {name}(", 1)[1].split("\n    }", 1)[0]
        assert "if (!leaveItemEdit()) return;" in body
    assert "if (!edit || edit.saving) return;" in html
    assert "if (activeItemEdit?.saving) return;" in html
    assert "selectedBin !== id || activeItemEdit" in html
    assert "selectedBin !== binId || activeItemEdit" in html
    assert "controls.forEach((control) => { control.disabled = true; });" in html
    assert "controls.forEach((control) => { control.disabled = false; });" in html
    assert "await selectChildLocation(edit.returnLocation)" in html
    assert 'window.addEventListener("beforeunload"' in html


def test_cabinet_controls_have_native_keyboard_semantics() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    for name in ("renderGrid", "cellFor"):
        body = html.split(f"function {name}(", 1)[1].split("\n    }", 1)[0]
        assert '"button"' in body
        assert '.type = "button"' in body
    for field in ("Name", "Qty", "Unit", "Category", "Aliases", "Size", "Dimensions", "Description"):
        assert f'<label for="edit{field}">' in html
    assert 'id="results" role="log"' in html
    assert 'id="status" role="status"' in html


def test_narrow_cabinet_prioritizes_selection_and_can_reveal_proposals() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert ".cabinet-workspace #selectionPanel { grid-column:1; grid-row:2; height:auto;" in html
    assert ".cabinet-workspace .stack { grid-column:1; grid-row:3; height:auto; }" in html
    assert 'disclosure.className = "cabinet-assistant"' in html
    assert "disclosure.open = !narrow.matches" in html
    assert "function revealCabinetAssistant()" in html
    assert "revealCabinetAssistant();" in html.split("function renderPendingPlan(", 1)[1].split("\n    }", 1)[0]


def test_assistant_status_failure_does_not_block_storage_startup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    load_body = html.split("async function load()", 1)[1].split("\n    }", 1)[0]
    assert "loadAssistantStatus();" in load_body
    assert 'api("/assistant/status")' not in load_body
    assert 'byId("assistantRetryBtn").onclick = loadAssistantStatus' in html
    assert 'byId("retryLoadBtn").onclick = startApplicationView' in html
    assert "retry.hidden = false;" in html


def test_mobile_results_keep_all_matches_and_link_to_item_details() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")
    body = html.split("function resultRows(", 1)[1].split("\n    }", 1)[0]
    assert ".slice(" not in body
    assert "result.quantity == null" in body
    assert "result.unit" in body
    assert "/storage?view=inventory&item=${encodeURIComponent(result.item_id)}" in body


def test_browser_photo_uploads_share_bounded_normalization() -> None:
    utility = PHOTO_UTILS_JS.read_text(encoding="utf-8")

    assert "const DEFAULT_MAX_EDGE = 2048" in utility
    assert "const DEFAULT_JPEG_QUALITY = 0.82" in utility
    assert "const MAX_UPLOAD_BYTES = 10 * 1024 * 1024" in utility
    assert "largestEdge > maxEdge" in utility
    assert "normalizedBytes < sourceBytes" in utility
    assert "window.magpiePhotos = Object.freeze" in utility
    for page in (INDEX_HTML, MOBILE_HTML, STORAGE_HTML, CHECK_STATION_HTML):
        assert '<script src="/photo-utils.js"></script>' in page.read_text(encoding="utf-8")


def test_location_paths_share_ambiguity_aware_labels() -> None:
    utility = LOCATION_DISPLAY_JS.read_text(encoding="utf-8")

    assert "function catalogLabel(location, catalog = [])" in utility
    assert "candidate.parent_id === location.parent_id" in utility
    assert "candidate.active !== false" in utility
    assert "`${label} · ${location.id}`" in utility
    assert "window.MagpieLocationDisplay" in utility
    for page in (INDEX_HTML, MOBILE_HTML, STORAGE_HTML, KIOSK_HTML):
        assert '<script src="/location-display.js"></script>' in page.read_text(
            encoding="utf-8"
        )


def test_check_station_connects_capture_measurement_placement_and_save() -> None:
    html = CHECK_STATION_HTML.read_text(encoding="utf-8")

    assert 'capture="environment"' in html
    assert 'window.magpiePhotos.prepare(file)' in html
    assert 'api("/photos"' in html
    assert 'api("/measurement/photo"' in html
    assert 'api("/placement/size"' in html
    assert 'api("/placement/suggest"' in html
    assert 'api("/inventory"' in html
    assert "size_source:measurementSource" in html
    assert 'href="/measurement/calibration-grid.pdf"' in html
    assert "Enter dimensions manually" in html


def test_check_station_only_offers_item_ready_destinations() -> None:
    html = CHECK_STATION_HTML.read_text(encoding="utf-8")

    assert (
        "locations.filter((location) => location.accepts_items === true)" in html
    )
    assert "!location.child_count" not in html
    assert "No storage destination yet" in html
    assert 'href="/storage?view=places">Open Storage</a>' in html
    assert 'setStatus("Add a storage destination", "setup")' in html
    assert 'id="statusAction" class="status-action"' in html
    assert 'byId("statusAction").hidden = false;' in html
    assert ".status-line.setup .status-dot { background:var(--warning); }" in html


def test_item_identity_clarification_is_one_tap_on_typed_and_voice_paths() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'res.action === "clarify_item_identity"' in html
    assert 'response.action === "clarify_item_identity"' in html
    assert "function renderIdentityClarification(response)" in html
    assert 'data-identity-choice="${escapeHtml(item.name || "")}"' in html
    assert 'byId("command").value = `Put away ${button.dataset.identityChoice || ""}`' in html


def test_assistant_followups_carry_recent_context() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "let lastAssistantContext" in html
    assert "function runContextFollowup" in html
    assert "Previous assistant context JSON:" in html
    assert "Resolve pronouns like this, that, those, it, they, and them" in html
    assert "call search_items before answering" in html


def test_fast_putaway_displays_confirmable_plan_for_correction_loop() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'api("/assistant/chat"' in html
    assert 'mode: "ask"' in html
    assert "const res = chat.decision || chat.raw || chat" in html
    assert "adoptPutAwayPlan(" in html


def test_photo_upload_and_attachment_retries_are_visible_and_durable() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'setStatus("Uploading photo")' in html
    assert 'setStatus("Photo uploaded")' in html
    assert 'queuePhotoAttachmentRetry(retryRequest)' in html
    assert 'api("/photos/attach-retry"' in html
    assert 'window.addEventListener("online", retryQueuedPhotoAttachments)' in html
    assert "response.placement || null" in html
    assert "Put-away ready" in html
    assert "Put-away response did not include a confirmable plan." in html
    assert "function pendingPhotoThumbHtml(photo, index)" in html
    assert 'data-remove-pending-photo="${escapeHtml(index)}"' in html
    assert 'await api("/photos/" + encodeURIComponent(photo.id), {method: "DELETE"})' in html
    assert "confirmationPhotoEvidence(pendingPlanPhotoIds)" in html
    assert (
        "pendingPhotos = photoIds.map((id) => "
        "({id, url: `/photos/${encodeURIComponent(id)}`}))"
    ) in html


def test_context_followup_tries_deterministic_size_correction_first() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'await api("/assistant/followup"' in html
    assert "body: JSON.stringify({text, context})" in html
    assert 'learned.handled' in html
    assert 'learned.action === "clarify_placement_destination"' in html
    assert "Which location?" in html
    assert "Need a location choice" in html
    assert "const colorPlan = clarificationColorAssignments(learned.candidates || [])" in html
    assert "lightClarificationCandidates(learned.candidates || [], colorPlan)" in html
    assert "function clarificationColorAssignments(candidates)" in html
    assert "function lightClarificationCandidates(candidates, colorPlan)" in html
    assert "Lit location choices" in html
    assert 'data-followup-location="${locationId}"' in html
    assert 'button.onclick = () => {' in html
    assert 'byId("command").value = button.dataset.followupLocation || ""' in html
    assert 'learned.action === "learned_placement_correction"' in html
    assert 'learned.action === "learned_quantity_correction"' in html
    assert 'learned.action === "learned_identity_correction"' in html
    assert 'learned.action === "learned_category_correction"' in html
    assert 'learned.action === "learned_alias_correction"' in html
    assert 'const proposedRule = learned.proposed_rule || null' in html
    assert 'const activeRule = proposedRule?.review_status === "promoted"' in html
    assert "Active now. Manage it in Rules or use Undo." in html
    assert 'Pending review in Learning.' in html
    assert 'if (proposedRule) loadLearningExamples()' in html
    assert "Updated quantity" in html
    assert "Updated item" in html
    assert "Updated category" in html
    assert "Learned alias" in html
    assert 'previous_name: learned.previous_name || ""' in html
    assert 'alias: learned.alias || ""' in html
    assert "aliases: learned.aliases || []" in html
    assert 'category: learned.category || ""' in html
    assert "quantity: learned.quantity || null" in html
    assert "Moved and learned" in html
    assert "Learned size rule" in html
    assert 'rememberAssistantContext(learned.action || "learned_correction"' in html


def test_pending_plan_followups_can_be_questions() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    api = API_PY.read_text(encoding="utf-8")

    assert 'api("/assistant/revise"' in html
    assert "If the follow-up is a question, answer briefly" in api
    assert "Return the full corrected plan when the plan changes" in api
    assert "If the plan should not change, return no write tools and explain" in api


def test_confirm_keeps_color_key_and_relights_plan() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const confirmedPlan = pendingPlan.slice()" in html
    assert "renderAppliedPlan(confirmedPlan, confirmedColorPlan" in html
    assert "await lightAllPlanSteps(confirmedPlan, confirmedColorPlan, {afterConfirm: true})" in html
    assert "Relight all colors" in html
    assert "Color key remains active for putting parts away." in html
    assert 'const confirmButton = node?.querySelector("[data-act=\'confirm\']")' in html
    assert "if (confirmButton?.disabled) return" in html
    assert 'confirmButton.textContent = "Saving"' in html
    assert "confirmButton.disabled = false" in html


def test_pending_plan_preserves_photos_and_lights_immediately() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "pendingPlanPhotoIds" in html
    assert "pendingSourceInteractionId" in html
    assert "const currentPhotoIds = pendingPhotos.map((p) => p.id)" in html
    assert "pendingPlanPhotoIds = currentPhotoIds.length ? currentPhotoIds : pendingPlanPhotoIds.slice()" in html
    assert "photo_ids: pendingPlanPhotoIds.slice()" in html
    assert "source_interaction_id: pendingSourceInteractionId" in html
    assert "lightAllPlanSteps(plan, colorPlan)" in html
    assert "pendingPlanPhotoIds = []" in html


def test_mobile_pending_photo_removal_deletes_upload_and_plan_owns_evidence() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "function removePendingPhoto(index)" in html
    assert 'await api(`/photos/${encodeURIComponent(photo.id)}`, {method:"DELETE"})' in html
    assert (
        "renderPendingPlan(result.plan, result.message, interactionId, "
        "command, photoIds, result.confirmation)"
    ) in html
    assert "pendingPhotos = [];" in html
    assert "keepPhotoEvidence(request.photo_ids)" in html
    assert 'request.photo_ids.length ? "Canceled · photo kept" : "Canceled"' in html


def test_confirmed_plan_shows_photo_attachment_summary() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function photoAttachmentSummaryHtml(response)" in html
    assert "response.photo_attachments" in html
    assert "item_links" in html
    assert "location_links" in html
    assert "review_links" in html
    assert "Photos:" in html
    assert "photoAttachmentSummaryHtml(response)" in html
    assert "renderAppliedPlan(confirmedPlan, confirmedColorPlan, r.applied || [], node, r)" in html


def test_photo_only_submit_uses_sorting_agent_not_find_path() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const PHOTO_ONLY_COMMAND" in html
    assert "Do not file the whole image as a generic assortment" in html
    assert (
        'await runAgentCommand(PHOTO_ONLY_COMMAND, '
        '{commandText: "photo", mode: "agent", source: requestSource, throwOnError: options.throwOnError})' in html
    )


def test_find_results_can_select_move_or_remove_loose_locations() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-select="${escapeHtml(locationId)}"' in html
    assert 'data-move-from="${escapeHtml(locationId)}"' in html
    assert 'data-remove-from="${escapeHtml(locationId)}"' in html
    assert "function selectStorageLocation" in html
    assert "selectStorageLocation(selectedBin)" in html
    assert "function moveTargetPath(id)" in html
    assert "function matchingMoveTargets(query" in html
    assert "function renderInlineMoveTargets(picker, itemId, fromBin)" in html
    assert 'data-move-search aria-label="Find destination"' in html
    assert 'data-move-destination="${escapeHtml(target.id)}"' in html
    assert "const visible = matches.slice(0, 30)" in html
    assert "async function moveInlineItem(picker, itemId, fromBin, toBin)" in html
    assert 'class="move-dest"' not in html
    assert ".location-picker-results { display: grid; gap: 5px; max-height: 240px; overflow-x: hidden;" in html
    assert "place-items: center stretch" in html


def test_find_results_use_color_assignments_for_multiple_matches() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    theme = THEME_CSS.read_text(encoding="utf-8")

    assert "function findColorAssignments" in html
    assert 'id="activeFindList"' in html
    assert 'id="assistantActiveFindsPanel" class="product-active-finds assistant-active-finds"' in html
    assert 'id="assistantActiveFindsList" class="product-active-finds-list"' in html
    assert 'id="assistantClearActiveFinds"' in html
    assert "const ACTIVE_FIND_COLOR_NAMES = new Map([" in html
    assert '["255,0,0", "Red"]' in html
    assert 'panel.hidden = rows.length === 0' in html
    assert "async function clearAssistantLights()" in html
    assert 'byId("assistantClearActiveFinds").onclick = clearAssistantLights' in html
    assert "await load();\n        await refreshActiveFinds();" in html
    assert "function renderActiveFinds" in html
    assert 'entry.kind === "put_away" ? "Put away"' in html
    assert 'await api("/lights/active")' in html
    assert "setLitBinColors(assignments)" in html
    assert "lit-color" in html
    assert "find-heading" in html
    assert ".product-active-finds {" in theme
    assert ".product-active-find-swatch {" in theme


def test_confirmed_plan_corrections_can_remove_last_filing() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "maybeHandleConfirmedPlanCorrection(text)" in html
    assert 'lastAssistantContext.kind !== "confirmed_plan"' in html
    assert "resolveAppliedItemId" in html
    assert "Removed from" in html
    assert 'api("/assistant/chat"' in html
    assert 'mode: "find"' in html


def test_ui_displays_build_label_from_health() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "health.build_label" in html
    assert "cabinetLayoutMeta(build)" in html
    assert "wired pixels${suffix}" in html
    assert "Main Cabinet profile${suffix}" in html
    assert 'fetch(path, {cache: "no-store", ...options})' in html


def test_ui_links_installable_web_app_metadata() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<meta name="theme-color" content="#0f766e">' in html
    assert '<meta name="mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<link rel="icon" href="/app-icon.svg" type="image/svg+xml">' in html
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">' in html
    assert "<title>Magpie Storage</title>" in html


def test_visual_management_pages_are_installable_pwa_entry_points() -> None:
    for page in (STORAGE_HTML, ACCOUNT_HTML):
        html = page.read_text(encoding="utf-8")
        assert '<meta name="mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-title" content="Magpie">' in html
        assert (
            '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
            in html
        )
        assert '<link rel="manifest" href="/manifest.webmanifest">' in html
        assert '<link rel="icon" href="/app-icon.svg" type="image/svg+xml">' in html
        assert (
            '<link rel="apple-touch-icon" sizes="180x180" '
            'href="/apple-touch-icon.png">'
        ) in html


def test_ui_registers_service_worker_for_installed_app_shell() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    auth = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")

    assert "function registerServiceWorker()" in html
    assert '"serviceWorker" in navigator' in html
    assert 'navigator.serviceWorker.register("/service-worker.js")' in html
    assert 'viewport-fit=cover' in html
    assert 'env(safe-area-inset-bottom)' in html
    assert '@media (pointer: coarse)' in html
    assert 'await setupInitialActionFromUrl();' in html
    assert 'await setupInitialPromptFromUrl();' in html
    assert "registerServiceWorker();" in html
    assert 'register("/service-worker.js", {updateViaCache:"none"})' in auth
    assert ".then((registration) => registration.update())" in auth
    assert "const controlledAtLoad = Boolean(navigator.serviceWorker.controller)" in auth
    assert 'navigator.serviceWorker.addEventListener("controllerchange"' in auth
    assert "if (!controlledAtLoad || reloadingForUpdate) return" in auth
    assert "window.location.reload()" in auth


def test_scanned_workspace_is_temporary_until_the_label_is_authorized() -> None:
    auth = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")

    assert 'const SCANNED_WORKSPACE_KEY = "magpie.auth.scan.workspace.v1"' in auth
    assert "sessionStorage.getItem(SCANNED_WORKSPACE_KEY)" in auth
    assert "function readScannedWorkspace()" in auth
    assert "function clearScannedWorkspace()" in auth
    assert "if (readScannedWorkspace()) clearScannedWorkspace();" in auth
    assert "if (!readScannedWorkspace()) saveWorkspace(identity.workspace_id);" in auth


def test_confirm_receipts_surface_adaptive_home_learning() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert "response.learning_message" in html
    assert "result.learning_message" in mobile
    assert "const learnedHome" in mobile


def test_empty_workspace_offers_deliberate_visual_storage_setup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")
    mobile = (STATIC_DIR / "mobile.html").read_text(encoding="utf-8")

    assert "function renderStorageStart()" in html
    assert "function openStorageManager(locationId" in html
    assert 'function addContainer() { openStorageManager("", "top"); }' in html
    assert "focusStorageSetupPrompt" not in html
    assert 'if (!containerRoots.length)' in html
    assert 'data-storage-start' in html
    assert "Open Storage" in html
    assert 'id="assistantStartWithMagpie"' not in html
    assert 'await selectContainer("main-cabinet")' not in html
    assert "function setupInitialPromptFromUrl()" in html
    assert 'params.get("prompt")' in html
    assert 'id="storageMain"' in storage
    assert 'classList.toggle("empty-state", !showArchived && roots.length === 0)' in storage
    assert 'classList.toggle("has-storage", roots.length > 0)' in storage
    assert 'main.empty-state .tree-pane > .pane-head { display:none; }' in storage
    assert 'main.empty-state #addChild { display:none; }' in storage
    assert 'main.empty-state .tree-pane { padding:18px 12px 24px; }' in storage
    assert 'main.empty-state .storage-start { min-height:0; padding:28px 22px; }' in storage
    assert '#storageMain.has-storage .places-overview { gap:9px; padding:11px 12px 12px; }' in storage
    assert '#storageMain.has-storage .places-overview > div:first-child p, #storageMain.has-storage .places-metrics { display:none; }' in storage
    assert '#storageMain.has-storage .places-overview-actions { grid-template-columns:minmax(0,1fr) minmax(0,1fr) 52px; gap:6px; }' in storage
    assert 'aria-label="More place actions" title="More place actions"' in storage
    assert 'byId("organizePlaces").disabled = disabled || visibleRoots().length === 0' in storage
    assert "if (!roots.length && organizingPlaces)" in storage
    assert 'id="emptyAddRoot"' in storage
    assert 'id="addPlace"' in storage
    assert 'id="placesMetrics"' in storage
    assert 'id="locationParent"' in storage
    assert "function renderPlaceMetrics(roots)" in storage
    assert "Create your first storage space" in storage
    assert 'data-empty-setup-template="home"' in storage
    assert 'data-empty-setup-template="workshop"' in storage
    assert 'data-empty-setup-template="studio"' in storage
    assert 'data-empty-setup-template="storage"' in storage
    assert 'id="emptyAddRoot" class="secondary">Start from scratch</button>' in storage
    assert 'button.onclick = () => openStorageSetup({template:button.dataset.emptySetupTemplate})' in storage
    assert 'id="emptyViewArchived"' in storage
    assert "const archivedPlaceCount = allLocations.filter((location) => !location.active).length;" in storage
    assert "setArchiveView(true).catch((error) => setStatus(error.message))" in storage
    assert 'id="planWithMagpie"' not in storage
    assert 'id="storageSetupDialog"' in storage
    assert "function openStorageSetup(options = {})" in storage
    assert "async function createStorageSetup(event)" in storage
    assert 'let onboardingSetupPending = initialParams.get("onboarding") === "1"' in storage
    assert (
        'let destinationSetupPending = initialParams.get("onboarding") === '
        '"destination"'
    ) in storage
    assert "let firstRunSetupConsidered = false" in storage
    assert "const firstRunSetup = (" in storage
    assert "!requestedPrimaryView" in storage
    assert "!initialLocationId" in storage
    assert "!initialCreateMode" in storage
    assert "!initialResumeInteractionId" in storage
    assert 'if (firstRunSetup) setPrimaryView("places")' in storage
    assert "&& (onboardingSetupPending || destinationSetupPending)" in storage
    assert "&& (onboardingSetupPending || firstRunSetup)" not in storage
    assert "firstRunSetupConsidered = true" in storage
    assert 'url.searchParams.delete("onboarding")' in storage
    assert "onboardingSetupPending = false" in storage
    assert "function clearOnboardingIntent(dialogId)" in storage
    assert 'onboardingDialogId = "locationDialog"' in storage
    assert "openDestinationOnboarding(roots);" in storage
    assert 'onboardingDialogId = "storageSetupDialog"' in storage
    assert "openStorageSetup({accountOnboarding:!destinationRequested})" in storage
    assert (
        'byId(dialogId).addEventListener("close", () => '
        "clearOnboardingIntent(dialogId))"
    ) in storage
    assert 'onboardingDialogId === "storageSetupDialog"' in storage
    assert 'onboardingDialogId = "locationDialog"' in storage
    onboarding_branch_start = storage.index(
        "if (\n          !firstRunSetupConsidered"
    )
    onboarding_branch = storage[
        onboarding_branch_start : storage.index(
            "      } catch (error)", onboarding_branch_start
        )
    ]
    assert 'url.searchParams.delete("onboarding")' not in onboarding_branch
    assert 'id="storageSetupProgress" class="setup-progress" hidden' in storage
    assert "<strong>Storage setup</strong>" in storage
    assert "Step 2 of 2" not in storage
    assert (
        'byId("storageSetupProgress").hidden = '
        "!(firstSetup && options?.accountOnboarding)"
    ) in storage
    assert 'window.location.href = mobileAgentUrl(prompt, "", "setup-new")' not in storage
    assert 'byId("addRoot").onclick = openStorageSetup' in storage
    assert (
        'byId("addPlace").onclick = () => '
        "openLocationDialog(overviewCreationParentId())"
    ) in storage
    assert 'byId("detailAddInside").onclick = () => openLocationDialog(location.id)' in storage
    assert 'const initialCreateMode = initialParams.get("create") || ""' in storage
    assert 'if (initialCreateMode === "inside")' in storage
    assert 'if (initialCreateMode === "top")' in storage
    assert "function openDestinationOnboarding(roots)" in storage
    assert (
        'openLocationDialog(parentId, {type:"bin"}, {destinationOnly:true})'
        in storage
    )
    assert "allowTopLevel:!destinationOnboardingMode" in storage
    assert '"Choose where this destination belongs"' in storage
    assert '"Choose a storage place"' in storage
    assert "byId(\"createLocationSubmit\").disabled = !location" in storage
    assert "destinationOnboardingMode && !addParentId" in storage
    assert (
        'destinationOnboardingMode && !byId("locationAcceptsItems").checked'
        in storage
    )
    assert 'let conversationMode = ""' in mobile
    assert 'result.action === "setup_complete"' in mobile
    assert 'if (["setup", "setup-new"].includes(action)) {' in mobile
    assert 'const onboarding = action === "setup-new" ? "1" : "destination"' in mobile
    assert 'window.location.replace(`/storage?view=places&onboarding=${onboarding}`)' in mobile
    assert 'requestAction === "setup-new" ? "setup_new"' not in mobile
    assert 'setConversationMode(action)' in mobile
    assert 'await submit(action)' not in mobile
    assert 'id="emptyAskMagpie"' not in html
    assert 'id="emptyAddInside"' in html
    assert 'command.value = `Add storage inside ${tree.label || id}: `' not in html
    assert 'id="addModal"' not in html


def test_empty_place_hierarchy_distinguishes_places_from_direct_inventory() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'const initialInventoryRootId = initialParams.get("root") || ""' in storage
    assert 'byId("inventoryRoot").value = initialInventoryRootId' in storage
    assert "No places inside ${escapeHtml(rootLabel)}" in storage
    assert (
        '${directItems} item record${directItems === 1 ? " is" : "s are"} '
        "stored directly here."
    ) in storage
    assert 'href="/storage?view=inventory&root=${encodeURIComponent(scopedRoot.id)}"' in storage
    assert 'data-empty-add-inside="${escapeHtml(scopedRoot.id)}"' in storage
    assert "openLocationDialog(button.dataset.emptyAddInside)" in storage
    assert "Nothing inside ${escapeHtml(displayLocationLabel(scopedRoot))}" not in storage


def test_places_uses_inventory_visual_hierarchy_at_desktop_and_mobile_widths() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert (
        "main { display:grid; grid-template-columns:280px minmax(360px,1fr) "
        "minmax(380px,420px);"
    ) in storage
    assert (
        ".places-tabs { grid-column:1/-1; margin:0; "
        "padding:0 var(--page-gutter); }"
    ) in storage
    assert (
        ".places-metrics { grid-column:1/-1; display:grid; "
        "grid-template-columns:repeat(4,minmax(0,1fr));"
    ) in storage
    assert (
        ".places-metrics strong { display:block; margin-bottom:2px; "
        "color:var(--text); font-size:20px;"
    ) in storage
    assert (
        ".root-row { min-height:58px; border:1px solid var(--line); "
        "border-radius:6px; background:var(--surface); }"
    ) in storage
    assert (
        ".root-row.active { border-color:color-mix(in srgb,var(--accent) "
        "42%,var(--line));"
    ) in storage
    assert ".places-tabs { padding:0 12px; }" in storage
    assert (
        ".places-metrics { grid-template-columns:repeat(4,minmax(0,1fr)); "
        "font-size:8px; line-height:1.2; }"
    ) in storage
    assert (
        ".places-metrics span { min-height:58px; padding:9px 6px; "
        "border-right:1px solid var(--line); border-bottom:0; }"
    ) in storage


def test_empty_inventory_routes_to_storage_setup_before_putaway() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="inventoryPrimaryAction"' in storage
    assert 'aria-disabled="true">Loading storage</a>' in storage
    assert "function syncInventoryPrimaryAction()" in storage
    assert 'action.removeAttribute("aria-disabled")' in storage
    assert "location.accepts_items !== false" in storage
    assert 'action.textContent = "Put away an item"' in storage
    assert 'action.textContent = activeLocations.length ? "Add a destination" : "Create storage"' in storage
    assert '"/storage?view=places&onboarding=destination"' in storage
    assert '"/storage?view=places&onboarding=1"' in storage
    assert "if (activeLocations.length) openDestinationOnboarding(roots)" in storage
    assert "else openStorageSetup()" in storage
    assert "syncInventoryPrimaryAction()" in storage
    assert "const storageEmpty = !showArchived" in storage
    assert "Create a storage place before putting away your first item." in storage
    assert 'id="emptyInventorySetup"' not in storage


def test_phone_guided_setup_keeps_common_choices_and_commit_actions_available() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert (
        "@media (max-width:480px) { .setup-template-switch, "
        ".setup-suggestions, .storage-start-options { grid-template-columns:"
        "repeat(2,minmax(0,1fr)); }"
    ) in storage
    assert ".storage-start-option { min-height:72px; }" in storage
    start = storage.index("@media (max-width:480px) {\n      .place-dialog-footer")
    phone_styles = storage[start : storage.index("    @media (max-width:480px) {", start + 1)]
    assert "position:sticky;" in phone_styles
    assert "bottom:0;" in phone_styles
    assert "margin:16px -18px -18px;" in phone_styles
    assert (
        ".place-dialog-footer .actions { display:grid; "
        "grid-template-columns:minmax(0,1fr) minmax(0,1fr); }"
    ) in phone_styles
    assert ".place-dialog-footer .actions button { width:100%; min-width:0; }" in phone_styles


def test_touch_surfaces_keep_primary_controls_at_least_44px() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    account = ACCOUNT_HTML.read_text(encoding="utf-8")
    station = CHECK_STATION_HTML.read_text(encoding="utf-8")

    assert "@media (pointer:coarse)" in storage
    assert ".path button," in storage
    assert ".item-more > summary," in storage
    assert ".item-more-actions button," in storage
    assert ".item-more-actions .button { min-height:44px; }" in storage
    assert ".inventory-filter { min-width:44px; min-height:44px;" in storage
    assert ".global-search button { width:44px; height:44px;" in storage
    assert ".path button { min-height:44px;" in storage
    assert ".item-more > summary { min-height:44px;" in storage
    assert ".item-more-actions button, .item-more-actions .button { width:100%; min-height:44px;" in storage
    assert ".create-location-head button { width:44px; min-height:44px;" in storage
    assert ".history-head button { width:44px; height:44px;" in storage
    assert ".quick-actions button, .quick-actions a { min-width:0; min-height:44px;" in mobile
    assert ".location-context { display:flex; align-items:center; gap:8px; min-height:44px;" in mobile
    assert ".location-context button { width:44px; min-width:44px; min-height:44px;" in mobile
    assert ".tool-button { min-width:44px; min-height:44px;" in mobile
    assert ".send-button { min-width:74px; min-height:44px;" in mobile
    assert ".photo button { position:absolute; top:-11px; right:-11px; width:44px; height:44px;" in mobile
    assert ".scanner-head button { width:44px; height:44px;" in mobile
    assert "button, .button { min-height:44px;" in account
    assert ".device-actions button { min-height:44px; }" in account
    assert ".photo button { position:absolute; top:8px; right:8px; width:44px; height:44px;" in station


def test_storage_hierarchy_workbench_scopes_tree_to_selected_root_and_persists_context() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'const STORAGE_CONTEXT_KEY_PREFIX = "magpie.storage.context.v2"' in storage
    assert 'const LEGACY_STORAGE_CONTEXT_KEY = "magpie.storage.context.v1"' in storage
    assert "function loadStorageContext(identity)" in storage
    assert "identity?.workspace_id" in storage
    assert 'encodeURIComponent(workspaceId || "local")' in storage
    assert "if (raw === null && !workspaceId)" in storage
    assert "localStorage.removeItem(LEGACY_STORAGE_CONTEXT_KEY)" in storage
    assert "storageContextLoaded = true" in storage
    assert "function persistStorageContext()" in storage
    assert "if (!storageContextLoaded || !storageContextKey) return" in storage
    assert "selected_id:selectedId || \"\"" in storage
    assert "expanded_ids:[...expandedIds].slice(0, 300)" in storage
    assert "const availableIds = new Set(allLocations.map((location) => location.id))" in storage
    assert "if (!availableIds.has(id)) expandedIds.delete(id)" in storage
    assert "function visibleRoots()" in storage
    assert "const scopedRoot = roots.find((root) => root.id === currentRootId)" in storage
    assert "const scopedIds = scopedRoot ? subtree(scopedRoot.id) : []" in storage
    assert "const scopedChildren = scopedRoot ? childrenOf(scopedRoot.id) : []" in storage
    assert "visibleScopedChildren.map((child) => treeRow(child, 0)).join(\"\")" in storage
    assert "Add a cabinet, shelf, drawer, bin, box, or another place" in storage
    assert "function selectRootForBrowsing(rootId, roots)" in storage
    assert "persistStorageContext();\n      syncSelectedLocationUrl(rootId);" in storage
    assert "await loadLocations(response.id);\n      syncSelectedLocationUrl(response.id);" in storage
    assert "await loadLocations(focus.id);\n        syncSelectedLocationUrl(focus.id);" in storage
    assert "const mobile = isMobileStorageView();" in storage
    assert 'const agentPath = isMobileStorageView() ? "/mobile" : "/";' in storage
    assert 'action.href = mobileAgentUrl("", "", "put-away")' in storage
    assert "if (mobile) {\n        const url = new URL(window.location.href);" in storage
    assert "await loadLocations(selectedId);\n          syncSelectedLocationUrl(selectedId);" in storage
    assert 'data-add-inside="${escapeHtml(location.id)}"' in storage
    assert 'aria-label="Add inside ${escapeHtml(displayLabel)}"' in storage
    assert 'const role = location.accepts_items === false ? "Contains places" : "Stores items"' in storage
    assert "nestedIds.reduce(" in storage
    assert 'byId("addChild").textContent = creationParent' in storage
    assert "renderRoots(visibleRoots())" in storage
    assert "loadStorageContext(identity)" in storage
    assert "await loadLocations(initialLocationId || selectedId)" in storage
    assert ".tree-line { position:relative; display:grid; grid-template-columns:44px minmax(0,1fr) auto;" in storage
    assert ".tree-inline-action { width:64px; min-height:44px;" in storage
    assert 'id="toggleHierarchy" class="secondary" type="button" disabled>Expand</button>' in storage
    assert 'byId("toggleHierarchy").dataset.action = allExpanded ? "collapse" : "expand"' in storage
    assert 'const expandableIds = subtree(scopedRoot.id).filter((id) => childrenOf(id).length > 0)' in storage
    assert 'style="--tree-depth:${depth};padding-left:${depth * 20}px"' in storage
    assert "const TREE_CHILD_PAGE_SIZE = 32" in storage
    assert "const TREE_CHILD_MOBILE_PAGE_SIZE = 12" in storage
    assert "function treeChildPageSize()" in storage
    assert "const visibleTreeChildWindows = new Map()" in storage
    assert 'data-tree-page="previous"' in storage
    assert 'data-tree-page="next"' in storage
    assert "count:current.count + pageSize" in storage
    assert "visibleTreeChildWindows.delete(rootId)" in storage


def test_archived_places_preserve_their_canonical_path_context() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "function visibleRootIdFor(id)" in storage
    assert "const parent = current.parent_id ? locationById(current.parent_id) : null" in storage
    assert "currentRootId = visibleRootIdFor(locationId)" in storage
    assert "const preferredRootId = visibleRootIdFor(preferredId)" in storage
    assert "function rootDisplayLabel(location)" in storage
    assert "showArchived ? (pathLabel(location.id)" in storage
    assert "naturalCompare(rootDisplayLabel(left), rootDisplayLabel(right))" in storage
    assert "const label = rootDisplayLabel(root)" in storage
    assert "escapeHtml(label)" in storage
    assert 'archived group${roots.length === 1 ? "" : "s"}' in storage
    assert '? rootDisplayLabel(scopedRoot)' in storage
    assert 'id="rootPaneTitle">Storage spaces</h2>' in storage
    assert 'byId("rootPaneTitle").textContent = showArchived ? "Archived groups" : "Storage spaces"' in storage
    assert "No other archived places in this group" in storage
    assert "Open the archived group to review its preserved inventory and restore it." in storage
    archived_detail = storage[
        storage.index("if (!location.active) {") :
        storage.index("const data = await api(`", storage.index("if (!location.active) {"))
    ]
    assert "const path = pathLabel(location.id)" in archived_detail
    assert "MagpieLocationDisplay.pathText" not in archived_detail


def test_workspace_duplicate_repair_link_opens_visual_conflict_review() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'initialParams.get("conflicts") === "1"' in storage
    assert 'url.searchParams.delete("conflicts")' in storage
    assert "openPlaceConflictDialog(0).catch((error) => setStatus(error.message));" in storage


def test_storage_hierarchy_organize_mode_uses_reviewed_move_flow() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="organizePlaces"' in storage
    assert 'aria-pressed="false" disabled>Organize</button>' in storage
    assert "let organizingPlaces = false" in storage
    assert "organizingPlaces = !organizingPlaces" in storage
    assert 'organizingPlaces ? "Done" : "Organize"' in storage
    assert 'organizingPlaces ? "Choose a place to edit or move"' in storage
    assert 'data-move-place="${escapeHtml(location.id)}"' in storage
    assert 'data-edit-place="${escapeHtml(location.id)}"' in storage
    assert 'aria-label="Edit ${escapeHtml(displayLabel)}"' in storage
    assert 'aria-label="Move ${escapeHtml(displayLabel)}"' in storage
    assert 'data-move-search-result="${escapeHtml(location.location_id)}"' in storage
    assert "openMoveLocationDialog(button.dataset.movePlace)" in storage
    assert "beginLocationEdit(button.dataset.editPlace).catch(" in storage
    assert "openMoveLocationDialog(button.dataset.moveSearchResult)" in storage
    assert 'id="moveLocationCurrentPath" class="item-path"' in storage
    assert "`Currently at ${pathLabel(source.id) || source.id}`" in storage
    assert "const sourcePath = (preview.source?.path || [])" in storage
    assert "const nextPath = [destinationPath" in storage
    assert "`${sourcePath} → ${nextPath}`" in storage
    assert "nodes:hierarchyPickerLocations(new Set(subtree(source.id)))" in storage
    assert "onSelect:reviewLocationMove" in storage
    assert "offerUndo(`Moved ${source.label} ${moveDescription}`)" in storage


def test_storage_manager_offers_a_workspace_wide_place_catalog() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="browseAllPlaces" class="place-catalog-launch secondary"' in storage
    assert 'aria-pressed="false">All places <span>Browse</span>' in storage
    assert "let placeCatalogMode = false" in storage
    assert "let placeCatalogScope = \"all\"" in storage
    assert "function renderPlaceCatalog()" in storage
    assert 'aria-label="Filter all places by use"' in storage
    assert '["destinations", "Destinations"]' in storage
    assert '["used", "Used"]' in storage
    assert '["empty", "Empty"]' in storage
    assert 'data-open-catalog-place="${escapeHtml(location.id)}"' in storage
    assert 'data-move-catalog-place="${escapeHtml(location.id)}"' in storage
    assert "openLocationDetail(button.dataset.openCatalogPlace)" in storage
    assert "openMoveLocationDialog(button.dataset.moveCatalogPlace)" in storage
    assert "placeCatalogLimit += PLACE_CATALOG_PAGE_SIZE" in storage
    assert ".place-catalog-actions button { min-height:44px;" in storage
    assert "placeCatalogMode = false" in storage[storage.index("function selectRootForBrowsing(") :]


def test_guided_setup_shows_container_identity_before_parent_picker() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    destination_fields = storage[
        storage.index('id="storageSetupDestinationFields"') :
        storage.index("</div>", storage.index('id="storageSetupDestinationParent"')) + 6
    ]
    assert destination_fields.index('id="storageSetupDestinationName"') < (
        destination_fields.index('id="storageSetupDestinationParent"')
    )
    assert destination_fields.index('id="storageSetupDestinationType"') < (
        destination_fields.index('id="storageSetupDestinationParent"')
    )


def test_guided_setup_collapses_optional_container_configuration() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert (
        'id="storageSetupDestinationOptions" class="setup-destination-options"'
        in storage
    )
    assert (
        '<summary>Container options '
        '<span id="storageSetupDestinationOptionsSummary">Bin · Medium</span>'
        in storage
    )
    assert 'class="setup-destination-options-grid"' in storage
    assert ".setup-destination-options > summary { min-height:44px;" in storage
    assert "function renderStorageSetupDestinationOptionsSummary()" in storage
    assert (
        'byId("storageSetupDestinationOptionsSummary").textContent = '
        "[type, capacity, parent]"
        in storage
    )
    assert 'byId("storageSetupDestinationOptions").open = false;' in storage


def test_storage_place_creation_is_destination_first_and_continuable() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="placesOverviewActions" class="places-overview-actions" hidden' in storage
    assert 'byId("placesOverviewActions").hidden = showArchived || locations.length === 0' in storage
    assert 'id="locationDialog" class="create-location-dialog"' in storage
    assert 'id="locationParentLabel" class="create-parent-label">Inside</span>' in storage
    assert 'id="locationParent" type="hidden"' in storage
    assert 'id="locationParentCurrent" class="create-parent-current"' in storage
    assert 'aria-controls="locationParentPicker"' in storage
    assert 'id="locationParentSearch" type="search"' in storage
    assert 'aria-label="Search parent places"' in storage
    assert 'id="locationParentResults" class="create-parent-results destination-list"' in storage
    assert 'id="locationParentSummary" class="create-parent-summary" role="status"' in storage
    assert "function syncPlaceCreationControls()" in storage
    assert "const disabled = !storageLoaded || showArchived" in storage
    assert 'byId("addPlace").disabled = disabled' in storage
    assert 'setStatus(storageLoaded ? "Switch to Active places to add a place" : "Wait for storage to finish loading")' in storage
    assert "function hierarchyPickerLocations(excludedIds = new Set())" in storage
    assert "function renderHierarchyPicker({" in storage
    assert "function renderCreateParentSelection()" in storage
    assert "function setCreateParentPicker(open)" in storage
    assert "function chooseCreateParent(parentId)" in storage
    assert "function renderCreateParentResults()" in storage
    assert ".filter((location) => location.active && !excludedIds.has(location.id))" in storage
    assert "nodes.filter((node) => String(node.parent_id || \"\") === currentBrowseId)" in storage
    assert 'data-hierarchy-up="${escapeHtml(upId)}"' in storage
    assert 'data-hierarchy-browse="${escapeHtml(node.id)}"' in storage
    assert 'data-hierarchy-select="${escapeHtml(node.id)}"' in storage
    assert "const browseOnly = !selectable && childCount > 0" in storage
    assert "childCount && selectable" in storage
    assert "matches.slice(0, includeTopLevel ? 39 : 40)" not in storage
    assert "No matching places." in storage
    assert "across all storage" in storage
    assert "onSelect:chooseCreateParent" in storage
    assert 'byId("locationParentSearch").oninput = renderCreateParentResults' in storage
    assert 'if (event.key === "Escape")' in storage
    assert 'if (event.key === "Enter") first.click()' in storage
    assert 'byId("locationType").value = suggestedTypesForParent(parentId)[0]' in storage
    assert 'addParentId = byId("locationParent").value || null' in storage
    assert '<details class="create-location-details">' in storage
    assert 'id="createLocationStatus" class="create-location-status" role="status"' in storage
    assert (
        ".storage-setup-dialog .dialog-head, .create-location-dialog .dialog-head "
        "{ position:sticky; top:0;"
    ) in storage
    assert ".place-dialog-footer { position:sticky; bottom:-18px;" in storage
    assert storage.count('class="place-dialog-footer"') == 2
    assert storage.index('id="storageSetupStatus"') < storage.index(
        'id="storageSetupSubmit"'
    )
    assert storage.index('id="createLocationStatus"') < storage.index(
        'id="createLocationSubmit"'
    )
    assert 'id="addStorageSpace" type="button">Set up storage</button>' in storage
    assert 'id="addPlace" class="secondary" type="button">Add inside</button>' in storage
    assert 'byId("addStorageSpace").disabled = disabled' in storage
    assert 'const parent = locationById(overviewCreationParentId())' in storage
    assert '"Add storage space"' in storage
    assert '"Set up storage"' in storage
    assert 'byId("addPlace").textContent = parent ? "Add inside" : "Quick add place"' in storage
    assert "`Add a place inside ${parentName}`" in storage
    open_detail = storage[
        storage.index("async function openLocationDetail(locationId)") :
        storage.index("async function loadLocations", storage.index("async function openLocationDetail(locationId)"))
    ]
    assert "currentRootId = visibleRootIdFor(locationId);" in open_detail
    assert "syncPlaceCreationControls();" in open_detail
    load_locations = storage[
        storage.index("async function loadLocations") :
        storage.index("function visibleRoots", storage.index("async function loadLocations"))
    ]
    assert "selectedId = currentRootId;" in load_locations
    assert load_locations.index("selectedId = currentRootId;") < load_locations.index(
        "syncPlaceCreationControls();",
        load_locations.index("selectedId = currentRootId;"),
    )
    assert 'id="createLocationSubmit" type="submit">Create place</button>' in storage
    assert 'id="storageSetupCommitSummary" class="place-commit-summary"' in storage
    assert 'id="createLocationCommitSummary" class="place-commit-summary"' in storage
    assert "function renderCreateLocationCommitSummary()" in storage
    assert "Put-away ready at" in storage
    assert "No put-away destination yet" in storage
    assert "Ready for put-away" in storage
    assert "Organizes places only" in storage
    assert 'byId("dialogTitle").textContent = location ? `Add inside ${location.label}` : "Add place"' in storage
    assert 'byId("createLocationSubmit").textContent = "Create place"' in storage
    assert "background:var(--surface-inset)" in storage
    assert "dialog::backdrop { background:var(--overlay); }" in storage
    assert "background:var(--danger-soft); color:var(--danger)" in storage
    for legacy_color in ("#d8b56b", "#e5cca0", "#dfc791", "#6b4709", "#7f1d1d", "#744d08"):
        assert legacy_color not in storage
    assert 'id="creationReceiptDialog" class="creation-receipt"' in storage
    assert (
        'collectionTitle = "Structure ready"'
        in storage
    )
    assert 'id="creationNext" class="button primary-action"' in storage
    assert 'id="creationBuildStructure"' in storage
    assert "Build cabinet or shelving" in storage
    assert 'id="creationDownloadLabels"' in storage
    assert 'id="creationOpenPlace"' in storage
    assert 'id="creationAddInside"' in storage
    assert 'id="creationAddPeer"' in storage
    assert 'id="creationLabel"' not in storage
    assert 'id="creationMore" class="creation-more"' in storage
    assert "<summary>More options</summary>" in storage
    assert 'byId("creationOpenPlace").hidden = selectedId === receiptLocation.id' in storage
    assert (
        'byId("creationOpenPlace").textContent = collectionRoot '
        '? "Open storage space" : "Open place"'
        in storage
    )
    assert "const receiptLocation = collectionRoot || location" in storage
    assert 'byId("creationReceiptPath").hidden = receiptPath === receiptName' in storage
    assert "Put-away ready at" in storage
    assert "lastCreatedReceiptLocationId || lastCreatedLocationId" in storage
    assert "const readyForPutAway = location.accepts_items || createdDestinations.length > 0" in storage
    assert 'nextAction.dataset.action = readyForPutAway ? "put-away" : "add-inside"' in storage
    assert "Put away something in this structure" in storage
    assert 'byId("creationAddInside").hidden = !location.accepts_items' in storage
    assert 'byId("creationBuildStructure").hidden = !collectionRootId' in storage
    assert "lastCreatedStructureParentId = collectionRootId" in storage
    assert 'byId("creationMore").open = false' in storage
    assert "nextAction.focus()" in storage
    assert 'event.currentTarget.dataset.action !== "add-inside"' in storage
    assert (
        'new URLSearchParams({location_ids:labelIds.join(",")})'
        in storage
    )
    assert 'openLocationDialog(locationId)' in storage
    assert 'openLocationDialog(parentId)' in storage
    assert 'byId("creationBuildStructure").onclick' in storage
    assert "openStructureDialog(parentId)" in storage


def test_storage_quick_create_blocks_duplicate_sibling_names_before_submit() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "function createLocationNameConflict()" in storage
    assert "function syncCreateLocationValidation()" in storage
    assert '(location.parent_id || null) === parentId' in storage
    assert '.replace(/\\s+/g, " ")' in storage
    assert "already exists ${escapeHtml(context)}. Open it or use another name." in storage
    assert 'id="openCreateLocationConflict" type="button">Open existing</button>' in storage
    assert "openLocationDetail(conflict.id)" in storage
    assert 'byId("locationLabel").oninput = () => {' in storage
    assert "renderCreateLocationCommitSummary();" in storage
    assert "syncCreateLocationValidation();" in storage
    assert "if (!syncCreateLocationValidation()) return;" in storage
    assert (
        'label:byId("locationLabel").value.trim().replace(/\\s+/g, " ")'
        in storage
    )
    assert ".create-location-status.conflict {" in storage


def test_nested_place_creation_prefills_a_unique_replaceable_name() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "function suggestedLocationLabel(type, parentId)" in storage
    assert 'if (!parentId) return "";' in storage
    assert ".filter((location) => (location.parent_id || null) === parentId)" in storage
    assert "while (siblingNames.has(candidate.toLocaleLowerCase()))" in storage
    assert "function syncSuggestedLocationLabel()" in storage
    assert 'const previousSuggestion = input.dataset.suggestedLabel || ""' in storage
    assert "const replaceable = !input.value.trim() || input.value === previousSuggestion" in storage
    assert "if (replaceable) input.value = nextSuggestion" in storage
    assert "syncSuggestedLocationLabel();" in storage
    assert 'byId("locationLabel").select();' in storage


def test_storage_place_editor_labels_every_editable_field() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    for field_id in (
        "editLabel",
        "editType",
        "editCapacity",
        "editAcceptsItems",
        "editWidth",
        "editDepth",
        "editHeight",
        "editDimensionUnit",
        "editTags",
        "editNotes",
    ):
        assert f'for="{field_id}"' in storage
        assert f'id="{field_id}"' in storage


def test_storage_surfaces_roots_that_cannot_accept_putaway() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="placeReadiness" class="place-readiness"' in storage
    assert 'id="placeReadinessTitle"' in storage
    assert 'id="placeReadinessList"' in storage
    assert "<strong>-</strong> destinations" in storage
    assert "function rootsWithoutDestinations(roots)" in storage
    assert (
        "!subtree(root.id).some((id) => "
        "locationById(id)?.accepts_items === true)"
    ) in storage
    assert "function renderPlaceReadiness(roots)" in storage
    assert 'data-add-destination="${escapeHtml(root.id)}"' in storage
    assert "{destinationOnly:true}" in storage
    assert "renderPlaceReadiness(roots)" in storage
    assert ".place-readiness { grid-template-columns:minmax(0,1fr) auto;" in storage
    assert ".place-readiness-copy span { display:none; }" in storage
    assert ".place-readiness-list button { width:auto; min-height:44px; }" in storage
    assert ".place-readiness:has(.place-readiness-list button:nth-child(2)) { grid-template-columns:1fr; }" in storage
    assert ".place-readiness:has(.place-readiness-list button:nth-child(2)) .place-readiness-list { justify-content:flex-start; }" in storage
    assert 'aria-label="Add a put-away destination in ${escapeHtml(label)}"' in storage
    assert "Add in ${escapeHtml(label)}" in storage


def test_storage_surfaces_actionable_physical_inventory_confidence() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="placeConfidence" class="place-confidence"' in storage
    assert 'id="placeConfidenceSummary"' in storage
    assert "function renderPlaceConfidence()" in storage
    assert 'location.confidence?.state === "unchecked"' in storage
    assert 'location.confidence?.state === "stale"' in storage
    assert 'location.confidence?.state === "changed"' in storage
    assert 'location.confidence?.state === "unknown"' in storage
    assert "coveredByAncestorCheck" not in storage
    assert "Review uncertain items" in storage
    assert 'data-check-location="${escapeHtml(location.id)}"' in storage
    assert "changed since checking" in storage
    assert "await openContentsAudit(button.dataset.checkLocation)" in storage
    assert 'setPrimaryView("inventory")' in storage
    assert 'await setInventoryScope("review")' in storage
    assert "renderPlaceConfidence();" in storage
    assert 'id="locationConfidenceStatus"' in storage


def test_places_desktop_workspace_keeps_large_hierarchies_in_scrollable_panes() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "@media (min-width:1081px)" in storage
    assert "height:calc(100dvh - 62px);" in storage
    assert "grid-template-rows:auto auto auto minmax(0,1fr);" in storage
    assert "#storageMain > .places-tabs { grid-row:1; position:static; }" in storage
    assert "#storageMain > .putaway-handoff { grid-row:3; }" in storage
    assert "#storageMain > .roots," in storage
    assert "#storageMain > .tree-pane," in storage
    assert "#storageMain > .detail {" in storage
    assert "overscroll-behavior:contain;" in storage


def test_storage_place_creation_offers_contextual_types_and_direct_batch_setup() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="locationTypeChoices" class="create-type-choices"' in storage
    assert 'aria-label="Suggested place types"' in storage
    assert "const TYPE_DESCRIPTIONS = {" in storage
    assert "const TYPE_PLACEHOLDERS = {" in storage
    assert "function suggestedTypesForParent(parentId)" in storage
    assert '["room","area","closet","cabinet","storage_unit"]' in storage
    assert '["closet","cabinet","shelf","rack","tote"]' in storage
    assert '["drawer","shelf","bin","box","organizer"]' in storage
    assert '["cell","tray","bin","pouch","drawer"]' in storage
    assert "function renderCreateTypeChoices()" in storage
    assert 'byId("locationLabel").placeholder = TYPE_PLACEHOLDERS[current]' in storage
    assert "const TYPE_NAME_PATTERNS = [" in storage
    assert '["cabinet", /\\bcabinets?\\b/i]' in storage
    assert '["shelf", /\\b(?:shelf|shelves)\\b/i]' in storage
    assert '["drawer", /\\bdrawers?\\b/i]' in storage
    assert '["tray", /\\btrays?\\b/i]' in storage
    assert '["bag", /\\bbags?\\b/i]' in storage
    assert '["organizer", /\\borganizers?\\b/i]' in storage
    assert '["bin", /\\bbins?\\b/i]' in storage
    assert "function inferredCreateType(label)" in storage
    assert "function syncCreateTypeFromLabel()" in storage
    assert "if (locationTypeTouched) return;" in storage
    assert "|| suggestedTypesForParent(addParentId)[0]" in storage
    assert 'byId("locationLabel").oninput = () => {' in storage
    assert "syncCreateTypeFromLabel();" in storage
    assert 'data-create-type="${type}"' in storage
    assert 'aria-pressed="${type === current}"' in storage
    assert 'byId("locationType").onchange = () => {' in storage
    assert 'id="createRoleToggle" class="create-role-toggle"' in storage
    assert 'for="locationAcceptsItems"' in storage
    assert "Put-away eligibility" in storage
    assert "You can still add smaller places inside it." in storage
    assert 'id="locationAcceptsItems" type="checkbox"' in storage
    assert "function renderCreateRoleChoices()" in storage
    assert "function changeCreateRole()" in storage
    assert 'byId("locationAcceptsItems").disabled = destinationOnboardingMode' in storage
    assert 'if (destinationOnboardingMode) byId("locationAcceptsItems").checked = true' in storage
    assert 'byId("locationAcceptsItems").onchange = changeCreateRole' in storage
    assert storage.index('id="locationTypeChoices"') < storage.index(
        'id="createRoleToggle"'
    ) < storage.index('id="locationParentCurrent"')
    assert ".create-role-summary:empty { display:none; }" in storage
    assert "will organize smaller places only. Add a destination inside it before put-away." in storage
    assert 'id="detailAddMany" class="secondary" type="button">Add several places</button>' in storage
    assert 'byId("detailAddMany").onclick = () => openBatchDialog(location.id)' in storage
    assert 'function openBatchDialog(parentId, mode = "names")' in storage
    assert 'id="batchParentContext" class="batch-context"' in storage
    assert 'data-batch-mode="names"' in storage
    assert 'data-batch-mode="numbered"' in storage
    assert 'id="batchLabels"' in storage
    assert "function batchLocationNames()" in storage
    assert "function setBatchMode(mode)" in storage
    assert 'payload.labels = batchLocationNames()' in storage
    assert 'payload.label_prefix = byId("batchPrefix").value' in storage
    assert 'id="creationAddSeveralInside"' in storage
    assert 'openBatchDialog(locationId, "names")' in storage
    assert 'id="batchPreview" class="batch-preview" role="status"' in storage
    assert "function renderBatchPreview()" in storage
    assert 'byId("batchCount").oninput = renderBatchPreview' in storage
    assert 'byId("batchPrefix").oninput = renderBatchPreview' in storage


def test_storage_deep_link_selects_a_scanned_location() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    assert 'const initialParams = new URLSearchParams(location.search)' in storage
    assert 'initialParams.get("location") || ""' in storage
    assert "loadLocations(initialLocationId || selectedId)" in storage
    assert "function rootIdFor(id)" in storage
    assert "const preferredRootId = visibleRootIdFor(preferredId)" in storage
    assert "currentRootId = preferredRootId" in storage
    assert 'if (initialLocationId && locationById(initialLocationId)) {' in storage
    assert "showMobileDetail();" in storage


def test_scanned_item_find_runs_immediately_but_only_for_read_only_find() -> None:
    api_source = API_PY.read_text(encoding="utf-8")
    assistant = INDEX_HTML.read_text(encoding="utf-8")
    mobile = (STATIC_DIR / "mobile.html").read_text(encoding="utf-8")

    assert '"&action=find&auto=find"' in api_source
    assert 'params.get("action") === "find" && params.get("auto") === "find"' in assistant
    assert 'params.delete("auto")' in assistant
    assert "if (autoFind) requestAnimationFrame(() => runCommand());" in assistant
    assert 'const autoFind = action === "find" && params.get("auto") === "find";' in mobile
    assert 'params.delete("auto")' in mobile
    assert 'if (autoFind) await submit("find");' in mobile


def test_storage_manager_uses_mobile_master_detail_navigation() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    assert 'id="mobileBackToStorage"' in storage
    assert '#storageMain:not(.mobile-detail-open) .detail { display:none; }' in storage
    assert '#storageMain.mobile-detail-open .roots, #storageMain.mobile-detail-open .tree-pane { display:none; }' in storage
    assert 'function showMobileBrowser({resetScroll = false} = {})' in storage
    assert 'function showMobileDetail()' in storage
    assert 'byId("mobileBackToStorage").onclick = showMobileBrowser' in storage
    assert "async function openLocationDetail(locationId)" in storage
    assert "renderTree();" in storage
    assert "showMobileDetail();" in storage
    assert "await renderDetail();" in storage
    assert "function selectRootForBrowsing(rootId, roots)" in storage
    assert "if (isMobileStorageView()) scrollStorageHierarchyIntoView();" in storage
    assert 'data-root-detail="${escapeHtml(root.id)}"' in storage
    assert 'aria-label="Browse ${escapeHtml(label)}"' in storage
    assert "openLocationDetail(button.dataset.rootDetail).catch(" in storage
    assert ".root-open-detail { display:none;" in storage
    assert ".root-entry { display:grid; grid-template-columns:minmax(0,1fr) 44px; gap:4px; }" in storage
    assert ".root-open-detail { display:block; }" in storage
    assert "if (isMobileStorageView()) {" in storage
    assert "openLocationDetail(button.dataset.root).catch" in storage
    assert "function renderLocationDetailError(error, locationId)" in storage
    assert 'id="retryLocationDetail"' in storage
    assert "await openLocationDetail(locationId)" in storage
    assert 'await loadLocations(locationId);\n      await openLocationDetail(locationId);' in storage
    assert "const firstLocationLoad = !storageLoaded;" in storage
    assert "firstLocationLoad && isMobileStorageView() && !initialLocationId" in storage
    assert "expandedIds.clear();" in storage
    assert "!firstLocationLoad || !isMobileStorageView() || Boolean(initialLocationId)" in storage
    assert (
        "!firstLocationLoad\n"
        "          || !isMobileStorageView()\n"
        "          || Boolean(initialLocationId)\n"
        "        ) {\n"
        "          scrollSelectedTreeRowIntoView();"
    ) in storage


def test_storage_manager_exposes_location_contents_and_item_actions() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    assert 'class="place-utility-actions" aria-label="Place actions"' in storage
    assert 'id="detailEditPlace" class="secondary" type="button">Edit</button>' in storage
    assert 'id="detailMovePlace" class="secondary" type="button">Move</button>' in storage
    assert 'id="detailArchivePlace" class="danger" type="button">Archive place</button>' in storage
    assert "async function beginLocationEdit(locationId)" in storage
    assert 'byId("detail").querySelector(".place-details")' in storage
    assert 'byId("editLabel").select()' in storage
    assert 'byId("detailEditPlace").onclick = () => beginLocationEdit(location.id)' in storage
    assert 'byId("detailMovePlace").onclick = () => openMoveLocationDialog(location.id)' in storage
    assert 'byId("detailArchivePlace").onclick = archiveSelected' in storage
    assert 'id="locationContents"' in storage
    assert 'id="contentsFilter"' in storage
    assert "currentDetailData.contents" in storage
    assert "include_children=false&include_contents=false" in storage
    assert 'id="loadMoreContents"' in storage
    assert 'id="locationVerificationStatus"' in storage
    assert 'id="verifyLocationContents"' in storage
    assert "function locationVerificationText(verification)" in storage
    assert 'id="contentsAuditDialog"' in storage
    assert "async function openContentsAudit(locationId = selectedId)" in storage
    assert "async function finishContentsAudit()" in storage
    assert 'api(`/locations/${encodeURIComponent(locationId)}/audit`)' in storage
    assert 'api(`/locations/${encodeURIComponent(locationId)}/verify`' in storage
    assert 'status:contentsAuditStatuses.get(contentsAuditKey(placement))' in storage
    assert "All shown are here" in storage
    assert "Add something not listed" in storage
    assert "async function loadContents(reset = false)" in storage
    assert 'new URLSearchParams({limit:"30", offset:String(existing.length)})' in storage
    assert 'api(`/locations/${encodeURIComponent(locationId)}/contents?${params}`)' in storage
    assert "contents.length >= contentsTotal" in storage
    assert "function pathLabel(id)" in storage
    assert '<script src="/location-display.js"></script>' in storage
    assert "function displayLocationLabel(location)" in storage
    assert "MagpieLocationDisplay.catalogLabel(location, allLocations)" in storage
    assert 'MagpieLocationDisplay.pathText(pathFor(id), allLocations, " › ")' in storage
    assert 'data-find-item="${escapeHtml(item.name)}"' in storage
    assert 'api("/assistant/find"' in storage
    assert 'id="moveItemDialog"' in storage
    assert 'id="moveItemSearch"' in storage
    assert 'id="moveItemDestinations"' in storage
    assert 'id="moveItemSummary"' in storage
    assert 'id="moveItemDestination"' not in storage
    assert "function renderMoveItemDestinations()" in storage
    assert "function initialItemBrowseId(sourceLocations)" in storage
    assert "const activeRootCount = allLocations.filter" in storage
    assert "roots.size === 1 && activeRootCount <= 1" in storage
    assert "canSelect:(location) => location.accepts_items && !(sources.size === 1 && sources.has(location.id))" in storage
    assert "nodes:hierarchyPickerLocations()" in storage
    assert "onSelect:reviewMoveDestination" in storage
    assert "const visible = matches.slice(0, 60)" not in storage
    assert 'byId("moveItemSearch").oninput = renderMoveItemDestinations' in storage
    assert "async function reviewMoveDestination(destination)" in storage
    assert 'api("/inventory/move-preview"' in storage
    assert "function movingItemEntries()" in storage
    assert "This creates one undoable inventory action." in storage
    assert "async function moveItemTo(destination)" in storage
    assert "if (!movingItem || movingItemRequest" in storage
    assert "movingItemRequest = false;" in storage
    assert 'byId("confirmBulkMove").disabled = false;' in storage
    assert 'const path = bulk ? "/inventory/move-bulk" : "/inventory/move";' in storage
    assert "activeFinds = Array.isArray(result.active_finds) ? result.active_finds : activeFinds;" in storage
    assert "renderActiveFinds();" in storage
    assert 'id="editItemDialog"' in storage
    assert 'data-edit-item="${escapeHtml(item.item_id)}"' in storage
    item_actions = storage.index('<div class="item-actions"><button class="secondary" data-find-item=')
    item_edit = storage.index('data-edit-item="${escapeHtml(item.item_id)}"', item_actions)
    item_more = storage.index('<details class="item-more"><summary>More</summary>', item_actions)
    assert item_edit < item_more
    assert "function openEditItem(itemId, sourceLocation)" in storage
    assert '<details class="item-more"><summary>More</summary>' in storage
    assert 'class="item-more-actions"' in storage
    for action in ("data-photo-item", "data-history-item", "data-remove-item"):
        assert action in storage


def test_storage_inventory_supports_reviewed_bulk_moves_without_ai() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="inventorySelection"' in storage
    assert 'id="moveInventorySelection"' in storage
    assert 'data-inventory-select="${escapeHtml(group.item_id)}"' in storage
    assert "const selectedInventoryPlacements = new Set()" in storage
    assert "function selectedInventoryRows()" in storage
    assert "function openBulkMove()" in storage
    assert 'id="bulkMoveConfirmation"' in storage
    assert 'id="confirmBulkMove"' in storage
    assert 'id="changeBulkMoveDestination"' in storage
    assert "async function reviewMoveDestination(destination)" in storage
    assert 'body:JSON.stringify({entries:movingItemEntries(), to_bin:destination})' in storage
    assert 'movingItem.bulk ? "Move inventory" : "Move item"' in storage
    assert 'const path = bulk ? "/inventory/move-bulk" : "/inventory/move";' in storage
    assert "This creates one undoable inventory action." in storage


def test_storage_inventory_downloads_one_qr_label_per_selected_item() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="labelInventorySelection"' in storage
    assert "async function downloadInventoryLabels()" in storage
    assert "new Set(selectedInventoryRows().map((item) => Number(item.item_id)))" in storage
    assert 'new URLSearchParams({item_ids:itemIds.join(",")})' in storage
    assert 'fetch(`/items/labels.pdf?${params}`)' in storage
    assert 'link.download = "magpie-item-labels.pdf"' in storage
    assert (
        'byId("labelInventorySelection").onclick = () => downloadInventoryLabels()'
        in storage
    )


def test_storage_inventory_can_select_the_current_filtered_results() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="selectVisibleInventoryLabel"' in storage
    assert 'id="selectVisibleInventory" type="checkbox"' in storage
    assert "function syncVisibleInventorySelection(groups = renderedInventoryGroups)" in storage
    assert "function setVisibleInventorySelected(selected)" in storage
    assert "renderedInventoryGroups.forEach((group) => {" in storage
    assert "const keys = groups.flatMap((group) =>" in storage
    assert "checkbox.indeterminate = selectedCount > 0 && selectedCount < keys.length" in storage
    assert "syncVisibleInventorySelection(groups)" in storage
    assert (
        'byId("selectVisibleInventory").onchange = (event) => '
        "setVisibleInventorySelected(event.currentTarget.checked)"
    ) in storage


def test_storage_inventory_keeps_mobile_and_desktop_scope_controls_in_sync() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="inventoryScopeSelect" class="inventory-scope-select"' in storage
    assert "function syncInventoryScopeControls()" in storage
    assert 'byId("inventoryScopeSelect").value = inventoryScope' in storage
    assert "async function setInventoryScope(scope)" in storage
    assert 'button.onclick = () => setInventoryScope(button.dataset.inventoryScope)' in storage
    assert (
        'byId("inventoryScopeSelect").onchange = (event) => '
        "setInventoryScope(event.currentTarget.value)"
    ) in storage


def test_storage_inventory_exposes_repairable_placement_issues() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert '<option value="placement">Placement issues</option>' in storage
    assert 'data-inventory-scope="placement">Placement issues</button>' in storage
    assert 'initialParams.get("scope")' in storage
    assert "Boolean(item.placement_issue)" in storage
    assert "Move into an item destination" in storage
    assert "Move into a bin, drawer, shelf, box, or pouch" in storage
    assert 'item.placement_issue === "missing_location"' in storage
    assert "Place no longer exists - move this item" in storage
    assert "Move to a valid place" in storage


def test_storage_inventory_reviews_and_undoes_batch_lifecycle_actions() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="lifecycleInventorySelection"' in storage
    assert "async function openItemLifecycleItems(itemIds, action" in storage
    assert "function openBulkItemLifecycle()" in storage
    assert 'api("/inventory/lifecycle/preview"' in storage
    assert 'api("/inventory/lifecycle/apply"' in storage
    assert "itemLifecycleRequest = {item_ids:itemIds, action, preview};" in storage
    assert "placements, photos, history, and learned homes" in storage
    assert 'selected.every((item) => item.active === false) ? "restore" : "archive"' in storage
    assert 'byId("lifecycleInventorySelection").textContent = restoring ? "Restore selected" : "Archive selected"' in storage
    assert 'byId("lifecycleInventorySelection").onclick = () => openBulkItemLifecycle()' in storage
    assert "offerUndo(`${result.item_count} item${result.item_count === 1 ? \"\" : \"s\"}" in storage


def test_storage_manager_makes_single_and_batch_place_labels_explicit() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="openLabelDialog"' in storage
    assert 'id="labelDialog" class="label-dialog"' in storage
    assert 'id="labelQr"' in storage
    assert 'id="labelScan"' in storage
    assert 'id="labelPdf"' in storage
    assert 'id="labelBatchPdf"' in storage
    assert "function openLabelPreview(locationId)" in storage
    assert 'byId("labelQr").src = `/locations/${encodedId}/qr.svg`' in storage
    assert 'byId("labelPdf").href = `/locations/${encodedId}/label.pdf`' in storage
    assert (
        'byId("labelBatchPdf").href = `/locations/${encodedId}/labels.pdf`'
        in storage
    )
    assert 'byId("labelBatchPdf").hidden = labelCount <= 1' in storage
    assert 'byId("labelScan").href = scanUrl("l", location.id)' in storage
    assert (
        'byId("openLabelDialog").onclick = () => openLabelPreview(location.id)'
        in storage
    )
    assert '>Label this place</button>' in storage


def test_storage_can_build_exact_cross_root_place_label_batches() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="printPlaceLabels"' in storage
    assert 'id="locationLabelBatchDialog" class="location-label-batch-dialog"' in storage
    assert 'id="locationLabelSearch" type="search"' in storage
    assert 'id="selectVisibleLocationLabels" type="checkbox"' in storage
    assert 'id="locationLabelBatchList"' in storage
    assert 'id="clearLocationLabelSelection"' in storage
    assert 'id="downloadLocationLabels"' in storage
    assert "const selectedLocationLabelIds = new Set();" in storage
    assert "function locationLabelCandidates()" in storage
    assert ".filter((location) => location.active)" in storage
    assert "function renderLocationLabelBatch()" in storage
    assert "function setVisibleLocationLabels(selected)" in storage
    assert "function openLocationLabelBatch()" in storage
    assert 'data-location-label="${escapeHtml(location.id)}"' in storage
    assert "if (selectedLocationLabelIds.size >= 100) break;" in storage
    assert "This batch has reached the 100-label limit." in storage
    assert 'location_ids:selectedIds.join(",")' in storage
    assert 'byId("printPlaceLabels").onclick = openLocationLabelBatch' in storage
    assert 'byId("locationLabelSearch").oninput = renderLocationLabelBatch' in storage
    assert "setVisibleLocationLabels(event.currentTarget.checked)" in storage
    assert "selectedLocationLabelIds.clear();" in storage
    assert 'byId("clearLocationLabelSelection").onclick' in storage
    assert ".location-label-batch-toolbar, .location-label-batch-footer { grid-template-columns:1fr; }" in storage


def test_storage_large_child_lists_use_searchable_progressive_disclosure() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "const INSIDE_PAGE_SIZE = 24;" in storage
    assert 'const NATURAL_COLLATOR = new Intl.Collator(undefined, {numeric:true, sensitivity:"base"});' in storage
    assert "function naturalCompare(left, right)" in storage
    assert ".sort((left, right) => naturalCompare(displayLocationLabel(left), displayLocationLabel(right)))" in storage
    assert 'id="insideList" class="inside-list" data-limit="${INSIDE_PAGE_SIZE}"' in storage
    assert 'id="insideProgressStatus" role="status"' in storage
    assert 'id="showMoreInside" class="secondary"' in storage
    assert 'id="insideScopes" class="inside-scopes"' in storage
    assert 'data-inside-scope="empty"' in storage
    assert 'data-inside-scope="used"' in storage
    assert 'data-inside-scope="destinations"' in storage
    assert 'data-inside-scope="destinations" aria-pressed="false">Stores items</button>' in storage
    assert 'data-child-item-count="${itemCount}"' in storage
    assert 'data-child-count="${childCount}"' in storage
    assert 'data-child-accepts-items="${child.accepts_items === true}"' in storage
    assert "function updateInsideChildren({more = false} = {})" in storage
    assert 'const scope = byId("insideScopes")?.querySelector("[data-inside-scope].active")?.dataset.insideScope || "all";' in storage
    assert "if (query !== previousQuery || scope !== previousScope) limit = INSIDE_PAGE_SIZE;" in storage
    assert "if (more) limit += INSIDE_PAGE_SIZE;" in storage
    assert 'if (scope === "empty") return itemCount === 0 && childCount === 0;' in storage
    assert 'if (scope === "used") return itemCount > 0 || childCount > 0;' in storage
    assert 'if (scope === "destinations") return row.dataset.childAcceptsItems === "true";' in storage
    assert "const matchIndexes = new Map(matches.map((row, index) => [row, index]));" in storage
    assert "row.hidden = index === undefined || index >= limit;" in storage
    assert "button.textContent = `Show ${Math.min(INSIDE_PAGE_SIZE, remaining)} more`;" in storage
    assert 'byId("insideFilter").oninput = () => updateInsideChildren()' in storage
    assert 'byId("insideScopes")?.querySelectorAll("[data-inside-scope]").forEach' in storage
    assert 'byId("showMoreInside").onclick = () => updateInsideChildren({more:true})' in storage
    assert ".inside-progress" in storage
    assert ".inside-toolbar { display:grid; grid-template-columns:1fr;" in storage
    assert ".inside-scopes { display:grid; grid-template-columns:repeat(4,minmax(0,1fr));" in storage
    assert 'setStatus(error.message || "Place details could not be saved")' in storage


def test_storage_manager_defaults_to_item_first_inventory_view() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    theme = THEME_CSS.read_text(encoding="utf-8")

    assert '<meta name="theme-color" content="#0f766e">' in storage
    assert "--bg: #f3f6f5;" in theme
    assert "--surface-muted: #edf2f0;" in theme
    assert "--surface-hover: #f0f5f3;" in theme
    assert "--warning-soft: #fff8e7;" in theme
    assert "--blue-soft: #eef5fb;" in theme
    assert "--accent: #0f766e;" in theme
    assert "--small: #e6f4f1;" in theme
    assert 'class="product-header"' in storage
    assert 'class="brand product-brand"' in storage
    assert 'class="product-nav"' in storage
    assert "#17324d" not in storage
    assert 'id="inventoryMain" class="inventory-main"' in storage
    assert 'id="storageMain" hidden' in storage
    inventory_markup = storage[
        storage.index('<main id="inventoryMain"'):storage.index(
            '<main id="rulesMain"'
        )
    ]
    assert "product-view-tabs" not in inventory_markup
    assert storage.count('aria-label="Storage settings"') == 2
    assert 'data-show-primary-view="inventory"' not in storage
    assert storage.count('data-show-primary-view="places"') == 2
    assert storage.count('data-show-primary-view="rules"') == 2
    assert 'const requestedPrimaryView = initialParams.get("view")' in storage
    assert '["places","rules"].includes(requestedPrimaryView)' in storage
    assert 'function setPrimaryView(view, updateUrl = true)' in storage
    assert 'function groupedInventory()' in storage
    assert 'groups.set(key, {item_id:item.item_id' in storage
    assert 'id="loadMoreInventory"' in storage
    assert "const INVENTORY_PAGE_SIZE = 50;" in storage
    assert 'const data = await api(`/inventory/catalog?${params}`)' in storage
    assert 'byId("loadMoreInventory").onclick = () => loadInventory({append:true})' in storage
    assert "inventoryItems = append" in storage
    assert "inventoryHasMore = Boolean(data.has_more)" in storage
    assert "inventorySearchTimer = setTimeout(() => loadInventory(), 250)" in storage
    assert 'data-inventory-scope="archived"' in storage
    assert 'id="itemLifecycleDialog"' in storage
    assert '"No archived items"' in storage
    assert 'id="inventorySearch"' in storage
    assert 'id="inventoryCategory"' in storage
    assert 'id="inventorySort"' in storage
    assert "Browse by what an item is. Every row still shows where it lives." in storage
    assert "Places used" in storage
    assert 'placeholder="Search items, categories, or places"' in storage
    assert 'const role = location.accepts_items === false ? "Contains places" : "Stores items";' in storage
    assert 'location.accepts_items ? "Stores items directly" : "Contains places only"' in storage
    assert "Organizer only" not in storage
    assert "Selected location" not in storage
    assert 'data-inventory-scope="review"' in storage
    assert 'data-inventory-scope="photos"' in storage
    assert 'data-inventory-scope="no-photo"' in storage
    assert 'data-inventory-scope="uncategorized"' in storage
    assert 'data-inventory-find="${escapeHtml(group.name)}"' in storage
    assert 'data-inventory-details="${escapeHtml(group.item_id)}"' in storage
    assert 'id="inventoryDetailDialog"' in storage
    assert 'id="inventoryDetailBody"' in storage
    assert "function openInventoryDetail(itemId)" in storage
    assert 'class="inventory-detail-gallery"' in storage
    assert 'class="inventory-profile"' in storage
    assert '<div class="inventory-profile-cell"><span>Home</span>' in storage
    assert 'home_location_id:item.home_location_id || ""' in storage
    assert 'data-detail-set-home="${escapeHtml(item.item_id)}"' in storage
    assert 'data-home-location="${escapeHtml(item.bin_id)}"' in storage
    assert '<span class="placement-home">Home</span>' in storage
    assert "async function setInventoryHome(itemId, locationId)" in storage
    assert 'api(`/inventory/${encodeURIComponent(itemId)}/home`' in storage
    assert "offerUndo(`Home set to ${pathLabel(locationId) || locationId}`)" in storage
    assert 'class="placement-review">Reported issue:' in storage
    assert 'data-detail-approve-review="${escapeHtml(item.item_id)}"' in storage
    assert 'data-review-location="${escapeHtml(item.bin_id)}"' in storage
    assert "async function approveInventoryReview(itemId, locationId)" in storage
    assert 'api("/inventory/reviewed"' in storage
    assert 'body:JSON.stringify({item_id:Number(itemId), bin_id:locationId})' in storage
    assert 'inventoryScope !== "review" || group?.placements.some((item) => item.needs_review)' in storage
    assert "function activeInventoryPlacements(group)" in storage
    assert 'data-detail-open-location="${escapeHtml(primaryActive.bin_id)}"' in storage
    assert 'locationActive = item.location_active === true' in storage
    assert ">Restore place</button>" in storage
    assert "async function openInventoryPlacement(locationId)" in storage
    assert "const archivedView = !location.active;" in storage
    assert 'group.active && hasActivePlacement && inventoryScope !== "review" ? `<button type="button" data-inventory-find=' in storage
    assert '${inventoryScope === "review" ? "Resolve" : "Details"}' in storage
    assert 'data-detail-edit="${escapeHtml(primaryActive.item_id)}"' in storage
    assert 'id="rulesMain" class="rules-main" hidden' in storage
    assert 'id="rulesSearch"' in storage
    assert 'id="rulesType"' in storage
    assert 'id="rulesStatus"' in storage
    assert '<option value="issues">Needs attention</option>' in storage
    assert 'const requestedRulesScope = initialParams.get("scope") === "issues" ? "issues" : "";' in storage
    assert 'status === "issues" && !(rule.rule_type === "placement" && rule.active && !rule.target_active)' in storage
    assert "Edit this rule or disable it." in storage
    assert 'id="correctionEvidenceList"' in storage
    assert 'id="correctionEvidenceStatus"' in storage
    assert 'id="refreshCorrectionEvidence"' in storage
    assert "function renderCorrectionEvidence()" in storage
    assert "function loadCorrectionEvidence()" in storage
    assert "function reviewCorrectionEvidence(exampleId, action)" in storage
    assert 'scenario:"correction"' in storage
    assert "action.before || action.after" in storage
    assert "Array.isArray(action.corrected_plan)" in storage
    assert "The reviewed inventory values were confirmed." in storage
    assert "Edited the photo sorting plan before confirmation" in storage
    assert 'data-approve-correction="${escapeHtml(example.id)}"' in storage
    assert 'data-reject-correction="${escapeHtml(example.id)}"' in storage
    assert "Rejecting evidence stops that reuse; it does not undo the inventory change." in storage
    assert ">Approve evidence</button>" in storage
    assert ">Reject evidence</button>" in storage
    assert 'const data = await api("/rules")' in storage
    assert "function renderRuleConflicts(manage)" in storage
    assert "Both match:" in storage
    assert "These rules share triggers but lead to different outcomes." in storage
    assert "Outcome:" in storage
    assert "bindRuleControls(panel)" in storage
    assert "if (rulesLoaded) await loadRules();" in storage
    assert 'function openRuleEditor(key)' in storage
    assert 'function openRuleStatus(key, action)' in storage
    assert 'method:"PATCH"' in storage
    assert '/${action}`, {method:"POST"}' in storage
    assert "Changes here are deterministic and do not call the AI." in storage
    assert 'class="storage-start-options" role="group" aria-label="Choose a storage space type"' in storage
    assert 'id="storageSetupDialog" class="storage-setup-dialog"' in storage
    assert 'id="storageSetupForm"' in storage
    assert 'data-setup-template="home"' in storage
    assert 'data-setup-template="workshop"' in storage
    assert 'data-setup-template="studio"' in storage
    assert 'data-setup-template="storage"' in storage
    assert 'id="storageSetupDestinationEnabled" type="checkbox" checked' in storage
    assert 'id="storageSetupDestinationParent"' in storage
    assert 'id="storageSetupDestinationName" required' in storage
    assert 'id="storageSetupDestinationType"' in storage
    assert "Create a first container" in storage
    assert "Magpie will have a real destination ready for your first put-away." in storage
    assert 'id="addStorageSpace" type="button">Set up storage</button>' in storage
    assert 'id="storageSetupTitle">Set up a storage space</span>' in storage
    assert '<label for="storageSetupName">Storage space name</label>' in storage
    assert '<div class="setup-section-title">Places inside this space</div>' in storage
    assert "Directly inside the storage space" in storage
    assert "Top-level place" not in storage
    assert "top-level place" not in storage
    assert 'id="storageSetupSectionCopy" class="setup-section-copy"' in storage
    assert 'id="storageSetupPreviewTitle">Structure preview</span>' in storage
    assert 'id="storageSetupPreviewCount"' in storage
    assert (
        'id="storageSetupPreviewPaths" class="setup-preview-paths" '
        'aria-live="polite"'
    ) in storage
    assert 'firstSetup ? "Set up a storage space" : "Add a storage space"' in storage
    assert "Select rooms or zones inside this storage space" in storage
    assert 'firstSetup ? "Create storage space" : "Add storage space"' in storage
    assert "function storageSetupLabels()" in storage
    assert "function storageSetupRootNameExists(name)" in storage
    assert "function defaultStorageSetupTemplate(firstSetup)" in storage
    assert "function syncStorageSetupValidation()" in storage
    assert "function storageSetupValidationIssue()" in storage
    assert "Choose no more than 20 starter places." in storage
    assert "Each starter place name must be 120 characters or fewer." in storage
    assert "is already a starter place at this level." in storage
    assert 'setAttribute("aria-invalid", String(issue?.field === "root"))' in storage
    assert 'setAttribute("aria-invalid", String(issue?.field === "children"))' in storage
    assert 'setAttribute("aria-invalid", String(issue?.field === "destination"))' in storage
    assert 'aria-describedby="storageSetupNameError"' in storage
    assert 'aria-describedby="storageSetupChildrenError"' in storage
    assert 'aria-describedby="storageSetupDestinationError"' in storage
    assert 'class="setup-field-error" hidden' in storage
    assert "Object.hasOwn(STORAGE_SETUP_TEMPLATES, options?.template)" in storage
    assert ": defaultStorageSetupTemplate(firstSetup)" in storage
    assert "already exists. Use a distinct name for this storage space." in storage
    assert "if (!syncStorageSetupValidation()) return;" in storage
    assert "function renderStorageSetupPreview()" in storage
    assert "const visible = rows.slice(0, 6)" in storage
    assert "function renderStorageSetupDestinationParents()" in storage
    assert "function syncStorageSetupDestination()" in storage
    assert "function renderStorageSetupTemplate({preserveInputs = false} = {})" in storage
    assert "const previousLabels = preserveInputs ? storageSetupLabels() : [];" in storage
    assert "preservedLabels.filter((label) => !suggestedKeys.has(label.toLocaleLowerCase()))" in storage
    assert "preserveInputs && storageSetupNameTouched" in storage
    assert (
        'byId("storageSetupDestinationName").value = '
        "preserveInputs && storageSetupDestinationNameTouched"
    ) in storage
    assert "? previousDestinationName" in storage
    assert "renderStorageSetupTemplate({preserveInputs:true})" in storage
    assert "function openStorageSetup(options = {})" in storage
    assert "async function createStorageSetup(event)" in storage
    assert "let storageSetupNameTouched = false" in storage
    assert "let storageSetupDestinationNameTouched = false" in storage
    assert "storageSetupNameTouched = true" in storage
    assert "storageSetupDestinationNameTouched = true" in storage
    assert (
        'byId("storageSetupDestinationType").onchange = '
        "renderStorageSetupPreview"
    ) in storage
    assert 'const result = await api("/storage/setup"' in storage
    assert "parent_label:byId(\"storageSetupDestinationParent\").value || null" in storage
    assert "const focus = result.destination || result.children?.[0] || result.root" in storage
    assert "(result.created || []).map((location) => location.id)" in storage
    assert "result.root.id," in storage
    assert ".setup-destination-fields { margin-left:0; }" in storage
    assert 'byId("storageSetupBlank").onclick' in storage
    assert 'byId("addStorageSpace").onclick = openStorageSetup' in storage
    assert "Object.hasOwn(STORAGE_SETUP_TEMPLATES, options?.template)" in storage
    assert 'id="creationNext"' in storage
    assert 'id="creationReceiptClose"' in storage
    assert 'id="creationPreviewLabel"' in storage
    assert "const nextAction = byId(\"creationNext\")" in storage
    assert '(resumePutAway ? resumePutAwayUrl(location.id) : mobileAgentUrl("Put away ", location.id, "put-away"))' in storage
    assert 'new URLSearchParams({view:"places", location:location.id, create:"inside"})' in storage
    assert "(result.created || []).map((location) => location.id)" in storage
    assert '"Put away your first item"' in storage
    assert '"Add your first place inside"' in storage
    assert 'id="buildStructure"' in storage
    assert 'id="structureDialog" class="structure-dialog"' in storage
    assert 'data-structure-template="cabinet"' in storage
    assert 'data-structure-template="rack"' in storage
    assert 'data-structure-template="shelf"' in storage
    assert 'data-structure-template="drawer"' in storage
    assert 'const result = await api("/locations/structure"' in storage
    assert "result.created.map((location) => location.id)" in storage
    assert "const createdIds = response.created.map((location) => location.id)" in storage
    assert 'showCreationReceipt(createdIds[0], false, createdIds, "Places ready")' in storage
    assert "You can undo the complete structure in one step." in storage
    assert "/locations/labels.pdf?${new URLSearchParams" in storage
    assert "primaryLabels.hidden = !labelIds.length" in storage
    assert "setupComplete || createdCount <= 1" not in storage
    assert "? `Download ${createdCount} labels`" in storage
    assert ': "Download label"' in storage
    assert 'openLabelPreview(locationId)' in storage
    assert 'data-detail-move="${escapeHtml(primaryActive.item_id)}"' in storage
    assert 'data-detail-remove="${escapeHtml(item.item_id)}"' in storage
    for action in (
        "data-detail-find",
        "data-detail-photo",
        "data-detail-history",
        "data-detail-open-location",
        "data-detail-edit",
        "data-detail-move",
    ):
        assert action in storage
    assert 'await loadInventory()' in storage


def test_places_surface_exposes_legacy_duplicate_name_conflicts() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="placeConflicts" class="place-conflicts"' in storage
    assert "function placeNameConflictGroups()" in storage
    assert "function renderPlaceConflicts()" in storage
    assert 'location.parent_id || ""' in storage
    assert "group.length > 1" in storage
    assert 'id="placeConflictDialog" class="place-conflict-dialog"' in storage
    assert "async function openPlaceConflictDialog(groupIndex)" in storage
    assert "async function resolvePlaceConflict(locationId, action)" in storage
    assert 'data-resolve-place-conflict="rename"' in storage
    assert 'data-resolve-place-conflict="move"' in storage
    assert 'data-resolve-place-conflict="archive"' in storage
    assert "const hasMeaningfulPlace = group.some" in storage
    assert "const emptyCandidates = hasMeaningfulPlace" in storage
    assert "const lifecyclePreviews = await Promise.all" in storage
    assert 'api(`/locations/${encodeURIComponent(location.id)}/removal-preview`)' in storage
    assert "lifecyclePreviews[index]?.can_remove === true" in storage
    assert "const recommendedIds = new Set(" in storage
    assert (
        "Magpie found an unreferenced empty duplicate; archiving it is recommended and reversible."
    ) in storage
    assert 'class="place-conflict-recommendation">Empty duplicate</span>' in storage
    assert '"Archive empty duplicate"' in storage
    conflict_dialog = storage[
        storage.index("function openPlaceConflictDialog"):
        storage.index("async function resolvePlaceConflict")
    ]
    assert "displayLocationLabel(location)</strong>" not in conflict_dialog
    assert (
        "Nothing changes until you choose an action; inventory and history "
        "stay attached to that place."
    ) in storage
    assert 'if (settings) settings.open = true;' in storage
    assert "openMoveLocationDialog(locationId);" in storage
    assert 'await openLocationLifecycle("archive");' in storage


def test_storage_inventory_removal_uses_reviewed_dialog() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="removeItemDialog" class="remove-item-dialog"' in storage
    assert 'id="removeItemName"' in storage
    assert 'id="removeItemPath"' in storage
    assert 'id="confirmRemoveItem"' in storage
    assert "function openRemoveItem(itemId, sourceLocation, name)" in storage
    assert "async function removeItem()" in storage
    assert 'byId("removeItemDialog").showModal()' in storage
    assert 'byId("confirmRemoveItem").textContent = "Removing"' in storage
    assert "if (!removingItem || removeItemRequest) return;" in storage
    assert "confirm(`Remove ${name} from" not in storage


def test_storage_location_lifecycle_uses_reviewed_archive_and_removal_dialog() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="locationLifecycleDialog" class="location-lifecycle-dialog"' in storage
    assert 'id="locationLifecycleScope" class="lifecycle-scope"' in storage
    assert 'id="locationLifecycleMessage" class="lifecycle-message" role="status"' in storage
    assert 'id="locationLifecycleBlockers" class="lifecycle-blockers"' in storage
    assert 'id="confirmLocationLifecycle"' in storage
    assert 'id="reviewLocationLifecycle"' in storage
    assert 'id="archiveInsteadLocationLifecycle"' in storage
    assert "function lifecycleBlockerGuidance(preview)" in storage
    assert "function lifecycleReferenceSummary(preview)" in storage
    assert 'lifecycleReferenceCount(preview) === 1 ? "stays" : "stay"' in storage
    assert "function openLocationLifecycle(mode, locationId = selectedId, focusId = locationId)" in storage
    assert "function applyLocationLifecycle()" in storage
    assert 'openLocationLifecycle("remove")' in storage
    assert 'openLocationLifecycle("archive")' in storage
    assert 'openLocationLifecycle("restore", locationId, focusId)' in storage
    assert 'api(`/locations/${encodeURIComponent(location.id)}/removal-preview${previewSuffix}`)' in storage
    assert 'mode === "restore" ? "?include_archived=true" : ""' in storage
    assert "preview.inventory_count" in storage
    assert "preview.home_item_count" in storage
    assert "preview.placement_rule_count" in storage
    assert "preview.hardware_binding_count" in storage
    assert "Permanent removal is blocked." in storage
    assert "This permanently removes the empty place and its nested places. It cannot be undone." in storage
    assert "Reviewing inventory, learned homes, placement rules, and hardware bindings before archiving." in storage
    assert "Items, photos, labels, and history stay intact." in storage
    assert "Archived places stop appearing in put-away suggestions until restored." in storage
    assert "locationLifecycleRequest = true;" in storage
    assert "locationLifecycleRequest = false;" in storage
    assert 'confirmButton.textContent = "Archive"' in storage
    assert 'confirmButton.textContent = "Restore"' in storage
    assert storage.count("showMobileBrowser({resetScroll:true})") == 3
    assert "if (!target.preview ||" in storage
    assert "Nothing changed. Close this review and try again." in storage
    assert 'selectedId = target.parent_id || "";' in storage
    assert 'currentRootId = target.parent_id ? rootIdFor(target.parent_id) : "";' in storage
    assert "await loadLocations(selectedId);" in storage
    assert "function archivedRestoreTarget(locationId)" in storage
    assert "if (!parent || parent.active) break;" in storage
    assert "Restore the group to preserve its storage path." in storage
    assert "restoreSelected(restoreTarget.id, location.id)" in storage
    assert "function restoreSelected(locationId = selectedId, focusId = selectedId)" in storage
    assert "await loadLocations(target.focus_id);" in storage
    assert "confirm(`Remove ${location.label}" not in storage
    assert "confirm(`Archive ${location.label}" not in storage


def test_primary_surfaces_share_product_theme_contract() -> None:
    theme = THEME_CSS.read_text(encoding="utf-8")

    for page in (
        INDEX_HTML,
        MOBILE_HTML,
        KIOSK_HTML,
        STORAGE_HTML,
        ACCOUNT_HTML,
        CHECK_STATION_HTML,
    ):
        html = page.read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/theme.css">' in html
        assert ":root {" not in html

    assert ':root[data-theme="kiosk"]' in theme
    kiosk_theme = theme.split(':root[data-theme="kiosk"] {', 1)[1].split("}", 1)[0]
    for token in (
        "--bg:",
        "--panel:",
        "--surface:",
        "--surface-subtle:",
        "--surface-muted:",
        "--surface-hover:",
        "--surface-inset:",
        "--line-strong:",
        "--accent-strong:",
        "--soft:",
        "--danger-ink:",
        "--danger-soft:",
        "--warning-soft:",
        "--positive:",
        "--focus-ring:",
        "--overlay:",
        "--shadow:",
    ):
        assert token in kiosk_theme
    assert ".product-header" in theme
    assert "min-height: 65px;" in theme
    assert ".product-brand" in theme
    assert ".product-mark" in theme
    assert 'url("/app-icon.svg")' in theme
    assert "text-indent: -999px" in theme
    assert ".product-title" in theme
    assert ".product-context" in theme
    assert ".product-header-actions" in theme
    assert ".product-status" in theme
    assert ".product-load-state" in theme
    assert ".product-inline-notice" in theme
    assert ".product-app .product-bottom-nav" in theme
    assert ".product-app .product-bottom-nav a::before" in theme
    assert ".product-app .product-bottom-nav a.active::before" in theme
    assert ".product-nav a.active" in theme
    assert ".product-nav a:hover" in theme
    assert ":where(.product-app) button" in theme
    assert "color: var(--accent-ink);" in theme
    assert ":where(.product-app) input" in theme
    assert ':where(.product-app) input[type="checkbox"]' in theme
    assert "accent-color: var(--accent);" in theme
    assert ".product-view-tabs" in theme
    assert ".product-view-tab.active" in theme
    assert ".product-object-main" in theme
    assert ".product-object-grid" in theme
    assert ".product-object-form" in theme
    assert ".product-object-list" in theme
    assert ".product-object-actions" in theme
    assert ":focus-visible" in theme
    assert "@media (pointer: coarse), (max-width: 680px)" in theme
    for touch_selector in (
        ".product-app button",
        ".product-app a",
        ".product-app .button",
        '.product-app input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"])',
        ".product-app select",
        ".product-app textarea",
        ".product-app summary",
        ".product-app .receipt-action",
    ):
        assert touch_selector in theme
    assert ".product-app button[aria-label]" in theme
    assert 'title="Remove photo" aria-label="Remove photo"' in INDEX_HTML.read_text(
        encoding="utf-8"
    )
    assert '<span class="product-context" data-product-context>Assistant</span>' in (
        MOBILE_HTML.read_text(encoding="utf-8")
    )
    assert '<html lang="en" data-theme="kiosk">' in KIOSK_HTML.read_text(
        encoding="utf-8"
    )
    for page in (INDEX_HTML, STORAGE_HTML, ACCOUNT_HTML, CHECK_STATION_HTML):
        html = page.read_text(encoding="utf-8")
        assert 'class="product-header"' in html
        assert "product-brand" in html
        assert 'class="product-mark"' in html
        assert 'class="product-title"' in html
        assert 'class="product-nav"' in html
    for page in (STORAGE_HTML, ACCOUNT_HTML):
        html = page.read_text(encoding="utf-8")
        assert "data-product-context" in html
        assert 'data-product-nav aria-label="Product views"' in html
    index = INDEX_HTML.read_text(encoding="utf-8")
    assert '<span class="product-context" data-product-context>Assistant</span>' in index
    assert "data-include-cabinet" not in index
    assert "Find and put away across all storage" in index
    for page in (
        INDEX_HTML,
        STORAGE_HTML,
        MOBILE_HTML,
        ACCOUNT_HTML,
        CHECK_STATION_HTML,
    ):
        html = page.read_text(encoding="utf-8")
        assert '<body class="product-app">' in html
        assert '<script src="/product-shell.js" defer></script>' in html
    check_station = CHECK_STATION_HTML.read_text(encoding="utf-8")
    assert ">Check station</span>" in check_station
    assert 'class="button secondary station-exit"' in check_station
    assert "brand-mark" not in check_station
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    assert '<header class="product-header">' in mobile
    assert 'class="brand product-brand"' in mobile
    assert 'class="connection product-status"' in mobile
    assert "<strong>Find an item</strong>" in mobile
    assert "<strong>Put away an item</strong>" in mobile
    assert "<strong>Sort a photo</strong>" in mobile
    assert ".install-button { min-height:36px;" in mobile
    assert ".new-conversation { width:36px; min-width:36px; height:36px;" in mobile
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    assert ".section-tabs { top:65px; }" in storage


def test_assistant_surfaces_share_navigation_and_confirmation_contract() -> None:
    desktop = INDEX_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    kiosk = KIOSK_HTML.read_text(encoding="utf-8")
    confirmation = CONFIRMATION_JS.read_text(encoding="utf-8")
    theme = THEME_CSS.read_text(encoding="utf-8")

    assert 'class="product-nav"' in desktop
    assert 'class="product-nav"' in mobile
    for html in (desktop, mobile, kiosk):
        assert '<script src="/confirmation.js"></script>' in html
        assert "MagpieConfirmation" in html
    for html in (desktop, mobile):
        assert "product-confirmation" in html
    for html in (desktop, mobile, kiosk):
        assert "product-confirmation-actions" in html
        assert "product-confirmation-path" in html
        assert "product-confirmation-row" in html
        assert "MagpieConfirmation.changePrompts" in html
    assert ".product-confirmation-row" in theme
    assert ".product-confirmation-actions" in theme
    assert ".product-confirmation-explanation" in theme
    assert ".product-confirmation-change-prompts" in theme
    assert "--conversation-max" in theme
    for html in (desktop, mobile):
        assert "function confirmationExplanation(presentation)" in html
        assert "MagpieConfirmation.explanation(presentation)" in html
        assert "MagpieConfirmation.changePrompts(presentation)" in html
        assert "MagpieConfirmation.actionLabel(presentation, actionId, fallback)" in html
        assert "MagpieConfirmation.row(presentation, index)" in html
        assert 'data-change-id="${escapeHtml(prompt.id)}"' in html
        assert 'data-change-prompt="${escapeHtml(prompt.text)}"' in html
        assert "product-confirmation-change-prompts" in html
    assert "function changePrompts(presentation)" in confirmation
    assert ".slice(0, 4)" in confirmation
    assert "changePrompts," in confirmation
    assert "function applyPendingPlanRevisionPrompt(text, changeId = \"\")" in desktop
    assert 'if (changeId === "different_place")' in desktop
    assert "function openPendingPlanDestinationPicker()" in desktop
    assert "function revisePendingPlanDestination(stepIndex, locationId, picker)" in desktop
    assert "location_id:locationId" in desktop
    assert "step_index:stepIndex" in desktop
    assert "{destinationsOnly:true}" in desktop
    assert 'revisePendingPlan(correction, "ui")' in desktop
    assert "function applyPlanRevisionPrompt(text, changeId = \"\", node = null)" in mobile
    assert 'if (changeId === "different_place")' in mobile
    assert "async function openPlanDestinationPicker(node = null)" in mobile
    assert "async function revisePlanDestination(stepIndex, locationId, picker)" in mobile
    assert "location_id:locationId" in mobile
    assert "step_index:stepIndex" in mobile
    assert 'submitRevision("mobile")' in mobile
    assert 'value?.label || "Why this location?"' in confirmation
    assert '"No light"' in confirmation
    assert '!rowValue?.light?.location_id' in confirmation
    assert '`${name} · No light`' in confirmation
    assert "Light did not respond" in confirmation
    assert "Ready to light" in confirmation
    assert "window.MagpieConfirmation = Object.freeze" in confirmation
    assert "function uniqueLightRows(rows)" in mobile
    assert "const rows = uniqueLightRows(" in mobile


def test_desktop_assistant_uses_top_aligned_command_workspace() -> None:
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    index = INDEX_HTML.read_text(encoding="utf-8")

    assert "@media (min-width:681px)" in mobile
    assert "grid-template-columns:minmax(0,var(--command-max)) minmax(280px,340px);" in mobile
    assert '"feed sidecar";' in mobile
    assert '<main class="assistant-main">' in mobile
    assert "width:min(var(--workspace-max),100%);" in mobile
    assert "padding:28px var(--page-gutter) calc(var(--dock-space) + 12px);" in mobile
    assert '<aside class="assistant-sidecar" aria-label="Assistant attention items">' in mobile
    assert ".assistant-sidecar { grid-area:sidecar;" in mobile
    assert 'main:not(:has(.assistant-sidecar > :not([hidden]))) {' in mobile
    assert 'main:not(:has(.assistant-sidecar > :not([hidden]))) .assistant-sidecar { display:none; }' in mobile
    assert 'body:not(:has(.assistant-sidecar > :not([hidden]))) .composer-shell > * { margin-left:auto; }' in mobile
    assert '.assistant-main > .feed { grid-area:feed;' in mobile
    assert ".feed > :not(.welcome) { width:min(var(--conversation-max),100%); }" not in mobile
    assert ".feed > .message { width:fit-content; }" in mobile
    assert ".feed > .plan-card," in mobile
    assert ".feed > .evidence-card { width:min(var(--conversation-max),100%); }" in mobile
    assert "@media (min-width:681px) and (max-width:1120px)" in mobile
    assert (
        ".feed:has(> .welcome:only-child) { margin-top:0; margin-bottom:18px; }"
        in mobile
    )
    assert "color:#d9efeb" not in mobile
    assert "item.location_path_label || item.bin_id" in mobile
    assert "item.location_path_label || item.bin_id" in index


def test_product_shell_maps_scanned_objects_to_their_visual_views() -> None:
    shell = PRODUCT_SHELL_JS.read_text(encoding="utf-8")

    assert r'/^\/(?:scan\/[^/]+\/)?i\/[^/]+\/?$/' in shell
    assert 'return "inventory"' in shell
    assert r'/^\/(?:scan\/[^/]+\/)?l\/[^/]+\/?$/' in shell
    assert 'return "places"' in shell


def test_assistant_disambiguates_storage_roots_with_duplicate_labels() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const labelCounts = containerRoots.reduce" in html
    assert "const displayLabel = labelCounts.get(label) > 1" in html
    assert "`${label} (${c.id})`" in html


def test_storage_manager_exposes_preview_first_data_portability() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="openDataDialog"' in storage
    assert 'id="dataDialog" class="data-dialog"' in storage
    assert 'href="/export/inventory.json"' in storage
    assert 'href="/export/inventory.csv"' in storage
    assert 'href="/export/inventory.pdf"' in storage
    assert 'id="dataImportFile" type="file"' in storage
    assert 'id="dataDefaultLocation" class="field" hidden' in storage
    assert 'id="dataDefaultLocationSelect"' in storage
    assert "async function previewDataImport(event)" in storage
    assert "async function applyReviewedDataImport()" in storage
    assert 'body = {snapshot, dry_run:true}' in storage
    assert 'body:{snapshot, dry_run:false}' in storage
    assert 'body = {csv_text:text, dry_run:true}' in storage
    assert "create_missing_locations:createMissingLocations" in storage
    assert "preview.requires_default_location" in storage
    assert "preview.missing_location_rows || []" in storage
    assert "location.active && location.accepts_items" in storage
    assert "default_location_id:locationId" in storage
    assert "without a place will go to ${destination}" in storage
    assert 'byId("dataPreview").hidden = false' in storage
    assert 'byId("applyDataImport").onclick = () => applyReviewedDataImport()' in storage
    assert 'pending.kind === "inventory"' in storage
    assert "result.interaction_id" in storage
    assert "result.applied?.length" in storage
    assert "offerUndo(message)" in storage
    assert 'byId("dataImportSection").hidden = viewer' in storage
    assert "validationErrors.join(\"; \")" in storage


def test_storage_manager_assigns_location_light_controllers() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    assert "async function loadEdgeDevices(identity)" in storage
    assert 'identity?.workspace_id ? "/workspace/devices" : "/edge/status"' in storage
    assert "await loadEdgeDevices(identity)" in storage
    assert 'id="hardwareController"' in storage
    assert 'id="hardwareAddress"' in storage
    assert "function parseLightIndexes(value)" in storage
    assert "function hardwareControllerOptions(current" in storage
    assert "function syncHardwareControllerOptions(current" in storage
    assert "binding.metadata.pixels = parseLightIndexes(binding.address)" in storage
    assert 'api(`/inventory/${encodeURIComponent(itemId)}`' in storage
    assert 'dimensions: editingItem.dimensions || null' in storage
    assert 'id="itemPhotoInput"' in storage
    assert 'data-photo-item="${escapeHtml(item.item_id)}"' in storage
    assert "async function attachItemPhoto(event)" in storage
    assert 'id="mergeItemDialog"' in storage
    assert 'data-detail-merge="${escapeHtml(group.item_id)}"' in storage
    assert "function openItemMerge(itemId, name)" in storage
    assert "async function reviewItemMerge(targetItemId)" in storage
    assert "async function applyItemMerge()" in storage
    assert '}/merge-preview?target_item_id=${encodeURIComponent(targetItemId)}`' in storage
    assert 'body:JSON.stringify({target_item_id:request.target_item_id})' in storage
    assert 'tool:"attach_photo"' in storage
    assert 'href="/items/${encodeURIComponent(item.item_id)}/label.pdf"' in storage
    assert 'id="locationPhotoInput"' in storage
    assert 'id="addLocationPhoto"' in storage
    assert "async function attachLocationPhoto(event)" in storage
    assert 'object_type:"location"' in storage
    assert 'class="location-photos"' in storage
    assert 'id="undoAction"' in storage
    assert "async function undoLastAction()" in storage
    assert 'setStatus(result.message || "Last change undone")' in storage
    assert 'api("/assistant/undo", {method:"POST"})' in storage
    assert 'id="historyDialog"' in storage
    assert 'data-history-item="${escapeHtml(item.item_id)}"' in storage
    assert "function showItemHistory(itemId, name)" in storage
    assert "function showLocationHistory(locationId, name)" in storage
    assert '`/items/${encodeURIComponent(id)}/history`' in storage
    assert '`/locations/${encodeURIComponent(id)}/history`' in storage
    assert "function renderHistory(events, kind)" in storage
    assert 'offerUndo(`${payload.name} updated`)' in storage
    assert 'const resolvedReview = result.resolved_review_count ? " and resolved Needs Review"' in storage
    assert 'const moveSummary = `Moved ${movedLabel}${resolvedReview}' in storage
    assert "offerUndo(moveSummary)" in storage
    assert 'offerUndo(`${target.name} removed from this place`)' in storage
    assert 'offerUndo(`${target.label} archived`)' in storage
    assert 'offerUndo(`${target.label} restored`)' in storage
    assert 'offerUndo(`${response.location?.label || response.id} created`)' in storage
    assert 'offerUndo(`Created ${response.created.length} places`)' in storage
    assert "const savedLocation = locationById(selectedId)" in storage
    assert 'offerUndo(`${savedLocation?.label || location.label} saved`)' in storage
    assert 'id="moveLocationDialog"' in storage
    assert 'id="moveLocationSearch"' in storage
    assert 'id="moveLocationDestinations"' in storage
    assert "nodes:hierarchyPickerLocations(new Set(subtree(source.id)))" in storage
    assert "moveLocationBrowseId = source.parent_id || \"\"" in storage
    assert "function openMoveLocationDialog(locationId)" in storage
    assert 'api(`/locations/${encodeURIComponent(source.id)}/move`' in storage
    assert "body:JSON.stringify({new_parent_id:newParentId || null})" in storage
    assert 'offerUndo(`Moved ${source.label} ${moveDescription}`)' in storage
    assert 'id="editParent"' not in storage
    assert '/inventory/${encodeURIComponent(target.item_id)}/locations/' in storage
    assert 'mobileAgentUrl("Put away ", location.id, "put-away")' in storage
    assert 'href="${scanUrl("i", item.item_id)}"' in storage


def test_mobile_storage_add_inside_action_keeps_context_without_long_button_copy() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    assert 'id="addPlaceContext" class="place-creation-context"' in storage
    assert '`Adding inside ${parentName}`' in storage
    assert ".place-creation-context { display:block; grid-column:1/-1;" in storage
    assert "const creationParent = locationById(overviewCreationParentId())" in storage
    assert "creationParent ? \"Add inside\" : \"Add place\"" in storage
    assert '`Add inside ${displayLocationLabel(creationParent)}`' in storage
    assert "function overviewCreationParentId()" in storage
    assert '!byId("storageMain").classList.contains("mobile-detail-open")' in storage
    assert "return currentRootId || selectedId || null" in storage
    assert 'byId("addPlace").onclick = () => openLocationDialog(overviewCreationParentId())' in storage
    assert 'byId("addChild").onclick = () => openLocationDialog(overviewCreationParentId())' in storage


def test_mobile_inventory_keeps_items_in_the_first_viewport() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert ".inventory-metrics { grid-template-columns:repeat(4,minmax(0,1fr))" in storage
    assert ".inventory-toolbar { grid-template-columns:repeat(2,minmax(0,1fr))" in storage
    assert ".inventory-search { grid-column:1/-1; }" in storage
    assert 'id="inventoryScopeSelect" class="inventory-scope-select"' in storage
    assert ".inventory-filters {" in storage
    assert "grid-template-columns:minmax(0,1fr) auto;" in storage
    assert ".inventory-scope-select { display:block; min-width:0; }" in storage
    assert ".inventory-filter { display:none; }" in storage
    assert 'class="inventory-name inventory-name-button"' in storage
    assert 'data-inventory-details="${escapeHtml(group.item_id)}"' in storage


def test_storage_manager_captures_physical_location_dimensions() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    for prefix in ("location", "batch", "edit"):
        assert f'id="{prefix}Width"' in storage
        assert f'id="{prefix}Depth"' in storage
        assert f'id="{prefix}Height"' in storage
        assert f'id="{prefix}DimensionUnit"' in storage
    assert 'const MM_PER_INCH = 25.4' in storage
    assert 'function dimensionPayload(prefix)' in storage
    assert 'function changeDimensionUnit(prefix)' in storage
    assert 'function formatDimensions(dimensions = {})' in storage
    assert 'internal_dimensions:dimensionPayload("location")' in storage
    assert 'internal_dimensions:dimensionPayload("batch")' in storage
    assert 'internal_dimensions:dimensionPayload("edit")' in storage
    assert 'id="locationAcceptsItems" type="checkbox"' in storage
    assert 'id="editAcceptsItems" type="checkbox"' in storage
    assert "function typeAcceptsItemsByDefault(type)" in storage
    assert 'accepts_items:byId("locationAcceptsItems").checked' in storage
    assert 'accepts_items:byId("editAcceptsItems").checked' in storage
    assert 'location.accepts_items ? "Stores items directly" : "Contains places only"' in storage
    assert 'nextAction.dataset.action = readyForPutAway ? "put-away" : "add-inside"' in storage
    assert 'formatDimensions(location.internal_dimensions)' in storage


def test_storage_manager_prioritizes_daily_actions_on_phone_screens() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    overview = storage.index('class="location-overview"')
    primary_actions = storage.index('class="primary-actions"', overview)
    assert 'const contents = `<div class="contents-head">' in storage
    contents = storage.index("${contents}", primary_actions)
    settings = storage.index('class="location-settings-group"', contents)

    assert overview < primary_actions < contents < settings
    assert "<summary>Place details</summary>" in storage
    assert '<details class="location-settings product-disclosure place-details">' in storage
    assert '<details class="location-settings product-disclosure hardware-settings">' in storage
    assert '<details class="location-settings product-disclosure lifecycle-settings">' in storage
    assert storage.index("<summary>Place details</summary>") < storage.index(
        "<summary>Connected hardware "
    )
    assert storage.index("<summary>Connected hardware ") < storage.index(
        "<summary>Archive or remove</summary>"
    )
    assert "Save details" in storage
    assert "Archive keeps inventory history and can be reversed." in storage
    assert "locationSettingsOpenAttribute" not in storage
    assert (
        ".primary-actions { display:grid; "
        "grid-template-columns:repeat(auto-fit,minmax(118px,1fr));"
        in storage
    )
    assert (
        ".primary-actions { position:sticky; top:107px; z-index:3; "
        "grid-template-columns:1fr 1fr;"
        in storage
    )
    assert 'id="detailAddInside"' in storage
    assert '>Add inside</button>' in storage
    assert 'id="detailAddMany" class="secondary" type="button">Add several places</button>' in storage
    assert 'class="management-menu-panel"' in storage
    assert 'document.querySelectorAll(".management-menu[open]")' in storage
    assert '.primary-actions { position:sticky;' in storage
    assert '.item-more-actions { position:absolute;' in storage


def test_storage_manager_distinguishes_loading_empty_and_error_states() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    initial_tree = storage[
        storage.index('id="tree" class="tree"'):
        storage.index("</section>", storage.index('id="tree" class="tree"'))
    ]
    assert "Add a storage place to begin." not in initial_tree
    assert 'aria-busy="true"' in initial_tree
    assert 'class="skeleton"' in storage
    assert "function renderStorageLoading()" in storage
    assert "function renderStorageError(error)" in storage
    assert 'id="retryStorage"' in storage
    assert "Try again" in storage
    assert ":focus-visible" in storage
    assert ".inventory-select-visible," in storage
    assert ".item-more-actions .button { min-height:44px; }" in storage
    assert ".dialog-head button," in storage
    assert ".history-head button { width:44px; height:44px; min-height:44px; }" in storage
    assert ".contents-progress.error {" in storage
    assert 'id="retryLocationContents"' in storage
    assert "byId(\"retryLocationContents\").onclick = () => loadContents(reset)" in storage


def test_default_assistant_is_agent_first_without_kiosk_or_diagnostic_polling() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    theme = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(560px, 1fr) minmax(380px, 440px)" in html
    assert "width: min(var(--workspace-max), 100%)" in html
    assert "padding: 16px calc(var(--page-gutter)" in html
    assert "--workspace-max: 1420px" in theme
    assert "--page-gutter: clamp(16px, 4vw, 56px)" in theme
    assert "padding:28px var(--page-gutter) 56px" in storage
    assert 'id="addPlace"' in storage
    assert 'id="micBtn"' not in storage
    assert 'id="wakeBtn"' not in storage
    assert "<h2>Ask Magpie</h2>" in html
    assert "<p>Find and put away across all storage</p>" in html
    assert 'data-quick-action="find"' not in html
    assert 'data-quick-action="put-away"' not in html
    assert 'data-quick-action="learn-rule"' in html
    assert ">Teach Magpie</button>" in html
    assert 'class="quick-link" href="/storage?view=rules">Rules</a>' in html
    assert 'data-quick-action="rules"' not in html
    assert 'id="sortPhotoBtn"' in html
    assert 'applyQuickCommand("sort-photo")' in html
    assert "assistant-panel product-surface" in html
    assert "min-height:calc(100dvh - 94px)" in html
    assert "grid-template-rows:minmax(120px,1fr) auto auto auto auto" in html
    assert ".assistant-panel .product-surface-title h2 { font-size:26px; }" in html
    assert ".assistant-panel .assistant-meta-actions { display:contents !important; }" in html
    assert ".command-box {" in html
    assert "box-shadow:var(--shadow)" in html
    assert "grid-template-columns: repeat(14, minmax(18px, 1fr))" in html
    assert "const TYPE_EMOJI" not in html
    assert "⚑" not in html
    assert 'window.MagpieProductShell?.setReviewCount(reviewItems.length)' in html
    assert 'id="reviewTab"' not in html
    assert 'class="product-bottom-nav"' in html
    assert '.stack { order: 1;' in html
    assert "max-height: calc(100dvh - 94px)" in html
    assert ".cabinet-panel { position: static; order: 2; max-height: none; overflow-x: auto; }" in html
    assert '.cabinet-panel .grid.storage-start-grid { min-width: 0; }' in html
    assert "grid-template-columns: minmax(0, var(--command-max));" in html
    assert "body.advanced-mode main {" in html
    assert 'class="panel cabinet-panel product-surface advanced-only" id="cabinetTools"' in html
    assert 'grid.classList.add("storage-start-grid")' in html
    assert 'grid.classList.remove("storage-start-grid")' in html
    assert 'id="advancedToggle" type="button" aria-controls="cabinetTools" aria-expanded="false">Cabinet tools</button>' in html
    assert 'class="panel selection-panel" id="selectionPanel" hidden' in html
    assert 'class="secondary advanced-only-inline">Clear Inventory</button>' in html
    assert 'body.advanced-mode .advanced-only' in html
    assert 'let advancedMode = window.location.pathname === "/cabinet"' in html
    assert 'new URLSearchParams(window.location.search).get("advanced") === "1"' in html
    assert 'localStorage.getItem("magpie.advanced.v1")' not in html
    assert 'localStorage.setItem("magpie.advanced.v1"' not in html
    assert 'button.textContent = advancedMode ? "Close tools" : "Cabinet tools";' in html
    assert 'button.setAttribute("aria-expanded", advancedMode ? "true" : "false");' in html
    assert "function setSelectionPanelVisible(visible)" in html
    assert "if (!kioskMode) return;" in html
    assert "if (kioskMode) {\n        startScreenHeartbeat();\n        startKioskStateChannel();\n      }" in html
    load_body = html.split("async function load()", 1)[1].split("function renderGrid()", 1)[0]
    assert "loadDemoReadiness" not in load_body


def test_assistant_empty_state_does_not_duplicate_composer_actions() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="assistant-empty" data-assistant-empty' in html
    assert 'data-empty-action=' not in html
    assert html.count('id="command"') == 1
    assert html.count('id="sortPhotoBtn"') == 1
    assert 'log.querySelector("[data-assistant-empty]")?.remove()' in html
    assert "function clearChat()" in html
    assert "if (pendingPlan || commandBusy)" in html
    assert "Confirm or cancel the current proposal before clearing." in html
    assert 'class="muted advanced-only-inline" id="assistantMeta"' in html
    assert ".product-header > div:nth-child(2) { grid-column:2; grid-row:1;" in html
    assert ".product-header:has(> .magpie-auth-signout) > div:nth-child(2)" in html
    assert "#advancedToggle { display:none; }" in html


def test_ui_renders_forget_rule_plan_label() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 's.tool === "forget_rule"' in html
    assert "Forget ${escapeHtml(a.rule_type" in html
    assert 's.tool === "update_rule"' in html
    assert 's.tool === "update_size_rule"' in html
    assert "Update placement rule #" in html
    assert "Update size rule #" in html
    assert 's.tool === "update_item"' in html
    assert "Update item:" in html
    assert 's.tool === "learn_alias"' in html
    assert "Learn alias:" in html
    assert 's.tool === "print_label"' in html
    assert "Print label for" in html


def test_location_detail_links_printable_label_sheet() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'href="/locations/${encodeURIComponent(id)}/labels.html"' in html
    assert 'href="/locations/${encodeURIComponent(id)}/labels.pdf"' in html
    assert "Label Sheet" in html
    assert "Label PDF" in html
    assert "button-link secondary" in html


def test_storage_place_tools_stay_above_large_child_lists() -> None:
    html = STORAGE_HTML.read_text(encoding="utf-8")

    menu_start = html.index('<div class="place-utility-actions"')
    inside_list = html.index("${inside}", menu_start)
    assert menu_start < html.index('id="openLabelDialog"', menu_start) < inside_list
    assert menu_start < html.index('id="addLocationPhoto"', menu_start) < inside_list
    assert menu_start < html.index('id="locationHistory"', menu_start) < inside_list
    assert 'class="location-tools"' not in html


def test_inventory_rows_link_item_qr_and_pdf_labels() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'href="${scanUrl("i", item.item_id)}"' in html
    assert 'href="/items/${encodeURIComponent(item.item_id)}/label.pdf"' in html
    assert 'href="${scanUrl("i", itemId)}"' in html
    assert 'href="/items/${encodeURIComponent(itemId)}/label.pdf"' in html
    assert 'href="${scanUrl("l", id)}"' in html
    assert "Item QR" in html
    assert "Item PDF" in html


def test_storage_inventory_opens_an_exact_item_deep_link() -> None:
    storage = (STATIC_DIR / "storage.html").read_text(encoding="utf-8")

    assert 'const initialItemId = Number(initialParams.get("item") || 0)' in storage
    assert "async function openInitialInventoryItem()" in storage
    assert 'api(`/items/${encodeURIComponent(initialItemId)}/history?limit=1`)' in storage
    assert 'if (itemId) params.set("item_id", String(itemId))' in storage
    assert "await loadInventory({itemId:itemHistory.item_id || initialItemId})" in storage
    assert 'url.searchParams.delete("item")' in storage
    assert "openInventoryDetail(group.item_id)" in storage
    assert "await openInitialInventoryItem()" in storage


def test_inventory_rows_can_show_item_history_inline() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function itemHistoryButton(itemId)" in html
    assert 'data-item-history="${escapeHtml(itemId)}"' in html
    assert "function showItemHistory(triggerBtn, itemId)" in html
    assert 'api(`/items/${encodeURIComponent(itemId)}/history`)' in html
    assert "function renderItemHistoryEvents(events)" in html
    assert "Recent history" in html
    assert 'button.onclick = () => showItemHistory(button, button.dataset.itemHistory)' in html


def test_location_detail_can_show_location_history_inline() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function locationHistoryButton(locationId)" in html
    assert 'data-location-history="${escapeHtml(locationId)}"' in html
    assert "function showLocationHistory(triggerBtn, locationId)" in html
    assert 'api(`/locations/${encodeURIComponent(locationId)}/history`)' in html
    assert "function renderLocationHistoryEvents(events)" in html
    assert "${locationHistoryButton(id)}" in html
    assert "showLocationHistory(b, b.dataset.locationHistory)" in html


def test_assistant_routes_place_management_to_canonical_storage_view() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'const params = new URLSearchParams({view:"places"})' in html
    assert 'params.set("location", locationId)' in html
    assert 'params.set("create", createMode)' in html
    assert 'window.location.href = `/storage?${params}`' in html
    assert 'addInsideBtn.onclick = () => openStorageManager(id, "inside")' in html
    assert 'querySelector("[data-edit-location]").onclick = () => openStorageManager(id)' in html
    assert 'querySelector("[data-manage-location]").onclick = () => openStorageManager(binId)' in html
    assert "Manage in Storage" in html
    assert "Add inside in Storage" in html


def test_storage_owns_reviewed_location_lifecycle_actions() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="addModal"' not in html
    assert "Manage places" in html
    assert 'id="locationLifecycleDialog"' in storage
    assert "function openLocationLifecycle(mode, locationId = selectedId, focusId = locationId)" in storage
    assert 'api(`/locations/${encodeURIComponent(location.id)}/removal-preview${previewSuffix}`)' in storage
    assert "Permanent removal is blocked." in storage
    assert "This permanently removes the empty place and its nested places. It cannot be undone." in storage


def test_account_manages_workspace_edge_devices_without_persisting_plaintext_tokens() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="devicesSection"' in html
    assert 'id="deviceForm"' in html
    assert 'id="deviceProfile"' in html
    assert 'id="deviceProfileSummary"' in html
    assert 'id="deviceIdPreview" role="status"' in html
    assert "const PROFILE_SCOPES" in html
    assert "const PROFILE_DETAILS" in html
    assert 'cabinet: ["commands", "screen", "voice"]' in html
    assert 'printer: ["commands", "printer"]' in html
    assert "function deviceSlug(value)" in html
    assert "function deviceProfileForScopes(scopes)" in html
    assert "function updateDeviceEnrollmentPreview()" in html
    assert "const deviceId = deviceSlug(byId(\"deviceId\").value)" in html
    assert "Use at least one letter or number in the device name." in html
    assert "Magpie will create a secure device ID." in html
    assert 'const role = PROFILE_DETAILS[profile]?.label || "Custom device"' in html
    assert "Granted:" in html
    assert "Hardware report:" in html
    assert "Live services:" in html
    assert "scopes:PROFILE_SCOPES[profile] || []" in html
    assert 'id="devicePairing" class="token-panel" hidden' in html
    assert 'id="devicePairingCommand"' in html
    assert 'id="devicePairingState" class="pairing-state" role="status"' in html
    assert 'id="devicePairingSteps" class="pairing-steps"' in html
    assert 'id="checkDeviceConnection" type="button">Check connection</button>' in html
    assert 'id="devicePairingSuccess" class="pairing-success" hidden' in html
    assert 'api("/workspace/devices")' in html
    assert 'api("/workspace/devices/pairing"' in html
    assert "let devicePairingAvailable = true" in html
    assert "data.pairing_available !== false" in html
    assert "!data.can_manage || !devicePairingAvailable" in html
    assert "result.machine_url || window.magpieAuthConfig?.edge_url || origin" in html
    assert 'api(`/workspace/devices/${encodeURIComponent(deviceId)}/pairing`' in html
    assert 'api(`/workspace/devices/${encodeURIComponent(deviceId)}`, {method:"DELETE"})' in html
    assert "showDevicePairing(result.device.device_id, result)" in html
    assert "showDevicePairing(deviceId, result)" in html
    assert 'const scopes = Array.isArray(result.device?.credential_scopes)' in html
    assert '["commands", "screen", "voice"].every' in html
    assert "scripts/connect-pi5-cabinet.sh --central-url" in html
    assert "--pairing-code ${shellQuote(result.pairing_code)}" in html
    assert "scripts/install-pi5-edge-service.sh" in html
    assert "scripts/install-pi5-voice-service.sh" in html
    assert "scripts/install-pi5-kiosk.sh" in html
    assert "Local voice and kiosk stay unchanged until the central inventory is verified." in html
    assert "--activate-central-experience" in html
    assert "navigator.clipboard.writeText" in html
    assert "showDeviceToken" not in html
    assert "Static fallback active" in html
    assert "device.connection_state" in html
    assert "const EDGE_SERVICE_LABELS" in html
    assert "device.service_readiness || {}" in html
    assert "device.reported_capabilities || device.capabilities || []" in html
    assert "device.runtime_capabilities || []" in html
    assert "Hardware report:" in html
    assert "Live services:" in html
    assert 'Ready: ${readyServices.join(", ")}' in html
    assert 'Waiting: ${waitingServices.join(", ")}' in html
    assert "beginDeviceConnectionWatch(result.device.device_id)" in html
    assert "beginDeviceConnectionWatch(deviceId)" in html
    assert "deviceConnectionWatch = setInterval(check, 3000)" in html
    assert "function setDevicePairingProgress(step, state, tone = \"\")" in html
    assert "async function checkDeviceConnection(deviceId = activePairingDeviceId)" in html
    assert "function completeDevicePairing(device)" in html
    assert "if (device?.connected && !device.pairing_pending)" in html
    assert "if (device?.pairing_pending)" in html
    assert 'byId("devicePairingCommand").value = ""' in html
    assert 'setDevicePairingProgress(3, "Connected", "connected")' in html
    assert "Run the command again or check the device service and network." in html


def test_account_manages_revocable_workspace_agent_connections() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="agentConnectionsSection"' in html
    assert 'id="agentConnectionForm"' in html
    assert 'id="agentConnectionToken" class="token-panel" hidden' in html
    assert 'id="agentConnectionDetails"' in html
    assert 'api("/workspace/agent-credentials")' in html
    assert 'api("/workspace/agent-credentials", {method:"POST"' in html
    assert 'api(`/workspace/agent-credentials/${encodeURIComponent(credentialId)}`' in html
    assert "Authorization: Bearer ${result.token}" in html
    assert "This token is shown once." in html
    assert 'data-revoke-agent="${escapeHtml(credential.id)}"' in html


def test_ui_exposes_inventory_import_export_controls() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel data-portability advanced-only"' in html
    assert 'href="/export/inventory.json"' in html
    assert 'href="/export/inventory.csv"' in html
    assert 'id="jsonImportText"' in html
    assert 'id="jsonDryRunBtn"' in html
    assert 'id="jsonImportBtn"' in html
    assert 'id="csvImportText"' in html
    assert 'id="csvDefaultLocation"' in html
    assert 'id="csvDryRunBtn"' in html
    assert 'id="csvImportBtn"' in html
    assert 'function runJsonImport(dryRun)' in html
    assert 'function runCsvImport(dryRun)' in html
    assert '"/import/inventory.json"' in html
    assert '"/import/inventory.csv"' in html
    assert "dry_run: dryRun" in html
    assert 'byId("jsonDryRunBtn").onclick = () => runJsonImport(true)' in html
    assert 'byId("jsonImportBtn").onclick = () => runJsonImport(false)' in html
    assert 'byId("csvDryRunBtn").onclick = () => runCsvImport(true)' in html
    assert 'byId("csvImportBtn").onclick = () => runCsvImport(false)' in html


def test_account_exposes_reviewed_workspace_backup_and_restore() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="dataSection"' in html
    assert 'href="/export/inventory.json"' in html
    assert 'download="magpie-backup.json"' in html
    assert 'href="/export/inventory.pdf"' in html
    assert 'download="magpie-inventory.pdf"' in html
    assert 'id="backupFileLabel" hidden' in html
    assert 'id="backupFile" type="file" accept="application/json,.json"' in html
    assert "async function previewBackup(event)" in html
    assert 'body:JSON.stringify({snapshot, dry_run:true})' in html
    assert 'id="backupPreview" class="backup-preview" hidden' in html
    assert "async function restoreBackup()" in html
    assert 'body:JSON.stringify({snapshot:backupSnapshot, dry_run:false})' in html
    assert "${preview.photos || 0} photos" in html
    assert "${result.photos || 0} photos" in html
    assert 'canRestoreBackup = identity.role !== "viewer"' in html
    assert 'byId("backupFileLabel").hidden = false' in html
    assert 'byId("workspaceSection").innerHTML = \'<div class="local-state"' in html
    assert 'byId("main").innerHTML' not in html


def test_account_exposes_reviewed_csv_inventory_import() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="csvFileLabel" hidden' in html
    assert 'id="csvFile" type="file" accept="text/csv,.csv"' in html
    assert 'id="csvPreview" class="import-preview" hidden' in html
    assert 'id="csvDefaultLocationLabel" hidden' in html
    assert 'id="csvDefaultLocation"' in html
    assert '<script src="/location-display.js"></script>' in html
    assert "async function previewCsv(event)" in html
    assert 'body:JSON.stringify({csv_text:csvText, dry_run:true})' in html
    assert "preview.requires_default_location" in html
    assert 'const data = await api("/locations")' in html
    assert "location.active && location.accepts_items" in html
    assert "const locationsById = new Map" in html
    assert 'MagpieLocationDisplay.pathText(path, locations, " › ")' in html
    assert "csvImportDefaultLocationId = select.value" in html
    assert "async function importCsv()" in html
    assert "body.default_location_id = csvImportDefaultLocationId" in html
    assert "body:JSON.stringify(body)" in html
    assert 'byId("csvFileLabel").hidden = !canRestoreBackup' in html
    assert 'byId("csvFileLabel").hidden = false' in html


def test_ui_exposes_item_size_fields_in_manual_and_edit_forms() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="itemSize"' in html
    assert 'id="itemDimensions"' in html
    assert 'id="editSize"' in html
    assert 'id="editDimensions"' in html
    assert 'function parseDimensionText(value)' in html
    assert 'function dimensionsToText(dimensions)' in html
    assert "thread_diameter_mm" in html
    assert '`${thread} x ${dimensions.length_mm} mm`' in html
    assert 'function sizeOptions(selected)' in html
    assert 'id="suggestItemSizeBtn"' in html
    assert 'id="itemSizeReason"' in html
    assert 'function suggestItemSize()' in html
    assert '"/placement/size"' in html
    assert 'size_class: byId("itemSize").value' in html
    assert 'dimensions: parseDimensionText(byId("itemDimensions").value)' in html
    assert 'size_class: byId("editSize").value' in html
    assert 'dimensions: parseDimensionText(byId("editDimensions").value)' in html
    assert '"itemSize", "itemDimensions"' in html
    assert 'byId("suggestItemSizeBtn").onclick = suggestItemSize' in html
    assert 'href="/measurement/calibration-grid.pdf"' in html
    assert 'api("/measurement/photo"' in html
    assert "pendingPhotos.map((photo) => photo.id)" in html
    assert 'id="editWidth"' in storage
    assert 'id="editDepth"' in storage
    assert 'id="editHeight"' in storage
    assert 'internal_dimensions:dimensionPayload("edit")' in storage


def test_ui_exposes_item_home_visibility_and_quick_update() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function itemLocationMetaHtml(item)" in html
    assert "home_location_id" in html
    assert "last_seen_location_id" in html
    assert "function setHomeButton(item, locationId)" in html
    assert 'data-set-home-item="${escapeHtml(item.item_id)}"' in html
    assert 'data-set-home-location="${escapeHtml(locationId)}"' in html
    assert "function setItemHome(itemId, locationId, button)" in html
    assert '`/inventory/${encodeURIComponent(itemId)}/home`' in html
    assert "body: JSON.stringify({location_id: locationId})" in html
    assert "Set home to ${locationId}. Undo available." in html


def test_ui_exposes_learning_examples_review_panel() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel learning-review advanced-only"' in html
    assert "body.advanced-mode .assistant-panel { position:static; }" in html
    assert "body.advanced-mode main { grid-template-columns: minmax(0, 1fr); }" in html
    assert 'id="learningRefreshBtn"' in html
    assert 'id="learningApproveVisibleBtn"' in html
    assert 'id="learningStatusFilter"' in html
    assert '<option value="pending" selected>Pending</option>' in html
    assert 'id="learningKindFilter"' in html
    assert '<option value="manual">Manual</option>' in html
    assert '<option value="review_approved">Review approved</option>' in html
    assert 'id="learningIncludeRejected"' in html
    assert 'id="learningSummary"' in html
    assert 'id="learningEvalCoverage"' in html
    assert 'id="learningExamples"' in html
    assert 'function loadLearningExamples()' in html
    assert 'function renderLearningExamples(examples)' in html
    assert 'function renderLearningEvalCoverage(replay)' in html
    assert 'function promoteLearningExample(exampleId)' in html
    assert 'function approveLearningExample(exampleId)' in html
    assert 'function approveVisibleLearningExamples()' in html
    assert 'function rejectLearningExample(exampleId)' in html
    assert 'visibleLearningExampleIds' in html
    assert 'new URLSearchParams({limit: "20"})' in html
    assert 'params.set("review_status", statusFilter)' in html
    assert 'params.set("kind", kindFilter)' in html
    assert 'params.set("include_rejected", "true")' in html
    assert 'api("/assistant/learning/summary")' in html
    assert 'api("/assistant/evals/replay", {' in html
    assert "JSON.stringify({limit: 25, minimum_cases: 1, run_model: false})" in html
    assert "replay.scenario_coverage || {}" in html
    assert "Approve only examples you trust as release evidence." in html
    assert 'summaryResponse.review_counts' in html
    assert 'summaryResponse.kind_counts' in html
    assert "Object.entries(kindCounts)" in html
    assert "action.approved_review" in html
    assert 'return ["accepted", item, location].filter(Boolean).join(" ")' in html
    assert 'return ["corrected", before, after ? `to ${after}` : ""].filter(Boolean).join(" ")' in html
    assert '`/assistant/learning/examples?${params.toString()}`' in html
    assert 'data-promote-example="${escapeHtml(example.id)}"' in html
    assert '`/assistant/learning/examples/${encodeURIComponent(exampleId)}/promote`' in html
    assert 'data-approve-example="${escapeHtml(example.id)}"' in html
    assert '`/assistant/learning/examples/${encodeURIComponent(exampleId)}/approve`' in html
    assert '"/assistant/learning/examples/bulk-approve"' in html
    assert 'JSON.stringify({example_ids: ids})' in html
    assert 'data-reject-example="${escapeHtml(example.id)}"' in html
    assert "Forget rule" in html
    assert 'response.rolled_back?.length ? "Rule forgotten"' in html
    assert '`/assistant/learning/examples/${encodeURIComponent(exampleId)}`' in html
    assert 'byId("learningRefreshBtn").onclick = loadLearningExamples' in html
    assert 'byId("learningApproveVisibleBtn").onclick = approveVisibleLearningExamples' in html
    assert 'byId("learningStatusFilter").onchange = loadLearningExamples' in html
    assert 'byId("learningKindFilter").onchange = loadLearningExamples' in html
    assert 'byId("learningIncludeRejected").onchange = loadLearningExamples' in html


def test_ui_allows_reparenting_locations_from_edit_panel() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="moveLocationDialog"' in storage
    assert 'id="moveLocationSearch"' in storage
    assert 'id="moveLocationDestinations"' in storage
    assert 'id="moveLocationConfirmation"' in storage
    assert 'id="confirmLocationMove"' in storage
    assert 'id="changeLocationMoveDestination"' in storage
    assert "function renderMoveLocationDestinations()" in storage
    assert "allowTopLevel:true" in storage
    assert "onSelect:reviewLocationMove" in storage
    assert "async function reviewLocationMove(newParentId)" in storage
    assert "/move-preview" in storage
    assert "function moveLocationImpactText(preview)" in storage
    assert "Inventory, rules, history, labels, and hardware assignments stay attached." in storage
    assert "async function moveLocationTo(newParentId)" in storage
    assert '/move`, {' in storage
    assert "new_parent_id:newParentId || null" in storage


def test_agent_trace_cards_show_rules_and_placement_reasons() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function renderAgentTraceCards" in html
    assert 'entry.tool === "list_rules"' in html
    assert 'entry.tool === "list_review_items"' in html
    assert 'entry.tool === "get_location"' in html
    assert 'entry.tool === "explain_location_choice"' in html
    assert 'entry.tool === "preview_label_sheet"' in html
    assert 'entry.tool === "list_storage_roots"' in html
    assert "function renderStorageRootsTraceCard" in html
    assert 'entry.tool === "preview_label_batch"' in html
    assert 'href.replace(/labels\\.html$/, "labels.pdf")' in html
    assert "Export PDF" in html
    assert 'entry.tool === "plan_reorganization"' in html
    assert "Learned rules" in html
    assert "renderRuleRow" in html
    assert "renderRuleConflict" in html
    assert "function tracePathLabel(path, fallback" in html
    assert "rule.target_path_label || tracePathLabel(rule.target_path" in html
    assert 'rule.target_archived ? " (archived)"' in html
    assert "conflict.target_path_labels" in html
    assert "renderLocationTraceCard" in html
    assert "renderReviewTraceCard" in html
    assert "need review" in html
    assert "Location not found" in html
    assert "item${contents.length === 1 ? \"\" : \"s\"} inside" in html
    assert "Conflicts to review" in html
    assert "Placement reasons" in html
    assert "Open Label Sheet" in html
    assert "renderLabelSheetTraceCard" in html
    assert "renderLabelBatchTraceCard" in html
    assert "Label batch" in html
    assert "missing_location_ids" in html
    assert "renderReorganizationTraceCard" in html
    assert "Reorganization plan" in html
    assert "trace-cards" in html
    assert "else await runAgentCommand(text, {source: requestSource, throwOnError: options.throwOnError});" in html


def test_rule_cards_have_confirm_gated_forget_and_edit_actions() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-forget-rule="${escapeHtml(String(rule.id || ""))}"' in html
    assert 'data-edit-rule="${escapeHtml(String(rule.id || ""))}"' in html
    assert 'data-rule-terms="${escapeHtml(terms)}"' in html
    assert 'data-rule-target="${escapeHtml(targetValue)}"' in html
    assert "function attachRuleCardActions" in html
    assert "function proposeForgetRule" in html
    assert 'tool: "forget_rule"' in html
    assert "renderPendingPlan(plan" in html
    assert "function openRuleEditForm" in html
    assert "function proposeRuleUpdateFromForm" in html
    assert "function renderRuleLocationTargetControl" in html
    assert "function bindRuleLocationTargetControl" in html
    assert "function renderRuleLocationTargets" in html
    assert 'data-rule-location-search aria-label="Find target location"' in html
    assert 'data-rule-location-destination="${escapeHtml(target.id)}"' in html
    assert "Choose a known target location" in html
    assert "data-rule-edit-text" in html
    assert "data-rule-edit-terms" in html
    assert "data-rule-edit-target" in html
    assert 'ruleType === "size" ? "update_size_rule" : "update_rule"' in html
    assert "Preview this rule update before applying it." in html
    assert "Before: ${before}. After: ${after}." in html


def test_pending_plan_only_offers_lights_for_lightable_steps() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "const canLight = colorPlan.some(Boolean);" in html
    assert 'class="actions plan-actions product-confirmation-actions"' in html
    assert "lightAllPlanSteps(plan, colorPlan);" in html
    assert 'data-act="light-all">Light all colors' not in html
    assert "if (lightAll) lightAll.onclick = () => lightAllPlanSteps(plan, colorPlan);" in html
    assert "if (canLight) lightAllPlanSteps(plan, colorPlan);" in html
    assert "${canLight ? '<span class=\"muted\">Color key remains active for putting parts away.</span>' : \"\"}" in html
    assert "${canLight ? '<div class=\"actions\"><button class=\"secondary\" data-act=\"light-all\">Relight all colors</button></div>' : \"\"}" in html
    assert "setStatus(`Saved ${confirmedPlan.length} change${confirmedPlan.length === 1 ? \"\" : \"s\"}`);" in html
    assert 'const destination = a.new_parent_id ? escapeHtml(a.new_parent_id) : "top level";' in html


def test_desktop_pending_plan_offers_conversational_revision() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'confirmationActionLabel(presentation, "change", "Change")' in html
    assert 'data-act="change">${escapeHtml(changeLabel)}</button>' in html
    assert (
        'node.querySelector("[data-act=\'change\']").onclick = () => '
        "beginPendingPlanRevision(node);"
    ) in html
    assert "function beginPendingPlanRevision(node = pendingPlanNode)" in html
    assert 'input.placeholder = "What should Magpie change?"' in html
    assert 'setStatus("Tell Magpie what to change")' in html


def test_confirm_refresh_does_not_reselect_an_archived_root() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    confirm_start = html.index("async function confirmPlan(node)")
    confirm_end = html.index("async function cancelPendingPlan(node)", confirm_start)
    confirm_source = html[confirm_start:confirm_end]

    assert "await loadContainers(); await loadReviewQueue();" in confirm_source
    assert "selectContainer(currentContainer)" not in confirm_source


def test_assistant_ui_prioritizes_fast_command_entry() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel assistant-panel product-surface"' in html
    assert 'data-quick-action="find"' not in html
    assert 'data-quick-action="put-away"' not in html
    assert 'href="/storage?view=rules">Rules</a>' in html
    assert 'function applyQuickCommand' in html
    assert 'event.key === "Enter" && !event.shiftKey' in html
    assert 'button.textContent = isBusy ? "Working" : "Ask"' in html


def test_assistant_supports_magpie_wake_keyword_mode() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="wakeBtn"' in html
    assert 'function toggleWake()' in html
    assert 'function handleWakeTranscript' in html
    assert 'function submitWakeCommand' in html
    assert 'function setupAutoWake()' in html
    assert 'recognition.continuous = mode === "wake"' in html
    assert 'params.get("wake") !== "1"' in html
    assert "Say 'Magpie,' then find or put away." in html
    assert "byId(\"wakeBtn\").onclick = toggleWake" in html


def test_kiosk_uses_combined_state_channel_for_voice_commands() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "lastSeenAssistantInteractionId" in html
    assert "lastSeenVoiceUpdatedAt" in html
    assert "screenHeartbeatPoller" in html
    assert "startKioskStateChannel" in html
    assert "startScreenHeartbeat" in html
    assert 'api("/screen/heartbeat"' in html
    assert "pollKioskState" in html
    assert "renderKioskVoiceState" in html
    assert "renderKioskInteractions" in html
    assert "syncKioskPendingConfirmation" in html
    assert "res.pending_confirmation || null" in html
    assert "wait_seconds: \"20\"" in html
    assert "setInterval(loadVoiceState, 600)" not in html
    assert "setInterval(pollAssistantInteractions, 1800)" not in html
    assert "last_seen_interaction_id: lastSeenAssistantInteractionId" in html
    assert "`/kiosk/state?${query}`" in html
    assert "renderRemoteAssistantInteraction" in html
    assert "if (commandBusy) return;" in html
    assert "Number(res.interaction_id || chat.interaction_id || 0)" in html
    assert "response.voice_attempt" in html
    assert "response.voice_attempt || response.voice" in html
    assert "Voice heard" in html
    assert "voiceAttempt.stt_model" in html
    assert "adoptPutAwayPlan" in html
    assert '["/assistant/agent", "/assistant/chat", "/assistant/revise"].includes(interaction.route)' in html
    assert "Voice proposal — awaiting confirm" in html
    assert "pendingSourceInteractionId = interaction.id || null" in html
    assert 'interaction.route === "/assistant/voice/confirm"' in html
    assert 'interaction.route === "/assistant/voice/cancel"' in html
    assert 'interaction.route === "/assistant/undo"' in html


def test_dedicated_kiosk_is_a_hands_off_persistent_display() -> None:
    html = KIOSK_HTML.read_text(encoding="utf-8")

    assert "<title>Magpie Kiosk</title>" in html
    assert '<body class="product-app" data-state="starting">' in html
    assert "product-brand kiosk-brand" in html
    assert "product-mark kiosk-product-mark" in html
    assert "--state-accent: var(--accent);" in html
    assert 'body[data-state="listening"] { --state-accent: var(--blue); }' in html
    assert 'body[data-state="setup"] { --state-accent: var(--warning); }' in html
    assert "color: var(--state-accent);" in html
    assert "background: #111816;" not in html
    assert 'class="shell"' in html
    assert 'id="stateLabel"' in html
    assert 'id="activeFindList"' not in html
    assert 'id="findList"' in html
    assert 'id="proposal"' in html
    assert 'id="visualHandoff"' in html
    assert 'data-handoff-url="__MAGPIE_STORAGE_HANDOFF_URL__"' in html
    assert 'src="__MAGPIE_STORAGE_HANDOFF_QR__"' in html
    assert 'id="activityList"' in html
    assert ".activity-row:only-child { grid-column: 1 / -1; }" in html
    assert 'id="confirmAction"' in html
    assert 'id="changeAction"' in html
    assert 'id="kioskChangePrompts"' in html
    assert 'id="cancelAction"' in html
    assert 'id="clearAction"' in html
    assert 'id="undoAction"' in html
    assert 'function performKioskAction(action)' in html
    assert 'function renderVisualHandoff(visible)' in html
    assert 'renderVisualHandoff(Boolean(actionOverride.handoff))' in html
    assert 'renderVisualHandoff(true)' in html
    assert 'let interactionsInitialized = false' in html
    assert 'const showFreshHandoffs = interactionsInitialized' in html
    assert 'title:"Open Storage on your phone"' in html
    assert 'handoff:true' in html
    assert 'const HANDOFF_HOLD_SECONDS = 60' in html
    assert 'holdSeconds:HANDOFF_HOLD_SECONDS' in html
    assert 'Number(override.holdSeconds || RESULT_HOLD_SECONDS) * 1000' in html
    assert 'function toggleKioskChangeOptions()' in html
    assert 'async function performKioskRevision(promptId)' in html
    assert 'api("/kiosk/actions/revise"' in html
    assert 'body:JSON.stringify({prompt_id:promptId})' in html
    assert 'confirm: {path:"/kiosk/actions/confirm"' in html
    assert 'cancel: {path:"/kiosk/actions/cancel"' in html
    assert 'clear: {path:"/kiosk/actions/clear"' in html
    assert 'undo: {path:"/kiosk/actions/undo"' in html
    assert (
        "Number(voice.updated_at || 0) > actionOverride.voiceUpdatedAt"
        in html
    )
    assert 'body[data-state="offline"], body[data-state="error"]' in html
    assert "const RESULT_HOLD_SECONDS = 20" in html
    assert 'const liveState = voice.active ? (voice.state || "waiting") : "offline"' in html
    assert "lastAge <= RESULT_HOLD_SECONDS" in html
    assert "function clearActionOverride()" in html
    assert "function holdActionOverride(override)" in html
    assert "holdActionOverride({" in html
    assert "override.holdSeconds || RESULT_HOLD_SECONDS" in html
    assert (
        'setConnection(Boolean(voice.active), voice.active ? '
        '"Voice connected" : "Voice offline")'
        in html
    )
    assert "The storage service is online, but the Pi voice listener stopped checking in." in html
    assert 'id="voiceBuildLabel"' in html
    assert "function compactRuntimeLabel(label)" in html
    assert "compactRuntimeLabel(voice.runtime_version)" in html
    assert '`Service ${compactRuntimeLabel(buildLabel)}`' in html
    assert 'voice.device_id ? ` · ${voice.device_id}`' in html
    assert "function voiceCapabilityLabel(voice)" in html
    assert 'parts.push("local hearing")' in html
    assert 'parts.push("speech unavailable")' in html
    assert 'aria-live="assertive"' in html
    assert 'api(`/kiosk/state?${query}`)' in html
    assert "if (state.reload_required && !reloadRequested)" in html
    assert 'setConnection(false, "Updating Magpie")' in html
    assert "window.location.reload()" in html
    assert 'api("/screen/heartbeat"' in html
    assert 'wait_seconds: "20"' in html
    assert 'screen_id: "cabinet-kiosk"' in html
    assert "screen_url: window.location.href" in html
    assert "last_seen_interaction_id: String(lastInteractionId)" in html
    assert "setInterval(heartbeat" not in html
    assert "if (elapsed < 250)" in html
    assert "renderFinds(voice.active_finds || [])" in html
    assert "let latestStorage = {}" in html
    assert "function storageSetupPresentation(voice)" in html
    assert "latestStorage.put_away_ready !== false" in html
    assert 'if (!["waiting", "starting", "offline"].includes(voiceState)) return null' in html
    assert 'title: "Set up storage"' in html
    assert 'title: "Add a destination"' in html
    assert "Open Magpie Storage on a phone or computer" in html
    assert "latestStorage = state.storage || {}" in html
    assert "const FIND_COLOR_NAMES = new Map([" in html
    assert "function renderActiveFindFocus(rows)" in html
    assert '![\"waiting\", \"offline\"].includes(document.body.dataset.state)' in html
    assert 'byId("title").textContent = rows.length === 1 ? first.name' in html
    assert "Use the color key and storage paths in Active locations." in html
    assert "renderActiveFindFocus(rows);" in html
    assert "let sharedPendingConfirmation = false" in html
    assert "let sharedPendingInteractionId = 0" in html
    assert "confirmation_interaction_id: String(sharedPendingInteractionId || 0)" in html
    assert "function syncConfirmationState(confirmationState)" in html
    assert "syncConfirmationState(state.confirmation_state || null)" in html
    assert 'title: replaced ? "Proposal replaced" : "Proposal expired"' in html
    assert "That storage proposal timed out." in html
    assert 'id="undoAction" type="button" hidden' in html
    assert 'id="findsSection"' in html
    assert 'interaction.confirmable !== false' in html
    assert 'interaction.voice_confirmable === true' in html
    assert "sharedPendingVisible" in html
    assert "&& sharedPendingPresentation" in html
    assert 'interaction.status === "expired"' in html
    assert 'const action = response.action || response.decision?.action || ""' in html
    assert '["place_configuration_ui_required", "voice_scope_ui_required"].includes(action)' in html
    assert 'return {title: "Use Storage"' in html
    assert 'byId("findsSection").hidden = rows.length === 0' in html
    assert "let sharedPendingPresentation = null" in html
    assert "voiceAwaitingConfirmation || (" in html
    assert 'const planRoute = ["/assistant/agent", "/assistant/chat", "/assistant/revise", "/mcp/propose_plan"].includes(route)' in html
    assert "function renderProposal(presentation)" in html
    assert 'class="proposal product-confirmation-body"' in html
    assert 'class="kiosk-actions product-confirmation-actions"' in html
    assert '"proposal-row product-confirmation-row"' in html
    assert '"proposal-path product-confirmation-path"' in html
    assert "MagpieConfirmation.actionLabel(" in html
    assert 'Say \\"Magpie\\" and ask to find or put away.' in html
    assert "function proposalActivityDetail(plan, command)" in html
    assert 'route.startsWith("/locations/")' in html
    assert "if (applied.stored) {" in html
    assert 'if (action === "put_away") return {title: "No destination"' in html
    assert 'detail: destinationLabel || proposalActivityDetail(response.plan, command)' in html
    assert 'sharedPendingPresentation.title || "Ready to confirm"' in html
    assert 'sharedPendingPresentation.voice_instruction || "Say yes to confirm or no to cancel."' in html
    assert 'const liveVoiceState = voice.active ? (voice.state || "waiting") : "offline"' in html
    assert '["waiting", "starting", "offline"].includes(liveVoiceState)' in html
    assert 'const voiceReady = liveVoiceState === "waiting"' in html
    assert '"Confirmation paused"' in html
    assert '"Your request is ready. Voice confirmation will resume when the Pi listener reconnects."' in html
    assert "window.innerHeight <= 760 ? 2 : 4" in html
    assert '"proposal-more"' in html
    assert "response.confirmation || {}" in html
    assert html.count(
        'command: response.original_command || interaction.command || "Magpie request"'
    ) == 2
    assert "@media (max-height: 760px) and (min-width: 701px)" in html
    assert "@media (max-height: 540px) and (min-width: 701px)" in html
    assert ".shell { grid-template-rows: 64px minmax(0, 1fr); }" in html
    assert ".kiosk-actions { min-height: 48px; flex-wrap: nowrap; gap: 8px; }" in html
    assert "body.change-options-visible .proposal" in html
    assert 'document.body.classList.toggle("change-options-visible", !container.hidden)' in html
    assert ".activity { display: none; }" in html
    assert "@media (max-width: 700px), (orientation: portrait)" in html
    assert 'byId("transcript").textContent = `Request: ${sharedPendingPresentation.command}`' in html
    assert '["confirm", "cancel"].includes(action) && succeeded' in html
    assert "last_seen_interaction_id: lastInteractionId" in html
    assert "localStorage" not in html
    assert "Authorization" not in html
    assert "MAGPIE_EDGE_TOKEN" not in html


def test_main_service_exposes_dedicated_kiosk_route_and_capability() -> None:
    html = KIOSK_HTML.read_text(encoding="utf-8")
    assert "No active locations." in html

    api_source = Path("src/searchable_storage_led/api.py").read_text(encoding="utf-8")
    assert '@app.get("/kiosk", response_class=HTMLResponse)' in api_source
    assert '"kiosk_display": True' in api_source
    assert '"kiosk_touch_actions": True' in api_source
    assert '"kiosk_display": "/kiosk"' in api_source
    assert '"kiosk_confirm": "/kiosk/actions/confirm"' in api_source
    assert '"kiosk_revise": "/kiosk/actions/revise"' in api_source
    assert '"kiosk_cancel": "/kiosk/actions/cancel"' in api_source
    assert '"kiosk_undo": "/kiosk/actions/undo"' in api_source
    assert '"kiosk_clear": "/kiosk/actions/clear"' in api_source
    assert "APP_SHELL_PATHS = (" in api_source
    assert '    "/mobile",' in api_source


def test_mobile_client_is_agent_led_and_supports_daily_storage_workflows() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "<title>Magpie</title>" in html
    assert '<script src="/auth.js"></script>' in html
    assert 'id="command"' in html
    assert 'data-action="find" data-mode-action aria-pressed="false"' in html
    assert 'data-action="put-away" data-mode-action aria-pressed="false"' in html
    assert 'id="sortPhotoButton"' in html
    assert 'data-action="sort-photo"' in html
    assert 'data-action="learn-rule" data-mode-action aria-pressed="false"' in html
    assert ">Teach Magpie</button>" in html
    assert '<a href="/storage?view=rules">Rules</a>' in html
    assert 'data-action="rules"' not in html
    assert 'grid-template-columns:repeat(4,minmax(0,1fr))' in html
    assert 'if (["find","put-away","learn-rule"].includes(action) && !byId("command").value.trim())' in html
    assert 'if (action === "find") return clean ?' in html
    assert 'if (action === "put-away") return clean ?' in html
    assert 'if (action === "learn-rule") return clean;' in html
    assert 'What storage rules do you know about ${clean}?' in html
    assert "function quickActionPrompt(action)" in html
    assert "setComposerStatus(quickActionPrompt(action))" in html
    assert "function quickActionPlaceholder(action)" in html
    assert "function syncConversationMode()" in html
    assert 'document.querySelectorAll("[data-mode-action]")' in html
    assert 'button.setAttribute("aria-pressed", String(active))' in html
    assert "function setConversationMode(action = \"\")" in html
    assert 'if (conversationMode === action)' in html
    assert 'setConversationMode("")' in html
    assert 'if (["find","put-away","learn-rule"].includes(requestAction)) setConversationMode("")' in html
    assert 'if (["find","put-away","learn-rule"].includes(action)) setConversationMode(action)' in html
    assert '.quick-actions button.active {' in html
    assert 'root.querySelectorAll("[data-action]")' in html
    assert "function bindActionButtons(root = document)" in html
    assert 'id="newConversation"' in html
    assert "function generalWelcomeHtml()" in html
    assert "function renderGeneralWelcome()" in html
    assert "async function startNewConversation()" in html
    assert 'setComposerStatus("Confirm or cancel the current proposal first")' in html
    assert "renderGeneralWelcome();" in html
    assert 'let reviewsCollapsed = window.matchMedia("(max-width:680px)").matches;' in html
    assert "reviewsCollapsed = true;" in html
    assert "renderReviewQueue();" in html
    assert "await renderWorkspaceWelcome();" in html
    assert "byId(\"newConversation\").onclick = startNewConversation" in html
    assert 'byId("newConversation").disabled = value' in html
    assert 'class="welcome-launcher" type="button" data-action="find"' in html
    assert 'class="welcome-launcher" type="button" data-action="put-away"' in html
    assert 'class="welcome-launcher" type="button" data-action="sort-photo"' in html
    assert 'title="Speak a find, put-away, or confirmation"' in html
    assert 'capture="environment"' in html
    assert 'api("/photos"' in html
    assert 'api("/assistant/chat"' in html
    assert 'const requestMode = "auto"' in html
    assert 'window.location.replace(`/storage?view=places&onboarding=${onboarding}`)' in html
    assert "source:requestSource" in html
    assert "function friendlyError(error" in html
    assert '!route.startsWith("/assistant/") && !route.includes("quick-put")' in html
    assert 'if (route.includes("quick-put")) {' in html
    assert "response.destination?.path || []" in html
    assert "const currentPlacements = (currentItem?.placements || [])" in html
    assert "Currently in ${destination} · Originally stored in ${originalDestination}" in html
    assert "photoCount === 1 ? \"\" : \"s\"" in html
    assert 'api("/assistant/confirm"' in html
    assert "result.confirmation_interaction_id" in html
    assert 'api("/assistant/cancel"' in html
    assert 'confirmationActionLabel(presentation, "change", "Change")' in html
    assert 'data-change>${escapeHtml(changeLabel)}</button>' in html
    assert 'byId("welcome")?.remove();' in html
    assert "function beginPlanRevision(node = null)" in html
    assert 'async function submitRevision(source = "mobile")' in html
    assert 'api("/assistant/revise"' in html
    assert "JSON.stringify({text:followup, source})" in html
    assert '"Revise the pending storage plan."' not in html
    assert "const originalPhotoIds = [...pendingPlanPhotoIds];" in html
    assert "renderPendingPlan(result.plan, result.message || \"Updated proposal.\", result.interaction_id, revisedCommand, originalPhotoIds, result.confirmation);" in html
    assert "function confirmationRow(presentation, index)" in html
    assert "function planLightRows(plan, presentation = null)" in html
    assert "function renderPendingPlan(plan, message, interactionId, command, photoIds = [], presentation = null)" in html
    assert "function confirmationPhotoEvidence(photoIds = [])" in html
    assert "photoIds.map(Number).filter((id) => Number.isInteger(id) && id > 0)" in html
    assert "visible = ids.slice(0,4)" in html
    assert 'class="plan-evidence"' in html
    assert 'src="/photos/${encodeURIComponent(id)}"' in html
    assert "will be saved with the inventory change." in html
    assert "confirmationPhotoEvidence(pendingPlanPhotoIds)" in html
    assert ".plan-evidence-photo {" in html
    assert "renderPendingPlan(result.plan, result.message, interactionId, command, photoIds, result.confirmation);" in html
    assert "renderPendingPlan(response.plan, response.message, interaction.id, interaction.command, interaction.photo_ids || [], response.confirmation);" in html
    assert "const copy = presentation?.instruction || message || \"\";" in html
    assert "function confirmationLightLabel(row)" in html
    assert "shared?.destination?.label || locationId" in html
    assert "shared && !shared.color ? null" in html
    assert "The original proposal is still pending." in html
    assert 'data-undo>Undo</button>' in html
    assert 'api("/assistant/undo", {method:"POST"})' in html
    assert "result.undo_interaction_id" in html
    assert 'interaction.status === "canceled" || ["/assistant/cancel","/assistant/voice/cancel"].includes(route)' in html
    assert "canceledSourceId === pendingSourceInteractionId" in html
    assert 'response?.decision && typeof response.decision === "object"' in html
    assert 'api("/locations/light-colors"' in html
    assert "proposalLights = result.lit" in html
    assert 'api("/clear",{method:"POST"})' in html
    assert 'window.SpeechRecognition || window.webkitSpeechRecognition' in html
    assert 'new BarcodeDetector({formats:["qr_code"]})' in html
    assert '<script src="/jsqr.min.js"></script>' in html
    assert 'typeof window.jsQR !== "function"' in html
    assert "async function decodeQrFrame(video, detector)" in html
    assert "function magpieScanDestination(raw)" in html
    assert 'new URL(`${url.pathname}${url.search}${url.hash}`, location.origin).href' in html
    assert 'scannerContext.getImageData(0, 0, width, height)' in html
    assert "function qrScannerAvailable()" in html
    assert '("BarcodeDetector" in window || typeof window.jsQR === "function")' in html
    assert 'if (!qrScannerAvailable()) {' in html
    assert 'inversionAttempts:"attemptBoth"' in html
    assert "function scannerStartupError(error)" in html
    assert 'byId("scannerStatus").textContent = scannerStartupError(error);' in html
    assert 'if (!("BarcodeDetector" in window) || !navigator.mediaDevices?.getUserMedia)' not in html
    assert 'href="/storage?view=places&onboarding=1"' in html
    assert '<script src="/product-shell.js" defer></script>' in html
    assert 'class="bottom-nav product-bottom-nav" data-product-nav' in html
    assert 'id="locationContext"' in html
    assert "function renderLocationContext()" in html
    assert "async function setLocationContext(locationId)" in html
    assert 'let currentLocationPath = ""' in html
    assert '? "Put-away destination"' in html
    assert ': "Selected place"' in html
    assert 'MagpieLocationDisplay.pathText(data.path || [], [], " › ")' in html
    assert "currentLocationPath || currentLocationId" in html
    assert "renderLocationContext();" in html
    assert "current_location_id:currentLocationId || null" in html
    assert 'params.get("prompt") || ""' in html
    assert 'params.get("location") || ""' in html
    assert 'byId("clearLocationContext").onclick = clearLocationContext' in html
    clear_context = html[
        html.index("function clearLocationContext()") :
        html.index("function generalWelcomeHtml", html.index("function clearLocationContext()"))
    ]
    assert 'setConversationMode("")' not in clear_context
    assert 'setLocationContext("")' in clear_context
    assert "conversationMode ? quickActionPrompt(conversationMode)" in clear_context
    assert "function consumeLaunchParams()" in html
    assert "Find and put away items across every storage space." in html
    assert ".assistant-page-heading { grid-area:heading; width:100%; display:flex; margin-inline:0; }" in html
    assert ".composer-shell > * { width:min(var(--command-max),100%); margin-right:auto; }" in html
    assert 'response.headers.get("X-Magpie-Interaction-ID")' in html
    assert "rememberInteraction(error.interactionId);" in html
    assert 'response.headers.get("X-Magpie-Error-Action")' in html
    assert 'response.headers.get("X-Magpie-Failure-Category")' in html
    assert "function appendRequestFailure(error, message, command)" in html
    assert 'href="/account#usageSection">Open usage</a>' in html
    assert 'data-retry-request>Try again</button>' in html
    assert 'String(result.action || "").startsWith("model_")' in html
    assert "{action:result.action, interactionId:interaction.id}" in html
    assert 'params.delete("prompt")' in html
    assert 'params.delete("action")' in html
    assert 'params.delete("resume")' in html
    initial_action = html[
        html.index("async function setupInitialAction()") :
        html.index("function bindActionButtons", html.index("async function setupInitialAction()"))
    ]
    assert 'params.delete("prompt")' not in initial_action
    assert 'params.delete("action")' not in initial_action
    assert 'params.delete("resume")' not in initial_action
    assert html.index("renderResponse(response, command, photoIds);") < html.index(
        "consumeLaunchParams();",
        html.index("renderResponse(response, command, photoIds);"),
    )
    assert "function renderAgentEvidence(trace)" in html
    assert 'entry.tool === "list_rules"' in html
    assert 'entry.tool === "explain_location_choice"' in html
    assert 'entry.tool === "list_storage_roots"' in html
    assert 'entry.tool === "get_location"' in html
    assert 'entry.tool === "preview_label_sheet"' in html
    assert 'entry.tool === "preview_label_batch"' in html
    assert "renderAgentEvidence(result.trace)" in html
    assert "Learned rules" in html
    assert "Why this location" in html
    assert "pathLabel(result) || result.location_id" in html
    assert "rule.target_path_label || pathLabel({path:rule.target_path" in html
    assert 'rule.target_archived ? " (archived)"' in html
    assert "Open label sheet" in html


def test_mobile_learned_rules_are_conversationally_editable_and_confirm_gated() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert 'data-change-rule data-rule-id="${escapeHtml(rule.id)}"' in html
    assert 'data-forget-rule data-rule-id="${escapeHtml(rule.id)}"' in html
    assert "function beginRuleChange(button)" in html
    assert "function proposeForgetRule(button)" in html
    assert "Change ${ruleType} rule #${ruleId} (${ruleText}) so " in html
    assert 'tool:"forget_rule", arguments:{rule_id:ruleId, rule_type:ruleType}' in html
    assert "renderPendingPlan(" in html
    assert 'if (step.tool === "forget_rule")' in html
    assert 'card?.querySelectorAll("[data-change-rule]")' in html
    assert 'card?.querySelectorAll("[data-forget-rule]")' in html
    assert "scroll-padding-bottom:calc(var(--dock-space) + 12px)" in html
    assert 'node.scrollIntoView({behavior:"auto", block:"nearest"});' in html
    assert "revealPendingPlan(node);" in html


def test_mobile_confirmation_receipt_surfaces_photos_recovery_and_location() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "function confirmationReceiptHtml(" in html
    assert "options.allowUndo === false" in html
    assert "result.receipt || {}" in html
    assert 'const summary = String(receipt.summary || "").trim()' in html
    assert "const resultSummary = summary ||" in html
    assert "escapeHtml(resultSummary)" in html
    assert "focus_location_id" in html
    assert 'href="/storage?view=places&location=${encodeURIComponent(focusLocationId)}"' in html
    assert "saved with the inventory record" in html
    assert "photo attachment${pending === 1" in html
    assert 'data-retry-photo' in html
    assert 'api("/photos/attach-retry"' in html
    assert "queuePhotoAttachmentRetry(retryRequest)" in html
    assert "retryQueuedPhotoAttachments()" in html
    assert 'window.addEventListener("online",retryQueuedPhotoAttachments)' in html


def test_mobile_history_hides_stale_proposals_and_replays_confirmed_receipts() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "const summarizedConfirmationInteractions = new Set();" in html
    assert "const summarizedConfirmationSources = new Set();" in html
    assert 'interaction.status === "confirmed"' in html
    assert "response.confirmed_plan?.length" in html
    assert "response.confirmation_response" in html
    assert "summarizedConfirmationInteractions.add(confirmationId)" in html
    assert "summarizedConfirmationSources.add(Number(interaction.id || 0))" in html
    assert "{allowUndo:false}" in html
    assert (
        '["superseded","expired","undone","confirmation_failed"].includes(interaction.status)'
        in html
    )
    assert "summarizedConfirmationInteractions.has(Number(interaction.id || 0))" in html
    assert (
        "summarizedConfirmationSources.has(Number(response.source_interaction_id || 0))"
        in html
    )
    assert 'String(interaction.command || "") === pendingCommand' in html
    assert 'setComposerStatus("Saved")' in html
    assert "interaction.current_items || []" in html
    assert "Currently in ${destination} · Originally stored in ${originalDestination}" in html
    assert '"No active inventory placement"' in html


def test_empty_mobile_workspace_routes_setup_to_storage_manager() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "async function renderWorkspaceWelcome()" in html
    assert 'const data = await api("/locations")' in html
    assert 'if ((data.locations || []).length) {' in html
    assert "function hasExplicitAssistantIntent()" in html
    assert '["action","prompt","resume","location"]' in html
    assert "function setStorageOnboarding(active)" in html
    assert 'classList.toggle("storage-onboarding", active)' in html
    assert "setStorageOnboarding(!hasExplicitAssistantIntent())" in html
    assert "body.storage-onboarding .composer-shell" in html
    assert "body.storage-onboarding .new-conversation" in html
    assert "setStorageOnboarding(false);" in html
    assert "function focusNewStorageSetup()" not in html
    assert 'id="startStorageSetup"' not in html
    assert "Add your first usable bin or shelf" in html
    assert ">Set up storage</a>" in html
    assert 'href="/storage?view=places&onboarding=1"' in html
    assert "await Promise.all([renderWorkspaceWelcome(),loadReviewQueue(),loadOfflineQueue()]);" in html


def test_mobile_confirmation_actions_are_revealed_above_dynamic_composer() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "function revealPendingPlan(node)" in html
    assert 'document.querySelector(".composer-shell")?.getBoundingClientRect().top' in html
    assert 'const actions = node.querySelector(".plan-actions") || node;' in html
    assert 'window.scrollBy({top:Math.ceil(overlap),behavior:"auto"})' in html
    assert 'revealPendingPlan(byId("feed")?.querySelector(".plan-card"));' in html
    assert 'node.scrollIntoView({behavior:"auto", block:"nearest"});' in html
    assert "revealPendingPlan(node);" in html


def test_mobile_composer_stays_compact_until_input_or_status_needs_space() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert '<textarea id="command" rows="1"' in html
    assert "textarea { width:100%; height:44px; min-height:44px; max-height:112px;" in html
    assert ".composer-status:empty { display:none; }" in html
    assert "function resizeCommandInput()" in html
    assert 'command.style.height = "44px"' in html
    assert "Math.min(Math.max(command.scrollHeight, 44), 112)" in html
    assert 'byId("command").addEventListener("input", resizeCommandInput)' in html
    assert 'byId("command").addEventListener("focus", resizeCommandInput)' in html
    assert "function clearCommandInput()" in html
    assert "background:color-mix(in srgb,var(--bg) 97%,transparent)" in html
    assert (
        "background:color-mix(in srgb,var(--surface) 96%,transparent)"
        in html
    )


def test_assistant_uses_the_shared_operational_visual_language() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")
    theme = THEME_CSS.read_text(encoding="utf-8")

    assert '<span class="product-context" data-product-context>Assistant</span>' in html
    assert 'class="reviews product-inline-notice"' in html
    assert 'class="offline-queue product-inline-notice"' in html
    assert ".product-inline-notice > :first-child {" in theme
    assert ".welcome h2 { max-width:18ch; margin:0; font-size:26px;" in html
    assert (
        ".quick-actions button, .quick-actions a { min-width:0; min-height:44px; "
        "display:grid; place-items:center; padding:5px 7px; border:1px solid "
        "var(--line);"
    ) in html


def test_mobile_client_surfaces_agent_review_and_correction_queue() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert 'id="reviews" class="reviews product-inline-notice" hidden' in html
    assert "<strong>Storage checks</strong>" in html
    assert 'byId("reviewCount").textContent = `${total} due`' in html
    assert "async function loadReviewQueue()" in html
    assert 'api("/review/queue?limit=20")' in html
    assert "let reviewPlaceChecks = [];" in html
    assert "const placeCards = reviewPlaceChecks.map((place) =>" in html
    assert 'class="review-kind">Physical check</div>' in html
    assert "place.action_label || \"Check contents\"" in html
    assert "&check=1`" in html
    assert "[...placeCards, ...itemCards]" in html
    assert 'data-review-approve' in html
    assert "async function approveReviewItem(itemId, binId, button)" in html
    assert 'api("/inventory/reviewed"' in html
    assert 'edit_item=${encodeURIComponent(item.item_id)}' in html
    assert '>Fix item</a>' in html
    assert "function fixReviewItem(itemId, binId)" not in html
    assert "await loadReviewQueue();" in html
    assert 'if (interaction.status === "confirmed") loadReviewQueue();' in html


def test_mobile_client_queues_only_offline_mutations_for_confirmed_replay() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert 'id="offlineQueue" class="offline-queue product-inline-notice" hidden' in html
    assert 'const OFFLINE_STORE_NAME = "pending-requests"' in html
    assert "indexedDB.open(OFFLINE_DB_NAME,3)" in html
    assert 'const PHOTO_RETRY_STORE_NAME = "photo-attachment-retries"' in html
    assert 'createObjectStore(PHOTO_RETRY_STORE_NAME,{keyPath:"key"})' in html
    assert "const OFFLINE_QUEUE_LIMIT = 25" in html
    assert "const OFFLINE_PHOTO_BYTES_LIMIT = 18 * 1024 * 1024" in html
    assert "function offlineQueueIdentity()" in html
    assert "function offlineQueueScope()" in html
    assert 'workspaceId:String(identity?.workspace_id || "")' in html
    assert 'userId:String(identity?.user_id || "")' in html
    assert 'JSON.stringify([identity.workspaceId,identity.userId])' in html
    assert "function removeUnownedHostedEntries(store)" in html
    assert 'if ((entry.scope || "local") !== "local" && !entry.owner_user_id)' in html
    assert "removeUnownedHostedEntries(request.transaction.objectStore(OFFLINE_STORE_NAME))" in html
    assert "removeUnownedHostedEntries(request.transaction.objectStore(PHOTO_RETRY_STORE_NAME))" in html
    assert '(entry.scope || "local") === offlineQueueScope()' in html
    assert "workspace_id:identity.workspaceId" in html
    assert "owner_user_id:identity.userId" in html
    assert "function offlineMutationEligible(action, command)" in html
    assert '["put-away","sort-photo"].includes(action)' in html
    assert '["put-away","sort-photo","setup","setup-new"].includes(action)' not in html
    assert "async function queueOfflineMutation" in html
    assert "async function retryOfflineRequests()" in html
    assert "if (replayingOffline || working || pendingPlan)" in html
    assert "replayingOffline = true;" in html
    assert "let requestStarted = false;" in html
    assert "client_request_id:entry.id" in html
    assert "client_request_id:clientRequestId" in html
    assert "queueOfflineMutation({id:clientRequestId" in html
    assert 'source:entry.source || "mobile"' in html
    assert 'source:source || "mobile"' in html
    assert "Saved offline. Magpie will prepare it when the connection returns" in html
    assert "function clientRequestMessage(clientRequestId)" in html
    assert "function appendInteractionUser(interaction, response = {})" in html
    assert "markClientRequestMessage(entry.id, \"Saved offline\")" in html
    assert "rememberInteraction(result.interaction_id);" in html
    assert "{clientRequestId:entry.id, interactionId:result.interaction_id}" in html
    assert 'queuedOffline\n            ? "Waiting for a connection"' in html
    assert 'window.addEventListener("online",retryOfflineRequests)' in html
    assert 'id="retryOffline"' in html
    assert 'id="clearOffline"' in html
    assert 'id="offlineDiscardDialog" class="install-dialog"' in html
    assert 'id="confirmOfflineDiscard" class="danger"' in html
    assert 'byId("offlineDiscardDialog").showModal()' in html
    assert 'byId("confirmOfflineDiscard").onclick = async () =>' in html
    assert 'confirm("Discard all saved offline requests?")' not in html
    assert "await clearOfflineRequests();" in html
    assert "error.status = response.status" in html
    assert "let resumedOfflinePhotoIds = []" in html
    assert "resumedOfflinePhotoIds = Array.isArray(entry.photo_ids)" in html
    assert "[...resumedOfflinePhotoIds,...pendingPhotos.map" in html
    assert "let pendingPhotoPayloads = []" in html
    assert "function photoPayloadBytes(payload)" in html
    assert "async function uploadPhotoPayload(payload)" in html
    assert "entry.photo_payloads.shift();" in html
    assert "await saveOfflineRequest(entry);" in html
    assert 'pendingPhotoPayloads.length ? "Photo saved on this device" : "Photo ready"' in html


def test_mobile_surface_offers_one_tap_location_disambiguation() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "function locationChoiceRows(candidates)" in html
    assert "candidate.selection_command" in html
    assert "data-location-command=" in html
    assert "function bindLocationChoices(node)" in html
    assert "await submit()" in html
    assert "function keepPhotoEvidence(photoIds = [])" in html
    assert "pendingPhotos.push({id})" in html
    assert 'setComposerStatus("Photo kept for next request")' in html


def test_storage_phone_layout_keeps_filters_and_places_readable() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "grid-template-columns:minmax(0,1fr) auto;" in storage
    assert "overflow:visible;" in storage
    assert ".inventory-scope-select { display:block; min-width:0; }" in storage
    assert (
        ".root-list { display:grid; grid-template-columns:1fr; gap:6px; }"
        in storage
    )
    assert ".root-row { width:100%; min-width:0; align-items:start; }" in storage
    assert (
        ".root-row { min-height:58px; border:1px solid var(--line);"
        in storage
    )
    assert (
        ".root-row .root-main strong, .root-row .root-meta { overflow:visible; "
        "text-overflow:clip; white-space:normal;"
        in storage
    )
    assert ".inventory-select-visible { order:0; margin-left:0; }" in storage
    assert "#moveLocationDialog {" in storage
    assert "width:100vw;" in storage
    assert ".destination-row {" in storage
    assert "grid-template-columns:minmax(0,1fr);" in storage
    assert "white-space:normal;" in storage
    assert "overflow-wrap:anywhere;" in storage
    assert "let mobileBrowserScrollY = 0;" in storage
    assert 'classList.contains("mobile-detail-open")' in storage
    assert "mobileBrowserScrollY = window.scrollY;" in storage
    assert "if (mobile && resetScroll) mobileBrowserScrollY = 0;" in storage
    assert "if (mobile && (wasDetailOpen || resetScroll))" in storage
    assert "window.scrollTo({top:mobileBrowserScrollY, behavior:\"auto\"})" in storage


def test_mobile_client_uses_one_secure_visibility_aware_state_channel() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert "async function initializeInteractionCursor()" in html
    assert 'api("/assistant/interactions?limit=1")' in html
    assert "Number(latest?.id || 0)" in html
    assert "await initializeInteractionCursor();" in html
    assert html.index("await initializeInteractionCursor();") < html.index(
        "pollState();", html.index("await initializeInteractionCursor();")
    )
    assert 'api(`/kiosk/state?${query}`)' in html
    assert 'wait_seconds:"20"' in html
    assert 'document.visibilityState === "hidden"' in html
    assert 'document.addEventListener("visibilitychange",resolve,{once:true})' in html
    assert "setInterval(" not in html
    assert "if (elapsed < 250)" in html
    assert "function updateDockSpace()" in html
    assert "function syncAuthoritativePending(interaction, confirmationState = null)" in html
    assert "state.pending_confirmation || null" in html
    assert "const response = normalizeResponse(interaction.response || {});" in html
    assert "interaction.photo_ids || []" in html
    assert 'setComposerStatus("Confirmation needed")' in html
    assert 'new ResizeObserver(updateDockSpace).observe(document.querySelector(".composer-shell"))' in html
    pending_interaction = html[html.index('if (interaction.status === "pending"'):html.index('if (!["/assistant/confirm"')]
    assert "appendInteractionUser(interaction, response)" in pending_interaction
    assert pending_interaction.index("appendInteractionUser(") < pending_interaction.index("renderPendingPlan(")
    remember = html[html.index("function rememberInteraction"):html.index("async function undoLatest")]
    assert "seenInteractions.add(id)" in remember
    assert "latestInteractionId" not in remember
    assert "localStorage" not in html
    assert "Authorization" not in html
    assert "MAGPIE_EDGE_TOKEN" not in html


def test_operational_pages_share_the_mobile_product_navigation() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    account = ACCOUNT_HTML.read_text(encoding="utf-8")
    review = REVIEW_HTML.read_text(encoding="utf-8")
    shell = PRODUCT_SHELL_JS.read_text(encoding="utf-8")

    for html in (storage, account, review):
        assert 'aria-label="Mobile product views"' in html
        assert "product-bottom-nav" in html
        assert "data-product-nav" in html

    for html in (storage, account):
        assert '.brand nav { display:none; }' in html

    assert '{key: "inventory", label: "Items", ariaLabel: "Browse items", context: "Items", href: "/?view=inventory"}' in shell
    assert '{key: "places", label: "Places", ariaLabel: "Browse places", context: "Places", href: "/"}' in shell
    assert '{key: "review", label: "Needs review", ariaLabel: "Review Magpie decisions", context: "Needs review", href: "/review"}' in shell
    assert '{key:"chat", label:"Chat", ariaLabel:"Chat with Magpie", href:"/mobile"}' in shell
    assert 'key: "ask"' not in shell
    assert 'key: "cabinet"' not in shell
    assert '{key: "account", label: "Account", ariaLabel: "Manage account", context: "Account", href: "/account"}' in shell
    assert 'if (url.pathname === "/review") return "review"' in shell
    assert 'link.setAttribute("aria-label", view.ariaLabel)' in shell
    assert "link.title = view.ariaLabel" in shell
    assert 'link.dataset.productView = view.key' in shell
    assert 'root.querySelectorAll("[data-product-context]")' in shell
    assert "function activeContext(url = window.location)" in shell
    assert 'new URLSearchParams(url.search).get("view") === "rules"' in shell
    assert 'return "Rules";' in shell
    assert "context.textContent = contextLabel" in shell
    assert 'const productView = showPlaces || showRules ? "places" : "inventory";' in storage
    assert 'const productContext = showRules ? "Rules" : showPlaces ? "Storage" : "Inventory";' in storage
    assert 'link.setAttribute("aria-current", "page")' in storage
    assert "function mobileAgentUrl(" in storage
    assert 'mobileAgentUrl("Put away ", location.id, "put-away")' in storage
    assert 'mobileAgentUrl(prompt, "", "setup-new")' not in storage
    assert 'mobileAgentUrl(`Help me set up storage inside ${path}.`, location.id, "setup")' not in storage


def test_storage_header_status_stays_scoped_to_the_active_view() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "const primaryViewStatus = {" in storage
    assert 'inventory:"Loading inventory"' in storage
    assert 'places:"Loading storage"' in storage
    assert 'rules:"Loading rules"' in storage
    assert "function currentPrimaryView()" in storage
    assert "function syncPrimaryViewStatus(view = currentPrimaryView())" in storage
    assert "syncPrimaryViewStatus(view);" in storage
    primary_view = storage[
        storage.index("function setPrimaryView"):storage.index(
            "function renderStorageLoading"
        )
    ]
    assert primary_view.count('window.scrollTo({top:0, behavior:"auto"});') == 3
    assert '"inventory",' in storage[
        storage.index("async function loadInventory"):storage.index(
            "function canManageRules"
        )
    ]
    assert '"rules",' in storage[
        storage.index("async function loadRules"):storage.index(
            "function correctionParts"
        )
    ]
    assert '"places",' in storage[
        storage.index("async function loadLocations"):storage.index(
            "function syncViewSwitch"
        )
    ]


def test_storage_and_account_share_operational_page_rhythm() -> None:
    theme = THEME_CSS.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    account = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert ".product-page-main {" in theme
    assert ".product-page-heading {" in theme
    assert ".product-section {" in theme
    assert "scroll-margin-top: 78px" in theme
    assert ".product-section-head {" in theme
    assert ".product-app [hidden] {" in theme
    assert 'class="inventory-heading product-page-heading"' in storage
    assert 'class="product-page-main account-main"' in account
    assert "<h2>Account</h2>" in account
    assert 'class="account-layout"' in account
    assert 'class="account-nav" aria-label="Account sections"' in account
    assert 'class="account-sections"' in account
    assert 'id="workspaceSection" class="product-section"' in account
    assert 'class="section-head product-section-head"' in account
    assert ".account-nav a:focus-visible" in account


def test_review_surface_unifies_actionable_decision_queues_without_polling() -> None:
    html = REVIEW_HTML.read_text(encoding="utf-8")

    assert '<body class="product-app">' in html
    assert '<span class="product-context" data-product-context>Review</span>' in html
    assert '<script src="/product-shell.js" defer></script>' in html
    assert '<script src="/auth.js"></script>' in html
    assert "Items to check" in html
    assert "Storage checks" in html
    assert "Learning decisions" in html
    assert "Rule issues" in html
    assert 'api("/review/queue?limit=50")' in html
    assert 'id="placeReviewCount"' in html
    assert 'id="placeReviewList"' in html
    assert "function renderPlaceChecks(places)" in html
    assert 'check:"1"' in html
    assert 'api("/inventory/review")' not in html
    assert 'api("/assistant/learning/examples' not in html
    assert 'api("/rules")' not in html
    assert 'api("/inventory/reviewed"' in html
    assert 'data-learning-action="promote"' in html
    assert 'data-learning-action="approve"' in html
    assert 'data-learning-action="reject"' in html
    assert 'params.set(action === "move" ? "review_item" : "edit_item", item.item_id)' in html
    assert '/storage?view=rules&amp;scope=issues' in html
    assert "setInterval" not in html
    assert "/assistant/command" not in html
    assert "Review unavailable" in html
    assert 'id="retryReview"' in html
    assert ".review-main { padding-bottom:calc(76px + env(safe-area-inset-bottom)); }" in html
    assert 'id="reviewReceipt" class="review-receipt" role="status" aria-live="polite" hidden' in html
    assert "function showReceipt(" in html
    assert 'data-undo-interaction="${escapeHtml(interactionId)}"' in html
    assert 'body:JSON.stringify({expected_interaction_id:interactionId})' in html
    assert '"Item approved",' in html
    assert 'showReceipt("Rule learned"' in html
    assert 'showReceipt("Learning evidence approved"' in html
    assert '"Learning evidence rejected",' in html
    assert 'showReceipt("Could not undo", error.message, {error:true})' in html


def test_account_section_navigation_only_shows_available_sections() -> None:
    account = ACCOUNT_HTML.read_text(encoding="utf-8")

    for section_id in (
        "workspaceSection",
        "dataSection",
        "agentConnectionsSection",
        "hardwareInterfacesSection",
        "diagnosticsSection",
    ):
        assert f'data-account-target="{section_id}"' in account
    assert 'data-account-target="agentConnectionsSection" hidden' in account
    assert "function syncAccountNavigation()" in account
    assert "link.hidden = !section || section.hidden" in account
    assert account.count("syncAccountNavigation();") >= 3


def test_operational_surfaces_share_direct_product_destinations() -> None:
    shell = PRODUCT_SHELL_JS.read_text(encoding="utf-8")
    for page in (
        INDEX_HTML,
        MOBILE_HTML,
        STORAGE_HTML,
        REVIEW_HTML,
        ACCOUNT_HTML,
        CHECK_STATION_HTML,
    ):
        html = page.read_text(encoding="utf-8")
        assert '<script src="/product-shell.js" defer></script>' in html
        assert "data-product-nav" in html

    for destination in (
        '{key: "inventory", label: "Items", ariaLabel: "Browse items", context: "Items", href: "/?view=inventory"}',
        '{key: "places", label: "Places", ariaLabel: "Browse places", context: "Places", href: "/"}',
        '{key: "review", label: "Needs review", ariaLabel: "Review Magpie decisions", context: "Needs review", href: "/review"}',
        '{key: "account", label: "Account", ariaLabel: "Manage account", context: "Account", href: "/account"}',
    ):
        assert destination in shell
    assert '|| url.pathname === "/mobile"' in shell
    assert 'if (url.pathname === "/cabinet") return new URLSearchParams(url.search).get("view") === "inventory" ? "inventory" : "places"' in shell
    assert 'if (view === "places" || view === "rules") return "places"' in shell
    assert 'if (view === "inventory") return "inventory"' in shell
    assert 'if (params.has("location") || params.has("resume")) return "places"' in shell
    assert 'const ASSISTANT_SURFACE_KEY = "magpie.assistant-surface"' in shell
    assert 'if (mobilePath) window.sessionStorage.setItem(ASSISTANT_SURFACE_KEY, "mobile")' in shell
    assert 'else if (desktopPath) window.sessionStorage.removeItem(ASSISTANT_SURFACE_KEY)' in shell
    assert 'window.matchMedia("(max-width: 680px)").matches' in shell
    assert 'if (view.key === "chat") return "/mobile"' in shell
    assert 'if (view.key === "inventory") return "/storage?view=inventory"' in shell
    assert 'if (view.key === "places") return "/cabinet"' in shell
    assert "link.href = viewHref(view, url)" in shell
    assert 'link.setAttribute("aria-current", "page")' in shell
    assert "context.textContent = contextLabel" in shell

    theme = THEME_CSS.read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in theme
    assert ".product-app .product-bottom-nav a.active" in theme
    assert ".product-app .product-bottom-nav a.active::before" in theme


def test_storage_keeps_color_coded_find_context_until_cleared() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="activeFindsPanel" class="product-active-finds"' in storage
    assert 'aria-labelledby="activeFindsTitle" aria-live="polite"' in storage
    assert '<h3 id="activeFindsTitle">Active locations</h3>' in storage
    assert 'id="activeFindsList" class="product-active-finds-list"' in storage
    assert 'id="clearActiveFinds"' in storage
    assert 'await api("/lights/active")' in storage
    assert 'await api("/clear", {method:"POST"})' in storage
    assert "function activeFindRows()" in storage
    assert "function renderActiveFinds()" in storage
    assert 'activeFinds = Array.isArray(result.active_finds)' in storage
    assert 'class="product-active-find has-action"' in storage
    assert 'class="product-active-find-swatch"' in storage
    assert 'style="--find-color:rgb(${row.color.join(",")})"' in storage
    assert "MagpieLocationDisplay.pathText(match.path || [])" in storage
    assert 'const sourceDialog = button.closest("dialog")' in storage
    assert "Array.isArray(result.results) && result.results.length && sourceDialog?.open" in storage
    assert "locationId:match.location_id || match.lit_location || path" in storage
    assert 'data-open-active-find="${escapeHtml(row.locationId)}"' in storage
    assert "function reportActiveFindNotHere(button)" in storage
    assert 'api("/assistant/find/not-here"' in storage
    assert 'data-active-find-not-here' in storage
    assert 'setPrimaryView("places")' in storage
    assert "openLocationDetail(button.dataset.openActiveFind)" in storage


def test_find_sessions_use_consistent_cross_surface_language() -> None:
    desktop = INDEX_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")
    kiosk = KIOSK_HTML.read_text(encoding="utf-8")

    assert "MagpieLocationDisplay.pathText(match.path || [])" in desktop
    assert ".filter((match) => match.lit_location)" in desktop
    assert 'id="lights" class="product-active-finds assistant-active-finds"' in mobile
    assert 'aria-labelledby="lightCount" aria-live="polite"' in mobile
    assert 'id="lightList" class="product-active-finds-list"' in mobile
    assert 'class="product-active-find${row.kind === "find" && row.itemId ? " has-action" : ""}"' in mobile
    assert 'kind:entry.kind === "put_away" ? "put_away" : "find"' in mobile
    assert 'class="product-active-find-kind"' in mobile
    assert 'row.kind === "find" && row.itemId' in mobile
    assert 'MagpieLocationDisplay.pathText(path, [], " › ")' in mobile
    assert 'new ResizeObserver(updateDockSpace).observe(document.querySelector(".composer-shell"))' in mobile
    assert "active location${rows.length === 1" in mobile
    assert "function reportFindNotHere(button)" in mobile
    assert 'data-find-not-here' in mobile
    assert 'api("/assistant/find/not-here"' in mobile
    assert 'return {action:"not-here", command:clean}' in mobile
    assert 'item.review_reason === "Reported missing during retrieval."' in mobile
    assert 'review_item=${encodeURIComponent(item.item_id)}' in mobile
    assert ">Move item</a>" in mobile
    assert '<h3 id="activeFindsTitle">Active locations</h3>' in storage
    assert 'class="product-active-find-kind"' in storage
    assert 'row.kind === "find" && row.itemId' in storage
    assert 'aria-label="Active locations"' in kiosk
    assert "<h2>Active locations</h2>" in kiosk
    assert 'class="find-kind"' in kiosk
    assert 'first.kind === "put_away"' in kiosk
    assert '"put away" : "find"' in kiosk
    assert "Clear all" in kiosk
    assert "No active locations." in kiosk


def test_kiosk_labels_completed_quick_puts_as_stored() -> None:
    html = KIOSK_HTML.read_text(encoding="utf-8")
    interaction_display = html[
        html.index("function interactionDisplay(interaction)"):
        html.index("function renderInteractions(interactions)")
    ]

    assert 'if (route.includes("quick-put")) {' in interaction_display
    assert "response.item?.name || command" in interaction_display
    assert "response.destination?.path || []" in interaction_display
    assert (
        'title: response.ok === false ? "Put-away failed" : "Stored"'
        in interaction_display
    )
    assert 'detail: destination ? `${item} in ${destination}`' in interaction_display
    assert interaction_display.index('if (route.includes("quick-put")) {') < (
        interaction_display.index('if (action === "put_away")')
    )
    assert "const destinationLabel = MagpieLocationDisplay.pathText(" in interaction_display
    assert "destination.path || []" in interaction_display
    assert "detail: destinationLabel || proposalActivityDetail" in interaction_display


def test_kiosk_only_claims_a_physical_light_when_one_is_available() -> None:
    html = KIOSK_HTML.read_text(encoding="utf-8")

    assert 'MagpieLocationDisplay.pathText(path, [], " › ")' in html
    assert "lit: Boolean(match.lit_location)" in html
    assert '${first.lit ? `${colorName} ` : ""}' in html
    assert "? first.lit" in html
    assert ': `Put it in ${first.path || "the selected storage location"}.`' in html
    assert ': `${first.name} is in ${first.path || "the selected storage location"}.`' in html
    assert '"Use the color key and storage paths in Active locations."' in html


def test_reviewed_missing_item_handoff_opens_the_move_flow() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'const initialReviewItemId = Number(initialParams.get("review_item") || 0)' in storage
    assert 'const initialEditItemId = Number(initialParams.get("edit_item") || 0)' in storage
    assert "async function openInitialReviewEdit()" in storage
    assert 'url.searchParams.delete("edit_item")' in storage
    assert "await openInitialReviewEdit();" in storage
    assert 'const initialReviewItemFrom = initialParams.get("from") || ""' in storage
    assert 'inventoryScope = "review"' in storage
    assert "async function openInitialReviewMove()" in storage
    assert 'await api("/inventory/review")' in storage
    assert "openMoveItem(initialReviewItemId, initialReviewItemFrom)" in storage
    assert "await openInitialReviewMove()" in storage
    assert "preview.review_resolution_count" in storage
    assert "result.resolved_review_count" in storage
    assert 'result.resolved_review_count && inventoryScope === "review"' in storage


def test_account_and_check_station_offer_recoverable_loading_states() -> None:
    account = ACCOUNT_HTML.read_text(encoding="utf-8")
    station = CHECK_STATION_HTML.read_text(encoding="utf-8")

    assert 'id="workspaceSection" class="product-section" aria-busy="true"' in account
    assert "function showAccountLoadError(error)" in account
    assert 'id="retryAccount"' in account
    assert "load().catch(showAccountLoadError)" in account

    assert 'id="destinationList" aria-busy="true"' in station
    assert 'id="locationSelect" disabled' in station
    assert 'id="suggestButton" type="button" disabled' in station
    assert "async function loadLocations()" in station
    assert 'id="retryLocations"' in station
    assert "if (!locationsReady)" in station


def test_storage_manager_searches_across_all_roots() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert (
        'id="globalSearch" type="search" placeholder="Search all storage"'
        in storage
    )
    assert 'id="clearGlobalSearch"' in storage
    assert '.global-search input::-webkit-search-cancel-button' in storage
    assert "async function searchAllStorage()" in storage
    assert 'api(`/storage/search?${params}`)' in storage
    assert 'archived:String(showArchived)' in storage
    assert 'if (byId("globalSearch").value.trim()) await searchAllStorage()' in storage
    assert 'result.result_type === "location"' in storage
    assert 'aria-label="Matching places"' in storage
    assert 'aria-label="Matching items"' in storage
    assert 'location.accepts_items === true ? "Stores items" : "Contains places"' in storage
    assert ">Open location</button>" in storage
    assert '.item-row.location-search-result { grid-template-columns:minmax(0,1fr); }' in storage
    assert 'data-open-search-result="${escapeHtml(location.location_id)}"' in storage
    assert (
        'data-open-search-result="${escapeHtml(item.location_id || item.bin_id)}"'
        in storage
    )
    assert 'data-find-search-result="${escapeHtml(item.name)}"' in storage
    assert "function openSearchResult(locationId)" in storage
    assert 'byId("addChild").hidden = true' in storage


def test_inventory_can_filter_items_by_top_level_storage_space() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert (
        'id="inventoryRoot" aria-label="Filter by storage space"' in storage
    )
    assert "function syncInventoryRoots()" in storage
    assert '.filter((location) => !location.parent_id)' in storage
    assert 'const rootId = byId("inventoryRoot").value' in storage
    assert 'params.set("root_id", rootId)' in storage
    assert 'byId("inventoryRoot").onchange = () => {' in storage
    assert 'byId("inventoryCategory").value = ""' in storage
    assert "#inventoryRoot, .rules-toolbar .inventory-search" in storage


def test_storage_location_detail_navigates_nested_places_on_mobile() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'aria-label="Place path"' in storage
    assert 'data-path-location="${escapeHtml(node.id)}"' in storage
    assert 'class="inside-section"' in storage
    assert 'data-child-location="${escapeHtml(child.id)}"' in storage
    assert 'id="insideAdd"' not in storage
    assert 'id="detailAddInside"' in storage
    assert 'id="insideFilter"' in storage
    assert 'aria-label="Filter places inside"' in storage
    assert 'max-height:min(360px,45vh)' in storage
    assert "async function openLocationDetail(locationId)" in storage
    assert 'openLocationDetail(button.dataset.childLocation)' in storage
    assert 'url.searchParams.set("location", locationId)' in storage
    assert 'url.searchParams.delete("location")' in storage


def test_storage_progressively_renders_large_hierarchies_and_reveals_selection() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "function revealLocationInTree(locationId)" in storage
    assert "path.slice(0, -1).forEach" in storage
    assert "Math.floor(childIndex / pageSize)" in storage
    assert "children.slice(" in storage
    assert "visibleScopedChildren = scopedChildren.slice(" in storage
    assert 'class="tree-more tree-more-root"' in storage
    assert ".tree-more-root { width:100%; margin-left:0; }" in storage
    assert "hiddenBeforeCount" in storage
    assert "hiddenAfterCount" in storage
    assert "function scrollSelectedTreeRowIntoView()" in storage
    assert 'row?.scrollIntoView({block:"nearest", inline:"nearest"})' in storage
    assert 'aria-expanded="${expanded}"' in storage


def test_shared_product_mark_uses_one_brand_treatment() -> None:
    theme = THEME_CSS.read_text(encoding="utf-8")

    assert "--brand: #18231f;" in theme
    assert "--brand-ink: #ffffff;" in theme
    assert "--signal: #d4a72c;" in theme
    assert 'background: var(--brand) url("/app-icon.svg") center / cover no-repeat;' in theme
    assert "color: transparent;" in theme
    assert "box-shadow: 0 1px 3px" in theme


def test_assistant_and_storage_interactions_use_semantic_theme_colors() -> None:
    theme = THEME_CSS.read_text(encoding="utf-8")
    assistant = INDEX_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "--danger-ink: #ffffff;" in theme
    assert "--danger-ink: #111816;" in theme
    assert "color: var(--accent-ink);" in theme
    assert (
        ".tab.active { background: var(--accent); color: var(--accent-ink);"
        in assistant
    )
    assert "background: var(--brand);" in assistant
    assert "color: var(--brand-ink);" in assistant
    assert "background: var(--danger); color: var(--danger-ink);" in assistant
    assert "border-top: 1px solid var(--line);" in assistant
    assert "rgba(15,118,110,.1)" not in storage


def test_mobile_client_is_the_installed_pwa_surface() -> None:
    api_source = Path("src/searchable_storage_led/api.py").read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert '@app.get("/mobile", response_class=HTMLResponse)' in api_source
    assert '"start_url": "/mobile?source=pwa"' in api_source
    assert '"url": "/mobile?action=find&source=shortcut"' in api_source
    assert '"url": "/mobile?action=put-away&source=shortcut"' in api_source
    assert '"url": "/storage?view=inventory&source=shortcut"' in api_source
    assert (
        '"url": "/storage?view=places&create=top&source=shortcut"'
        in api_source
    )
    assert '"url": "/check-station?source=shortcut"' not in api_source
    assert '"url": "/?action=rules&source=shortcut"' not in api_source
    assert "APP_SHELL_PATHS = (" in api_source
    assert '    "/mobile",' in api_source
    assert '"mobile_client": True' in api_source
    assert '"mobile_client": "/mobile"' in api_source
    assert '"cabinet_console": "/cabinet"' in api_source
    assert 'id="installButton" class="secondary install-button" type="button" hidden' in mobile
    assert 'window.matchMedia("(display-mode: standalone)").matches' in mobile
    assert 'id="installDialog" class="install-dialog"' in mobile
    assert "function isIosDevice()" in mobile
    assert 'platform === "MacIntel" && window.navigator.maxTouchPoints > 1' in mobile
    assert "function isIosSafari()" in mobile
    assert "function iosInstallAvailable()" in mobile
    assert "isIosDevice() && window.isSecureContext" in mobile
    assert "(!deferredInstallPrompt && !iosInstallAvailable())" in mobile
    assert "function showIosInstallHelp()" in mobile
    assert '"Open this page in Safari, then add Magpie to your Home Screen."' in mobile
    assert 'byId("installDialog").showModal()' in mobile
    assert 'id="closeInstall"' in mobile
    assert 'id="finishInstall"' in mobile
    assert 'byId("installDialog").addEventListener("cancel"' in mobile
    assert 'window.addEventListener("beforeinstallprompt"' in mobile
    assert "event.preventDefault();" in mobile
    assert "deferredInstallPrompt = event;" in mobile
    assert "await prompt.prompt();" in mobile
    assert "await prompt.userChoice" in mobile
    assert 'window.addEventListener("appinstalled"' in mobile
    assert "setupPwaInstall();" in mobile
    assert "setupPwaInstall();\n    (window.magpieAuthReady || Promise.resolve()).then(start);" in mobile


def test_cabinet_kiosk_shows_voice_transaction_takeover() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="voiceTakeover"' in html
    assert 'aria-live="assertive"' in html
    assert 'get("kiosk") === "1"' in html
    assert "function renderVoiceTakeover(voice)" in html
    assert 'const lastResult = voice.last_result || {}' in html
    assert 'title: "Magpie Ready"' in html
    assert '"Magpie ready - say \\"Magpie\\" for another request"' in html
    assert '["listening", "heard", "processing"].includes(state)' in html
    assert 'state === "done" && age <= 12' in html
    assert 'title: "Confirm?"' in html
    assert 'notFound ? "Not found" : (found ? "Found" : "Done")' in html
    assert 'data-outcome="not-found"' in html
    assert 'confirmed ? "Saved to inventory" : (state === "waiting" ? "Always listening" : "Magpie voice")' in html
    assert '"Say yes or no"' in html


def test_kiosk_activity_is_transaction_focused_and_uses_readable_paths() -> None:
    html = KIOSK_HTML.read_text(encoding="utf-8")
    theme = THEME_CSS.read_text(encoding="utf-8")

    assert "function interactionLocationPath(interaction, locationId)" in html
    assert "placement?.path_label" in html
    assert '`${applied.stored} in ${destination}`' in html
    assert 'route.startsWith("/locations/")' in html
    assert 'String(action).startsWith("manual_create_location")' in html
    assert "if (display) {" in html
    assert "-webkit-line-clamp: 2" in html
    assert "function findActivity(interaction, response, command)" in html
    assert 'MagpieLocationDisplay.pathText(item.path || [], [], " › ")' in html
    assert '`${item.name || query} in ${path}`' in html
    assert "function activityRowStyle(row)" in html
    assert '--activity-color:rgb(${color.join(",")})' in html
    assert "border-left: 3px solid var(--activity-color, var(--line))" in html
    assert "border-left-color: var(--activity-color, var(--accent))" in html
    assert ".product-active-find-copy span:not(.product-active-find-kind)" in theme
    assert "white-space: normal" in theme


def test_voice_diagnostics_show_activation_success_rates() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "summary.routing_rate" in html
    assert "summary.end_to_end_rate" in html
    assert "% routed" in html
    assert "% end-to-end" in html
    assert html.rindex("startScreenHeartbeat();") < html.rindex("startApplicationView();")
    assert html.rindex("startKioskStateChannel();") < html.rindex("startApplicationView();")
    assert "Voice confirmed plan" in html
    assert "Voice canceled plan" in html
    assert "Undid latest plan" in html


def test_assistant_activity_panel_shows_unified_timeline() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel assistant-activity advanced-only"' in html
    assert 'id="assistantActivity"' in html
    assert 'id="activityRefreshBtn"' in html
    assert 'id="undoLatestBtn"' in html
    assert "function loadAssistantActivity" in html
    assert "function undoLatestInteraction" in html
    assert 'api("/assistant/interactions?limit=8")' in html
    assert 'api("/assistant/undo", {method: "POST"})' in html
    assert "function renderActivityRow" in html
    assert "interactionSourceLabel" in html
    assert 'route.includes("/quick-put")' in html
    assert 'route === "/inventory/manual"' in html
    assert "manual_put_inventory" in html
    assert "manual_update_inventory" in html
    assert "manual_move_inventory" in html
    assert "manual_move_location" in html
    assert "manual_remove_inventory" in html
    assert "manual_clear_inventory" in html
    assert "manual_location_add_item" in html
    assert 'response.source === "voice" || response.voice' in html
    assert "response.voice_attempt" in html
    assert "const voice = response.voice || response.voice_attempt || {}" in html
    assert "sent ${voice.query}" in html
    assert 'byId("activityRefreshBtn").onclick = loadAssistantActivity' in html
    assert 'byId("undoLatestBtn").onclick = undoLatestInteraction' in html
    assert 'button.textContent = "Undoing"' in html
    assert 'button.textContent = "Undo latest"' in html
    assert "await refreshCurrentView()" in html


def test_voice_diagnostics_panel_surfaces_mic_and_routing_attempts() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel voice-diagnostics advanced-only"' in html
    assert 'id="voiceAttempts"' in html
    assert 'id="voiceCalibration"' in html
    assert 'id="voiceRefreshBtn"' in html
    assert "function loadVoiceDiagnostics" in html
    assert 'api("/voice/tuning?limit=8")' in html
    assert 'api("/voice/calibration?limit=50")' in html
    assert 'api("/voice/status")' in html
    assert 'capabilities.local_stt_available ? "local hearing"' in html
    assert 'capabilities.local_tts_available ? "local speech"' in html
    assert "suggested threshold" in html
    assert "cal.quiet_samples" in html
    assert "cal.speech_samples" in html
    assert "function renderVoiceAttempt" in html
    assert "attempt.audio_threshold" in html
    assert "attempt.activity_rms" in html
    assert "speech activity" in html
    assert "attempt.tuning" in html
    assert "needs tuning" in html
    assert "Object.entries(checks)" in html
    assert "attempt.stt_model" in html
    assert "attempt.wake_heard" in html
    assert "tuning.screen_observation" in html
    assert "tuning.screen_seen_interaction" in html
    assert "interaction observed" in html
    assert "screen active · interaction not observed" in html
    assert "screen not active · interaction not observed" in html
    assert 'byId("voiceRefreshBtn").onclick = loadVoiceDiagnostics' in html


def test_voice_acceptance_panel_guides_end_to_end_cabinet_check() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel voice-acceptance advanced-only"' in html
    assert 'id="voiceAcceptanceStatus"' in html
    assert 'id="voiceAcceptancePrompt"' in html
    assert 'id="voiceAcceptanceInstruction"' in html
    assert 'id="voiceAcceptanceFailures"' in html
    assert "function loadVoiceAcceptance" in html
    assert 'api("/voice/acceptance")' in html
    assert "res.lit_find" in html
    assert "res.hands_free_confirmation?.ready" in html
    assert "hands-free put-away" in html
    assert 'res.evidence_state === "missing_current"' in html
    assert "No voice checks for build" in html
    assert "What failed" in html
    assert 'byId("voiceAcceptanceRefreshBtn").onclick = loadVoiceAcceptance' in html
    assert 'document.querySelector(".voice-acceptance").addEventListener("toggle"' in html


def test_product_readiness_panel_surfaces_feature_complete_gates() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="panel demo-readiness advanced-only"' in html
    assert 'id="readinessStatus"' in html
    assert 'id="readinessChecks"' in html
    assert 'id="readinessRefreshBtn"' in html
    assert "function loadDemoReadiness" in html
    assert "Product readiness" in html
    assert 'api("/product/readiness")' in html
    assert "renderReadinessCheck" in html
    assert "needs proof" in html
    assert "Next: ${escapeHtml(check.next_step)}" in html
    assert 'check.action?.href && check.action?.label' in html
    assert 'href="${escapeHtml(check.action.href)}"' in html
    assert "Number(a.passed) - Number(b.passed)" in html
    assert 'document.querySelector(".demo-readiness").addEventListener("toggle"' in html
    assert "if (advancedMode && event.currentTarget.open) loadDemoReadiness()" in html
    assert 'byId("readinessRefreshBtn").onclick = loadDemoReadiness' in html
    assert "res.required_total" in html
    assert "Software ready:" in html


def test_account_guides_visual_workspace_setup_without_operator_gates() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")
    load_body = html[
        html.index("async function load()") : html.index("function showAccountLoadError")
    ]

    assert 'id="readinessSection"' in html
    assert 'id="readinessList"' in html
    assert 'id="readinessSummary"' in html
    assert "Workspace setup" in html
    assert "function renderWorkspaceReadiness(data)" in html
    assert "const checks = Array.isArray(data.checks) ? data.checks : []" in html
    assert 'const nextCheckKey = checks.find((check) => !check.passed)?.key || ""' in html
    assert "Number(data.required_passed || 0)" in html
    assert "Magpie is ready for find and put-away." in html
    assert "check.key === nextCheckKey && check.action?.href && check.action?.label" in html
    assert 'class="readiness-step${passed ? " passed" : ""}"' in html
    assert 'const readinessRequest = api("/workspace/readiness").catch(() => null)' in html
    assert "const readinessData = await readinessRequest" in html
    assert "if (readinessData) renderWorkspaceReadiness(readinessData)" in html
    assert "api(\"/workspace/agent-credentials\"),\n        readinessRequest" in html
    assert 'api("/product/readiness")' not in load_body
    assert "connected hardware" not in html


def test_account_shows_recorded_agent_usage_without_triggering_model_work() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="usageSection"' in html
    assert 'id="usageRequests"' in html
    assert 'id="usageInput"' in html
    assert 'id="usageOutput"' in html
    assert 'id="usageCached"' in html
    assert 'id="usageAssistantInteractions"' in html
    assert 'id="usageDeterministicInteractions"' in html
    assert 'id="usageModelInteractions"' in html
    assert 'id="usageModelRate"' in html
    assert 'id="usageSucceeded"' in html
    assert 'id="usageFailures"' in html
    assert 'id="usageFailureRate"' in html
    assert 'id="usageProviderState"' in html
    assert 'id="usageReliability"' in html
    assert "function renderUsage(data)" in html
    assert 'api("/usage/agent").catch(() => null)' in html
    assert "magpie.agent_usage.v2" in html
    assert 'providerState.textContent = reliability.degraded ? "Cooldown" : "Available"' in html
    assert "Provider billing remains authoritative." in html
    assert "Check the provider account for dollar spend and billing limits." in html
    assert "Handled locally" in html
    assert "Model-routed interactions" in html


def test_account_shows_process_local_database_transfer_without_extra_polling() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")
    load_body = html[
        html.index("async function load()") : html.index("function showAccountLoadError")
    ]

    assert 'id="storageUsageSection"' in html
    assert 'id="storageUsageRequests"' in html
    assert 'id="storageUsageBytes"' in html
    assert 'id="storageUsageRate"' in html
    assert 'id="storageUsageErrors"' in html
    assert 'id="storageUsageOperations"' in html
    assert "function renderStorageUsage(data)" in html
    assert 'api("/usage/storage").catch(() => null)' in html
    assert "magpie.storage_usage.v1" in html
    assert "Supabase Usage remains authoritative for billed egress" in html
    assert "Counters reset when the service restarts." in html
    assert 'api("/usage/storage")' not in load_body


def test_account_loads_advanced_diagnostics_only_when_opened() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")
    load_body = html[
        html.index("async function load()") : html.index("function showAccountLoadError")
    ]
    diagnostics_body = html[
        html.index("async function loadDiagnostics") : html.index(
            "async function updateAgentQualityExample"
        )
    ]

    assert 'id="diagnosticsSection" class="account-diagnostics"' in html
    assert "Diagnostics and learning" in html
    assert "Service build, model usage, voice activity, and learning review." in html
    assert 'data-account-target="diagnosticsSection">Advanced</a>' in html
    assert 'data-account-target="usageSection"' not in html
    assert 'data-account-target="agentQualitySection"' not in html
    assert 'api("/usage/agent")' not in load_body
    assert 'api("/product/readiness")' not in load_body
    assert 'api("/voice/status")' not in load_body
    assert "loadAgentQuality()" not in load_body
    assert 'api("/product/readiness").catch(() => null)' in diagnostics_body
    assert 'api("/usage/agent").catch(() => null)' in diagnostics_body
    assert 'api("/voice/status").catch(() => null)' in diagnostics_body
    assert "loadAgentQuality()" in diagnostics_body
    assert 'diagnosticsSection.addEventListener("toggle"' in html
    assert "async function openAccountHashTarget()" in html
    assert '"usageSection", "voiceUsageSection", "agentQualitySection"' in html
    assert "await openAccountHashTarget();" in html
    assert 'window.addEventListener("hashchange", openAccountHashTarget)' in html


def test_account_advanced_groups_actionable_product_readiness() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")
    diagnostics_body = html[
        html.index("async function loadDiagnostics") : html.index(
            "async function updateAgentQualityExample"
        )
    ]

    assert 'id="productReadinessSection" class="product-section" aria-busy="true"' in html
    assert 'id="productReadinessSummary"' in html
    assert 'id="productReadinessOverview" class="product-readiness-overview"' in html
    assert 'id="productReadinessGroups" class="product-readiness-groups"' in html
    assert "function renderProductReadiness(data)" in html
    assert "data.groups.map((group)" in html
    assert "data.checks.filter((check) => check.group === group.key)" in html
    assert 'group.required === false ? "Optional enhancement" : "Required"' in html
    assert '!group.ready && group.required !== false ? " open" : ""' in html
    assert "check.action?.href && check.action?.label" in html
    assert "Next:" in html
    assert 'api("/product/readiness").catch(() => null)' in diagnostics_body
    assert "renderProductReadiness(readinessData)" in diagnostics_body
    assert ".product-readiness-overview { grid-template-columns:repeat(2,minmax(0,1fr)); }" in html


def test_account_diagnostics_identify_the_connected_magpie_runtime() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")
    diagnostics_body = html[
        html.index("async function loadDiagnostics") : html.index(
            "async function updateAgentQualityExample"
        )
    ]

    assert 'id="runtimeSection" class="product-section" hidden' in html
    assert 'id="runtimeBuild"' in html
    assert 'id="runtimeRole"' in html
    assert 'id="runtimeStorage"' in html
    assert 'id="runtimeWorkspace"' in html
    assert "function renderRuntime(buildData, healthData)" in html
    assert 'api("/build").catch(() => null)' in diagnostics_body
    assert 'api("/health").catch(() => null)' in diagnostics_body
    assert "renderRuntime(buildData, healthData)" in diagnostics_body
    assert "storage_authority" in html
    assert "workspace_scope" in html


def test_account_shows_voice_privacy_gate_and_external_call_counts() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="voiceUsageSection"' in html
    assert 'id="voiceIgnoredClips"' in html
    assert 'id="voiceWakeDetections"' in html
    assert 'id="voiceWakeRate"' in html
    assert 'id="voiceSttCalls"' in html
    assert 'id="voiceAgentCalls"' in html
    assert 'id="voiceTtsCalls"' in html
    assert "function renderVoiceUsage(data)" in html
    assert 'api("/voice/status").catch(() => null)' in html
    assert "Audio before the wake word is not uploaded." in html
    assert "Bare yes/no confirmations stay local." in html
    assert "Assistant requests" in html
    assert "capabilities.assistant_requests ?? capabilities.agent_requests" in html
    assert "wakeDetections / speechClips" in html
    assert "Microphone heard speech, but the local wake gate has not recognized Magpie." in html
    assert "The wake gate recognized Magpie, but no cloud transcription followed." in html
    assert "Speech was transcribed, but no assistant request followed." in html


def test_account_owns_reviewed_agent_quality_without_voice_place_configuration() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="agentQualitySection"' in html
    assert "Voice executes storage work" in html
    assert "Find, put away, confirm, change, cancel, undo, and clear." in html
    assert "The Storage UI configures places" in html
    assert "Create, rename, move, archive, and remove places visually." in html
    assert 'id="agentQualityCoverage"' in html
    assert 'id="agentQualitySummary"' in html
    assert 'id="agentQualityList"' in html
    assert 'id="agentQualityScenario"' in html
    assert '<option value="organization">Organization</option>' in html
    assert "example.scenarios || []" in html
    assert 'class="quality-example-scenario"' in html
    assert "function loadAgentQuality()" in html
    assert "function renderAgentQualityExamples(examples)" in html
    assert "function renderAgentQualityCoverage(replay)" in html
    assert 'api("/assistant/learning/summary")' in html
    assert 'api("/assistant/evals/replay"' in html
    assert "JSON.stringify({limit:25, minimum_cases:1, run_model:false})" in html
    assert "Dry coverage checks make no model calls." in html
    assert 'identity.role !== "viewer"' in html
    assert "visibleAgentQualityIds" in html
    assert 'api("/assistant/learning/examples/bulk-approve"' in html
    assert 'params.set("scenario", scenario)' in html
    assert "Only trusted approved or promoted examples become release evidence." in html


def test_account_readiness_actions_name_the_surface_that_owns_the_work() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert "function readinessAction(action)" in html
    assert 'surface = "Ask Magpie"' in html
    assert 'surface = "Open Places"' in html
    assert 'surface = "Open Rules"' in html
    assert 'surface = "Open Items"' in html
    assert 'class="readiness-action-surface"' in html


def test_account_owner_can_delete_workspace_with_typed_confirmation() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="dangerSection" class="product-section" hidden' in html
    assert 'id="deleteWorkspaceConfirmation"' in html
    assert 'id="deleteWorkspaceButton" type="submit" disabled' in html
    assert 'identity.role !== "owner"' in html
    assert 'api("/workspace", {method:"DELETE"' in html
    assert 'localStorage.removeItem("magpie.auth.workspace.v1")' in html


def test_account_supports_workspace_rename_and_self_service_leave() -> None:
    html = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="workspaceSettingsSection" class="product-section" hidden' in html
    assert 'id="renameWorkspaceForm"' in html
    assert 'api("/workspace", {method:"PATCH"' in html
    assert 'id="leaveSection" class="product-section" hidden' in html
    assert 'id="leaveWorkspaceButton"' in html
    assert 'api("/workspace/membership", {method:"DELETE"' in html
    assert "!config.multi_workspace" in html


def test_agent_move_item_plans_get_color_and_light_controls() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '["put_item", "move_item"].includes(s.tool)' in html
    assert 'args.location_id || args.to_location_id || args.new_parent_id' in html
    assert 'step.arguments?.location_id || step.arguments?.to_location_id' in html
    assert 's.tool === "move_item"' in html
    assert "Move ${escapeHtml(item)} from" in html
    assert 's.tool === "remove_item"' in html
    assert "Remove ${escapeHtml(item)}" in html
    assert 's.tool === "attach_photo"' in html
    assert "Attach photo #" in html
    assert 's.tool === "update_location"' in html
    assert "Update ${escapeHtml(target)}" in html


def test_item_dimensions_are_visible_before_web_and_mobile_confirmation() -> None:
    web = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    mobile = (STATIC_DIR / "mobile.html").read_text(encoding="utf-8")

    for html in (web, mobile):
        assert "function planItemSize" in html
        assert 'dimensions.length_mm, dimensions.width_mm, dimensions.height_mm' in html
        assert 'measured.join(" × ")' in html
        assert "args?.size_class" in html or "a?.size_class" in html


def test_quick_actions_submit_current_text_as_one_shot_commands() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '"put-away": current ? `Put away ${current}` : "Put away "' in html
    assert '"find": current ? `Where is ${current}` : "Where is "' in html
    assert 'if (current || (action === "sort-photo" && pendingPhotos.length) || action === "rules")' in html
    assert "runCommand();" in html


def test_app_opens_scanned_location_from_url() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function setupInitialLocationFromUrl" in html
    assert 'params.get("location")' in html
    assert "await selectStorageLocation(locationId)" in html
    assert 'params.get("mode") === "put"' in html
    assert 'command.value = "Put away "' in html
    assert "await setupInitialLocationFromUrl();" in html


def test_location_panel_surfaces_hardware_bindings() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "Manage in Storage" in html
    assert 'id="editLightTarget"' not in storage
    assert 'id="editLightIndexes"' not in storage
    assert 'id="hardwareList"' in storage
    assert 'id="hardwareForm"' in storage
    assert 'id="hardwareController"' in storage
    assert "function commandDevices()" in storage
    assert "function hardwareControllerOptions(current" in storage
    assert "Storage works without them." in storage
    assert 'href="/account#devicesSection"' in storage
    assert "Local controller" in storage
    assert "function hardwareBindingMarkup(bindings)" in storage
    assert "function saveHardwareBinding()" in storage
    assert 'api(`/locations/${encodeURIComponent(locationId)}/hardware-bindings`' in storage
    assert 'api(`/locations/${encodeURIComponent(selectedId)}/light`' in storage
    assert "data-toggle-binding" in storage
    assert "Hardware binding saved" in storage


def test_storage_commissions_subtree_lights_with_preview_and_explicit_apply() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'id="commissionHardware"' in storage
    assert 'id="hardwareCommissionDialog" class="hardware-commission-dialog"' in storage
    assert 'id="hardwareCommissionController"' in storage
    assert 'id="hardwareCommissionAllocation"' in storage
    assert '<option value="existing_layout">Preserve current layout</option>' in storage
    assert '<option value="one">One pixel per place</option>' in storage
    assert '<option value="place_width">Use place width</option>' in storage
    assert 'id="hardwareCommissionReplace"' in storage
    assert 'id="previewHardwareCommission"' in storage
    assert 'id="applyHardwareCommission" type="button" disabled' in storage
    assert "function hardwareCommissionPayload()" in storage
    assert "function renderHardwareCommissionPreview(preview)" in storage
    assert "async function previewHardwareCommissioning()" in storage
    assert "async function applyHardwareCommissioning()" in storage
    assert (
        "`/locations/${encodeURIComponent(selectedId)}/hardware-bindings/batch-preview`"
        in storage
    )
    assert (
        "`/locations/${encodeURIComponent(locationId)}/hardware-bindings/batch`"
        in storage
    )
    preview_call = storage.index("async function previewHardwareCommissioning()")
    apply_call = storage.index("async function applyHardwareCommissioning()")
    assert preview_call < apply_call
    assert 'hardwareCommissionPreview?.can_apply' in storage
    assert "await loadLocations(locationId)" in storage[apply_call:]


def test_browser_voice_is_scoped_to_find_and_putaway() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert 'title="Speak a find, put-away, or confirmation"' in html
    assert "function voiceCommandSupported(command)" in html
    assert "Voice is for find and put-away. Type other requests or open Storage." in html
    assert "if (!voiceCommandSupported(command))" in html
    desktop_scope = html[
        html.index("function voiceCommandSupported(command)"):
        html.index("function submitWakeCommand(command)")
    ]
    assert "consolidate|organize|sort" not in desktop_scope
    assert "const demonstrativeMove =" in desktop_scope
    assert 'const routedCommand = demonstrativeMove ? `Put away ${demonstrativeMove[1]}` : command' in html
    assert 'nextCommandSource = "voice"' in html
    assert "source: options.source || \"ui\"" in html
    assert "let dictationSubmitted = false;" in html
    assert "for (let i = 0; i < event.results.length; i += 1)" in html
    assert "const finalResult = event.results[event.results.length - 1];" in html
    assert "finalResult?.isFinal && !dictationSubmitted && transcript.trim()" in html
    assert "recognition.stop();\n            runCommand();" in html
    assert "Listening for a find or put-away" in html
    assert "Ask Magpie to find something or put something away." in html
    assert "function routeBrowserVoiceCommand(text)" in mobile
    assert "function voiceCommandSupported(text)" in mobile
    mobile_scope = mobile[
        mobile.index("function routeBrowserVoiceCommand(text)"):
        mobile.index("function quickActionPrompt(action)")
    ]
    assert "consolidate|organize|sort" not in mobile_scope
    assert 'return {action:"put-away", command:`Put away ${demonstrativeMove[1]}`}' in mobile_scope
    assert 'pendingInputSource = "voice"' in mobile
    assert "source:requestSource" in mobile
    assert 'source:entry.source || "mobile"' in mobile
    assert 'setComposerStatus("Listening for a find or put-away")' in mobile


def test_mobile_voice_can_finish_a_pending_putaway_without_expanding_scope() -> None:
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert 'title="Speak a find, put-away, or confirmation"' in mobile
    assert "if (pendingPlan)" in mobile
    assert 'return {action:"confirm", command:clean}' in mobile
    assert 'return {action:"cancel", command:clean}' in mobile
    assert 'return {action:"revise", command:clean}' in mobile
    assert "async function applyBrowserVoiceCommand(routed)" in mobile
    assert "await confirmPlan(planNode)" in mobile
    assert "await cancelPlan(planNode)" in mobile
    assert 'await submitRevision("voice")' in mobile
    assert 'async function submitRevision(source = "mobile")' in mobile
    assert "JSON.stringify({text:followup, source})" in mobile
    assert (
        "Voice handles find, put-away, and their confirmations. "
        "Type other requests or open Storage."
    ) in mobile
    assert 'href="/storage?view=places">Open Places</a>' in mobile


def test_mobile_local_confirmation_suppresses_the_polled_duplicate_receipt() -> None:
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert "const sourceInteractionId = Number(pendingSourceInteractionId || 0);" in mobile
    assert (
        "if (sourceInteractionId) "
        "summarizedConfirmationSources.add(sourceInteractionId);"
    ) in mobile
    assert (
        "if (sourceInteractionId) "
        "summarizedConfirmationSources.delete(sourceInteractionId);"
    ) in mobile
    assert (
        "if (summarizedConfirmationSources.has(Number(interaction.id || 0))) "
        "return;"
    ) in mobile


def test_mobile_assistant_uses_shared_desktop_page_framing() -> None:
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert (
        'class="assistant-page-heading product-page-heading" '
        'aria-labelledby="assistantPageTitle"' in mobile
    )
    assert '<h2 id="assistantPageTitle">Ask Magpie</h2>' in mobile
    assert ".assistant-page-heading { display:none; }" in mobile
    assert (
        ".assistant-page-heading { grid-area:heading; width:100%; display:flex; "
        "margin-inline:0; }"
        in mobile
    )


def test_assistant_renders_one_tap_cross_root_choices() -> None:
    desktop = INDEX_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")

    assert "res.location_candidates || []" in desktop
    assert (
        'data-agent-location-command="${escapeHtml(candidate.selection_command)}"'
        in desktop
    )
    assert 'setStatus(locationChoices ? "Choose a storage place" : "Done")' in desktop
    assert "locationChoiceRows(result.location_candidates)" in mobile
    assert (
        'data-location-command="${escapeHtml(candidate.selection_command)}"'
        in mobile
    )


def test_account_exposes_hardware_interfaces_without_voice_place_setup() -> None:
    account = ACCOUNT_HTML.read_text(encoding="utf-8")

    assert 'id="hardwareInterfacesSection"' in account
    assert 'aria-label="Hardware interfaces"' in account
    assert 'href="/kiosk">Kiosk</a>' in account
    assert '<a class="button secondary" href="/kiosk?mode=voice-check">Voice check</a>' in account
    assert 'href="/cabinet">Cabinet console</a>' in account
    assert 'href="/check-station">Check station</a>' in account
    assert '<option value="check_station">Check station</option>' not in account
    assert "check_station: [\"check_station\"]" not in account
    assert "check_station: {label:\"Check station\"" not in account
    assert "create place" not in account[
        account.index('id="hardwareInterfacesSection"'):
        account.index('id="devicesSection"')
    ].lower()


def test_kiosk_voice_check_mode_guides_acceptance_without_idle_polling() -> None:
    html = KIOSK_HTML.read_text(encoding="utf-8")

    assert 'kioskParams.get("mode") === "voice-check"' in html
    assert "async function loadVoiceAcceptance()" in html
    assert 'voiceAcceptance = await api("/voice/acceptance")' in html
    assert "function renderVoiceCheck(voice)" in html
    assert 'byId("stateLabel").textContent = ready ? "Passed" : "Voice check"' in html
    assert "voiceAcceptance.prompt" in html
    assert "voiceAcceptance.instruction" in html
    assert "voiceAcceptance.remaining" in html
    assert "voiceAcceptance.required_passes" in html
    assert '(voice.active_finds || []).some((entry) => (entry.matches || []).length > 0)' in html
    assert "if (renderVoiceCheck(voice)) return;" in html
    assert "voiceCheckMode && showFreshHandoffs && (interactions || []).length" in html
    assert html.count('api("/voice/acceptance")') == 1


def test_check_station_lights_selected_and_saved_destinations() -> None:
    station = CHECK_STATION_HTML.read_text(encoding="utf-8")

    assert "let lightRequestId = 0" in station
    assert "async function lightSelectedLocation(locationId)" in station
    assert (
        'api(`/locations/${encodeURIComponent(locationId)}/light`, '
        '{method:"POST"})'
    ) in station
    assert 'if (selectedLocationId) lightSelectedLocation(selectedLocationId)' in station
    assert (
        'result.lit ? "Inventory updated and destination lit" : '
        '"Inventory updated"'
    ) in station


def test_voice_putaway_can_resume_after_ui_only_place_creation() -> None:
    desktop = INDEX_HTML.read_text(encoding="utf-8")
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert 'res.action === "place_configuration_ui_required" && res.places_url' in desktop
    assert "Choose the destination in Places" in desktop
    assert "Create or choose a destination" in desktop
    assert '["place_configuration_ui_required", "voice_scope_ui_required"].includes(result.action)' in mobile
    assert 'const placeHandoffUrl = result.places_url || result.storage_url || ""' in mobile
    assert 'href="${escapeHtml(placeHandoffUrl)}">Open Places</a>' in mobile
    assert 'result.action === "place_configuration_ui_required" ? "Destination needed" : "Use Places"' in mobile
    assert 'const placeHandoff = ["place_configuration_ui_required", "voice_scope_ui_required"].includes(result.action)' in mobile
    assert 'const resumeInteractionId = Number(params.get("resume") || 0)' in mobile
    assert "await api(`/assistant/handoffs/${resumeInteractionId}`)" in mobile
    assert 'pendingPhotos = (handoff.photo_ids || []).map((id) => ({id:Number(id)}))' in mobile
    assert 'await submit("put-away")' in mobile

    assert 'id="putAwayHandoff" class="putaway-handoff"' in storage
    assert 'id="putAwayHandoffState">Put-away paused' in storage
    assert 'id="chooseHandoffDestination"' in storage
    assert 'const initialResumeInteractionId = Number(initialParams.get("resume") || 0)' in storage
    assert "async function loadPutAwayHandoff()" in storage
    assert "await api(`/assistant/handoffs/${initialResumeInteractionId}`)" in storage
    assert "function resumePutAwayUrl(locationId)" in storage
    assert "function markPutAwayDestinationReady(locationId)" in storage
    assert 'byId("putAwayHandoffState").textContent = "Destination ready"' in storage
    assert 'byId("createHandoffDestination").textContent = "Continue here"' in storage
    assert 'byId("chooseHandoffDestination").textContent = "Choose different"' in storage
    assert "const createdForPutAway = destinationOnboardingMode && Boolean(resumePutAway)" in storage
    assert "if (createdForPutAway) markPutAwayDestinationReady(response.id)" in storage
    assert "if (resumePutAway) markPutAwayDestinationReady(focus.id)" in storage
    assert "function chooseExistingPutAwayDestination()" in storage
    assert 'placeCatalogScope = "destinations"' in storage
    assert '`Choose existing (${existingDestinationCount})`' in storage
    assert 'Continue put-away here</a>' in storage
    assert "showArchived || resumePutAway" in storage
    assert 'resumePutAway ? "Continue put-away here" : "Put something here"' in storage
    assert 'openStorageSetup({destination:suggestion})' in storage
    assert "if (!roots.length) openStorageSetup({destination:suggestion});" in storage
    assert "suggestedParent || contextualParent || onlyRoot || null" in storage
    assert "{destinationOnly:true}" in storage
    assert "suggestedParent || selectedId || currentRootId || null" not in storage
    assert "const requestedDestination = options?.destination || null" in storage
    assert "storageSetupDestinationNameTouched = true" in storage
    assert 'id="storageSetupDestinationCapacity"' in storage
    assert "requestedDestination?.capacity_class" in storage
    assert 'capacity_class:byId("storageSetupDestinationCapacity").value' in storage
    assert "draft.capacity_class" in storage
    assert 'byId("locationCapacity").value = String(draft.capacity_class)' in storage
    assert 'byId("dialogTitle").textContent = "Create put-away destination"' in storage
    assert 'byId("createLocationSubmit").textContent = destinationOnboardingMode' in storage
    assert "function launchDestinationLocked()" in desktop
    assert "destination_locked: launchDestinationLocked()" in desktop
    assert "current_location_id:selectedBin || null, destination_locked:launchDestinationLocked()" in desktop
    assert 'currentLocationId && new URLSearchParams(location.search).get("resume")' in mobile
    assert "destination_locked:destinationLocked" in mobile
    assert "destination_locked:Boolean(entry.destination_locked)" in mobile
    assert "destination_locked:Boolean(destinationLocked)" in mobile


def test_photo_backed_commands_route_to_agent() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "if (pendingPhotos.length) {" in html
    assert "await runAgentCommand(text, {source: requestSource, throwOnError: options.throwOnError});" in html
    assert (
        'runAgentCommand(PHOTO_ONLY_COMMAND, '
        '{commandText: "photo", mode: "agent", source: requestSource, throwOnError: options.throwOnError})' in html
    )
    assert "PHOTO_ONLY_COMMAND" in html


def test_desktop_assistant_uses_fast_router_and_unwraps_ask_responses() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'mode: options.mode || "auto"' in html
    assert "const res = chat.decision || chat.raw || chat" in html
    assert 'res.action === "put_away" && res.item' in html
    assert 'res.action === "clarify_item_identity"' in html
    assert 'mode: "agent"' in html
    assert "function assistantErrorMessage" in html
    assert "error?.status === 429" in html
    assert "parsed.detail" in html
    assert "Nothing was changed." in html


def test_agent_waiting_state_and_pending_plan_buttons_stay_usable() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function appendWorking" in html
    assert "Asking the storage agent." in html
    assert "const workingNode = appendWorking()" in html
    assert "if (workingNode.isConnected) workingNode.remove()" in html
    assert "pendingPlanNode && pendingPlanNode.contains(b)" in html
    assert "pending-plan-card" in html
    assert 'options.replaceNode ? "Updated proposal."' in html


def test_pending_put_item_plans_are_editable_before_confirm() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "renderPlanSteps(plan, colorPlan, {editable: true}, presentation)" in html
    assert "function confirmationRow(presentation, index)" in html
    assert "function planColorAssignments(plan, presentation = null)" in html
    assert "function renderPlanSteps(plan, colorPlan, options = {}, presentation = null)" in html
    assert "renderPendingPlan(res.plan, displayMessage, options.replaceNode, res.trace || [], res.confirmation)" in html
    assert "renderPendingPlan(response.plan, response.message || \"Voice proposed a storage plan.\", null, response.trace || [], response.confirmation)" in html
    assert 'presentation?.instruction || message' in html
    assert "shared?.destination?.id || s?.arguments?.location_id" in html
    assert "if (shared && !shared.color) return null;" in html
    assert "function renderPutItemPlanEditor" in html
    assert 'data-plan-field="item"' in html
    assert 'data-plan-field="quantity"' in html
    assert 'data-plan-field="unit"' in html
    assert 'data-plan-field="location_id"' in html
    assert 'data-plan-field="needs_review"' in html
    assert 'type="checkbox"${reviewChecked}' in html
    assert "function updatePendingPlanField" in html
    assert 'field === "needs_review"' in html
    assert "args[field] = Boolean(input.checked)" in html
    assert "function removePendingPlanStep" in html
    assert "function mergePendingPlanStep" in html
    assert "input.oninput = () => updatePendingPlanField(input)" in html
    assert "Merge up" in html
    assert "Removed proposal row" in html
    assert "Merged proposal rows" in html


def test_review_queue_can_edit_or_remove_uncertain_items() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-edit-review-item="${escapeHtml(item.item_id)}"' in html
    assert 'data-remove-item="${escapeHtml(item.item_id)}" data-remove-from="${escapeHtml(item.bin_id)}"' in html
    assert "function startReviewEditItem" in html
    assert "function reviewMetaHtml(item)" in html
    assert 'Review: ${escapeHtml(item.review_reason || "not checked")}' in html
    assert 'returnView: "review"' in html
    assert 'options.returnView === "review"' in html
    assert 'if (currentView !== "review") await refreshCurrentView()' in html
    assert "button.onclick = () => startReviewEditItem(button.dataset.editReviewItem)" in html
    assert 'Learned: ${response.approved.name} looks right' in html


def test_inventory_review_presents_three_explicit_placement_resolutions() -> None:
    storage = STORAGE_HTML.read_text(encoding="utf-8")

    assert "Confirm it is here" in storage
    assert "Move to correct place" in storage
    assert "Remove placement" in storage
    assert "Archive missing item" in storage
    assert "group.placements.length === 1" in storage
    assert 'placement-row${item.needs_review ? " needs-review" : ""}' in storage
    assert 'Reported issue: ${escapeHtml(item.review_reason' in storage
    assert '${inventoryScope === "review" ? "Resolve" : "Details"}' in storage
    assert 'id="removeItemTitle"' in storage
    assert 'id="removeItemNote"' in storage
    assert 'resolving_review:resolvingReview' in storage
    assert '"Remove uncertain placement"' in storage
    assert 'querySelectorAll("[data-detail-lifecycle]")' in storage
    assert "The action remains in history and can be undone." in storage


def test_remove_item_uses_inline_confirmation_not_native_dialog() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function confirmInlineRemove" in html
    assert 'button.textContent = "Confirm remove"' in html
    assert "Tap again to remove from" in html
    assert 'button.textContent = "Removing"' in html
    assert "removeItemFromLocation(button.dataset.removeItem, button.dataset.removeFrom, button)" in html
    assert "confirm(`Remove this item from" not in html


def test_pending_plan_cancel_records_source_interaction() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "function cancelPendingPlan" in html
    assert 'const result = await api("/assistant/cancel"' in html
    assert "source_interaction_id: sourceInteractionId" in html
    assert "const photoIds = pendingPlanPhotoIds.slice()" in html
    assert "renderActiveFinds(result.active_finds)" in html


def test_mobile_cancel_removes_pending_putaway_from_current_finds() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")

    assert 'const result = await api("/assistant/cancel"' in html
    assert "activeFinds = result.active_finds" in html
    assert "renderLights();" in html
    assert "renderLightList" not in html


def test_mobile_keeps_new_proposal_during_transient_empty_kiosk_state() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")
    sync_start = html.index(
        "function syncAuthoritativePending(interaction, confirmationState = null)"
    )
    sync_end = html.index("async function pollState()", sync_start)
    sync = html[sync_start:sync_end]

    assert "if (authoritativeId) {" in sync
    assert sync.index("if (authoritativeId) {") < sync.index("if (!pendingPlan) return;")
    assert "confirmationState?.interaction_id" in sync
    assert '["pending", "confirming", "unknown"].includes(status)' in sync
    assert 'setComposerStatus("Proposal expired")' in sync
    assert "state.pending_agent_plan || state.pending_confirmation || null" in html
    assert "confirmation_interaction_id:String(pendingSourceInteractionId || 0)" in html
    assert "Number(interaction.id || 0) === Number(pendingSourceInteractionId || 0)" in html


def test_web_and_mobile_clear_only_authoritatively_terminal_proposals() -> None:
    mobile = MOBILE_HTML.read_text(encoding="utf-8")
    web = INDEX_HTML.read_text(encoding="utf-8")
    kiosk = KIOSK_HTML.read_text(encoding="utf-8")

    assert "state.confirmation_state || null" in mobile
    assert "res.confirmation_state || null" in web
    assert "requestedStatus" in web
    assert '["pending", "confirming", "unknown"].includes(requestedStatus)' in web
    assert '["pending", "confirming", "unknown"].includes(status)' in kiosk
    assert "requestedId !== localId" in web
    assert "confirmation_interaction_id: String(pendingSourceInteractionId || 0)" in web


def test_mobile_reserves_the_measured_dock_for_confirmation_actions() -> None:
    html = MOBILE_HTML.read_text(encoding="utf-8")
    render_start = html.index("function renderLights()")
    render_end = html.index("async function reportFindNotHere", render_start)
    render = html[render_start:render_end]

    assert "padding:28px var(--page-gutter) calc(var(--dock-space) + 12px)" in html
    assert "scroll-padding-bottom:240px" not in html
    assert "padding:28px var(--page-gutter) 194px" not in html
    assert "requestAnimationFrame(updateDockSpace);" in render


def test_additive_put_plan_is_labeled_as_an_increment() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'a.quantity_mode === "add"' in html
    assert "return `Add ${escapeHtml(String(a.quantity || 1))}" in html


def test_manual_inventory_form_is_fallback_disclosure() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert '<details class="panel manual-entry advanced-only">' in html
    assert '<summary>Add / Update Inventory</summary>' in html
    assert 'details.manual-entry summary::after' in html


def test_measured_item_dimensions_are_used_for_placement() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'api("/measurement/photo"' in html
    assert 'api("/placement/suggest"' in html
    assert "dimensions: measuredDimensions" in html


def test_all_browser_surfaces_load_shared_auth_session_shell() -> None:
    for name in ("index.html", "storage.html", "account.html"):
        html = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert '<script src="/auth.js"></script>' in html

    script = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    assert 'window.fetch = async function magpieAuthenticatedFetch' in script
    assert 'window.fetch("/client/bootstrap"' in script
    assert "window.magpieAuthReady = new Promise" in script
    assert "window.magpieClientBootstrap = null" in script
    assert "window.magpieAuthConfig = null" in script
    assert "/auth/v1/token?grant_type=refresh_token" in script
    assert "/auth/v1/otp?redirect_to=" in script
    assert 'body: JSON.stringify({email, create_user: true})' in script
    assert 'headers.set("x-magpie-workspace", workspaceId)' in script
    assert 'window.fetch("/workspaces", {' in script
    assert (
        'saveSession(null);\n'
        '        saveWorkspace("");\n'
        '        location.assign("/");'
    ) in script
    assert (
        "saveWorkspace(body.workspace.id);\n"
        '        location.assign("/storage?onboarding=1");'
    ) in script
    assert 'add.title = "Create workspace"' in script
    assert "Create your Magpie workspace" in script
    assert "You will add the physical storage spaces next." in script
    assert 'class="magpie-auth-step"' in script
    assert '${canCancel ? "New workspace" : "Account setup"} · Step 1 of 2' in script
    assert '${canCancel ? "Cancel" : "Sign out"}' in script
    assert 'class="magpie-auth-delete"' in script
    assert 'window.fetch("/account", {method: "DELETE"})' in script
    assert 'saveSession(null)' in script
    assert "background:var(--bg,#f5f6f8)" in script
    assert "background:var(--surface,#fff)" in script
    assert "color:var(--text,#171a1f)" in script
    assert "border:1px solid var(--line,#d7dfdc)" in script
    assert "outline:3px solid var(--focus-ring,#8dc8be)" in script
    assert "@media (pointer:coarse)" in script
    assert "background:#f5f6f8" not in script
    assert "border:1px solid #d9dee7" not in script
    assert "color:#303640" not in script
    assert "window.magpieAuthReady || Promise.resolve()" in (
        STATIC_DIR / "index.html"
    ).read_text(encoding="utf-8")
    assert "window.magpieAuthReady || Promise.resolve(null)" in (
        STATIC_DIR / "storage.html"
    ).read_text(encoding="utf-8")
    account = (STATIC_DIR / "account.html").read_text(encoding="utf-8")
    assert "window.magpieAuthReady || Promise.resolve()" in account
    assert "window.magpieClientBootstrap || {}" in account
    assert "window.magpieAuthConfig || await" in account
    assert 'api("/workspace/members")' in account
    assert 'api("/workspace/invitations"' in account
    assert 'method:"PATCH"' in account
    assert 'method:"DELETE"' in account
    assert "window.magpieAuthConfig?.edge_url || origin" in account
    assert "shellQuote(centralUrl)" in account


def test_auth_startup_preserves_session_during_service_failures() -> None:
    script = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")

    assert "function showAuthUnavailable(message)" in script
    assert 'shell.id = "magpieAuthUnavailable"' in script
    assert "Your session and workspace are still saved." in script
    assert ">Try again</button>" in script
    assert "configPromise = null;" in script
    assert "await initialize();" in script
    assert "showAuthUnavailable(error.message);" in script

    initialize = script[script.index("async function initialize()") :]
    failure_branch = initialize[
        initialize.index("if (!response.ok)") : initialize.index(
            "const body = await response.json();"
        )
    ]
    assert 'response.status === 401' in failure_branch
    assert failure_branch.count("saveSession(null);") == 1
    assert "showAuthUnavailable(" in failure_branch
    assert "Magpie could not start" in failure_branch
