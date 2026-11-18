// Official MCP TypeScript SDK client against the read-only MEA server (stdio).
// initialize -> tools/list -> tools/call, plus refused inputs. Synthetic fixture only, no network.
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {readFile, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const root = fileURLToPath(new URL('..', import.meta.url));
const fixture = process.env.MEA_MCP_SNAPSHOT || path.join(root, 'fixtures/review/v4.json');
const snapshot = JSON.parse(await readFile(fixture, 'utf8'));
const {base, head} = snapshot.revisions;
const sdkVersion = JSON.parse(await readFile(path.join(root, 'node_modules/@modelcontextprotocol/sdk/package.json'), 'utf8')).version;

const transport = new StdioClientTransport({
  command: 'python3', args: ['-B', '-m', 'mea.mcp_server', '--snapshot', fixture],
  cwd: root, env: {PATH: process.env.PATH, LANG: 'C.UTF-8'}, stderr: 'pipe',
});
const client = new Client({name: 'mea-roundtrip-test', version: '1.0.0'});
const steps = [], sent = [];
const send = transport.send.bind(transport);
transport.send = message => { sent.push(message.method ? {method: message.method, protocolVersion: message.params?.protocolVersion} : {reply: message.id}); return send(message); };
const json = result => JSON.parse(result.content[0].text);

await client.connect(transport);  // initialize + notifications/initialized
const server = client.getServerVersion();
assert.equal(server.name, 'mea-review-readonly');
assert.deepEqual(client.getServerCapabilities(), {tools: {listChanged: false}});
assert.equal(sent[0].method, 'initialize');
steps.push({step: 'initialize', requestedProtocolVersion: sent[0].protocolVersion, server, capabilities: client.getServerCapabilities()});

await client.ping();
const {tools} = await client.listTools();
const names = tools.map(t => t.name).sort();
assert.deepEqual(names, ['find_symbol', 'read_diff', 'read_source', 'read_test', 'references', 'related_files', 'search', 'snapshot_info']);
for (const t of tools) {
  assert.equal(t.annotations.readOnlyHint, true, t.name);
  assert.equal(t.annotations.destructiveHint, false, t.name);
  assert.equal(t.annotations.openWorldHint, false, t.name);
  assert.equal(t.inputSchema.additionalProperties, false, t.name);
}
assert.ok(!names.some(n => /send|confirm|write|publish|post|model|edit/.test(n)));
steps.push({step: 'tools/list', tools: names});

const info = await client.callTool({name: 'snapshot_info', arguments: {}});
assert.equal(info.isError, false);
assert.deepEqual(json(info).revisions, snapshot.revisions);
const renamed = json(info).files.find(f => f.status === 'renamed');
steps.push({step: 'tools/call snapshot_info', files: json(info).files.length});

const page = await client.callTool({name: 'read_source', arguments: {path: renamed.path, revision: head, start: 1, end: 3}});
assert.equal(page.isError, false);
const read = page.structuredContent;
assert.equal(read.read.status, 'COMPLETE');
assert.ok(read.evidence.length >= 1);
for (const e of read.evidence) {
  assert.equal(e.path, renamed.path); assert.equal(e.side, 'head'); assert.equal(e.revision, head);
  assert.match(e.id, /^ev-[0-9a-f]{64}$/);
}
steps.push({step: 'tools/call read_source', lines: read.data.map(r => r.line), evidence: read.evidence[0].id});

const changed = json(info).files.find(f => f.status === 'modified' && f.diff);
const diff = await client.callTool({name: 'read_diff', arguments: {path: changed.base_path, revision: base}});
assert.equal(diff.isError, false);
assert.ok(diff.structuredContent.evidence.length > 0);
assert.ok(diff.structuredContent.evidence.every(e => e.side === 'base' && e.revision === base));
const related = await client.callTool({name: 'related_files', arguments: {path: renamed.path, revision: head}});
assert.equal(related.isError, false);
assert.ok(related.structuredContent.evidence.length > 0);
steps.push({step: 'tools/call read_diff', base_changes: diff.structuredContent.evidence.length});
steps.push({step: 'tools/call related_files', edges: related.structuredContent.data.length});

for (const [name, args, expectedPath] of [
  ['read_test', {path: 'test_app.py', start: 1, end: 4}, 'test_app.py'],
  ['find_symbol', {symbol: 'quota'}, 'limits.py'],
  ['references', {symbol: 'quota'}, 'app.py'],
  ['search', {query: 'available'}, 'app.py'],
]) {
  const result = await client.callTool({name, arguments: {...args, revision: head}});
  assert.equal(result.isError, false, name);
  const evidence = result.structuredContent.evidence;
  assert.ok(evidence.some(e => e.path === expectedPath && e.side === 'head' && e.revision === head), name);
  assert.deepEqual(json(result), result.structuredContent);
  steps.push({step: 'tools/call ' + name, citations: evidence.length});
}

// Refused inputs: path traversal, a revision outside the snapshot, an extra argument, an unknown tool.
const refused = {};
for (const [label, args, tool] of [
  ['traversal', {path: '../etc/passwd', revision: head, start: 1, end: 1}, 'read_source'],
  ['dotenv', {path: '.env', revision: head, start: 1, end: 1}, 'read_source'],
  ['foreign revision', {path: renamed.path, revision: 'f'.repeat(40), start: 1, end: 1}, 'read_source'],
  ['extra argument', {query: 'quota', revision: head, url: 'https://example.invalid'}, 'search'],
]) {
  const r = await client.callTool({name: tool, arguments: args});
  assert.equal(r.isError, true, label);
  refused[label] = r.content[0].text;
}
await assert.rejects(client.callTool({name: 'send_comment', arguments: {}}), err => err.code === -32602);
refused['unknown tool'] = -32602;
steps.push({step: 'refused inputs', refused});

await client.close();
const summary = {result: 'MCP_ROUNDTRIP_PASS', messages: sent.map(m => m.method).filter(Boolean), sdk: '@modelcontextprotocol/sdk@' + sdkVersion, fixture: path.relative(root, fixture), steps};
if (process.env.MEA_MCP_OUT) await writeFile(process.env.MEA_MCP_OUT, JSON.stringify(summary, null, 2) + '\n', {flag: 'wx'});
console.log(JSON.stringify(summary));
