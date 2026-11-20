// From the checkout: npm ci --ignore-scripts && npx --no-install playwright install chromium
// With FFmpeg, ffprobe and libass installed, run:
//   node docs/demo/showcase/record.mjs OUTPUT_DIRECTORY
import assert from 'node:assert/strict';
import {execFileSync, spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';
import {chromium, expect} from '@playwright/test';

assert.equal(process.argv.length, 3, 'Pass an output directory');
const output = path.resolve(process.argv[2]);
const root = fileURLToPath(new URL('../../../', import.meta.url));
const desktop = {width: 1440, height: 960};
const work = await mkdtemp(path.join(tmpdir(), 'mea-readme-showcase-'));
const server = spawn('python3', ['-B', '-m', 'mea', 'serve', '--port', '0',
  '--task-dir', path.join(work, 'tasks')], {
  cwd: root, env: {PATH: process.env.PATH, LANG: 'C.UTF-8'},
  stdio: ['ignore', 'pipe', 'inherit'],
});
const done = once(server, 'exit');
const attemptedOutsideLoopback = [];
let browser;

const button = (scope, name) => scope.getByRole('button', {name, exact: true});
const row = (page, pr) => page.locator('.pr-row').filter({has: page.getByText('#' + pr, {exact: true})});
async function shot(page, name) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await page.screenshot({path: path.join(output, name + '.png')});
}
const sleep = seconds => new Promise(resolve => setTimeout(resolve, seconds * 1000));

async function openContext(options, origin) {
  const context = await browser.newContext(options);
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin === origin) return route.continue();
    attemptedOutsideLoopback.push(url.origin);
    return route.abort();
  });
  // The authored fixture stays in its original language when UI labels switch.
  await context.addInitScript(() => localStorage.setItem('mea.language', 'en'));
  return context;
}

function timestamp(seconds) {
  const ms = Math.round(seconds * 1000);
  return new Date(ms).toISOString().slice(11, 23).replace('.', ',');
}

async function showStop(page) {
  await row(page, 252).click();
  await expect(page.locator('.title-row .status')).toHaveText('Unable to complete review');
  await expect(page.locator('.stop-reason')).toContainText('Missing files · services/dispatch/worker.py, deploy/dispatch/values.yaml');
  await expect(page.locator('.stop-reason')).toContainText('Capture the complete changed-file list and source for this frozen version');
  await expect(page.locator('#review-actions')).toBeHidden();
}

try {
  let origin;
  const startup = setTimeout(() => server.kill(), 10000);
  try {
    for await (const line of createInterface({input: server.stdout})) {
      const ready = line.match(/^MEA OFFLINE (http:\/\/127\.0\.0\.1:\d+)$/);
      if (ready) {
        origin = ready[1];
        break;
      }
    }
    assert.ok(origin, 'The isolated demo server did not start');
  } finally {
    clearTimeout(startup);
  }
  await mkdir(output, {recursive: true});
  browser = await chromium.launch({executablePath: process.env.MEA_BROWSER || undefined,
    args: ['--disable-background-networking', '--no-proxy-server']});
  const desktopContext = await openContext({viewport: desktop}, origin);
  const page = await desktopContext.newPage();
  await page.goto(origin);
  await button(page, 'Open local demo').click();
  await expect(page.locator('.pr-identity')).toContainText('#248');
  await expect(page.locator('.finding[aria-pressed="true"]')).toBeVisible();
  await expect(page.getByText('Demo data', {exact: true})).toBeVisible();
  const reviewUrl = page.url();
  await shot(page, 'risk-inbox');

  await button(page, 'Full diff ↗').click();
  const diff = page.getByRole('dialog', {name: 'Unified diff'});
  await expect(diff.locator('[data-located]')).toHaveCount(1);
  await shot(page, 'frozen-diff');
  await page.keyboard.press('Escape');

  await showStop(page);
  await shot(page, 'review-stopped');
  await row(page, 246).click();
  await expect(page.locator('.pr-identity')).toContainText('#246');
  const inboxUrl = page.url();
  await desktopContext.close();

  const phoneContext = await openContext({viewport: {width: 390, height: 844}, isMobile: true,
    deviceScaleFactor: 1, hasTouch: true}, origin);
  const phone = await phoneContext.newPage();
  await phone.goto(reviewUrl);
  await expect(phone.locator('.pr-identity')).toContainText('#248');
  await shot(phone, 'mobile-review');
  await button(phone, 'Full diff ↗').click();
  await expect(phone.getByRole('dialog', {name: 'Unified diff'}).locator('[data-located]')).toHaveCount(1);
  await shot(phone, 'mobile-diff');
  await phoneContext.close();

  const recording = await openContext({viewport: desktop, recordVideo: {dir: work, size: desktop}}, origin);
  const tour = await recording.newPage();
  const posts = [];
  tour.on('request', request => {
    if (request.method() === 'POST') posts.push(new URL(request.url()).pathname);
  });
  await tour.goto(inboxUrl);
  await expect(tour.locator('.pr-identity')).toContainText('#246');
  await expect(row(tour, 248)).toBeVisible();
  await tour.evaluate(() => document.fonts.ready);

  const cues = [];
  const start = performance.now();
  async function caption(message, seconds) {
    cues.push({start: (performance.now() - start) / 1000, message});
    await sleep(seconds);
  }

  await caption('The PR Inbox shows risk and review status. Open high-risk PR #248 to begin.', 7);
  await row(tour, 248).click();
  await expect(tour.locator('.pr-identity')).toContainText('#248');
  await expect(tour.locator('.finding[aria-pressed="true"]')).toContainText('endpoint');
  await caption('The highlighted finding says startup can fail when the endpoint is missing.', 8);
  await button(tour, 'Full diff ↗').click();
  const tourDiff = tour.getByRole('dialog', {name: 'Unified diff'});
  await expect(tourDiff.locator('[data-located]')).toContainText('config.get("endpoint")');
  await caption('The selected line in the frozen diff shows where the default endpoint was removed.', 9);
  await button(tourDiff, 'Close').click();
  await tour.locator('.evidence-item').first().click();
  const evidence = tour.getByRole('dialog');
  await expect(evidence).toContainText(/config\/dispatch\.toml:\d.*After change.*[a-f0-9]{40}/);
  await expect(evidence.getByRole('region')).toContainText('timeout_ms');
  await caption('The cited config supplies a timeout and no endpoint. Its path, lines, side and revision identify the evidence.', 10);
  await button(evidence, 'Close').click();
  await button(tour, 'Record judgment').click();
  const judgment = tour.getByRole('dialog', {name: 'Record judgment'});
  await expect(judgment).toContainText('Your judgment is saved in this workspace.');
  await caption('After checking the diff and evidence, record a judgment on this finding.', 8);
  await button(judgment, 'Accept finding').click();
  await expect(tour.locator('#review-actions .action-caption')).toHaveText('Finding accepted');
  await caption('The finding is accepted in this workspace. GitHub writeback is disabled in this demo.', 8);
  await showStop(tour);
  await caption('STOP on PR #252. Two files are missing from the captured changes.', 9);
  await caption('Next step: capture the complete file list and source for this revision, then import the new snapshot.', 11);

  const duration = (performance.now() - start) / 1000;
  await recording.close();
  assert.deepEqual(posts, ['/api/human/disposition']);
  assert.deepEqual(attemptedOutsideLoopback, []);

  const raw = await tour.video().path();
  const videoDuration = Number(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration',
    '-of', 'default=noprint_wrappers=1:nokey=1', raw], {encoding: 'utf8', timeout: 30000}));
  const subtitles = cues.map((cue, index) => [
    index + 1,
    timestamp(cue.start) + ' --> ' + timestamp(cues[index + 1]?.start ?? duration),
    'SYNTHETIC OFFLINE DEMO  |  ' + cue.message,
  ].join('\n')).join('\n\n') + '\n';
  await writeFile(path.join(work, 'captions.srt'), subtitles);
  const style = 'FontName=DejaVu Sans,FontSize=8,PrimaryColour=&H00FFFFFF,Outline=0,Shadow=0,MarginV=6';
  execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-n',
    '-ss', Math.max(0, videoDuration - duration).toFixed(3), '-i', raw, '-t', duration.toFixed(3),
    '-vf', "pad=iw:ih+92:0:0:0x203047,subtitles=captions.srt:force_style='" + style + "'",
    '-an', '-c:v', 'libx264', '-crf', '21', '-preset', 'fast', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
    path.join(output, 'walkthrough.mp4')], {cwd: work, stdio: 'inherit', timeout: 120000});
  await writeFile(path.join(output, 'walkthrough.srt'), subtitles);
  console.log(JSON.stringify({origin, duration, posts, attemptedOutsideLoopback}, null, 2));
} finally {
  await browser?.close();
  server.kill('SIGINT');
  await done;
  await rm(work, {recursive: true, force: true});
}
