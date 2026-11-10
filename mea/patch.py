"""Extracted bounded unified-hunk reader; supplied text is never executed."""
import re
SIDES = ("base", "head")

def parse(text,sides):
    if not isinstance(text,str) or not text or len(text.encode())>60000:raise ValueError('PATCH_MISSING_OR_BUDGET')
    rows=[];hunk=None;seen={};last={s:0 for s in SIDES}
    def finish():
        if hunk and any(seen[s]!=hunk[s][1] for s in SIDES):raise ValueError('PATCH_TRUNCATED_OR_COUNT_MISMATCH')
    for i,line in enumerate(text.splitlines(),1):
        match=re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*',line)
        if match:
            finish();hunk={'base':(int(match[1]),int(match[2] or 1)),'head':(int(match[3]),int(match[4] or 1))};seen={s:0 for s in SIDES}
            for s,(start,count) in hunk.items():
                if start<last[s] or (count and start<1):raise ValueError('PATCH_OVERLAP_OR_ORDER')
                last[s]=start+count
            rows.append(dict(line=i,text=line,kind='header',base=None,head=None));continue
        if line=='\\ No newline at end of file':
            if not rows or hunk is None:raise ValueError('PATCH_FORMAT_UNKNOWN')
            rows.append(dict(line=i,text=line,kind='marker',base=None,head=None));continue
        if hunk is None or not line or line[0] not in ' +-':raise ValueError('PATCH_FORMAT_UNKNOWN')
        row=dict(line=i,text=line,kind={' ':'context','+':'addition','-':'deletion'}[line[0]],base=None,head=None)
        for s in SIDES:
            if line[0] not in (' -' if s=='base' else ' +'):continue
            n=hunk[s][0]+seen[s];seen[s]+=1;row[s]=n
            if s in sides:
                ls=sides[s]['text'].splitlines()
                if not 1<=n<=len(ls) or ls[n-1]!=line[1:]:raise ValueError('PATCH_SOURCE_MISMATCH')
        rows.append(row)
    finish()
    if not rows or hunk is None or not any(r['kind'] in ['addition','deletion'] for r in rows):raise ValueError('PATCH_NO_CHANGE')
    return rows
