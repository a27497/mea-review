// Records the workbench demo with Playwright.
//
//   MEA_DEMO_OUT=DIR [MEA_DEMO_REAL_STORE=DIR] node scripts/record-demo.mjs
//
// Real part: MEA_DEMO_REAL_STORE is a task store written by `mea.investigation.live_run` (deepseek-flash)
// for fixtures/review/v1.json; it is copied, never modified. Without a real store, the guide opens
// the offline investigation instead.
// Everything else runs on a fresh task directory with the owned Mock fixtures. The browser blocks
// every request outside the local server; no model or GitHub request is made here.
import {chromium, expect} from '@playwright/test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {cp, mkdir, mkdtemp, readFile, rename, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('..', import.meta.url));
const out = path.resolve(process.env.MEA_DEMO_OUT || path.join(root, 'artifacts', 'demo-' + Date.now()));
await mkdir(out, {recursive: true});
const work = await mkdtemp(path.join(tmpdir(), 'mea-demo-'));
const W = 1280, H = 800;
const servers = [];
const timeline = [];
const start0 = Date.now();

async function serve(taskDir) {
  const proc = spawn('python3', ['-B', '-m', 'mea', 'serve', '--port', '0', '--task-dir', taskDir],
    {cwd: root, env: {PATH: process.env.PATH, LANG: 'C.UTF-8'}, stdio: ['ignore', 'pipe', 'pipe']});
  servers.push(proc);
  return new Promise((resolve, reject) => {
    let text = '';
    proc.once('exit', code => reject(Error('server exited ' + code)));
    proc.stdout.on('data', d => { text += d; const m = text.match(/MEA OFFLINE (http:\/\/127\.0\.0\.1:\d+)\n/); if (m) resolve(m[1]); });
  });
}

const browser = await chromium.launch({executablePath: process.env.MEA_BROWSER || undefined,
  args: ['--disable-background-networking', '--no-proxy-server', '--disable-dev-shm-usage']});
const context = await browser.newContext({viewport: {width: W, height: H}, bypassCSP: true,
  recordVideo: {dir: work, size: {width: W, height: H}}});
const external = [];
let allowed = new Set();
await context.route('**/*', r => {
  const u = new URL(r.request().url());
  if (u.protocol === 'data:' || allowed.has(u.origin)) return r.continue();
  external.push(u.href); return r.abort();
});
const p = await context.newPage();
p.setDefaultTimeout(10000);
const pause = ms => p.waitForTimeout(process.env.MEA_DEMO_FAST ? 0 : ms);
const btn = (name, scope = p) => scope.getByRole('button', {name, exact: true});
const rail = () => p.locator('#review');

// Caption bar drawn into the page (bottom), so the video needs no editing.
async function caption(label, text) {
  timeline.push({t: ((Date.now() - start0) / 1000).toFixed(1), label, text});
  await p.evaluate(([label, text]) => {
    let bar = document.getElementById('demo-caption');
    if (!bar) {
      bar = document.createElement('div'); bar.id = 'demo-caption';
      Object.assign(bar.style, {position: 'fixed', left: '0', right: '0', bottom: '0', zIndex: '99999', padding: '12px 20px',
        background: 'rgba(17,17,34,0.92)', color: '#fff', font: '500 17px/1.4 Inter, system-ui, sans-serif', display: 'flex', gap: '14px',
        alignItems: 'center', pointerEvents: 'none'});
      document.body.append(bar);
    }
    bar.replaceChildren();
    const tag = document.createElement('span'); tag.textContent = label;
    const colors = {REAL: '#0a7d3e', MOCK: '#8a5a00', OFFLINE: '#3b4cc0', RESULT: '#a1263a'};
    Object.assign(tag.style, {background: colors[label] || '#444', padding: '3px 10px', borderRadius: '4px', font: '700 14px/1.4 monospace', whiteSpace: 'nowrap'});
    const body = document.createElement('span'); body.textContent = text;
    bar.append(tag, body);
  }, [label, text]);
}

async function card(title, lines, label, ms) {
  const html = `<html><body style="margin:0;background:#f6f6fb;font-family:Inter,system-ui,sans-serif;color:#111">
    <div style="padding:70px 90px"><div style="font:600 15px monospace;color:#3b4cc0">MEA Review</div>
    <h1 style="font-size:40px;margin:14px 0 26px">${title}</h1>
    ${lines.map(l => `<p style="font-size:22px;line-height:1.5;margin:10px 0">${l}</p>`).join('')}</div></body></html>`;
  await p.setContent(html);
  if (label) await caption(label[0], label[1]);
  await pause(ms);
}

async function guide(name) {
  await btn('Demo guide').click();
  const dialog = p.getByRole('dialog', {name: 'MEA Review offline demo'});
  await pause(900);
  await btn(name, dialog).click();
  await dialog.waitFor({state: 'hidden'});
}

async function compare(pair, status) {
  await p.locator('#files').getByRole('button', {name: pair}).click();
  if (await btn('Run manual comparison').isVisible()) await btn('Run manual comparison').click();
  await expect(rail().getByText(status, {exact: true}).first()).toBeVisible();
}

try {
  await card('A code review agent on frozen PR snapshots',
    ['Captures a pull request read-only, investigates it with bounded read-only tools,',
     'validates every candidate against cited lines, and lets a human confirm each comment.',
     'Segments are labelled <b>REAL</b> (saved DeepSeek run), <b>MOCK</b> (simulated GitHub) or <b>OFFLINE</b>.'],
    ['OFFLINE', 'All code shown is synthetic. No comment is posted to GitHub in this recording.'], 6000);

  // 1. Real model investigation, opened in the workbench from its task store.
  const realStore = process.env.MEA_DEMO_REAL_STORE;
  if (realStore) {
    const copy = path.join(work, 'real-store');
    await cp(realStore, copy, {recursive: true});
    const base = await serve(copy); allowed.add(base);
    const [summary] = await (await p.request.get(base + '/api/tasks')).json();
    const task = await (await p.request.get(base + '/api/task-json/' + summary.id)).json();
    const fixture = JSON.parse(await readFile(path.join(root, 'fixtures/review/v1.json'), 'utf8'));
    assert.deepEqual(task.view.revisions, fixture.revisions, 'Real recording requires the owned v1 fixture');
    assert.deepEqual(task.pr, fixture.pr);
    assert.equal(task.mode, 'REAL_PROVIDER_DEEPSEEK');
    assert.equal(task.status, 'COMPLETED');
    await p.goto(base + '/?task=' + task.id);
    await expect(rail().getByRole('heading', {name: 'Real model investigation'})).toBeVisible();
    await caption('REAL', `The workbench opens a saved investigation of a synthetic snapshot. DeepSeek Flash made ${task.requests.length} requests and ${task.results.length} read-only tool calls.`);
    await pause(5000);
    const inv = rail().locator('details.investigation > summary');
    await inv.click(); await pause(1200);
    await caption('REAL', 'Tool-call trace: read_diff and references, then read_source (app.py) and read_test (test_app.py), then finish.');
    await rail().getByText('Event timeline', {exact: true}).click(); await pause(4500);
    const tool = rail().locator('details.investigation summary', {hasText: 'Tool 3'}).first();
    await tool.scrollIntoViewIfNeeded(); await tool.click(); await pause(1500);
    const evidence = rail().locator('details.investigation .rail-card', {hasText: 'app.py:4'}).first();
    if (await evidence.count()) {
      await evidence.scrollIntoViewIfNeeded(); await evidence.click();
      await caption('REAL', 'Every tool result cites path, side, revision and line. Clicking a citation opens the frozen line.');
      await pause(4000);
    }
    await inv.click(); await pause(600);
    const cand = rail().getByRole('button', {name: /quota\(\) returning None/}).first();
    await cand.scrollIntoViewIfNeeded(); await pause(800);
    await caption('REAL', 'The candidate passed citation checks against lines returned to the model. A human decides whether the issue is real.');
    await cand.click(); await pause(4500);
    await btn('Open human review').scrollIntoViewIfNeeded();
    await btn('Open human review').click(); await pause(2500);
    await caption('REAL', 'The real candidate opens for human review. This synthetic snapshot supports local preview.');
    await pause(4000);
  } else {
    const base = await serve(path.join(work, 'offline-store')); allowed.add(base);
    await p.goto(base);
    await guide('Completed');
    await expect(rail().getByRole('heading', {name: 'Offline investigation'})).toBeVisible();
    await caption('OFFLINE', 'A deterministic investigation opens the synthetic frozen snapshot.');
    await pause(8000);
    await rail().locator('details.investigation > summary').click();
    await rail().getByText('Event timeline', {exact: true}).click();
    await caption('OFFLINE', 'The trace shows each tool result and its frozen source citations.');
    await pause(10000);
  }

  // 2. Mock human review: per-candidate confirmation, Mock send, uncertain outcome and read-only reconciliation.
  const base = await serve(path.join(work, 'mock-store')); allowed.add(base);
  await p.goto(base);
  await pause(1200);
  await guide('Human review');
  await caption('MOCK', 'Owned Mock fixture: quota() now returns None and the caller subtracts from it.');
  await pause(3500);
  await btn('Open candidate · L2').click(); await pause(1200);
  await rail().getByRole('button', {name: /Select candidate/}).click(); await pause(1500);
  await rail().getByRole('button', {name: /Caller evidence.*app.py:4/}).click();
  await caption('MOCK', 'The draft is bound to the changed line and to the caller evidence the agent cited.');
  await pause(3000);
  await btn('Back to review').click().catch(() => {});
  const body = 'If quota() returns None, available() fails on the subtraction. Please handle the missing value.';
  const box = p.getByRole('textbox', {name: 'Comment text'});
  await box.scrollIntoViewIfNeeded(); await box.fill(''); await box.pressSequentially(body, {delay: 18});
  await btn('Save draft').click(); await pause(1200);
  await btn('Preview exact comment').click(); await pause(1000);
  await p.locator('#exact-preview').scrollIntoViewIfNeeded();
  await caption('MOCK', 'Preview shows the exact text, commit, path, line and side that would be written.');
  await pause(4000);
  await p.getByRole('checkbox', {name: /I reviewed the exact text/}).check(); await pause(800);
  await btn('Confirm Mock comment').click(); await pause(1200);
  await caption('MOCK', 'Confirmation and sending are separate steps. The send writes an intent first and never retries.');
  await btn('Send to Mock').scrollIntoViewIfNeeded(); await pause(1500);
  await btn('Send to Mock').click(); await pause(2500);
  await expect(p.locator('#draft-status')).toHaveText('MOCK_PUBLISHED');
  await p.locator('#draft-status').scrollIntoViewIfNeeded(); await pause(2500);

  // 3. Re-review after new commits: rename and moved line, the old comment stays on its commit.
  await guide('Re-review');
  await compare(/v1\s+→\s+v2/, 'STILL_PRESENT');
  await caption('MOCK', 'New commit v1 → v2: the file was renamed and the line moved. The sent comment stays on v1; nothing is re-posted.');
  await pause(6000);
  // Uncertain send on v2, then read-only reconciliation.
  const mock = await p.request.post(base + '/api/human/mock-state', {data: {version: 'v2', can_comment: true, fault: 'AFTER_EFFECT', complete_listing: true}});
  assert.ok(mock.ok());
  await btn('Human review', p.getByRole('navigation', {name: 'Review cases'})).click();
  await pause(1200);
  await rail().getByText('Workspace history & exports', {exact: true}).click();
  await btn('Open v2').click(); await pause(1200);
  await rail().getByRole('button', {name: /Select candidate/}).click(); await pause(1200);
  await btn('Back to review').click().catch(() => {});
  const moved = 'The moved quota function can still return None.';
  await box.scrollIntoViewIfNeeded(); await box.fill(moved);
  await btn('Save draft').click(); await btn('Preview exact comment').click();
  await p.getByRole('checkbox', {name: /I reviewed the exact text/}).check();
  await btn('Confirm Mock comment').click(); await pause(800);
  await caption('MOCK', 'Fault injected: the simulated GitHub stores the comment but the response is lost.');
  await btn('Send to Mock').click(); await pause(1500);
  await expect(p.locator('#draft-status')).toHaveText('UNCERTAIN');
  await p.locator('#draft-status').scrollIntoViewIfNeeded();
  await caption('MOCK', 'Outcome UNCERTAIN: no automatic resend. Only a read-only lookup may settle it.');
  await pause(4000);
  await btn('Reconcile read-only').click(); await pause(1500);
  await expect(p.locator('#draft-status')).toHaveText('MOCK_PUBLISHED_RECONCILED');
  await caption('MOCK', 'Reconciled: exactly one matching comment found by a read-only listing. Still one comment, no second POST.');
  await pause(4500);
  // Outdated anchor.
  await btn('Re-review', p.getByRole('navigation', {name: 'Review cases'})).click(); await pause(1000);
  await compare(/v3\s+→\s+v4/, 'OUTDATED');
  await pause(1500);
  await caption('MOCK', 'v3 → v4 changes the commented line: OUTDATED. The comment is never moved to a new line automatically.');
  await pause(5500);

  await card('What it measured',
    ['ReviewBench v4, 35 held-out public PRs, blind judge, 3 repetitions:',
     'Agent F1 <b>0.201</b> vs single-request Direct <b>0.143</b>. The +5.8 point gain is below the',
     'pre-registered 10 point bar, so the result is <b>negative</b> and tuning stopped. Agent recall is 13.5%.',
     'Output is a list of candidates for human review, not an automatic reviewer.',
     'Other agents can call the same read-only tools through the MCP server (8 tools, stdio).'],
    ['RESULT', 'Numbers and sources: benchmarks/reviewbench/v4/RESULTS.md'], 9000);
  assert.deepEqual(external, []);
} finally {
  const video = await p.video()?.path();
  await context.close(); await browser.close();
  for (const s of servers) s.kill('SIGINT');
  if (video) await rename(video, path.join(out, 'demo-raw.webm'));
  await writeFile(path.join(out, 'timeline.json'), JSON.stringify({seconds: (Date.now() - start0) / 1000, external, timeline}, null, 2) + '\n');
  await rm(work, {recursive: true, force: true});
}
console.log(JSON.stringify({out, external: external.length, seconds: (Date.now() - start0) / 1000}));
