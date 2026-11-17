"""Durable LOCAL mock of GitHub's line-comment contract. No HTTP implementation.

Mock commits its own side effects before returning, enabling ambiguous-result
faults independently of the caller's intent journal. No real repo is configured.
"""
import copy
from ..contract import digest,fields,text
from .store import get,put,record
from . import demo,github_contract
from ..investigation.tools import View

TARGET=dict(repository='mea-owned/phase4-demo',number=1)


class MockGitHub:
    mode='MOCK_GITHUB_NOT_LIVE'
    published='MOCK_PUBLISHED';reconciled='MOCK_PUBLISHED_RECONCILED';confirmed='CONFIRMED_MOCK_ONLY';send_mode='MOCK'
    def __init__(self,store):self.store=store

    @staticmethod
    def identity_of(remote):
        return {k:copy.deepcopy(remote[k]) for k in ['identity','head','base','state','can_comment','can_read','principal','generation','complete_listing']}|dict(comments_sha256=digest(remote['comments']))

    @staticmethod
    def comment_url(target,identifier):
        return f'https://mock-github.invalid/{target["repository"]}/pull/{target["number"]}#discussion_r{identifier}'

    def provision(self,task):
        if not demo.registered(task) or task['pr']['repository']!=TARGET['repository'] or task['pr']['number']!=1:raise ValueError('MOCK_TARGET_NOT_ALLOWLISTED')
        identifier='mock-'+digest(TARGET)
        with self.store.transaction() as db:
            try:return get(db,identifier,'mock')
            except ValueError:pass
            value=record('mock',TARGET,head=task['view']['revisions']['head'],base=task['view']['revisions']['base'],state='open',can_comment=True,can_read=True,principal='mock-owner',generation=1,comments=[],post_attempts=0,complete_listing=True,fault='NONE')
            put(db,value);return value

    def read(self,target):
        if target!=TARGET:raise ValueError('LIVE_PUBLISH_DISABLED_NO_TARGET')
        with self.store.connection() as db:value=get(db,'mock-'+digest(TARGET),'mock')
        if not value['can_read']:raise ValueError('REMOTE_READ_PERMISSION')
        return value

    def configure(self,*,label=None,can_comment=None,fault=None,complete_listing=None):
        with self.store.transaction() as db:
            value=get(db,'mock-'+digest(TARGET),'mock')
            if label:
                v=demo.load(label);value.update(head=v['revisions']['head'],base=v['revisions']['base'])
            if can_comment is not None:
                if type(can_comment) is not bool:raise ValueError('MOCK_BOOLEAN')
                value['can_comment']=can_comment
            if complete_listing is not None:
                if type(complete_listing) is not bool:raise ValueError('MOCK_BOOLEAN')
                value['complete_listing']=complete_listing
            if fault is not None:
                if fault not in {'NONE','BEFORE_EFFECT','AFTER_EFFECT','HTTP_500_AFTER_EFFECT'}:raise ValueError('MOCK_FAULT')
                value['fault']=fault
            value['generation']+=1;put(db,value);return value

    def post(self,target,payload,expected_generation):
        if target!=TARGET:raise ValueError('LIVE_PUBLISH_DISABLED_NO_TARGET')
        wire=github_contract.comment_request(target,payload)
        failure=None;comment=None
        with self.store.transaction() as db:
            value=get(db,'mock-'+digest(TARGET),'mock');value['post_attempts']+=1;value['last_mock_wire_request']=wire
            row=next((r for r in demo.versions() if r['head']==value['head']),None)
            if not row:raise ValueError('MOCK_FROZEN_HEAD_REQUIRED')
            view=View.from_input(demo.load(row['label']))
            side={'RIGHT':'head','LEFT':'base'}.get(payload['side']);file=next((f for f in view.value['files'] if f['path']==payload['path']),None)
            if not side or not file or type(payload['line']) is not int or (side,file['path' if side=='head' else 'base_path'],payload['line']) not in view.changes:raise ValueError('REMOTE_DIFF_ANCHOR_INVALID')
            if expected_generation!=value['generation'] or payload['commit_id']!=value['head'] or not value['can_comment'] or value['state']!='open':failure='REMOTE_CHANGED_AFTER_ADMISSION'
            elif value['fault']=='BEFORE_EFFECT':failure='MOCK_CONNECTION_UNKNOWN'
            else:
                identifier=len(value['comments'])+1
                comment=dict(id=identifier,html_url=f'https://mock-github.invalid/{TARGET["repository"]}/pull/1#discussion_r{identifier}',user=dict(login=value['principal']),**payload)
                value['comments'].append(comment);value['generation']+=1
                if value['fault'] in {'AFTER_EFFECT','HTTP_500_AFTER_EFFECT'}:failure='MOCK_RESPONSE_UNKNOWN'
            put(db,value)
        if failure:raise OSError(failure)
        return comment
