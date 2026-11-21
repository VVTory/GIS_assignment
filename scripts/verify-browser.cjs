// Requires Playwright and Chromium/Edge; run against a started sample instance.
const { chromium } = require('playwright');
const { selectCalendarDate, getSelectedId } = require('./browser-helpers.cjs');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), base = process.env.WEBGIS_BASE_URL || 'http://127.0.0.1:5173';
const reports = path.join(root, 'reports');
async function main() {
  const browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromium.executablePath()) ? {} : { channel: 'msedge' }) });
  try {
    const page = await browser.newPage({ viewport: { width: 1540, height: 1080 }, deviceScaleFactor: 1 });
    const errors = [], failures = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) failures.push(r.url() + ' ' + r.status()); });
    assert.equal((await page.goto(base, { waitUntil: 'networkidle' })).status(), 200);
    await page.waitForFunction(() => document.querySelector('.coverage-number')?.textContent.includes('%'));
    assert.match(await getSelectedId(page), /^ndvi-/);
    const catalog = await (await page.request.get(base + '/api/v1/catalog')).json();
    assert.equal(catalog.assets.length, 18);
    const verification = JSON.parse(fs.readFileSync(path.join(root, 'data/sample/verification.json'), 'utf8'));
    async function resetView() {
      await page.getByRole('button', { name: '返回研究区' }).click();
      await page.waitForTimeout(550);
    }
    async function clickCoordinate(lon, lat) {
      const canvas = page.locator('.maplibregl-canvas').first(), box = await canvas.boundingBox();
      const rad = n => n * Math.PI / 180, my = n => Math.log(Math.tan(Math.PI / 4 + rad(n) / 2));
      const scale = Math.min((box.width - 88) / rad(2), (box.height - 88) / (my(37.5) - my(35.5)));
      await canvas.click({ position: { x: box.width / 2 + rad(lon + 97.5) * scale, y: box.height / 2 - (my(lat) - (my(37.5) + my(35.5)) / 2) * scale } });
    }
    async function selectVariable(label, id) {
      await page.getByRole('button', { name: label, exact: true }).click();
      if (id) await selectCalendarDate(page, id);
      await page.waitForFunction(() => document.querySelector('.coverage-number')?.textContent.includes('%'));
      await page.waitForLoadState('networkidle');
    }
    async function downloadCsv(button) {
      const pending = page.waitForEvent('download'); await button.click();
      const download = await pending, body = fs.readFileSync(await download.path(), 'utf8');
      for (const field of ['unit', 'method_version', 'data_version', 'sample_count', 'exported_at_utc']) assert(body.includes('"' + field + '"'), field);
      assert(body.includes(catalog.data_version));
      return { filename: download.suggestedFilename(), rows: body.trim().split('\r\n').length, body };
    }
    await selectVariable('EF EF 工程基线 派生', 'ef-2019-06-05');
    await resetView();
    const example = verification.example_ef_points.find(p => p.asset_id === 'ef-2019-06-05');
    const pointPending = page.waitForResponse(r => r.url().includes('/ef-2019-06-05/point?'));
    await clickCoordinate(example.lon, example.lat);
    const efPoint = await (await pointPending).json();
    assert.equal(efPoint.valid, true);
    assert(Math.abs(efPoint.value - example.value) < 1e-5, JSON.stringify(efPoint));
    await page.waitForFunction(() => document.querySelector('.series-meta')?.textContent.includes('7 个日历日'));
    assert(await page.locator('.missing-mark').count() >= 4);
    const pointCsv = await downloadCsv(page.getByRole('button', { name: '导出位置查询 CSV' }));
    const seriesCsv = await downloadCsv(page.getByRole('button', { name: /^导出 CSV/ }));
    assert.equal(seriesCsv.rows, 8);
    assert(seriesCsv.body.includes('no_published_asset'));
    await page.getByRole('button', { name: '矩形统计', exact: true }).click();
    const statsPending = page.waitForResponse(r => r.url().endsWith('/api/v1/statistics') && r.request().method() === 'POST');
    await clickCoordinate(-98.2, 36.15); await clickCoordinate(-96.9, 37.15);
    const stats = await (await statsPending).json();
    await page.waitForFunction(() => document.querySelectorAll('.stats-grid strong').length === 6);
    assert(stats.results[0].valid_pixels > 0);
    assert(stats.results[0].min <= stats.results[0].mean && stats.results[0].mean <= stats.results[0].max);
    const statsCsv = await downloadCsv(page.getByRole('button', { name: '导出区域统计 CSV' }));
    assert(statsCsv.body.includes('"geometry"'));
    await selectVariable('NDVI 植被指数', 'ndvi-2019-06-05');
    await page.waitForFunction(() => document.querySelectorAll('.chart-dot').length === 6);
    await page.getByRole('button', { name: '矩形统计', exact: true }).click();
    await clickCoordinate(-98.2, 36.15); await clickCoordinate(-96.9, 37.15);
    await page.waitForFunction(() => document.querySelectorAll('.stats-grid strong').length === 6);
    await page.waitForTimeout(600);
    assert(await page.locator('.maplibregl-canvas').first().evaluate(el => el.width === el.clientWidth && el.height === el.clientHeight));
    await page.screenshot({ path: path.join(reports, 'WebGIS改进_桌面.png'), fullPage: true });
    await selectVariable('EF EF 工程基线 派生', 'ef-2019-06-03');
    await page.waitForFunction(() => document.querySelector('.coverage-card .card-label')?.textContent.includes('低覆盖'));
    assert.equal(await page.locator('.stats-grid').count(), 0);
    await selectVariable('LST 地表温度', 'lst-2019-06-02');
    await page.getByRole('switch', { name: '日期对比' }).click();
    await page.selectOption('#compare-date', 'lst-2019-06-05');
    await page.waitForFunction(() => document.querySelectorAll('canvas').length === 2);
    await page.waitForLoadState('networkidle');
    const scales = [];
    for (const id of ['lst-2019-06-02', 'lst-2019-06-05']) {
      const response = await page.request.get(base + '/api/v1/rasters/' + id + '/render');
      assert.equal(response.status(), 200);
      scales.push([response.headers()['x-render-min'], response.headers()['x-render-max']]);
    }
    assert.deepEqual(scales[0], scales[1]);
    await page.locator('#opacity').fill('50');
    await page.screenshot({ path: path.join(reports, 'WebGIS改进_日期对比.png'), fullPage: true });
    await page.getByRole('switch', { name: '日期对比' }).click();
    await selectVariable('SMAP 土壤湿度', 'smap-am-2019-06-01');
    await resetView();
    const smap = await (await page.request.get(base + '/api/v1/vectors/smap-am-2019-06-01')).json();
    const [lon, lat] = smap.features[0].geometry.coordinates;
    const smapPending = page.waitForResponse(r => r.url().includes('/smap-am-2019-06-01/point?'));
    await clickCoordinate(lon, lat);
    const smapPoint = await (await smapPending).json();
    assert.equal(smapPoint.value, smap.features[0].properties.soil_moisture);
    await page.waitForFunction(() => document.querySelector('.details-list')?.textContent.includes('QA = 0'));
    const file = await page.request.get(base + '/api/v1/assets/smap-am-2019-06-01/download');
    assert.equal((await file.json()).features.length, smap.features.length);
    for (const label of ['NDVI 植被指数', 'LST 地表温度', 'NDVI 植被指数']) await page.getByRole('button', { name: label, exact: true }).click();
    await page.waitForLoadState('networkidle');
    assert.match(await getSelectedId(page), /^ndvi-/);
    await page.waitForFunction(() => document.querySelector('.point-value-card .card-label')?.textContent.includes('植被指数'));
    await page.getByRole('button', { name: '数据与方法' }).click();
    await page.getByRole('dialog').waitFor();
    assert(await page.getByRole('button', { name: '关闭来源说明' }).evaluate(el => el === document.activeElement));
    await page.keyboard.press('Shift+Tab');
    assert(await page.getByRole('link', { name: '接口说明' }).evaluate(el => el === document.activeElement));
    await page.keyboard.press('Tab');
    assert(await page.getByRole('button', { name: '关闭来源说明' }).evaluate(el => el === document.activeElement));
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert((await page.locator('.map').first().boundingBox()).height >= 400);
    await page.screenshot({ path: path.join(reports, 'WebGIS改进_移动端.png'), fullPage: true });
    await page.getByRole('switch', { name: '日期对比' }).click();
    await page.waitForTimeout(400);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    for (const scenario of ['empty', 'error']) {
      const testPage = await browser.newPage();
      await testPage.route('**/api/v1/catalog', route => route.fulfill({ status: scenario === 'error' ? 503 : 200, contentType: 'application/json', body: JSON.stringify(scenario === 'error' ? { detail: '测试：目录暂时不可用' } : { assets: [], status: 'empty' }) }));
      await testPage.goto(base, { waitUntil: 'networkidle' });
      await testPage.getByText(scenario === 'error' ? '测试：目录暂时不可用' : '暂无已发布数据，请先导入真实处理结果。', { exact: false }).waitFor();
      assert.equal(await testPage.locator('.point-value-card').count(), 0);
      await testPage.close();
    }
    assert.deepEqual(errors, []); assert.deepEqual(failures, []);
    const summary = { status: 'passed', base_url: base, published_assets: catalog.assets.length, ef_point: efPoint, smap_point: smapPoint, statistics: stats, exports: [pointCsv, seriesCsv, statsCsv].map(({ body, ...info }) => info), shared_lst_scale: scales[0], browser_errors: errors, api_failures: failures, responsive_width: 390, checked_states: ['empty_catalog', 'unavailable_catalog', 'low_coverage', 'calendar_gaps', 'rapid_switching'] };
    fs.writeFileSync(path.join(reports, 'browser-verification.json'), JSON.stringify(summary, null, 2));
    process.stdout.write(JSON.stringify(summary, null, 2));
  } finally { await browser.close(); }
}
main().catch(e => { process.stderr.write(e.stack + '\n'); process.exitCode = 1; });
