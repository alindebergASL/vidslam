// Headless screenshot capture of the polished surfaces.
//
// Usage:
//   1. Boot backend + frontend (uvicorn + next dev), seed demo data, drive a render.
//   2. node scripts/capture_screenshots.mjs
//
// Env vars (all optional):
//   AVS_BASE_URL    frontend URL                  (default http://localhost:3000)
//   AVS_API_URL     backend URL                   (default http://localhost:8000)
//   AVS_PASSWORD    MVP password                  (default test-pw)
//   AVS_OUT_DIR     output directory              (default ./screenshots)
//   AVS_CHROMIUM    path to headless chromium     (default auto-detected)
//
// Cookies are set on the API origin, so AVS_BASE_URL and AVS_API_URL must agree
// with the backend's FRONTEND_ORIGIN/CORS config — same hostname, not 127 vs localhost.

import { mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

const BASE = process.env.AVS_BASE_URL || 'http://localhost:3000';
const API = process.env.AVS_API_URL || 'http://localhost:8000';
const PW = process.env.AVS_PASSWORD || 'test-pw';
const OUT = process.env.AVS_OUT_DIR || './screenshots';
mkdirSync(OUT, { recursive: true });

// Resolve playwright from either the global install (Claude Code remote env)
// or the local node_modules.
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  const pw = require('/opt/node22/lib/node_modules/playwright');
  chromium = pw.chromium;
}

const browser = await chromium.launch({
  executablePath: process.env.AVS_CHROMIUM || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});

async function login(page) {
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
  await page.fill('#avs-password', PW);
  await page.click('button[type=submit]');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(400);
}

async function shot(page, file) {
  await page.screenshot({ path: `${OUT}/${file}` });
  console.log('shot', file);
}

// 1. Login screen — fresh context, no cookies.
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(400);
  await shot(page, '01-login.png');
  await ctx.close();
}

// 2-9. Authenticated walkthrough on a single context.
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();
await login(page);

const desktop = [
  ['/',                       '02-dashboard.png',      900],
  ['/cast',                   '03-cast.png',           900],
  ['/studio',                 '04-studio.png',         900],
  ['/projects',               '05-projects.png',       900],
  ['/projects/1',             '06-project-editor.png', 1100],
  ['/projects/1/render?id=1', '07-render.png',         1000],
  ['/brand',                  '08-brand.png',          900],
  ['/settings',               '09-settings.png',       900],
];
for (const [path, file, h] of desktop) {
  await page.setViewportSize({ width: 1280, height: h });
  await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(700);
  await shot(page, file);
}

// 10-12. Mobile (drawer closed + open) and the public /share/<token> page.
{
  const mctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    storageState: await ctx.storageState(),
  });
  const mpage = await mctx.newPage();
  await mpage.goto(`${BASE}/`, { waitUntil: 'networkidle' });
  await mpage.waitForTimeout(500);
  await shot(mpage, '10-mobile-dashboard.png');

  const ham = await mpage.$(
    'button[aria-label*="navigation" i], button[aria-label*="menu" i], button[aria-label*="open" i]',
  );
  if (ham) {
    await ham.click();
    await mpage.waitForTimeout(350);
    await shot(mpage, '12-mobile-nav-drawer.png');
  }
  await mctx.close();
}

// Public share page — needs the share_token from /api/renders/1.
{
  const cookies = (await ctx.cookies()).map((c) => `${c.name}=${c.value}`).join('; ');
  const res = await fetch(`${API}/api/renders/1`, { headers: { cookie: cookies } });
  if (res.ok) {
    const { share_token } = await res.json();
    if (share_token) {
      const sctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
      const spage = await sctx.newPage();
      await spage.goto(`${BASE}/share/${share_token}`, { waitUntil: 'networkidle' });
      await spage.waitForTimeout(500);
      await shot(spage, '11-share.png');
      await sctx.close();
    }
  }
}

await browser.close();
console.log('done →', OUT);
