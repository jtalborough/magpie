// Rendered fixture checks. These do not assert live API or physical LED behavior.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const staticDir = path.join(root, 'src/searchable_storage_led/static');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const artifactDir = process.env.DESKTOP_ARTIFACT_DIR || path.join(root,'.home');
fs.mkdirSync(artifactDir,{recursive:true});
const cleanHtml = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
const storageHtml = fs.readFileSync(path.join(staticDir,'storage.html'),'utf8');
const cleanStorage = storageHtml.replace(/<script\b[^>]*>([\s\S]*?)<\/script>/gi,
  (script,body) => body.includes('window.parent.location.origin') ? script : '');
const names = ['initializeCabinetWorkspace', 'initializeDesktopWorkspace', 'revealCabinetAssistant'];
const source = names.map(name => {
  const match = html.match(new RegExp(`^    function ${name}\\(.*?^    \\}`, 'ms'));
  assert(match, name);
  return match[0];
}).join('\n');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route('http://magpie.test/**', route => {
      const name = new URL(route.request().url()).pathname.slice(1);
      if (name === 'theme.css') return route.fulfill({contentType:'text/css', body:fs.readFileSync(path.join(staticDir,name))});
      if (name === 'storage') return route.fulfill({contentType:'text/html', body:cleanStorage});
      return route.fulfill({contentType:'text/html', body:cleanHtml});
    });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const route of ['/', '/cabinet']) {
      await page.setViewportSize({width:1440,height:900});
      await page.goto(`http://magpie.test${route}`);
      await page.addScriptTag({content:fs.readFileSync(path.join(staticDir,'product-shell.js'),'utf8')});
      await page.evaluate(() => {
        document.querySelector('#grid').innerHTML = Array.from({length:128}, (_,i) => `<button class="bin ${i >= 112 ? 'large' : 'small'}${i === 18 ? ' selected' : ''}" style="grid-column:span ${i >= 112 ? 2 : 1}" type="button">B${String(i+1).padStart(3,'0')}</button>`).join('');
        document.querySelector('#containerTabs').innerHTML = '<button class="tab active">Main cabinet</button><button class="tab">Workshop</button><button class="tab">Electronics</button>';
        document.querySelector('#containerTitle').textContent = 'Main cabinet';
        document.querySelector('#layoutMeta').textContent = '128 locations';
        document.querySelector('#selectionPanel h2').textContent = 'Bottom Drawer';
        document.querySelector('#binDetail').innerHTML = '<strong class="location-name">Bottom Drawer</strong><div class="muted">drawer</div><div class="actions"><button>Open</button><button>Location options</button></div>' + Array.from({length:12},(_,i) => `<div class="result"><strong>M3 flat washers, stainless steel ${i}</strong><p>48 each</p><div class="actions"><button>Edit</button><button>Move</button></div></div>`).join('');
        document.querySelector('#command').value = 'Put away these M3 washers';
        window.originalCommand = document.querySelector('#command');
        window.originalConfirm = document.createElement('button');
        originalConfirm.textContent = 'Confirm';
        originalConfirm.onclick = () => { window.confirmClicks = (window.confirmClicks || 0) + 1; };
        const proposal = document.createElement('section');
        proposal.className = 'pending-plan-card';
        proposal.innerHTML = '<strong>Put away 12 M3 flat washers</strong><p>Main cabinet / B019</p>';
        proposal.append(originalConfirm);
        document.querySelector('#results').replaceChildren(proposal);
      });
      await page.addScriptTag({content:`const byId = id => document.getElementById(id); const kioskMode = false; let selectedBin = 'B019'; const leaveItemEdit = () => true; const loadContainers = async () => {}; const setStatus = () => {}; ${source}\ninitializeCabinetWorkspace(); initializeDesktopWorkspace();`});
      for (const [width,height] of [[2672,1522],[1440,900],[1280,800],[1024,768],[1024,600]]) {
        await page.setViewportSize({width,height});
        await page.locator('.desktop-composer').waitFor({state:'visible'});
        const boxes = await page.evaluate(() => {
          const box = selector => {
            const r = document.querySelector(selector).getBoundingClientRect();
            return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height};
          };
          return {map:box('#cabinetTools'),grid:box('#grid'),selection:box('#selectionPanel'),selectionHead:box('#selectionPanel > .panel-head'),selectionBody:box('#selectionPanel > .body'),context:box('.desktop-context'),chat:box('.stack'),composer:box('.desktop-composer'),results:box('#results'),pageWidth:document.documentElement.scrollWidth,viewport:innerWidth};
        });
        await page.screenshot({path:path.join(artifactDir,`desktop-${route === '/' ? 'root' : 'cabinet'}-${width}x${height}.png`)});
        assert(boxes.chat.right <= boxes.context.x + 1, 'Chat must stay left of the storage workspace');
        assert(boxes.map.bottom <= boxes.selection.y + 1, 'Cabinet overlaps selected place');
        assert(boxes.grid.bottom <= boxes.selectionHead.y + 1, 'Map cells overlap selected-place controls');
        assert(boxes.selectionHead.bottom <= boxes.selectionBody.y + 1, 'Selected-place header overlaps contents');
        assert(boxes.selectionHead.height >= 60, 'Selected-place controls were compressed');
        assert.equal(await page.locator('#binDetail > .location-name').isVisible(),false,'Selected-place name is duplicated');
        assert(boxes.results.bottom <= boxes.composer.y + 1, 'Results overlap composer');
        assert(boxes.composer.bottom <= height, 'Composer below viewport');
        assert(boxes.results.height > 100, 'Conversation has no usable height');
        assert(boxes.pageWidth <= width, 'Page overflows horizontally');
        assert.equal(await page.locator('#command').count(),1);
        assert(await page.locator('#commandBtn').evaluate(node => getComputedStyle(node,'::before').maskImage.includes('data:image/svg+xml')),'Send icon is missing');
        await page.locator('.desktop-more > summary').click();
        assert(await page.locator('#sortPhotoBtn').isVisible());
        await page.locator('.desktop-more > summary').press('Escape');
        assert.equal(await page.locator('.desktop-more').getAttribute('open'),null);
        await page.locator('.product-header [data-product-view="inventory"]').click();
        const inventory = page.frameLocator('.workspace-manager iframe');
        await inventory.locator('#inventoryMain').waitFor({state:'visible'});
        assert.equal(await inventory.locator('.product-brand').isVisible(),false,'Embedded manager repeats navigation');
        assert.equal(await inventory.locator('.product-bottom-nav').isVisible(),false,'Embedded manager repeats mobile navigation');
        assert(await page.locator('#command').isVisible(),'Items hid the conversation composer');
        const managerBox = await page.locator('.workspace-manager').boundingBox();
        assert(managerBox.y + managerBox.height <= height + 1,'Items manager is below viewport');
        await page.screenshot({path:path.join(artifactDir,`desktop-items-${route === '/' ? 'root' : 'cabinet'}-${width}x${height}.png`)});
        await page.locator('.product-header [data-product-view="places"]').click();
        assert(await page.locator('#cabinetTools').isVisible(),'Places did not restore the map');
        assert(await page.evaluate(() => document.querySelector('#command') === originalCommand));
      }
      await page.locator('.product-header [data-product-view="inventory"]').click();
      await page.frameLocator('.workspace-manager iframe').locator('#inventoryMain').evaluate(() => {
        window.managerDocumentMarker = 'same-document-across-resize';
        const dialog = document.createElement('dialog');
        dialog.id = 'fixtureEdit';
        dialog.innerHTML = '<input id="fixtureDraft" value="Unsaved washer edit">';
        document.body.append(dialog);
        dialog.showModal();
      });
      await page.setViewportSize({width:390,height:844});
      await page.waitForFunction(() => !document.body.classList.contains('desktop-workspace'));
      assert.equal(await page.locator('.desktop-composer').count(),0);
      assert.equal(await page.locator('.desktop-context').getAttribute('hidden'),'');
      await page.setViewportSize({width:1280,height:800});
      await page.waitForFunction(() => document.body.classList.contains('desktop-workspace'));
      const managerFrame = page.frameLocator('.workspace-manager iframe');
      assert.equal(await managerFrame.locator('#fixtureDraft').inputValue(),'Unsaved washer edit','Resize discarded the manager draft');
      assert.equal(await managerFrame.locator('#fixtureEdit').evaluate(node => node.open),true);
      assert.equal(await managerFrame.locator('body').evaluate(() => window.managerDocumentMarker),'same-document-across-resize');
      await page.locator('.product-header [data-product-view="places"]').click();
      assert(await page.locator('.desktop-context').evaluate(node => node.classList.contains('manager-open')),'Navigation escaped an unfinished editor');
      await managerFrame.locator('#fixtureEdit').evaluate(node => node.close());
      await page.locator('.product-header [data-product-view="places"]').click();
      assert(await page.evaluate(() => document.querySelector('#command') === originalCommand));
      assert.equal(await page.locator('#command').inputValue(),'Put away these M3 washers');
      assert(await page.evaluate(() => document.querySelector('.pending-plan-card button') === originalConfirm));
      await page.getByRole('button',{name:'Confirm',exact:true}).click();
      assert.equal(await page.evaluate(() => confirmClicks),1);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: two desktop routes, five geometries, mixed-size 128-bin map/detail boundaries, embedded Items navigation, single composer, resize preserves draft/proposal/handler.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
