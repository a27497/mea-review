import {copy, diagnostic, m, mountLanguage, option, setAttr, setText, setTitle} from './i18n.js';

const $ = id => document.getElementById(id);
const ui = {files: $('files'), code: $('code'), review: $('review')};
const narrow = matchMedia('(max-width: 880px)');
const cases = new Set(['completed', 'human', 'compare']);
const panels = new Set(['files', 'code', 'review']);
const url = new URL(location.href);
const requestedCase = url.searchParams.get('case');
const initialMode = cases.has(requestedCase) ? requestedCase
  : url.searchParams.has('task') ? 'completed'
  : url.searchParams.has('comparison') ? 'compare'
  : url.searchParams.has('review') ? 'human' : 'inbox';
const context = {completed: {}, human: {}, compare: {}};
const state = {
  mode: initialMode,
  overview: url.searchParams.get('overview') === '1' && initialMode !== 'compare',
  panel: panels.has(url.searchParams.get('panel')) ? url.searchParams.get('panel') : 'code',
  task: null, human: null, compareReview: null, versions: [], tasks: [], reviews: [],
  preview: null, acknowledged: false, busy: false, loading: true, compareView: 'anchor', pair: null,
};
const inbox = {loading: true, errors: [], rows: []};
let poll, toastTimer;
const current = () => context[state.mode];
const short = value => value?.slice(0, 7) || '—';
const name = path => path?.split('/').at(-1) || '—';
const repo = pr => pr ? pr.repository + ' #' + pr.number : '';
const label = task => task?.source?.label || short(task?.view.revisions.head);
const sides = {base: 'BASE', head: 'HEAD'};
const anchors = {base: 'LEFT', head: 'RIGHT'};
const terminal = new Set(['COMPLETED', 'STOPPED', 'CANCELLED']);
const owns = review => review?.mode === 'MOCK_OWNED_REVIEW';
const liveReview = review => review?.mode === 'LIVE_GITHUB_REVIEW';
const liveSend = send => send?.mode === 'LIVE_GITHUB';
const humanTask = () => {
  const id = current().taskId;
  return id ? state.human?.tasks.find(t => t.id === id) : state.human?.tasks[0];
};
const candidateSources = {
  REAL_PROVIDER_DEEPSEEK: m('Real model (DeepSeek)'),
  MOCK_REVIEW_DEMO_NOT_AI: m('Deterministic Mock'),
  MOCK_PROVIDER_NOT_AI: m('Mock provider'),
  OFFLINE_POLICY_NOT_AI: m('Offline policy'),
};
const writebackModes = {
  MOCK_GITHUB_NOT_LIVE: m('Mock (loopback)'),
  LIVE_GITHUB: m('GitHub (CLI)'),
  LOCAL_PREVIEW_ONLY: m('Local preview only'),
};
const reviewModes = {
  MOCK_OWNED_REVIEW: m('Owned Mock'),
  LIVE_GITHUB_REVIEW: m('GitHub (CLI)'),
  LOCAL_PREVIEW_ONLY: m('Local preview only'),
};
const activeTask = () => state.mode === 'completed' ? state.task : state.mode === 'human' ? humanTask() : null;
const candidate = () => humanTask()?.candidates.find(c => c.id === current().candidateId) || humanTask()?.candidates[0];
const currentDraft = () => state.human?.drafts.find(d => d.task_id === humanTask()?.id && d.candidate_id === candidate()?.id);
const activePr = () => activeTask()?.pr || (state.mode === 'compare' ? state.compareReview?.pr : null);
const investigationStatuses = new Map([
  ['QUEUED', m('Queued')], ['RUNNING', m('Running')], ['COMPLETED', m('Completed')],
  ['STOPPED', m('Stopped')], ['CANCELLED', m('Cancelled')],
]);
const investigationStatus = status => ({
  QUEUED: m('Investigation queued'), RUNNING: m('Investigation running'), COMPLETED: m('Investigation completed'),
  STOPPED: m('Investigation stopped'), CANCELLED: m('Investigation cancelled'),
})[status] || m('Investigation status unknown');
const statusTone = status => status === 'STOPPED' ? 'danger'
  : ['QUEUED', 'RUNNING', 'COMPLETED'].includes(status) ? 'accent' : 'warn';
const inboxGroups = {tasks: m('Saved investigations'), reviews: m('Saved human reviews')};

function el(tag, value = '', cls = '') {
  const n = document.createElement(tag);
  if (value) setText(n, value);
  if (cls) n.className = cls;
  return n;
}
function button(title, fn, cls = 'button', id) {
  const n = el('button', title, cls);
  n.type = 'button';
  if (id) n.id = id;
  n.addEventListener('click', fn);
  return n;
}
function currentAttr(n, yes) {
  if (yes) n.setAttribute('aria-current', 'true');
  else n.removeAttribute('aria-current');
  return n;
}
function link(text, href, cls = '') {
  const a = el('a', text, cls); a.href = href; return a;
}
function detail(title, ...children) {
  const d = el('details', '', 'details'); d.append(el('summary', title), ...children); return d;
}
function fields(entries) {
  const dl = el('dl', '', 'fields');
  for (const [key, value] of entries) dl.append(el('dt', key), el('dd', value ?? '—'));
  return dl;
}
function block(parent, title, ...children) {
  const section = el('section', '', 'rail-block');
  if (title) section.append(el('h3', title));
  section.append(...children); parent.append(section); return section;
}
function card(title, subtitle, fn, id) {
  const b = button('', fn, 'rail-card', id);
  const row = el('span', '', 'card-title');
  row.append(el('span', title), $('icons').content.cloneNode(true));
  b.append(row, el('small', subtitle));
  return b;
}
const pre = value => el('pre', JSON.stringify(value, null, 2));
const tag = (value, tone = '') => el('span', value, 'tag ' + tone);
function reviewLabels(task) {
  const version = state.human.versions.find(v => v.task_id === task.id);
  const source = candidateSources[version?.candidate_source] || version?.candidate_source || m('Unknown');
  const writeback = writebackModes[version?.writeback_mode] || version?.writeback_mode || m('Unknown');
  return [el('span', m('Candidate source: {source}', {source}), 'tag candidate-source'),
    el('span', m('Writeback: {mode}', {mode: writeback}), 'tag writeback-mode')];
}
const endpoint = (path, id) => path + encodeURIComponent(id);
async function request(path, data) {
  const res = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const json = await res.json();
  if (!res.ok) throw Error(json.error || 'REQUEST_FAILED');
  return json;
}
const human = (path, data) => request('/api/human/' + path, data);
const rawTask = id => request(endpoint('/api/task-json/', id));
function notify(message, error = false) {
  clearTimeout(toastTimer);
  const n = $('notice'); setText(n, message); n.hidden = false;
  n.classList.toggle('error', error);
  toastTimer = setTimeout(() => {n.hidden = true;}, error ? 11000 : 3600);
}
const errorText = {
  TASK_NOT_FOUND: m('This saved task could not be found.'),
  PR_HEAD_DRIFT: m('The Mock PR is on a different frozen head. No comment was sent.'),
  DUPLICATE_REMOTE_ANCHOR: m('A Mock comment already exists on this exact anchor.'),
  REMOTE_PERMISSION_OR_STATE: m('The Mock target does not currently allow comments.'),
  CONFIRMATION_SNAPSHOT_CHANGED: m('The approved payload or Mock target changed. Generate a fresh preview.'),
  LIVE_PUBLISH_DISABLED_NO_TARGET: m('Live publishing is disabled.'),
  COMMENT_BODY_REQUIRED: m('Enter a comment before saving.'),
};
async function act(fn, {reloadOnError = false} = {}) {
  if (state.busy) return;
  state.busy = true;
  $('workspace').setAttribute('aria-busy', 'true');
  const active = document.activeElement;
  try { await fn(); }
  catch (e) {
    if (reloadOnError && state.human) {
      resetPreview();
      try { await refreshHuman(); renderRail(true); } catch { /* report original failure */ }
    }
    notify(errorText[e.message] || m('Action unavailable · {error}', {error: diagnostic(e.message)}), true);
  } finally {
    state.busy = false;
    $('workspace').removeAttribute('aria-busy');
    if (active?.id && !active.isConnected && document.activeElement === document.body) $(active.id)?.focus({preventScroll: true});
  }
}
function action(title, fn, id, primary = false) {
  return button(title, () => act(fn), primary ? 'button primary' : 'button', id);
}
function syncUrl() {
  if (state.loading) return;
  const u = new URL(location.href);
  if (state.mode === 'completed' && state.task) u.searchParams.delete('case'); else u.searchParams.set('case', state.mode);
  u.searchParams.delete('q'); u.searchParams.delete('group'); u.searchParams.delete('status');
  if (state.panel === 'code') u.searchParams.delete('panel'); else u.searchParams.set('panel', state.panel);
  for (const [key, value] of Object.entries({
    overview: state.overview ? '1' : null,
    'scope-file': current().scope?.file, 'scope-candidate': current().scope?.candidate,
    'overview-return': current().fromOverview ? '1' : null,
    task: state.task?.id, review: state.human?.id, comparison: state.compareReview?.id,
    file: current().path, candidate: state.mode === 'human' ? current().candidateId : null,
    version: state.mode === 'human' ? current().taskId : null,
    line: (current().location || current().requestedLocation)?.line,
    side: (current().location || current().requestedLocation)?.side, view: current().sourceSide,
    back: current().returnToFiles ? 'files' : current().returnToReview ? 'review' : null,
    'back-focus': current().returnToReview ? current().returnFocus : null,
    pair: state.mode === 'compare' ? state.pair?.join(':') : null,
    result: state.mode === 'compare' ? context.compare.result : null,
    'compare-view': state.mode === 'compare' && state.compareView === 'caller' ? 'caller' : null,
  })) {
    if (value) u.searchParams.set(key, value); else u.searchParams.delete(key);
  }
  history.replaceState(null, '', u);
}
function setPanel(panel, focus = false) {
  const previous = state.panel;
  state.panel = panel; $('workspace').dataset.panel = panel;
  $('skip').href = state.overview ? '#overview' : '#' + panel;
  currentAttr($('mobile-overview'), state.overview);
  for (const b of document.querySelectorAll('[data-panel][aria-controls]')) currentAttr(b, !state.overview && b.dataset.panel === panel);
  if (focus) ui[panel].focus({preventScroll: true});
  if (narrow.matches && previous !== panel && panel === 'code') restoreCodePosition();
  syncUrl();
}
function restoreCodePosition() {
  if (!current().location || state.panel !== 'code') return;
  requestAnimationFrame(() => {
    const row = ui.code.querySelector('[data-located]');
    row?.scrollIntoView({block:'center', inline:'nearest'});
  });
}
function setCase(mode) {
  $('switch').open = false;
  if (state.mode === mode) return;
  if (current()) {
    current().scroll = Object.fromEntries(Object.entries(ui).map(([key,pane]) => [key,pane.scrollTop]));
    current().panel = state.panel;
    current().overview = state.overview;
  }
  state.mode = mode;
  state.overview = mode !== 'compare' && !!current().overview;
  state.panel = current().panel || 'code';
  render();
  for (const [key,pane] of Object.entries(ui)) pane.scrollTop = current().scroll?.[key] || 0;
  restoreCodePosition();
}
function render() {
  const atInbox = state.mode === 'inbox';
  $('inbox').hidden = !atInbox;
  $('overview').hidden = atInbox || !state.overview;
  $('workspace').hidden = atInbox || state.overview;
  $('mobile-overview').hidden = state.mode === 'compare';
  for (const id of ['intro', 'footer', 'switch']) $(id).hidden = atInbox;
  for (const selector of ['.panes', '.desktop-cases']) document.querySelector(selector).hidden = atInbox;
  currentAttr($('inbox-link'), atInbox);
  $('skip').href = atInbox ? '#inbox' : '#' + state.panel;
  if (atInbox) {
    setTitle(m('PR Inbox · MEA Review'));
    renderInbox();
    return;
  }
  const t = activeTask(), pr = activePr();
  const mock = state.mode === 'compare' ? owns(state.compareReview) : state.mode === 'human' && owns(state.human);
  const title = t?.pr.title || pr?.title || (state.mode === 'compare' ? m('Re-review frozen versions')
    : state.mode === 'human' ? m('Human review') : m('Review a frozen pull request'));
  setTitle(m('{title} · MEA Review', {title}));
  setText(document.querySelector('.switch-label'), narrow.matches ? m('Switch case') : state.mode === 'compare' && mock ? m('Mock fixture') : m('Frozen snapshot'));
  document.querySelectorAll('[data-case]').forEach(b => {
    b.disabled = state.loading;
    currentAttr(b, b.dataset.case === state.mode);
  });
  document.querySelector('.desktop-cases [data-case="compare"]').hidden = state.mode === 'human';
  setText(document.querySelector('[data-panel="code"]'), state.mode === 'compare' ? m('Compare') : m('Diff'));
  const intro = $('intro');
  const meta = el('div', '', 'intro-meta');
  if (t) {
    const frozenHead = tag(m('Frozen HEAD {head}', {head: short(t.view.revisions.head)}), 'accent');
    frozenHead.title = t.view.revisions.head;
    meta.append(frozenHead, tag(investigationStatus(t.status), statusTone(t.status)));
    if (!t.view.coverage.complete_listing || (t.view.coverage.capture && t.view.coverage.capture.status !== 'COMPLETE')) {
      meta.append(tag(m('Partial capture'), 'warn'));
    }
  }
  if (state.mode === 'completed' && t) meta.append(tag(candidateSources[t.mode] || t.mode || m('Unknown mode')));
  if (state.mode === 'human' && t) meta.append(...reviewLabels(t));
  if (state.mode === 'compare' && mock) meta.append(tag('MOCK', 'accent'));
  const tools = el('div', '', 'actions intro-actions');
  if (state.mode !== 'compare' && !state.overview) tools.append(button(m('Overview'),
    () => setOverview(true, true), 'button overview-toggle', 'overview-toggle'));
  if (state.mode === 'human') tools.append(button(m('Re-review'), () => act(async () => {
    if (state.compareReview?.id !== state.human?.id) {
      state.compareReview = state.human;
      context.compare = {};
      state.pair = null;
      state.compareView = 'anchor';
    }
    setCase('compare');
    ui[state.panel].focus({preventScroll: true});
  })));
  tools.append(button(m('Demo guide'), () => $('demo-dialog').showModal(), 'button', 'demo-guide-open'));
  const sub = state.mode === 'compare' ? (mock ? (narrow.matches ? m('{count} frozen versions · Manual comparison', {count: state.versions.length}) : m('Owned {first}–{last} fixtures · Compare frozen evidence by hand', {first: state.versions[0]?.label, last: state.versions.at(-1)?.label})) : m('Compare saved frozen versions by hand'))
    : state.mode === 'human' && t ? fileCount(t)
    : t ? prSummary(t)
      : m('Open a saved task, use the Demo guide, or import a snapshot.');
  intro.replaceChildren(el('p', repo(pr), 'intro-identity mono'), el('h1', title), meta, tools);
  if (sub) meta.append(el('span', sub, 'intro-sub'));
  setText($('footer'), state.mode === 'compare' ? m('Frozen versions    ·    Manual comparison')
    : state.mode === 'human' ? m('Frozen snapshot    ·    Human confirmation')
    : t ? m('Frozen HEAD {head}    ·    {files}', {head: short(t.view.revisions.head), files: fileCount(t)}) : m('Read-only review workspace'));
  if (state.overview) renderOverview();
  else {renderFiles(); renderCode(); renderRail();}
  setPanel(state.panel);
}
const fileCount = t => m('{count} changed files', {count: t.view.coverage.reported_changed_files ?? changed(t).length});
function prSummary(t) {
  return t.view.coverage.complete_listing ? fileCount(t) : m('Partial file listing');
}
const changed = task => task.view.files.filter(f => f.status !== 'context');
function activeFile(t) {
  const c = current();
  return t.view.files.find(f => f.path === c.path) || t.view.files.find(f => f.path === candidateAnchor(t)?.path)
    || [...changed(t)].sort((a,b) => changeCounts(b).removed - changeCounts(a).removed)[0] || t.view.files[0];
}
const candidateAnchor = t => state.mode === 'human' && t === humanTask() ? candidate()?.change : null;
function samePullRequest(a, b) {
  return a.repository === b.repository && a.number === b.number && a.url === b.url;
}

function overviewVersions(task) {
  if (state.mode === 'human') return [...state.human.tasks].reverse();
  const records = state.tasks.filter(row => row.identity?.revisions?.head && samePullRequest(row.pr, task.pr));
  return records.some(row => row.id === task.id) ? records : [...records, task];
}

async function selectOverviewVersion(id) {
  if (state.mode === 'human') {
    selectHumanVersion(id);
  } else {
    const next = await rawTask(id);
    if (!samePullRequest(next.pr, state.task.pr)) throw Error('REVIEW_PR_MISMATCH');
    state.task = next;
    context.completed = {};
    render(); startPoll();
  }
  $('overview').scrollTop = 0;
  $('overview-version')?.focus({preventScroll: true});
}

function setOverview(open, restoreScope = false) {
  state.overview = open;
  render();
  if (open) {
    const selected = restoreScope && $('overview-scope-selected');
    (selected || $('overview-heading')).focus({preventScroll: !selected});
  } else {
    ui[state.panel].focus({preventScroll: true});
  }
}

function setScope(scope) {
  current().scope = scope;
  renderOverview();
  syncUrl();
  $(narrow.matches ? 'overview-context' : 'overview-scope-selected')?.focus();
}

function findEvidenceFile(t, e) {
  return t.view.revisions[e.side] === e.revision &&
    t.view.files.find(f => (e.side === 'base' ? f.base_path : f.path) === e.path);
}

function scopeCode(t, file, side) {
  const ctx = current();
  state.overview = false;
  state.panel = 'code';
  chooseFile(file, t, side, null, false);
  ctx.fromOverview = true;
  // chooseFile may follow the finding's citation and schedule focus on its row.
  render();
  ui.code.focus({preventScroll: true});
  restoreCodePosition();
}

function scopeEvidence(t, e, isChange, origin) {
  if (!locate(t, e, isChange, origin, 'overview')) return;
  current().fromOverview = true;
  state.overview = false;
  render();
  syncUrl();
}

function scopeFinding(t, finding) {
  const ctx = current();
  if (state.mode === 'human') {
    if (candidate()?.id !== finding.id) {
      ctx.editorKey = null; ctx.editorText = null;
      resetPreview();
    }
    ctx.candidateId = finding.id;
  }
  ctx.fromOverview = true;
  state.overview = false;
  state.panel = 'review';
  render();
  ($(state.mode === 'human' ? 'active-finding' : 'finding-' + finding.id) || ui.review).focus();
}

function overviewScope(root, t) {
  const scope = current().scope;
  const selectedFile = t.view.files.find(f => f.path === scope?.file);
  const selectedCandidate = t.candidates.find(c => c.id === scope?.candidate);
  const wrap = el('div', '', 'overview-scope');
  const navigation = el('section', '', 'overview-card scope-navigation');
  navigation.append(el('h3', m('Review scope')));
  const display = el('section', '', 'overview-card scope-display');
  display.id = 'overview-context';
  display.tabIndex = -1;
  const pick = (title, subtitle, selected, scope) => {
    const b = card(title, subtitle, () => setScope(scope), selected ? 'overview-scope-selected' : undefined);
    b.setAttribute('aria-controls', display.id);
    return currentAttr(b, selected);
  };
  for (const [heading, files] of [
    [m('Changed files'), changed(t)],
    [m('Collected context'), t.view.files.filter(f => f.status === 'context')],
  ]) {
    if (!files.length) continue;
    const group = el('details', '', 'scope-group');
    group.open = !selectedCandidate && (!selectedFile || files.includes(selectedFile));
    group.append(el('summary', m('{label} · {count}', {label: heading, count: files.length})));
    for (const file of files) {
      const format = file.patch ? m('Unified diff') : file.sides.base || file.sides.head ? m('Frozen source') : m('Source unavailable');
      group.append(pick(file.path, format, file === selectedFile, {file: file.path}));
    }
    navigation.append(group);
  }
  if (!t.view.files.length) navigation.append(el('p', m('No collected files'), 'rail-sub'));
  const group = el('details', '', 'scope-group');
  group.open = !!selectedCandidate || !selectedFile;
  group.append(el('summary', m('{label} · {count}', {label: m('Candidates'), count: t.candidates.length})));
  for (const c of t.candidates) {
    group.append(pick(c.title, c.change.path + ':' + c.change.line, c === selectedCandidate, {candidate: c.id}));
  }
  if (!t.candidates.length) group.append(el('p', m('No recorded candidates.'), 'rail-sub'));
  navigation.insertBefore(group, navigation.children[1] || null);

  if (selectedFile) {
    const f = selectedFile;
    display.append(el('h3', f.path, 'mono'));
    if (f.base_path !== f.path) display.append(el('p', f.base_path + ' → ' + f.path, 'mono'));
    const actions = el('div', '', 'actions scope-actions');
    if (f.patch) actions.append(button(m('Open diff'), () => scopeCode(t, f, null), 'button primary'));
    for (const side of ['base', 'head']) {
      if (f.sides[side]) actions.append(button(m('Open {side} source', {side: sides[side]}), () => scopeCode(t, f, side)));
    }
    if (!f.patch && !f.sides.base && !f.sides.head) actions.append(el('p', m('No text or diff was collected for this file.'), 'rail-sub'));
    display.append(actions);
    const related = t.candidates.filter(c => [c.change, ...c.citations].some(e => findEvidenceFile(t, e) === f));
    display.append(el('h4', m('Related candidates')));
    for (const c of related) display.append(pick(c.title, c.change.path + ':' + c.change.line, false, {candidate: c.id}));
    if (!related.length) display.append(el('p', m('No recorded candidate cites this file.'), 'rail-sub'));
  } else if (selectedCandidate) {
    const c = selectedCandidate;
    const path = c.change.path + ':' + c.change.line;
    display.append(el('h3', c.title), el('p', path, 'mono'), el('p', c.trigger), el('p', c.impact));
    if (state.mode === 'human') {
      display.append(tag(m('Local judgment · {judgment}', {judgment: dispositionLabels[dispositionFor(t, c)]})),
        tag(diagnostic(candidateStatus(t, c))));
    }
    const actions = el('div', '', 'actions scope-actions');
    actions.append(button(m('Open finding'), () => scopeFinding(t, c), 'button primary'));
    display.append(actions);
    display.append(el('h4', m('Frozen evidence')));
    for (const e of [c.change, ...c.citations.filter(e =>
      e.path !== c.change.path || e.side !== c.change.side || e.line !== c.change.line)]) {
      const file = findEvidenceFile(t, e);
      const title = e === c.change ? m('Changed line') : m('Supporting evidence');
      const where = e.path + ':' + e.line + ' · ' + sides[e.side] + ' · ' + short(e.revision);
      if (!file) {
        display.append(el('p', where, 'mono'), el('p', m('Source unavailable in this frozen version.'), 'rail-sub'));
        continue;
      }
      const evidence = card(title, where, event => scopeEvidence(t, e, e === c.change, event.currentTarget));
      evidence.title = e.revision;
      display.append(evidence);
    }
  } else {
    display.append(el('h3', m('Scope details')), el('p', scope ? m('This scope is unavailable in the selected frozen version.')
      : m('Select a collected file or candidate to see its links to code and evidence.'), 'rail-sub'));
  }
  wrap.append(navigation, display);
  root.append(wrap);
}

function renderOverview() {
  const n = $('overview');
  n.replaceChildren();
  const wrap = el('div', '', 'overview-inner');
  const heading = el('header', '', 'overview-heading');
  const title = el('h2', m('Review overview'));
  title.id = 'overview-heading';
  title.tabIndex = -1;
  heading.append(title, button(m('Return to workspace'), () => setOverview(false)));
  wrap.append(heading);
  n.append(wrap);
  const t = activeTask();
  if (!t) {
    wrap.append(el('p', state.loading ? m('Loading workspace…') : current().evidenceAlert || m('No saved review selected')));
    if (state.human?.tasks.length && !state.loading) wrap.append(...humanVersionButtons());
    return;
  }
  const versions = overviewVersions(t);
  const summary = el('section', '', 'overview-card overview-summary');
  const picker = el('div', '', 'version-picker');
  const label = el('label', m('Frozen version'));
  label.htmlFor = 'overview-version';
  const select = el('select');
  select.id = label.htmlFor;
  const headOf = v => v.view?.revisions.head || v.identity.revisions.head;
  const headCounts = new Map();
  for (const v of versions) headCounts.set(headOf(v), (headCounts.get(headOf(v)) || 0) + 1);
  for (const v of versions) {
    const sha = headOf(v);
    const marker = v.source?.label ? v.source.label + ' · ' : '';
    const identifier = headCounts.get(sha) > 1 ? short(sha) + ' / ' + v.id.slice(5, 12) : short(sha);
    const row = option(m('{version} · {status}', {version: marker + identifier, status: investigationStatus(v.status)}), v.id);
    row.title = sha;
    select.append(row);
  }
  select.value = t.id;
  select.disabled = versions.length < 2;
  select.addEventListener('change', () => {
    const id = select.value;
    select.value = t.id;
    act(() => selectOverviewVersion(id));
  });
  const versionControls = el('div', '', 'overview-version-controls');
  picker.append(label, select);
  versionControls.append(picker);
  if (versions.length < 2) versionControls.append(el('p', m('Only one saved frozen version is available.'), 'rail-sub'));
  versionControls.append(fields([[m('Head SHA'), t.view.revisions.head], [m('Base SHA'), t.view.revisions.base]]));
  const metrics = el('div', '', 'overview-metrics');
  const files = changed(t).length;
  const contextCount = t.view.files.length - files;
  const row = (count, title) => {
    const value = el('div');
    value.append(el('strong', String(count)), el('span', title));
    return value;
  };
  metrics.append(row(files, m('Collected changed files')), row(contextCount, m('Collected context')), row(t.candidates.length, m('Candidates')));
  const status = el('div', '', 'overview-status');
  status.append(tag(investigationStatus(t.status), statusTone(t.status)));
  const latestLinked = state.mode === 'human' && state.human.tasks.at(-1);
  if (latestLinked && latestLinked.id !== t.id) {
    const head = latestLinked.view.revisions.head;
    const later = tag(m('Later linked version · {head}', {head: short(head)}));
    later.title = head;
    status.append(later);
  }
  if (t.reason && t.status !== 'COMPLETED') status.append(tag(diagnostic(t.reason), 'warn'));
  if (!t.view.coverage.complete_listing) status.append(tag(m('Partial file listing'), 'warn'));
  const availableChecks = state.mode === 'human' && state.human.rereviews?.find(r => r.new_task_id === t.id);
  const anchorNeedsCheck = availableChecks && (availableChecks.lineage === 'UNKNOWN' ||
    availableChecks.comments.some(row => row.status === 'NEEDS_RECHECK' || row.status === 'OUTDATED') ||
    availableChecks.candidates.some(row => ['AT_OUTDATED_COMMENT_LOCATION', 'LINEAGE_UNKNOWN'].includes(row.status)));
  const comparisonNeedsCheck = state.mode === 'human' && state.human.comparisons?.some(comparison =>
    comparison.new_task_id === t.id && comparison.rows.some(row => row.status === 'NEEDS_RECHECK'));
  const pending = state.mode === 'human' && t.candidates.filter(c => dispositionFor(t, c) === 'UNDECIDED').length;
  if (anchorNeedsCheck || comparisonNeedsCheck) status.append(tag(m('Recheck required'), 'warn'));
  if (pending) status.append(tag(m('{count} awaiting human judgment', {count: pending})));
  summary.append(versionControls, metrics, status);
  const evidence = el('div', '', 'overview-coverage');
  evidence.append(el('h3', m('Evidence coverage')));
  const reported = t.view.coverage.reported_changed_files;
  if (reported != null && reported !== files) evidence.append(el('p', m('{collected} of {reported} reported changed files were collected.', {collected: files, reported})));
  const capture = t.view.coverage.capture;
  const omissions = t.view.coverage.omitted_sources || [];
  if (capture?.gaps.length) {
    const gaps = detail(m('Capture gaps'));
    const counts = el('div', '', 'overview-status');
    for (const [key, count] of Object.entries(capture.gap_counts)) {
      if (count) counts.append(tag(m('{count} · {kind}', {count, kind: diagnostic(key)}), 'warn'));
    }
    gaps.append(counts);
    for (const g of capture.gaps) gaps.append(el('p', m('{kind} · {detail}', {kind: diagnostic(g.kind),
      detail: [g.code, g.request, g.path, sides[g.side]].filter(Boolean).join(' · ')}), 'mono'));
    evidence.append(gaps);
  } else if (omissions.length) {
    evidence.append(detail(m('Unavailable sources'), ...omissions.map(omission =>
      el('p', [omission.path, sides[omission.side], omission.reason].filter(Boolean).join(' · '), 'mono'))));
  }
  const truncated = t.results.filter(r => r.truncated || r.read?.status === 'TRUNCATED').length;
  if (truncated) evidence.append(el('p', m(truncated === 1 ? '{count} recorded tool result was truncated.'
    : '{count} recorded tool results were truncated.', {count: truncated})));
  if (!evidence.querySelector('p, details')) evidence.append(el('p', capture?.status === 'COMPLETE' ? m('No capture gaps were recorded.')
    : m('No detailed capture gap report is available.'), 'rail-sub'));
  summary.append(evidence);
  wrap.append(summary);
  overviewScope(wrap, t);
}

function sourceLines(text) {
  if (!text) return [];
  // Match the Python snapshot reader's splitlines() numbering.
  const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
  if (lines.at(-1) === '') lines.pop();
  return lines;
}
function patches(patch) {
  const hunks = [];
  let h, base = 0, head = 0;
  for (const text of sourceLines(patch)) {
    const m = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (m) {
      base = +m[1]; head = +m[2];
      h = {base, head, text, rows: []}; hunks.push(h);
    } else if (h && text.startsWith(' ')) h.rows.push({base: base++, head: head++, text: text.slice(1)});
    else if (h && text.startsWith('+')) h.rows.push({head: head++, text: text.slice(1), kind: 'add'});
    else if (h && text.startsWith('-')) h.rows.push({base: base++, text: text.slice(1), kind: 'del'});
    else if (h && text.startsWith('\\')) h.rows.push({text});
  }
  return hunks;
}
function changeCounts(file) {
  const rows = patches(file.patch).flatMap(h => h.rows);
  return {added: rows.filter(r => r.kind === 'add').length, removed: rows.filter(r => r.kind === 'del').length};
}
function delta(added, removed) {
  const n = el('span', '', 'tree-meta');
  if (added) n.append(el('span', '+' + added, 'plus'));
  if (removed) n.append(el('span', '−' + removed, 'minus'));
  return n;
}
function treeRow(file, active, fn, secondary) {
  const b = button('', fn, 'tree-row');
  b.title = file.base_path !== file.path ? file.base_path + ' → ' + file.path : file.path;
  b.append(el('span', active ? '●' : '◇', 'tree-icon'), el('span', name(file.path), 'filename'));
  b.setAttribute('aria-label', file.path);
  b.dataset.path = file.path;
  const counts = changeCounts(file);
  if (secondary) b.append(el('span', secondary, 'tree-meta'));
  else b.append(delta(counts.added, counts.removed));
  return currentAttr(b, active);
}
function chooseFile(f, task, source, origin, followCitation = true) {
  const citation = followCitation && source && candidateAnchor(task) && candidate()?.citations.find(e => (e.side === 'base' ? f.base_path : f.path) === e.path);
  if (citation) {
    return locate(task, citation, false, origin, 'files');
  }
  Object.assign(current(), {
    path: f.path, sourceSide: source, location: null, requestedLocation: null, hunk: null, evidenceAlert: null,
    returnToReview: false, returnToFiles: false, returnOrigin: null, returnFocus: null,
  });
  renderFiles(); renderCode(); ui.code.scrollTop = 0;
  if (narrow.matches) setPanel('code', true);
  else {
    ui.files.querySelector('[aria-current]')?.focus({preventScroll: true});
    syncUrl();
  }
}
function renderFiles() {
  const n = ui.files; n.replaceChildren();
  if (state.mode === 'compare') return compareFiles(n);
  const t = activeTask();
  if (!t) { n.append(el('h2', m('Files'), 'tree-title')); return; }
  const filter = el('input', '', 'file-search');
  filter.id = 'file-search';
  filter.type = 'search';
  setAttr(filter, 'placeholder', m('File name or path'));
  filter.value = current().fileSearch || '';
  const caption = el('label', m('Filter files'), 'file-filter');
  caption.htmlFor = filter.id;
  const list = el('div');
  const draw = () => {
    list.replaceChildren();
    const term = filter.value.trim().toLowerCase();
    const matches = f => [f.path, f.base_path].some(path => path?.toLowerCase().includes(term));
    const changes = changed(t).filter(matches);
    const contexts = t.view.files.filter(f => f.status === 'context' && matches(f));
    const active = activeFile(t);
    for (const [heading, files] of [[m('Changed'), changes], [m('Collected context'), contexts]]) {
      if (!files.length) continue;
      list.append(el('h3', heading, 'tree-group'));
      for (const f of files) {
        const evidence = candidateAnchor(t) && candidate()?.citations.find(e => (e.side === 'base' ? f.base_path : f.path) === e.path);
        const side = f.sides.head ? 'head' : 'base';
        const sourceOnly = f.status === 'context' || !f.patch;
        const label = evidence ? 'L' + evidence.line : f.status === 'context' ? sides[side] : m('No diff');
        list.append(treeRow(f, f === active, event => chooseFile(f, t, sourceOnly ? side : null, event.currentTarget),
          sourceOnly ? label : null));
      }
    }
    if (!changes.length && !contexts.length) list.append(el('p', term ? m('No matching files') : m('No collected files'), 'muted'));
  };
  caption.append(filter);
  filter.addEventListener('input', () => {current().fileSearch = filter.value; draw();});
  n.append(el('h2', m('Files'), 'tree-title'), caption, list);
  draw();
  n.append(el('p', t.view.coverage.complete_listing ? m('Complete file listing') : m('Partial file listing'), 'tree-footer'));
}
const keywords = new Set(('def class import from as return assert for in if elif else and or not is try except finally with lambda yield while raise async await None True False del pass break continue nonlocal global match case').split(' '));
function syntax(code, str) {
  // Token spans are presentation only. The frozen string is never parsed as HTML.
  const rx = /#[^\n]*|"""|'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b[A-Za-z_]\w*\b|\b\d+(?:\.\d+)?\b/g;
  let at = 0;
  for (const m of str.matchAll(rx)) {
    if (m.index > at) code.append(str.slice(at,m.index));
    const word = m[0];
    const cls = word.startsWith('#') ? 'comment' : /^["']/.test(word) ? 'string'
      : keywords.has(word) ? 'key' : /^\d/.test(word) ? 'num' : '';
    code.append(cls ? el('span', word, 'tok-' + cls) : word);
    at = m.index + word.length;
  }
  code.append(str.slice(at));
}
function codeRow(r, loc, single = false) {
  const selected = loc && r[loc.side] === loc.line;
  const row = el('div', '', 'code-line' + (r.kind ? ' ' + r.kind : '') + (selected ? ' located' : ''));
  if (r.base) row.dataset.base = r.base;
  if (r.head) row.dataset.head = r.head;
  if (selected) { row.tabIndex = -1; row.dataset.located = ''; }
  const no = value => el('span', value == null ? '' : String(value), 'line-no');
  if (single) row.append(no(r.head ?? r.base));
  else row.append(no(r.base), no(r.head), el('span', r.kind === 'add' ? '+' : r.kind === 'del' ? '−' : '', 'mark'));
  const code = el('code'); syntax(code, r.text); row.append(code);
  return row;
}
function frozenLines(file, side) {
  return sourceLines(file?.sides?.[side]?.text);
}
function defaultLocation(t, file) {
  if (!file) return null;
  if (state.mode === 'human') {
    if (!currentDraft() && !current().candidateId) return null;
    const a = candidate()?.change;
    return a && (a.side === 'base' ? file.base_path : file.path) === a.path ? a : null;
  }
  const summary = t.events.findLast(e => e.kind === 'finished')?.summary || '';
  const ref = [...summary.matchAll(/([^\s(]+)\s+line\s+(\d+)/g)].find(m => m[1] === file.path);
  return ref ? {side:'head', line:+ref[2]} : null;
}
function locate(t, e, preferDiff = false, origin = document.activeElement, originPanel = 'review') {
  const f = t.view.files.find(f => (e.side === 'base' ? f.base_path : f.path) === e.path);
  const lines = frozenLines(f, e.side);
  const stale = !f || t.view.revisions[e.side] !== e.revision
    || (e.view_sha256 && t.identity?.view_sha256 !== e.view_sha256)
    || (e.content_sha256 && f.sides[e.side]?.sha256 !== e.content_sha256)
    || (typeof e.quote === 'string' && lines[e.line - 1] !== e.quote);
  if (stale) return notify(m('That citation does not match the collected path, side and revision.'), true);
  const hit = patches(f.patch).some(h => h.rows.some(r => r[e.side] === e.line));
  const source = Number.isSafeInteger(e.line) && e.line > 0 && lines.length >= e.line;
  if (!hit && !source) return notify(m('That line was not collected for this frozen path and side.'), true);
  Object.assign(current(), {
    path: f.path, location: {side:e.side, line:e.line}, sourceSide: hit && (preferDiff || !source) ? null : e.side,
    hunk: null, evidenceAlert: null, requestedLocation: null,
    returnToReview: originPanel === 'review', returnOrigin: origin, returnFocus: origin?.id || null,
    returnToFiles: originPanel === 'files',
  });
  renderFiles(); renderCode(); setPanel('code');
  requestAnimationFrame(() => {
    const row = ui.code.querySelector('[data-located]');
    row?.scrollIntoView({block:'center', inline:'nearest'});
    row?.focus({preventScroll:true});
  });
  return true;
}
function returnFromCode(panel) {
  const c = current();
  setPanel(panel);
  const active = panel === 'files' ? ui.files.querySelector('[aria-current]') : $('comment-body');
  const target = c.returnOrigin?.isConnected ? c.returnOrigin : (c.returnFocus && $(c.returnFocus)) || active || ui[panel];
  (ui[panel].contains(target) ? target : ui[panel]).focus();
}
const codeOverflow = new ResizeObserver(entries => {
  for (const {target} of entries) {
    const region = target.closest('.diff');
    if (region.clientWidth && region.scrollWidth > region.clientWidth) region.tabIndex = 0;
    else region.removeAttribute('tabindex');
  }
});
function appendCodeListing(parent, listing, title) {
  const region = el('div', '', 'diff');
  region.setAttribute('role', 'region');
  setAttr(region, 'aria-label', title);
  region.append(listing);
  parent.append(region);
  codeOverflow.observe(region);
  codeOverflow.observe(listing);
}
function renderCode() {
  codeOverflow.disconnect();
  const n = ui.code; n.replaceChildren();
  if (state.mode === 'compare') return compareCode(n);
  const t = activeTask();
  if (!t) {
    const box = el('div', '', 'empty');
    box.append(el('h2', state.loading ? m('Loading workspace…') : state.mode === 'human' ? m('Open a human review') : m('No saved review selected')));
    if (!state.loading) {
      box.append(el('p', current().evidenceAlert || m('Reopen a saved record from the PR Inbox.'), 'muted'),
        link(m('PR Inbox'), '/', 'button'));
      box.append(' ', link(m('Import & task controls'), '/classic', 'button'));
    }
    n.append(box); return;
  }
  const c = current(), f = activeFile(t);
  if (!f) {
    if (c.evidenceAlert) n.append(el('p', c.evidenceAlert, 'evidence-alert'));
    n.append(el('p', m('No frozen files were collected.'), 'empty'));
    return;
  }
  const s = c.sourceSide || (!f.patch ? (f.sides.head ? 'head' : 'base') : null);
  const path = s === 'base' ? f.base_path : f.path;
  const h = el('header', '', 'code-head');
  const pathInfo = el('div', '', 'code-path');
  pathInfo.append(el('strong', name(path)));
  if (path !== name(path)) pathInfo.append(el('span', path, 'mono'));
  h.append(pathInfo);
  const tools = el('div', '', 'controls');
  const loc = c.location || (!c.evidenceAlert && defaultLocation(t,f));
  const revision = side => {
    const value = el('span', sides[side] + ' ' + short(t.view.revisions[side]));
    value.title = t.view.revisions[side];
    return value;
  };
  if (s) {
    tools.append(revision(s));
    if (f.patch) tools.append(button(m('Unified diff'), () => {c.sourceSide = null; c.location = null; renderCode(); syncUrl();}));
  } else {
    tools.append(revision('base'), el('span', '→'), revision('head'));
  }
  if (loc) tools.append(tag(sides[loc.side] + ' · L' + loc.line, 'accent'));
  if (c.returnToReview) tools.append(button(m('Back to review'), () => returnFromCode('review')));
  if (c.fromOverview) tools.append(backToOverview());
  if (c.returnToFiles) tools.append(button(m('Back to files'), () => returnFromCode('files')));
  h.append(tools); n.append(h);
  if (c.evidenceAlert) n.append(el('p', c.evidenceAlert, 'evidence-alert'));
  const listing = el('div');
  if (s) {
    const lines = frozenLines(f,s);
    listing.append(el('div', m('Frozen {side} · {head}', {side: sides[s], head: short(t.view.revisions[s])}), 'hunk'));
    for (const [i, text] of lines.entries()) listing.append(codeRow({[s]:i+1, text}, loc));
    if (!lines.length) listing.append(el('p', m('No text is available for this side.'), 'empty'));
  } else {
    const hunks = patches(f.patch);
    const picked = c.hunk ?? Math.max(0,hunks.findIndex(h => loc && h.rows.some(r => r[loc.side] === loc.line)));
    if (hunks.length > 1) {
      const ctrl = el('div', '', 'hunk-switch');
      const next = (picked + 1) % hunks.length;
      const dest = hunks[next];
      const stop = dest.rows.findLast(r => r.head)?.head ?? dest.head;
      const title = m(dest.head === 1 ? 'Imports · L{start}–{stop}' : 'Hunk · L{start}–{stop}', {start: dest.head, stop});
      ctrl.append(button(title, () => {c.hunk = next; renderCode(); ui.code.scrollTop = 0; ui.code.querySelector('.hunk-switch button')?.focus();}));
      n.append(ctrl);
    }
    for (const [i, hunk] of hunks.entries()) {
      const group = el('section');
      group.hidden = narrow.matches && picked !== i;
      const title = el('h3','','hunk');
      const split = hunk.text.indexOf('@@',2)+2;
      title.append(hunk.text.slice(0,split),el('span',hunk.text.slice(split),'hunk-context'));
      group.append(title);
      for (const row of hunk.rows) group.append(codeRow(row, loc));
      listing.append(group);
    }
    if (!hunks.length) listing.append(el('p', m('No unified diff was collected for this file.'), 'empty'));
  }
  appendCodeListing(n, listing, m('{view} · {path}', {view: s ? m('Frozen {side} source', {side: sides[s]}) : m('Unified diff'), path}));
  const a = candidateAnchor(t);
  if (!s && a && (a.side === 'base' ? f.base_path : f.path) === a.path) {
    const note = el('div', '', 'candidate-note');
    note.append(el('p', m('A conditional candidate is attached to the changed line.')),
      button(m('Open candidate · L{line}', {line: a.line}), () => setPanel('review', true)));
    n.append(note);
  } else {
    n.append(el('p', s ? m('Frozen source') : m('Frozen unified diff'), 'code-foot'));
  }
}

function locationCard(e, task, title = name(e.path), subtitle = sides[e.side] + ' · L' + e.line, preferDiff = false, id) {
  const b = card(title, subtitle, event => locate(task, e, preferDiff, event.currentTarget), id);
  const source = sides[e.side] + ' · ' + short(e.revision);
  b.append(el('small', e.path + ':' + e.line + ' · ' + source, 'mono'));
  b.title = e.path + ' · ' + sides[e.side] + ' · L' + e.line + ' · ' + e.revision;
  return b;
}
function lazy(title, build) {
  const d = detail(title);
  d.addEventListener('toggle', () => {
    if (d.open && d.childElementCount === 1) d.append(...build());
  });
  return d;
}
function exportsFor(task, review) {
  const wrap = el('div', '', 'actions');
  for (const [ext, route] of (review
    ? [['json','/api/human/export-json/'],['md','/api/human/export-md/']]
    : [['json','/api/task-json/'],['md','/api/task-export/']])) {
    const a = link(ext === 'json' ? m('Export JSON') : m('Export Markdown'), endpoint(route,(review || task).id), 'button');
    a.download = (review || task).id + '.' + ext;
    wrap.append(a);
  }
  return wrap;
}
function renderCompleted(n, t) {
  // Real and Mock provider tasks are labelled as such; their request count comes from the task record.
  const heading = {REAL_PROVIDER_DEEPSEEK:m('Real model investigation'), MOCK_PROVIDER_NOT_AI:m('Mock provider investigation')}[t?.mode];
  n.append(el('h2',heading || m('Offline investigation'),'rail-heading'));
  if (!t) { n.append(el('p', m('No saved investigation is open.'), 'rail-sub')); return; }
  const results = t.results || [];
  const zero = t.status === 'COMPLETED' && t.reason === 'ZERO_CANDIDATES';
  const outcome = t.status === 'STOPPED' ? m('Investigation stopped') : t.status === 'CANCELLED' ? m('Investigation cancelled')
    : zero ? m('No supported candidate') : t.candidates.length ? m(t.candidates.length === 1 ? '{count} conditional candidate' : '{count} conditional candidates', {count: t.candidates.length}) : m('Investigation status');
  n.append(el('h3', outcome, 'outcome'), el('span', diagnostic(t.reason || t.status) || m('Unknown status'), 'outcome-code tag ' + statusTone(t.status)));
  if (t.status === 'STOPPED' && t.usage && t.budget) {
    n.append(el('p', m('{tools} of {toolBudget} tool calls · {rounds} of {roundBudget} decision rounds used. Recorded evidence may be incomplete.', {tools: t.usage.tools, toolBudget: t.budget.tools, rounds: t.usage.rounds, roundBudget: t.budget.rounds}), 'rail-sub'));
  }
  if (zero) n.append(el('p', m('No supported candidate emerged from the collected evidence. Defects may remain. Merge readiness was not assessed.'), 'rail-sub'));
  else n.append(el('p', t.notice || m('Only collected source was reviewed.'), 'rail-sub'));
  if (t.status === 'QUEUED' || t.status === 'RUNNING') {
    n.append(action(m('Cancel investigation'),async () => {
      await request(endpoint('/api/task-cancel/',t.id), {});
      state.task = await rawTask(t.id); render();
    }, 'cancel-investigation'));
  }
  const seen = new Map();
  for (const e of results.flatMap(r => r.evidence || [])) {
    if (e.side !== 'head' && e.side !== 'base') continue;
    const key = e.side + ':' + e.path;
    if (!seen.has(key)) seen.set(key, new Map());
    seen.get(key).set(e.line, e);
  }
  const headCount = [...seen.values()].reduce((sum, rows) => sum + ([...rows.values()][0]?.side === 'head' ? rows.size : 0), 0);
  const metrics = el('div','','metrics');
  for (const [value, title] of [[(t.requests || []).length,m('Model requests')],
    [results.filter(r => r.tool === 'read_source').length,m('Source reads')],[headCount,m('HEAD lines')]]) {
    const d = el('div'); d.append(el('strong',String(value)),el('span',title)); metrics.append(d);
  }
  const investigation = lazy(m('Agent investigation'), () => {
    const timeline = el('ol','','timeline');
    for (const e of t.events) timeline.append(el('li',e.summary));
    const output = results.map((r, i) => lazy(m('Tool {number} · {tool}', {number: i + 1, tool: r.tool + (r.read ? ' · ' + name(r.read.path) : '')}), () => [
      r.read ? fields([[m('Path'),r.read.path],[m('Returned'),r.read.returned_start + '–' + r.read.returned_end],[m('Status'),diagnostic(r.read.status)]]) : el('p',r.coverage),
      ...(r.truncated ? [tag(diagnostic('TRUNCATED'), 'warn')] : []),
      ...r.evidence.map(e => locationCard(e,t,name(e.path) + ':' + e.line, e.quote)),
      lazy(m('Raw feedback, limits and citations'), () => [pre(r)])]));
    return [el('p',m('Saved timeline and frozen tool feedback.')),detail(m('Event timeline'),timeline),...output];
  });
  const investigationSummary = investigation.querySelector('summary');
  investigationSummary.className = 'rail-card';
  investigationSummary.replaceChildren();
  const title = el('span','','card-title'); title.append(el('span',m('Tool investigation')),$('icons').content.cloneNode(true));
  investigationSummary.append(title,el('small',m('Event timeline and saved read results')));
  investigation.classList.add('investigation');
  block(n,'',metrics,investigation);
  const final = t.events.findLast(e => e.kind === 'finished')?.summary;
  const checked = block(n,m('Checked scope'));
  if (final && zero && final !== t.reason) checked.append(el('p',final.split(/(?<=\.)\s+(?=[A-Z])/)[0],'rail-sub'));
  else checked.append(el('p',seen.size ? m('Frozen source returned by the recorded tools.') : m('No source lines were returned in this task.'), 'rail-sub'));
  for (const lines of seen.values()) {
    const keys = [...lines.keys()].sort((a,b) => a-b);
    // Never claim a continuous coverage range across unobserved gaps.
    let first = keys[0], last = first;
    const append = () => checked.append(locationCard(lines.get(first), t, name(lines.get(first).path),
      m('{range} · {lines}', {range: sides[lines.get(first).side] + ' ' + first + (first === last ? '' : '–' + last), lines: m('{count} lines', {count: last - first + 1})}),
      false, 'checked-' + lines.get(first).id));
    for (const i of keys.slice(1)) {
      if (i !== last+1) {append(); first = i;}
      last = i;
    }
    append();
  }
  const unknown = block(n,m('Unknown'),el('p',m('Runtime and test outcomes, uncollected callers, dynamic dispatch and inputs outside the inspected path.'),'rail-sub'));
  const rest = final?.match(/Remaining risk.*$/i)?.[0];
  if (rest) unknown.append(detail(m('Reported limitations'),el('p',rest)));
  if (!t.view.coverage.complete_listing) unknown.append(el('p',m('The frozen file listing is incomplete.'),'rail-sub'));
  const truncated = results.filter(result => result.truncated || result.read?.status === 'TRUNCATED');
  if (truncated.length) unknown.append(el('p', m(truncated.length === 1 ? '{count} recorded tool result was truncated.' : '{count} recorded tool results were truncated.', {count: truncated.length}), 'rail-sub'));
  const capture = t.view.coverage.capture;
  if (capture && capture.status !== 'COMPLETE') {
    const counts = Object.entries(capture.gap_counts).filter(([,v]) => v).map(([k,v]) => v + ' ' + k).join(', ');
    const row = el('p',m('Snapshot capture incomplete · context selection {selection} · {counts}.', {selection: capture.context_selection, counts}),'rail-sub');
    row.id = 'capture-status';
    unknown.append(row, detail(m('Capture gaps'), ...capture.gaps.map(g => el('p', [g.kind, g.code, g.request, g.path].filter(Boolean).join(' · '), 'mono'))));
  }
  if (t.candidates.length) block(n,m('Candidates'),
    ...t.candidates.map(c => locationCard(c.change,t,c.title,name(c.change.path) + ':' + c.change.line,true, 'finding-' + c.id)),
    ...(t.status !== 'COMPLETED' ? [] : [
    action(m('Open human review'), async () => {
      state.human = await human('create',{task_id:t.id});
      context.human = {taskId:t.id}; resetPreview(); setCase('human');
      if (narrow.matches) setPanel('review', true);
    })]));
  block(n,'',lazy(m('Snapshot details & exports'), () => [
    fields([[m('Task'),t.id],[m('Base SHA'),t.view.revisions.base],[m('Head SHA'),t.view.revisions.head]]),
    detail(m('Original PR text'),el('h3',t.pr.title),el('p',t.pr.body)),
    lazy(m('Budget and recorded usage'), () => [pre({budget:t.budget,usage:t.usage,coverage:t.view.coverage})]), exportsFor(t)
  ]));
}
function resetPreview() { state.preview = null; state.acknowledged = false; }
function editorKey(d) { return d.id + ':' + d.revision; }
async function refreshHuman() {
  state.human = await human(endpoint('review/',state.human.id));
  if (state.compareReview?.id === state.human.id) state.compareReview = state.human;
}
function pickCandidate(t, c, origin) {
  const ctx = current();
  ctx.candidateId = c.id; ctx.editorText = null; ctx.editorKey = null; resetPreview();
  renderRail(true); locate(t,c.change,true,origin);
}
function selectHumanVersion(id) {
  Object.assign(current(), {
    taskId:id, candidateId:null, path:null, location:null, requestedLocation:null, evidenceAlert:null,
    sourceSide:null, editorText:null, editorKey:null, scope:null, fromOverview:false,
    returnToReview:false, returnToFiles:false, returnOrigin:null, returnFocus:null, fileSearch:'', hunk:null,
  });
  resetPreview(); render();
  for (const pane of Object.values(ui)) pane.scrollTop = 0;
  if (!state.overview) ui.review.focus({preventScroll: true});
}
function readStatus(d) {
  return d.status === 'CONFIRMED_MOCK_ONLY' ? 'CONFIRMED' : d.status === 'CONFIRMED_LIVE' ? 'CONFIRMED · LIVE' : d.status || 'UNKNOWN';
}
function candidateBlock(t, candidateId, side, draft) {
  const check = (state.human.rereviews || []).find(x => x.new_task_id === t.id)?.candidates.find(x => x.candidate_id === candidateId);
  if (check && check.status !== 'NEW') return check;
  if (side === 'head' || draft?.anchor.side === 'head') {
    return state.human.versions.find(x => x.task_id === t.id)?.head_draft_block;
  }
}
function candidateStatus(t, c) {
  const d = state.human.drafts.find(x => x.task_id === t.id && x.candidate_id === c.id);
  return candidateBlock(t, c.id, c.change.side, d)?.status || (d ? readStatus(d) : 'NOT DRAFTED');
}
const dispositionLabels = {ACCEPTED: m('Accepted'), IGNORED: m('Ignored'), UNDECIDED: m('Undecided')};
function dispositionFor(t, c) {
  return state.human.dispositions?.find(row => row.task_id === t.id && row.candidate_id === c.id)?.disposition || 'UNDECIDED';
}
function renderDisposition(n, t, c) {
  const selected = dispositionFor(t, c);
  const status = el('p', m('Local judgment · {judgment}', {judgment: dispositionLabels[selected] || selected}), 'rail-sub');
  status.id = 'disposition-status';
  status.setAttribute('role', 'status');
  const controls = el('div', '', 'actions disposition-actions');
  controls.setAttribute('role', 'group');
  setAttr(controls, 'aria-label', m('Human judgment'));
  for (const [value, title] of [['ACCEPTED',m('Accept')], ['IGNORED',m('Ignore')], ['UNDECIDED',m('Undecided')]]) {
    const control = action(title, async () => {
      if (value === dispositionFor(t, c)) return;
      const review = state.human;
      const row = await human('disposition', {review_id: review.id, task_id: t.id, candidate_id: c.id, disposition: value});
      review.dispositions = [...(review.dispositions || []).filter(existing => existing.id !== row.id), row];
      for (const b of controls.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b === control));
      setText(status, m('Local judgment · {judgment}', {judgment: dispositionLabels[row.disposition]}));
    }, 'disposition-' + value.toLowerCase(), value === 'ACCEPTED');
    control.setAttribute('aria-pressed', String(value === selected));
    controls.append(control);
  }
  block(n, m('Human judgment'), status, controls,
    el('p', m('Saved in this local workspace. Comments require separate drafting and exact confirmation.'), 'rail-sub'));
}
function renderEditor(n,t,c,d) {
  const ctx = current();
  const editable = !d.intent_id;
  if (ctx.editorKey !== editorKey(d)) {
    ctx.editorKey = editorKey(d); ctx.editorText = d.body; resetPreview();
  }
  const section = block(n, m('Comment draft'));
  section.classList.add('editor');
  const status = readStatus(d);
  const draftTag = tag(diagnostic(status),status === 'UNCERTAIN' ? 'warn' : 'accent'); draftTag.id = 'draft-status';
  section.append(el('p',m('Revision {revision} · {anchor}', {revision: d.revision, anchor: name(d.anchor.path) + ':' + d.anchor.line + ' ' + anchors[d.anchor.side]}), 'rail-sub'),draftTag);
  const lab = el('label',m('Comment text')); lab.htmlFor = 'comment-body';
  const body = el('textarea'); body.id = 'comment-body'; body.value = ctx.editorText;
  body.maxLength = 8000; body.readOnly = !editable; body.spellcheck = true;
  section.append(lab,body);
  if (editable) {
    const commands = el('div','','actions');
    const save = action(m('Save draft'),async () => {
      const saved = await human('edit',{draft_id:d.id,body:body.value,anchor:d.anchor});
      ctx.editorKey = editorKey(saved); ctx.editorText = saved.body; resetPreview();
      await refreshHuman(); renderRail(true); notify(m('Draft saved.'));
    },'save-draft');
    const previewButton = action(m('Preview exact comment'),async () => {
      state.preview = await human('preview',{draft_id:d.id}); state.acknowledged = false;
      renderRail(true); $('exact-preview')?.focus();
    },'preview-comment',true);
    const changed = () => body.value !== d.body;
    save.disabled = !changed(); previewButton.disabled = changed();
    body.addEventListener('input', () => {
      ctx.editorText = body.value; resetPreview();
      section.querySelector('#exact-preview')?.remove();
      save.disabled = !changed(); previewButton.disabled = changed();
      if ($('mock-send')) $('mock-send').disabled = changed() || !!ctx.invalidateMock;
    });
    commands.append(save, previewButton); section.append(commands);
    if (state.preview?.draft_id === d.id && !changed()) {
      const p = state.preview;
      const exact = el('section','','rail-block'); exact.id = 'exact-preview'; exact.tabIndex = -1;
      exact.append(el('h3',m('Exact comment preview')),fields([[m('Target'),repo(p.target)],[m('Commit'),p.payload.commit_id],
        [m('Diff anchor'),p.payload.path + ':' + p.payload.line + ' ' + p.payload.side],[m('Mode'),p.mode]]),
        el('pre',p.payload.body,'exact-body'));
      otherComments(exact,p.other_comments_at_anchor);
      if (p.mode === 'MOCK_GITHUB_NOT_LIVE' && p.confirmation_digest !== d.confirmation?.confirmation_digest) {
        const ack = el('input'); ack.type = 'checkbox'; ack.id = 'acknowledge'; ack.checked = state.acknowledged;
        const lab = el('label'); lab.htmlFor = ack.id;
        lab.append(ack,copy(m('I reviewed the exact text, revision and diff anchor.')));
        const confirm = button(m('Confirm Mock comment'),() => act(async () => {
          await human('confirm',{draft_id:d.id,preview:p,human_confirmed:state.acknowledged});
          ctx.invalidateMock = false;
          await refreshHuman(); renderRail(true); $('mock-send')?.focus(); notify(m('Exact Mock comment confirmed.'));
        }, {reloadOnError:true}),'button primary','confirm-comment');
        confirm.disabled = !ack.checked;
        ack.addEventListener('change',() => {state.acknowledged = ack.checked; confirm.disabled = !ack.checked;});
        exact.append(lab,confirm);
      }
      section.append(exact);
    }
    if (d.status === 'CONFIRMED_MOCK_ONLY' && d.confirmation && owns(state.human)) {
      const send = button(m('Send to Mock'),() => act(async () => {
        const r = await human('send',{draft_id:d.id,confirmation_digest:d.confirmation.confirmation_digest,mode:'MOCK'});
        resetPreview(); await refreshHuman(); renderRail(true);
        $('reconcile-send')?.focus(); notify(r.status === 'UNCERTAIN' ? m('Outcome unknown. Reconcile without resending.') : m('{status} · Simulated only', {status: diagnostic(r.status)}));
      }, {reloadOnError:true}),'button primary','mock-send');
      send.disabled = changed() || !!ctx.invalidateMock || !!(state.preview && state.preview.confirmation_digest !== d.confirmation.confirmation_digest);
      const c = d.confirmation.payload;
      section.append(detail(m('Saved confirmation'), fields([[m('Commit'),c.commit_id],[m('Diff anchor'),c.path + ':' + c.line + ' ' + c.side]]),el('pre',c.body,'exact-body')),send);
    }
  }
  if (d.approval && d.confirmation) {
    const c = d.confirmation.payload;
    const confirmed = block(n,m('Human confirmation'));
    confirmed.id = 'live-confirmation';
    confirmed.append(fields([[m('Pull request'),repo(d.confirmation.target)],[m('Head commit'),c.commit_id],[m('Line'),c.path + ':' + c.line + ' ' + c.side],
      [m('Approval'),d.approval.reference],[m('Binding'),d.approval.binding_sha256]]),el('pre',c.body,'exact-body'),
      el('p',m('Confirmed in the attended command-line lane. This workbench cannot send to GitHub.'),'rail-sub'));
    otherComments(confirmed,d.confirmation.other_comments_at_anchor,d.approval.acknowledged_other_comment_ids);
  }
  const sends = state.human.sends.filter(s => s.draft_id === d.id);
  if (sends.length) {
    const live = sends.some(liveSend);
    const history = block(n,live ? m('GitHub writeback') : m('Mock dispatch'));
    if (live) history.id = 'writeback-status';
    for (const send of sends) {
      history.append(el('p',m('{status} · {mode}', {status: diagnostic(send.status), mode: liveSend(send) ? m('LIVE GITHUB') : m('MOCK ONLY')}),'mono'));
      if (liveSend(send) && send.remote_comment?.html_url?.startsWith('https://github.com/')) {
        history.append(link(m('Comment {id} on GitHub', {id: send.remote_comment.id}),send.remote_comment.html_url,'mono'));
      }
      if (liveSend(send) && ['UNCERTAIN','SENDING','NOT_SENT'].includes(send.status)) {
        const callout = el('div','','callout warn');
        callout.append(el('h3',m('Live outcome needs the command-line lane')),el('p',m('Reconcile read-only with mea.github_writeback before any further send.')));
        history.append(callout);
      } else if (['UNCERTAIN','SENDING'].includes(send.status)) {
        const callout = el('div','','callout warn');
        callout.append(el('h3',m('Mock delivery outcome unknown')),el('p',m('The local Mock may already have stored the comment. Use read-only reconciliation to check for one exact match.')));
        history.append(callout,action(m('Reconcile read-only'), async () => {
          const r = await human('reconcile',{intent_id:send.id}); await refreshHuman(); renderRail(true);
          notify(r.status === 'UNCERTAIN' ? m('No unique complete match. Outcome remains unknown.') : m('{status} · No resend.', {status: diagnostic(r.status)}));
        },'reconcile-send'));
      }
      if (send.status === 'NOT_SENT' && !liveSend(send)) {
        const ack = el('input'); ack.type = 'checkbox';
        const id = 'unsent-ack'; ack.id = id;
        const lab = el('label'); lab.htmlFor = id; lab.append(ack,copy(m('Discard this provably unsent intent.')));
        const release = action(m('Require fresh confirmation'),async () => {
          await human('release-not-sent',{intent_id:send.id,human_confirmed:ack.checked});
          resetPreview(); await refreshHuman(); renderRail(true);
        },'release-not-sent');
        release.disabled = true; ack.addEventListener('change',() => {release.disabled = !ack.checked;});
        history.append(lab,release);
      }
      history.append(lazy(m('Dispatch receipt'),() => [pre({id:send.id,payload:send.payload,comment:send.remote_comment,receipts:send.receipts,events:send.events})]));
    }
  }
  if (d.history.length) section.append(lazy(m('Prior draft revisions'),() => [pre(d.history)]));
}
function renderHuman(n,t) {
  n.append(el('h2',t?.candidates.length ? m('Finding') : m('Human review'),'rail-heading'));
  if (!t) {
    if (state.human && current().taskId) {
      n.append(el('p',current().evidenceAlert,'rail-sub'), ...humanVersionButtons());
      return;
    }
    n.append(el('p',m('Open a saved workspace or the self-owned offline fixture.'),'rail-sub'),
      action(m('Open owned Mock fixture'), async () => { await ensureOwned(); render(); },'open-owned'));
    return;
  }
  const c = candidate(), mock = owns(state.human);
  if (c) {
    const d = currentDraft();
    const blocked = candidateBlock(t, c.id, c.change.side, d);
    const inference = c.inference === 'CONDITIONAL_INFERENCE' ? m('Conditional') : c.inference || m('Unknown candidate type');
    const meta = el('p'); meta.append(tag(inference,'warn')); n.append(meta);
    if (t.candidates.length > 1) {
      const list = block(n,m('Candidates'));
      for (const row of t.candidates) {
        const item = card(row.title, m('{anchor} · {status}', {anchor: row.change.path + ':' + row.change.line, status: diagnostic(candidateStatus(t,row))}),
          event => pickCandidate(t,row,event.currentTarget), 'pick-' + row.id);
        list.append(currentAttr(item, row === c));
      }
    }
    const provenance = el('p', '', 'candidate-provenance');
    const [source, writeback] = reviewLabels(t);
    provenance.append(source, ' ', writeback);
    const finding = el('h3', c.title, 'candidate-title');
    finding.id = 'active-finding';
    finding.tabIndex = -1;
    n.append(finding, provenance,
      el('p', c.trigger, 'candidate-description'), el('p', c.impact, 'candidate-description'));
    const evidence = block(n, m('Frozen evidence'),
      locationCard(c.change, t, m('Changed line'), '', true, 'changed-evidence'));
    for (const e of c.citations.filter(e => e.path !== c.change.path || e.line !== c.change.line || e.side !== c.change.side)) {
      const title = mock && t.results.some(r => r.tool === 'references' && r.evidence.some(ref => ref.id === e.id))
        ? m('Caller evidence') : m('Supporting evidence');
      evidence.append(locationCard(e, t, title, '', false, 'candidate-evidence-' + e.id));
    }
    evidence.append(el('p', m('{citation} · {semantics}', {
      citation: c.citation_validity === 'VALID' ? m('Citation valid') : m('Citation {status}', {status: diagnostic(c.citation_validity) || m('unknown')}),
      semantics: c.correctness === 'UNADJUDICATED' ? m('Semantics unadjudicated') : m('Semantics {status}', {status: diagnostic(c.correctness) || m('unknown')}),
    }), 'rail-sub'),
      lazy(m('Exact citation data'),() => [pre(c.citations)]));
    renderDisposition(n, t, c);
    const comment = (!d || blocked) && block(n, m('Comment state'), tag(diagnostic(candidateStatus(t, c)), blocked ? 'warn' : ''));
    if (blocked) {
      comment.append(el('p',blocked.reason,'rail-sub'));
    }
    if (!d && !blocked) {
      comment.append(card(m('Select candidate'), m('Create a draft on {anchor}', {anchor: name(c.change.path) + ':' + c.change.line + ' · ' + anchors[c.change.side]}), event => act(async () => {
        const origin = event.currentTarget;
        await human('draft',{review_id:state.human.id,task_id:t.id,candidate_id:c.id});
        await refreshHuman(); renderRail(true); locate(t,c.change,true,origin);
      }), 'select-candidate'));
    } else if (d && (!blocked || d.intent_id)) renderEditor(n,t,c,d);
  } else n.append(el('p',t.reason ? m('{message} {reason}', {message: m('This frozen version has no supported candidate.'), reason: diagnostic(t.reason)}) : m('This frozen version has no supported candidate.'), 'rail-sub'));
  renderRereview(n,t);
  const live = block(n,'',el('p',liveReview(state.human) ? m('Live GitHub writes run only in the attended command-line lane for allowlisted repositories.') : m('Live GitHub publishing is disabled.'),'rail-sub'));
  if (currentDraft()) {
    const disabled = button(m('Publish to GitHub'),() => {}); disabled.disabled = true; live.append(disabled);
  }
  live.append(lazy(m('Workspace history & exports'), () => [
    ...humanVersionButtons(),
    ...(mock ? [detail(m('Mock target controls'),el('p',m('Select this frozen head for the local Mock transport only. Permissions and fault settings stay as configured.')),
      action(m('Set Mock target to {version}', {version: label(t)}),async () => {
        await human('mock-state',{version:label(t),can_comment:null,fault:null,complete_listing:null});
        current().invalidateMock = true; resetPreview(); renderRail(true);
        notify(m('Mock target changed. Preview and confirm again before sending.'));
      }))] : []),
    detail(m('Review events'),pre(state.human.events)),exportsFor(null,state.human)
  ]));
}
function humanVersionButtons() {
  return state.human.tasks.map(task =>
    button(m('Open {version}', {version: label(task)}), () => selectHumanVersion(task.id), 'button saved-row'));
}
function otherComments(parent, rows, acknowledged) {
  if (!rows?.length) return;
  const box = el('div','','callout warn'); box.id = 'other-comments';
  box.append(el('h3',m('Same line already has comments from other accounts')));
  for (const row of rows) {
    const p = el('p','','');
    p.append(tag(row.label,'warn'),'  ' + row.excerpt.slice(0,160) + (row.excerpt.length > 160 ? '…' : ''));
    box.append(p);
    if (row.html_url?.startsWith('https://github.com/')) box.append(link(m('Comment {id} by {author}', {id: row.id, author: row.login}),row.html_url,'mono'));
  }
  box.append(el('p',acknowledged ? m('Seen by the confirmer before sending: {ids}', {ids: acknowledged.join(', ')}) : m('The confirmer decides whether to send anyway.'),'rail-sub'));
  parent.append(box);
}
function renderRereview(n,t) {
  const check = (state.human?.rereviews || []).find(x => x.new_task_id === t.id);
  if (!check) return;
  const section = block(n,m('Incremental re-review'));
  section.id = 'rereview-status';
  section.append(el('p',m('{relation} · {heads}', {relation: diagnostic(check.relation), heads: short(check.old_head) + ' → ' + short(check.new_head)}),'mono'));
  if (check.lineage === 'UNKNOWN') section.append(el('p',m('An earlier comment anchor has unknown file lineage.'),'rail-sub'));
  for (const row of check.comments) {
    const where = row.old.path + ':' + row.old.line + ' ' + row.old.side;
    const now = row.new ? ' → ' + row.new.path + ':' + row.new.line : '';
    const gh = row.github && typeof row.github === 'object' ? m(' · {location}', {
      location: row.github.outdated ? m('GitHub outdated') : m('GitHub line {line}', {line: row.github.line}),
    }) : '';
    const p = el('p'); p.append(tag(diagnostic(row.status), row.status === 'CURRENT' ? 'accent' : 'warn'),
      copy(m('  comment {id} · {anchor}{newAnchor}{github}', {id: row.comment_id, anchor: where, newAnchor: now, github: gh})));
    section.append(p);
  }
  if (!check.comments.length) section.append(el('p',m('No sent comment to check.'),'rail-sub'));
  for (const row of check.candidates) {
    const d = state.human.drafts.find(x => x.task_id === t.id && x.candidate_id === row.candidate_id);
    const status = candidateBlock(t, row.candidate_id, row.change.side, d)?.status || row.status;
    const p = el('p','',''); p.append(tag(diagnostic(status),status === 'NEW' ? 'accent' : 'warn'),'  ' + name(row.change.path) + ':' + row.change.line);
    section.append(p);
  }
  section.append(el('p',m('Outdated comments stay on their original commit and are never re-posted at a new location.'),'rail-sub'));
}
function backToOverview() {
  return button(m('Back to overview'), () => setOverview(true, true));
}

function renderRail(preserve = false) {
  const n = ui.review, previous = n.scrollTop;
  n.replaceChildren();
  if (current().fromOverview) n.append(backToOverview());
  if (state.mode === 'human') renderHuman(n,humanTask());
  else if (state.mode === 'compare') compareRail(n);
  else renderCompleted(n,state.task);
  if (preserve) n.scrollTop = previous;
}

const pairNotes = {
  'v1:v2':m('Rename and line movement'), 'v2:v3':m('Changed trigger and new candidate'), 'v3:v4':m('Anchor missing'),
  'v4:v5':m('Noncontiguous head'), 'v2:v5':m('Manual lineage recheck'),
};
const compareKey = t => owns(state.compareReview) ? label(t) : t.view.revisions.head;
function compareTask(value) { return state.compareReview?.tasks.find(t => compareKey(t) === value); }
function compareRevision(value) {
  return compareTask(value)?.view.revisions.head || state.versions.find(v => v.label === value)?.head;
}
function currentComparison() {
  const [old, next] = state.pair || [];
  const a = compareTask(old), b = compareTask(next);
  return a && b ? state.compareReview.comparisons.find(c => c.old_task_id === a.id && c.new_task_id === b.id) : null;
}
function availablePairs() {
  if (owns(state.compareReview) && state.versions.length > 1) {
    const labels = state.versions.map(v => v.label);
    return [...labels.slice(0,-1).map((v,i) => [v,labels[i+1]]), ...Object.keys(pairNotes)
      .filter(key => !labels.some((v,i) => v + ':' + labels[i+1] === key))
      .map(key => key.split(':')).filter(pair => pair.every(v => labels.includes(v)))];
  }
  return (state.compareReview?.comparisons || []).map(c => [c.old_task_id,c.new_task_id]
    .map(id => compareKey(state.compareReview.tasks.find(t => t.id === id))));
}
function pickPair(pair) {
  state.pair = pair; state.compareView = 'anchor'; context.compare.result = 0;
  renderFiles(); renderCode(); renderRail(); ui.code.scrollTop = 0; ui.review.scrollTop = 0;
  if (narrow.matches) setPanel('code',true); else syncUrl();
}
function pairTitle(pair) { return pair.map(value => /^v\d+$/.test(value) ? value : short(value)).join('  →  '); }
function compareFiles(n) {
  n.append(el('h2',m('Frozen version pairs'),'tree-title'));
  for (const p of availablePairs()) {
    const b = button('',() => pickPair(p),'pair-row');
    b.append(el('strong',pairTitle(p)),el('span',pairNotes[p.join(':')] || m('Saved comparison')));
    n.append(currentAttr(b,p.join(':') === state.pair?.join(':')));
  }
  if (owns(state.compareReview)) {
    const section = el('section','','tree-footer');
    const heads = el('div','','heads');
    for (const v of state.versions) heads.append(el('div',v.label + '  ' + short(v.head)));
    section.append(el('h3',m('Registered heads'),'tree-title'),heads,
      el('p', m('Registered versions may have unrelated history.')));
    n.append(section);
  }
}
async function runComparison() {
  const review = state.compareReview;
  if (!review || !state.pair) return;
  const [old,next] = state.pair;
  // Increment links to the current persisted tail. Only registered owned
  // versions are eligible for this helper; comparisons never configure or send.
  if (!compareTask(old) || !compareTask(next)) {
    const versions = state.versions.map(v => v.label);
    const last = versions.indexOf(label(review.tasks.at(-1)));
    const end = Math.max(versions.indexOf(old), versions.indexOf(next));
    if (!owns(review) || last < 0 || end <= last) throw Error('FROZEN_VERSION_NOT_LINKED');
    for (const version of versions.slice(last+1,end+1)) {
      const task = await human('demo',{version});
      state.compareReview = await human('increment',{review_id:review.id,task_id:task.task_id});
    }
  }
  if (!currentComparison()) {
    const oldTask = compareTask(old), newTask = compareTask(next);
    if (!oldTask || !newTask) throw Error('FROZEN_VERSION_NOT_LINKED');
    state.compareReview = await human('compare-linked',{review_id:review.id,old_task_id:oldTask.id,new_task_id:newTask.id});
  }
  if (state.human?.id === review.id) {state.human = state.compareReview; resetPreview();}
  render(); notify(m('Frozen comparison saved. No model call or comment.'));
}
function currentResult() { return currentComparison()?.rows[context.compare.result || 0]; }
function matchingCandidate(task,id) { return task?.candidates.find(c => c.id === id); }
function caller(c) {
  return c?.citations.find(e => e.path !== c.change.path || e.side !== c.change.side || e.line !== c.change.line);
}
function compareSource(t, row, side) {
  if (!t || !row) return null;
  const hit = side === 'old' ? row.old_change : row.new_change;
  const fn = side === 'old' ? row.old_function : row.new_function;
  const c = matchingCandidate(t, side === 'old' ? row.old_candidate_id : row.new_candidate_id);
  if (state.compareView === 'caller') {
    const e = caller(c);
    return e ? {path:e.path,line:e.line,side:e.side,revision:e.revision} : null;
  }
  if (hit) return {...hit,fn};
  if (fn) return {path:fn.path,line:fn.end,side:'head',revision:t.view.revisions.head,fn};
  return null;
}
function sourceCard(t,anchor,side) {
  const card = el('article','','source-card');
  const header = el('header','','source-head');
  header.append(el('small',m(side === 'old' ? 'OLD / {label} · {head}' : 'NEW / {label} · {head}', {label: label(t), head: short(t?.view.revisions.head)})),
    el('strong',anchor ? anchor.path + ':' + anchor.line : m('No validated anchor')));
  card.append(header);
  const f = anchor && t?.view.files.find(f => (anchor.side === 'base' ? f.base_path : f.path) === anchor.path);
  const lines = f && t.view.revisions[anchor.side] === anchor.revision ? frozenLines(f,anchor.side) : [];
  if (!lines.length || !anchor || !lines[anchor.line-1]?.trim()) {
    card.append(el('p',m('No matching frozen lines are available for this side.'),'code-foot')); return card;
  }
  const first = Math.max(1,(anchor.fn?.start ?? anchor.line) - 2);
  const last = Math.min(lines.length, Math.max(anchor.line,anchor.fn?.end || 0));
  const listing = el('div');
  for (let i = first; i <= last; i++) listing.append(codeRow({[anchor.side]:i,text:lines[i-1]},anchor,true));
  appendCodeListing(card, listing, m(side === 'old' ? 'Earlier {side} source · {path}' : 'Compared {side} source · {path}', {side: sides[anchor.side], path: anchor.path}));
  return card;
}
function publishedOriginal(row, old) {
  if (!row?.old_candidate_id || !old) return null;
  const r = state.compareReview;
  const draft = r.drafts.find(d => d.candidate_id === row.old_candidate_id && d.task_id === old.id);
  return draft && r.sends.find(s => s.draft_id === draft.id && s.remote_comment);
}
function originalComment(row, old) {
  const n = el('section','','original-comment');
  n.append(el('h3',m('Original comment'),'section-label'));
  const s = publishedOriginal(row,old);
  if (s) {
    n.append(el('p',label(old) + ' · ' + short(s.payload.commit_id) + '  /  ' + s.payload.path + ':' + s.payload.line + '  /  ' + s.payload.side + '  /  ' + s.status,'mono outcome-code'),
      el('p',s.payload.body),el('p',m('Pinned to original head. A comparison creates no comment.'),'rail-sub'));
  } else n.append(el('p',m('No published Mock comment is recorded for this old anchor. A comparison creates no comment.'),'rail-sub'));
  return n;
}
function compareCode(n) {
  const wrap = el('section','','compare'); n.append(wrap);
  if (!state.compareReview) {
    wrap.append(el('h2',state.loading ? m('Loading workspace…') : m('Open a review to compare frozen versions')));
    if (!state.loading) wrap.append(action(m('Open owned Mock fixture'),async () => {await ensureOwned(); render();},'open-comparison'));
    return;
  }
  const options = availablePairs();
  if (!state.pair) state.pair = options[0] || null;
  const head = el('header','','compare-head');
  const dropdown = el('details','','compare-picker');
  const menu = el('div','','popover');
  for (const p of options) menu.append(button(pairTitle(p),() => pickPair(p),''));
  dropdown.append(el('summary',m('Choose versions ⌄'),'button'),menu);
  head.append(el('strong',state.pair ? pairTitle(state.pair) : m('No saved comparison')));
  if (options.length) head.append(dropdown);
  wrap.append(head);
  const comparison = currentComparison();
  if (!comparison) {
    if (state.pair) {
      const [a,b] = state.pair;
      wrap.append(el('p',m('Compare the registered frozen versions using deterministic source evidence. No model call or comment.'),'muted'),
        action(m('Run manual comparison'),runComparison,'run-comparison',true));
      if (!compareTask(a) || !compareTask(b)) wrap.append(el('p',m('Required earlier registered heads will be linked to the saved workspace in version order.'),'rail-sub'));
    } else {
      const tasks = state.tasks.filter(t => t.status === 'COMPLETED' && repo(t.pr) === repo(state.compareReview.pr) && !state.compareReview.tasks.some(x => x.id === t.id));
      const select = el('select'); setAttr(select, 'aria-label', m('New saved frozen version'));
      for (const t of tasks) {const o = el('option',short(t.identity.revisions.head) + ' · ' + t.pr.title); o.value=t.id; select.append(o);}
      if (tasks.length) wrap.append(select,action(m('Link and compare'),async () => {
        state.compareReview = await human('increment',{review_id:state.compareReview.id,task_id:select.value});
        if (state.human?.id === state.compareReview.id) {state.human = state.compareReview; resetPreview();}
        pickPair(availablePairs().at(-1));
      }));
      else wrap.append(el('p',m('Import and complete another frozen version of this PR before comparing.'),'muted'),link(m('Import & task controls'),'/classic','button'));
    }
    return;
  }
  const viewTabs = el('div','','actions compare-tabs');
  for (const [key,text] of [['anchor',m('Anchor code')],['caller',m('Caller code')]]) {
    const b = button(text,() => {
      state.compareView = key; renderCode(); syncUrl();
      ui.code.querySelector('[data-compare-view="' + key + '"]')?.focus();
    },'button' + (key === state.compareView ? ' primary' : ''));
    b.dataset.compareView = key; b.setAttribute('aria-pressed',String(key === state.compareView)); viewTabs.append(b);
  }
  wrap.append(viewTabs);
  const [a,b] = state.pair.map(compareTask), row = currentResult();
  const grid = el('div','','compare-grid');
  grid.append(sourceCard(a,compareSource(a,row,'old'),'old'),sourceCard(b,compareSource(b,row,'new'),'new'));
  wrap.append(grid,originalComment(row,a));
  if (row) wrap.append(button(m('Open result · {status}', {status: diagnostic(row.status)}),() => setPanel('review',true),'button mobile-result'));
}
const resultTitles = {
  STILL_PRESENT:m('Condition persists at a new anchor'), RESOLVED:m('Recorded local trigger eliminated'),
  OUTDATED:m('Original anchor is outdated'), NEW:m('New conditional candidate'), NEEDS_RECHECK:m('Ancestry or evidence needs recheck'),
};
function compareRail(n) {
  n.append(el('h2',m('Comparison result'),'rail-heading'));
  const c = currentComparison();
  n.append(el('p',state.pair ? m('Manual · {pair}', {pair: pairTitle(state.pair)}) : m('Manual · Frozen evidence'),'rail-sub'));
  if (state.pair) n.append(fields([
    [m('Earlier frozen head'), compareRevision(state.pair[0]) || m('Unknown')],
    [m('Compared frozen head'), compareRevision(state.pair[1]) || m('Unknown')],
  ]));
  if (!c) {n.append(el('p',m('Select a pair and run a manual comparison.'),'rail-sub')); return;}
  const row = currentResult();
  if (!row) n.append(el('p',m('No candidate correspondence rows were recorded. This does not establish resolution.'),'rail-sub'));
  if (c.rows.length > 1) {
    const controls = el('div','','actions');
    for (const [i, r] of c.rows.entries()) {
      const b = button(diagnostic(r.status),() => {
        context.compare.result = i; renderCode(); renderRail(true); syncUrl(); $('result-' + i)?.focus({preventScroll:true});
      }, 'button', 'result-' + i);
      b.setAttribute('aria-pressed',String(r === row)); controls.append(b);
    }
    n.append(controls);
  }
  if (row) {
    const meta = el('p'); meta.append(tag(diagnostic(row.status), ['STILL_PRESENT', 'RESOLVED', 'NEW'].includes(row.status) ? 'accent' : 'warn')); n.append(meta);
    n.append(el('h3',resultTitles[row.status] || row.status,'candidate-title'),el('p',row.reason,'rail-sub'));
    const displayAnchor = x => x ? x.path + ':' + x.line : m('No validated anchor');
    block(n,m('Evidence state'),fields([
      [m('Old location'), displayAnchor(row.old_change)],
      [m('New location'), displayAnchor(row.new_change)],
      [m('Location status'), diagnostic(row.location_status) || m('Unknown')],
      [m('Semantics'), diagnostic(row.issue_status) || m('Unknown')],
    ]));
    if (!row.new_change && row.new_function) {
      n.append(el('p', m('Collected function context · {anchor}', {anchor: row.new_function.path + ':' + row.new_function.end}), 'rail-sub'));
    }
    const boundary = el('div','','callout' + (row.status === 'NEEDS_RECHECK' ? ' warn' : ''));
    boundary.append(el('h3',m('Evidence boundary')),el('p',row.status === 'STILL_PRESENT' ?
      m('The original comment stays on its original version. Matching function and caller context support this narrow comparison.') : c.limitations));
    if (row.status === 'NEEDS_RECHECK') boundary.append(el('p',m('The old issue remains open for manual verification. Matching code alone cannot establish lineage.')));
    n.append(boundary);
    n.append(button(m('View compared code'),() => setPanel('code',true),'button mobile-result'));
  }
  block(n,'',el('p',m('Read-only comparison. Original comment history stays on its source head.'),'rail-sub'),
    lazy(m('Comparison evidence & exports'),() => [
      fields([[m('Ancestry'),diagnostic(c.relation)],[m('Location'),diagnostic(row?.location_status)],[m('Semantics'),diagnostic(row?.issue_status)],[m('Correctness'),diagnostic(row?.correctness)]]),
      lazy(m('Old and new observed citations'),() => [pre({old:row?.old_evidence,new:row?.evidence})]),
      lazy(m('Raw comparison & deterministic reads'),() => [pre(c)]),exportsFor(null,state.compareReview)
    ]));
}

async function ensureOwned() {
  if (!state.versions.length) state.versions = (await human('status')).versions;
  const first = state.versions[0];
  if (!first) throw Error('NO_REGISTERED_MOCK_VERSIONS');
  state.reviews = await human('reviews');
  const owned = state.reviews.find(row => owns(row) && row.versions[0]?.revisions.head === first.head);
  let review;
  if (owned) {
    review = await human(endpoint('review/', owned.id));
  } else {
    const task = await human('demo', {version: first.label});
    review = await human('create', {task_id: task.task_id});
    state.reviews = await human('reviews');
  }
  if (state.mode === 'human' || !state.human) {
    if (state.human?.id !== review.id) {
      context.human = {};
      resetPreview();
    }
    state.human = review;
  }
  if (state.mode === 'compare' || !state.compareReview) {
    if (state.compareReview?.id !== review.id) {
      context.compare = {};
      state.compareView = 'anchor';
      state.pair = null;
    }
    state.compareReview = review;
  }
  state.pair ??= availablePairs()[0] || null;
}
async function openSavedTask(id) {
  if (state.task?.id === id) return setCase('completed');
  state.task = await rawTask(id);
  context.completed = {}; state.mode = 'completed'; state.overview = false; state.panel = 'code'; render(); startPoll();
}
function inboxRow(row) {
  const isTask = row.group === 'tasks';
  const params = isTask ? {task: row.id} : {case: 'human', review: row.id, panel: 'review'};
  if (row.taskId) params.version = row.taskId;
  const open = link('', '/?' + new URLSearchParams(params), 'inbox-row');
  const identity = el('span', '', 'inbox-pr');
  if (row.title) identity.append(el('span', repo(row.pr), 'mono'));
  identity.append(el('strong', row.title || repo(row.pr)));
  const revision = el('span', row.head ? 'HEAD ' + short(row.head) : m('Unknown head'), 'mono inbox-head');
  if (row.head) revision.title = row.head;
  open.append(identity, tag(investigationStatus(row.status), statusTone(row.status)), revision, el('span', row.mode, 'inbox-mode'));
  const item = el('li');
  const overview = link(m('Overview'), '/?' + new URLSearchParams({...params, overview: '1'}), 'button inbox-overview');
  setAttr(overview, 'aria-label', m('Overview · {pr} · {head}', {pr: repo(row.pr), head: short(row.head)}));
  item.append(open, overview);
  return item;
}
function renderInbox() {
  const n = $('inbox-results'), search = $('inbox-search'), group = $('inbox-group'), status = $('inbox-status');
  const rows = inbox.rows.filter(row => group.value === 'all' || row.group === group.value);
  const selectedStatus = status.value;
  const statuses = [...new Set(rows.map(row => row.status || 'Unknown'))].sort();
  status.replaceChildren(option(m('All statuses'), 'all'),
    ...statuses.map(value => option(investigationStatuses.get(value) || m('Unknown'), value)));
  status.value = statuses.includes(selectedStatus) ? selectedStatus : 'all';
  const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const matches = rows.filter(row => (status.value === 'all' || (row.status || 'Unknown') === status.value)
    && terms.every(term => (repo(row.pr) + ' ' + (row.title || '')).toLowerCase().includes(term)));
  $('inbox-clear').disabled = !search.value && group.value === 'all' && status.value === 'all';
  setText($('inbox-count'), inbox.loading ? m('Loading saved records…') : m('{matches} of {total} loaded records', {matches: matches.length, total: inbox.rows.length}));
  n.setAttribute('aria-busy', String(inbox.loading));
  n.replaceChildren();
  if (inbox.loading) return;
  if (inbox.errors.length) {
    const error = el('section', '', 'inbox-state');
    error.append(el('h2', m(inbox.errors.length > 1 ? 'Could not load investigations and human reviews' : inbox.errors[0] === 'investigations' ? 'Could not load investigations' : 'Could not load human reviews')),
      button(m('Retry'), async () => {await loadInbox(); ($('inbox-retry') || $('inbox-search')).focus();}, 'button', 'inbox-retry'));
    n.append(error);
  }
  if (!matches.length && (inbox.rows.length || !inbox.errors.length)) {
    const empty = el('section', '', 'inbox-state');
    empty.append(el('h2', inbox.rows.length ? m('No matching records') : m('No saved pull requests')),
      el('p', inbox.rows.length ? m('Clear the filters or search with a different term.') : m('Import a frozen snapshot or open the Demo guide.'), 'muted'));
    n.append(empty);
  }
  for (const [key, name] of Object.entries(inboxGroups)) {
    const groupRows = matches.filter(row => row.group === key);
    if (!groupRows.length) continue;
    const list = el('ul', '', 'inbox-list');
    list.append(...groupRows.map(inboxRow));
    const section = el('section', '', 'inbox-group');
    section.append(el('h2', name), list);
    n.append(section);
  }
}
async function loadInbox() {
  inbox.loading = true;
  renderInbox();
  const readList = async response => {
    const records = await response;
    if (!Array.isArray(records)) throw Error('SAVED_RECORDS_INVALID');
    return records;
  };
  const [tasks, reviews] = await Promise.allSettled([readList(request('/api/tasks')), readList(human('reviews'))]);
  state.tasks = tasks.value ?? [];
  state.reviews = reviews.value ?? [];
  const tasksById = new Map(state.tasks.map(t => [t.id, t]));
  inbox.rows = [
    ...state.tasks.map(t => ({
      id: t.id, group: 'tasks', pr: t.pr, title: t.pr.title, status: t.status, head: t.identity?.revisions?.head,
      mode: candidateSources[t.mode] || t.mode || m('Unknown mode'),
    })),
    ...state.reviews.map(r => {
      const version = r.versions?.at(-1);
      const head = version?.revisions?.head;
      const task = tasksById.get(version?.task_id);
      const matchesHead = head && head === task?.identity?.revisions?.head;
      return {
        id: r.id, group: 'reviews', taskId: version?.task_id, pr: r.pr, head,
        title: matchesHead ? task.pr?.title : undefined,
        status: matchesHead ? task.status : undefined,
        mode: reviewModes[r.mode] || r.mode || m('Unknown mode'),
      };
    }),
  ];
  inbox.errors = [tasks.status === 'rejected' && 'investigations', reviews.status === 'rejected' && 'human reviews'].filter(Boolean);
  inbox.loading = false;
  renderInbox();
}
function startPoll() {
  clearTimeout(poll);
  if (!state.task || terminal.has(state.task.status)) return;
  const id = state.task.id;
  poll = setTimeout(async () => {
    try {
      const t = await rawTask(id);
      if (state.task?.id !== id) return;
      const changed = state.task.updated_at !== t.updated_at;
      state.task = t;
      if (state.mode === 'completed' && changed) {
        if (!state.overview) renderRail(true);
        else {
          const focus = document.activeElement;
          const id = $('overview').contains(focus) ? focus.id : '';
          renderOverview();
          if (id) $(id)?.focus({preventScroll: true});
        }
      }
    } catch (e) {notify(m('Could not refresh task status. {error}', {error: diagnostic(e.message)}),true);}
    startPoll();
  },2500);
}
function applyURL() {
  const c = current();
  c.fromOverview = url.searchParams.get('overview-return') === '1';
  if (url.searchParams.has('scope-file')) c.scope = {file: url.searchParams.get('scope-file')};
  else if (url.searchParams.has('scope-candidate')) c.scope = {candidate: url.searchParams.get('scope-candidate')};
  c.path = url.searchParams.get('file');
  if (Object.hasOwn(sides,url.searchParams.get('view'))) c.sourceSide = url.searchParams.get('view');
  const line = Number(url.searchParams.get('line')), side = url.searchParams.get('side');
  if (state.mode === 'human') {
    c.taskId = url.searchParams.get('version') || humanTask()?.id;
    c.candidateId = url.searchParams.get('candidate');
  }
  const task = activeTask();
  const file = task && (c.path ? task.view.files.find(f => f.path === c.path) : activeFile(task));
  const missingVersion = state.mode === 'human' && c.taskId && task?.id !== c.taskId;
  if (missingVersion) c.evidenceAlert = m('The linked review does not contain the requested frozen version.');
  else if (c.path && task && !file) c.evidenceAlert = m('File not collected in this frozen version · {path}', {path: c.path});
  if (Number.isSafeInteger(line) && line > 0 && Object.hasOwn(sides,side)) {
    const source = c.sourceSide || (!file?.patch ? side : null);
    const hasLine = file && (source ? source === side && frozenLines(file, side).length >= line
      : patches(file.patch).some(h => h.rows.some(r => r[side] === line)));
    if (hasLine && !missingVersion) {
      c.location = {line, side};
      c.sourceSide = source;
      c.returnToFiles = url.searchParams.get('back') === 'files';
      c.returnToReview = !c.returnToFiles && (state.mode === 'human' || !!c.sourceSide || url.searchParams.get('back') === 'review');
      c.returnFocus = url.searchParams.get('back-focus');
    } else {
      c.requestedLocation = {line, side};
      c.evidenceAlert ||= m('Line not collected · {anchor}', {anchor: [c.path, sides[side] + ' L' + line].filter(Boolean).join(' · ')});
    }
  }
  if (state.mode === 'compare') {
    const pair = url.searchParams.get('pair');
    state.pair = availablePairs().find(p => p.join(':') === pair) || availablePairs()[0] || null;
    const row = Number(url.searchParams.get('result'));
    if (Number.isSafeInteger(row) && row > 0 && currentComparison()?.rows[row]) c.result = row;
    if (url.searchParams.get('compare-view') === 'caller') state.compareView = 'caller';
  }
}
async function init() {
  render();
  if (state.mode === 'inbox') {
    state.loading = false;
    await loadInbox();
    return;
  }
  // All startup actions are read only. Opening the owned Mock case explicitly
  // can create its deterministic task; viewing saved workspaces never runs a model.
  const requests = await Promise.allSettled([request('/api/tasks'),human('reviews'),human('status')]);
  const [tasks,reviews,status] = requests;
  if (tasks.status === 'fulfilled') state.tasks = tasks.value;
  if (reviews.status === 'fulfilled') state.reviews = reviews.value;
  if (status.status === 'fulfilled') state.versions = status.value.versions;
  const completed = state.tasks.find(t => t.status === 'COMPLETED') || state.tasks[0];
  const id = url.searchParams.get('task') || completed?.id;
  const mock = state.reviews.find(owns);
  const humanId = url.searchParams.get('review') || mock?.id || state.reviews[0]?.id;
  const compareId = url.searchParams.get('comparison') || humanId;
  const loads = await Promise.allSettled([id ? rawTask(id) : null, humanId ? human(endpoint('review/',humanId)) : null,
    compareId && compareId !== humanId ? human(endpoint('review/',compareId)) : null]);
  state.task = loads[0].value;
  state.human = loads[1].value;
  state.compareReview = loads[2].value || state.human;
  state.pair = availablePairs()[0] || null;
  applyURL(); state.loading = false; render(); startPoll();
  restoreCodePosition();
  if ([...requests,...loads].some(r => r.status === 'rejected')) notify(m('Some saved records could not be opened. Return to the PR Inbox to reopen them.'),true);
}
for (const b of document.querySelectorAll('[data-case]')) b.addEventListener('click',() => act(async () => {
  const fromGuide = $('demo-dialog').open;
  const fromInbox = state.mode === 'inbox';
  if (fromGuide) $('demo-dialog').close();
  setCase(b.dataset.case);
  if (fromGuide && state.mode === 'completed') {
    const result = await human('demo', {version: 'v4'});
    await openSavedTask(result.task_id);
  }
  if ((state.mode === 'human' && (fromGuide || !state.human)) ||
      (state.mode === 'compare' && (fromGuide || !state.compareReview))) {
    await ensureOwned(); render();
  }
  if (fromInbox && narrow.matches && state.mode === 'human') setPanel('review', true);
  else if (fromGuide) ui[state.panel].focus({preventScroll:true});
}));
for (const b of document.querySelectorAll('[data-panel][aria-controls]')) b.addEventListener('click', () => {
  if (state.overview) {
    state.panel = b.dataset.panel;
    setOverview(false);
  } else setPanel(b.dataset.panel);
});
$('mobile-overview').addEventListener('click', () => setOverview(!state.overview, true));
$('inbox-demo').addEventListener('click', () => $('demo-dialog').showModal());
$('switch-demo').addEventListener('click', () => {
  $('switch').open = false;
  $('switch').querySelector('summary').focus();
  $('demo-dialog').showModal();
});
$('inbox-filters').addEventListener('submit', e => e.preventDefault());
$('inbox-search').addEventListener('input', renderInbox);
for (const id of ['inbox-group', 'inbox-status']) $(id).addEventListener('change', renderInbox);
$('inbox-clear').addEventListener('click', () => {
  $('inbox-filters').reset();
  renderInbox();
  $('inbox-search').focus();
});
document.addEventListener('keydown',e => {
  if (e.key === 'Escape' && $('switch').open) {
    $('switch').open = false; $('switch').querySelector('summary').focus();
  }
});
mountLanguage();
narrow.addEventListener('change',render);
init().catch(e => {state.loading=false; render(); notify(m('Unable to load the workspace. {error}', {error: diagnostic(e.message)}),true);});
