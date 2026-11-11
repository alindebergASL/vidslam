// Axe-core accessibility audit across the main routes.
//
// Runs the WCAG2A + WCAG2AA + best-practice rule set on each page after
// login. Prints the full violation list for any failures and exits non-zero
// if any *serious* or *critical* violation is found. Moderate / minor
// findings print as warnings but don't fail — they're worth fixing but
// shouldn't block deploy.
//
// Usage:  node frontend/tests/a11y.mjs
// Env:
//   AVS_BASE_URL  (default http://localhost:3000)
//   AVS_PASSWORD  (default test-pw)
//   AVS_CHROMIUM  (default playwright's bundled chromium)
//   AVS_PROJECT_ID (default 1)

import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

const BASE = process.env.AVS_BASE_URL || 'http://localhost:3000';
const PW = process.env.AVS_PASSWORD || 'test-pw';
const PID = process.env.AVS_PROJECT_ID || '1';
const AMBIENT_ID = process.env.AVS_AMBIENT_ID || '2';

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  ({ chromium } = require('/opt/node22/lib/node_modules/playwright'));
}
let AxeBuilder;
try {
  AxeBuilder = require('@axe-core/playwright').default;
} catch {
  AxeBuilder = require('/tmp/avs-a11y/node_modules/@axe-core/playwright').default;
}

const ROUTES = [
  '/',
  '/cast',
  '/publishing',
  '/results',
  '/studio',
  '/projects',
  `/projects/${PID}`,
  `/projects/${PID}/render?id=1`,
  '/ambient',
  '/ambient/new',
  `/ambient/${AMBIENT_ID}`,
  '/brand',
  '/settings',
];

const browser = await chromium.launch({
  executablePath: process.env.AVS_CHROMIUM || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();

// Signed-in pages keep one live-event stream (SSE) open, so the network is
// never idle: wait for 'load' plus a settle delay instead of 'networkidle'.
await page.goto(`${BASE}/`, { waitUntil: 'load' });
await page.fill('#avs-password', PW);
await page.click('button[type=submit]');
await page.waitForLoadState('load');
await page.waitForTimeout(800);

const allViolations = [];
let blocking = 0;

// The activity panel (bell in the side nav) is a dialog over any page: scan it open.
const PANELS = [
  {
    name: '/ (activity panel)',
    route: '/',
    open: async () => {
      await page.click('aside button[aria-haspopup="dialog"]');
      await page.waitForSelector('[role=dialog]');
      await page.waitForTimeout(800);
    },
  },
];

for (const target of [...ROUTES.map((route) => ({ name: route, route, open: null })), ...PANELS]) {
  const route = target.name;
  await page.goto(`${BASE}${target.route}`, { waitUntil: 'load' });
  // Let any in-flight skeletons / fetches settle before the scan; otherwise
  // axe sees an aria-busy loading region that's about to disappear.
  await page.waitForTimeout(1500);
  if (target.open) await target.open();

  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'best-practice'])
    // Color-contrast on a dark theme often surfaces theoretical findings
    // that look fine to humans; we audit it but don't gate deploys on it.
    .disableRules([])
    .analyze();

  const violations = results.violations;
  if (violations.length === 0) {
    console.log(`ok  ${route}  (0 violations)`);
    continue;
  }
  const blockingHere = violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  blocking += blockingHere.length;
  const tag = blockingHere.length ? 'FAIL' : 'warn';
  console.log(`${tag}  ${route}  (${violations.length} total, ${blockingHere.length} serious/critical)`);
  for (const v of violations) {
    const targets = v.nodes.slice(0, 2).map((n) => n.target.join(' ')).join(' | ');
    console.log(
      `    [${v.impact || 'unknown'}] ${v.id}: ${v.help}\n      → ${targets}`
    );
    allViolations.push({ route, ...v });
  }
}

await browser.close();

console.log(
  `\n${blocking === 0 ? 'ok' : 'FAIL'}: ${ROUTES.length + PANELS.length} pages scanned, ` +
    `${allViolations.length} total findings (${blocking} serious/critical)`
);
process.exit(blocking === 0 ? 0 : 1);
