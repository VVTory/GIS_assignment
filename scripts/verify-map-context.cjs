// Exercise the geographic context and layer ordering in the real local app.
const { chromium } = require('playwright');
const { selectCalendarDate, getSelectedId } = require('./browser-helpers.cjs');
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const reports = path.resolve(__dirname, '../reports');
const base = process.env.WEBGIS_BASE_URL || 'http://127.0.0.1:5175/';

async function main() {
  const browser = await chromium.launch({ headless: true, ...(fs.existsSync(chromium.executablePath()) ? {} : { channel: 'msedge' }) });
  try {
    const page = await browser.newPage({ viewport: { width: 1540, height: 1080 }, deviceScaleFactor: 1 });
    page.setDefaultTimeout(60000);
    const errors = [], failures = [], external = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('response', r => { if (r.status() >= 400 && (r.url().includes('/api/') || r.url().includes('/basemap/'))) failures.push({ url: r.url(), status: r.status() }); });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (['http:', 'https:'].includes(url.protocol) && !['127.0.0.1', 'localhost'].includes(url.hostname)) {
        external.push(url.href); return route.abort();
      }
      return route.continue();
    });
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    async function ready() {
      await page.waitForFunction(() => {
        const s = document.querySelector('#app')?.__vue_app__?._instance?.setupState;
        return s && !s.catalogLoading && s.selectedId && !s.coverageLoading && (s.comparing ? [s.map, s.otherMap] : [s.map]).every(m => m && m.loaded() && m.areTilesLoaded() && m.getLayer('published-layer') && m.getLayer('outside-study-mask'));
      });
      await page.waitForTimeout(400);
    }
    async function inspect() {
      return page.evaluate(() => {
        const s = document.querySelector('#app').__vue_app__._instance.setupState;
        return (s.comparing ? [s.map, s.otherMap] : [s.map]).map(m => {
          const layers = m.getStyle().layers.map(l => l.id);
          const at = coordinates => m.queryRenderedFeatures(m.project(coordinates), { layers: ['outside-study-mask'] }).length;
          const northwest = m.project([-98.5, 37.5]), southeast = m.project([-96.5, 35.5]);
          return {
            layers, outsideOpacity: m.getPaintProperty('outside-study-mask', 'fill-opacity'),
            boundaryDash: m.getPaintProperty('study-outline', 'line-dasharray') ?? null,
            boundaryWidth: m.getPaintProperty('study-outline', 'line-width'),
            insideMasked: at([-97.5, 36.5]), outsideMasked: at([-98.65, 36.5]),
            places: m.queryRenderedFeatures({ layers: ['context-place-labels'] }).map(f => f.properties.name),
            states: m.queryRenderedFeatures({ layers: ['context-state-labels'] }).map(f => f.properties.name),
            rivers: m.queryRenderedFeatures({ layers: ['context-rivers'] }).length,
            lakes: m.queryRenderedFeatures({ layers: ['context-lakes'] }).length,
            studyFrame: { northwest: [northwest.x, northwest.y], southeast: [southeast.x, southeast.y], width: m.getContainer().clientWidth, height: m.getContainer().clientHeight },
          };
        });
      });
    }
    function checkOrdering(map) {
      const position = id => map.layers.indexOf(id);
      assert(position('published-layer') < position('context-place-dots'));
      assert(position('context-place-labels') < position('outside-study-mask'));
      assert(position('outside-study-mask') < position('study-halo'));
      assert(position('study-halo') < position('study-outline'));
      assert(position('study-outline') < position('selection-line'));
      assert(position('selection-line') < position('pin'));
      assert.equal(map.boundaryDash, null); assert(map.boundaryWidth <= 1.5);
      assert.equal(map.insideMasked, 0); assert.equal(map.outsideOpacity, .5);
    }
    await ready();
    assert.equal(await page.locator('.map-error,.basemap-warning').count(), 0);
    assert((await page.locator('.map-legend').innerText()).includes('缺测／未通过质量筛选'));
    const normal = (await inspect())[0]; checkOrdering(normal);
    await page.screenshot({ path: path.join(reports, '地图改进_默认视图.png'), fullPage: true });
    const mapBox = await page.locator('.maps').boundingBox(), keyBox = await page.locator('.map-legend').boundingBox();
    await page.screenshot({ path: path.join(reports, '地图改进_效果.png'), clip: { x: mapBox.x, y: mapBox.y, width: mapBox.width, height: keyBox.y + keyBox.height - mapBox.y } });
    assert(normal.outsideMasked > 0); assert(normal.rivers > 0);
    assert(normal.places.length >= 3); assert(normal.states.includes('Kansas'), JSON.stringify(normal)); assert(normal.states.includes('Oklahoma'), JSON.stringify(normal));
    const frame = normal.studyFrame;
    assert(frame.northwest[0] >= 20 && frame.northwest[1] >= 20);
    assert(frame.southeast[0] <= frame.width - 20 && frame.southeast[1] <= frame.height - 20);

    await page.locator('#opacity').evaluate(input => { input.value = '0'; input.dispatchEvent(new Event('input', { bubbles: true })); });
    await ready();
    await page.locator('.maps').screenshot({ path: path.join(reports, '地图改进_底图与蒙版.png') });
    await page.locator('#opacity').evaluate(input => { input.value = '85'; input.dispatchEvent(new Event('input', { bubbles: true })); });
    const catalog = await (await page.request.get(new URL('/api/v1/catalog', base).href)).json();
    const temperatures = catalog.assets.filter(a => a.variable === 'lst').sort((a, b) => a.statistics.valid_pixels - b.statistics.valid_pixels);
    await page.getByRole('button', { name: 'LST 地表温度', exact: true }).click();
    await selectCalendarDate(page, temperatures[0].id); await ready();
    checkOrdering((await inspect())[0]);
    await page.screenshot({ path: path.join(reports, '地图改进_低覆盖日期.png'), fullPage: true });
    await page.getByRole('switch', { name: '日期对比' }).click(); await ready();
    const compared = await inspect(); assert.equal(compared.length, 2); compared.forEach(checkOrdering);
    await page.screenshot({ path: path.join(reports, '地图改进_日期对比.png'), fullPage: true });

    await page.getByRole('switch', { name: '日期对比' }).click();
    await page.setViewportSize({ width: 390, height: 900 });
    await page.getByRole('button', { name: '返回研究区', exact: true }).click(); await ready();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    const mobile = (await inspect())[0], mobileFrame = mobile.studyFrame;
    assert(mobileFrame.northwest[0] >= 20 && mobileFrame.northwest[1] >= 20, JSON.stringify(mobileFrame));
    assert(mobileFrame.southeast[0] <= mobileFrame.width - 20 && mobileFrame.southeast[1] <= mobileFrame.height - 20, JSON.stringify(mobileFrame));
    await page.locator('.center-column').screenshot({ path: path.join(reports, '地图改进_手机视图.png') });
    assert.deepEqual(errors, []); assert.deepEqual(failures, []); assert.deepEqual(external, []);
    const report = { status: 'passed', checked_at: new Date().toISOString(), base,
      default_map: normal, low_coverage_asset: temperatures[0].id, compared_maps: compared.length,
      external_requests: external, page_errors: errors, failed_requests: failures, mobile_study_frame: mobileFrame, mobile_horizontal_overflow: false };
    fs.writeFileSync(path.join(reports, 'map-context-20261115-browser.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally { await browser.close(); }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
