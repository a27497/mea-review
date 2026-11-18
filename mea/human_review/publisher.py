"""Confirmed-comment publisher for the local Mock and, in the attended CLI lane, live GitHub.

The intent journal (PREPARED -> SENDING -> outcome) and the send slot are committed
before any remote effect. An uncertain outcome is never retried: it is reconciled by
a read-only listing, and only a complete listing without a match plus explicit human
release frees the slot for a freshly confirmed send.
"""
import copy
from ..contract import digest
from .store import get,put,record,event
from .mock_github import MockGitHub
from .remote import NotSent,binding,is_live


class Publisher:
    def __init__(self,service,fault=None):
        self.service=service;self.store=service.store;self.fault=fault or (lambda *_:None)
        if type(service.remote) is not MockGitHub and not is_live(service.remote):raise ValueError('LIVE_PUBLISH_DISABLED')
        self.remote=service.remote

    def send(self,identifier,confirmation_digest,mode):
        if mode!=self.remote.send_mode:raise ValueError('LIVE_PUBLISH_DISABLED')
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
                    if self.service.live:
                        if not d.get('approval') or d['approval']['binding_sha256']!=binding(fresh):raise ValueError('HUMAN_APPROVAL_BINDING_MISMATCH')
                    slot=self.service.slot(fresh)
                    if db.execute('select intent_id from human_send_slots where slot=?',(slot,)).fetchone():raise ValueError('DUPLICATE_OR_UNCERTAIN_SEND_SLOT')
                except ValueError as exc:
                    blocked=str(exc);d.update(status='DRAFT',confirmation=None,approval=None);event(d,'confirmation_invalidated',blocked);put(db,d)
                if not blocked:
                    intent=record('send',dict(draft_id=identifier,confirmation_digest=confirmation_digest),review_id=d['review_id'],draft_id=identifier,status='PREPARED',mode=self.remote.mode,target=conf['target'],payload=conf['payload'],remote_snapshot=conf['remote_identity'],approved_snapshot=copy.deepcopy(conf),approval=copy.deepcopy(d.get('approval')),remote_comment=None,receipts=[])
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
                current['status']='SENDING';event(current,'sending','Request may have a remote effect; HTTP failure would not prove absence.');put(db,current)
            self.fault('after_sending',current)
            try:
                if self.service.live:
                    # The other-account comments the human saw at this anchor must still be exactly those.
                    others=copy.deepcopy(current['approved_snapshot'].get('other_comments_at_anchor'))
                    comment=self.remote.post(current['target'],copy.deepcopy(current['payload']),current['remote_snapshot']['generation'],others=others)
                else:comment=self.remote.post(current['target'],copy.deepcopy(current['payload']),current['remote_snapshot']['generation'])
                self.validate_comment(comment,current)
                self.fault('after_remote',current)
            except NotSent as exc:
                with self.store.transaction() as db:
                    current=get(db,intent['id'],'send');current['status']='NOT_SENT';current['receipts'].append(exc.receipt or dict(outcome='NO_POST_ISSUED'))
                    event(current,'not_sent','Definitively no remote comment: '+exc.code+'. Slot retained until explicit release.');put(db,current)
                    d=get(db,identifier,'draft');d['status']='NOT_SENT';put(db,d)
                return current
            except Exception as exc:
                with self.store.transaction() as db:
                    current=get(db,intent['id'],'send');current['status']='UNCERTAIN'
                    receipt=getattr(exc,'receipt',None) or getattr(self.remote,'last_receipt',None)
                    if receipt:current['receipts'].append(receipt)
                    event(current,'uncertain','Remote result unknown ('+(getattr(exc,'code',None) or type(exc).__name__)+'); slot retained. Read-only reconciliation or manual handling only.');put(db,current)
                    d=get(db,identifier,'draft');d['status']='UNCERTAIN';put(db,d)
                return current
            with self.store.transaction() as db:
                current=get(db,intent['id'],'send');current.update(status=self.remote.published,remote_comment=comment)
                receipt=getattr(self.remote,'last_receipt',None)
                if receipt:current['receipts'].append(receipt)
                if self.service.live:event(current,'published','GitHub returned 201 with a comment matching the confirmed commit, path, line, side and body.')
                else:event(current,'mock_published','Simulated GitHub comment persisted. This is not live publication.')
                put(db,current)
                d=get(db,identifier,'draft');d['status']=self.remote.published;put(db,d)
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
            d.update(status='DRAFT',confirmation=None,approval=None,revision=d['revision']+1);event(d,'confirmation_required','No resend occurred. A new exact user confirmation is required.');put(db,d)
        return d

    def release_after_reconcile(self,identifier,human_confirmed):
        """Free an UNCERTAIN slot only after a complete read-only listing found no match.

        The draft then needs a fresh preview and confirmation; the next send re-reads the
        listing and refuses a duplicate anchor. No POST happens here."""
        if human_confirmed is not True:raise ValueError('EXPLICIT_HUMAN_CONFIRMATION_REQUIRED')
        with self.store.launch_lock(),self.store.transaction() as db:
            intent=get(db,identifier,'send')
            if intent['status']!='UNCERTAIN' or intent.get('last_reconcile')!='ABSENT':raise ValueError('RECONCILED_ABSENCE_REQUIRED')
            intent['status']='RELEASED_AFTER_RECONCILE';event(intent,'released','User released the slot after a complete listing showed no matching comment. Absence is evidence, not proof.');put(db,intent)
            db.execute('delete from human_send_slots where intent_id=?',(identifier,))
            d=get(db,intent['draft_id'],'draft')
            if len(d['history'])>=16:raise ValueError('DRAFT_REVISION_BOUND')
            d['history'].append({k:copy.deepcopy(d[k]) for k in ['revision','body','anchor','confirmation','status']})
            d.setdefault('abandoned_intents',[]).append(identifier);d.pop('intent_id',None)
            d.update(status='DRAFT',confirmation=None,approval=None,revision=d['revision']+1);event(d,'confirmation_required','Released after reconciliation. A new exact human confirmation is required before any send.');put(db,d)
        return d

    def validate_comment(self,comment,intent):
        if (not isinstance(comment,dict) or type(comment.get('id')) is not int or comment['id']<1 or
            not isinstance(comment.get('html_url'),str) or comment['html_url']!=self.remote.comment_url(intent['target'],comment['id']) or
            comment.get('user',{}).get('login')!=intent['remote_snapshot']['principal'] or
            any(comment.get(k)!=v for k,v in intent['payload'].items())):raise ValueError('REMOTE_RECEIPT_MISMATCH')

    def reconcile(self,identifier):
        with self.store.launch_lock():
            with self.store.connection() as db:intent=get(db,identifier,'send')
            if intent['status'] not in {'SENDING','UNCERTAIN'}:return intent
            remote=self.remote.read(intent['target'])
            if not remote['complete_listing']:raise ValueError('RECONCILIATION_LISTING_INCOMPLETE')
            matches=[c for c in remote['comments'] if c.get('user',{}).get('login')==intent['remote_snapshot']['principal'] and all(c.get(k)==v for k,v in intent['payload'].items())]
            with self.store.transaction() as db:
                intent=get(db,identifier,'send')
                if len(matches)==1:
                    self.validate_comment(matches[0],intent)
                    intent.update(status=self.remote.reconciled,remote_comment=matches[0]);event(intent,'reconciled','One matching remote comment found by read-only lookup; no POST or retry.',comment_id=matches[0]['id'])
                else:
                    intent['status']='UNCERTAIN'
                    if not matches:intent['reconciled_absent']=intent.get('reconciled_absent',0)+1
                    # Release is allowed only when the latest complete listing showed no match.
                    intent['last_reconcile']='ABSENT' if not matches else 'MULTIPLE'
                    event(intent,'manual_handling_required','No unique complete remote match ('+str(len(matches))+' found). Absence does not prove the request had no effect.',matches=len(matches))
                put(db,intent);d=get(db,intent['draft_id'],'draft');d['status']=intent['status'];put(db,d)
            return intent
