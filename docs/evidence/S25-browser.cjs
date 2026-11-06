const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.IAM_PLAYWRIGHT);

const base = process.env.IAM_PANEL_URL;
const output = process.env.IAM_BROWSER_OUT;
const axeSource = fs.readFileSync(process.env.IAM_AXE, 'utf8');
const routes = ['', 'events', 'state', 'selfmodel', 'memory', 'world', 'requests',
  'capabilities', 'experiments', 'usage', 'backups'];

async function main() {
  const browser = await chromium.launch({ executablePath: process.env.IAM_CHROMIUM });
  const page = await browser.newPage({ reducedMotion: 'reduce' });
  const errors = [];
  const checks = [];
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', error => errors.push(error.message));
  page.on('requestfailed', request => errors.push(request.failure().errorText));
  page.on('response', response => {
    if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
  });
  try {
    for (const [width, height] of [[1366, 900], [390, 844], [320, 720]]) {
      await page.setViewportSize({ width, height });
      for (const route of routes) {
        const response = await page.goto(`${base}/${route}`);
        assert.equal(page.url(), `${base}/${route}`);
        await page.evaluate(axeSource);
        const viewport = await page.evaluate(() => ({
          width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth
        }));
        const h1 = page.getByRole('heading', { level: 1 });
        const headings = await h1.allTextContents();
        const axe = await page.evaluate(async () => {
          const result = await window.axe.run({ runOnly: {
            type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']
          } });
          return result.violations.map(({ id, nodes }) => ({ id, nodes: nodes.map(node => node.target) }));
        });
        checks.push({ route: `/${route}`, viewport, status: response.status(), headings, axe });
        if (['world', 'backups', 'state', 'selfmodel'].includes(route) && width !== 320) {
          await page.screenshot({ path: path.join(output, `${route}-${width}.png`) });
        }
      }
    }
    await page.goto(`${base}/selfmodel`);
    const disclosure = page.locator('summary').filter({ hasText: 'Что со мной происходило' });
    for (let count = 0; count < 30; count++) {
      await page.keyboard.press('Tab');
      if (await disclosure.evaluate(element => document.activeElement === element)) break;
    }
    const focused = await disclosure.evaluate(element => document.activeElement === element);
    const outline = await disclosure.evaluate(element => getComputedStyle(element).outlineStyle);
    await page.keyboard.press('Enter');
    const expanded = await disclosure.evaluate(element => element.parentElement.open);
    await page.getByRole('link', { name: 'Копии и восстановление', exact: true }).click();
    const epochBefore = await page.locator('form[action="/actions/backup"] input[name="epoch"]').inputValue();
    const created = page.getByText('Действие сохранено в журнале.', { exact: true });
    await page.getByRole('button', { name: 'Создать копию', exact: true }).click();
    await created.waitFor();
    const restore = page.locator('form[action="/actions/restore"]').first();
    await restore.getByRole('checkbox', { name: 'Создать отдельную базу из этой копии' }).check();
    await restore.getByRole('button', { name: 'Восстановить в отдельную базу' }).click();
    await page.getByText(/Копия восстановлена в отдельную базу:/).waitFor();
    const epochAfter = await page.locator('form[action="/actions/backup"] input[name="epoch"]').inputValue();

    const report = { browser: browser.version(), fixture: process.env.IAM_FIXTURE || 'empty demo', checks, errors,
      selfmodelKeyboard: { focused, outline, expanded },
      backups: { created: true, restoredSeparately: true, liveEpochUnchanged: epochBefore === epochAfter } };
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify(report, null, 2) + '\n');
    assert.deepEqual(errors, []);
    assert.ok(checks.every(check => check.status === 200 && check.headings.length === 1 &&
      check.viewport.width === check.viewport.scroll && check.axe.length === 0));
    assert.deepEqual(report.selfmodelKeyboard, { focused: true, outline: 'solid', expanded: true });
    assert.ok(report.backups.liveEpochUnchanged);
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
