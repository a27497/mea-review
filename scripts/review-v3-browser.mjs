import {chromium, expect} from '@playwright/test';
import {spawn, execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';
import {checkReviewProvenance} from './review-provenance-check.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const artifacts = path.join(root, 'artifacts');
const out = path.resolve(process.env.MEA_V3_E2E_OUT ||
  path.join(artifacts, 'review-v3-' + new Date().toISOString().replaceAll(':', '-')));
await mkdir(path.dirname(out), {recursive:true});
await mkdir(out);
const v4 = JSON.parse(await readFile(path.join(root, 'fixtures/review/v4.json'), 'utf8'));
const cleanEnv = {PATH:process.env.PATH, LANG:'C.UTF-8'};
const py = (code,args) => execFileSync('python3',['-B','-c',code,...args],{cwd:root,env:cleanEnv,encoding:'utf8'});
let server, base, browser, log = '';
const contexts=[], errors=[], consoleErrors=[], external=[], flows=[], writes=[], screenshots=[];
async function start(session, port = 0) {
  server = spawn('python3', ['-B', '-m', 'mea', 'serve', '--port', String(port), '--task-dir', session],
    {cwd:root, env:cleanEnv, stdio:['ignore','pipe','pipe']});
  server.stderr.on('data', data => { log += data; });
  base = await new Promise((resolve, reject) => {
    let startup = '';
    const timer = setTimeout(() => reject(Error('Server startup timeout')), 10000);
    server.once('exit', code => { clearTimeout(timer); reject(Error('Server exited: ' + code + ' ' + log)); });
    server.stdout.on('data', data => {
      startup += data;
      const match = startup.match(/MEA OFFLINE (http:\/\/127\.0\.0\.1:\d+)\n/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
  });
}
async function stop() {
  if(server && server.exitCode==null && server.signalCode==null) {
    const code=await new Promise(resolve=>{server.once('exit',resolve);server.kill('SIGINT');});
    assert.equal(code,0,'Demo stops cleanly on Ctrl+C');
  }
}
async function page(width,height) {
  const c=await browser.newContext({viewport:{width,height},isMobile:width===390,hasTouch:width===390,acceptDownloads:true});
  contexts.push(c);
  await c.route('**/*',r=>{
    const url=new URL(r.request().url());
    if(url.origin!==base){external.push(url.href);return r.abort();}
    if(r.request().method()==='POST'){
      writes.push(url.pathname);
    }
    return r.continue();
  });
  await c.tracing.start({screenshots:true,snapshots:true});
  const p=await c.newPage();
  p.setDefaultTimeout(9000);
  p.on('pageerror',e=>errors.push(e.message));
  p.on('console', message => {if (message.type() === 'error') consoleErrors.push(message.text());});
  return p;
}
const btn=(p,name)=>p.getByRole('button',{name,exact:true});
const rail=p=>p.locator('#review');
const located=p=>p.locator('#code .located');
const panels=p=>p.getByRole('navigation',{name:'Workspace panels'});
const panel=(p,name)=>btn(panels(p),name).click();
const status=(p,value)=>expect(p.locator('#draft-status')).toHaveText(value);
async function guide(p,name){
  await expect(p.locator('#workspace')).not.toHaveAttribute('aria-busy', 'true');
  await btn(p,'Demo guide').click();
  const dialog=p.getByRole('dialog',{name:'MEA Review offline demo'});
  await expect(dialog).toContainText('Run the owned v4 example');
  await expect(dialog).toContainText('Owned Mock fixture');
  await btn(dialog,name).click();
  await expect(dialog).toBeHidden();
}
async function mode(p,name){
  if(p.viewportSize().width<=880){await p.locator('#switch > summary').click();await btn(p.locator('#switch'),name).click();}
  else await btn(p.getByRole('navigation',{name:'Review cases'}),name).click();
}
async function shot(p,name){
  await p.evaluate(()=>document.fonts.ready);
  const layout=await p.evaluate(()=>({
    width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth,
    sections:[...document.querySelectorAll('.top,.inbox,.intro,.panes,.files,.code,.review,footer')].filter(n=>n.offsetWidth).map(n=>{
      const r=n.getBoundingClientRect();return [n.id||n.className||n.tagName,r.x,r.y,r.width,r.height];
    }),
    fonts:[...document.fonts].map(f=>[f.family,f.status]),
  }));
  assert.equal(layout.width,p.viewportSize().width);assert.equal(layout.height,p.viewportSize().height);assert.equal(layout.scroll,layout.width);
  assert.equal(layout.fonts.filter(([,s])=>s==='loaded').length,
    await p.locator('.inbox-state').isVisible() ? 1 : 2);
  if(layout.width===1440 && layout.sections.some(r=>r[0]==='files')) {
    assert.deepEqual(['files','code','review'].map(key=>layout.sections.find(r=>r[0]===key)[3]),[228,758,378]);
  }
  await p.screenshot({path:path.join(out,name+'.png'),animations:'disabled'});
  screenshots.push({name,...layout});
}
async function download(p,name,file){
  const [d]=await Promise.all([p.waitForEvent('download'),rail(p).getByRole('link',{name,exact:true}).click()]);
  assert.equal(await d.failure(),null);await d.saveAs(path.join(out,file));return readFile(path.join(out,file),'utf8');
}
const box=p=>p.getByRole('textbox',{name:'Comment text'});
async function save(p,body){
  await box(p).fill(body);await expect(btn(p,'Preview exact comment')).toBeDisabled();
  await btn(p,'Save draft').click();await status(p,'DRAFT');
}
async function confirm(p,body,anchor){
  await btn(p,'Preview exact comment').click();
  await expect(p.locator('#exact-preview pre')).toHaveText(body);
  await expect(p.locator('#exact-preview')).toContainText(anchor);
  await expect(p.locator('#exact-preview')).toContainText('MOCK_GITHUB_NOT_LIVE');
  await expect(btn(p,'Confirm Mock comment')).toBeDisabled();
  await p.getByRole('checkbox',{name:/I reviewed the exact text/}).check();
  await btn(p,'Confirm Mock comment').click();await status(p,'CONFIRMED');
  await expect(btn(p,'Publish to GitHub')).toBeDisabled();
}
async function pair(p,name){
  await p.locator('#files').getByRole('button',{name:new RegExp(name)}).click();
  if(await btn(p,'Run manual comparison').isVisible()) await btn(p,'Run manual comparison').click();
  await expect(p.locator('.compare-tabs')).toBeVisible();
}
const comparison=p=>rail(p).getByText('Comparison evidence & exports',{exact:true});
try {
  const session = path.join(out, 'task-db');
  await start(session);
  browser = await chromium.launch({executablePath:process.env.MEA_BROWSER || undefined,
    args:['--disable-background-networking','--no-proxy-server','--disable-dev-shm-usage']});
  const d = await page(1440,940), t = await page(768,900), m = await page(390,844);
  const data = async (p, route) => {
    const response = await p.request.get(base + route);
    assert.equal(response.status(), 200);
    return response.json();
  };
  for (const route of ['/api/public-captures','/api/ai-runs']) {
    assert.equal((await d.request.get(base + route)).status(), 404);
  }
  assert.equal((await d.request.post(base + '/api/public-capture', {data:{}})).status(), 404);
  assert.equal((await d.request.get(base + '/classic')).status(), 200);
  assert.equal((await data(d, '/api/human/status')).live_publish, false);
  assert.equal((await data(d, '/api/investigation-provider')).real_enabled, false);
  const inbox = p => p.locator('#inbox');
  const queue = p => inbox(p).locator('.inbox-row');
  const search = p => inbox(p).getByRole('searchbox', {name: 'Search'});
  let respond;
  const waitForList = new Promise(resolve => { respond = resolve; });
  await d.route(base + '/api/tasks', async route => {
    await waitForList;
    await route.fulfill({contentType:'application/json', body:'invalid JSON'});
  }, {times:1});
  await d.goto(base);
  await expect(inbox(d).getByRole('heading', {name:'PR Inbox'})).toBeVisible();
  await expect(inbox(d).getByRole('status')).toHaveText('Loading saved records…');
  respond();
  await expect(inbox(d)).toContainText('Could not load investigations');
  await btn(inbox(d),'Retry').click();
  await expect(inbox(d)).toContainText('No saved pull requests');
  await expect(inbox(d).getByRole('link', {name:'Import snapshot'})).toHaveAttribute('href','/classic');
  assert.deepEqual(await data(d, '/api/tasks'), []);
  assert.deepEqual(await data(d, '/api/human/reviews'), []);
  assert.deepEqual(writes, []);
  await shot(d,'desktop-inbox-empty');
  flows.push('Bare Inbox loads read-only and presents loading, retry, empty state and import.');
  await guide(d,'Completed');
  await expect(rail(d).locator('.outcome-code')).toHaveText('ZERO_CANDIDATES');
  const recordId = new URL(d.url()).searchParams.get('task');
  assert.ok(recordId);
  const record = await data(d,'/api/task-json/' + recordId);
  assert.equal(record.mode, 'MOCK_REVIEW_DEMO_NOT_AI');
  assert.equal(record.provider, 'NONE');
  assert.deepEqual(record.view.revisions, v4.revisions);
  assert.deepEqual(record.candidates, []);

  async function followHead(p) {
    await rail(p).getByRole('button',{name:/^limits\.py.*HEAD/}).first().click();
    await expect(located(p)).toBeVisible();
    const line = Number(await located(p).getAttribute('data-head'));
    assert.ok(line > 0);
    const source = v4.files.find(f => f.path === 'limits.py').sides.head.text;
    assert.equal((await located(p).locator('code').textContent()).trim(), source.split('\n')[line - 1].trim());
    return line;
  }
  const completedLine = await followHead(d);
  await shot(d, 'desktop-01-completed');
  await rail(d).getByText('Snapshot details & exports',{exact:true}).click();
  assert.deepEqual(JSON.parse(await download(d,'Export JSON','completed.json')),record);
  assert.match(await download(d,'Export Markdown','completed.md'),/ZERO_CANDIDATES/);
  await m.goto(base + '/?task=' + recordId);
  await panel(m,'Review');
  assert.equal(await followHead(m),completedLine);
  await m.reload();
  await expect(located(m)).toHaveAttribute('data-head',String(completedLine));
  await shot(m, 'mobile-01-completed');
  flows.push('Self-owned completed investigation, exact source links on desktop and mobile, JSON and Markdown, and reload.');

  await guide(d,'Human review');
  await expect(d.locator('#code header')).toContainText('quota.py');await shot(d,'desktop-02-human');
  await m.reload();await mode(m,'Human review');await panel(m,'Diff');
  await expect(m.locator('#code header')).toContainText('quota.py');await shot(m,'mobile-02-human');
  const reviewId=new URL(d.url()).searchParams.get('review');
  await t.goto(base);
  await expect(queue(t)).toHaveCount(3);
  const reviewPR = (await data(t, '/api/human/review/' + reviewId)).pr;
  for (const term of [reviewPR.repository.toUpperCase(), '#' + reviewPR.number, reviewPR.title.split(' ').at(-1)]) {
    await search(t).fill(term);
    await expect(queue(t).filter({hasText:reviewPR.title}).first()).toBeVisible();
  }
  await search(t).fill('unmatched-pull-request');
  await expect(inbox(t)).toContainText('No matching records');
  await btn(inbox(t),'Clear filters').click();
  await expect(search(t)).toBeFocused();
  await inbox(t).getByLabel('Group').selectOption('reviews');
  await expect(queue(t)).toHaveCount(1);
  await queue(t).first().press('Enter');
  await expect(inbox(t)).toBeHidden();
  await expect(rail(t).getByRole('heading',{name:'Finding',exact:true})).toBeVisible();
  const disposition = (p, name) => rail(p).getByRole('group', {name:'Human judgment'}).getByRole('button',{name,exact:true});
  const beforeJudgment = writes.length;
  await disposition(t,'Ignore').press('Enter');
  await expect(disposition(t,'Ignore')).toHaveAttribute('aria-pressed','true');
  await t.reload();
  await expect(disposition(t,'Ignore')).toHaveAttribute('aria-pressed','true');
  await expect(btn(t,'Send to Mock')).toHaveCount(0);
  await disposition(t,'Undecided').click();
  await expect(t.locator('#disposition-status')).toHaveText('Local judgment · Undecided');
  await disposition(t,'Accept').click();
  await expect(t.locator('#disposition-status')).toHaveText('Local judgment · Accepted');
  assert.deepEqual(writes.slice(beforeJudgment), Array(3).fill('/api/human/disposition'));
  const judged = await data(t, '/api/human/review/' + reviewId);
  assert.equal(judged.dispositions[0].disposition, 'ACCEPTED');
  assert.deepEqual(judged.drafts, []);
  assert.deepEqual(judged.sends, []);
  await shot(t,'tablet-01-finding');
  for (const [p,prefix] of [[t,'tablet'],[m,'mobile']]) {
    await panel(p,'Review');
    const caller = rail(p).getByRole('button',{name:/Caller evidence.*app.py/});
    await caller.press('Enter');
    await expect(p.locator('#code header')).toContainText('HEAD');
    await expect(located(p)).toHaveAttribute('data-head','4');
    await expect(located(p)).toBeFocused();
    await shot(p,prefix+'-caller-evidence');
    await p.reload();
    await expect(located(p)).toHaveAttribute('data-head','4');
    await btn(p,'Back to review').press('Enter');
    await expect(caller).toBeFocused();
    await panel(p,'Files');
    const fileSearch = p.getByRole('searchbox',{name:'Filter files'});
    await fileSearch.fill('quota.py');
    await expect(p.locator('#files .tree-row')).toHaveCount(1);
    await p.locator('#files').getByRole('button',{name:'quota.py',exact:true}).press('Enter');
    await expect(p.locator('#code')).toBeFocused();
    await expect(p.locator('#code header')).toContainText('quota.py');
  }
  await t.locator('#inbox-link').click();
  await expect(queue(t)).toHaveCount(3);
  await shot(t,'tablet-inbox');
  flows.push('Inbox search and group filters, keyboard entry and return, local judgment/reload, and exact keyboard evidence round trips on tablet and phone.');
  await btn(d,'Open candidate · L2').click();await rail(d).getByRole('button',{name:/Select candidate/}).click();
  await expect(located(d)).toHaveAttribute('data-head','2');
  await rail(d).getByRole('button',{name:/Caller evidence.*app.py:4/}).click();await expect(located(d)).toHaveAttribute('data-head','4');
  const body='MOCK: If quota() returns None, available() may fail during subtraction. Please check the missing-value path.';
  await box(d).fill(body+' Draft to revise.');
  await mode(d,'Completed');await expect(located(d)).toHaveAttribute('data-head',String(completedLine));
  await mode(d,'Human review');await expect(located(d)).toHaveAttribute('data-head','4');
  await expect(box(d)).toHaveValue(body+' Draft to revise.');
  await save(d,body+' Draft to revise.');await confirm(d,body+' Draft to revise.','quota.py:2 RIGHT');
  await box(d).fill(body);await expect(btn(d,'Send to Mock')).toBeDisabled();
  await btn(d,'Save draft').click();await status(d,'DRAFT');await d.reload();await expect(box(d)).toHaveValue(body);
  await confirm(d,body,'quota.py:2 RIGHT');await shot(d,'desktop-exact-confirmation');
  await btn(d,'Send to Mock').dblclick({delay:10}).catch(()=>{});
  await status(d,'MOCK_PUBLISHED');
  flows.push('Mock v1: candidate and caller, revision invalidates confirmation, exact confirmation, one dispatch.');

  await m.reload();await mode(m,'Re-review');await panel(m,'Compare');
  await btn(m,'Run manual comparison').click();
  await expect(m.locator('.source-card')).toHaveCount(2);await shot(m,'mobile-03-compare');
  const earlierSource = m.getByRole('region',{name:'Earlier HEAD source · quota.py'});
  await expect(earlierSource).toBeVisible();
  assert.equal(await earlierSource.evaluate(region => region.scrollWidth), await earlierSource.evaluate(region => region.clientWidth));
  await expect(earlierSource).not.toHaveAttribute('tabindex', '0');
  await d.reload();
  await guide(d,'Re-review');
  await expect(rail(d).getByText('STILL_PRESENT',{exact:true})).toBeVisible();
  await expect(d.locator('.original-comment')).toContainText(body);
  await shot(d,'desktop-03-compare');
  await btn(m,'Caller code').click();await expect(m.locator('.source-card').first()).toContainText('app.py:4');
  await m.reload();await expect(btn(m,'Caller code')).toHaveAttribute('aria-pressed','true');
  await btn(m,'Open result · STILL_PRESENT').click();await expect(rail(m).getByText('STILL_PRESENT',{exact:true})).toBeVisible();
  flows.push('v1→v2 rename and moved anchor, old comment stays on v1, mobile caller and result panels.');

  const [v1, v2] = (await data(d, '/api/human/review/' + reviewId)).tasks;
  const v2Head = v2.view.revisions.head;
  const desktopComparison = d.url(), mobileComparison = m.url();
  const beforeOpening = writes.length;
  const reviewRow = p => queue(p).filter({hasText:'Owned Mock'});
  async function checkLinkedReview(p, task, judgment) {
    const head = task.view.revisions.head;
    await expect(p.locator('#intro h1')).toHaveText(task.pr.title);
    await expect(p.locator('#intro').getByText('Frozen HEAD ' + head.slice(0,7), {exact:true})).toHaveAttribute('title', head);
    await expect(p.locator('#intro').getByText('Investigation completed', {exact:true})).toBeInViewport();
    await expect(rail(p).getByRole('heading',{name:'Finding',exact:true})).toBeInViewport();
    await expect(rail(p).locator('#changed-evidence')).toHaveAttribute('title', new RegExp(head + '$'));
    await expect(p.locator('#disposition-status')).toHaveText('Local judgment · ' + judgment);
    assert.equal(new URL(p.url()).searchParams.get('version'), task.id);
  }
  for (const [p, layout] of [[d,'desktop'], [t,'tablet'], [m,'mobile']]) {
    await p.goto(base);
    await expect(reviewRow(p).locator('strong')).toHaveText(v2.pr.title);
    await inbox(p).getByLabel('Group').selectOption('reviews');
    await inbox(p).getByLabel('Investigation status').selectOption({label:'Completed'});
    await expect(inbox(p).getByLabel('Group')).toHaveValue('reviews');
    await expect(inbox(p).getByLabel('Investigation status')).toHaveValue('COMPLETED');
    await expect(queue(p)).toHaveCount(1);
    await expect(reviewRow(p).getByText('Investigation completed', {exact:true})).toBeVisible();
    await expect(reviewRow(p).locator('.inbox-head')).toHaveAttribute('title', v2Head);
    const target = new URL(await reviewRow(p).getAttribute('href'), base);
    assert.equal(target.searchParams.get('review'), reviewId);
    assert.equal(target.searchParams.get('version'), v2.id);
    if (p === d) await shot(d,'desktop-linked-review-inbox');
    if (p === m) await reviewRow(p).tap();
    else await reviewRow(p).press('Enter');
    await checkLinkedReview(p, v2, 'Undecided');
    if (p !== d) await expect(btn(panels(p),'Review')).toHaveAttribute('aria-current','true');
    await p.reload();
    await checkLinkedReview(p, v2, 'Undecided');
    await shot(p,layout + '-linked-review-v2');
  }
  await rail(t).getByText('Workspace history & exports',{exact:true}).click();
  await btn(t,'Open v1').click();
  await checkLinkedReview(t, v1, 'Accepted');
  await t.reload();
  await checkLinkedReview(t, v1, 'Accepted');
  await t.goto(base + '/?' + new URLSearchParams({review:reviewId, panel:'review'}));
  await checkLinkedReview(t, v1, 'Accepted');
  const missingVersion = 'task-' + '0'.repeat(64);
  await t.goto(base + '/?' + new URLSearchParams({review:reviewId, version:missingVersion, panel:'review'}));
  await expect(rail(t)).toContainText('The linked review does not contain the requested frozen version.');
  await expect(t.locator('#disposition-status')).toHaveCount(0);
  assert.equal(new URL(t.url()).searchParams.get('version'), missingVersion);
  flows.push('Inbox advertises and opens the exact linked v2 with an independent Undecided judgment at 1440, 768 and 390 px, including reload. Explicit and legacy v1 routes preserve the Accepted judgment.');

  // Simulate the selected task falling outside the bounded recent task list.
  await t.route(base + '/api/tasks', async route => {
    const response = await route.fetch();
    const tasks = await response.json();
    assert.ok(tasks.some(task => task.id === v2.id));
    await route.fulfill({response, json:tasks.filter(task => task.id !== v2.id)});
  }, {times:1});
  await t.goto(base);
  await expect(reviewRow(t).locator('strong')).toHaveText(v2.pr.repository + ' #' + v2.pr.number);
  await expect(reviewRow(t)).not.toContainText(v1.pr.title);
  await expect(reviewRow(t).locator('.inbox-head')).toHaveAttribute('title',v2Head);
  await inbox(t).getByLabel('Group').selectOption('reviews');
  await inbox(t).getByLabel('Investigation status').selectOption({label:'Unknown'});
  await expect(inbox(t).getByLabel('Group')).toHaveValue('reviews');
  await expect(queue(t)).toHaveCount(1);
  await expect(reviewRow(t).getByText('Investigation status unknown',{exact:true})).toBeVisible();
  await shot(t,'tablet-linked-review-missing-summary');
  await reviewRow(t).press('Enter');
  await checkLinkedReview(t, v2, 'Undecided');
  assert.equal(writes.length, beforeOpening);
  flows.push('A missing recent summary uses stable PR identity, retains the known head and exact version link, and exposes an unknown status filter. Opening and changing versions are read-only.');
  await d.goto(desktopComparison);
  await m.goto(mobileComparison);

  const mock=await m.request.post(base+'/api/human/mock-state',{data:{version:'v2',can_comment:true,fault:'AFTER_EFFECT',complete_listing:true}});
  assert(mock.ok());await mode(m,'Human review');await panel(m,'Review');
  await rail(m).getByText('Workspace history & exports',{exact:true}).click();await btn(m,'Open v2').click();
  await rail(m).getByRole('button',{name:/Select candidate/}).click();await expect(located(m)).toHaveAttribute('data-head','4');
  await btn(m,'Back to review').click();
  const moved='MOCK: The moved quota function can still return None.';
  await save(m,moved);await confirm(m,moved,'limits.py:4 RIGHT');await btn(m,'Send to Mock').click();
  await status(m,'UNCERTAIN');await expect(btn(m,'Send to Mock')).toHaveCount(0);await shot(m,'mobile-uncertain');
  await m.reload();
  await status(m,'UNCERTAIN');await btn(m,'Reconcile read-only').click();await status(m,'MOCK_PUBLISHED_RECONCILED');
  await expect(btn(m,'Send to Mock')).toHaveCount(0);await shot(m,'mobile-reconciled');
  flows.push('Mock v2: lost response after effect, UNCERTAIN persists on reload, reconciliation has no resend.');

  await d.reload();
  for(const [names,statuses] of [['v2 → v3',['RESOLVED','NEW']],['v3 → v4',['OUTDATED']],['v4 → v5',['NEW']],['v2 → v5',['NEEDS_RECHECK']]]){
    await pair(d,names);
    for(const status of statuses) await expect(rail(d).getByText(status,{exact:true}).first()).toBeVisible();
    if (names==='v2 → v3') {
      await btn(rail(d),'NEW').click();await d.reload();
      await expect(btn(rail(d),'NEW')).toHaveAttribute('aria-pressed','true');
    }
  }
  await comparison(d).click();const h=JSON.parse(await download(d,'Export JSON','human.json'));
  assert.match(await download(d,'Export Markdown','human.md'),/Live publication DISABLED/);
  assert.equal(h.id,reviewId);assert.equal(h.versions.length,5);assert.equal(h.comparisons.length,5);
  assert.deepEqual([...new Set(h.comparisons.flatMap(c=>c.rows.map(r=>r.status)))].sort(),['NEEDS_RECHECK','NEW','OUTDATED','RESOLVED','STILL_PRESENT']);
  const ids=h.tasks.map(t=>t.id);
  for(const [old_task_id,new_task_id,error] of [[ids[4],ids[1],'COMPARISON_VERSION_ORDER'],['task-' + '0'.repeat(64),ids[4],'REVIEW_VERSION_NOT_LINKED']]){
    const r=await d.request.post(base+'/api/human/compare-linked',{data:{review_id:reviewId,old_task_id,new_task_id}});
    assert.equal(r.status(),400);assert.equal((await r.json()).error,error);
  }
  const same=await d.request.post(base+'/api/human/compare-linked',{data:{review_id:reviewId,old_task_id:ids[1],new_task_id:ids[4]}});
  assert.deepEqual(await same.json(),h);
  await d.reload();await expect(rail(d).getByText('NEEDS_RECHECK',{exact:true}).first()).toBeVisible();
  await shot(d,'desktop-needs-recheck');
  await d.locator('#switch > summary').click();
  await d.getByRole('link',{name:'Saved workspaces',exact:true}).click();
  await expect(inbox(d)).toContainText('Saved investigations');
  await expect(inbox(d)).toContainText('Saved human reviews');
  await shot(d,'desktop-inbox-populated');
  const lastVersion = h.tasks.at(-1);
  const savedReview = queue(d).filter({hasText:'Owned Mock'});
  await expect(savedReview).toContainText(lastVersion.view.revisions.head.slice(0,7));
  await savedReview.click();
  await expect(d.locator('#intro h1')).toHaveText(lastVersion.pr.title);
  assert.equal(new URL(d.url()).searchParams.get('version'),lastVersion.id);
  await d.locator('#inbox-link').click();
  await queue(d).filter({hasText:record.pr.title}).click();
  await expect(rail(d).locator('.outcome-code')).toHaveText('ZERO_CANDIDATES');
  flows.push('Five persisted versions and all five outcomes, linked endpoint order/membership/idempotence, exports and task reopen.');

  const snapshot = JSON.parse(await readFile(path.join(root,'fixtures/demo/none-return.json'),'utf8'));
  const longPath = 'tests/application/integrations/worker-quota/limits-and-fallbacks/test_lookup.py';
  const extraFile = snapshot.files.find(file => file.path === 'tests/test_lookup.py');
  extraFile.path = longPath;
  extraFile.base_path = longPath;
  snapshot.scope.complete_listing = false;
  snapshot.scope.reported_changed_files++;
  const stopped = await m.request.post(base + '/api/tasks', {data:{snapshot, budget:{tools:1}}});
  assert.equal(stopped.status(), 200);
  const stoppedId = (await stopped.json()).id;
  await expect.poll(async () => (await data(m, '/api/task/' + stoppedId)).status).toBe('STOPPED');
  await m.goto(base);
  await inbox(m).getByLabel('Investigation status').selectOption({label:'Stopped'});
  await expect(inbox(m).getByLabel('Investigation status')).toHaveValue('STOPPED');
  await expect(queue(m)).toHaveCount(1);
  await expect(queue(m).first()).toContainText('Offline policy');
  await expect(queue(m).first().getByText('Investigation stopped',{exact:true})).toBeVisible();
  await shot(m,'mobile-inbox-stopped');
  await queue(m).first().click();
  await panel(m,'Files');
  await m.getByRole('searchbox',{name:'Filter files'}).fill('worker-quota/limits');
  await expect(m.locator('#files .tree-row')).toHaveCount(1);
  await m.locator('#files').getByRole('button',{name:longPath,exact:true}).press('Enter');
  await expect(m.locator('#code header')).toContainText(longPath);
  await shot(m,'mobile-long-path');
  await panel(m,'Review');
  await expect(rail(m)).toContainText('Investigation stopped');
  await expect(rail(m)).toContainText('1 of 1 tool calls');
  await expect(rail(m).locator('.outcome-code')).toHaveText('TOOL_BUDGET');
  await expect(rail(m)).toContainText('The frozen file listing is incomplete.');
  await expect(m.locator('#intro')).toContainText('Partial capture');
  await shot(m,'mobile-stopped');
  const stale = new URLSearchParams({case:'human', review:reviewId, version:ids[1], file:'quota.py', side:'head', line:'2'});
  await m.goto(base + '/?' + stale);
  await expect(m.locator('#code .evidence-alert')).toHaveText('File not collected in this frozen version · quota.py');
  await expect(located(m)).toHaveCount(0);
  await m.reload();
  await expect(m.locator('#code .evidence-alert')).toBeVisible();
  const staleDiff = m.getByRole('region',{name:'Unified diff · limits.py'});
  await expect(staleDiff).toHaveAttribute('tabindex','0');
  await m.locator('#code').focus();
  await m.keyboard.press('Tab');
  await expect(staleDiff).toBeFocused();
  await m.keyboard.press('ArrowRight');
  await expect.poll(() => staleDiff.evaluate(region => region.scrollLeft)).toBeGreaterThan(0);
  await shot(m,'mobile-stale-link');
  flows.push('An offline task stops at its tool budget, exposes partial capture and a long path, and stale links stay visible after reload at 390 px.');
  flows.push('Tab reaches the named overflowing unified diff at 390 px and ArrowRight scrolls it. A short compare listing is outside the tab sequence.');

  const boundaryTitle = 'Repository documentation, review workflow migration, scope verification, and workspace accessibility updates. '
    .repeat(5).slice(0,500).trim();
  const boundaryPath = 'src/phase_four_product_workbench/verification/synthetic_provider_adapter/' +
    'frozen_snapshot_collection/historical_source_context/workspace_module_10/' +
    'local_persistence_operation_with_review_evidence_lineage.py';
  const signature = 'def ratio_9():';
  const notes = 'offline-review-provenance-'.repeat(24);
  const oldLine = '    return 1  # ' + notes + ' previous';
  const newLine = '    return 2  # ' + notes + ' current';
  const source = line => {
    const text = signature + '\n' + line + '\n';
    return {
      text,
      sha256: createHash('sha256').update(text).digest('hex'),
      blob_sha: createHash('sha1').update(`blob ${Buffer.byteLength(text)}\0${text}`).digest('hex'),
    };
  };
  assert.equal(Buffer.byteLength(boundaryTitle),500);
  assert.equal(boundaryPath.length,205);
  snapshot.pr.title = boundaryTitle;
  snapshot.scope.reported_changed_files++;
  snapshot.files.push({
    path: boundaryPath, base_path: boundaryPath, status:'modified',
    patch: `@@ -1,2 +1,2 @@\n ${signature}\n-${oldLine}\n+${newLine}\n`,
    sides: {base:source(oldLine), head:source(newLine)},
  });
  const titleTask = await m.request.post(base + '/api/tasks', {data:{snapshot, budget:{tools:1}}});
  assert.equal(titleTask.status(),200);
  const titleTaskId = (await titleTask.json()).id;
  await expect.poll(async () => (await data(m,'/api/task/' + titleTaskId)).status).toBe('STOPPED');
  const titleQuery = new URLSearchParams({task:titleTaskId, file:boundaryPath, side:'head', line:'1', back:'files'});
  const boundaryLayout = [];
  for (const [width,height] of [[390,844],[720,450]]) {
    await m.setViewportSize({width,height});
    await m.goto(base + '/?' + titleQuery);
    await expect(m.locator('#intro h1')).toHaveText(boundaryTitle);
    const pane = m.locator('#code');
    const header = pane.locator('.code-head');
    const diff = pane.getByRole('region',{name:'Unified diff · ' + boundaryPath,exact:true});
    const back = btn(header,'Back to files');
    await expect(header.locator('.code-path span')).toHaveText(boundaryPath);
    await expect(header.locator('.controls > span[title]')).toHaveCount(2);
    await expect(header.locator('.controls > span[title]').first()).toHaveAttribute('title',snapshot.revisions.base);
    await expect(header.locator('.controls > span[title]').last()).toHaveAttribute('title',snapshot.revisions.head);
    await expect(header).toContainText('HEAD · L1');
    await expect(located(m).locator('code')).toHaveText(signature);
    await expect(diff).toHaveAttribute('tabindex','0');
    await m.evaluate(() => document.fonts.ready);
    await expect.poll(() => pane.evaluate(node => node.clientHeight)).toBeGreaterThanOrEqual(120);
    const measure = () => located(m).locator('code').evaluate(node => {
      // The code element can span the full long diff line. Measure the text itself.
      const range = document.createRange();
      range.selectNodeContents(node);
      const bounds = range.getBoundingClientRect();
      const hit = document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2);
      const pane = node.closest('#code');
      return {
        scrollTop:pane.scrollTop, maxScrollTop:pane.scrollHeight - pane.clientHeight,
        paneHeight:pane.clientHeight,
        headerHeight:pane.querySelector('.code-head').getBoundingClientRect().height,
        codeHit:!!hit && node.contains(hit), hit:hit?.closest('.code-head')?.className || hit?.tagName || null,
      };
    });
    await pane.scrollIntoViewIfNeeded();
    await pane.focus();
    await m.keyboard.press('Home');
    await expect.poll(async () => (await measure()).scrollTop).toBe(0);
    const wheelBefore = await measure();
    await pane.hover();
    await m.mouse.wheel(0,600);
    await expect.poll(measure, {message:'The wheel must expose the code text center'}).toMatchObject({codeHit:true});
    const wheelAfter = await measure();
    assert(wheelAfter.scrollTop > wheelBefore.scrollTop, 'Wheel should scroll the Diff pane');
    await shot(m,'long-title-wheel-' + width);

    await pane.focus();
    await m.keyboard.press('Tab');
    await expect(back).toBeFocused();
    await m.keyboard.press('Tab');
    await expect(diff).toBeFocused();
    await m.keyboard.press('Home');
    await expect.poll(async () => (await measure()).scrollTop).toBe(0);
    const keyboardBefore = await measure();
    await m.keyboard.press('End');
    await expect.poll(async () => {
      const {scrollTop,maxScrollTop} = await measure();
      return Math.abs(maxScrollTop - scrollTop);
    }).toBeLessThanOrEqual(1);
    const keyboardAfter = await measure();
    assert(keyboardAfter.scrollTop > keyboardBefore.scrollTop, 'End should scroll the focused Diff pane');
    assert(keyboardAfter.codeHit, 'End should expose the code text center');
    await expect(diff).toBeFocused();
    await shot(m,'long-title-' + width);
    await m.keyboard.press('Shift+Tab');
    await expect(back).toBeFocused();
    await m.keyboard.press('Enter');
    await expect(m.getByRole('button',{name:boundaryPath,exact:true})).toBeFocused();
    const filter = m.getByRole('searchbox',{name:'Filter files'});
    await filter.scrollIntoViewIfNeeded();
    await expect(filter).toBeInViewport();
    await panel(m,'Review');
    await expect(rail(m).locator('.outcome-code')).toHaveText('TOOL_BUDGET');
    boundaryLayout.push({width,height,wheelBefore,wheelAfter,keyboardBefore,keyboardAfter});
  }
  await d.goto(base + '/?' + titleQuery);
  await expect(d.locator('#code .code-head')).toHaveCSS('position','sticky');
  flows.push('With a 500-byte PR title and a 205-character path, wheel and keyboard scrolling expose the code text center. Diff focus and Back to files work at 390×844 and 720×450.');

  const audit=JSON.parse(py(`
import json,sqlite3,sys
from pathlib import Path
with sqlite3.connect((Path(sys.argv[1])/'tasks.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
    mock=json.loads(db.execute("SELECT doc FROM human_records WHERE kind='mock'").fetchone()[0])
    states=[json.loads(r[0])['status'] for r in db.execute("SELECT doc FROM human_records WHERE kind='send' ORDER BY rowid")]
    live=db.execute("SELECT count(*) FROM tasks WHERE json_extract(doc,'$.mode')='REAL_PROVIDER_DEEPSEEK'").fetchone()[0]
print(json.dumps(dict(attempts=mock['post_attempts'],comments=len(mock['comments']),send_states=states,real_tasks=live)))
`,[session]));
  assert.deepEqual(audit,{attempts:2,comments:2,send_states:['MOCK_PUBLISHED','MOCK_PUBLISHED_RECONCILED'],real_tasks:0});
  assert.equal(writes.filter(r=>r==='/api/human/send').length,2);
  assert.deepEqual(errors,[]);assert.deepEqual(consoleErrors,[]);assert.deepEqual(external,[]);
  const port = Number(new URL(base).port);
  await stop();
  await start(session, port);
  const restored = await data(d, '/api/human/review/' + reviewId);
  assert.deepEqual(restored, h);
  assert.deepEqual(await data(d, '/api/task-json/' + record.id), record);
  await stop();
  await start(path.join(out, 'fresh-task-db'), port);
  assert.deepEqual(await data(d, '/api/human/reviews'), []);
  assert.deepEqual(await data(d, '/api/tasks'), []);
  flows.push('Server restart preserves all reviews and outcomes. A clean SQLite directory starts with empty state.');
  await checkReviewProvenance(d, base, root, path.join(out, 'fresh-task-db'));
  flows.push('The desktop header and candidate card distinguish saved Real and deterministic Mock sources from Mock writeback.');
  assert.deepEqual(errors,[]);assert.deepEqual(consoleErrors,[]);assert.deepEqual(external,[]);
  await writeFile(path.join(out,'verification.json'),JSON.stringify({status:'PASS',flows,screenshots,
    errors,consoleErrors,external,writes,audit,boundaryLayout,real_model_requests:0,real_github_writes:0,exit_code:0},null,2)+'\n');
  console.log('PASS: desktop and mobile source navigation, candidate provenance, confirmation, Mock reconciliation, five comparison outcomes and persistence.');
} catch(error) {
  for(const [i,c] of contexts.entries()) await c.pages()[0]?.screenshot({path:path.join(out,'failure-'+i+'.png')}).catch(()=>{});
  await writeFile(path.join(out,'failure.json'),JSON.stringify({flows,screenshots,errors,consoleErrors,external,writes,message:error.stack,exit_code:1},null,2));
  throw error;
} finally {
  for(const [i,c] of contexts.entries()) await c.tracing.stop({path:path.join(out,'trace-'+i+'.zip')});
  await browser?.close();await stop();await writeFile(path.join(out,'server-stderr.log'),log);
}
