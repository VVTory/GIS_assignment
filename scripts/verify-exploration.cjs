// Browser acceptance for calendar, map exploration and time-series interactions.
const { chromium } = require('playwright');
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const base = process.env.WEBGIS_BASE_URL || 'http://127.0.0.1:5175/';
const reports = path.resolve(__dirname, '../reports');
async function main() {
  const browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromium.executablePath()) ? {} : { channel: 'msedge' }) });
  try {
    const page = await browser.newPage({ viewport: { width: 1540, height: 1080 }, deviceScaleFactor: 1 });
    page.setDefaultTimeout(60000);
    const errors = [], failures = [], external = [], coverageRequests = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('response', r => { if (r.status() >= 400 && r.url().includes('/api/')) failures.push({ url: r.url(), status: r.status() }); });
    page.on('request', r => { if (r.url().includes('/coverage')) coverageRequests.push(r.url()); });
    await page.route('**/*', route => { const url = new URL(route.request().url()); if (['http:', 'https:'].includes(url.protocol) && !['127.0.0.1', 'localhost'].includes(url.hostname)) { external.push(url.href); return route.abort(); } return route.continue(); });
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    async function ready(calendar = true) {
      await page.waitForFunction(() => { const s = document.querySelector('#app')?.__vue_app__?._instance?.setupState; return s && !s.catalogLoading && s.selectedId && !s.coverageLoading && s.map && s.map.loaded() && s.map.areTilesLoaded(); });
      if (calendar) await page.waitForFunction(() => !document.querySelector('.calendar-status')?.textContent.includes('正在'));
      await page.waitForTimeout(250);
    }
    async function seriesReady() { await page.waitForFunction(() => { const s = document.querySelector('#app').__vue_app__._instance.setupState; return !s.exampleLoading && !s.seriesLoading && !s.pointLoading && s.series.length > 0; }); }
    async function selectedId() { return page.evaluate(() => document.querySelector('#app').__vue_app__._instance.setupState.selectedId); }
    async function selectDate(date) { await page.getByRole('combobox', { name: '浏览月份' }).selectOption(date.slice(0, 7)); await ready(); await page.locator('[data-date="' + date + '"]').click(); await ready(); }
    async function clickCoordinate(lon, lat) { const position = await page.evaluate(([lon, lat]) => { const p = document.querySelector('#app').__vue_app__._instance.setupState.map.project([lon, lat]); return { x: p.x, y: p.y }; }, [lon, lat]); await page.locator('.maplibregl-canvas').first().click({ position }); }
    async function applyRange(start, end) { await page.getByLabel('时序开始日期').fill(start); await page.getByLabel('时序结束日期').fill(end); await page.getByRole('button', { name: '应用时段', exact: true }).click(); await seriesReady(); }
    await ready();
    assert.equal(await page.locator('#asset,.date-filter').count(), 0);
    assert(await page.locator('.maplibregl-ctrl-scale').isVisible());
    assert(await page.locator('.nodata-key').isVisible());
    const initialCoverageRequests = coverageRequests.length;
    assert(initialCoverageRequests >= 28 && initialCoverageRequests <= 34, String(initialCoverageRequests));
    const month = await page.getByRole('combobox', { name: '浏览月份' }).inputValue();
    const day = page.locator('.calendar-day.available').first(), date = await day.getAttribute('data-date');
    const catalog = await (await page.request.get(new URL('/api/v1/catalog', base).href)).json();
    const asset = catalog.assets.find(a => a.variable === 'ndvi' && a.date === date);
    const coverage = await (await page.request.get(new URL('/api/v1/assets/' + asset.id + '/coverage', base).href)).json();
    assert.equal(await day.locator('small').innerText(), (coverage.valid_fraction * 100).toFixed(1) + '%');
    await page.getByRole('button', { name: '下一个有数据的月份' }).click(); await ready();
    assert.notEqual(await page.getByRole('combobox', { name: '浏览月份' }).inputValue(), month);
    await page.getByRole('checkbox', { name: '优先显示高覆盖日期' }).check(); await page.locator('#threshold').selectOption('90');
    const disabledDay = page.locator('.calendar-day.filtered').first();
    if (await disabledDay.count()) { const before = await selectedId(); await disabledDay.dispatchEvent('click'); assert((await page.locator('.date-notice').innerText()).includes('阈值')); assert.equal(await selectedId(), before); }
    await page.getByRole('checkbox', { name: '优先显示高覆盖日期' }).uncheck();
    await page.screenshot({ path: path.join(reports, '探索改进_日历与地图.png'), fullPage: true });
    const originalWidth = (await page.locator('.maps').boundingBox()).width;
    await page.getByRole('button', { name: '收起日期面板' }).click(); await ready(false);
    assert((await page.locator('.maps').boundingBox()).width > originalWidth);
    await page.getByRole('button', { name: '展开变量与日期' }).click();
    await page.getByRole('button', { name: '一键载入 NDVI 示例', exact: true }).click(); await seriesReady(); await ready(false);
    assert((await page.locator('.point-value-card').innerText()).includes('有效值'));
    assert((await page.locator('.example-notice').innerText()).includes('已验证有有效值'));
    assert(await page.locator('.temporal-note').isVisible());
    await applyRange('2025-05-01', '2025-05-07');
    assert((await page.locator('.series-meta').innerText()).includes('7 个日历日'));
    await page.getByLabel('时序开始日期').fill('2025-05-20');
    await page.getByRole('button', { name: '应用时段', exact: true }).click();
    assert((await page.locator('.series-panel .error').innerText()).includes('开始日不晚于结束日'));
    assert((await page.locator('.series-meta').innerText()).includes('7 个日历日'));
    await page.getByLabel('时序开始日期').fill('2025-05-01');
    await page.getByRole('button', { name: '应用时段', exact: true }).click(); await seriesReady();
    await page.locator('.series-chart').focus(); await page.keyboard.press('ArrowRight');
    assert((await page.locator('.chart-tooltip').innerText()).includes('2025-05-02'));
    assert((await page.locator('.chart-tooltip').innerText()).includes('无量纲'));
    const downloadPending = page.waitForEvent('download'); await page.getByRole('button', { name: /^导出 CSV/ }).click();
    const download = await downloadPending, csv = fs.readFileSync(await download.path(), 'utf8');
    assert.equal(csv.trim().split('\r\n').length, 8);
    assert(csv.includes('2025-05-07')); assert(!csv.includes('2025-05-08'));
    assert(csv.includes('composite_period_interpolation_not_daily_observation'));
    await page.screenshot({ path: path.join(reports, '探索改进_时序与示例.png'), fullPage: true });
    await page.getByRole('button', { name: '展开变量与日期' }).click();
    await page.getByRole('button', { name: 'LST 地表温度', exact: true }).click(); await ready(); await seriesReady();
    await applyRange('2025-05-26', '2025-05-28');
    const emptyRange = await page.evaluate(() => document.querySelector('#app').__vue_app__._instance.setupState.series);
    assert.equal(emptyRange.length, 3); assert(emptyRange.every(p => !p.valid && p.value == null)); assert.equal(await page.locator('.chart-line').count(), 0);
    await page.locator('.series-chart').focus(); assert((await page.locator('.chart-tooltip').innerText()).includes('质量筛选'));
    await selectDate('2025-05-29'); assert(await page.locator('.map-coverage-alert').first().isVisible());
    const before = await selectedId(); await page.locator('[data-date="2025-05-26"]').dispatchEvent('click');
    assert((await page.locator('.date-notice').innerText()).includes('质量筛选')); assert.equal(await selectedId(), before);
    await page.getByRole('switch', { name: '日期对比' }).click();
    await page.waitForFunction(() => { const s = document.querySelector('#app').__vue_app__._instance.setupState; return s.otherMap?.loaded() && s.otherMap?.areTilesLoaded() && !s.coverageLoading; });
    assert.equal(await page.locator('.maplibregl-canvas').count(), 2);
    await page.getByRole('switch', { name: '日期对比' }).click();
    await page.getByRole('button', { name: '全部时段', exact: true }).click(); await seriesReady();
    await clickCoordinate(-98.45, 37.45); await seriesReady(); assert(await page.locator('.map-query-alert').isVisible());
    await page.screenshot({ path: path.join(reports, '探索改进_低覆盖与缺测.png'), fullPage: true });
    await page.getByRole('button', { name: 'SMAP 土壤湿度', exact: true }).click(); await ready(); await seriesReady();
    assert((await page.locator('.date-calendar').innerText()).includes('不代表面积覆盖率'));
    await page.getByRole('button', { name: '矩形统计', exact: true }).click(); await clickCoordinate(-98.4, 35.6); await clickCoordinate(-96.6, 37.4);
    await page.waitForSelector('.stats-grid'); assert.equal(await page.locator('.stats-grid strong').count(), 6);
    await page.getByRole('button', { name: '一键载入 NDVI 示例', exact: true }).click(); await seriesReady();
    await page.setViewportSize({ width: 390, height: 844 }); await ready(false);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); assert((await page.locator('.maps').boundingBox()).height >= 420);
    const frame = await page.evaluate(() => { const m = document.querySelector('#app').__vue_app__._instance.setupState.map; const a = m.project([-98.5, 37.5]), b = m.project([-96.5, 35.5]); return { west: a.x, north: a.y, east: b.x, south: b.y, width: m.getContainer().clientWidth, height: m.getContainer().clientHeight }; });
    assert(frame.west >= 20 && frame.north >= 20 && frame.east <= frame.width - 20 && frame.south <= frame.height - 20, JSON.stringify(frame));
    await page.locator('.series-chart').focus(); await page.keyboard.press('ArrowRight');
    assert((await page.locator('.chart-tooltip').innerText()).includes('2025-05-02'));
    for (const button of await page.locator('.map-actions button').all()) assert((await button.boundingBox()).height >= 44);
    await page.screenshot({ path: path.join(reports, '探索改进_移动端.png'), fullPage: true });
    await page.getByRole('button', { name: '展开变量与日期' }).click(); await page.getByRole('button', { name: 'LST 地表温度', exact: true }).click(); await ready(); await seriesReady();
    await selectDate('2025-05-29'); assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); assert((await page.locator('.calendar-day').first().boundingBox()).height >= 44);
    await page.screenshot({ path: path.join(reports, '探索改进_移动端日历.png'), fullPage: true });
    // Deliberate request failures live in a separate page, outside the normal-run error log.
    const failurePage = await browser.newPage();
    await failurePage.route('**/api/v1/assets/*/coverage', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '覆盖率暂不可用' }) }));
    await failurePage.goto(base, { waitUntil: 'domcontentloaded' });
    await failurePage.locator('.date-calendar .error').waitFor();
    assert((await failurePage.locator('.date-calendar .error').innerText()).includes('未视为零'));
    assert.equal(await failurePage.locator('.calendar-day.available small').first().innerText(), '待查');
    await failurePage.unroute('**/api/v1/assets/*/coverage');
    await failurePage.locator('.date-calendar .text-button').click();
    await failurePage.waitForFunction(() => !document.querySelector('.calendar-status')?.textContent.includes('正在'));
    assert.equal(await failurePage.locator('.date-calendar .error').count(), 0);
    await failurePage.close();
    assert.deepEqual(errors, []); assert.deepEqual(failures, []); assert.deepEqual(external, []);
    const result = { status: 'passed', checked_at: new Date().toISOString(), base, release: catalog.data_version, initial_month_requests: initialCoverageRequests, selected_range_csv_rows: 7, empty_range_days: emptyRange.length, responsive_width: 390, page_errors: errors, api_failures: failures, external_requests: external, checked: ['calendar_accurate_coverage', 'month_navigation', 'high_coverage_filter', 'unpublished_day_reason', 'panel_collapse', 'map_scale', 'example_valid_point', 'series_range', 'keyboard_tooltip', 'range_csv', 'all_missing_range', 'low_coverage_warning', 'missing_point_reason', 'date_comparison', 'smap_coverage_semantics', 'region_statistics', 'mobile_touch_targets', 'mobile_study_fit', 'invalid_range_keeps_result', 'coverage_failure_and_retry'] };
    fs.writeFileSync(path.join(reports, 'exploration-20261115-browser.json'), JSON.stringify(result, null, 2)); process.stdout.write(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
}
main().catch(e => { process.stderr.write(e.stack + '\n'); process.exitCode = 1; });
