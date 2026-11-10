import {chromium, expect} from '@playwright/test';
import {spawn, execFileSync} from 'node:child_process';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

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
const contexts=[], errors=[], external=[], flows=[], writes=[], screenshots=[];
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
  const p=await c.newPage();p.setDefaultTimeout(9000);p.on('pageerror',e=>errors.push(e.message));return p;
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
  if(p.viewportSize().width===390){await p.locator('#switch > summary').click();await btn(p.locator('#switch'),name).click();}
  else await btn(p.getByRole('navigation',{name:'Review cases'}),name).click();
}
async function shot(p,name){
  await p.evaluate(()=>document.fonts.ready);
  const layout=await p.evaluate(()=>({
    width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth,
    sections:[...document.querySelectorAll('.top,.intro,.panes,.files,.code,.review,footer')].filter(n=>n.offsetWidth).map(n=>{
      const r=n.getBoundingClientRect();return [n.id||n.className||n.tagName,r.x,r.y,r.width,r.height];
    }),
    fonts:[...document.fonts].map(f=>[f.family,f.status]),
  }));
  assert.equal(layout.width,p.viewportSize().width);assert.equal(layout.height,p.viewportSize().height);assert.equal(layout.scroll,layout.width);
  assert.equal(layout.fonts.filter(([,s])=>s==='loaded').length,2);
  if(layout.width===1440) assert.deepEqual(['files','code','review'].map(key=>layout.sections.find(r=>r[0]===key)[3]),[210,907,323]);
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
  const d = await page(1440,940), m = await page(390,844);
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
  await d.goto(base);
  await expect(d.locator('#intro')).toContainText('Review a frozen pull request');
  assert.deepEqual(await data(d, '/api/tasks'), []);
  assert.deepEqual(await data(d, '/api/human/reviews'), []);
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
  const reviewId=new URL(d.url()).searchParams.get('review');
  flows.push('Mock v1: candidate and caller, revision invalidates confirmation, exact confirmation, one dispatch.');

  await m.reload();await mode(m,'Re-review');await panel(m,'Compare');
  await btn(m,'Run manual comparison').click();
  await expect(m.locator('.source-card')).toHaveCount(2);await shot(m,'mobile-03-compare');
  await d.reload();
  await guide(d,'Re-review');
  await expect(rail(d).getByText('STILL_PRESENT',{exact:true})).toBeVisible();
  await expect(d.locator('.original-comment')).toContainText(body);
  await shot(d,'desktop-03-compare');
  await btn(m,'Caller code').click();await expect(m.locator('.source-card').first()).toContainText('app.py:4');
  await m.reload();await expect(btn(m,'Caller code')).toHaveAttribute('aria-pressed','true');
  await btn(m,'Open result · STILL_PRESENT').click();await expect(rail(m).getByText('STILL_PRESENT',{exact:true})).toBeVisible();
  flows.push('v1→v2 rename and moved anchor, old comment stays on v1, mobile caller and result panels.');

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
  await d.locator('#switch > summary').click();await btn(d,'Saved workspaces').click();
  await expect(d.getByRole('dialog')).toContainText('Saved investigations');
  await expect(d.getByRole('dialog')).toContainText('Saved human reviews');
  await d.getByRole('dialog').getByRole('button',{name:'Close',exact:true}).click();
  await d.goto(base + '/?task=' + record.id);
  await expect(rail(d).locator('.outcome-code')).toHaveText('ZERO_CANDIDATES');
  flows.push('Five persisted versions and all five outcomes, linked endpoint order/membership/idempotence, exports and task reopen.');

  const audit=JSON.parse(py(`
import json,sqlite3,sys
from pathlib import Path
with sqlite3.connect((Path(sys.argv[1])/'tasks.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
    mock=json.loads(db.execute("SELECT doc FROM human_records WHERE kind='mock'").fetchone()[0])
    states=[json.loads(r[0])['status'] for r in db.execute("SELECT doc FROM human_records WHERE kind='send' ORDER BY rowid")]
    live=db.execute("SELECT count(*) FROM tasks WHERE json_extract(doc,'$.mode')='REAL_AI'").fetchone()[0]
print(json.dumps(dict(attempts=mock['post_attempts'],comments=len(mock['comments']),send_states=states,real_tasks=live)))
`,[session]));
  assert.deepEqual(audit,{attempts:2,comments:2,send_states:['MOCK_PUBLISHED','MOCK_PUBLISHED_RECONCILED'],real_tasks:0});
  assert.equal(writes.filter(r=>r==='/api/human/send').length,2);
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
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
  await writeFile(path.join(out,'verification.json'),JSON.stringify({status:'PASS',flows,screenshots,
    errors,external,writes,audit,real_model_requests:0,real_github_writes:0,exit_code:0},null,2)+'\n');
  console.log('PASS: desktop and mobile source navigation, confirmation, Mock reconciliation, five comparison outcomes and persistence.');
} catch(error) {
  for(const [i,c] of contexts.entries()) await c.pages()[0]?.screenshot({path:path.join(out,'failure-'+i+'.png')}).catch(()=>{});
  await writeFile(path.join(out,'failure.json'),JSON.stringify({flows,screenshots,errors,external,writes,message:error.stack,exit_code:1},null,2));
  throw error;
} finally {
  for(const [i,c] of contexts.entries()) await c.tracing.stop({path:path.join(out,'trace-'+i+'.zip')});
  await browser?.close();await stop();await writeFile(path.join(out,'server-stderr.log'),log);
}
