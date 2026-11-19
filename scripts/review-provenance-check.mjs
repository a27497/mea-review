import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {expect} from '@playwright/test';

export async function checkReviewProvenance(page, base, root, storePath) {
  // Exercise the stored Real task shape offline with evidence produced by the owned demo.
  const fixture = JSON.parse(execFileSync('python3', ['-B', '-c', `
import json
import sys
from mea.contract import canonical, digest
from mea.human_review import demo
from mea.investigation.tasks import Store

store = Store(sys.argv[1])
mock = demo.run(store, 'v1')
mode = 'REAL_PROVIDER_DEEPSEEK'
identity = dict(mock['identity'], mode=mode)
key = digest(identity)
real = dict(mock, id='task-' + key, identity=identity, mode=mode, provider='DEEPSEEK_API',
            source={'kind': 'SYNTHETIC_TEST_TASK'})
with store.transaction() as db:
    db.execute('INSERT INTO tasks VALUES (?, ?, ?)', (real['id'], key, canonical(real).decode()))
print(json.dumps([real['id'], mock['id']]))
`, storePath], {cwd: root, encoding: 'utf8'}));

  const sources = ['Real model (DeepSeek)', 'Deterministic Mock'];
  const modes = ['REAL_PROVIDER_DEEPSEEK', 'MOCK_REVIEW_DEMO_NOT_AI'];
  for (const [index, taskId] of fixture.entries()) {
    await page.goto(base + '/?panel=review&task=' + taskId);
    await page.getByRole('button', {name: 'Open human review', exact: true}).click();
    const header = page.locator('#intro');
    const candidate = page.locator('#review .candidate-provenance');
    for (const section of [header, candidate]) {
      await expect(section.locator('.candidate-source')).toHaveText('Candidate source: ' + sources[index]);
      await expect(section.locator('.writeback-mode')).toHaveText('Writeback: Mock (loopback)');
    }
    const reviewId = new URL(page.url()).searchParams.get('review');
    const response = await page.request.get(base + '/api/human/review/' + reviewId);
    assert.equal(response.status(), 200);
    const review = await response.json();
    assert.equal(review.versions[0].candidate_source, modes[index]);
    assert.equal(review.versions[0].writeback_mode, 'MOCK_GITHUB_NOT_LIVE');
  }
}
