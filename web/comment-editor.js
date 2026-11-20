import {copy, diagnostic, m} from './i18n.js';
import {block, button, detail, el, fields, human, lazy, link, pre, tag} from './review-components.js';

const name = path => path.split('/').at(-1);
const repo = pr => pr.repository + ' #' + pr.number;
const anchors = {base: 'LEFT', head: 'RIGHT'};
const owns = review => review?.mode === 'MOCK_OWNED_REVIEW';
const liveSend = send => send?.mode === 'LIVE_GITHUB';

function resetPreview(session) {
  session.preview = null;
  session.acknowledged = false;
}
function editorKey(d) { return d.id + ':' + d.revision; }
function readStatus(d) {
  return d.status === 'CONFIRMED_MOCK_ONLY' ? 'CONFIRMED' : d.status === 'CONFIRMED_LIVE' ? 'CONFIRMED · LIVE' : d.status || 'UNKNOWN';
}
function candidateBlock(review, t, candidateId, side, draft) {
  const check = (review.rereviews || []).find(x => x.new_task_id === t.id)?.candidates.find(x => x.candidate_id === candidateId);
  if (check && check.status !== 'NEW') return check;
  if (side === 'head' || draft?.anchor.side === 'head') {
    return review.versions.find(x => x.task_id === t.id)?.head_draft_block;
  }
}
function candidateStatus(review, t, c) {
  const d = review.drafts.find(x => x.task_id === t.id && x.candidate_id === c.id);
  return candidateBlock(review, t, c.id, c.change.side, d)?.status || (d ? readStatus(d) : 'NOT DRAFTED');
}
function renderCommentEditor(n, d, {review, ctx, session, act, reload, render, notify}) {
  const $ = id => n.querySelector('#' + id);
  const action = (title, fn, id, primary = false) =>
    button(title, () => act(fn), primary ? 'button primary' : 'button', id);
  const editable = !d.intent_id;
  if (ctx.editorKey !== editorKey(d)) {
    ctx.editorKey = editorKey(d); ctx.editorText = d.body; resetPreview(session);
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
      const submitted = body.value;
      const saved = await human('edit',{draft_id:d.id,body:submitted,anchor:d.anchor});
      if (ctx.editorKey === editorKey(d)) {
        ctx.editorKey = editorKey(saved);
        if (ctx.editorText === submitted) ctx.editorText = saved.body;
        resetPreview(session);
      }
      await reload(); render(); notify(m('Draft saved.'));
    },'save-draft');
    const previewButton = action(m('Preview exact comment'),async () => {
      session.preview = await human('preview',{draft_id:d.id}); session.acknowledged = false;
      render(); $('exact-preview')?.focus();
    },'preview-comment',true);
    const changed = () => body.value !== d.body;
    save.disabled = !changed(); previewButton.disabled = changed();
    body.addEventListener('input', () => {
      ctx.editorText = body.value; resetPreview(session);
      section.querySelector('#exact-preview')?.remove();
      save.disabled = !changed(); previewButton.disabled = changed();
      if ($('mock-send')) $('mock-send').disabled = changed() || !!ctx.invalidateMock;
    });
    commands.append(save, previewButton); section.append(commands);
    if (session.preview?.draft_id === d.id && !changed()) {
      const p = session.preview;
      const exact = el('section','','rail-block'); exact.id = 'exact-preview'; exact.tabIndex = -1;
      exact.append(el('h3',m('Exact comment preview')),fields([[m('Target'),repo(p.target)],[m('Commit'),p.payload.commit_id],
        [m('Diff anchor'),p.payload.path + ':' + p.payload.line + ' ' + p.payload.side],[m('Mode'),p.mode]]),
        el('pre',p.payload.body,'exact-body'));
      otherComments(exact,p.other_comments_at_anchor);
      if (p.mode === 'MOCK_GITHUB_NOT_LIVE' && p.confirmation_digest !== d.confirmation?.confirmation_digest) {
        const ack = el('input'); ack.type = 'checkbox'; ack.id = 'acknowledge'; ack.checked = session.acknowledged;
        const lab = el('label'); lab.htmlFor = ack.id;
        lab.append(ack,copy(m('I reviewed the exact text, revision and diff anchor.')));
        const confirm = button(m('Confirm Mock comment'),() => act(async () => {
          await human('confirm',{draft_id:d.id,preview:p,human_confirmed:session.acknowledged});
          ctx.invalidateMock = false;
          await reload(); render(); $('mock-send')?.focus(); notify(m('Exact Mock comment confirmed.'));
        }, {reloadOnError:true}),'button primary','confirm-comment');
        confirm.disabled = !ack.checked;
        ack.addEventListener('change',() => {session.acknowledged = ack.checked; confirm.disabled = !ack.checked;});
        exact.append(lab,confirm);
      }
      section.append(exact);
    }
    if (d.status === 'CONFIRMED_MOCK_ONLY' && d.confirmation && owns(review)) {
      const send = button(m('Send to Mock'),() => act(async () => {
        const r = await human('send',{draft_id:d.id,confirmation_digest:d.confirmation.confirmation_digest,mode:'MOCK'});
        resetPreview(session); await reload(); render();
        $('reconcile-send')?.focus(); notify(r.status === 'UNCERTAIN' ? m('Outcome unknown. Reconcile without resending.') : m('{status} · Simulated only', {status: diagnostic(r.status)}));
      }, {reloadOnError:true}),'button primary','mock-send');
      send.disabled = changed() || !!ctx.invalidateMock || !!(session.preview && session.preview.confirmation_digest !== d.confirmation.confirmation_digest);
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
  const sends = review.sends.filter(s => s.draft_id === d.id);
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
          const r = await human('reconcile',{intent_id:send.id}); await reload(); render();
          notify(r.status === 'UNCERTAIN' ? m('No unique complete match. Outcome remains unknown.') : m('{status} · No resend.', {status: diagnostic(r.status)}));
        },'reconcile-send'));
      }
      if (send.status === 'NOT_SENT' && !liveSend(send)) {
        const ack = el('input'); ack.type = 'checkbox';
        const id = 'unsent-ack'; ack.id = id;
        const lab = el('label'); lab.htmlFor = id; lab.append(ack,copy(m('Discard this provably unsent intent.')));
        const release = action(m('Require fresh confirmation'),async () => {
          await human('release-not-sent',{intent_id:send.id,human_confirmed:ack.checked});
          resetPreview(session); await reload(); render();
        },'release-not-sent');
        release.disabled = true; ack.addEventListener('change',() => {release.disabled = !ack.checked;});
        history.append(lab,release);
      }
      history.append(lazy(m('Dispatch receipt'),() => [pre({id:send.id,payload:send.payload,comment:send.remote_comment,receipts:send.receipts,events:send.events})]));
    }
  }
  if (d.history.length) section.append(lazy(m('Prior draft revisions'),() => [pre(d.history)]));
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

export {candidateBlock, candidateStatus, renderCommentEditor, resetPreview};
