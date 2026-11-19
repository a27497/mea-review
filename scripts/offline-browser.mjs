import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium, expect} from '@playwright/test';
import {checkReviewProvenance} from './review-provenance-check.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const out = process.env.MEA_E2E_OUT ||
  path.join(root, 'artifacts', 'offline-browser-' + new Date().toISOString().replaceAll(':', '-'));
await mkdir(out, {recursive: true});
const evidence = name => path.join(out, name);
const fixture = name => path.join(root, 'fixtures', 'demo', name + '.json');
const errors = [];
const external = [];
const checks = [];
let server;
let base;
let browser;
let providerWorker;

async function start(port = 0) {
  server = spawn('python3', ['-B', '-m', 'mea', 'serve', '--port', String(port),
    '--task-dir', evidence('task-db')], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  const chunks = [];
  server.stderr.on('data', data => chunks.push(data.toString()));
  base = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Server startup timed out.')), 10_000);
    server.once('error', reject);
    server.once('exit', code => {
      clearTimeout(timeout);
      reject(new Error('Server exited: ' + code + ' ' + chunks.join('')));
    });
    server.stdout.on('data', data => {
      const match = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) {
        clearTimeout(timeout);
        resolve(match[0]);
      }
    });
  });
}

async function stop(signal = 'SIGTERM') {
  if (server && server.exitCode === null && server.signalCode === null) {
    const exited = once(server, 'exit');
    server.kill(signal);
    await exited;
  }
}

try {
  await start();
  browser = await chromium.launch({
    executablePath: process.env.MEA_BROWSER || undefined,
    args: ['--disable-dev-shm-usage'],
  });
  const context = await browser.newContext({viewport: {width: 1365, height: 900}, acceptDownloads: true});
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin === base) {
      await route.continue();
    } else {
      external.push(route.request().url());
      await route.abort();
    }
  });
  await context.addInitScript(() => { if (!localStorage.getItem('mea.language')) localStorage.setItem('mea.language', 'en'); });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  const $ = id => page.locator('#' + id);
  await page.goto(base + '/classic');

  async function upload(name) {
    const response = page.waitForResponse(r => r.url() === base + '/api/review' && r.request().method() === 'POST');
    await $('upload').setInputFiles(fixture(name));
    assert.equal((await response).status(), 200);
    await expect($('status')).toContainText('Human review required');
  }

  async function download(id, name) {
    const pending = page.waitForEvent('download');
    await $(id).click();
    await (await pending).saveAs(evidence(name));
    return readFile(evidence(name), 'utf8');
  }

  async function task(id, full = false) {
    const response = await page.request.get(base + '/api/' + (full ? 'task-json/' : 'task/') + id);
    assert.equal(response.status(), 200);
    return response.json();
  }

  async function create(goal) {
    await $('task-goal').fill(goal);
    const response = page.waitForResponse(r => r.url() === base + '/api/tasks' && r.request().method() === 'POST');
    await $('create-task').click();
    const result = await response;
    assert.equal(result.status(), 200);
    return (await result.json()).id;
  }

  async function reopen(id) {
    await expect($('task-saved').locator('option[value="' + id + '"]')).toBeAttached();
    await $('task-saved').selectOption(id);
    await $('open-task').click();
    await expect($('task-message')).toContainText(id);
  }

  // Open the built-in example, then exercise the actual file-input import separately.
  await expect($('demo').locator('option[value="owned-static-assertion"]')).toBeAttached();
  await $('demo').selectOption('owned-static-assertion');
  await $('load').click();
  await expect($('title')).toContainText('constant assertion');
  await upload('static-assertion');
  await $('findings').getByRole('button', {name: /Assertion cannot check the result/}).click();
  await $('evidence').getByRole('button', {name: 'Open frozen source at line 2'}).click();
  await expect($('source-title')).toContainText('tests/test_total.py · HEAD · ' + 'b'.repeat(40));
  await expect($('source-text').locator('.located')).toHaveText('2      assert True');
  await $('source-side').selectOption('base');
  await expect($('source-text').locator('.located')).toHaveText('2      assert total() == 2');
  await $('source-side').selectOption('head');
  await page.screenshot({path: evidence('static-citation.png'), fullPage: true});
  checks.push('Built-in example and file upload. HEAD and BASE source jump to line 2.');

  const report = JSON.parse(await download('json', 'static-report.json'));
  const snapshot = JSON.parse(await readFile(fixture('static-assertion'), 'utf8'));
  assert.deepEqual(report.revisions, snapshot.revisions);
  assert.equal(report.snapshot_id, snapshot.id);
  assert.equal(report.findings[0].rule, 'ASSERT_TRUE');
  const cite = report.evidence.find(item => item.id === report.findings[0].evidence_ids[0]);
  assert.deepEqual([cite.path, cite.side, cite.start, cite.quote], ['tests/test_total.py', 'head', 2, '    assert True']);
  const markdown = await download('export', 'static-report.md');
  assert.ok(markdown.includes(cite.id) && markdown.includes('assert True'));
  checks.push('Downloaded static JSON and Markdown preserve the observed citation and revisions.');

  const patchOnly = structuredClone(snapshot);
  patchOnly.id = 'owned-patch-only';
  patchOnly.pr.title = 'Synthetic example — patch only';
  delete patchOnly.files[0].sides.head;
  await $('upload').setInputFiles({
    name: 'patch-only.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(patchOnly)),
  });
  await expect($('title')).toContainText('patch only');
  await $('observations').getByRole('button', {name: /test_total.py/}).click();
  await expect($('evidence')).toContainText('HEAD · PATCH_ONLY');
  await $('evidence').getByRole('button', {name: /Open patch at row/}).click();
  await expect($('source-note')).toContainText('Source unavailable');
  await expect($('source-text').locator('.located')).toHaveCount(0);
  await expect($('source-diff').locator('.located')).toHaveText(/\+\s+assert True/);
  checks.push('Patch-only citation opens its exact patch row and claims no full source.');

  const invalid = await page.request.post(base + '/api/review', {
    data: {...snapshot, files: [{...snapshot.files[0], sides: {
      ...snapshot.files[0].sides, head: {...snapshot.files[0].sides.head, text: 'tampered'},
    }}]},
  });
  assert.equal(invalid.status(), 400);
  assert.equal((await page.request.post(base + '/api/review', {
    data: snapshot, headers: {Origin: 'https://outside.example'},
  })).status(), 403);
  assert.equal((await page.request.post(base + '/api/public-capture', {data: {url: snapshot.pr.url}})).status(), 404);
  assert.equal((await page.request.post(base + '/api/tasks', {data: {capture_id: 'unknown'}})).status(), 400);
  checks.push('Tampered snapshot, cross-origin review, live capture and capture-ID task are rejected.');

  await upload('none-return');
  const baselineId = await create('Uninterrupted control');
  await expect($('task-progress')).toContainText('COMPLETED · HYPOTHESIS_SUPPORTED', {timeout: 15_000});
  const baseline = await task(baselineId);
  assert.equal(baseline.provider, 'NONE');
  assert.equal(baseline.mode, 'OFFLINE_POLICY_NOT_AI');
  assert.equal(baseline.results.length, 6);

  const resumedId = await create('Browser restart recovery');
  await expect.poll(async () => (await task(resumedId)).results.length, {
    intervals: [35, 50, 75], timeout: 10_000,
  }).toBeGreaterThan(0);
  const before = await task(resumedId, true);
  assert.equal(before.status, 'RUNNING');
  assert.ok(before.results.length >= 1 && before.results.length < baseline.results.length);
  await writeFile(evidence('before-crash.json'), JSON.stringify(before, null, 2));
  const port = Number(new URL(base).port);
  await stop('SIGKILL');
  await start(port);
  await page.reload();
  await reopen(resumedId);
  await expect($('task-progress')).toContainText('COMPLETED · HYPOTHESIS_SUPPORTED', {timeout: 15_000});
  const after = await task(resumedId, true);
  assert.ok(after.events.some(e => e.kind === 'recovered'));
  assert.deepEqual(after.results, baseline.results);
  assert.deepEqual(after.candidates, baseline.candidates);
  assert.deepEqual(after.usage, baseline.usage);
  assert.deepEqual(after.results.slice(0, before.results.length), before.results);
  assert.equal(after.events.filter(e => e.kind === 'tool_started').length, 6);
  assert.equal(after.events.filter(e => e.kind === 'tool_result').length, 6);
  checks.push('SIGKILL during RUNNING, restart and reopen the same SQLite task with committed evidence intact.');
  checks.push('Recovered task matches the uninterrupted control for all six tool results, candidates and counters.');

  await page.setViewportSize({width: 390, height: 844});
  await $('task-candidates').getByRole('button', {name: 'Open frozen source at src/caller.py:4'}).click();
  await expect($('source-title')).toContainText('src/caller.py · HEAD');
  await expect($('source-text').locator('.located')).toHaveText('4      value = lookup()');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path: evidence('recovered-citation-mobile.png'), fullPage: true});
  assert.deepEqual(JSON.parse(await download('task-json', 'recovered-task.json')), after);
  const taskMarkdown = await download('task-md', 'recovered-task.md');
  assert.ok(taskMarkdown.includes(resumedId) && taskMarkdown.includes('src/caller.py:4'));
  checks.push('On mobile, reopened investigation jumps to the cited source and exports the restored task in both formats.');

  await upload('counterexample');
  const refutedId = await create('Guarded control');
  await expect($('task-progress')).toContainText('COMPLETED · HYPOTHESIS_REFUTED', {timeout: 15_000});
  assert.deepEqual((await task(refutedId)).candidates, []);
  checks.push('Guarded synthetic counterexample refutes the hypothesis and returns no candidates.');

  await $('provider-controls').locator('summary').click();
  await expect($('provider-status')).toContainText('Real model calls are disabled');
  await expect($('create-provider-task')).toBeDisabled();
  await $('demo').selectOption({label: 'Owned offline source adapter case-a'});
  await $('load').click();
  await expect($('title')).toContainText('source adapter case-a');
  await $('confirm-provider').check();
  await expect($('create-provider-task')).toBeDisabled();
  providerWorker = spawn('python3', ['-B', '-m', 'mea.investigation.provider_worker',
    '--mock', '--store', evidence('task-db')], {cwd: root, stdio: 'ignore'});
  await expect($('provider-status')).toContainText('Local Mock worker ready', {timeout: 15_000});
  await expect($('create-provider-task')).toBeEnabled();
  const admitted = page.waitForResponse(r => r.url() === base + '/api/tasks' && r.request().method() === 'POST');
  await $('create-provider-task').click();
  const mockId = (await (await admitted).json()).id;
  await expect($('task-message')).toContainText(mockId);
  await expect($('task-progress')).toContainText('COMPLETED · HYPOTHESIS_SUPPORTED', {timeout: 20_000});
  const mockTask = await task(mockId, true);
  assert.equal(mockTask.mode, 'MOCK_PROVIDER_NOT_AI');
  assert.equal(mockTask.requests.length, 4);
  assert.ok(mockTask.requests.every(r => r.receipt.http_posts_attempted === 0));
  assert.equal(mockTask.results.length, 3);
  assert.deepEqual(JSON.parse(await download('task-json', 'mock-provider-task.json')), mockTask);
  await page.screenshot({path: evidence('mock-provider-mobile.png'), fullPage: true});
  checks.push('Provider rehearsal stays disabled until the separate Mock worker and explicit confirmation are present.');
  checks.push('The owned sample completes four simulated requests and exports three frozen tool results.');
  await checkReviewProvenance(page, base, root, evidence('task-db'));
  checks.push('Opening saved Real and deterministic Mock candidates shows the candidate source separately from Mock writeback.');

  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  const summary = {
    status: 'PASS', checks, provider: after.provider, externalBrowserRequests: external.length,
    browserErrors: errors.length, recoveredTask: resumedId, interruptedTools: before.results.length,
    finalStatus: after.status, finalReason: after.reason, finalUsage: after.usage,
    mockProviderRequests: mockTask.requests.length, mockProviderRealPosts: 0,
  };
  await writeFile(evidence('summary.json'), JSON.stringify(summary, null, 2) + '\n');
  console.log(summary);
} finally {
  if (providerWorker?.exitCode === null && providerWorker?.signalCode === null) {
    const exited = once(providerWorker, 'exit');
    providerWorker.kill('SIGINT');
    await exited;
  }
  await browser?.close();
  await stop();
}
