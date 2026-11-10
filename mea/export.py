"""Safe plain Markdown export; quotes and titles remain untrusted text."""
import html
import re


def plain(value):
    return html.escape(str(value), quote=True).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]').replace('\n', ' ')


def markdown(report):
    out = ['# MEA PR Review', '', plain(report['pr']['title']), '',
           f"Mode: {report['mode']} | Real AI: {report['real_ai']} | Runtime: {report['runtime']}", '',
           plain(report['notice']), '', '## Findings', '']
    for row in report['findings']:
        out += [f"- P{row['priority']} {plain(row['title'])}: {plain(row['message'])} ({', '.join(row['evidence_ids'])})"]
    if not report['findings']:
        out += ['No accepted findings in the supplied context. This is not approval.']
    out += ['', '## Change observations', '']
    out += [f"- {plain(row['path'])}: {plain(row['message'])} ({', '.join(row['evidence_ids'])})" for row in report['observations']]
    out += ['', '## Evidence', '']
    for item in report['evidence']:
        fence = '`' * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', item['quote'])), default=0))
        out += [f"{item['id']}: {plain(item['path'])}:{item['start']} ({item['side']}, {item['revision']}, {item['binding']['kind']})", '',
                fence+'text', item['quote'], fence, '']
    out += ['## Abstentions', '']
    out += [f"- {plain(row.get('path') or 'PR')}: {plain(row['reason'])}" for row in report['abstentions']]
    return '\n'.join(out) + '\n'
