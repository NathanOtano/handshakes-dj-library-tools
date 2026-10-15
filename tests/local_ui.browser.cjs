// Run only against the temporary fixture server, never the real USB profile.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const base = process.env.DJ_UI_TEST_URL || 'http://127.0.0.1:8766';
  const output = process.env.DJ_UI_TEST_OUTPUT || 'runtime/local-ui-browser/captures';
  const state = await fetch(base+'/api/state').then(r=>r.json());
  if (!state.targetLabel.toLowerCase().includes('temporaire')) throw Error('Fixture profile required; refusing real copy');
  fs.mkdirSync(output,{recursive:true});
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  const captures = [], errors = [];
  try {
    const context = await browser.newContext({viewport:{width:1280,height:900}});
    const page = await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base);
    await page.getByRole('button',{name:'Vérifier la bibliothèque',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#audit-status').textContent==='Vérifiée');
    async function capture(p,name,viewport) {
      if (!await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)) throw Error('Horizontal overflow: '+name);
      const file=path.join(output,name+'.png');await p.screenshot({path:file,fullPage:true});
      captures.push({url:base,view:name,viewport,fullPage:true,path:file,fixture:true});
    }
    await capture(page,'desktop-library',{width:1280,height:900});
    await page.locator('[data-view="prepare"]').click();
    await page.locator('#prepare-action').click();
    await page.waitForFunction(()=>!document.querySelector('#sync').disabled);
    await capture(page,'desktop-prepare',{width:1280,height:900});
    await page.locator('[data-view="usb"]').click();
    await capture(page,'desktop-usb',{width:1280,height:900});
    await page.locator('#sync').click();
    await page.locator('#confirm').waitFor({state:'visible'});
    await capture(page,'desktop-confirm',{width:1280,height:900});
    await page.locator('#cancel').click();
    if((await fetch(base+'/api/state').then(r=>r.json())).reports.sync) throw Error('Cancel performed a copy');
    const mobile = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
    const mp=await mobile.newPage();await mp.goto(base);
    await mp.waitForFunction(()=>document.querySelector('#audit-status').textContent==='Vérifiée');
    for (const view of ['library','prepare','usb']) {
      await mp.locator('[data-view="'+view+'"]').click();
      await capture(mp,'mobile-'+view,{width:390,height:844});
    }
    const missingTitles=await page.locator('button').evaluateAll(nodes=>nodes.filter(n=>!n.title && !n.getAttribute('aria-label')).length);
    if(missingTitles) throw Error('Controls without help');
    await page.locator('#sync').click();await page.locator('#confirm-copy').click();
    await page.waitForFunction(()=>document.querySelector('#sync-status').textContent==='Copie vérifiée');
    if(!(await page.locator('#sync').isDisabled()))throw Error('Consumed preview still enabled');
    await capture(page,'desktop-complete',{width:1280,height:900});
    await page.route('**/api/state',route=>route.fulfill({status:503,body:'{}'}));
    await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('indisponible'));
    if(!(await page.locator('#audit').isDisabled()))throw Error('Offline action enabled');
    await capture(page,'desktop-offline',{width:1280,height:900});
    await page.unroute('**/api/state');
    await page.waitForFunction(()=>document.querySelector('#message').textContent==='');
    const bad=await fetch(base+'/api/state').then(r=>r.json());bad.auditFresh=true;bad.reports.audit.status='fail';bad.reports.audit.errors=['selected_tracks_without_bucket'];
    await page.route('**/api/state',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(bad)}));
    await page.locator('[data-view="library"]').click();
    await page.getByText('Des morceaux n’ont pas encore de DJ bac.',{exact:true}).waitFor();
    await capture(page,'desktop-issues',{width:1280,height:900});
    if(errors.length)throw Error(errors.join(';'));
    await mobile.close();
    await page.evaluate(async()=>{await fetch('/api/shutdown',{method:'POST',headers:{'Content-Type':'application/json','X-DJ-Token':document.querySelector('meta[name="dj-session"]').content},body:'{}'});});
    fs.writeFileSync(path.join(output,'manifest.json'),JSON.stringify({status:'pass',captures,checks:['real command execution on fixture','cancel does not copy','single-use preview','desktop/mobile three views','offline failure and recovery','controls have help','no page errors'],hardwareAcceptance:false},null,2));
    console.log(JSON.stringify({status:'pass',captures:captures.length,output}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
