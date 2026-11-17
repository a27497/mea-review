"""Byte-bounded whole-line pages for native provider tools; legacy reads stay v1."""
from ..contract import canonical, digest
from .tools import LIMITS as LEGACY_LIMITS, NOTICE, View as LegacyView

TOOL_VERSION = 'frozen-tools-v2-byte-pages'
CROSS_FILE_TOOL_VERSION = 'frozen-tools-v3-cross-file'
MAX_READ_LINE = 2**31 - 1
LIMITS = {key: value for key, value in LEGACY_LIMITS.items() if key != 'snippet_lines'}


class View(LegacyView):
    tool_version = TOOL_VERSION
    limits = LIMITS
    page_lines = None
    stored_bytes = LIMITS['result_bytes']

    @staticmethod
    def page_bytes(value):
        return len(canonical(value))

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
            next_start=returned_end+1 if status in {'BYTE_LIMIT','PAGE_LIMIT'} else None
            read=dict(path=path,side=side,requested_start=start,requested_end=end,total_lines=total,
                      returned_start=start if refs else None,returned_end=returned_end,returned_lines=len(refs),
                      eof=None if total is None else (status in {'EMPTY_SOURCE','START_BEYOND_EOF'} or returned_end==total),
                      status=status,next_start=next_start,blocked_line=start if status=='RESOURCE_INSUFFICIENT' else None,
                      continuation=dict(tool=name,args=dict(args,start=next_start)) if next_start is not None else None,
                      source_sha256=source['sha256'] if source else None,blob_sha=source['blob_sha'] if source else None)
            value=dict(tool=name,tool_version=self.tool_version,view_sha256=self.sha256,revision=args['revision'],
                       data=list(data),evidence=list(refs),read=read,unknown=unknown+self.capture_unknown(),coverage='COLLECTED_FILES_ONLY',
                       truncated=partial,limits=self.limits,notice=NOTICE)
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
                if self.page_lines and n<last and len(refs)>=self.page_lines:
                    candidate=result('PAGE_LIMIT',[],n)
                    if self.page_bytes(candidate)<=LIMITS['result_bytes'] and len(canonical(candidate))<=self.stored_bytes:
                        page=candidate
                    break
                candidate=result('COMPLETE' if n==last else 'BYTE_LIMIT',[] if n==last else ['TOOL_OUTPUT_BYTE_BOUND'],n)
                if self.page_bytes(candidate)>LIMITS['result_bytes'] or len(canonical(candidate))>self.stored_bytes:
                    refs.pop();data.pop()
                    if refs:page=result('BYTE_LIMIT',['TOOL_OUTPUT_BYTE_BOUND'],refs[-1]['line'])
                    break
                page=candidate
        if self.page_bytes(page)>LIMITS['result_bytes'] or len(canonical(page))>self.stored_bytes:raise ValueError('TOOL_RESULT_METADATA_BYTE_BOUND')
        return page


class CrossFileView(View):
    """Opt-in cross-file tools: import edges, JS/TS lexical symbols and JS/TS test reads.

    Citations still come only from View.citation, so every reference resolves to a
    collected file and line of the selected snapshot.
    """
    tool_version = CROSS_FILE_TOOL_VERSION
    cross_file = True
    # Source and test pages hold at most 80 lines. The 24000-byte bound counts what the
    # model sees (line, evidence ID, quote and read metadata); the stored result keeps full
    # citations and stays within four times that bound.
    page_lines = 80
    stored_bytes = 4 * LIMITS['result_bytes']
    limits = dict(LIMITS, page_lines=80, stored_result_bytes=4 * LIMITS['result_bytes'], result_bytes_basis='MODEL_VISIBLE')

    @staticmethod
    def page_bytes(value):
        visible = {k: v for k, v in value.items() if k not in ('data', 'evidence', 'limits', 'notice')}
        visible['rows'] = [[e['line'], e['id'], e['quote']] for e in value['evidence']]
        return len(canonical(visible))
