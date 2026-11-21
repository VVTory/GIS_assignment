// Browser acceptance driven by the installed catalog, rather than old sample dates.
const { chromium } = require('playwright');
const { selectCalendarDate, getSelectedId } = require('./browser-helpers.cjs');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), reports = path.join(root, 'reports');
const base = process.env.WEBGIS_BASE_URL || 'http://127.0.0.1:5175/';

async function main() {
  const browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromium.executablePath()) ? {} : { channel: 'msedge' }) });
  try {
    const page = await browser.newPage({ viewport: { width: 1540, height: 1080 }, deviceScaleFactor: 1 });
    page.setDefaultTimeout(60000);
    const errors = [], failures = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => { if (response.url().includes('/api/') && response.status() >= 400) failures.push({ url: response.url(), status: response.status() }); });
    assert.equal((await page.goto(base, { waitUntil: 'domcontentloaded' })).status(), 200);
    const catalog = await (await page.request.get(new URL('/api/v1/catalog', base).href)).json();
    const installed = JSON.parse(fs.readFileSync(path.join(root, 'data/sample/catalog.json'), 'utf8'));
    assert.equal(catalog.data_version, installed.data_version);
    assert.equal(catalog.assets.length, installed.assets.length);
    // This acceptance script targets the local Vite development instance. Wait
    // for MapLibre's actual render state; network-idle alone can precede tiles.
    async function waitMapsReady() {
      await page.waitForFunction(() => {
        const state = document.querySelector('#app')?.__vue_app__?._instance?.setupState;
        if (!state || state.coverageLoading) return false;
        const maps = state.comparing ? [state.map, state.otherMap] : [state.map];
        return maps.every(map => map && map.getSource('published') && map.loaded() && map.areTilesLoaded());
      });
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    }
    await page.waitForFunction(() => document.querySelector('.coverage-number')?.textContent.includes('%'));
    assert.equal(await page.locator('canvas').count(), 1);
    const defaultDate = await getSelectedId(page);
    assert(defaultDate.includes(String(installed.provenance.selected_year)));
    await waitMapsReady();
    await page.screenshot({ path: path.join(reports, '数据更新_NDVI.png'), fullPage: true });

    async function selectVariable(label, asset) {
      await page.getByRole('button', { name: label, exact: true }).click();
      await selectCalendarDate(page, asset.id);
      await page.waitForFunction(() => document.querySelector('.coverage-number')?.textContent.includes('%'));
      await waitMapsReady();
    }
    async function clickCoordinate(lon, lat) {
      const position = await page.evaluate(([longitude, latitude]) => {
        const map = document.querySelector('#app').__vue_app__._instance.setupState.map;
        const point = map.project([longitude, latitude]);
        return { x: point.x, y: point.y };
      }, [lon, lat]);
      await page.locator('.maplibregl-canvas').first().click({ position });
    }

    const smap = catalog.assets.filter(a => a.variable === 'soil_moisture').at(-1);
    assert(smap);
    await selectVariable('SMAP 土壤湿度', smap);
    assert((await page.locator('.grid-note').innerText()).includes(String(smap.grid_spacing_km)));
    const features = (await (await page.request.get(new URL(smap.geojson_url, base).href)).json()).features;
    const grids = (await (await page.request.get(new URL(smap.grid_url, base).href)).json()).features;
    assert.equal(grids.length, features.length);
    assert.equal(grids[0].properties.cell_size_m, smap.cell_size_m);
    await page.getByRole('button', { name: '返回研究区' }).click(); await page.waitForTimeout(600);
    const seriesPending = page.waitForResponse(r => r.url().includes('/api/v1/series?'));
    await clickCoordinate(...features[Math.floor(features.length / 2)].geometry.coordinates);
    const seriesResponse = await seriesPending;
    assert.equal(seriesResponse.status(), 200);
    const points = (await seriesResponse.json()).points;
    const expectedDays = (Date.parse(installed.provenance.acquisition_end) - Date.parse(installed.provenance.acquisition_start)) / 86400000 + 1;
    assert.equal(points.length, expectedDays);
    assert(points.some(p => p.valid));
    assert(points.some(p => !p.valid));
    assert(points.filter(p => !p.valid).every(p => p.value == null));
    await page.waitForFunction(() => document.querySelector('.series-meta')?.textContent.includes('日历日'));
    assert((await page.locator('.series-chart .axis-label').count()) <= 11);
    const downloadPending = page.waitForEvent('download');
    await page.getByRole('button', { name: /^导出 CSV/ }).click();
    const download = await downloadPending;
    const csv = fs.readFileSync(await download.path(), 'utf8');
    assert(csv.includes(catalog.data_version));
    assert.equal(csv.trim().split('\r\n').length, expectedDays + 1);
    await page.screenshot({ path: path.join(reports, '数据更新_SMAP.png'), fullPage: true });

    const temperatures = catalog.assets.filter(a => a.variable === 'lst').sort((a, b) => b.statistics.valid_pixels - a.statistics.valid_pixels);
    assert(temperatures.length >= 2);
    await selectVariable('LST 地表温度', temperatures[0]);
    await page.getByRole('button', { name: '返回研究区' }).click(); await page.waitForTimeout(600);
    await page.getByRole('button', { name: '矩形统计', exact: true }).click();
    const statisticsPending = page.waitForResponse(r => r.url().endsWith('/api/v1/statistics') && r.request().method() === 'POST');
    await clickCoordinate(-98.2, 36.15); await clickCoordinate(-96.9, 37.15);
    const statisticsResponse = await statisticsPending;
    assert.equal(statisticsResponse.status(), 200);
    const statistics = (await statisticsResponse.json()).results[0];
    assert(statistics.valid_pixels > 0);
    assert(statistics.min <= statistics.mean && statistics.mean <= statistics.max);
    await page.waitForFunction(() => document.querySelectorAll('.stats-grid strong').length === 6);
    await page.getByRole('switch', { name: '日期对比' }).click();
    await page.selectOption('#compare-date', temperatures[1].id);
    await page.waitForFunction(() => document.querySelectorAll('canvas').length === 2);
    await waitMapsReady();
    await page.screenshot({ path: path.join(reports, '数据更新_日期对比.png'), fullPage: true });
    assert.deepEqual(errors, []); assert.deepEqual(failures, []);
    const report = { checked_at: new Date().toISOString(), status: 'passed', base, data_version: catalog.data_version,
      asset_count: catalog.assets.length, default_asset: defaultDate, smap_asset: smap.id, smap_grid_km: smap.grid_spacing_km,
      series_calendar_days: points.length, series_valid_days_at_clicked_location: points.filter(p => p.valid).length,
      missing_values_preserved: true, csv_download: download.suggestedFilename(),
      statistics_asset: temperatures[0].id, statistics_valid_pixels: statistics.valid_pixels,
      compared_dates: temperatures.slice(0, 2).map(a => a.date), page_errors: errors, api_failures: failures };
    fs.writeFileSync(path.join(reports, 'data-refresh-20261115-browser.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
