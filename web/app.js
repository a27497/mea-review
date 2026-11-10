const $ = id => document.getElementById(id);
let snapshot, snapshotText, report, sourceFile, sourceRevisions, patchLine, busy = false;

function node(tag, text, className) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}

async function request(url, data, raw = false) {
  const response = await fetch(url, data === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: raw ? data : JSON.stringify(data),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || 'HTTP ' + response.status);
  }
  return response;
}

function sourceLink(view, e) {
  const diffLine = e.binding.patch_line;
  const label = diffLine ? 'Open patch at row ' + diffLine : 'Open frozen source at line ' + e.start;
  const button = node('button', label);
  button.addEventListener('click', async () => {
    try {
      const files = view.files.filter(f => (e.side === 'base' ? f.base_path : f.path) === e.path);
      for (const file of files) {
        if (diffLine) {
          if (!file.patch) continue;
          const raw = new TextEncoder().encode(file.patch);
          const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', raw));
          const hash = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
          if (hash !== e.binding.sha256) continue;
        } else if (file.sides[e.side]?.sha256 !== e.binding.sha256) {
          continue;
        }
        if (snapshot === view) showSource(file, view.revisions, e.side, e.start, diffLine);
        return;
      }
      throw new Error('Source does not match the citation.');
    } catch (error) {
      $('status').textContent = 'Citation unavailable · ' + error.message;
    }
  });
  return button;
}

function evidence(ids) {
  $('evidence').replaceChildren();
  $('evidence-help').hidden = true;
  for (const id of ids) {
    const e = report.evidence.find(item => item.id === id);
    const section = node('article', '', 'citation');
    section.append(node('h4', e.path + ':' + e.start),
      node('p', e.side.toUpperCase() + ' · ' + e.binding.kind + ' · ' + e.revision, 'mono'),
      node('pre', e.start + '  ' + e.quote),
      node('p', e.id + ' · SHA-256 ' + e.binding.sha256, 'mono'),
      sourceLink(snapshot, e));
    $('evidence').append(section);
  }
}

function cards(target, rows, finding) {
  $(target).replaceChildren();
  for (const row of rows) {
    const button = node('button', '', 'review-card');
    button.append(node('strong', finding ? 'P' + row.priority + ' · ' + row.title : row.path),
      node('span', row.message));
    button.addEventListener('click', () => evidence(row.evidence_ids));
    $(target).append(button);
  }
  if (!rows.length) {
    $(target).append(node('p', finding ? 'No candidates from these rules. Human review is still required.' :
      'No parsed change observations available.', 'muted'));
  }
}

function render() {
  $('mode-badge').textContent = report.mode;
  $('mode-notice').textContent = report.notice;
  $('workspace').hidden = false;
  $('title').textContent = report.pr.title;
  $('identity').textContent = report.pr.repository + ' #' + report.pr.number + ' · ' +
    report.revisions.base.slice(0, 12) + ' → ' + report.revisions.head.slice(0, 12);
  $('count').textContent = '(' + report.findings.length + ')';
  cards('findings', report.findings.filter(row => $('priority').value === 'all' ||
    String(row.priority) === $('priority').value), true);
  cards('observations', report.observations, false);
  $('limits').replaceChildren(...report.abstentions.map(row => node('li', (row.path || 'PR') + ': ' + row.reason)));
}

async function run(data) {
  $('status').textContent = 'Reviewing bounded supplied text…';
  const result = await (await request('/api/review', data, true)).json();
  snapshotText = typeof data === 'string' ? data : new TextDecoder('utf-8', {fatal: true}).decode(data);
  snapshot = JSON.parse(snapshotText);
  $('confirm-provider').checked = false;
  report = result;
  $('source-panel').hidden = true;
  $('evidence').replaceChildren();
  $('evidence-help').hidden = false;
  $('priority').value = 'all';
  render();
  $('status').textContent = report.findings.length + ' findings · ' + report.observations.length +
    ' change observations · Human review required';
}

function action(fn) {
  return async () => {
    if (busy) return;
    busy = true;
    syncTaskControls();
    try {
      await fn();
    } catch (error) {
      $('status').textContent = 'Failed · ' + error.message;
    } finally {
      busy = false;
      syncTaskControls();
    }
  };
}

$('load').addEventListener('click', action(async () => {
  if (!$('demo').value) throw new Error('Choose an example snapshot first.');
  await run(await (await request('/api/demo/' + encodeURIComponent($('demo').value))).text());
}));

$('upload').addEventListener('change', action(async () => {
  const file = $('upload').files[0];
  $('upload').value = '';
  if (!file) return;
  if (file.size > 2_000_000) throw new Error('Snapshot must be at most 2 MB.');
  // Send original bytes so duplicate keys and malformed encoding reach the server.
  await run(await file.arrayBuffer());
}));

$('priority').addEventListener('change', render);

function download(content, type, filename) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const link = node('a', '');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$('export').addEventListener('click', action(async () => {
  download(await (await request('/api/export', snapshotText, true)).text(), 'text/markdown', report.snapshot_id + '.md');
}));
$('json').addEventListener('click', () => {
  download(JSON.stringify(report, null, 2), 'application/json', report.snapshot_id + '.json');
});

function showSource(file, revisions, side, line, diffLine) {
  sourceFile = file;
  sourceRevisions = revisions;
  patchLine = diffLine;
  $('source-side').value = side;
  $('source-line').value = line;
  $('source-panel').hidden = false;
  renderSource();
  ($('source-panel').querySelector('.located') || $('source-panel')).scrollIntoView({block: 'center'});
}

function sourceLines(text) {
  if (!text) return [];
  const rows = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
  if (rows.at(-1) === '') rows.pop();
  return rows;
}

function renderSource() {
  const side = $('source-side').value;
  const line = Number($('source-line').value);
  const source = sourceFile.sides[side];
  $('source-title').textContent = (side === 'base' ? sourceFile.base_path : sourceFile.path) +
    ' · ' + side.toUpperCase() + ' · ' + sourceRevisions[side];
  const lines = source ? sourceLines(source.text) : [];
  $('source-text').replaceChildren(...lines.map((row, i) =>
    node('span', (i + 1) + '  ' + row, 'source-row' + (i + 1 === line ? ' located' : ''))));
  $('source-note').textContent = source ?
    'Frozen blob ' + source.blob_sha + ' · SHA-256 ' + source.sha256 +
      (line >= 1 && line <= lines.length ? '' : ' · Line outside this source') :
    'Source unavailable on this side. The supplied patch only shows changed context.';
  $('source-diff').replaceChildren(...sourceLines(sourceFile.patch || 'No supplied text diff.').map((row, i) =>
    node('span', (i + 1) + '  ' + row, 'source-row' + (i + 1 === patchLine ? ' located' : ''))));
  if (patchLine) $('source-diff').parentElement.open = true;
}

$('source-side').addEventListener('change', () => { patchLine = undefined; renderSource(); });
$('go-line').addEventListener('click', () => {
  showSource(sourceFile, sourceRevisions, $('source-side').value, Number($('source-line').value));
});

function taskSourceLink(savedTask, e) {
  const button = node('button', 'Open frozen source at ' + e.path + ':' + e.line);
  button.addEventListener('click', async () => {
    try {
      const stored = await (await request('/api/task-json/' + encodeURIComponent(savedTask.id))).json();
      if (task?.id !== savedTask.id) return;
      const file = stored.view.files.find(f => (e.side === 'base' ? f.base_path : f.path) === e.path);
      const source = file?.sides[e.side];
      if (stored.view.revisions[e.side] !== e.revision || source?.sha256 !== e.content_sha256 ||
          sourceLines(source.text)[e.line - 1] !== e.quote) {
        throw new Error('Stored source no longer matches the citation.');
      }
      showSource(file, stored.view.revisions, e.side, e.line);
    } catch (error) {
      $('task-message').textContent = 'Source unavailable · ' + error.message;
    }
  });
  return button;
}

// The investigation is independently cancellable while its worker runs. Polling
// only reads persisted state; it never starts or repeats a decision/tool step.
let task, taskTimer, taskRequest = 0, providerReady = false;
const terminalTask = t => ['COMPLETED','STOPPED','CANCELLED'].includes(t.status);
function syncTaskControls() {
  $('create-task').disabled = busy || !snapshot;
  $('create-provider-task').disabled = busy || !snapshot || !providerReady || !$('confirm-provider').checked;
  $('cancel-task').disabled = !task || terminalTask(task);
}
async function refreshTasks() {
  const previous = $('task-saved').value;
  const rows = await (await request('/api/tasks')).json();
  $('task-saved').replaceChildren(node('option', 'Choose a saved task'));
  $('task-saved').firstChild.value = '';
  for (const t of rows) {
    const option = node('option', `${t.mode} · ${t.pr.repository} #${t.pr.number} · ${t.status} · ${t.goal}`);
    option.value = t.id; $('task-saved').append(option);
  }
  $('task-saved').value = previous;
}
function renderTask(t) {
  task = t; $('task-detail').hidden = false;
  $('task-title').textContent = `${t.mode} · ${t.pr.repository} #${t.pr.number}`;
  $('task-progress').textContent = `${t.status} · ${t.reason || 'Investigation in progress'} · decisions ${t.usage.rounds}/${t.budget.rounds} · tools ${t.usage.tools}/${t.budget.tools} · output ${t.usage.output_bytes}/${t.budget.output_bytes} bytes`;
  $('task-goal-display').textContent = 'Objective: '+t.goal;
  $('task-identity').textContent = JSON.stringify({id:t.id,identity:t.identity,budget:t.budget,provider:t.provider},null,2);
  const visibleEvents=t.events.filter(e=>e.kind!=='decision_started'&&e.kind!=='tool_started'&&!(e.kind==='decision'&&e.decision.kind==='finish'));
  $('task-events').replaceChildren(...visibleEvents.map(e => {
    const item = node('li', `${e.kind} · ${e.summary}`);
    if (e.decision?.tool) {
      const nextDecision=t.events.find(n=>n.kind==='decision'&&n.seq>e.seq);
      const executed=t.events.some(n=>n.kind==='tool_started'&&n.seq>e.seq&&(!nextDecision||n.seq<nextDecision.seq));
      item.append(node('pre', `${executed ? 'Executed' : 'Selected (not started)'} ${e.decision.tool} ${JSON.stringify(e.decision.args)}`));
    }
    if(e.kind==='tool_result') {
      const r=t.results[e.result_index];item.append(node('span', ` ${r.tool}: ${r.evidence.length} source references; ${r.unknown.join(', ') || 'collected scope only'}.`));
    }
    return item;
  }));
  // Keep disclosure state while polling so evidence stays readable.
  const opened = new Set([...$('task-results').querySelectorAll('details[open]')].map(e => e.dataset.index));
  $('task-results').replaceChildren(...t.results.map((r,i) => {
    const detail = node('details',''); detail.dataset.index=String(i); detail.open=opened.has(String(i));
    detail.append(node('summary', `${i+1}. ${r.tool} · ${r.evidence.length} references · ${r.coverage}${r.truncated ? ' · TRUNCATED' : ''}`));
    detail.append(node('p', `${r.revision} · Content SHA-256 ${r.content_sha256}`, 'mono'));
    detail.append(node('p', `Unknown: ${r.unknown.join(', ') || 'No additional omission in this bounded response'}. ${r.notice}`));
    detail.append(node('pre', typeof r.data === 'string' ? r.data : JSON.stringify(r.data,null,2)));
    for (const e of r.evidence) detail.append(taskSourceLink(t, e), node('pre', `${e.path}:${e.line} · ${e.side} · ${e.revision}\n${e.quote}\n${e.id}\nSource SHA-256 ${e.content_sha256} · blob ${e.blob_sha}`));
    return detail;
  }));
  $('task-candidates').replaceChildren(...t.candidates.map(c => {
    const article=node('article','','task-candidate');
    article.append(node('h4',c.title),node('p','Trigger: '+c.trigger),node('p','Change: '+JSON.stringify(c.change)),node('p','Impact: '+c.impact),node('p',`Citation validation: ${c.citation_validity} · ${c.inference} · correctness ${c.correctness}`));
    for (const e of c.citations) article.append(taskSourceLink(t, e), node('pre',`${e.path}:${e.line} · ${e.side} · ${e.revision}\n${e.quote}\n${e.id}`));
    return article;
  }));
  if (!t.candidates.length) $('task-candidates').append(node('p', 'No accepted candidates. Evidence may be insufficient or the hypothesis withdrawn. This is not approval.'));
  if (t.rejected.length) $('task-candidates').append(node('p',`${t.rejected.length} rejected candidate(s): deterministic reference, version, change or duplicate validation failed.`));
  $('task-limits').textContent = `Stop reason: ${t.reason || 'Not stopped'}. ${t.notice} No model called; no calibrated confidence or correctness score.`;
  $('task-message').textContent = `Saved automatically · ${t.id}`;
  syncTaskControls();
}
async function watchTask(id) {
  clearTimeout(taskTimer); const sequence=++taskRequest;
  try {
    const t=await (await request('/api/task/'+encodeURIComponent(id))).json();
    if(sequence!==taskRequest)return;
    renderTask(t);
    if(!terminalTask(t)) taskTimer=setTimeout(()=>watchTask(id),350);
    else await refreshTasks();
  } catch(error) { $('task-message').textContent='Task read failed: '+error.message+' Reopen the saved task after restarting the server.'; }
}
async function createTask(options) {
  try {
    const body = '{"snapshot":' + snapshotText + ',' + JSON.stringify(options).slice(1);
    const t = await (await request('/api/tasks', body, true)).json();
    await refreshTasks();
    $('task-saved').value = t.id;
    await watchTask(t.id);
  } catch (error) {
    $('task-message').textContent = 'Task creation failed: ' + error.message;
  }
}

$('create-task').addEventListener('click', action(async () => {
  await createTask({goal: $('task-goal').value, budget: {
    rounds: Number($('task-rounds').value), tools: Number($('task-tools').value),
    output_bytes: Number($('task-bytes').value), seconds: Number($('task-seconds').value),
  }});
}));

async function refreshProvider() {
  try {
    providerReady = (await (await request('/api/investigation-provider')).json()).ready;
  } catch {
    providerReady = false;
  }
  $('provider-status').textContent = 'Real model calls are disabled. ' +
    (providerReady ? 'Local Mock worker ready.' : 'Local Mock worker unavailable.');
  syncTaskControls();
}

$('provider-controls').addEventListener('toggle', () => {
  if ($('provider-controls').open) refreshProvider();
});
setInterval(() => {
  if ($('provider-controls').open) refreshProvider();
}, 1500);
$('confirm-provider').addEventListener('change', syncTaskControls);
$('create-provider-task').addEventListener('click', action(async () => {
  if (!providerReady || !$('confirm-provider').checked) return;
  $('confirm-provider').checked = false;
  await createTask({mode: 'MOCK_PROVIDER_NOT_AI', confirmed: true});
}));
$('open-task').addEventListener('click', async () => {
  if($('task-saved').value) await watchTask($('task-saved').value);
});
$('cancel-task').addEventListener('click', async () => {
  if(!task)return;
  clearTimeout(taskTimer);++taskRequest;
  try {renderTask(await (await request('/api/task-cancel/'+task.id,{})).json());await refreshTasks();}
  catch(error) {$('task-message').textContent='Cancellation failed: '+error.message;await watchTask(task.id);}
});
for(const [button,prefix,ext,type] of [['task-md','task-export','md','text/markdown'],['task-json','task-json','json','application/json']]) {
  $(button).addEventListener('click',async()=>{
    if(!task)return;
    try {
      const content=await (await request(`/api/${prefix}/${task.id}`)).text();
      download(content, type, `${task.id}.${ext}`);
    }catch(error){$('task-message').textContent='Export failed: '+error.message;}
  });
}
refreshTasks().catch(error => {$('task-message').textContent='Saved tasks unavailable: '+error.message;});

action(async () => {
  for (const demo of await (await request('/api/demos')).json()) {
    const option = node('option', demo.title);
    option.value = demo.id;
    $('demo').append(option);
  }
})();
