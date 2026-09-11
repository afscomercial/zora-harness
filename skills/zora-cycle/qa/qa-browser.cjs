// Browser network boundary for the QA VM. The runner ships this with every job and
// Codex launches Chromium through it, instead of improvising its own proxy.
//
//   const { launchQaContext } = require(process.env.QA_BROWSER_HELPER);
//   const { browser, context, blockedOrigins } = await launchQaContext();
//
// Two layers:
//   1. Chromium's host resolver refuses every host name except loopback and the approved
//      authentication origins. It sees every hop, redirects and sub-resources included.
//   2. Request interception aborts anything else, which catches IP literals the resolver
//      never looks up. Playwright routes only the first URL of a redirect chain, so a
//      redirect hop to a bare IP from an approved origin is the one gap left open.
// Service workers are blocked, since they would bypass interception.
//
// `node qa-browser.cjs --self-test` proves the boundary holds before Codex starts.
'use strict';
const path = require('path');

const LOOPBACK = ['localhost', '127.0.0.1'];

function approvedOrigins() {
  return (process.env.QA_AUTH_ORIGINS || '')
    .split(',').map(s => s.trim()).filter(Boolean)
    .map(o => { const u = new URL(o); if (u.protocol !== 'https:') throw Error(`approved origin must be https: ${o}`); return u.origin; });
}

function originOf(url) {
  try { return new URL(url).origin; } catch { return '<unparseable>'; }
}

function isAllowed(url, origins) {
  let u;
  try { u = new URL(url); } catch { return false; }
  if (['data:', 'blob:', 'about:'].includes(u.protocol)) return true;
  if (['http:', 'ws:'].includes(u.protocol) && LOOPBACK.includes(u.hostname)) return true;
  return origins.includes(u.origin);
}

function playwright() {
  const work = process.env.QA_WORK || process.cwd();
  const from = [work, path.join(work, 'tests', 'b2b-e2e')];
  return require(require.resolve('@playwright/test', { paths: from }));
}

async function launchQaContext(contextOptions = {}) {
  const { chromium, devices } = playwright();
  const origins = approvedOrigins();
  const hosts = [...LOOPBACK, ...origins.map(o => new URL(o).hostname)];
  const rules = 'MAP * ~NOTFOUND, ' + hosts.map(h => `EXCLUDE ${h}`).join(', ');
  const browser = await chromium.launch({ headless: true, args: [`--host-resolver-rules=${rules}`, '--disable-quic'] });
  // The repo's desktop profile: the default headless user agent gets the link-preview page.
  const context = await browser.newContext({ ...devices['Desktop Chrome'], ...contextOptions, serviceWorkers: 'block' });
  const blocked = new Set();
  await context.route('**/*', route => {
    const url = route.request().url();
    if (isAllowed(url, origins)) return route.continue();
    blocked.add(originOf(url));
    return route.abort('blockedbyclient');
  });
  return { browser, context, approvedOrigins: origins, blockedOrigins: () => [...blocked].sort() };
}

async function selfTest() {
  const { browser, context, approvedOrigins: origins, blockedOrigins } = await launchQaContext();
  const page = await context.newPage();
  const probes = {};
  for (const target of ['https://example.com/', 'https://1.1.1.1/', 'http://169.254.169.254/']) {
    probes[`fetch ${target}`] = await page.evaluate(async t => {
      try { await fetch(t, { mode: 'no-cors', signal: AbortSignal.timeout(8000) }); return 'reached'; }
      catch { return 'blocked'; }
    }, target);
  }
  try { await page.goto('https://example.com/', { timeout: 10000 }); probes['navigate https://example.com/'] = 'reached'; }
  catch { probes['navigate https://example.com/'] = 'blocked'; }
  await browser.close();
  const holds = Object.values(probes).every(v => v === 'blocked');
  console.log(JSON.stringify({ holds, approvedOrigins: origins, probes, blockedOrigins: blockedOrigins() }, null, 2));
  process.exit(holds ? 0 : 1);
}

module.exports = { launchQaContext, isAllowed, approvedOrigins };

if (require.main === module && process.argv.includes('--self-test')) {
  selfTest().catch(e => { console.log(JSON.stringify({ holds: false, error: String(e.message).split('\n')[0] })); process.exit(1); });
}
