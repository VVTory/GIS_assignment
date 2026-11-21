const { chromium } = require('playwright');
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const { selectCalendarDate, getSelectedId } = require('./browser-helpers.cjs');
const reports = path.resolve(__dirname, '../reports');
async function main() {
  const browser = await chromium.launch({headless:true,...(fs.existsSync(chromium.executablePath())?{}:{channel:'msedge'})});
  try {
    const page = await browser.newPage({viewport:{width:1540,height:1080}});
    page.setDefaultTimeout(60000);
    const errors=[], failures=[], seriesRequests=[];
    page.on('pageerror', e=>errors.push(e.message));
    page.on('response', r=>{if(r.status()>=400 && r.url().includes('/api/')) failures.push([r.status(),r.url()]);});
    page.on('request',r=>{if(r.url().includes('/api/v1/series?'))seriesRequests.push(r.url());});
    await page.goto('http://127.0.0.1:5175/',{waitUntil:'domcontentloaded'});
    async function ready() { await page.waitForFunction(()=>{const s=document.querySelector('#app')?.__vue_app__?._instance?.setupState;return s?.map?.loaded()&&s.map.areTilesLoaded()&&!s.coverageLoading&&!s.qualityLoading&&!s.catalogLoading;}); }
    async function clickPosition(lon,lat) {const position=await page.evaluate(([x,y])=>{const p=document.querySelector('#app').__vue_app__._instance.setupState.map.project([x,y]);return {x:p.x,y:p.y};},[lon,lat]);await page.locator('.maplibregl-canvas').first().click({position});}
    await ready();
    await page.getByRole('button',{name:'LST 地表温度',exact:true}).click();
    await selectCalendarDate(page,'lst-2025-05-01');await ready();
    await page.getByRole('button',{name:'质量与缺测',exact:true}).click();await ready();
    const lst=await page.evaluate(()=>document.querySelector('#app').__vue_app__._instance.setupState.qualityData);
    assert(lst.detailed_qa_available);assert(lst.legend.find(x=>x.code===2).count>0);
    assert.equal(lst.legend.reduce((n,x)=>n+x.count,0),lst.total_pixels);
    assert(await page.evaluate(()=>!!document.querySelector('#app').__vue_app__._instance.setupState.map.getLayer('quality-layer')));
    await clickPosition(-97.5,36.5);
    await page.waitForSelector('.quality-point');
    await page.screenshot({path:path.join(reports,'四项功能-质量诊断.png'),fullPage:true});
    // Each download must be produced by the visible control and contain the rendered map.
    for(const type of ['PNG','PDF']) {
      const pending=page.waitForEvent('download');
      await page.getByRole('button',{name:'导出地图 '+type,exact:true}).click();
      const download=await pending;
      await download.saveAs(path.join(reports,'地图成果-质量诊断.'+type.toLowerCase()));
      await page.waitForFunction(()=>!document.querySelector('#app').__vue_app__._instance.setupState.exportBusy);
      assert((await page.locator('.export-notice').innerText()).includes('已生成'));
    }
    // A date change while export waits must not pair old metadata with a new map.
    await page.evaluate(async()=>{
      const s=document.querySelector('#app').__vue_app__._instance.setupState,m=s.map,loaded=m.loaded;
      m.loaded=()=>false;
      const task=s.downloadMap('png');s.selectedId=s.variableAssets[1].id;
      await new Promise(resolve=>setTimeout(resolve,200));
      m.loaded=loaded;m.fire('idle');await task;
    });
    assert((await page.locator('.export-notice').innerText()).includes('已变化'));
    await selectCalendarDate(page,'lst-2025-05-01');await ready();
    await page.getByRole('button',{name:'同点三变量',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.multi-variable .series-meta').length===3);
    assert.equal(await page.locator('.multi-variable .series-chart').count(),3);
    for(const text of await page.locator('.multi-variable .series-meta').allTextContents())assert(text.includes('123 个日历日'));
    assert((await page.locator('.multi-note').innerText()).includes('分辨率不同'));
    assert((await page.locator('[data-variable="ndvi"] .multi-method').innerText()).includes('16 日合成'));
    const csvPending=page.waitForEvent('download');await page.getByRole('button',{name:'导出三变量 CSV'}).click();
    const csv=await csvPending;await csv.saveAs(path.join(reports,'同点三变量.csv'));
    const rows=fs.readFileSync(path.join(reports,'同点三变量.csv'),'utf8').trim().split('\r\n');assert.equal(rows.length,370);
    const multi=await page.locator('.multi-series').evaluate(el=>el.__vueParentComponent.setupState.rows);
    const missing={};for(const variable of ['lst','ndvi','soil_moisture']){assert.equal(multi[variable].length,123);missing[variable]=multi[variable].filter(p=>!p.valid).length;assert(multi[variable].filter(p=>!p.valid).every(p=>p.value==null));}
    await page.screenshot({path:path.join(reports,'四项功能-同点三变量.png'),fullPage:true});
    const beforeSeries=seriesRequests.length;
    await page.getByRole('button',{name:'质量与缺测',exact:true}).click();
    await page.getByLabel('播放速度').selectOption('900');
    const before=await getSelectedId(page);
    await page.getByRole('button',{name:'按日期播放',exact:true}).click();
    await page.waitForFunction(id=>document.querySelector('#app').__vue_app__._instance.setupState.selectedId!==id,before);
    await page.getByRole('button',{name:'暂停播放',exact:true}).click();await ready();
    const paused=await getSelectedId(page);await page.waitForTimeout(1600);assert.equal(await getSelectedId(page),paused);
    const playbackCoverage=await page.evaluate(()=>{const s=document.querySelector('#app').__vue_app__._instance.setupState;return {date:s.selected.date,fraction:s.coverage.valid_fraction,text:document.querySelector('.playback-status').textContent};});
    assert(playbackCoverage.text.includes(playbackCoverage.date));assert(playbackCoverage.text.includes((playbackCoverage.fraction*100).toFixed(1)+'%'));
    assert(seriesRequests.length-beforeSeries<=1,'playback must not re-query all series each frame');
    // No-loop stops at the last published date; loop wraps to the first.
    await page.evaluate(()=>{const s=document.querySelector('#app').__vue_app__._instance.setupState;s.selectedId=s.variableAssets.at(-2).id;});await ready();
    await page.getByRole('button',{name:'按日期播放',exact:true}).click();
    await page.waitForFunction(()=>{const s=document.querySelector('#app').__vue_app__._instance.setupState;return !s.playing&&s.selectedId===s.variableAssets.at(-1).id;});
    await page.getByRole('checkbox',{name:'循环',exact:true}).check();
    await page.evaluate(()=>{const s=document.querySelector('#app').__vue_app__._instance.setupState;s.selectedId=s.variableAssets.at(-2).id;});await ready();
    await page.getByRole('button',{name:'按日期播放',exact:true}).click();
    await page.waitForFunction(()=>{const s=document.querySelector('#app').__vue_app__._instance.setupState;return s.playing&&s.selectedId===s.variableAssets[0].id;});
    await page.getByRole('button',{name:'暂停播放',exact:true}).click();
    await page.getByRole('button',{name:'NDVI 植被指数',exact:true}).click();await ready();
    await page.getByRole('button',{name:'质量与缺测',exact:true}).click();await ready();
    assert((await page.locator('.quality-panel').innerText()).includes('当天'));
    const ndvi=await page.evaluate(()=>document.querySelector('#app').__vue_app__._instance.setupState.qualityData);assert.equal(ndvi.source_dates.length,2);
    await page.getByRole('button',{name:'SMAP 土壤湿度',exact:true}).click();await selectCalendarDate(page,'soil_moisture-2025-06-01');await ready();
    const smap=await page.evaluate(()=>document.querySelector('#app').__vue_app__._instance.setupState.qualityData);
    assert(smap.geojson.features.length>0);assert.equal(smap.legend.find(x=>x.code===2).count,0);
    await page.setViewportSize({width:390,height:844});await ready();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    for(const button of await page.locator('.map-actions button,.map-export-actions button,.play-button').all())assert((await button.boundingBox()).height>=44);
    await page.screenshot({path:path.join(reports,'四项功能-移动端.png'),fullPage:true});
    assert.deepEqual(errors,[]);assert.deepEqual(failures,[]);
    const result={status:'passed',checked_at:new Date().toISOString(),checks:['quality_native_qa_overlay','quality_point','png_download','pdf_download','export_date_race_rejected','three_variable_charts','shared_123_calendar_days','missing_not_zero','369_row_csv','playback_coverage','pause','stop_at_end','loop','series_cache','ndvi_composite_warning','smap_no_cloud_inference','mobile_no_overflow','44px_touch_targets'],missing_days:missing,lst_quality:lst.legend,smap_quality:smap.legend,page_errors:errors,api_failures:failures};
    fs.writeFileSync(path.join(reports,'demonstration-browser.json'),JSON.stringify(result,null,2));process.stdout.write(JSON.stringify(result,null,2));
  }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
