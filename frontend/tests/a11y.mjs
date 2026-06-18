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
  '/studio',
  '/projects',
  `/projects/${PID}`,
  `/projects/${PID}/render?id=1`,
  '/brand',
  '/settings',
];

const browser = await chromium.launch({
  executablePath: process.env.AVS_CHROMIUM || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();

await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
await page.fill('#avs-password', PW);
await page.click('button[type=submit]');
await page.waitForLoadState('networkidle');

const allViolations = [];
let blocking = 0;

for (const route of ROUTES) {
  await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle' });
  // Let any in-flight skeletons / fetches settle before the scan; otherwise
  // axe sees an aria-busy loading region that's about to disappear.
  await page.waitForTimeout(800);

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
  `\n${blocking === 0 ? 'ok' : 'FAIL'}: ${ROUTES.length} routes scanned, ` +
    `${allViolations.length} total findings (${blocking} serious/critical)`
);
process.exit(blocking === 0 ? 0 : 1);
