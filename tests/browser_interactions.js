async function runBrowserInteractionChecks(sources) {
  const passed = [];
  for (const source of Object.values(sources)) new Function(source);
  function check(condition, message) {
    if (!condition) throw new Error(message);
  }
  function bind(names, dependencies, extraExports = "") {
    return new Function(...Object.keys(dependencies),
      names.map((name) => sources[name]).join("\n") + `\nreturn {${names.join(",")},${extraExports}};`
    )(...Object.values(dependencies));
  }
  const noop = () => {};

  for (const [pendingPlan, commandBusy] of [[{}, false], [null, true], [null, false]]) {
    let cleared = 0;
    let revealed = 0;
    const {clearChat} = bind(["clearChat"], {
      pendingPlan, commandBusy, lastAssistantContext: {}, setStatus: noop,
      finishHandoff: noop,
      revealCabinetAssistant: () => revealed++, renderAssistantEmpty: () => cleared++,
    });
    clearChat();
    check(cleared === Number(!pendingPlan && !commandBusy), "Clear discarded active work");
    check(revealed === Number(Boolean(pendingPlan)), "Pending proposal was not revealed");
  }
  passed.push("Clear preserves pending and in-flight work");

  function editFixture() {
    const fields = Object.fromEntries(Object.entries({
      editName: "Screws", editQty: "0", editUnit: "each", editCategory: "hardware",
      editAliases: "bolt, fastener", editSize: "", editDimensions: "", editDescription: "",
    }).map(([id, value]) => [id, {value, disabled: false}]));
    const status = {textContent: ""};
    const edit = {
      dirty: true, saving: false, returnLocation: "nested drawer",
      target: {
        querySelector: () => status, querySelectorAll: () => Object.values(fields),
        setAttribute: noop, removeAttribute: noop,
      },
    };
    const destinations = [];
    const dependencies = {
      activeItemEdit: edit, inventoryMutationBusy: false, byId: (id) => fields[id], setStatus: noop,
      parseDimensionText: () => null, loadReviewQueue: async () => {},
      document: {body: {classList: {remove: noop}}},
      showReviewView: () => destinations.push("review"),
      selectChildLocation: async (id) => destinations.push(id),
      selectBin: async (id) => destinations.push(id),
    };
    return {fields, status, edit, destinations, dependencies};
  }
  {
    const fixture = editFixture();
    let finish;
    const payloads = [];
    const api = async (_url, options) => {
      payloads.push(JSON.parse(options.body));
      return new Promise((resolve) => { finish = resolve; });
    };
    const actions = bind(["submitEditItem", "leaveItemEdit"], {...fixture.dependencies, api});
    const save = actions.submitEditItem(42, "actual placement", "location");
    await actions.submitEditItem(42, "actual placement", "location");
    check(payloads.length === 1, "Save was submitted twice");
    check(payloads[0].bin_id === "actual placement", "Save lost placement identity");
    check(payloads[0].quantity === 0, "Save changed zero quantity");
    check(!actions.leaveItemEdit(), "Navigation was allowed during Save");
    check(Object.values(fixture.fields).every((field) => field.disabled), "Saving fields stayed enabled");
    finish({ok: true});
    await save;
    check(fixture.destinations[0] === "nested drawer", "Save returned to a different selection");
    check(actions.leaveItemEdit(), "Successful Save retained the navigation lock");
    check(Object.values(fixture.fields).every((field) => !field.disabled), "Save did not release controls");
  }
  passed.push("Save serializes requests and preserves placement and return location");
  {
    const fixture = editFixture();
    const actions = bind(["submitEditItem", "leaveItemEdit"], {
      ...fixture.dependencies, api: async () => { throw new Error("Offline"); },
    });
    check(!actions.leaveItemEdit(), "Dirty draft could be discarded by navigation");
    await actions.submitEditItem(42, "B001");
    check(fixture.status.textContent === "Offline", "Save error not shown beside draft");
    check(fixture.fields.editName.value === "Screws", "Failed Save lost draft data");
    check(fixture.destinations.length === 0, "Failed Save navigated away");
    check(!fixture.edit.saving && !fixture.fields.editName.disabled, "Failed Save could not be retried");
    check(!actions.leaveItemEdit(), "Failed Save cleared draft protection");
  }
  passed.push("Failed Save preserves editable draft and shows its error");

  {
    const deferred = [];
    const fixture = editFixture();
    const actions = bind(["renderContainer", "leaveItemEdit"], {
      ...fixture.dependencies, activeItemEdit: null, currentView: "storage",
      currentContainer: "drawer", containerRenderGeneration: 0,
      api: () => new Promise((resolve) => deferred.push(resolve)),
      updateLightControls: () => { throw new Error("Stale container response rendered"); },
    }, "setView: (view) => { currentView = view; }, setEdit: (edit) => { activeItemEdit = edit; }");
    const first = actions.renderContainer("drawer");
    const second = actions.renderContainer("drawer");
    deferred[0]({});
    await first;
    actions.setView("review");
    deferred[1]({});
    await second;
    actions.setView("storage");
    const third = actions.renderContainer("drawer");
    actions.setEdit(fixture.edit);
    deferred[2]({});
    await third;
    fixture.edit.target.querySelector = () => null;
    check(!actions.leaveItemEdit(), "Missing editor status broke draft protection");
  }
  passed.push("Late container responses respect request generation, view and active edit");

  {
    const fixture = editFixture();
    let deleted = 0;
    const locked = bind(["clearBinInventory", "leaveItemEdit"], {
      ...fixture.dependencies, selectedBin: "B001", api: async () => deleted++, confirm: () => true,
    });
    await locked.clearBinInventory();
    fixture.edit.saving = true;
    await locked.clearBinInventory();
    check(deleted === 0, "Clear Inventory deleted an active edit or saving placement");
    let finish;
    const button = {disabled: false};
    const clearing = bind(["clearBinInventory", "leaveItemEdit"], {
      ...fixture.dependencies, activeItemEdit: null, selectedBin: "B001", confirm: () => true,
      byId: () => button, api: () => new Promise((resolve) => { finish = resolve; }),
    });
    const request = clearing.clearBinInventory();
    check(button.disabled && !clearing.leaveItemEdit(), "Clear allowed concurrent editing");
    finish({removed: 1});
    await request;
    check(!button.disabled && clearing.leaveItemEdit(), "Clear retained its mutation lock");
  }
  passed.push("Clear Inventory and item editing cannot mutate concurrently");

  {
    const nodes = Object.fromEntries(["assistantRetryBtn", "assistantMeta", "layoutMeta"].map((id) => [id, {}]));
    let assistantBroken = true;
    let rendered = 0;
    let loaded = 0;
    const actions = bind(["loadAssistantStatus", "load"], {
      byId: (id) => nodes[id], cabinetHardwareOwner: false, currentBuildLabel: "", bins: [], pixelCount: 0,
      api: async (url) => {
        if (url === "/assistant/status") {
          if (assistantBroken) throw new Error("Unavailable");
          return {ready: true};
        }
        if (url === "/health") return {runtime: {}, backend: "none"};
        if (url === "/layout") return {bins: []};
        throw new Error(`Unexpected URL ${url}`);
      },
      assistantStatusLabel: () => "Ready", cabinetLayoutMeta: () => "Cabinet",
      renderGrid: () => rendered++, updateLightControls: noop, setStatus: noop,
      loadContainers: async () => loaded++, loadReviewQueue: async () => {}, loadMoveTargets: async () => {},
    });
    await actions.load();
    check(rendered === 1 && loaded === 1, "Assistant status failure blocked storage");
    check(nodes.assistantRetryBtn.hidden === false, "Assistant retry is missing");
    assistantBroken = false;
    await actions.loadAssistantStatus();
    check(nodes.assistantRetryBtn.hidden === true, "Assistant retry did not recover");
  }
  passed.push("Assistant status failure and retry are independent of storage startup");

  {
    const {resultRows, escapeHtml} = bind(["resultRows", "escapeHtml"], {
      COLORS: [{rgb: [1, 2, 3]}], pathLabel: (result) => result.location_id,
    });
    const rows = resultRows(Array.from({length: 11}, (_, index) => ({
      item_id: index, name: `Item ${index} <script>`, location_id: "A&B",
      quantity: index, unit: "each",
    })));
    check((rows.match(/class="result-row"/g) || []).length === 11, "Mobile results were truncated");
    check(rows.includes("item=10"), "Last mobile result has no item link");
    check(rows.includes("0 each"), "Zero quantity was omitted");
    check(rows.includes(escapeHtml("<script>")) && !rows.includes("<script>"), "Result text was not escaped");
  }
  passed.push("Mobile results show every match, quantities and escaped item links");
  return passed;
}
