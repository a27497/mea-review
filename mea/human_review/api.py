"""Same-origin local API; only explicit Mock side effects, no live transport."""
from ..contract import decode,fields
from . import demo
from .publisher import Publisher


def get_request(handler):
    service=handler.server.human_reviews;path=handler.path
    try:
        if path=='/api/human/reviews':return handler.send(200,service.list())
        if path=='/api/human/status':return handler.send(200,dict(live_publish=False,mode='MOCK_ONLY',versions=demo.versions(),real_model_requests=0,real_github_writes=0))
        for prefix,kind in [('/api/human/review/','json'),('/api/human/export-json/','json'),('/api/human/export-md/','md')]:
            if path.startswith(prefix):
                identifier=path[len(prefix):]
                return handler.send(200,service.export(identifier).encode(),'text/markdown') if kind=='md' else handler.send(200,service.open(identifier))
        return handler.send(404,dict(error='HUMAN_ROUTE_NOT_FOUND'))
    except (ValueError,KeyError,TypeError):return handler.send(400,dict(error='HUMAN_RECORD_INVALID'))


def post_request(handler):
    sizes=handler.headers.get_all('Content-Length') or []
    if (handler.headers.get('Transfer-Encoding') or len(sizes) != 1
            or not sizes[0].isascii() or not sizes[0].isdecimal() or len(sizes[0]) > 5
            or not 0 < int(sizes[0]) <= 64000 or handler.headers.get('Content-Type') != 'application/json'):
        return handler.send(400, dict(error='HUMAN_BODY_BOUND'))
    service=handler.server.human_reviews
    try:
        handler.connection.settimeout(5);data=decode(handler.rfile.read(int(sizes[0])))
        action=handler.path.removeprefix('/api/human/')
        schema={'create':'task_id','demo':'version','draft':'review_id task_id candidate_id','edit':'draft_id body anchor','preview':'draft_id','confirm':'draft_id preview human_confirmed','send':'draft_id confirmation_digest mode','reconcile':'intent_id','release-not-sent':'intent_id human_confirmed','increment':'review_id task_id','compare-linked':'review_id old_task_id new_task_id','mock-state':'version can_comment fault complete_listing'}
        if action not in schema:raise ValueError('HUMAN_ACTION_NOT_ALLOWED')
        fields(data,schema[action])
        if action=='create':result=service.create(data['task_id'])
        elif action=='demo':
            task=demo.run(service.store,data['version']);result=dict(task_id=task['id'],mode=demo.MODE,candidates=len(task['candidates']))
        elif action=='draft':result=service.draft(data['review_id'],data['task_id'],data['candidate_id'])
        elif action=='edit':result=service.edit(data['draft_id'],data['body'],data['anchor'])
        elif action=='preview':result=service.preview(data['draft_id'])
        elif action=='confirm':result=service.confirm(data['draft_id'],data['preview'],data['human_confirmed'])
        elif action=='send':result=Publisher(service).send(data['draft_id'],data['confirmation_digest'],data['mode'])
        elif action=='release-not-sent':result=Publisher(service).abandon_not_sent(data['intent_id'],data['human_confirmed'])
        elif action=='reconcile':result=Publisher(service).reconcile(data['intent_id'])
        elif action=='increment':result=service.increment(data['review_id'],data['task_id'])
        elif action=='compare-linked':result=service.compare_linked(data['review_id'],data['old_task_id'],data['new_task_id'])
        else:result=service.remote.configure(label=data['version'],can_comment=data['can_comment'],fault=data['fault'],complete_listing=data['complete_listing'])
        return handler.send(200,result)
    except (ValueError,KeyError,TypeError,UnicodeError,RecursionError) as exc:
        code=str(exc) if type(exc) is ValueError else 'HUMAN_INPUT_INVALID'
        return handler.send(400,dict(error=code))
