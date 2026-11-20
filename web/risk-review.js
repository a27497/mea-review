import {diagnostic, m, mountLanguage, setAttr, setText, setTitle} from './i18n.js';
import {patches, sourceLines} from './frozen-code.js';
import {candidateBlock, renderCommentEditor, resetPreview} from './comment-editor.js';
import {actionError, detail, evidenceCoverage, exportsFor, fields, human, request, toolTrace} from './review-components.js';

const $ = id => document.getElementById(id);
const mobile = matchMedia('(max-width: 760px)');
const authored = 'AUTHORED_OFFLINE_DEMO';
const final = new Set(['COMPLETED', 'STOPPED', 'CANCELLED']);
const demo = task => task?.source?.kind === authored ? task.source.presentation : null;
const short = sha => sha?.slice(0, 7) || '—';
const sides = {head: m('After change'), base: m('Before change')};
const severity = {HIGH: m('High risk'), MEDIUM: m('Medium risk'), LOW: m('Low risk')};
const statuses = {
  NEEDS_RECHECK: m('Needs another review'), STOPPED: m('Unable to complete review'),
  CANCELLED: m('Cancelled'), RUNNING: m('Investigating'), QUEUED: m('Waiting for investigation'),
  PENDING: m('Awaiting judgment'), REVIEWED: m('Reviewed'), COMPLETED: m('Investigation complete'),
};
const judgments = {ACCEPTED: m('Finding accepted'), IGNORED: m('Finding dismissed'), UNDECIDED: m('Judgment deferred')};

const state = {
  rows: [], tasks: new Map(), reviews: new Map(), linkedReviews: new Map(), row: null, task: null, review: null,
  judgmentSaves: new Map(),
  finding: null, queueOpen: false, filter: 'all', expanded: false, stage: null,
  listError: false, openError: false, loading: true, pending: false, selection: 0,
};
let poll, toastTimer, dialogOrigin;

function el(tag, value = '', cls = '') {
  const node = document.createElement(tag);
  if (value !== '') setText(node, value);
  if (cls) node.className = cls;
  return node;
}
function btn(text, onClick, cls = '', id) {
  const node = el('button', text, cls);
  node.type = 'button';
  if (id) node.id = id;
  node.addEventListener('click', onClick);
  return node;
}
function link(text, href, cls = '') {
  const node = el('a', text, cls);
  node.href = href;
  return node;
}
function glyph(text, cls) {
  const node = el('span', text, cls);
  node.setAttribute('aria-hidden', 'true');
  return node;
}
function iconLabel(value, cls) {
  const node = el('span', '', cls);
  node.append(el('i'), el('span', value));
  return node;
}
function riskLabel(risk) {
  return iconLabel(severity[risk], 'risk-label ' + risk.toLowerCase());
}
function sectionHead(id, title, count, accessory) {
  const head = el('header', '', 'section-head');
  const heading = el('h2');
  heading.append(el('strong', title));
  heading.id = id;
  heading.tabIndex = -1;
  if (count !== undefined) heading.append(el('span', count));
  head.append(heading);
  if (accessory) head.append(accessory);
  return head;
}
const getTask = id => request('/api/task-json/' + encodeURIComponent(id));
const getReview = id => request('/api/human/review/' + encodeURIComponent(id));

function toast(text) {
  clearTimeout(toastTimer);
  setText($('toast'), text);
  $('toast').hidden = false;
  toastTimer = setTimeout(() => {$('toast').hidden = true;}, 5000);
}
function showDialog(title, ...body) {
  setText($('dialog-title'), title);
  $('dialog-body').replaceChildren(...body);
  const dialog = $('risk-dialog');
  if (dialog.open) {
    $('dialog-title').focus({preventScroll: true});
  } else {
    dialogOrigin = document.activeElement;
    dialog.showModal();
  }
  dialog.scrollTop = 0;
}
function focusHeading(id) {
  const heading = $(id);
  if (!heading) return;
  heading.focus({preventScroll: true});
  heading.scrollIntoView({block: 'start'});
}
function focusPageTitle(id) {
  window.scrollTo(0, 0);
  $('review-content').scrollTop = 0;
  $(id)?.focus({preventScroll: true});
}
function workbench(params = {}) {
  const {row, task} = state;
  const finding = selectedFinding(task)?.id ?? state.finding;
  const returnParams = row ? {
    'risk-record': row.id,
    ...(task && task.id !== row.taskId && {'risk-revision': task.id}),
    ...(finding && {'risk-finding': finding}),
  } : {};
  return '/workbench?' + new URLSearchParams({...params, ...returnParams});
}
function urlFor(row, version, finding) {
  const params = new URLSearchParams({record: row.id});
  if (version && version !== row.taskId) params.set('revision', version);
  if (finding) params.set('finding', finding);
  return '/?' + params;
}
function followLocally(anchor, action) {
  anchor.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    action();
  });
  return anchor;
}
function samePR(left, right) {
  return left.repository === right.repository && left.number === right.number && left.url === right.url;
}
function disposition(task, candidate, review) {
  return review?.dispositions.find(row => row.task_id === task.id && row.candidate_id === candidate.id)?.disposition
    || 'UNDECIDED';
}
function needsRecheck(task, review) {
  return !!(demo(task)?.recheck_reason || review?.versions.find(v => v.task_id === task?.id)?.head_draft_block
    || review?.rereviews?.some(run => run.new_task_id === task?.id &&
      run.candidates.some(row => ['NEEDS_RECHECK', 'AT_OUTDATED_COMMENT_LOCATION'].includes(row.status))));
}
function mainStatus(task, review) {
  if (!task) return null;
  if (task.status === 'STOPPED') return task.status;
  if (needsRecheck(task, review)) return 'NEEDS_RECHECK';
  if (task.status !== 'COMPLETED' || !task.candidates?.length) return task.status;
  return task.candidates.every(c => disposition(task, c, review) !== 'UNDECIDED') ? 'REVIEWED' : 'PENDING';
}
function stopReason(task) {
  if (demo(task)) return task.events.findLast(event => event.kind === 'stopped')?.summary;
  return diagnostic(task.reason);
}
function stoppedBanner(task) {
  const banner = el('div', '', 'risk-banner neutral stop-reason');
  const copy = el('div');
  copy.append(el('p', stopReason(task)));
  const coverage = task.view.coverage;
  if (!coverage.complete_listing) {
    const missing = (coverage.capture?.gaps || []).filter(gap => gap.kind === 'MISSING' && gap.request === 'files');
    const paths = [...new Set(missing.map(gap => gap.path).filter(Boolean))];
    if (paths.length) copy.append(el('p', m('Missing files · {paths}', {paths: paths.join(', ')})));
    if (!paths.length || missing.some(gap => !gap.path)) {
      copy.append(el('p', m('The frozen snapshot does not record the missing file names.')));
    }
    copy.append(el('p', m('Capture the complete changed-file list and source for this frozen version, then import the new snapshot to review it.')));
  }
  banner.append(copy);
  return banner;
}
const fileCount = task => task?.view?.coverage.reported_changed_files;
const location = ref => ref.path + (ref.start ? ':' + ref.start + (ref.end > ref.start ? '–' + ref.end : '') : '');
const editorials = (task, candidate) => demo(task)?.findings[candidate?.id];
const findingAnchor = (task, candidate) => editorials(task, candidate)?.anchor
  || {...candidate.change, start: candidate.change.line, end: candidate.change.line};
function selectedFinding(task) {
  return state.finding == null ? task?.candidates[0] : task?.candidates.find(c => c.id === state.finding);
}

function showQueue(open, focus = false) {
  state.queueOpen = open;
  $('risk-workspace').classList.toggle('inbox-open', open);
  $('inbox-toggle').setAttribute('aria-expanded', String(open));
  setText($('inbox-toggle'), m(open ? 'Review page' : '‹ Inbox'));
  $('inbox-toggle').hidden = !state.row;
  $('skip').href = mobile.matches && open ? '#inbox-title' : '#review-title';
  if (focus) focusPageTitle(open ? 'inbox-title' : 'review-title');
}
function renderQueue() {
  const waiting = row => !['REVIEWED', 'CANCELLED'].includes(mainStatus(state.tasks.get(row.taskId), state.reviews.get(row.id)));
  const rows = state.filter === 'all' ? state.rows : state.rows.filter(waiting);
  const filters = $('queue-filters');
  filters.replaceChildren(...[['all', m('All'), state.rows.length], ['pending', m('To review'), state.rows.filter(waiting).length]]
    .map(([filter, label, count]) => {
      const control = btn('', () => {
        state.filter = filter;
        renderQueue();
        $('filter-' + filter).focus({preventScroll: true});
      }, 'filter', 'filter-' + filter);
      control.append(el('span', label), el('b', count));
      control.setAttribute('aria-pressed', String(state.filter === filter));
      return control;
    }));
  filters.hidden = !state.rows.length;
  $('queue-items').replaceChildren(...rows.map(row => {
    const task = state.tasks.get(row.taskId);
    const status = mainStatus(task, state.reviews.get(row.id));
    const item = followLocally(link('', urlFor(row), 'pr-row'), () => {
      history.pushState({}, '', urlFor(row));
      openRecord(row, {focus: true});
    });
    if (state.row?.id === row.id) item.setAttribute('aria-current', 'true');
    const top = el('span', '', 'pr-row-top');
    top.append(el('span', '#' + row.pr.number, 'pr-no'));
    if (demo(task)?.risk) top.append(riskLabel(demo(task).risk));
    item.append(top, el('strong', task?.pr.title || row.pr.title),
      ...(state.rows.some(r => r.pr.repository !== row.pr.repository) ? [el('span', row.pr.repository, 'pr-row-context')] : []));
    const bottom = el('span', '', 'pr-row-bottom');
    const count = fileCount(task);
    const timestamp = demo(task)?.captured_at || (task?.created_at && task.created_at * 1000);
    const date = timestamp && new Date(timestamp);
    const dateParts = date && !Number.isNaN(date.valueOf()) && new Intl.DateTimeFormat('en', {month:'numeric', day:'numeric', timeZone:'UTC'}).formatToParts(date);
    const day = dateParts && m('{month}/{day}', {
      month: dateParts.find(p => p.type === 'month').value, day: dateParts.find(p => p.type === 'day').value,
    });
    const files = count ? m('{count} files', {count}) : short(task?.identity.revisions.head || row.head);
    bottom.append(el('span', day ? m('{value} · {date}', {value: files, date: day}) : files),
      el('span', statuses[status] || '', 'queue-status'));
    item.append(bottom);
    const li = el('li');
    li.append(item);
    return li;
  }));
  const message = $('queue-message');
  message.replaceChildren();
  if (state.listError) message.append(el('p', m('Some pull requests could not be loaded.')), btn(m('Retry'), init, 'secondary'));
  else if (!rows.length && state.rows.length) message.append(el('p', m('No pull requests to review.')));
  else if (!state.rows.length && !state.loading) message.append(el('p', m('Import a frozen snapshot or open the local demo.')));
  if (state.rows.some(row => !demo(state.tasks.get(row.taskId)))) {
    message.append(link(m('Search saved records'), '/workbench', 'text-link'));
  }
}

function dialogVersions(task, review) {
  const row = state.row;
  const versions = review?.tasks || state.rows.filter(item => samePR(task.pr, item.pr))
    .map(item => state.tasks.get(item.taskId)).filter(Boolean);
  const list = el('ul', '', 'version-list');
  for (const version of versions) {
    if (review && !review.versions.some(v => v.task_id === version.id)) continue;
    const li = el('li');
    const selected = version.id === task.id;
    const control = btn(m('Frozen version {head}', {head: version.identity.revisions.head}), () => {
      $('risk-dialog').close();
      const nextRow = review ? row : state.rows.find(item => item.taskId === version.id);
      if (!nextRow) return;
      history.pushState({}, '', urlFor(nextRow, version.id));
      openRecord(nextRow, {version: version.id, focus: true});
    });
    if (selected) control.setAttribute('aria-current', 'true');
    li.append(control);
    list.append(li);
  }
  showDialog(m('Frozen versions'),
    el('p', m('Before change · {revision}', {revision: task.view.revisions.base}), 'dialog-anchor'),
    el('p', m('After change · {revision}', {revision: task.view.revisions.head}), 'dialog-anchor'), list);
}
function dialogFiles(task) {
  const list = el('ul', '', 'file-list');
  const candidate = selectedFinding(task);
  for (const file of task.view.files) {
    const li = el('li');
    li.append(el('code', file.path));
    const anchor = candidate && fileFor(task, candidate.change) === file ? candidate.change : null;
    const back = () => dialogFiles(task);
    if (file.patch) li.append(btn(m('Open diff'), () => dialogCode(task, file, anchor, {back}), 'text-button'));
    for (const side of ['base', 'head']) {
      if (file.sides[side]) li.append(btn(sides[side], () =>
        dialogCode(task, file, anchor?.side === side ? anchor : null, {source: side, back}), 'text-button'));
    }
    if (!file.patch && !Object.keys(file.sides).length) li.append(el('span', m('Source not collected'), 'empty-detail'));
    list.append(li);
  }
  showDialog(m('Collected files'),
    ...(!task.view.coverage.complete_listing ? [el('p', m('The frozen file list is incomplete.'))] : []), list);
}
function detailsButton(task) {
  return btn(m('Review details'), () => dialogDetails(task), 'text-button');
}
function dialogDetails(task) {
  const {view, status} = task;
  const citation = ref => {
    const location = {...ref, start: ref.line, end: ref.line};
    const label = referenceLocation(location);
    return sourceFor(task, location).issue
      ? el('p', label, 'dialog-anchor')
      : btn(label, () => showEvidence(task, location), 'text-button citation-button');
  };
  const controls = el('div', '', 'dialog-buttons');
  controls.append(btn(m('View files ↗'), () => dialogFiles(task), 'secondary'));
  showDialog(m('Review details'), fields([
    [m('Collected changed files'), view.files.filter(f => f.status !== 'context').length],
    [m('Candidates'), task.candidates.length],
  ]), ...(status === 'STOPPED' ? [el('p', stopReason(task))]
    : task.reason && !demo(task) ? [el('p', diagnostic(task.reason))] : []),
  evidenceCoverage(task), controls, toolTrace(task, citation, m('Tool investigation')),
  detail(m('Technical details'), fields([
    [m('Before change'), view.revisions.base], [m('After change'), view.revisions.head],
    [m('Model requests'), (task.requests || []).length],
  ]), exportsFor(task)));
}
function prereviewTools(task, review, candidate) {
  const controls = el('div', '', 'review-tools');
  controls.append(detailsButton(task));
  if (review || task.status === 'COMPLETED') {
    controls.append(btn(m('Human review'), () => launchHumanReview(task, candidate), 'text-button', 'human-review'));
  }
  if (review) {
    if (review.comparisons.length || review.mode === 'MOCK_OWNED_REVIEW') {
      controls.append(link(m('Re-review'), workbench({case: 'compare', comparison: review.id}), 'text-link'));
    }
  }
  if (demo(task)?.recheck_reason) controls.append(btn(m('Re-review details'), () => showDialog(m('Re-review details'),
    el('p', demo(task).recheck_reason), detailsButton(task), link(m('Import snapshot'), '/classic', 'text-link')), 'text-button'));
  return controls;
}
async function launchHumanReview(task, candidate) {
  if (state.pending) return;
  const {row, selection, review: savedReview} = state;
  const panel = el('div', '', 'human-panel');
  const notice = el('p');
  const session = {};
  const ctx = {};
  let review;
  let busy = false;
  notice.setAttribute('role', 'status');
  showDialog(m('Human review'), panel);
  panel.append(el('p', m('Loading workspace…')));
  const isOpen = () => panel.isConnected && $('risk-dialog').open && selection === state.selection;
  const notify = message => {
    if (isOpen()) {
      setText(notice, message);
      notice.scrollIntoView({block: 'nearest'});
    }
  };
  const remember = value => {
    review = value;
    state.reviews.set(value.id, value);
    state.reviews.set(row.id, value);
    if (selection === state.selection) {
      state.review = value;
      renderQueue();
      renderActions(state.task, selectedFinding(state.task), review);
    }
  };
  const load = async read => {
    const lastSave = state.judgmentSaves.get(row.id);
    const value = await read();
    if (lastSave === state.judgmentSaves.get(row.id)) remember(value);
    else review = state.reviews.get(row.id);
  };
  const reload = () => load(() => getReview(review.id));
  const act = async (fn, {reloadOnError = false} = {}) => {
    if (busy || !isOpen()) return;
    busy = true;
    panel.setAttribute('aria-busy', 'true');
    const active = document.activeElement;
    setText(notice, '');
    try {
      await fn();
    } catch (error) {
      if (reloadOnError) {
        resetPreview(session);
        try { await reload(); render(); } catch { /* Report the original operation error. */ }
      }
      notify(actionError(error));
    } finally {
      busy = false;
      panel.removeAttribute('aria-busy');
      const activeId = active?.id;
      if (isOpen() && activeId && !active.isConnected && !panel.contains(document.activeElement)) {
        panel.querySelector('#' + activeId)?.focus({preventScroll: true});
      }
    }
  };
  const render = () => {
    if (!isOpen()) return;
    const version = review.versions.find(version => version.task_id === task.id);
    const permission = {
      LOCAL_PREVIEW_ONLY: m('Local preview only'),
      MOCK_GITHUB_NOT_LIVE: m('Mock (loopback)'),
      LIVE_GITHUB: m('GitHub (CLI)'),
    }[version?.writeback_mode] || m('Unknown');
    panel.replaceChildren(fields([
      [m('Head SHA'), task.view.revisions.head], [m('Writeback'), permission],
    ]), notice);
    if (!candidate) {
      panel.append(el('p', m('This frozen version has no supported candidate.')));
      return;
    }
    const finding = el('section', '', 'rail-block');
    const anchor = candidate.change;
    finding.append(el('h3', candidate.title), el('p', referenceLocation({...anchor, start: anchor.line, end: anchor.line}), 'dialog-anchor'));
    panel.append(finding);
    const judgment = el('section', '', 'rail-block');
    judgment.append(el('h3', m('Human judgment')));
    appendJudgmentControls(judgment, task, candidate, review);
    panel.append(judgment);

    const draft = review.drafts.find(draft => draft.task_id === task.id && draft.candidate_id === candidate.id);
    const block = candidateBlock(review, task, candidate.id, anchor.side, draft);
    const comment = el('section', '', 'rail-block');
    if (block) {
      comment.append(el('h3', m('Comment state')), el('p', block.reason));
    } else if (task.reason !== 'HYPOTHESIS_SUPPORTED') {
      comment.append(el('h3', m('Comment state')), el('p', m('Review the missing or outdated evidence before drafting a comment.')));
    } else if (!draft) {
      comment.append(el('h3', m('Comment draft')), btn(m('Create a draft on {anchor}', {
        anchor: anchor.path + ':' + anchor.line,
      }), () => act(async () => {
        await human('draft', {review_id: review.id, task_id: task.id, candidate_id: candidate.id});
        await reload();
        render();
        if (isOpen()) panel.querySelector('textarea')?.focus();
      }), 'secondary'));
    }
    if (comment.childElementCount) panel.append(comment);
    if (draft && ((!block && task.reason === 'HYPOTHESIS_SUPPORTED') || draft.intent_id)) {
      renderCommentEditor(panel, draft, {review, ctx, session, act, reload, render, notify});
    }
  };
  try {
    await load(() => savedReview ? getReview(savedReview.id) : human('create', {task_id: task.id}));
    render();
  } catch {
    if (isOpen()) panel.replaceChildren(el('p', m('Human review could not be opened. Try again.')));
  }
}

function header(task, review) {
  const pr = task.pr;
  const authoredData = demo(task);
  const status = mainStatus(task, review);
  const title = el('h1', pr.title);
  title.id = 'review-title';
  title.tabIndex = -1;
  const heading = el('div', '', 'title-row');
  heading.append(title, iconLabel(statuses[status], 'status ' + status.toLowerCase()));
  const meta = el('div', '', 'meta');
  if (authoredData?.author) {
    const author = el('span', '', 'author');
    author.append(glyph(authoredData.author.charAt(0), 'avatar'), el('span', authoredData.author));
    meta.append(author);
  }
  const count = task.view.coverage.reported_changed_files;
  meta.append(btn(m('{count} changed files', {count}), () => dialogFiles(task), 'meta-button'),
    btn(m('Frozen version {head}', {head: short(task.view.revisions.head)}), () => dialogVersions(task, review), 'meta-button'),
    btn(m('PR description'), () => showDialog(m('PR description'), el('p', pr.body || m('No description.'))), 'meta-button'));
  const result = el('header');
  result.append(el('p', pr.repository + ' / #' + pr.number, 'pr-identity'), heading, meta);
  const risk = authoredData?.risk;
  const highest = task.candidates[0];
  if (status === 'STOPPED') {
    result.append(stoppedBanner(task));
  } else if (highest || status === 'NEEDS_RECHECK') {
    const tone = risk || (status === 'NEEDS_RECHECK' ? 'MEDIUM' : 'NEUTRAL');
    const banner = el('section', '', 'risk-banner ' + tone.toLowerCase());
    if (risk) banner.append(glyph('!', 'risk-icon'));
    const explanation = el('div');
    explanation.append(el('h2', authoredData?.risk_summary || highest?.title || statuses.NEEDS_RECHECK));
    const reason = authoredData?.recheck_reason || (status === 'NEEDS_RECHECK' ? m('Review the frozen location again before preparing a comment.') : highest?.impact);
    if (reason) explanation.append(el('p', reason));
    banner.append(explanation);
    result.append(banner);
  }
  return result;
}

const stages = [['finding-title', m('Review findings')], ['code-title', m('Code')], ['evidence-title', m('Evidence')]];
function stageNav() {
  const nav = el('nav', '', 'stage-nav');
  setAttr(nav, 'aria-label', m('Review sections'));
  for (const [id, label] of stages) {
    const control = followLocally(link(label, '#' + id), () => {
      state.stage = id;
      for (const other of nav.querySelectorAll('a')) other.removeAttribute('aria-current');
      control.setAttribute('aria-current', 'location');
      focusHeading(id);
    });
    if ((state.stage || 'finding-title') === id) control.setAttribute('aria-current', 'location');
    nav.append(control);
  }
  return nav;
}
function renderFindings(task, candidate) {
  const list = el('section', '', 'findings');
  list.append(sectionHead('finding-title', m('Review findings'), task.candidates.length));
  const cards = el('div', '', 'finding-list');
  for (const finding of task.candidates) {
    const control = btn('', () => {
      if (finding === candidate) return;
      state.finding = finding.id;
      state.expanded = false;
      history.replaceState({}, '', urlFor(state.row, task.id, finding.id));
      renderReview();
      $('finding-' + finding.id).focus({preventScroll: true});
    }, 'finding', 'finding-' + finding.id);
    control.setAttribute('aria-pressed', String(finding === candidate));
    const details = el('span', '', 'finding-name');
    const path = location(findingAnchor(task, finding));
    const locationText = el('code', path);
    locationText.title = path;
    details.append(el('strong', finding.title), locationText);
    const risk = editorials(task, finding)?.risk;
    control.append(risk ? riskLabel(risk) : el('span'), details, glyph('›', 'finding-arrow'));
    cards.append(control);
  }
  list.append(cards);
  return list;
}

function fileFor(task, ref) {
  return task.view.files.find(file => (ref.side === 'base' ? file.base_path : file.path) === ref.path);
}
function sourceFor(task, ref) {
  if (task.view.revisions[ref.side] !== ref.revision) return {issue: m('This reference points to a different version.')};
  const file = fileFor(task, ref);
  const source = file?.sides[ref.side];
  if (!source) {
    const gap = task.view.coverage.capture?.gaps.find(item => item.path === ref.path && item.side === ref.side);
    if (gap?.kind === 'TRUNCATED') return {issue: m('Source capture reached its limit.')};
    if (gap?.kind === 'TRANSPORT') return {issue: m('Source could not be retrieved during capture.')};
    return {issue: m('Source not collected')};
  }
  const lines = sourceLines(source.text);
  if ((ref.view_sha256 && ref.view_sha256 !== task.identity.view_sha256) ||
      (ref.content_sha256 && ref.content_sha256 !== source.sha256) ||
      (typeof ref.quote === 'string' && ref.quote !== lines[ref.start - 1])) {
    return {issue: m('That citation does not match the collected path, side and revision.')};
  }
  if (!ref.start || ref.start < 1 || ref.end > lines.length || ref.end < ref.start) {
    return {issue: m('The referenced lines were not collected.')};
  }
  return {file, lines};
}
function listing(rows, task, file, ref, {diff = false, selected} = {}) {
  const body = el('div', '', diff ? 'source-lines diff-lines' : 'source-lines');
  setAttr(body, 'aria-label', referenceLocation(ref, diff));
  body.tabIndex = 0;
  body.setAttribute('role', 'region');
  for (const row of rows) {
    if (row.header || (!row.base && !row.head)) {
      body.append(el('div', row.text, 'diff-header'));
      continue;
    }
    const actualSide = row.kind === 'del' ? 'base' : row.kind === 'add' ? 'head' : ref.side;
    const line = row[actualSide];
    const node = el('div', '', 'code-line ' + ({add: 'added', del: 'removed'}[row.kind] || ''));
    node.dataset.side = actualSide;
    if (line) node.dataset.line = line;
    for (const side of diff ? ['base', 'head'] : [actualSide]) {
      const number = el('span', row[side] ?? '', 'line-no');
      if (row[side]) {
        const path = side === 'base' ? file.base_path : file.path;
        setAttr(number, 'title', referenceLocation({
          path, start: row[side], end: row[side], side, revision: task.view.revisions[side],
        }, diff));
        setAttr(number, 'aria-label', m('{side}, line {line}', {side: sides[side], line: row[side]}));
        if (selected?.side === side && row[side] === selected.line) {
          node.dataset.located = '';
          number.classList.add('located-line');
        }
      }
      node.append(number);
    }
    node.append(glyph(row.kind === 'del' ? '−' : row.kind === 'add' ? '+' : '', 'line-sign'), el('code', row.text));
    body.append(node);
  }
  return body;
}
function snippetRows(file, lines, ref, expanded) {
  const first = expanded ? Math.max(1, ref.start - 8) : ref.start;
  const last = expanded ? Math.min(lines.length, ref.end + 8) : ref.end;
  if (!expanded && file.patch) {
    const rows = patches(file.patch).flatMap(hunk => hunk.rows);
    const indices = rows.flatMap((row, i) => row[ref.side] >= first && row[ref.side] <= last ? [i] : []);
    if (indices.length === last - first + 1) {
      let start = indices[0];
      // Show adjacent removed lines that precede the selected addition.
      if (ref.side === 'head' && rows[start].kind === 'add') {
        while (start && rows[start - 1].kind === 'del') start--;
      }
      return rows.slice(start, indices.at(-1) + 1).filter(row => row.base || row.head);
    }
  }
  return lines.slice(first - 1, last).map((text, index) => ({text, [ref.side]: first + index}));
}
function referenceLocation(ref, abbreviateRevision = false) {
  return m('{location} · {side} · {revision}', {location: location(ref), side: sides[ref.side],
    revision: abbreviateRevision ? short(ref.revision) : ref.revision});
}
function dialogCode(task, file, ref, {source, back} = {}) {
  const title = m(source ? 'Frozen source' : 'Unified diff');
  const sideRef = side => ({
    path: side === 'base' ? file.base_path : file.path, side, revision: task.view.revisions[side],
  });
  const binding = !ref || (fileFor(task, ref) === file && ref.revision === task.view.revisions[ref.side] &&
    (!source || ref.side === source));
  const selected = binding && ref ? {...ref, line: ref.line || ref.start} : null;
  const rows = source
    ? sourceLines(file.sides[source]?.text).map((text, index) => ({text, [source]: index + 1}))
    : patches(file.patch).flatMap(hunk => [{text: hunk.text, header: true}, ...hunk.rows]);
  const anchor = selected ? {...sideRef(selected.side), start: selected.line, end: selected.line}
    : sideRef(source || 'head');
  const body = el('div', '', 'full-code');
  if (back) body.append(btn(m('Back to files'), back, 'text-button'));
  const refs = (source ? [source] : ['base', 'head']).map(sideRef);
  if (selected) refs.push(anchor);
  const caption = (ref, abbreviated) => el('p', referenceLocation(ref, abbreviated), 'dialog-anchor');
  body.append(...refs.map(ref => caption(ref, !source)));
  const hasLine = !selected || rows.some(row => row[selected.side] === selected.line);
  if (!binding || !hasLine) {
    body.append(el('p', m(binding ? 'The referenced lines were not collected.' :
      'That citation does not match the collected path, side and revision.')));
  }
  if (rows.length) {
    const code = listing(rows, task, file, anchor, {diff: !source, selected});
    body.append(code);
  } else {
    body.append(el('p', m(source ? 'No text is available for this side.' : 'No unified diff was collected for this file.')));
  }
  if (!source) body.append(detail(m('Technical details'), ...refs.map(ref => caption(ref, false))));
  showDialog(title, body);
  body.querySelector('[data-located]')?.scrollIntoView({block: 'nearest', inline: 'start'});
}
function fullCodeButton(task, file, ref, text, source) {
  return btn(text, () => dialogCode(task, file, ref, {source}), 'text-button');
}
function renderCode(task, candidate) {
  const anchor = findingAnchor(task, candidate);
  const {file, lines, issue} = sourceFor(task, anchor);
  const section = el('section', '', 'code-section');
  const shortcuts = el('div', '', 'code-shortcuts');
  shortcuts.append(btn(m('View files ↗'), () => dialogFiles(task), 'text-button'));
  section.append(sectionHead('code-title', m('Code'), undefined, shortcuts));
  const panel = el('div', '', 'code-panel');
  const top = el('div', '', 'file-head');
  const meta = el('span', 'L' + anchor.start + (anchor.end > anchor.start ? '–' + anchor.end : ''), 'anchor-label');
  setAttr(meta, 'title', referenceLocation(anchor));
  top.append(glyph('', 'file-icon'), el('code', anchor.path), meta);
  panel.append(top);
  const changeFile = fileFor(task, candidate.change);
  const diff = changeFile?.patch;
  if (changeFile && (diff || file === changeFile)) {
    const full = fullCodeButton(task, changeFile, candidate.change,
      meta.textContent + ' ↗', diff ? undefined : anchor.side);
    setAttr(full, 'aria-label', m(diff ? 'Full diff ↗' : 'Full source ↗'));
    setAttr(full, 'title', referenceLocation(anchor));
    full.classList.add('anchor-label');
    meta.replaceWith(full);
  }
  if (issue) {
    panel.append(el('p', issue, 'conclusion'), el('p', referenceLocation(anchor), 'conclusion'));
    section.append(panel);
    return section;
  }

  const controls = el('div', '', 'code-controls');
  controls.append(fullCodeButton(task, file, anchor, m('Full source ↗'), anchor.side),
    el('span', referenceLocation(anchor), 'evidence-side'));
  const updateSnippet = () => {
    panel.querySelector('.source-lines')?.remove();
    const listingBody = listing(snippetRows(file, lines, anchor, state.expanded), task, file, anchor);
    listingBody.id = 'finding-code';
    panel.append(listingBody);
    controls.hidden = !state.expanded;
    setText(expand, m(state.expanded ? 'Collapse context' : 'Expand context'));
    expand.setAttribute('aria-expanded', String(state.expanded));
  };
  const expand = btn('', () => {
    state.expanded = !state.expanded;
    updateSnippet();
  }, 'text-button');
  expand.setAttribute('aria-controls', 'finding-code');
  if (anchor.start > 1 || anchor.end < lines.length) shortcuts.prepend(expand);
  updateSnippet();
  section.append(panel, controls);
  return section;
}
function showEvidence(task, ref) {
  const {file, lines, issue} = sourceFor(task, ref);
  const body = [el('p', referenceLocation(ref), 'dialog-anchor')];
  if (ref.summary) body.push(el('p', ref.summary));
  if (issue) {
    body.push(el('p', issue));
    const gap = task.view.coverage.omitted_sources.find(row => row.path === ref.path && row.side === ref.side);
    if (gap?.reason && gap.reason !== 'SOURCE_NOT_CAPTURED') body.push(el('p', m('Capture record · {reason}', {reason: gap.reason}), 'dialog-anchor'));
    body.push(detailsButton(task));
  } else {
    body.push(listing(snippetRows(file, lines, ref, true).filter(row => row[ref.side] >= ref.start && row[ref.side] <= ref.end),
      task, file, ref), fullCodeButton(task, file, ref, m('Full source ↗'), ref.side));
  }
  showDialog(ref.title || m('Supporting evidence'), ...body);
}
function renderEvidence(task, candidate) {
  const data = editorials(task, candidate);
  const refs = data?.evidence || candidate.citations.filter(ref => ref.path !== candidate.change.path ||
    ref.side !== candidate.change.side || ref.line !== candidate.change.line)
    .map(ref => ({...ref, start: ref.line, end: ref.line}));
  const section = el('section', '', 'evidence');
  const revision = task.view.revisions[refs[0]?.side || candidate.change.side];
  const label = refs.every(ref => ref.revision === revision) ?
    el('span', m('{side} · {revision}', {side: sides[refs[0]?.side || candidate.change.side], revision: short(revision)}), 'evidence-side') : null;
  section.append(sectionHead('evidence-title', m('Evidence'), refs.length, label));
  const shell = el('div', '', 'evidence-shell');
  if (data?.conclusion) shell.append(el('p', data.conclusion, 'conclusion'));
  const items = el('ul', '', 'evidence-list');
  for (const ref of refs) {
    const item = btn('', () => showEvidence(task, ref), 'evidence-item');
    const {issue} = sourceFor(task, ref);
    if (issue) item.dataset.unavailable = 'true';
    const text = el('span', '', 'evidence-text');
    const path = el('code', location(ref));
    path.title = location(ref);
    text.append(el('strong', ref.title || m('Supporting evidence')), path);
    if (issue) text.append(el('small', issue, 'evidence-side'));
    item.append(glyph(issue ? '!' : '↳', 'evidence-glyph'), text, glyph('↗', 'evidence-arrow'));
    const li = el('li');
    li.append(item);
    items.append(li);
  }
  if (items.children.length) shell.append(items);
  else shell.append(el('p', m('No additional evidence was recorded.'), 'conclusion'));
  if (task.results.some(result => result.truncated || result.read?.status === 'TRUNCATED')) {
    shell.append(el('p', m('Some evidence was truncated. Open review details to see the recorded limits.'), 'conclusion'));
  }
  section.append(shell);
  return section;
}
function notes(task, candidate) {
  const root = el('section', '', 'finding-notes');
  const columns = [[m('Trigger'), candidate.trigger], [m('Suggested action'), editorials(task, candidate)?.suggested_action]];
  if (candidate !== task.candidates[0] || demo(task)?.recheck_reason) columns.push([m('Impact'), candidate.impact]);
  for (const [label, value] of columns) {
    if (!value) continue;
    const item = el('div');
    item.append(el('h3', label), el('p', value));
    root.append(item);
  }
  return root;
}

async function saveJudgment(value, controls = []) {
  if (state.pending) return;
  const {task, row, review} = state;
  const candidate = selectedFinding(task);
  if (!candidate || !canJudge(task, candidate, review)) return;
  const humanOpen = $('risk-dialog').open && dialogOrigin?.id === 'human-review';
  state.pending = true;
  controls.forEach(control => {control.disabled = true;});
  try {
    const active = review || await request('/api/human/create', {task_id: task.id});
    const record = await request('/api/human/disposition', {
      review_id: active.id, task_id: task.id, candidate_id: candidate.id, disposition: value,
    });
    active.dispositions = [...active.dispositions.filter(item => item.id !== record.id), record];
    state.judgmentSaves.set(row.id, record);
    state.reviews.set(active.id, active);
    state.reviews.set(row.id, active);
    if (state.row === row && state.task?.id === task.id) {
      state.review = active;
      if ($('risk-dialog').open) $('risk-dialog').close();
      renderReview();
      (humanOpen ? $('human-review') : $('record-judgment') || $('review-title'))?.focus({preventScroll: true});
    }
    renderQueue();
    toast(m('Local judgment saved · {judgment}', {judgment: judgments[record.disposition]}));
  } catch {
    toast(m('Judgment could not be saved. Try again.'));
  } finally {
    state.pending = false;
    controls.forEach(control => {control.disabled = false;});
  }
}
function judgmentDialog(task, candidate, review) {
  const actions = el('div', '', 'dialog-buttons');
  const controls = [['IGNORED', m('Dismiss finding')], ['ACCEPTED', m('Accept finding')]].map(([value, label]) =>
    btn(label, () => saveJudgment(value, controls), value === 'ACCEPTED' ? 'primary' : 'secondary'));
  actions.append(...controls);
  const previous = disposition(task, candidate, review);
  showDialog(m('Record judgment'), el('p', candidate.title),
    el('p', m('Your judgment is saved in this workspace. Sending a comment requires separate confirmation.')),
    ...(previous !== 'UNDECIDED' ? [el('p', judgments[previous], 'empty-detail')] : []), actions);
}
function needsFindingRecheck(task, candidate, review) {
  return task.reason === 'EVIDENCE_INSUFFICIENT' || demo(task)?.recheck_reason ||
    review?.rereviews?.some(run => run.new_task_id === task.id && run.candidates.some(row =>
      row.candidate_id === candidate.id && ['NEEDS_RECHECK', 'AT_OUTDATED_COMMENT_LOCATION'].includes(row.status)));
}
function canJudge(task, candidate, review) {
  return task.status === 'COMPLETED' && mainStatus(task, review) !== 'REVIEWED' &&
    !needsFindingRecheck(task, candidate, review);
}
function appendJudgmentControls(actions, task, candidate, review) {
  const current = disposition(task, candidate, review);
  if (!canJudge(task, candidate, review)) {
    if (current !== 'UNDECIDED') actions.append(el('p', judgments[current], 'action-caption'));
    else if (needsFindingRecheck(task, candidate, review)) actions.append(el('p',
      m('Review the missing or outdated evidence before recording a judgment.'), 'action-caption'));
    return;
  }
  actions.append(el('p', current === 'UNDECIDED' ? m('Judgments are saved locally') : judgments[current], 'action-caption'));
  const buttons = el('div', '', 'action-buttons');
  const defer = btn(m('Defer'), () => saveJudgment('UNDECIDED', [defer, record]), 'secondary');
  const record = btn(m('Record judgment'), () => judgmentDialog(task, candidate, review), 'primary',
    actions.id === 'review-actions' ? 'record-judgment' : 'human-judgment');
  buttons.append(defer, record);
  actions.append(buttons);
}
function renderActions(task, candidate, review) {
  const actions = $('review-actions');
  actions.replaceChildren();
  if (candidate && mainStatus(task, review) !== 'REVIEWED') appendJudgmentControls(actions, task, candidate, review);
  actions.hidden = !actions.childElementCount;
}

function renderReview() {
  const {task, review} = state;
  const root = $('review-content');
  $('risk-review').setAttribute('aria-busy', String(state.loading));
  $('demo-badge').hidden = !demo(task);
  if (!task) {
    const empty = el('div', '', 'empty-review');
    const title = el('h1', m(state.loading ? 'Loading pull requests…' :
      state.openError ? 'This pull request could not be opened.' : 'No saved PRs yet'));
    title.tabIndex = -1;
    title.id = 'review-title';
    empty.append(title);
    if (state.openError) empty.append(btn(m('Retry'), init, 'secondary'));
    else if (!state.loading) empty.append(el('p', m('Import a frozen snapshot to review its changes and evidence.'), 'empty-detail'),
      link(m('Import snapshot'), '/classic', 'text-link'), btn(m('Open local demo'), loadDemo, 'secondary'));
    root.replaceChildren(empty);
    $('review-actions').hidden = true;
    return;
  }
  setTitle(m('{title} · MEA Review', {title: task.pr.title}));
  const candidate = selectedFinding(task);
  root.replaceChildren(header(task, review));
  if (candidate) {
    const detail = el('div', '', 'detail-workspace');
    detail.append(renderCode(task, candidate), renderEvidence(task, candidate));
    root.append(stageNav(), renderFindings(task, candidate), detail, notes(task, candidate));
  } else if (state.finding != null) {
    const notice = el('p', m('The linked finding is unavailable in this frozen version.'), 'empty-detail');
    notice.setAttribute('role', 'status');
    root.append(renderFindings(task), notice);
  } else {
    root.append(sectionHead('finding-title', m('Review findings'), 0), el('p', final.has(task.status) ?
      m('There are no findings ready for judgment. See the review details for what was checked.') :
      m('Findings will appear when the investigation finishes.'), 'empty-detail'));
  }
  const coverage = task.view.coverage;
  if (task.status !== 'STOPPED' && !demo(task)?.recheck_reason &&
    (!coverage.complete_listing || coverage.capture?.status === 'INCOMPLETE')) {
    root.append(el('p', m('Some changes or evidence may be missing. See review details for the collected material.'), 'scope-notice'));
  }
  root.append(prereviewTools(task, review, candidate));
  renderActions(task, candidate, review);
}

async function openRecord(row, {version, finding, focus} = {}) {
  clearTimeout(poll);
  if ($('risk-dialog').open) $('risk-dialog').close();
  const selection = ++state.selection;
  Object.assign(state, {row, task: null, review: null, finding, expanded: false, stage: null, loading: true, openError: false});
  showQueue(false);
  renderQueue();
  renderReview();
  if (!row) {
    state.loading = false;
    renderReview();
    return;
  }
  try {
    const review = state.reviews.get(row.id) || (row.review ? await getReview(row.id) : null);
    if (review) state.reviews.set(review.id, review);
    const id = version || row.taskId;
    const task = review ? review.tasks.find(item => item.id === id) : await getTask(id);
    if (!task || !samePR(task.pr, row.pr)) throw Error('VERSION_UNAVAILABLE');
    if (selection !== state.selection) return;
    if (id === row.taskId) state.tasks.set(task.id, task);
    Object.assign(state, {review, task});
    if (!final.has(task.status)) poll = setTimeout(() => refreshTask(id, selection), 2500);
  } catch {
    if (selection !== state.selection) return;
    state.openError = true;
  }
  state.loading = false;
  renderQueue();
  renderReview();
  if (focus) focusPageTitle('review-title');
}
async function refreshTask(id, selection) {
  try {
    const task = await getTask(id);
    if (selection !== state.selection) return;
    if (task.updated_at !== state.task.updated_at) {
      state.task = task;
      state.tasks.set(id, task);
      renderReview();
      renderQueue();
    }
  } catch {
    if (selection !== state.selection) return;
    toast(m('Review status could not be refreshed. Try reloading.'));
  }
  if (!final.has(state.task.status)) poll = setTimeout(() => refreshTask(id, selection), 2500);
}
function currentRecord() {
  const params = new URLSearchParams(window.location.search);
  const id = params.get('record');
  const row = id ? state.rows.find(item => item.id === id || item.id === state.linkedReviews.get(id)) : state.rows[0];
  if (id && !row) {
    clearTimeout(poll);
    state.selection++;
    Object.assign(state, {row: null, task: null, review: null, loading: false, openError: true});
    showQueue(false);
    renderQueue();
    renderReview();
    return;
  }
  const version = params.get('revision') || (id && id !== row?.id ? id : null);
  openRecord(row, {version, finding: params.get('finding')}).then(() => {
    const section = window.location.hash.slice(1);
    if (stages.some(([id]) => id === section)) focusHeading(section);
  });
}
async function loadDemo(event) {
  const control = event.currentTarget;
  control.disabled = true;
  try {
    const {review_id: reviewId} = await request('/api/human/risk-demo', {});
    history.replaceState({}, '', '/?' + new URLSearchParams({record: reviewId}));
    await init();
  } catch {
    toast(m('The local demo could not be loaded. Try again.'));
  } finally {
    control.disabled = false;
  }
}
async function init() {
  state.loading = true;
  state.listError = false;
  renderReview();
  const [tasks, reviews] = await Promise.allSettled([request('/api/tasks'), request('/api/human/reviews')]);
  state.listError = tasks.status === 'rejected' || reviews.status === 'rejected';
  const records = tasks.value || [];
  const human = reviews.value || [];
  state.tasks = new Map(records.map(task => [task.id, task]));
  state.reviews.clear();
  state.linkedReviews = new Map(human.flatMap(review => review.versions.map(version => [version.task_id, review.id])));
  state.rows = [
    ...human.map(review => {
      const version = review.versions.at(-1);
      return {id: review.id, pr: review.pr, taskId: version.task_id, head: version.revisions.head, review: true};
    }),
    ...records.filter(task => !state.linkedReviews.has(task.id)).map(task => ({
      id: task.id, pr: task.pr, taskId: task.id, head: task.identity.revisions.head, review: false,
    })),
  ];
  const seeded = state.rows.filter(row => demo(state.tasks.get(row.taskId)));
  await Promise.allSettled(seeded.map(async row => {
    const review = row.review ? await getReview(row.id) : null;
    if (review) state.reviews.set(row.id, review);
    const task = review?.tasks.find(item => item.id === row.taskId) || await getTask(row.taskId);
    state.tasks.set(task.id, task);
  }));
  // The seed declares its default and preserves the authored queue order.
  state.rows.sort((a, b) => {
    const x = demo(state.tasks.get(a.taskId)), y = demo(state.tasks.get(b.taskId));
    if (x && y) return Number(y.default) - Number(x.default) || b.pr.number - a.pr.number;
    return Number(!!x) - Number(!!y);
  });
  currentRecord();
}

$('inbox-toggle').addEventListener('click', () => showQueue(!state.queueOpen, true));
$('skip').addEventListener('click', event => {
  event.preventDefault();
  focusHeading($('skip').hash.slice(1));
});
mobile.addEventListener('change', () => showQueue(false));
window.addEventListener('popstate', currentRecord);
mountLanguage();
init();
