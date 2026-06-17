// Headless walk through every main UI route after login and assert that:
//   1. Every page returns 200
//   2. The expected sentinel text actually rendered (catches blank/error pages
//      that still HTTP-200 because Next.js can ship an empty shell)
//   3. No console errors or page errors fired during navigation
//
// Usage:
//   node scripts/smoke_ui.mjs
// Env (all optional):
//   AVS_BASE_URL    frontend URL                  (default http://localhost:3000)
//   AVS_PASSWORD    MVP password                  (default test-pw)
//   AVS_CHROMIUM    path to headless chromium     (default playwright default)
//
// Exits non-zero on the first failure so CI can wire it up directly. Designed
// to run against the same seeded demo backend the screenshot script uses.

import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

const BASE = process.env.AVS_BASE_URL || 'http://localhost:3000';
const PW = process.env.AVS_PASSWORD || 'test-pw';

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  ({ chromium } = require('/opt/node22/lib/node_modules/playwright'));
}

const ROUTES = [
  { path: '/',                       expect: /Dashboard/ },
  { path: '/cast',                   expect: /Cast/ },
  { path: '/studio',                 expect: /Studio/ },
  { path: '/projects',               expect: /Projects/ },
  { path: '/projects/1',             expect: /Voice script|Voiceover/i },
  { path: '/projects/1/render?id=1', expect: /Render/ },
  { path: '/brand',                  expect: /Brand Kits/ },
  { path: '/settings',               expect: /Settings/ },
];

const failures = [];
const browser = await chromium.launch({
  executablePath: process.env.AVS_CHROMIUM || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();

// Anything thrown during a page's lifetime is a failure for that page.
const errors = [];
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`);
});

await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
await page.fill('#avs-password', PW);
await page.click('button[type=submit]');
await page.waitForLoadState('networkidle');

for (const { path, expect } of ROUTES) {
  errors.length = 0;
  const start = Date.now();
  const resp = await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle' });
  if (!resp || !resp.ok()) {
    failures.push(`${path}: HTTP ${resp ? resp.status() : 'no response'}`);
    continue;
  }
  await page.waitForTimeout(500);
  const body = await page.content();
  if (!expect.test(body)) {
    failures.push(`${path}: sentinel ${expect} not found`);
    continue;
  }
  if (errors.length) {
    failures.push(`${path}: ${errors.length} runtime error(s): ${errors.slice(0, 3).join(' | ')}`);
    continue;
  }
  console.log(`ok  ${path}  (${Date.now() - start}ms)`);
}

await browser.close();

if (failures.length) {
  console.error(`\nFAIL ${failures.length} of ${ROUTES.length} routes:`);
  for (const f of failures) console.error('  ' + f);
  process.exit(1);
}
console.log(`\nok ${ROUTES.length}/${ROUTES.length} routes`);
