"""Manual review preparation and source-bound incremental revisions.

The default remote is the local Mock. A LiveGitHub remote is only injected by the
attended command-line lane (mea.github_writeback); the workbench never has one.
"""
import copy
import json
from ..contract import digest,canonical,fields,text
from ..investigation.tools import View
from ..investigation.validation import validate_candidates
from .store import ensure,get,put,record,event,rows
from .mock_github import MockGitHub,TARGET
from .remote import anchor_others,binding,is_live
from . import demo,compare,rereview


def task_checked(store,identifier):
    t=store.get(identifier);v=View(t['view'])
    if t['id']!='task-'+digest(t['identity']) or t['pr']!=v.value['pr'] or v.value['input_sha256']!=t['identity']['input_sha256'] or v.sha256!=t['identity']['view_sha256'] or v.value['revisions']!=t['identity']['revisions']:raise ValueError('REVIEW_VIEW_MISMATCH')
    for r in t['results']:
        if r['view_sha256']!=v.sha256 or digest({k:x for k,x in r.items() if k!='content_sha256'})!=r['content_sha256']:raise ValueError('REVIEW_EVIDENCE_MISMATCH')
    proposed=[{k:c[k] for k in ['title','trigger','change','impact','evidence_ids']} for c in t['candidates']]
    accepted,rejected=validate_candidates(v,proposed,t['results'])
    if rejected or accepted!=t['candidates']:raise ValueError('REVIEW_CANDIDATE_INVALID')
    if t['status']!='COMPLETED':raise ValueError('COMPLETED_REVIEW_REQUIRED')
    return t,v


class Service:
    def __init__(self,store,remote=None):
        self.store=store;ensure(store);self.remote=remote if remote is not None else MockGitHub(store)
        self.live=is_live(self.remote)

    def recover(self):
        # A second server must not classify another process's live preparation
        # as provably unsent while that process can still dispatch it.
        with self.store.launch_lock(),self.store.transaction() as db:
            pending=[json.loads(r[0]) for r in db.execute("select doc from human_records where kind='send' and json_extract(doc,'$.status') in ('PREPARED','SENDING')")]
            for value in pending:
                if value['status'] in {'PREPARED','SENDING'}:
                    value['status']='NOT_SENT' if value['status']=='PREPARED' else 'UNCERTAIN'
                    event(value,'recovered','No automatic send: deterministic preparation was not sent, or remote outcome is uncertain.');put(db,value)
                    d=get(db,value['draft_id'],'draft');d['status']=value['status'];put(db,d)

    def list(self):
        with self.store.connection() as db:return rows(db,'review')

    def open(self,identifier):
        with self.store.connection() as db:
            value=get(db,identifier,'review');value['drafts']=rows(db,'draft',identifier)
            value['sends']=rows(db,'send',identifier)
        value['tasks']=[self.store.get(v['task_id']) for v in value['versions']]
        value['live_publish']='COMMAND_LINE_ALLOWLIST_ONLY' if self.live else 'DISABLED_NO_AUTHORIZED_REPOSITORY_OR_CREDENTIALS'
        return value

    def create(self,task_id):
        t,v=task_checked(self.store,task_id);identity=dict(task_id=task_id,view_sha256=v.sha256)
        mode='LIVE_GITHUB_REVIEW' if self.live and self.remote.admits(t['pr']) else 'MOCK_OWNED_REVIEW' if demo.registered(t) else 'LOCAL_PREVIEW_ONLY'
        value=record('review',identity,pr=t['pr'],mode=mode,versions=[dict(task_id=task_id,view_sha256=v.sha256,revisions=v.value['revisions'])],comparisons=[])
        with self.store.transaction() as db:
            exists=db.execute('select 1 from human_records where id=?',(value['id'],)).fetchone()
            if not exists:
                event(value,'created','Local human review workspace; no model request and live publication disabled.');put(db,value)
        if demo.registered(t) and not self.live:self.remote.provision(t)
        return self.open(value['id'])

    def draft(self,review_id,task_id,candidate_id):
        with self.store.connection() as db:review=get(db,review_id,'review')
        if task_id not in [v['task_id'] for v in review['versions']]:raise ValueError('REVIEW_VERSION_NOT_LINKED')
        t,v=task_checked(self.store,task_id)
        if t['reason']!='HYPOTHESIS_SUPPORTED':raise ValueError('EVIDENCE_INSUFFICIENT_FOR_COMMENT')
        c=next((c for c in t['candidates'] if c['id']==candidate_id),None)
        if not c:raise ValueError('CANDIDATE_NOT_FOUND')
        self.rereview_block(review,task_id,candidate_id)
        value=record('draft',dict(review_id=review_id,task_id=task_id,candidate_id=candidate_id),review_id=review_id,task_id=task_id,candidate_id=candidate_id,revision=1,history=[],status='DRAFT',confirmation=None,anchor=copy.deepcopy(c['change']),body=f'{c["title"]}\n\nTrigger: {c["trigger"]}\nImpact: {c["impact"]}\n\nConditional inference; correctness UNADJUDICATED.',source_kind=t['mode'])
        with self.store.transaction() as db:
            try:return get(db,value['id'],'draft')
            except ValueError:pass
            event(value,'drafted','Candidate selected by user; editable draft is not a confirmed or published comment.');put(db,value)
        return value

    def edit(self,identifier,body,anchor):
        text(body,8000)
        if not body.strip():raise ValueError('COMMENT_BODY_REQUIRED')
        with self.store.transaction() as db:
            d=get(db,identifier,'draft')
            if d.get('intent_id'):raise ValueError('DRAFT_HAS_SEND_INTENT')
            if body==d['body'] and anchor==d['anchor']:return d
            if len(d['history'])>=16:raise ValueError('DRAFT_REVISION_BOUND')
            d['history'].append({k:copy.deepcopy(d[k]) for k in ['revision','body','anchor','confirmation','status']})
            d.update(body=body,anchor=copy.deepcopy(anchor),revision=d['revision']+1,confirmation=None,approval=None,status='DRAFT')
            self.payload(d);event(d,'edited','Final text or anchor changed; previous confirmation invalidated.');put(db,d)
        return d

    @staticmethod
    def rereview_block(review,task_id,candidate_id):
        # An outdated comment is never relocated, and a sent comment is never repeated.
        for check in review.get('rereviews',[]):
            row=next((x for x in check['candidates'] if check['new_task_id']==task_id and x['candidate_id']==candidate_id),None)
            if row and row['status']=='DUPLICATE_OF_SENT_COMMENT':raise ValueError('REREVIEW_DUPLICATE_OF_SENT_COMMENT')
            if row and row['status']=='AT_OUTDATED_COMMENT_LOCATION':raise ValueError('REREVIEW_AT_OUTDATED_COMMENT_LOCATION')

    def payload(self,d):
        t,v=task_checked(self.store,d['task_id'])
        with self.store.connection() as db:review=get(db,d['review_id'],'review')
        # Checked on every preview, confirmation and send, not only when a draft is created.
        self.rereview_block(review,d['task_id'],d['candidate_id'])
        if t['reason']!='HYPOTHESIS_SUPPORTED':raise ValueError('EVIDENCE_INSUFFICIENT_FOR_COMMENT')
        candidate=next((c for c in t['candidates'] if c['id']==d['candidate_id']),None)
        if not candidate:raise ValueError('CANDIDATE_NOT_FOUND')
        a=d['anchor'];fields(a,'path side line revision')
        if a['side']=='base' and self.incremental(review,d['task_id']):raise ValueError('INCREMENT_BASE_SIDE_NOT_COMMENTABLE')
        if a['side'] not in {'head','base'} or type(a['line']) is not int or a['revision']!=v.value['revisions'][a['side']] or (a['side'],a['path'],a['line']) not in v.changes:raise ValueError('COMMENT_DIFF_ANCHOR_INVALID')
        citation=v.citation(a['side'],a['path'],a['line'])
        if citation not in candidate['citations']:raise ValueError('COMMENT_ANCHOR_NOT_CANDIDATE_EVIDENCE')
        file=next(f for f in v.value['files'] if f['path' if a['side']=='head' else 'base_path']==a['path'])
        target=dict(repository=t['pr']['repository'],number=t['pr']['number'])
        return dict(target=target,mock_admitted=demo.registered(t),pr_url=t['pr']['url'],view_sha256=v.sha256,task_id=t['id'],draft_revision=d['revision'],evidence=citation,base_revision=v.value['revisions']['base'],
                    payload=dict(commit_id=v.value['revisions']['head'],path=file['path'],line=a['line'],side='RIGHT' if a['side']=='head' else 'LEFT',body=d['body']))

    @staticmethod
    def incremental(review,task_id):
        """Local evidence that a version's base side is its predecessor's head, not the PR merge base.

        History cannot prove the opposite: a --since snapshot opened as the first version has no
        linked predecessor. The live lane therefore admits a LEFT anchor only when GitHub's merge
        base, read in the same admission, equals the frozen base revision (admit_remote)."""
        versions=review['versions'];index=next(i for i,v in enumerate(versions) if v['task_id']==task_id)
        return index>0 and versions[index]['revisions']['base']==versions[index-1]['revisions']['head']

    def remote_identity(self,remote):
        return copy.deepcopy(self.remote.identity_of(remote))

    def admitted(self,value):
        return self.remote.admits(value['target']) if self.live else value['target']==TARGET and value['mock_admitted']

    def preview(self,identifier):
        with self.store.connection() as db:d=get(db,identifier,'draft')
        value=self.payload(d);admitted=self.admitted(value)
        value.update(mode=self.remote.mode if admitted else 'LOCAL_PREVIEW_ONLY',draft_id=d['id'],correctness='UNADJUDICATED',live_publish=self.live and admitted)
        if admitted:
            remote=self.remote.read(value['target']);self.admit_remote(value,remote)
            value['remote_identity']=self.remote_identity(remote)
            value['other_comments_at_anchor']=self.others(value,remote)
        else:value['remote_identity']=None
        value['confirmation_digest']=digest(value)
        return value

    @staticmethod
    def others(value,remote):
        """Comments by other accounts (including bots) on the same anchor, shown to the confirmer."""
        return anchor_others(value['payload'],remote)

    @staticmethod
    def slot(value):
        return digest(dict(target=value['target'],**{k:value['payload'][k] for k in ['commit_id','path','line','side']}))

    def admit_remote(self,value,remote):
        if remote['identity']!=value['target'] or remote['head']!=value['payload']['commit_id']:raise ValueError('PR_HEAD_DRIFT')
        if not remote['can_comment'] or remote['state']!='open':raise ValueError('REMOTE_PERMISSION_OR_STATE')
        if not remote['complete_listing']:raise ValueError('REMOTE_DUPLICATES_UNKNOWN')
        # Idempotency: our own principal never comments twice on one anchor. Other reviewers' comments do not block.
        if any(c.get('user',{}).get('login')==remote['principal'] and all(c.get(k)==value['payload'][k] for k in ['commit_id','path','line','side']) for c in remote['comments']):raise ValueError('DUPLICATE_REMOTE_ANCHOR')
        p=value['payload']
        # LEFT means the PR merge base on GitHub; refuse when the frozen base side is another commit (an increment),
        # whatever the review history says. A live remote must report the merge base; its absence is a refusal.
        if p['side']=='LEFT' and (self.live or 'merge_base' in remote) and remote.get('merge_base')!=value['base_revision']:raise ValueError('LEFT_SIDE_BASE_MISMATCH')
        if 'commentable' in remote and p['line'] not in remote['commentable'].get(p['path'],{}).get(p['side'],[]):raise ValueError('REMOTE_DIFF_ANCHOR_INVALID')

    def decline(self,identifier,reference):
        """Record a human decision not to send this draft. No remote request."""
        text(reference,500)
        if not reference.strip():raise ValueError('HUMAN_APPROVAL_REFERENCE_REQUIRED')
        with self.store.transaction() as db:
            d=get(db,identifier,'draft')
            if d.get('intent_id'):raise ValueError('DRAFT_HAS_SEND_INTENT')
            if d['status']!='DECLINED_BY_HUMAN':
                d.update(status='DECLINED_BY_HUMAN',confirmation=None,approval=None,declined=dict(reference=reference))
                event(d,'declined','Human decided not to send this comment. Nothing was written remotely.',reference=reference);put(db,d)
        return d

    def confirm(self,identifier,preview,human_confirmed,approval=None):
        if human_confirmed is not True:raise ValueError('EXPLICIT_HUMAN_CONFIRMATION_REQUIRED')
        if self.live:
            # The live lane needs the recorded human decision for this exact binding.
            fields(approval,'reference binding_sha256 acknowledged_other_comment_ids');text(approval['reference'],500)
            if not approval['reference'].strip():raise ValueError('HUMAN_APPROVAL_REFERENCE_REQUIRED')
            ack=approval['acknowledged_other_comment_ids']
            if not isinstance(ack,list) or any(type(x) is not int for x in ack):raise ValueError('ACKNOWLEDGED_OTHER_COMMENTS_INVALID')
        elif approval is not None:raise ValueError('APPROVAL_ONLY_FOR_LIVE')
        fresh=self.preview(identifier)
        if preview!=fresh:raise ValueError('CONFIRMATION_SNAPSHOT_CHANGED')
        if fresh['mode']!=self.remote.mode:raise ValueError('LIVE_PUBLISH_DISABLED_NO_TARGET')
        if self.live and approval['binding_sha256']!=binding(fresh):raise ValueError('HUMAN_APPROVAL_BINDING_MISMATCH')
        # Another account's comment on the same anchor must be seen and accepted, never silently ignored.
        if self.live and sorted(approval['acknowledged_other_comment_ids'])!=sorted(c['id'] for c in fresh['other_comments_at_anchor']):raise ValueError('OTHER_COMMENTS_AT_ANCHOR_NOT_ACKNOWLEDGED')
        with self.store.transaction() as db:
            d=get(db,identifier,'draft')
            if d.get('intent_id'):raise ValueError('DRAFT_HAS_SEND_INTENT')
            if d['status']=='DECLINED_BY_HUMAN':raise ValueError('DRAFT_DECLINED_BY_HUMAN')
            # Re-read source/body/revision under the same local write transaction.
            if self.payload(d)!= {k:fresh[k] for k in self.payload(d)}:raise ValueError('CONFIRMATION_SNAPSHOT_CHANGED')
            if self.live:
                d.update(status=self.remote.confirmed,confirmation=copy.deepcopy(fresh),approval=dict(approval));event(d,'confirmed','Recorded human approval binds repository, PR, head, file, line, side and exact body; remote permission snapshot read live.',confirmation_snapshot=copy.deepcopy(fresh),approval=dict(approval))
            else:
                d.update(status='CONFIRMED_MOCK_ONLY',confirmation=copy.deepcopy(fresh));event(d,'confirmed','Explicit local user confirmation binds repository, PR, head, diff anchor, final body and remote permission snapshot. Mock only.',confirmation_snapshot=copy.deepcopy(fresh))
            put(db,d)
        return d

    def increment(self,review_id,new_task_id):
        new,nv=task_checked(self.store,new_task_id)
        with self.store.launch_lock(),self.store.transaction() as db:
            r=get(db,review_id,'review')
            if new_task_id in [v['task_id'] for v in r['versions']]:return self.open(review_id)
            if len(r['versions'])>=8:raise ValueError('REVIEW_VERSION_BOUND')
            old,ov=task_checked(self.store,r['versions'][-1]['task_id'])
            if any(new['pr'][k]!=old['pr'][k] for k in ['repository','number','url']):raise ValueError('REVIEW_PR_MISMATCH')
            if nv.value['revisions']['head']==ov.value['revisions']['head']:raise ValueError('NEW_FROZEN_HEAD_REQUIRED')
            result=compare.compare(old,new)
            r['versions'].append(dict(task_id=new_task_id,view_sha256=nv.sha256,revisions=nv.value['revisions']));r['comparisons'].append(dict(old_task_id=old['id'],new_task_id=new['id'],**result))
            event(r,'manual_rereview','Explicit user action linked independently frozen tasks and source-based comparisons. No model, publication or comment modification.');put(db,r)
            for d in rows(db,'draft',review_id):
                if d['review_id']==review_id and not d.get('intent_id') and d.get('confirmation'):
                    event(d,'version_changed','New frozen review version invalidated the old confirmation.',previous_confirmation=d['confirmation'])
                    d.update(confirmation=None,approval=None,status='DRAFT');put(db,d)
        return self.open(review_id)

    def rereview(self,review_id,new_task_id,observed=None):
        """Link an incremental frozen version and check every sent comment's anchor against it.

        observed is an optional read-only remote listing (LiveGitHub.read) recorded next to the
        local anchor result. Nothing is sent, moved or edited."""
        self.increment(review_id,new_task_id)
        with self.store.launch_lock(),self.store.transaction() as db:
            r=get(db,review_id,'review')
            done=next((x for x in r.get('rereviews',[]) if x['new_task_id']==new_task_id),None)
            if done:
                # Repeat: keep the local anchor result; only refresh the read-only remote observation.
                if observed is not None:
                    remote={c['id']:c for c in observed.get('comments',[])}
                    for row in done['comments']:row['github']=dict(remote[row['comment_id']]['current']) if row['comment_id'] in remote else 'NOT_LISTED'
                    done['observed_remote']=True
                    event(r,'remote_reobserved','Read-only remote comment state refreshed for an existing re-review.',new_task_id=new_task_id);put(db,r)
            else:
                ids=[v['task_id'] for v in r['versions']];index=ids.index(new_task_id)
                if index==0:raise ValueError('REREVIEW_PREDECESSOR_REQUIRED')
                # Re-reviews run in version order: a later mapping is never computed without its
                # predecessor's result, so no stored result has to be recomputed afterwards.
                if index>1 and not any(x['new_task_id']==ids[index-1] for x in r.get('rereviews',[])):raise ValueError('REREVIEW_PREDECESSOR_RESULT_REQUIRED')
                old_id=ids[index-1];old,_=task_checked(self.store,old_id);new,_=task_checked(self.store,new_task_id)
                sends=rows(db,'send',review_id)
                prior=[x for x in r.get('rereviews',[]) if x['new_task_id'] in ids[:index]]
                result=rereview.check(old,new,sends,observed,prior)
                r.setdefault('rereviews',[]).append(dict(old_task_id=old_id,new_task_id=new_task_id,**result))
                event(r,'incremental_rereview','Sent comment anchors checked against the increment; outdated comments are not re-posted at a new location.',new_task_id=new_task_id)
                put(db,r)
        return self.open(review_id)

    def compare_linked(self,review_id,old_task_id,new_task_id):
        """Persist a manual comparison without changing the active version or a comment."""
        with self.store.launch_lock(),self.store.transaction() as db:
            r=get(db,review_id,'review')
            versions=[v['task_id'] for v in r['versions']]
            if old_task_id not in versions or new_task_id not in versions:
                raise ValueError('REVIEW_VERSION_NOT_LINKED')
            if versions.index(old_task_id)>=versions.index(new_task_id):
                raise ValueError('COMPARISON_VERSION_ORDER')
            if not any(x['old_task_id']==old_task_id and x['new_task_id']==new_task_id for x in r['comparisons']):
                old,_=task_checked(self.store,old_task_id)
                new,_=task_checked(self.store,new_task_id)
                r['comparisons'].append(dict(old_task_id=old_task_id,new_task_id=new_task_id,**compare.compare(old,new)))
                event(r,'manual_linked_comparison','Explicit deterministic comparison of linked frozen versions. No model, publication or comment modification.')
                put(db,r)
        return self.open(review_id)

    def export(self,identifier):
        value=self.open(identifier)
        return '# Human-gated review · '+value['mode']+'\n\nLive publication DISABLED. Mock is not live success.\n\n```json\n'+json.dumps(value,ensure_ascii=False,indent=2)+'\n```\n'
