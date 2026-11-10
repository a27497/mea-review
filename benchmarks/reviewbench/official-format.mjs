// Load only the pinned, dependency-free ReviewBench input reader.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';

const [loaderPath, expectedPath] = process.argv.slice(2);
const {loadCandidateFindings, candidateIdentityProblem} =
  await import(pathToFileURL(loaderPath).href);
const expected = JSON.parse(readFileSync(expectedPath, 'utf8'));
assert.ok(expected.length > 0);
const reports = [];
for (const row of expected) {
  // Read each arm's file separately. ReviewBench merges same-PR files in a directory.
  const {byPR, stats} = loadCandidateFindings(row.file);
  assert.equal(stats.files_loaded, 1);
  assert.equal(stats.skipped_invalid_files + stats.skipped_malformed_files, 0);
  assert.equal(byPR.size, 1);
  const item = [...byPR.values()][0];
  assert.equal(candidateIdentityProblem(item.pr, row.pr), null);
  assert.equal(item.findings.length, row.count);
  const otherHead = row.pr.head === '0'.repeat(40) ? '1'.repeat(40) : '0'.repeat(40);
  assert.match(candidateIdentityProblem(item.pr, {...row.pr, head: otherHead}), /head/);
  reports.push({file: row.file, findings: item.findings.length});
}
console.log(JSON.stringify({mode: 'OFFICIAL_INPUT_FORMAT_ONLY', reports}));
