// Renders every built email in headless Chrome at three widths, in light and dark mode, and checks it.
//   npm install            (installs playwright-core; uses the Chrome you already have, no browser download)
//   python3 scripts/build.py && node scripts/render-check.cjs
// Writes screenshots to out/screenshots/. Exits 1 if a check fails.
// This is Chrome's engine only. It says nothing about Outlook, Gmail, Yahoo or Apple Mail.
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright-core');

const root = path.resolve(__dirname, '..');
const widths = [600, 375, 320];
const schemes = ['light', 'dark'];
let failures = 0;
const fail = (msg) => { failures += 1; console.log('FAIL ' + msg); };

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  fs.mkdirSync(path.join(root, 'out', 'screenshots'), { recursive: true });
  for (const variant of fs.readdirSync(path.join(root, 'out')).filter((d) => ['full', 'minimal'].includes(d))) {
    for (const file of fs.readdirSync(path.join(root, 'out', variant)).filter((f) => f.endsWith('.html'))) {
      for (const scheme of schemes) {
        for (const width of widths) {
          const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1 });
          const page = await ctx.newPage();
          await page.goto('file://' + path.join(root, 'out', variant, file));
          await page.waitForLoadState('load');
          const m = await page.evaluate(() => {
            const card = document.querySelector('td.dm-card');
            const h1 = document.querySelector('h1');
            const btn = document.querySelector('a.btn-link');
            const r = btn.getBoundingClientRect();
            const imgs = [...document.images];
            return {
              scrollW: document.documentElement.scrollWidth,
              clientW: document.documentElement.clientWidth,
              cardBg: getComputedStyle(card).backgroundColor,
              pageBg: getComputedStyle(document.body).backgroundColor,
              h1Color: getComputedStyle(h1).color,
              btnW: Math.round(r.width), btnH: Math.round(r.height),
              btnColor: getComputedStyle(btn).color,
              imagesLoaded: imgs.every((i) => i.complete && i.naturalWidth > 0),
              smallestFont: Math.min(...[...document.querySelectorAll('td, a, span, h1')].filter((e) => e.childNodes.length && [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())).map((e) => parseFloat(getComputedStyle(e).fontSize))),
              height: document.documentElement.scrollHeight,
            };
          });
          const tag = `${variant}/${file} ${scheme} ${width}px`;
          if (m.scrollW > m.clientW) fail(`${tag}: horizontal overflow ${m.scrollW} > ${m.clientW}`);
          if (!m.imagesLoaded) fail(`${tag}: an image did not load`);
          if (m.btnH < 44) fail(`${tag}: button height ${m.btnH}px is under 44px`);
          if (m.smallestFont < 13) fail(`${tag}: text smaller than 13px (${m.smallestFont})`);
          if (scheme === 'dark' && m.cardBg !== 'rgb(30, 33, 40)') fail(`${tag}: dark card background not applied (${m.cardBg})`);
          if (scheme === 'light' && m.cardBg !== 'rgb(255, 255, 255)') fail(`${tag}: light card background wrong (${m.cardBg})`);
          if (variant === 'full' && scheme === 'light' && width === 600) console.log(`ok   ${tag}: height ${m.height}px, button ${m.btnW}x${m.btnH}, smallest text ${m.smallestFont}px`);
          if (variant === 'full') {
            await page.screenshot({ path: path.join(root, 'out', 'screenshots', `${file.replace('.html', '')}-${scheme}-${width}.png`), fullPage: true });
          }
          await ctx.close();
        }
      }
    }
  }
  await browser.close();
  console.log(failures ? `${failures} check(s) failed` : 'all render checks passed');
  process.exit(failures ? 1 : 0);
})();
