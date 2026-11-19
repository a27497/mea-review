export function sourceLines(text) {
  if (!text) return [];
  // Match the Python snapshot reader's splitlines() numbering.
  const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
  if (lines.at(-1) === '') lines.pop();
  return lines;
}
export function patches(patch) {
  const hunks = [];
  let h, base = 0, head = 0;
  for (const text of sourceLines(patch)) {
    const m = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (m) {
      base = +m[1]; head = +m[2];
      h = {base, head, text, rows: []}; hunks.push(h);
    } else if (h && text.startsWith(' ')) h.rows.push({base: base++, head: head++, text: text.slice(1)});
    else if (h && text.startsWith('+')) h.rows.push({head: head++, text: text.slice(1), kind: 'add'});
    else if (h && text.startsWith('-')) h.rows.push({base: base++, text: text.slice(1), kind: 'del'});
    else if (h && text.startsWith('\\')) h.rows.push({text});
  }
  return hunks;
}
