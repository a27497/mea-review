import {diagnostic, m, setText} from './i18n.js';

function el(tag, value = '', cls = '') {
  const n = document.createElement(tag);
  if (value !== '') setText(n, value);
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
const pre = value => el('pre', JSON.stringify(value, null, 2));
const tag = (value, tone = '') => el('span', value, 'tag ' + tone);
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
const errorText = {
  TASK_NOT_FOUND: m('This saved task could not be found.'),
  PR_HEAD_DRIFT: m('The Mock PR is on a different frozen head. No comment was sent.'),
  DUPLICATE_REMOTE_ANCHOR: m('A Mock comment already exists on this exact anchor.'),
  REMOTE_PERMISSION_OR_STATE: m('The Mock target does not currently allow comments.'),
  CONFIRMATION_SNAPSHOT_CHANGED: m('The approved payload or Mock target changed. Generate a fresh preview.'),
  LIVE_PUBLISH_DISABLED_NO_TARGET: m('Live publishing is disabled.'),
  COMMENT_BODY_REQUIRED: m('Enter a comment before saving.'),
};
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
function actionError(error) {
  return errorText[error.message] || m('Action unavailable · {error}', {error: diagnostic(error.message)});
}

function evidenceCoverage(t) {
  const sides = {base: 'BASE', head: 'HEAD'};
  const evidence = el('div', '', 'overview-coverage');
  evidence.append(el('h3', m('Evidence coverage')));
  const files = t.view.files.filter(f => f.status !== 'context').length;
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
  return evidence;
}

function toolTrace(t, renderCitation, title = m('Agent investigation')) {
  const results = t.results || [];
  const name = path => path.split('/').at(-1);
  return lazy(title, () => {
    const timeline = el('ol','','timeline');
    for (const e of t.events) timeline.append(el('li',e.summary));
    const output = results.map((r, i) => lazy(m('Tool {number} · {tool}', {number: i + 1, tool: r.tool + (r.read ? ' · ' + name(r.read.path) : '')}), () => [
      r.read ? fields([[m('Path'),r.read.path],[m('Returned'),r.read.returned_start + '–' + r.read.returned_end],[m('Status'),diagnostic(r.read.status)]]) : el('p',r.coverage),
      ...(r.truncated ? [tag(diagnostic('TRUNCATED'), 'warn')] : []),
      ...r.evidence.map(renderCitation),
      lazy(m('Raw feedback, limits and citations'), () => [pre(r)])]));
    return [el('p',m('Saved timeline and frozen tool feedback.')),detail(m('Event timeline'),timeline),...output];
  });
}

export {actionError, block, button, detail, el, endpoint, evidenceCoverage, exportsFor, fields, human, lazy, link, pre, request, tag, toolTrace};
