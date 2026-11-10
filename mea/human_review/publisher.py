"""Explicit local Mock publisher. No live adapter, HTTP library or credential capability."""
import copy
from ..contract import digest
from .store import get,put,record,event
from .mock_github import MockGitHub


class Publisher:
    def __init__(self,service,fault=None):
        self.service=service;self.store=service.store;self.fault=fault or (lambda *_:None)
        if type(service.remote) is not MockGitHub:raise ValueError('LIVE_PUBLISH_DISABLED')

    def send(self,identifier,confirmation_digest,mode):
        if mode!='MOCK':raise ValueError('LIVE_PUBLISH_DISABLED')
        with self.store.launch_lock():
            blocked=None
            with self.store.transaction() as db:
                d=get(db,identifier,'draft')
                if d.get('intent_id'):return get(db,d['intent_id'],'send')
                conf=d.get('confirmation')
                if not conf or conf['confirmation_digest']!=confirmation_digest:raise ValueError('HUMAN_CONFIRMATION_REQUIRED')
                try:
                    fresh=self.service.preview(identifier)
                    review=get(db,d['review_id'],'review')
                    if review['versions'][-1]['task_id']!=d['task_id']:raise ValueError('REVIEW_VERSION_OUTDATED')
                    if fresh!=conf:raise ValueError('CONFIRMATION_INVALIDATED_BY_CHANGE')
                    slot=self.service.slot(fresh)
                    if db.execute('select intent_id from human_send_slots where slot=?',(slot,)).fetchone():raise ValueError('DUPLICATE_OR_UNCERTAIN_SEND_SLOT')
                except ValueError as exc:
                    blocked=str(exc);d.update(status='DRAFT',confirmation=None);event(d,'confirmation_invalidated',blocked);put(db,d)
                if not blocked:
                    intent=record('send',dict(draft_id=identifier,confirmation_digest=confirmation_digest),review_id=d['review_id'],draft_id=identifier,status='PREPARED',mode='MOCK_GITHUB_NOT_LIVE',target=conf['target'],payload=conf['payload'],remote_snapshot=conf['remote_identity'],approved_snapshot=copy.deepcopy(conf),remote_comment=None)
                    event(intent,'intent','Exact comment and confirmation committed before possible remote side effect. No automatic retry.');put(db,intent)
                    db.execute('insert into human_send_slots values (?,?)',(slot,intent['id']))
                    d.update(status='SEND_PREPARED',intent_id=intent['id']);put(db,d)
            if blocked:raise ValueError(blocked)
            self.fault('after_intent',intent)
            with self.store.transaction() as db:
                d=get(db,identifier,'draft');current=get(db,intent['id'],'send')
                # Independent final state check immediately before dispatch.
                try:
                    review=get(db,d['review_id'],'review')
                    if review['versions'][-1]['task_id']!=d['task_id']:raise ValueError('REVIEW_VERSION_OUTDATED')
                    if self.service.preview(identifier)!=d['confirmation']:raise ValueError('CONFIRMATION_CHANGED_BEFORE_LAUNCH')
                except ValueError as exc:
                    current['status']='NOT_SENT';event(current,'not_sent',str(exc));put(db,current)
                    d['status']='NOT_SENT';put(db,d);return current
                current['status']='SENDING';event(current,'sending','Mock request may have a remote effect; HTTP failure would not prove absence.');put(db,current)
            self.fault('after_sending',current)
            try:
                comment=self.service.remote.post(current['target'],copy.deepcopy(current['payload']),current['remote_snapshot']['generation'])
                self.validate_comment(comment,current)
                self.fault('after_remote',current)
            except Exception:
                with self.store.transaction() as db:
                    current=get(db,intent['id'],'send');current['status']='UNCERTAIN';event(current,'uncertain','Remote result unknown; slot retained. Read-only reconciliation or manual handling only.');put(db,current)
                    d=get(db,identifier,'draft');d['status']='UNCERTAIN';put(db,d)
                return current
            with self.store.transaction() as db:
                current=get(db,intent['id'],'send');current.update(status='MOCK_PUBLISHED',remote_comment=comment);event(current,'mock_published','Simulated GitHub comment persisted. This is not live publication.');put(db,current)
                d=get(db,identifier,'draft');d['status']='MOCK_PUBLISHED';put(db,d)
            self.fault('after_receipt',current)
            return current

    def abandon_not_sent(self,identifier,human_confirmed):
        if human_confirmed is not True:raise ValueError('EXPLICIT_HUMAN_CONFIRMATION_REQUIRED')
        with self.store.launch_lock(),self.store.transaction() as db:
            intent=get(db,identifier,'send')
            if intent['status']!='NOT_SENT':raise ValueError('ONLY_PROVABLY_NOT_SENT_MAY_RELEASE')
            intent['status']='ABANDONED_NEVER_SENT';event(intent,'abandoned','User discarded a provably unsent intent. Uncertain attempts can never use this path.');put(db,intent)
            db.execute('delete from human_send_slots where intent_id=?',(identifier,))
            d=get(db,intent['draft_id'],'draft')
            if len(d['history'])>=16:raise ValueError('DRAFT_REVISION_BOUND')
            d['history'].append({k:copy.deepcopy(d[k]) for k in ['revision','body','anchor','confirmation','status']})
            d.setdefault('abandoned_intents',[]).append(identifier);d.pop('intent_id',None)
            d.update(status='DRAFT',confirmation=None,revision=d['revision']+1);event(d,'confirmation_required','No resend occurred. A new exact user confirmation is required.');put(db,d)
        return d

    @staticmethod
    def validate_comment(comment,intent):
        if (not isinstance(comment,dict) or type(comment.get('id')) is not int or comment['id']<1 or
            not isinstance(comment.get('html_url'),str) or comment['html_url']!=f'https://mock-github.invalid/{intent["target"]["repository"]}/pull/{intent["target"]["number"]}#discussion_r{comment["id"]}' or
            comment.get('user',{}).get('login')!=intent['remote_snapshot']['principal'] or
            any(comment.get(k)!=v for k,v in intent['payload'].items())):raise ValueError('REMOTE_RECEIPT_MISMATCH')

    def reconcile(self,identifier):
        with self.store.launch_lock():
            with self.store.connection() as db:intent=get(db,identifier,'send')
            if intent['status'] not in {'SENDING','UNCERTAIN'}:return intent
            remote=self.service.remote.read(intent['target'])
            if not remote['complete_listing']:raise ValueError('RECONCILIATION_LISTING_INCOMPLETE')
            matches=[c for c in remote['comments'] if c.get('user',{}).get('login')==intent['remote_snapshot']['principal'] and all(c.get(k)==v for k,v in intent['payload'].items())]
            with self.store.transaction() as db:
                intent=get(db,identifier,'send')
                if len(matches)==1:
                    self.validate_comment(matches[0],intent)
                    intent.update(status='MOCK_PUBLISHED_RECONCILED',remote_comment=matches[0]);event(intent,'reconciled','One matching remote Mock comment found by read-only lookup; no POST or retry.')
                else:
                    intent['status']='UNCERTAIN';event(intent,'manual_handling_required','No unique complete remote match. Absence does not prove the request had no effect.')
                put(db,intent);d=get(db,intent['draft_id'],'draft');d['status']=intent['status'];put(db,d)
            return intent
