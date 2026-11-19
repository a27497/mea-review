import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium, expect} from '@playwright/test';

const root = fileURLToPath(new URL('..', import.meta.url));
const out = path.resolve(process.env.MEA_OVERVIEW_E2E_OUT ||
  path.join(root, 'artifacts', 'review-overview-' + new Date().toISOString().replaceAll(':', '-')));
const revision = execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim();
const store = await mkdtemp(path.join(tmpdir(), 'mea-overview-'));
await mkdir(out, {recursive: true});
const server = spawn('python3', ['-B', '-m', 'mea', 'serve', '--port', '0', '--task-dir', store],
  {cwd: root, env: {PATH: process.env.PATH, LANG: 'C.UTF-8'}, stdio: ['ignore', 'pipe', 'pipe']});
const exited = once(server, 'exit');
const screenshots = [], flows = [], errors = [], requests = [], responses = [], external = [];
let browser, origin, log = '';
server.stderr.on('data', data => { log += data; });

async function json(api, route, data) {
  const response = data === undefined ? await api.get(origin + route) : await api.post(origin + route, {data});
  assert.equal(response.status(), 200, route + ': ' + await response.text());
  return response.json();
}
const pin = (review, task) => '/?' + new URLSearchParams({review: review.id, version: task.id, panel: 'review'});
const btn = (page, name) => page.getByRole('button', {name, exact: true});
const exactBody = page => page.locator('#exact-preview .exact-body');
const zhOverview = '审查概览';
const body = 'Mock exact text\nPreserve ./quota.py and the second line exactly. 核对原文。';
const scope = page => page.locator('#overview');
const detail = page => page.locator('#overview-context');
const bodyInput = page => page.locator('#comment-body');
const language = page => page.locator('#language-toggle');

async function shot(page, name, view) {
  await page.evaluate(() => document.fonts.ready);
  const layout = await page.evaluate(() => ({
    width: innerWidth, height: innerHeight, documentWidth: document.documentElement.scrollWidth,
    language: document.documentElement.lang, url: location.pathname + location.search,
  }));
  assert.equal(layout.width, page.viewportSize().width);
  assert.equal(layout.height, page.viewportSize().height);
  assert.equal(layout.width, layout.documentWidth, name + ' introduces page overflow');
  const filename = name + '-' + view + '.png';
  await page.screenshot({path: path.join(out, filename), animations: 'disabled'});
  screenshots.push({filename, ...layout});
}

async function newPage(locale, viewport) {
  const context = await browser.newContext({locale, viewport,
    isMobile: viewport.width === 390, hasTouch: viewport.width === 390});
  await context.route('**/*', route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) {
      external.push(url.href);
      return route.abort();
    }
    requests.push({method: request.method(), path: url.pathname});
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  page.on('response', response => responses.push({status: response.status(), path: new URL(response.url()).pathname}));
  page.on('requestfailed', request => errors.push(request.failure()?.errorText || request.url()));
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  return page;
}

try {
  origin = await new Promise((resolve, reject) => {
    let stdout = '';
    const timer = setTimeout(() => reject(Error('Server startup timeout')), 10000);
    server.once('exit', code => {clearTimeout(timer); reject(Error('Server exited: ' + code + ' ' + log));});
    server.stdout.on('data', data => {
      stdout += data;
      const match = stdout.match(/MEA OFFLINE (http:\/\/127\.0\.0\.1:\d+)\n/);
      if (match) {clearTimeout(timer); resolve(match[1]);}
    });
  });
  browser = await chromium.launch({executablePath: process.env.MEA_BROWSER || undefined,
    args: ['--disable-background-networking', '--no-proxy-server', '--disable-dev-shm-usage']});
  const api = await browser.newContext();
  const capabilities = await json(api.request, '/api/human/status');
  assert.equal(capabilities.live_publish, false);
  assert.equal((await json(api.request, '/api/investigation-provider')).real_enabled, false);
  const demo = version => json(api.request, '/api/human/demo', {version});
  const first = await demo('v1');
  const review0 = await json(api.request, '/api/human/create', {task_id: first.task_id});
  const second = await demo('v2');
  const review = await json(api.request, '/api/human/increment', {review_id: review0.id, task_id: second.task_id});
  const [v1, v2] = review.tasks;
  const finding = v1.candidates[0];
  const caller = finding.citations.find(e => e.path !== finding.change.path);
  assert.equal(caller.path, 'app.py');
  assert.equal(caller.line, 4);

  const snapshot = JSON.parse(await readFile(path.join(root, 'fixtures/demo/none-return.json'), 'utf8'));
  const omitted = 'tests/uncollected_smoke.py', truncated = 'tests/test_lookup.py';
  snapshot.scope.reported_changed_files++;
  snapshot.scope.complete_listing = false;
  snapshot.scope.omitted_sources = [{path: truncated, side: 'head', reason: 'SYNTHETIC_TRUNCATED'}];
  delete snapshot.files.find(f => f.path === truncated).sides.head;
  snapshot.scope.capture = {status: 'INCOMPLETE', context_selection: 'SKIPPED_INCOMPLETE_CHANGE_LISTING',
    gap_counts: {MISSING: 1, TRUNCATED: 1, TRANSPORT: 0},
    gaps: [{kind: 'MISSING', code: 'SYNTHETIC_OMISSION', request: 'files', path: omitted, side: null},
      {kind: 'TRUNCATED', code: 'SYNTHETIC_TRUNCATED', request: 'blob', path: truncated, side: 'head'}]};
  const stopped = await json(api.request, '/api/tasks', {snapshot, budget: {tools: 1}});
  await expect.poll(async () => (await json(api.request, '/api/task/' + stopped.id)).status).toBe('STOPPED');

  const english = await newPage('fr-FR', {width: 1440, height: 900});
  await english.goto(origin);
  await expect(english.getByRole('heading', {name: 'PR Inbox'})).toBeVisible();
  await expect(english.locator('html')).toHaveAttribute('lang', 'en');
  await language(english).focus();
  await english.keyboard.press('Space');
  await expect(english.getByRole('heading', {name: 'PR 收件箱'})).toBeVisible();
  await english.reload();
  await expect(english.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await english.getByRole('link', {name: '导入快照', exact: true}).click();
  await expect(english.getByRole('button', {name: '审查快照'})).toBeVisible();
  await english.locator('#demo').selectOption('owned-static-assertion');
  await btn(english, '审查快照').click();
  await expect(english.locator('#status')).toContainText('需要人工审查');
  await english.locator('#findings').getByRole('button', {name: /Assertion cannot check the result/}).click();
  await btn(english, '打开冻结源码第 2 行').click();
  await expect(english.locator('#source-text .located')).toBeVisible();
  const sourceText = await english.locator('#source-text .located').textContent();
  const goal = english.locator('#task-goal');
  await goal.fill('未提交的目标 draft objective');
  await language(english).press('Enter');
  await expect(goal).toHaveValue('未提交的目标 draft objective');
  await expect(english.locator('#source-text .located')).toHaveText(sourceText);
  await expect(english.getByRole('button', {name: 'Review snapshot'})).toBeVisible();
  await english.getByRole('link', {name: 'Review v3 workspace'}).click();
  await expect(english.locator('html')).toHaveAttribute('lang', 'en');
  await english.context().close();
  flows.push('A non-Zh browser defaults to English. A keyboard override persists on reload and in both directions across Classic and Review v3. Classic keeps an unsaved objective and the exact selected source.');

  let mobile;
  for (const [name, width, height] of [['desktop', 1440, 900], ['tablet', 768, 1024], ['phone', 390, 844]]) {
    const p = await newPage('zh-TW', {width, height});
    if (name === 'phone') mobile = p;
    await p.goto(origin);
    await expect(p.locator('html')).toHaveAttribute('lang', 'zh-CN');
    await expect(p.getByRole('heading', {name: 'PR 收件箱'})).toBeVisible();
    const search = p.getByRole('searchbox', {name: '搜索', exact: true});
    await search.fill('nothing-matches');
    await expect(p.getByText('没有匹配的记录')).toBeVisible();
    await btn(p, '清除筛选').press('Enter');
    await expect(search).toBeFocused();
    await p.getByLabel('记录类型').selectOption('reviews');
    const row = p.locator('.inbox-list li').filter({hasText: v2.pr.title});
    await expect(row).toContainText('调查已完成');
    await shot(p, name, 'zh-inbox');
    const writesBefore = requests.filter(r => r.method === 'POST').length;
    await row.getByRole('link', {name: /^审查概览 · /}).press('Enter');
    await expect(scope(p).getByRole('heading', {name: zhOverview})).toBeVisible();
    const version = p.getByLabel('冻结版本', {exact: true});
    await expect(version).toHaveValue(v2.id);
    await expect(scope(p)).toContainText(v2.view.revisions.head);
    await shot(p, name, 'zh-overview');
    await version.focus();
    await p.keyboard.press('ArrowDown');
    await expect(version).toHaveValue(v1.id);
    await expect(version).toBeFocused();
    await expect(scope(p)).toContainText(v1.view.revisions.head);
    await expect(scope(p)).not.toContainText(v2.view.revisions.head);
    if (name === 'desktop') await expect(scope(p)).toContainText('待人工判断');

    const file = scope(p).getByRole('button', {name: /^quota\.py 统一差异$/});
    await file.press('Enter');
    await expect(detail(p)).toContainText('相关候选问题');
    await expect(detail(p)).toContainText(finding.title);
    await shot(p, name, 'zh-scope');
    await btn(detail(p), '打开代码差异').press('Enter');
    await expect(p.getByRole('region', {name: '统一差异 · quota.py'})).toBeVisible();
    await expect(p.locator('#code')).toContainText('return None');
    await shot(p, name, 'zh-diff');
    await btn(p.locator('#code'), '返回概览').press('Enter');
    await expect(file).toBeFocused();

    await detail(p).getByRole('button', {name: new RegExp(finding.title)}).press('Enter');
    await expect(detail(p)).toContainText(finding.trigger);
    await detail(p).getByRole('button', {name: /支持证据.*app.py:4/}).press('Enter');
    const line = p.locator('#code [data-located]');
    await expect(line).toHaveAttribute('data-head', '4');
    await expect(line).toBeFocused();
    await expect(p.getByRole('region', {name: '冻结 HEAD 源码 · app.py'})).toBeVisible();
    await shot(p, name, 'zh-source-evidence');
    await p.reload();
    await expect(line).toHaveAttribute('data-head', '4');
    await btn(p.locator('#code'), '返回概览').press('Enter');
    await expect(p.locator('#overview-scope-selected')).toBeFocused();
    await expect(detail(p)).toContainText(finding.title);
    await btn(detail(p), '打开此发现项').press('Enter');
    await expect(p.getByRole('heading', {name: '发现项', exact: true})).toBeVisible();
    await expect(p.locator('#active-finding')).toHaveText(finding.title);
    await expect(p.locator('#active-finding')).toBeFocused();
    assert.equal(requests.filter(r => r.method === 'POST').length, writesBefore);

    // A single draft is shared by the three read-only evidence paths.
    if (name === 'desktop') {
      await p.locator('#disposition-ignored').press('Enter');
      await expect(p.locator('#disposition-status')).toHaveText('本地判断 · 已忽略');
      await p.reload();
      await expect(p.locator('#disposition-status')).toHaveText('本地判断 · 已忽略');
      await p.locator('#select-candidate').press('Enter');
      await expect(p.locator('#code [data-located]')).toHaveAttribute('data-head', String(finding.change.line));
      await p.locator('#code').getByRole('button', {name: '返回审查', exact: true}).press('Enter');
      await expect(bodyInput(p)).toBeVisible();
      await bodyInput(p).fill(body);
      await bodyInput(p).evaluate(node => node.setSelectionRange(2, 6));
      const versionUrl = p.url();
      await language(p).press('Enter');
      await expect(bodyInput(p)).toHaveValue(body);
      assert.deepEqual(await bodyInput(p).evaluate(node => [node.selectionStart, node.selectionEnd]), [2, 6]);
      assert.equal(p.url(), versionUrl);
      await expect(language(p)).toBeFocused();
      await language(p).press('Space');
      await btn(p, '保存草稿').press('Enter');
    }
    await expect(bodyInput(p)).toHaveValue(body);
    await expect(btn(p, '预览评论原文')).toBeEnabled();
    await expect(p.locator('#workspace')).not.toHaveAttribute('aria-busy', 'true');
    await btn(p, '预览评论原文').press('Enter');
    await expect(exactBody(p)).toBeVisible();
    assert.equal(await exactBody(p).textContent(), body);
    await expect(p.locator('#exact-preview')).toContainText(v1.view.revisions.head);
    await expect(p.locator('#exact-preview')).toContainText(finding.change.path + ':' + finding.change.line + ' RIGHT');
    await expect(btn(p, '确认 Mock 评论')).toBeDisabled();
    await p.locator('#acknowledge').press('Space');
    await expect(btn(p, '确认 Mock 评论')).toBeEnabled();
    await btn(p, '确认 Mock 评论').scrollIntoViewIfNeeded();
    await shot(p, name, 'zh-exact-confirmation');
    const exactUrl = p.url();
    await language(p).press('Enter');
    await expect(p.locator('html')).toHaveAttribute('lang', 'en');
    await expect(p.locator('#acknowledge')).toBeChecked();
    await expect(btn(p, 'Confirm Mock comment')).toBeEnabled();
    await expect(exactBody(p)).toBeVisible();
    assert.equal(await exactBody(p).textContent(), body);
    assert.equal(p.url(), exactUrl);
    await btn(p, 'Confirm Mock comment').scrollIntoViewIfNeeded();
    await shot(p, name, 'en-exact-confirmation');
    await p.reload();
    await expect(p.locator('html')).toHaveAttribute('lang', 'en');
    await expect(bodyInput(p)).toHaveValue(body);
    assert.equal(new URL(p.url()).searchParams.get('version'), v1.id);
    if (name !== 'phone') await p.context().close();
    flows.push(name + ': Chinese inbox, exact version rollback, file-to-diff, candidate-to-citation, pinned reload, focus return, exact preview and live language switch.');
  }

  const p = mobile;
  await language(p).press('Enter');
  await p.goto(origin + pin(review, v2));
  await expect(p.locator('#intro')).toContainText(v2.view.revisions.head.slice(0, 7));
  await expect(p.locator('#disposition-status')).toHaveText('本地判断 · 待定');
  await json(api.request, '/api/human/mock-state', {version: 'v2', can_comment: true, fault: 'AFTER_EFFECT', complete_listing: true});
  await p.locator('#select-candidate').press('Enter');
  await expect(p.locator('#code [data-located]')).toHaveAttribute('data-head', String(v2.candidates[0].change.line));
  await btn(p.locator('#code'), '返回审查').press('Enter');
  const v2Body = 'Mock exact text for limits.py. 保留评论原文。';
  await bodyInput(p).fill(v2Body);
  await btn(p, '保存草稿').click();
  await expect(btn(p, '预览评论原文')).toBeEnabled();
  await expect(p.locator('#workspace')).not.toHaveAttribute('aria-busy', 'true');
  await btn(p, '预览评论原文').click();
  await expect(exactBody(p)).toBeVisible();
  assert.equal(await exactBody(p).textContent(), v2Body);
  await p.locator('#acknowledge').check();
  await btn(p, '确认 Mock 评论').scrollIntoViewIfNeeded();
  await shot(p, 'phone', 'zh-latest-confirmation');
  await btn(p, '确认 Mock 评论').click();
  await btn(p, '发送到 Mock').click();
  await expect(p.locator('#draft-status')).toContainText('结果未知（UNCERTAIN）');
  await p.reload();
  await expect(p.locator('#draft-status')).toContainText('结果未知（UNCERTAIN）');
  await btn(p, '只读对账').press('Enter');
  await expect(p.locator('#draft-status')).toContainText('MOCK_PUBLISHED_RECONCILED');
  await p.locator('#draft-status').scrollIntoViewIfNeeded();
  await shot(p, 'phone', 'zh-reconciled');
  assert.equal(requests.filter(r => r.path === '/api/human/send' && r.method === 'POST').length, 1);
  const persisted = await json(api.request, '/api/human/review/' + review.id);
  assert.equal(persisted.sends.length, 1);
  assert.equal(persisted.sends[0].payload.body, v2Body);
  assert.equal(persisted.sends[0].payload.commit_id, v2.view.revisions.head);
  let v5;
  for (const version of ['v3', 'v4', 'v5']) {
    const task = await demo(version);
    v5 = await json(api.request, '/api/human/increment', {review_id: review.id, task_id: task.task_id});
  }
  const comparison = await json(api.request, '/api/human/compare-linked',
    {review_id: review.id, old_task_id: v2.id, new_task_id: v5.tasks.at(-1).id});
  assert(comparison.comparisons.some(record => record.rows.some(r => r.status === 'NEEDS_RECHECK')));
  await p.goto(origin + pin(review, v5.tasks.at(-1)) + '&overview=1');
  await expect(scope(p)).toContainText('待复查');
  await p.goto(origin + '/?' + new URLSearchParams({task: stopped.id, overview: '1'}));
  await expect(scope(p)).toContainText('调查已停止');
  await expect(scope(p)).toContainText('TOOL_BUDGET');
  await expect(scope(p)).toContainText('目前只有一个已保存的冻结版本。');
  await p.getByText('采集缺口', {exact: true}).click();
  await expect(scope(p)).toContainText('缺失（MISSING）');
  await expect(scope(p)).toContainText('已截断（TRUNCATED）');
  await expect(scope(p)).toContainText(omitted);
  await expect(scope(p).getByRole('button', {name: new RegExp(omitted)})).toHaveCount(0);
  await shot(p, 'phone', 'zh-stopped-capture');
  flows.push('Chinese exact confirmation, one Mock send with a lost response, read-only reconciliation after refresh, version-bound dispositions, and recorded stopped/missing/truncated evidence.');

  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  const result = {status: 'PASS', revision, chromium: browser.version(), exit_code: 0, passed: flows.length,
    failed: 0, skipped: 0, flows, screenshots, errors, external,
    browserRequests: requests.length, requests, responses, mockSends: 1, realModelRequests: 0, realGitHubWrites: 0};
  await writeFile(path.join(out, 'verification.json'), JSON.stringify(result, null, 2) + '\n');
  console.log('PASS: ' + flows.length + ' flows, ' + screenshots.length + ' screenshots.');
} catch (error) {
  for (const [i, context] of (browser?.contexts() || []).entries()) {
    await context.pages()[0]?.screenshot({path: path.join(out, 'failure-' + i + '.png')}).catch(() => {});
  }
  await writeFile(path.join(out, 'failure.json'), JSON.stringify({revision, flows, errors, external,
    screenshots, message: error.stack, exit_code: 1}, null, 2) + '\n');
  throw error;
} finally {
  await browser?.close();
  if (server.exitCode === null && server.signalCode === null) server.kill('SIGINT');
  await exited;
  await rm(store, {recursive: true, force: true});
  await writeFile(path.join(out, 'server-stderr.log'), log);
}
