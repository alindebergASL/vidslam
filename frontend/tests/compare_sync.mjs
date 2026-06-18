// UI test for the render-comparison side-by-side player.
//
// Asserts:
//   1. ?compare=<id> switches the page to the two-video layout
//   2. The "Linked playback" checkbox is on by default
//   3. Mirroring works: setting currentTime on one video and dispatching
//      'seeked' propagates to the other (within tolerance)
//   4. Unlinking via the checkbox stops further mirroring
//   5. The version chips reflect the comparison (current / compare badge)
//
// We don't rely on the videos actually decoding — headless chromium-shell
// lacks H.264. Instead we drive currentTime + 'seeked' events directly,
// which is exactly what the mirror code in render/page.tsx hooks.
//
// Usage:
//   node frontend/tests/compare_sync.mjs
// Env:
//   AVS_BASE_URL  (default http://localhost:3000)
//   AVS_PASSWORD  (default test-pw)
//   AVS_CHROMIUM  (default playwright's bundled chromium)
//
// Exits non-zero on any failure.

import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

const BASE = process.env.AVS_BASE_URL || 'http://localhost:3000';
const API_BASE = process.env.AVS_API_BASE || process.env.NEXT_PUBLIC_API_BASE || BASE.replace(/:\d+$/, ':8000');
const PW = process.env.AVS_PASSWORD || 'test-pw';
const PROJECT_ID = process.env.AVS_PROJECT_ID || '1';

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  ({ chromium } = require('/opt/node22/lib/node_modules/playwright'));
}

const fail = (msg) => {
  console.error('FAIL:', msg);
  process.exit(1);
};
const ok = (msg) => console.log('ok:', msg);

const browser = await chromium.launch({
  executablePath: process.env.AVS_CHROMIUM || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } });
const page = await ctx.newPage();

// Login
await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
await page.fill('#avs-password', PW);
await page.click('button[type=submit]');
await page.waitForLoadState('networkidle');

// Resolve a pair of completed render ids by going through the same API the
// frontend uses — login already issued an HttpOnly cookie on the backend
// origin, so we read it from the browser context and forward it on the
// node-side fetch (page.evaluate's fetch is blocked by CORS for cross-origin
// credentials in some setups; the node fetch is straightforward).
// Settle anything the login flow kicked off (auth/status, providers, etc.)
// before reading cookies — otherwise we can race ahead of the Set-Cookie.
await page.waitForTimeout(500);
const cookies = await ctx.cookies();
const cookieHeader = cookies
  .filter((c) => c.name === 'avs_session')
  .map((c) => `${c.name}=${c.value}`)
  .join('; ');
if (!cookieHeader) fail('login did not set avs_session cookie — backend reachable?');
const rendersResp = await fetch(`${API_BASE}/api/projects/${PROJECT_ID}/renders`, {
  headers: { cookie: cookieHeader },
});
if (!rendersResp.ok) {
  fail(`/renders responded ${rendersResp.status}: ${(await rendersResp.text()).slice(0, 200)}`);
}
const rs = await rendersResp.json();
const done = rs.filter((x) => x.status === 'completed');
const ids = done.length >= 2 ? [done[0].id, done[1].id] : null;
if (!ids) fail('need at least 2 completed renders for this project — run a render twice and retry');
const [latestId, otherId] = ids;
ok(`comparing v(latest)=${latestId} vs v(other)=${otherId}`);

// Comparison view
await page.goto(`${BASE}/projects/${PROJECT_ID}/render?id=${latestId}&compare=${otherId}`, {
  waitUntil: 'networkidle',
});
await page.waitForTimeout(800);

// 1. Two videos present
const count = await page.locator('video').count();
if (count !== 2) fail(`expected exactly 2 <video> elements, got ${count}`);
ok('two videos rendered side-by-side');

// 2. Linked checkbox checked by default
const linkedChecked = await page.locator('input[type=checkbox]:near(:text("Linked playback"))').first().isChecked();
if (!linkedChecked) fail('"Linked playback" checkbox should be checked by default');
ok('linked playback default-on');

// 3. Mirror on seeked
const drift1 = await page.evaluate(() => {
  const [a, b] = Array.from(document.querySelectorAll('video'));
  a.currentTime = 4.0;
  a.dispatchEvent(new Event('seeked'));
  return { a: a.currentTime, b: b.currentTime };
});
if (Math.abs(drift1.b - 4.0) > 0.2) {
  fail(`seek mirror failed: left=${drift1.a} right=${drift1.b}`);
}
ok(`seek mirrored left→right (left=${drift1.a}, right=${drift1.b})`);

// 4. Mirror is bidirectional
const drift2 = await page.evaluate(() => {
  const [a, b] = Array.from(document.querySelectorAll('video'));
  b.currentTime = 7.5;
  b.dispatchEvent(new Event('seeked'));
  return { a: a.currentTime, b: b.currentTime };
});
if (Math.abs(drift2.a - 7.5) > 0.2) {
  fail(`reverse seek mirror failed: left=${drift2.a} right=${drift2.b}`);
}
ok(`seek mirrored right→left (left=${drift2.a}, right=${drift2.b})`);

// 5. Unlink stops mirroring
await page.locator('input[type=checkbox]:near(:text("Linked playback"))').first().uncheck();
await page.waitForTimeout(200);
const drift3 = await page.evaluate(() => {
  const [a, b] = Array.from(document.querySelectorAll('video'));
  const initialB = b.currentTime;
  a.currentTime = 1.0;
  a.dispatchEvent(new Event('seeked'));
  return { a: a.currentTime, b: b.currentTime, initialB };
});
if (Math.abs(drift3.b - drift3.initialB) > 0.2) {
  fail(`unlinked mode should NOT mirror, but b moved from ${drift3.initialB} to ${drift3.b}`);
}
ok('unlink stops mirroring');

// 6. Version chip reflects the compare badge — the chip pointing at the
// comparison render should contain a small "compare" label.
const compareBadgeCount = await page.locator('a.chip >> text=compare').count();
if (compareBadgeCount < 1) fail('expected a "compare" label on the comparison-version chip');
ok('comparison version chip shows compare badge');

await browser.close();
console.log('\nok: render comparison sync test passed');
