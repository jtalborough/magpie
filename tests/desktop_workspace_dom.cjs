// DOM/state checks only. Run desktop_workspace.cjs separately for rendered geometry.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const html = fs.readFileSync(path.join(__dirname,'../src/searchable_storage_led/static/index.html'),'utf8');
for (const [,script] of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) new vm.Script(script);
const cleanHtml = html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi,'');
const names = ['initializeCabinetWorkspace','initializeDesktopWorkspace','revealCabinetAssistant','compactCabinetItemActions'];
const source = names.map(name => html.match(new RegExp(`^    function ${name}\\(.*?^    \\}`,'ms'))[0]).join('\n');
const routing = html.match(/<script>\s*(window\.magpieMobileRedirect[\s\S]*?)<\/script>/)[1];
for (const [pathname,narrow,expected] of [['/',true,'/mobile?resume=abc#pending'],['/',false,null],['/cabinet',true,null]]) {
  let destination = null;
  const window = {location:{pathname,search:'?resume=abc',hash:'#pending',replace:value => destination=value},matchMedia:() => ({matches:narrow})};
  vm.runInNewContext(routing,{window});
  assert.equal(destination,expected,'Initial route must preserve mobile handoff context');
}

(async () => {
  const workspaceHandoffSource = html.match(/^    async function openWorkspaceAssistant\(.*?^    \}/ms)[0];
  const checkWorkspaceHandoff = new (Object.getPrototypeOf(async function(){}).constructor)('draft','photoCount','hasPlan','doubleClick', `
    const command={value:draft,focus(){}};
    const byId=()=>command, leaveItemEdit=()=>true, revealCabinetAssistant=()=>{}, setStatus=()=>{};
    const commandBusy=false, pendingPlan=hasPlan, pendingPhotos=Array(photoCount);
    const window={location:{href:'https://magpie.test/'}};
    let initialHandoffLoaded=true, workspaceHandoffBusy=false, calls=[];
    const history={pushState:()=>calls.push('url')};
    const setupInitialLocationFromUrl=async()=>{ calls.push('location'); await new Promise(resolve=>setTimeout(resolve,5)); };
    const setupInitialHandoffFromUrl=async()=>false;
    const setupInitialActionFromUrl=()=>calls.push('action');
    const setupInitialPromptFromUrl=()=>calls.push('prompt');
    ${workspaceHandoffSource}
    const first=openWorkspaceAssistant(new URL('https://magpie.test/mobile?prompt=Put+away&location=B019'));
    if(doubleClick) await openWorkspaceAssistant(new URL('https://magpie.test/mobile?prompt=Another&location=B020'));
    await first;
    return calls;
  `);
  assert.deepEqual(await checkWorkspaceHandoff('Existing draft',0,false),[]);
  assert.deepEqual(await checkWorkspaceHandoff('',1,false),[]);
  assert.deepEqual(await checkWorkspaceHandoff('',0,true),[]);
  assert.deepEqual(await checkWorkspaceHandoff('',0,false),['url','location','action','prompt']);
  assert.deepEqual(await checkWorkspaceHandoff('',0,false,true),['url','location','action','prompt'],'Concurrent handoffs must not change the destination');
  const storage = fs.readFileSync(path.join(__dirname,'../src/searchable_storage_led/static/storage.html'),'utf8');
  const managerHandoff = storage.match(/^    function navigateToAssistant\(.*?^    \}/ms)[0];
  for (const embedded of [true,false]) {
    const destinations = [];
    const window = {location:{href:'/storage'},parent:{MagpieWorkspace:{openAssistant:url=>destinations.push(url)}}};
    vm.runInNewContext(`${managerHandoff}; navigateToAssistant('/?resume=7&location=B019');`, {
      window,document:{documentElement:{classList:{contains:()=>embedded}}},
    });
    assert.equal(window.location.href,embedded ? '/storage' : '/?resume=7&location=B019');
    assert.equal(destinations.length,embedded ? 1 : 0);
  }
  const handoffSource = html.match(/^    async function setupInitialHandoffFromUrl\(.*?^    \}/ms)[0];
  const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
  for (const [search,selectedBin,expectedRequests] of [
    ['?resume=7&location=B019','B019',1],
    ['?resume=7&location=B019',null,0],
    ['?resume=7&location=B019','B020',0],
    ['',null,0],
  ]) {
    const requests = [];
    const commands = [];
    const command = {value:''};
    const verify = new AsyncFunction('window','URLSearchParams','selectedBin','api','byId','runCommand','renderPendingPhotos','setStatus',
      `let initialHandoffLoaded=false; let activeHandoffLocationId=''; let pendingPhotos=[]; ${handoffSource}; await setupInitialHandoffFromUrl(); await setupInitialHandoffFromUrl(); return pendingPhotos;`);
    const photos = await verify({location:{search}},URLSearchParams,selectedBin,
      async url => { requests.push(url); return {resume_command:'Put away M3 flat washers',photo_ids:[9,9,0,-1,'bad']}; },
      () => command,async () => commands.push(command.value),() => {},() => {});
    assert.equal(requests.length,expectedRequests);
    assert.equal(commands.length,expectedRequests,'Handoff retries must not submit duplicate proposals');
    if (expectedRequests) {
      assert.deepEqual(photos,[{id:9,url:'/photos/9'}]);
      assert.equal(commands[0],'Put away M3 flat washers');
      assert.equal(requests[0],'/assistant/handoffs/7');
    }
  }
  const lifecycleSource = ['setupInitialHandoffFromUrl','runCommand','runAgentCommand','launchDestinationLocked','finishHandoff','clearPendingPlan','confirmPlan','cancelPendingPlan']
    .map(name => html.match(new RegExp(`^    (?:async )?function ${name}\\(.*?^    \\}`,'ms'))[0]).join('\n');
  for (const ending of ['confirm','cancel']) {
    const requests = [];
    let fail = true;
    const window = {location:new URL('https://magpie.test/?resume=7&location=B019')};
    const history = {replaceState:(_state,_title,url) => window.location = new URL(url,window.location)};
    const exercise = new AsyncFunction('assert','window','history','URL','URLSearchParams','api','ending', `
      let initialHandoffLoaded=false, activeHandoffLocationId='', selectedBin='B019';
      let pendingPhotos=[], pendingPlan=null, pendingPlanNode=null, pendingPlanPhotoIds=[], pendingSourceInteractionId=null, pendingCommand='';
      let commandBusy=false, nextCommandSource='ui', lastSeenAssistantInteractionId=0;
      const command={value:'',placeholder:''};
      const byId=()=>command, escapeHtml=x=>x, setStatus=()=>{}, renderError=()=>{}, renderPendingPhotos=()=>{};
      const setCommandBusy=value=>commandBusy=value, clearCommandInput=()=>command.value='';
      const disablePastActions=()=>{}, appendChat=()=>{}, appendWorking=()=>({isConnected:true,remove(){}});
      const maybeHandleConfirmedPlanCorrection=async()=>false, isContextFollowup=()=>false;
      const renderPendingPlan=()=>({}), rememberAssistantContext=()=>{}, planColorAssignments=()=>[];
      const renderAppliedPlan=()=>{}, lightAllPlanSteps=async()=>{}, loadContainers=async()=>{}, loadReviewQueue=async()=>{};
      ${lifecycleSource}
      await assert.rejects(setupInitialHandoffFromUrl(),/network failed/);
      assert.equal(initialHandoffLoaded,false);
      assert.equal(command.value,'Put away M3 flat washers');
      assert.equal(pendingPhotos[0].id,9);
      assert.equal(commandBusy,false);
      await setupInitialHandoffFromUrl();
      assert.equal(initialHandoffLoaded,true);
      assert.equal(pendingPlan.length,1);
      assert.equal(launchDestinationLocked(),true);
      await setupInitialHandoffFromUrl();
      if(ending==='confirm') await confirmPlan({querySelector:()=>null});
      else await cancelPendingPlan({remove(){}});
      assert.equal(pendingPlan,null);
      assert.equal(launchDestinationLocked(),false);
      assert.equal(new URLSearchParams(window.location.search).has('resume'),false);
      command.value='Put away M4 screws';
      await runCommand();
    `);
    await exercise(assert,window,history,URL,URLSearchParams,async(url,options) => {
      const body = options?.body ? JSON.parse(options.body) : null;
      requests.push({url,body});
      if (url.startsWith('/assistant/handoffs/')) return {resume_command:'Put away M3 flat washers',photo_ids:[9]};
      if (url === '/assistant/chat') {
        if (fail) { fail=false; throw new Error('network failed'); }
        return {interaction_id:8,plan:[{tool:'put_item',arguments:{name:'washer',location_id:'B019'}}]};
      }
      return {applied:[]};
    },ending);
    const chats = requests.filter(request => request.url === '/assistant/chat');
    assert.equal(chats.length,3);
    assert.equal(chats[1].body.destination_locked,true);
    assert.equal(chats[2].body.destination_locked,false);
    assert.equal(requests.filter(request => request.url === '/assistant/confirm').length,Number(ending==='confirm'));
  }
  for (const route of ['/', '/cabinet']) {
    for (const initialWidth of [390,1280]) {
      const dom = new JSDOM(cleanHtml,{url:`https://magpie.test${route}`,runScripts:'outside-only'});
      const {window:w} = dom;
      let width = initialWidth;
      const queries = new Map();
      w.matchMedia = query => {
        if (!queries.has(query)) queries.set(query,{
          matches:query.includes('min-width') ? width > 900 : width <= 900,
          listeners:[], addEventListener(_type,listener) { this.listeners.push(listener); },
        });
        return queries.get(query);
      };
      w.ResizeObserver = class { observe() {} };
      const doc = w.document;
      const command = doc.querySelector('#command');
      const photo = doc.querySelector('#photoBtn');
      const originalPhotoParent = photo.parentElement;
      command.value = 'Keep this draft and its attached photo';
      let photos = 0;
      photo.onclick = () => photos++;
      const pending = doc.createElement('section');
      pending.className = 'pending-plan-card';
      pending.textContent = 'Confirmation required';
      doc.querySelector('#results').append(pending);
      const attachment = doc.createElement('img');
      attachment.alt = 'Uploaded washer photo';
      doc.querySelector('#assistantPhotos').append(attachment);
      w.eval(`const byId = id => document.getElementById(id); const kioskMode = false; let selectedBin = null; const leaveItemEdit = () => true; const loadContainers = async () => {}; const setStatus = () => {}; ${source}; initializeCabinetWorkspace(); initializeDesktopWorkspace();`);
      w.eval(fs.readFileSync(path.join(__dirname,'../src/searchable_storage_led/static/product-shell.js'),'utf8'));
      w.MagpieProductShell.renderAll();
      const resize = async nextWidth => {
        width = nextWidth;
        const changed = [];
        for (const [query,media] of queries) {
          const matches = query.includes('min-width') ? width > 900 : width <= 900;
          if (matches !== media.matches) { media.matches = matches; changed.push(media); }
        }
        for (const media of changed) for (const fn of media.listeners) fn(media);
        await new Promise(resolve => setImmediate(resolve));
      };
      for (let cycle = 0; cycle < 3; cycle++) {
        command.focus();
        command.setSelectionRange(5,9,'forward');
        await resize(1280);
        assert(doc.body.classList.contains('desktop-workspace'));
        assert(doc.querySelector('.stack').compareDocumentPosition(doc.querySelector('.desktop-context')) & w.Node.DOCUMENT_POSITION_FOLLOWING,'Keyboard order must follow the left-to-right desktop layout');
        assert.equal(doc.querySelectorAll('#command').length,1);
        assert.equal(doc.querySelector('#command'),command);
        assert.equal(doc.activeElement,command);
        assert.equal(command.selectionStart,5);
        assert.equal(command.selectionEnd,9);
        assert.equal(doc.querySelector('#assistantPhotos img'),attachment);
        assert.equal(doc.querySelector('.pending-plan-card'),pending);
        assert.equal(doc.querySelector('.desktop-context #selectionPanel').hidden,false);
        assert.equal(doc.querySelector('#flashBinBtn').disabled,true);
        photo.click();
        assert.equal(photos,cycle+1);
        const more = doc.querySelector('.desktop-more');
        more.open = true;
        more.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
        assert.equal(more.open,false);
        more.open = true;
        command.click();
        assert.equal(more.open,false);
        doc.querySelector('#micBtn').textContent = 'Stop';
        doc.querySelector('#commandBtn').disabled = true;
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(doc.querySelector('#micBtn').getAttribute('aria-label'),'Stop recording');
        assert.equal(doc.querySelector('#commandBtn').getAttribute('aria-label'),'Working');
        command.focus();
        command.setSelectionRange(7,11,'backward');
        await resize(390);
        assert(!doc.body.classList.contains('desktop-workspace'));
        assert.equal(doc.querySelector('.desktop-composer'),null);
        assert.equal(photo.parentElement,originalPhotoParent);
        assert.equal(doc.querySelectorAll('#command').length,1);
        assert.equal(command.value,'Keep this draft and its attached photo');
        assert.equal(doc.activeElement,command);
        assert.equal(command.selectionStart,7);
        assert.equal(command.selectionEnd,11);
        assert.equal(command.selectionDirection,'backward');
        assert.equal(doc.querySelector('#assistantPhotos img'),attachment);
        assert.equal(doc.querySelector('.pending-plan-card'),pending);
        if (route === '/') assert.equal(doc.querySelector('#selectionPanel').hidden,true);
        assert.equal(doc.querySelector('#commandBtn').getAttribute('aria-label'),null);
      }
      await resize(1280);
      w.MagpieProductShell.renderAll();
      assert.equal(doc.querySelectorAll('[data-product-view="ask"], [data-product-view="cabinet"]').length,0);
      doc.querySelector('[data-product-view="inventory"]').click();
      assert(doc.querySelector('.desktop-context').classList.contains('manager-open'));
      assert.equal(doc.querySelector('[data-product-view="inventory"]').getAttribute('aria-current'),'page');
      const inventoryFrame = doc.querySelector('.workspace-manager iframe');
      assert.equal(inventoryFrame.getAttribute('src'),'/storage?view=inventory');
      assert.equal(new URLSearchParams(w.location.search).get('view'),'inventory');
      assert.equal(doc.querySelector('#command'),command);
      assert.equal(doc.querySelector('.pending-plan-card'),pending);
      doc.querySelector('[data-product-view="places"]').click();
      assert(!doc.querySelector('.desktop-context').classList.contains('manager-open'));
      assert.equal(doc.querySelector('[data-product-view="places"]').getAttribute('aria-current'),'page');
      doc.querySelector('[data-product-view="inventory"]').click();
      assert.equal(doc.querySelector('.workspace-manager iframe'),inventoryFrame,'Switching views must retain manager state');
      w.MagpieProductShell.setReviewCount(3);
      assert.equal(doc.querySelector('[data-product-view="review"]').textContent,'Needs review (3)');
      doc.querySelector('[data-product-view="places"]').click();
      doc.querySelector('#binDetail').innerHTML = '<div class="actions"><button data-open="B001">Open</button><button data-print-label="B001">Print</button></div><div class="result"><div class="actions"><button data-edit-item="1">Edit</button><button data-remove-item="1">Remove</button></div></div>';
      const print = doc.querySelector('[data-print-label]');
      w.eval('compactCabinetItemActions()');
      assert.equal(doc.querySelector('.desktop-location-more [data-print-label]'),print);
      assert.equal(doc.querySelector('.desktop-location-more [data-open]'),null);
      dom.window.close();
    }
  }
  console.log('PASS: desktop DOM ownership, three resize cycles from both initial widths, drafts/photos/proposals/handlers preserved, menu dismissal, accessible recording/busy state, location disclosure, full inline JS syntax.');
})().catch(error => { console.error(error); process.exitCode = 1; });
