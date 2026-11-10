"""Versioned local records in the existing SQLite database; no credentials/network."""
import json
import re
import time
from ..contract import canonical,digest

VERSION='mea-human-review-v1'


def ensure(store):
    with store.transaction() as db:
        db.execute('create table if not exists human_records (id text primary key, kind text not null, doc text not null)')
        db.execute('create table if not exists human_send_slots (slot text primary key, intent_id text not null)')


def get(db,identifier,kind=None):
    if not isinstance(identifier,str) or not re.fullmatch('(review|draft|send|mock)-[0-9a-f]{64}',identifier):raise ValueError('REVIEW_RECORD_ID')
    row=db.execute('select kind,doc from human_records where id=?',(identifier,)).fetchone()
    if not row or (kind and row[0]!=kind):raise ValueError('REVIEW_RECORD_NOT_FOUND')
    value=json.loads(row[1])
    if value['id']!=identifier or value['schema']!=VERSION:raise ValueError('REVIEW_RECORD_VERSION')
    return value


def put(db,value):
    value['updated_at']=time.time()
    db.execute('insert into human_records values (?,?,?) on conflict(id) do update set doc=excluded.doc',(value['id'],value['kind'],canonical(value).decode()))


def record(kind,identity,**data):
    return dict(schema=VERSION,id=kind+'-'+digest(identity),kind=kind,identity=identity,created_at=time.time(),updated_at=time.time(),events=[],**data)


def event(value,kind,summary,**data):
    if len(value['events'])>=1000:raise ValueError('REVIEW_EVENT_BOUND')
    value['events'].append(dict(seq=len(value['events'])+1,kind=kind,summary=summary,at=time.time(),**data))


def rows(db,kind,review_id=None):
    if review_id is not None:
        # A workspace export must not silently lose older drafts when other
        # workspaces have filled the bounded top-level selection list.
        return [json.loads(r[0]) for r in db.execute("select doc from human_records where kind=? and json_extract(doc,'$.review_id')=? order by rowid desc",(kind,review_id))]
    return [json.loads(r[0]) for r in db.execute('select doc from human_records where kind=? order by rowid desc limit 200',(kind,))]
