"""Conservative source/AST correspondence. No target execution or text-only issue matching."""
import ast
import copy
from ..contract import digest
from ..investigation.tools import LIMITS, View
from . import demo


def functions(view):
    result=[]
    rename_modules={f['path'][:-3].replace('/','.') : f['base_path'][:-3].replace('/','.') for f in view.value['files'] if f['path'].endswith('.py') and f['base_path'].endswith('.py')}
    for (side,path),source in view.sources.items():
        if side!='head' or not path.endswith('.py'):continue
        try:tree=ast.parse(source['text'])
        except (SyntaxError,ValueError,RecursionError):continue
        context=[copy.deepcopy(n) for n in tree.body if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
        for item in context:
            for node in ast.walk(item):
                if isinstance(node,ast.ImportFrom):node.module=rename_modules.get(node.module,node.module)
                if isinstance(node,ast.Import):
                    for alias in node.names:alias.name=rename_modules.get(alias.name,alias.name)
        module_context=digest([ast.dump(n,include_attributes=False) for n in context])
        for n in ast.walk(tree):
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
                result.append(dict(path=path,name=n.name,start=n.lineno,end=n.end_lineno,structural_sha256=digest(ast.dump(n,include_attributes=False)),node=n,top_level=n in tree.body,module_context_sha256=module_context))
    return result


def enclosing(items,path,line):
    matches=[f for f in items if f['path']==path and f['start']<=line<=f['end']]
    return min(matches,key=lambda f:f['end']-f['start']) if matches else None


def public_function(f):
    return {k:v for k,v in f.items() if k!='node'} if f else None


def compare(old,new):
    ov=View(old['view']);nv=View(new['view']);ofs=functions(ov);nfs=functions(nv)
    relation='UNKNOWN'
    if demo.registered(old) and demo.registered(new):
        row=next(r for r in demo.versions() if r['input_sha256']==new['identity']['input_sha256'])
        relation='MOCK_LINEAR' if row['parent_head']==ov.value['revisions']['head'] else 'FORCE_PUSH_OR_NONCONTIGUOUS'
    matches=[];consumed=set();observed={e['id'] for r in new['results'] for e in r['evidence']};reads=[]
    for c in old['candidates']:
        change=c['change'];of=enclosing(ofs,change['path'],change['line'])
        exact=[f for f in nfs if of and f['structural_sha256']==of['structural_sha256']]
        mapped=exact[0] if len(exact)==1 else None
        if not mapped and not exact and of:
            paths={change['path']}|{f['path'] for f in nv.value['files'] if f['base_path']==change['path']}
            named=[f for f in nfs if f['path'] in paths and f['name']==of['name']]
            if len(named)==1:mapped=named[0]
        row=dict(old_candidate_id=c['id'],old_change=change,old_version=ov.value['revisions'],new_version=nv.value['revisions'],old_view_sha256=ov.sha256,new_view_sha256=nv.sha256,old_function=public_function(of),old_evidence=copy.deepcopy(c['citations']),new_function=public_function(mapped),status='NEEDS_RECHECK',issue_status='UNKNOWN',location_status='OUTDATED',correctness='UNADJUDICATED',evidence=[],reason='Version ancestry or semantic evidence is insufficient.')
        if mapped:
            row['location_status']='MAPPED_NOT_REPUBLISHED'
            end = min(mapped['end'], mapped['start'] + LIMITS['snippet_lines'] - 1)
            result = nv.execute('read_source', dict(path=mapped['path'],
                                revision=nv.value['revisions']['head'],
                                start=mapped['start'], end=end))
            reads.append(result)
            row['evidence'] = copy.deepcopy(result['evidence'])
            complete = (not result['truncated'] and
                        [e['line'] for e in result['evidence']] ==
                        list(range(mapped['start'], mapped['end'] + 1)))
            nc=[x for x in new['candidates'] if enclosing(nfs,x['change']['path'],x['change']['line']) is mapped]
            if len(nc)==1:
                consumed.add(nc[0]['id']);row['new_candidate_id']=nc[0]['id'];row['new_change']=nc[0]['change']
            if relation=='MOCK_LINEAR' and len(nc)==1 and of['structural_sha256']==mapped['structural_sha256'] and all(e['id'] in observed for e in nc[0]['citations']):
                # Also require old caller-function context to have an unchanged,
                # uniquely mapped counterpart actually observed by the new task.
                context_ok=True
                for e in c['citations']:
                    oldf=enclosing(ofs,e['path'],e['line'])
                    if not oldf or not oldf['top_level']:context_ok=False;break
                    same=[f for f in nfs if f['structural_sha256']==oldf['structural_sha256']]
                    if len(same)!=1 or not same[0]['top_level'] or same[0]['module_context_sha256']!=oldf['module_context_sha256'] or not any(x['path']==same[0]['path'] and same[0]['start']<=x['line']<=same[0]['end'] for x in nc[0]['citations']):context_ok=False
                if context_ok:row.update(status='STILL_PRESENT',issue_status='CONDITIONALLY_SUPPORTED',reason='New validated candidate reasserts the conditional issue; unique unchanged function/caller ASTs and new observed citations support correspondence despite location movement.')
            # A deliberately narrow proof for REGISTERED owned literal-None demos
            # only. Disappearance, matching text or arbitrary model prose never fixes an issue.
            n=mapped['node'];on=of['node'] if of else None
            literal=lambda fn:fn.body[0].value if isinstance(fn,ast.FunctionDef) and not fn.decorator_list and len(fn.body)==1 and isinstance(fn.body[0],ast.Return) and isinstance(fn.body[0].value,ast.Constant) else None
            before=literal(on) if on else None;after=literal(n)
            if relation=='MOCK_LINEAR' and demo.registered(old) and old['identity']['policy_version']==demo.POLICY and before is not None and before.value is None and after is not None and type(after.value) is int and ast.dump(on.args)==ast.dump(n.args) and complete and not nc:
                row.update(status='RESOLVED',issue_status='NARROW_STATIC_TRIGGER_ELIMINATED',reason='Owned Mock rule only: observed undecorated single-return function changed from literal None to an integer. This eliminates the recorded local None-return trigger, not all runtime defects.')
        elif relation=='MOCK_LINEAR' and not exact:
            row.update(status='OUTDATED',reason='Original function/anchor no longer maps uniquely; disappearance is not proof of resolution.')
        if relation!='MOCK_LINEAR':row.update(status='NEEDS_RECHECK',issue_status='UNKNOWN',reason='Force push, noncontiguous or unverified ancestry; no automatic semantic carry-over or resolution.')
        matches.append(row)
    for c in new['candidates']:
        if c['id'] not in consumed:matches.append(dict(status='NEW',new_candidate_id=c['id'],new_change=c['change'],new_version=nv.value['revisions'],new_view_sha256=nv.sha256,correctness='UNADJUDICATED',issue_status='CONDITIONAL_NEW_CANDIDATE',location_status='CURRENT_FROZEN_DIFF',evidence=c['citations'],reason='New-version validated candidate not uniquely paired to an old anchor; not a proven new regression.'))
    return dict(policy='phase4-source-correspondence-v1',relation=relation,rows=matches,comparison_tool_results=reads,limitations='AST/source correspondence is not semantic correctness. Missing context, ambiguous moves and unverified ancestry require recheck. No comments modified or sent.')
