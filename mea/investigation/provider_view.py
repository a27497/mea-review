"""Byte-bounded whole-line pages for native provider tools; legacy reads stay v1."""
from ..contract import canonical, digest
from .tools import LIMITS as LEGACY_LIMITS, NOTICE, View as LegacyView

TOOL_VERSION = 'frozen-tools-v2-byte-pages'
MAX_READ_LINE = 2**31 - 1
LIMITS = {key: value for key, value in LEGACY_LIMITS.items() if key != 'snippet_lines'}


class View(LegacyView):
    tool_version = TOOL_VERSION
    limits = LIMITS

    def __init__(self, value):
        super().__init__(value)
        self.rows = {key: source['text'].splitlines() for key, source in self.sources.items()}

    @staticmethod
    def validate_range(start, end):
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= MAX_READ_LINE:
            raise ValueError('READ_RANGE_INVALID')

    def execute(self, name, args):
        if name in ('read_source', 'read_test'):
            return self.read_page(name, args, self.validate_call(name, args))
        return super().execute(name, args)

    def read_page(self, name, args, side):
        """Largest whole-line prefix fitting the complete serialized result.

        Budget includes data, citations, coverage/continuation and result digest.
        A blocked first line returns explicit insufficiency, never a partial quote
        or an unchanged automatic continuation that cannot make progress.
        """
        path=args['path'];start=args['start'];end=args['end']
        source=self.sources.get((side,path));rows=self.rows.get((side,path))
        total=len(rows) if rows is not None else None
        last=min(end,total) if total is not None else None
        refs=[];data=[]

        def result(status,unknown,returned_end=None):
            partial=status in {'BYTE_LIMIT','RESOURCE_INSUFFICIENT'}
            next_start=returned_end+1 if status=='BYTE_LIMIT' else None
            read=dict(path=path,side=side,requested_start=start,requested_end=end,total_lines=total,
                      returned_start=start if refs else None,returned_end=returned_end,returned_lines=len(refs),
                      eof=None if total is None else (status in {'EMPTY_SOURCE','START_BEYOND_EOF'} or returned_end==total),
                      status=status,next_start=next_start,blocked_line=start if status=='RESOURCE_INSUFFICIENT' else None,
                      continuation=dict(tool=name,args=dict(args,start=next_start)) if next_start is not None else None,
                      source_sha256=source['sha256'] if source else None,blob_sha=source['blob_sha'] if source else None)
            value=dict(tool=name,tool_version=TOOL_VERSION,view_sha256=self.sha256,revision=args['revision'],
                       data=list(data),evidence=list(refs),read=read,unknown=unknown,coverage='COLLECTED_FILES_ONLY',
                       truncated=partial,limits=LIMITS,notice=NOTICE)
            value['content_sha256']=digest(value)
            return value

        if rows is None:page=result('SOURCE_NOT_COLLECTED',['SOURCE_NOT_COLLECTED'])
        elif not rows:page=result('EMPTY_SOURCE',[])
        elif start>total:page=result('START_BEYOND_EOF',['LINE_OUTSIDE_SOURCE'])
        else:
            page=result('RESOURCE_INSUFFICIENT',['TOOL_OUTPUT_BYTE_BOUND','WHOLE_LINE_EXCEEDS_TOOL_BYTE_BOUND'])
            for n in range(start,last+1):
                ref=self.citation(side,path,n)
                refs.append(ref);data.append(dict(line=n,text=ref['quote']))
                candidate=result('COMPLETE' if n==last else 'BYTE_LIMIT',[] if n==last else ['TOOL_OUTPUT_BYTE_BOUND'],n)
                if len(canonical(candidate))>LIMITS['result_bytes']:break
                page=candidate
        if len(canonical(page))>LIMITS['result_bytes']:raise ValueError('TOOL_RESULT_METADATA_BYTE_BOUND')
        return page
