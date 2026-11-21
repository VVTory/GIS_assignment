<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import maplibregl from 'maplibre-gl'
import DateCalendar from './DateCalendar.vue'
import SeriesChart from './SeriesChart.vue'
import MultiSeries from './MultiSeries.vue'
import { exportMap } from './mapExport'
import { addGeographicContext, contextUrl, dataLayerAnchor, emptyContext, fitStudyArea, studyBounds, type GeographicContext } from './mapContext'
type Asset = { id: string; date: string; variable: string; unit: string; method_version: string; grid_spacing_km?: number; preview_url?: string; tile_url?: string; point_url?: string; geojson_url?: string; grid_url?: string; download_url?: string; bounds?: [number, number, number, number]; image_coordinates?: [[number, number], [number, number], [number, number], [number, number]]; statistics: { valid_pixels: number; min?: number; max?: number } }
type Sample = { date?: string; value?: number | null; valid?: boolean; reason?: string; asset_id?: string; unit?: string; source_pixel?: { row: number; col: number; lon: number; lat: number }; distance_m?: number; matched_coordinates?: number[]; quality_flag?: number }
type Summary = { total_pixels: number | null; valid_pixels: number; valid_fraction: number | null; min?: number | null; max?: number | null; mean?: number | null; std?: number | null; coverage_note?: string }
type Palette = { colors: string[]; label: string; range: number[]; version: string }
type Position = { lon: number; lat: number }
const labels: Record<string, string> = { lst: '地表温度', ndvi: '植被指数', soil_moisture: '土壤湿度', ef: 'EF 工程基线' }
const shortLabels: Record<string, string> = { lst: 'LST', ndvi: 'NDVI', soil_moisture: 'SMAP', ef: 'EF' }
const hints: Record<string, string> = { lst: 'MOD11A1 逐日地表温度，以摄氏度（°C）发布；°C = K − 273.15。下载、查询与统计使用相同单位，缺测不参与转换。', ndvi: 'MOD13A2 合成期线性插值，并非每日独立观测。', soil_moisture: 'SMAP 原生粗网格，查询30公里内最近有效格点；不是1公里土壤湿度。', ef: '经验 TVX 派生工程基线，未经站点精度验证，不用于正式干旱判定。' }
const mapEl = ref<HTMLElement | null>(null), compareEl = ref<HTMLElement | null>(null), scaleEl = ref<HTMLElement | null>(null)
const assets = ref<Asset[]>([]), variable = ref('ndvi'), selectedId = ref(''), compareId = ref('')
const controlsOpen = ref(true), inspectorOpen = ref(false), exampleLoading = ref(false), exampleNotice = ref('')
const processingReport = ref<any>(null)
const playing = ref(false), playbackDelay = ref(1800), playbackLoop = ref(false), playbackNotice = ref('')
const playbackHelpOpen = ref(false)
const exportBusy = ref(false), exportNotice = ref('')
const qualityOpen = ref(false), qualityLoading = ref(false), qualityError = ref(''), qualityData = ref<any>(null), qualityPoint = ref<any>(null)
const multiMode = ref(false)
let playbackTimer: ReturnType<typeof setTimeout> | null = null, qualityController: AbortController | null = null, qualityEpoch = 0
const seriesCache = new Map<string, Sample[]>()
function stopPlayback() { playing.value = false; if (playbackTimer) clearTimeout(playbackTimer); playbackTimer = null }
function schedulePlayback() {
  if (!playing.value) return
  playbackTimer = setTimeout(() => {
    if (!playing.value) return
    if (mapError.value || coverageError.value || qualityError.value) { playbackNotice.value = '加载失败，播放已暂停；重试成功后可继续。'; stopPlayback(); return }
    if (document.hidden || coverageLoading.value || qualityLoading.value || !map?.loaded() || !map.areTilesLoaded()) { schedulePlayback(); return }
    const items = variableAssets.value, index = items.findIndex(a => a.id === selectedId.value)
    if (index >= items.length - 1 && !playbackLoop.value) { stopPlayback(); playbackNotice.value = '已播放至最后一个已发布日期。'; return }
    selectedId.value = items[(index + 1) % items.length].id
    schedulePlayback()
  }, playbackDelay.value)
}
function togglePlayback() {
  if (playing.value) { stopPlayback(); return }
  if (variableAssets.value.length < 2) return
  playbackNotice.value = ''; cancelDrawing()
  if (selectedId.value === variableAssets.value.at(-1)?.id) selectedId.value = variableAssets.value[0].id
  playing.value = true; schedulePlayback()
}
function removeQuality(target: maplibregl.Map | null) {
  if (!target) return
  for (const id of ['quality-layer', 'quality-outline']) if (target.getLayer(id)) target.removeLayer(id)
  if (target.getSource('quality')) target.removeSource('quality')
}
function showQuality() {
  removeQuality(map)
  if (!qualityOpen.value || !qualityData.value || !map?.getSource('pin')) return
  const data = qualityData.value
  if (data.image_url) {
    map.addSource('quality', {type:'image', url: data.image_url, coordinates: data.image_coordinates})
    map.addLayer({id:'quality-layer', type:'raster', source:'quality', paint:{'raster-opacity': .8, 'raster-resampling':'nearest', 'raster-fade-duration': 0}}, dataLayerAnchor)
  } else if (data.geojson) {
    map.addSource('quality', {type:'geojson', data: data.geojson})
    const colors: any[] = ['match', ['get', 'quality_code'], ...data.legend.filter((k: any) => k.code !== 1).flatMap((k: any) => [k.code, k.color]), 'rgba(0,0,0,0)']
    map.addLayer({id:'quality-layer', type:'fill', source:'quality', paint:{'fill-color': colors as any, 'fill-opacity':.7}}, dataLayerAnchor)
  }
}
async function loadQuality() {
  qualityController?.abort(); const controller = new AbortController(); qualityController = controller; const epoch = ++qualityEpoch
  qualityData.value = null; qualityPoint.value = null; qualityError.value = ''; removeQuality(map)
  if (!qualityOpen.value || !selected.value) { qualityLoading.value = false; return }
  qualityLoading.value = true
  try {
    const result = await json('/api/v1/assets/' + encodeURIComponent(selected.value.id) + '/quality', {signal:controller.signal})
    if (epoch !== qualityEpoch || !mounted) return
    qualityData.value = result; showQuality()
    if (pinned.value) await loadQualityPoint()
  } catch (e) { if (!isAbort(e) && epoch === qualityEpoch) qualityError.value = errorText(e) }
  finally { if (epoch === qualityEpoch) qualityLoading.value = false }
}
async function loadQualityPoint() {
  const id = selectedId.value, position = pinned.value
  if (!qualityOpen.value || !position) return
  qualityPoint.value = null
  try {
    const data = await json('/api/v1/assets/' + encodeURIComponent(id) + '/quality/point?lon=' + position.lon + '&lat=' + position.lat, {signal:qualityController?.signal})
    if (qualityOpen.value && selectedId.value === id && pinned.value === position) qualityPoint.value = data
  } catch (e) { if (!isAbort(e) && selectedId.value === id) qualityError.value = errorText(e) }
}
async function downloadMap(type: 'png' | 'pdf') {
  stopPlayback(); exportNotice.value = ''
  if (!map || !selected.value) return
  exportBusy.value = true
  const asset = selected.value, scale = scaleEl.value?.querySelector<HTMLElement>('.maplibregl-ctrl-scale')
  const snapshot = () => JSON.stringify([selectedId.value, qualityOpen.value, opacity.value, release.value,
    pinned.value, selectionGeometry.value, comparing.value, qualityData.value?.asset_id,
    map?.getCenter().toArray(), map?.getZoom(), map?.getBearing(), map?.getPitch(),
    map?.getCanvas().width, map?.getCanvas().height])
  const originalState = snapshot()
  try {
    await exportMap(map, {title:'地表温度、植被与土壤湿度分析', date:asset.date, variable:labels[asset.variable], unit:unit.value,
      source: asset.method_version + (asset.variable === 'lst' ? '；摄氏度发布（°C = K − 273.15）' : asset.variable === 'ndvi' ? '；合成期插值，不是每日独立观测' : asset.variable === 'soil_moisture' ? '；AM 原生粗网格，不代表1公里产品' : '；工程基线'),
      version:release.value, colors:currentPalette.value.colors, range:sharedRange.value, coverage:percent(coverage.value?.valid_fraction),
      scale:{label:scale?.textContent ?? '', pixels:scale?.clientWidth ?? 0}, boundaryAvailable:!!aoi,
      quality:qualityOpen.value ? qualityData.value?.legend ?? [] : [], qualityNotes:qualityOpen.value ? qualityData.value?.notes ?? [] : [],
    }, type, () => originalState === snapshot() && !coverageLoading.value && !qualityLoading.value && !mapError.value && !qualityError.value)
    exportNotice.value = type.toUpperCase() + ' 已生成，包含标题、日期、图例、比例尺、来源与研究区边界。'
  } catch (e) { exportNotice.value = errorText(e) } finally { exportBusy.value = false }
}

const rangeStart = ref(''), rangeEnd = ref(''), appliedStart = ref(''), appliedEnd = ref(''), rangeError = ref('')
const dateLimits = computed(() => { const dates = assets.value.map(a => a.date).sort(); return { min: processingReport.value?.acquisition_start ?? dates[0] ?? '', max: processingReport.value?.acquisition_end ?? dates.at(-1) ?? '' } })
const rangePending = computed(() => rangeStart.value !== appliedStart.value || rangeEnd.value !== appliedEnd.value)
const pointer = ref<Position | null>(null)
let resizeObserver: ResizeObserver | null = null
const selected = computed(() => assets.value.find(a => a.id === selectedId.value))
const compareAsset = computed(() => assets.value.find(a => a.id === compareId.value))
const gridSpacing = computed(() => selected.value?.grid_spacing_km ?? 36)
const variableAssets = computed(() => assets.value.filter(a => a.variable === variable.value).sort((a, b) => a.date.localeCompare(b.date)))
const availableVariables = computed(() => ['lst', 'ndvi', 'soil_moisture', 'ef'].filter(v => assets.value.some(a => a.variable === v)))
const palettes = ref<Record<string, Palette>>({})
const currentPalette = computed(() => palettes.value[variable.value] ?? { colors: ['#365fa9', '#8ac5dc', '#fff0b0', '#eb9352', '#b43638'], label: '由低至高', range: sharedRange.value, version: '' })
const paletteStops = computed(() => currentPalette.value.colors.map((color, i) => color + ' ' + ((i / Math.max(1, currentPalette.value.colors.length - 1)) * 100) + '%').join(', '))
const release = ref(''), reportUrl = ref(''), catalogLoading = ref(true), catalogError = ref('')
const opacity = ref(85), comparing = ref(false), sourcesOpen = ref(false)
const sourceDialog = ref<HTMLElement | null>(null)
const boundaryError = ref('')
const basemapError = ref('')
let previousFocus: HTMLElement | null = null
const coverage = ref<Summary | null>(null), compareCoverage = ref<Summary | null>(null), coverageError = ref(''), coverageLoading = ref(false), mapError = ref('')
const pinned = ref<Position | null>(null), point = ref<Sample | null>(null), pointLoading = ref(false), pointError = ref('')
const series = ref<Sample[]>([]), seriesLoading = ref(false), seriesError = ref('')
const drawing = ref(false), drawingType = ref<'rectangle' | 'polygon'>('rectangle'), corner = ref<Position | null>(null), polygonVertices = ref<Position[]>([])
const statistics = ref<Summary | null>(null), statsLoading = ref(false), statsError = ref('')
const selectionGeometry = ref<any>(null), statsSnapshot = ref<any>(null)
let map: maplibregl.Map | null = null, otherMap: maplibregl.Map | null = null
let bounds: maplibregl.LngLatBoundsLike = [[-98.5, 35.5], [-96.5, 37.5]]
let aoi: any = null, mounted = true, syncing = false, manualView = false
let geographicContext: GeographicContext = emptyContext()
let inspectController: AbortController | null = null, coverageController: AbortController | null = null, statsController: AbortController | null = null
let inspectEpoch = 0, coverageEpoch = 0, statsEpoch = 0
const format = (value: unknown, digits = 3) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: digits }) : '—'
const percent = (value: number | null | undefined) => value == null ? '未提供' : (value * 100).toFixed(1) + '%'
const unit = computed(() => selected.value?.unit === 'm3/m3' ? 'm³/m³' : selected.value?.unit === '1' ? '无量纲' : selected.value?.unit ?? '')
const sharedRange = computed(() => {
  if (variable.value === 'soil_moisture') return [0, 0.6]
  if (variable.value === 'ndvi') return [-0.2, 1]
  if (variable.value === 'ef') return [0, 1]
  const items = variableAssets.value.filter(a => a.statistics.min != null && a.statistics.max != null)
  return items.length ? [Math.min(...items.map(a => a.statistics.min!)), Math.max(...items.map(a => a.statistics.max!))] : [-3.15, 46.85]
})
const validSeries = computed(() => series.value.filter(p => p.valid && p.value != null))
const reasonLabels: Record<string, string> = { nodata: '源像元缺测或未通过质量筛选；当前发布掩膜不能进一步区分原因。', outside_raster: '点击位置在当天栅格范围之外。', no_nearby_valid_smap_cell: '30 公里内没有通过质量筛选的 SMAP 原生格点。', no_published_asset: '当天没有已发布数据。', no_valid_pixels_after_qa: '当天所有候选像元均未通过质量筛选，未发布图层。', missing_lst: '当天缺少地表温度源文件。', missing_lst_input: '缺少地表温度源文件，未发布当天结果。', missing_input: '缺少必需的源数据，未发布当天结果。', missing_ndvi: '缺少 NDVI 合成期数据，未发布当天结果。', insufficient_edge_samples: '干湿边样本不足，未发布 EF 基线。', insufficient_valid_bins: '有效样本不足，无法拟合 EF 基线。', non_decreasing_dry_edge: '干边未随温度递减，未发布 EF 基线。', no_valid_inputs: '输入变量通过 QA 后没有共同有效像元。', unavailable_bracketing_composites: '缺少包围日期的 NDVI 合成期数据，未进行外推。' }
const unavailableDates = computed<Record<string, string>>(() => {
  const report = processingReport.value; if (!report) return {}
  const reasons: Record<string, string> = {}
  if (variable.value === 'soil_moisture') for (const item of report.smap_dates ?? []) if (item.status !== 'ready') reasons[item.date] = reasonLabels[item.reason] ?? item.reason ?? '当天 SMAP 数据未通过质量筛选或不可用。'
  for (const item of report.unpublished_assets ?? []) if (item.variable === variable.value) reasons[item.date] = reasonLabels[item.reason] ?? '处理报告标记为未发布：' + item.reason
  if (['lst', 'ef'].includes(variable.value)) for (const date of report.missing_lst_dates ?? []) reasons[date] = '当天缺少地表温度源文件。'
  if (['ndvi', 'ef'].includes(variable.value)) for (const date of report.unavailable_ndvi_dates ?? []) reasons[date] = '缺少包围该日期的 NDVI 合成期数据，未做外推。'
  if (variable.value === 'ef') for (const item of report.modis_dates ?? []) if (item.reason && !variableAssets.value.some(a => a.date === item.date)) reasons[item.date] ??= reasonLabels[item.reason] ?? 'EF 基线未发布；处理报告：' + item.reason
  return reasons
})
function sampleReason(sample: Sample) { return sample.reason === 'no_published_asset' && sample.date ? unavailableDates.value[sample.date] ?? reasonLabels.no_published_asset : reasonLabels[sample.reason ?? ''] ?? sample.reason ?? '当前位置没有有效观测值。' }
function resetRange() { rangeStart.value = appliedStart.value = dateLimits.value.min; rangeEnd.value = appliedEnd.value = dateLimits.value.max; rangeError.value = '' }
function applyRange() {
  if (!rangeStart.value || !rangeEnd.value || rangeStart.value > rangeEnd.value || rangeStart.value < dateLimits.value.min || rangeEnd.value > dateLimits.value.max || (Date.parse(rangeEnd.value) - Date.parse(rangeStart.value)) / 86400000 > 365) { rangeError.value = '请选择发布时段内的起止日期，开始日不晚于结束日，最多 366 天。'; return }
  appliedStart.value = rangeStart.value; appliedEnd.value = rangeEnd.value; rangeError.value = ''; if (pinned.value) void inspect()
}
async function loadExample() {
  exampleLoading.value = true; exampleNotice.value = ''; cancelDrawing()
  try {
    variable.value = 'ndvi'; await nextTick()
    const asset = [...variableAssets.value].sort((a, b) => b.statistics.valid_pixels - a.statistics.valid_pixels)[0]
    if (!asset?.point_url) throw new Error('当前目录没有可用的 NDVI 示例。')
    selectedId.value = asset.id; resetRange(); await nextTick(); resetView()
    const box = maplibregl.LngLatBounds.convert(bounds), center = box.getCenter()
    const positions = [[.5,.5],[.35,.5],[.65,.5],[.5,.35],[.5,.65],[.35,.35],[.65,.65],[.35,.65],[.65,.35]]
    for (const [x, y] of positions) {
      const lon = box.getWest() + (box.getEast() - box.getWest()) * x, lat = box.getSouth() + (box.getNorth() - box.getSouth()) * y
      const sample = await json(asset.point_url + '?lon=' + lon + '&lat=' + lat)
      if (sample.valid) { controlsOpen.value = false; await mapClick({ lon, lat }); exampleNotice.value = '已载入 ' + asset.date + ' 的 NDVI 示例，标记位置已验证有有效值。'; return }
    }
    throw new Error('示例候选位置均缺测，请在图层着色区域点击查询。研究区中心：' + center.lng.toFixed(3) + '°, ' + center.lat.toFixed(3) + '°')
  } catch (e) { exampleNotice.value = errorText(e) } finally { exampleLoading.value = false }
}
async function json(url: string, options: RequestInit = {}) {
  const response = await fetch(url, options)
  if (!response.ok) { let detail = ''; try { const e = await response.json(); detail = typeof e.detail === 'string' ? e.detail : '' } catch { /* use status */ }; throw new Error(detail || '服务请求失败（' + response.status + '），请重试。') }
  return response.json()
}
function isAbort(e: unknown) { return e instanceof Error && e.name === 'AbortError' }
function errorText(e: unknown) { return e instanceof Error ? e.message : '服务暂不可用，请稍后重试。' }
function resetView() { manualView = false; fitStudyArea(map, bounds) }
function sync(source: maplibregl.Map, target: maplibregl.Map | null) {
  if (syncing || !target || !comparing.value) return
  syncing = true; target.jumpTo({ center: source.getCenter(), zoom: source.getZoom(), bearing: source.getBearing(), pitch: source.getPitch() }); syncing = false
}
function applyLayer(target: maplibregl.Map | null, asset: Asset | undefined) {
  if (!target?.getSource('pin')) return
  if (target.getLayer('published-layer')) target.removeLayer('published-layer')
  if (target.getLayer('published-cells')) target.removeLayer('published-cells')
  if (target.getLayer('published-grid-outline')) target.removeLayer('published-grid-outline')
  if (target.getSource('published')) target.removeSource('published')
  if (target.getSource('published-grid')) target.removeSource('published-grid')
  if (!asset) return
  if (asset.tile_url) {
    target.addSource('published', { type: 'raster', tiles: [asset.tile_url], tileSize: 256, minzoom: 0, maxzoom: 14, bounds: asset.bounds as [number, number, number, number] })
    target.addLayer({ id: 'published-layer', type: 'raster', source: 'published', paint: { 'raster-opacity': opacity.value / 100, 'raster-resampling': 'nearest', 'raster-fade-duration': 0 } }, dataLayerAnchor)
  } else if (asset.geojson_url) {
    target.addSource('published', { type: 'geojson', data: asset.geojson_url })
    if (asset.grid_url) {
      target.addSource('published-grid', { type: 'geojson', data: asset.grid_url })
      target.addLayer({ id: 'published-cells', type: 'fill', source: 'published-grid', paint: { 'fill-color': paletteExpression(asset.variable) as any, 'fill-opacity': opacity.value * .48 / 100, 'fill-outline-color': '#456579' } }, dataLayerAnchor)
      target.addLayer({ id: 'published-grid-outline', type: 'line', source: 'published-grid', paint: { 'line-color': '#446d82', 'line-width': .8, 'line-opacity': opacity.value * .85 / 100 } }, dataLayerAnchor)
    }
    target.addLayer({ id: 'published-layer', type: 'circle', source: 'published', paint: { 'circle-radius': 3, 'circle-color': paletteExpression(asset.variable) as any, 'circle-opacity': opacity.value / 100, 'circle-stroke-color': '#fff', 'circle-stroke-width': .8 } }, dataLayerAnchor)
  }
}
function paletteExpression(key: string): any[] {
  const palette = palettes.value[key]; if (!palette) return ['literal', '#678470']
  const [lo, hi] = palette.range, step = (hi - lo) / (palette.colors.length - 1)
  return ['interpolate', ['linear'], ['get', key], ...palette.colors.flatMap((color, index) => [lo + step * index, color])] as any
}
function setSelection(geometry: any) {
  for (const target of [map, otherMap]) (target?.getSource('selection') as maplibregl.GeoJSONSource | undefined)?.setData(geometry ? { type: 'Feature', properties: {}, geometry } : { type: 'FeatureCollection', features: [] })
}
function setPin() {
  for (const target of [map, otherMap]) (target?.getSource('pin') as maplibregl.GeoJSONSource | undefined)?.setData(pinned.value ? { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [pinned.value.lon, pinned.value.lat] } } : { type: 'FeatureCollection', features: [] })
}
function rectangle(a: Position, b: Position) {
  const west = Math.min(a.lon, b.lon), east = Math.max(a.lon, b.lon), south = Math.min(a.lat, b.lat), north = Math.max(a.lat, b.lat)
  return { type: 'Polygon', coordinates: [[[west, south], [east, south], [east, north], [west, north], [west, south]]] }
}
function clearStatistics() {
  statsEpoch++; statsController?.abort(); statistics.value = null; statsSnapshot.value = null; statsLoading.value = false; statsError.value = ''; selectionGeometry.value = null; corner.value = null; polygonVertices.value = []; setSelection(null)
}
function toggleDraw(type: 'rectangle' | 'polygon') { const enabled = !(drawing.value && drawingType.value === type); clearStatistics(); polygonVertices.value = []; drawingType.value = type; drawing.value = enabled; for (const target of [map, otherMap]) if (target) target.getCanvas().style.cursor = drawing.value ? 'crosshair' : '' }
function finishPolygon() {
  const vertices = polygonVertices.value
  if (vertices.length > 1 && vertices[0].lon === vertices[vertices.length - 1].lon && vertices[0].lat === vertices[vertices.length - 1].lat) vertices.pop()
  const unique: Position[] = []
  for (const item of vertices) if (!unique.some(p => Math.abs(p.lon - item.lon) < 1e-8 && Math.abs(p.lat - item.lat) < 1e-8)) unique.push(item)
  if (unique.length < 3) { statsError.value = '多边形至少需要三个不同顶点。'; drawing.value = false; setSelection(null); return }
  unique.push(unique[0])
  const geometry = { type: 'Polygon', coordinates: [unique.map(p => [p.lon, p.lat])] }
  selectionGeometry.value = geometry; setSelection(geometry); drawing.value = false; corner.value = null; polygonVertices.value = []
  for (const target of [map, otherMap]) if (target) target.getCanvas().style.cursor = ''
  void computeStatistics(geometry)
}
function cancelDrawing() { drawing.value = false; corner.value = null; polygonVertices.value = []; setSelection(null); for (const target of [map, otherMap]) if (target) target.getCanvas().style.cursor = '' }
async function computeStatistics(geometry: any) {
  const asset = selected.value; if (!asset) return
  inspectorOpen.value = true
  statsController?.abort(); const controller = new AbortController(); statsController = controller; const epoch = ++statsEpoch
  statsLoading.value = true; statsError.value = ''; statistics.value = null
  try {
    const result = await json('/api/v1/statistics', { method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_ids: [asset.id], geometry, mask_policy: 'valid_only', pixel_rule: 'center_inside' }) })
    if (epoch !== statsEpoch || !mounted) return
    statistics.value = result.results[0]; statsSnapshot.value = result
  } catch (e) { if (!isAbort(e) && epoch === statsEpoch) statsError.value = errorText(e) } finally { if (epoch === statsEpoch) statsLoading.value = false }
}
async function inspect() {
  const asset = selected.value, position = pinned.value; if (!asset?.point_url || !position) return
  inspectController?.abort(); const controller = new AbortController(); inspectController = controller; const epoch = ++inspectEpoch
  point.value = null; series.value = []; pointError.value = ''; seriesError.value = ''; pointLoading.value = true; seriesLoading.value = true
  const suffix = '?lon=' + position.lon + '&lat=' + position.lat, start = appliedStart.value || dateLimits.value.min, end = appliedEnd.value || dateLimits.value.max
  await Promise.allSettled([
    (async () => { try { const data = await json(asset.point_url! + suffix, { signal: controller.signal }); if (epoch === inspectEpoch && mounted) point.value = data }
    catch (e) { if (!isAbort(e) && epoch === inspectEpoch) pointError.value = errorText(e) } finally { if (epoch === inspectEpoch) pointLoading.value = false } })(),
    (async () => { try { const cacheKey = [asset.variable, position.lon, position.lat, start, end, release.value].join('/'); const cached = seriesCache.get(cacheKey); if (cached) { if (epoch === inspectEpoch) series.value = cached; return }; const data = await json('/api/v1/series' + suffix + '&variable=' + asset.variable + '&start=' + start + '&end=' + end, { signal: controller.signal }); if (epoch === inspectEpoch && mounted) { const byDate = new Map<string, Sample>((data.points ?? []).map((p: Sample) => [p.date!, p])); series.value = Array.from({ length: Math.round((Date.parse(end) - Date.parse(start)) / 86400000) + 1 }, (_, i) => { const date = new Date(Date.parse(start) + i * 86400000).toISOString().slice(0,10); return byDate.get(date) ?? { date, valid: false, value: null, reason: 'no_published_asset' } }); seriesCache.set(cacheKey, series.value); if (seriesCache.size > 12) seriesCache.delete(seriesCache.keys().next().value!) } }
    catch (e) { if (!isAbort(e) && epoch === inspectEpoch) seriesError.value = errorText(e) } finally { if (epoch === inspectEpoch) seriesLoading.value = false } })(),
  ])
}
async function loadCoverage() {
  coverageController?.abort(); const controller = new AbortController(); coverageController = controller; const epoch = ++coverageEpoch
  coverage.value = null; compareCoverage.value = null; coverageLoading.value = true; coverageError.value = ''
  try {
    const primary = selected.value, secondary = comparing.value ? compareAsset.value : null; if (!primary) return
    const results = await Promise.all([json('/api/v1/assets/' + encodeURIComponent(primary.id) + '/coverage', { signal: controller.signal }), secondary ? json('/api/v1/assets/' + encodeURIComponent(secondary.id) + '/coverage', { signal: controller.signal }) : Promise.resolve(null)])
    if (epoch === coverageEpoch && mounted) [coverage.value, compareCoverage.value] = results
  } catch (e) { if (!isAbort(e) && epoch === coverageEpoch) coverageError.value = errorText(e) } finally { if (epoch === coverageEpoch) coverageLoading.value = false }
}
function mapClick(position: Position) {
  if (drawing.value) {
    if (drawingType.value === 'polygon') { polygonVertices.value.push(position); if (polygonVertices.value.length >= 3) setSelection({ type: 'Polygon', coordinates: [[...polygonVertices.value.map(p => [p.lon, p.lat]), [polygonVertices.value[0].lon, polygonVertices.value[0].lat]]] }); return }
    if (!corner.value) { corner.value = position; return }
    if (Math.abs(position.lon - corner.value.lon) < 1e-7 || Math.abs(position.lat - corner.value.lat) < 1e-7) return
    const geometry = rectangle(corner.value, position); selectionGeometry.value = geometry; setSelection(geometry); drawing.value = false; corner.value = null
    for (const target of [map, otherMap]) if (target) target.getCanvas().style.cursor = ''
    void computeStatistics(geometry); return
  }
  stopPlayback(); inspectorOpen.value = true; pinned.value = position; pointer.value = position; setPin(); void loadQualityPoint(); return inspect()
}
function createMap(element: HTMLElement, secondary = false) {
  const target = new maplibregl.Map({ container: element, canvasContextAttributes: {preserveDrawingBuffer: true}, bounds, maxBounds: geographicContext.bbox, fitBoundsOptions: { padding: 30, maxZoom: 10 }, attributionControl: false, style: { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#edf0f2' } }] } })
  target.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
  const scale = new maplibregl.ScaleControl({ maxWidth: 100, unit: 'metric' })
  if (!secondary && scaleEl.value) scaleEl.value.appendChild(scale.onAdd(target))
  else target.addControl(scale, 'bottom-right')
  if (!secondary) target.on('remove', () => scale.onRemove())
  target.on('movestart', event => { if ((event as any).originalEvent) manualView = true })
  target.on('mouseout', () => { pointer.value = null })
  target.on('load', () => {
    addGeographicContext(target, geographicContext, aoi)
    target.addSource('selection', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
    target.addLayer({ id: 'selection-fill', type: 'fill', source: 'selection', paint: { 'fill-color': '#b8582d', 'fill-opacity': .12 } })
    target.addLayer({ id: 'selection-line', type: 'line', source: 'selection', paint: { 'line-color': '#b8582d', 'line-width': 2, 'line-dasharray': [3, 2] } })
    target.addSource('pin', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
    target.addLayer({ id: 'pin', type: 'circle', source: 'pin', paint: { 'circle-radius': 6, 'circle-color': '#b8582d', 'circle-stroke-color': '#fff', 'circle-stroke-width': 3 } })
    applyLayer(target, secondary ? compareAsset.value : selected.value); setPin(); setSelection(selectionGeometry.value); if (!secondary) showQuality()
    if (secondary && map) sync(map, target); else fitStudyArea(target, bounds, 0)
  })
  target.on('click', e => mapClick({ lon: e.lngLat.lng, lat: e.lngLat.lat }))
  target.on('mousemove', e => {
    pointer.value = { lon: e.lngLat.lng, lat: e.lngLat.lat }
    if (!drawing.value) return
    const cursorPosition = { lon: e.lngLat.lng, lat: e.lngLat.lat }
    if (drawingType.value === 'rectangle' && corner.value) setSelection(rectangle(corner.value, cursorPosition))
    else if (drawingType.value === 'polygon' && polygonVertices.value.length >= 2) {
      const points = [...polygonVertices.value, cursorPosition, polygonVertices.value[0]].map(p => [p.lon, p.lat])
      setSelection({ type: 'Polygon', coordinates: [points] })
    }
  })
  target.on('dblclick', e => { if (drawing.value && drawingType.value === 'polygon') { e.preventDefault(); finishPolygon() } })
  target.getCanvas().addEventListener('keydown', e => { if (e.key === 'Escape' && drawing.value) cancelDrawing() })
  target.on('move', () => sync(target, secondary ? map : otherMap))
  target.on('error', () => { mapError.value = '图层加载失败，请检查服务后点击“重新加载”。' })
  return target
}
async function loadCatalog() {
  catalogLoading.value = true; catalogError.value = ''
  try {
    const [doc, colors, report] = await Promise.all([json('/api/v1/catalog'), json('/api/v1/styles'), json('/api/v1/processing-report').catch(() => null)]); if (!mounted) return
    palettes.value = colors
    processingReport.value = report
    assets.value = doc.assets ?? []; release.value = doc.data_version ?? ''; reportUrl.value = doc.report_url ?? ''
    resetRange()
    if (!availableVariables.value.includes(variable.value)) variable.value = availableVariables.value[0] ?? 'ndvi'
    const items = variableAssets.value
    if (!items.some(a => a.id === selectedId.value)) selectedId.value = [...items].sort((a, b) => b.statistics.valid_pixels - a.statistics.valid_pixels)[0]?.id ?? ''
    if (!items.some(a => a.id === compareId.value)) compareId.value = items.find(a => a.id !== selectedId.value)?.id ?? selectedId.value
    await loadCoverage()
  } catch (e) { catalogError.value = errorText(e) } finally { catalogLoading.value = false }
}
function downloadCsv(name: string, records: Record<string, unknown>[]) {
  if (!records.length) return
  const keys = Array.from(new Set(records.flatMap(row => Object.keys(row))))
  const escape = (value: unknown) => { let str = value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value); if (typeof value === 'string' && /^[=+@\-]/.test(str)) str = "'" + str; return '"' + str.replaceAll('"', '""') + '"' }
  const csv = '\ufeff' + [keys.map(escape).join(','), ...records.map(row => keys.map(key => escape(row[key])).join(','))].join('\r\n')
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = name + '.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function metadata(withPosition = true) { return { variable: variable.value, unit: selected.value?.unit, method_version: selected.value?.method_version, data_version: release.value, exported_at_utc: new Date().toISOString(), mask_policy: 'published_valid_only', ...(withPosition ? { longitude: pinned.value?.lon, latitude: pinned.value?.lat } : {}) } }
function exportPoint() { if (point.value) downloadCsv('位置查询-' + selectedId.value, [{ ...metadata(), ...point.value, date: selected.value?.date, sample_count: point.value.valid ? 1 : 0 }]) }
function exportSeries() { downloadCsv('点位时序-' + variable.value + '-' + appliedStart.value + '-' + appliedEnd.value, series.value.map(p => ({ ...metadata(), ...p, range_start: appliedStart.value, range_end: appliedEnd.value, temporal_method: variable.value === 'ndvi' ? 'composite_period_interpolation_not_daily_observation' : 'published_daily_values', missing_reason: p.valid ? '' : sampleReason(p), sample_count: p.valid ? 1 : 0 }))) }
function exportStatistics() { if (statsSnapshot.value) downloadCsv('区域统计-' + selectedId.value, [{ ...metadata(false), ...statsSnapshot.value.results[0], geometry: statsSnapshot.value.geometry, mask_policy: statsSnapshot.value.mask_policy, pixel_rule: statsSnapshot.value.pixel_rule, sample_count: statistics.value?.valid_pixels }]) }
function dialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { sourcesOpen.value = false; return }
  if (event.key !== 'Tab') return
  const elements = sourceDialog.value?.querySelectorAll<HTMLElement>('button, a[href]')
  if (!elements?.length) return
  const first = elements[0], last = elements[elements.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}
watch(sourcesOpen, async enabled => {
  if (enabled) { previousFocus = document.activeElement as HTMLElement; await nextTick(); sourceDialog.value?.querySelector('button')?.focus() }
  else previousFocus?.focus()
})
watch(variable, () => {
  stopPlayback(); playbackNotice.value = ''
  const previousDate = selected.value?.date, list = variableAssets.value
  selectedId.value = list.find(a => a.date === previousDate)?.id ?? list[0]?.id ?? ''; compareId.value = list.find(a => a.id !== selectedId.value)?.id ?? selectedId.value
  resetRange(); exampleNotice.value = ''
})
watch(selectedId, () => {
  void loadQuality()
  mapError.value = ''; clearStatistics(); drawing.value = false
  for (const target of [map, otherMap]) if (target) target.getCanvas().style.cursor = ''
  applyLayer(map, selected.value); void loadCoverage(); inspectEpoch++; inspectController?.abort(); point.value = null; series.value = []; pointError.value = ''; seriesError.value = ''; pointLoading.value = false; seriesLoading.value = false
  if (pinned.value) void inspect()
})
watch(compareId, () => { applyLayer(otherMap, compareAsset.value); if (comparing.value) void loadCoverage() })
watch(selected, (asset, previous) => {
  if (asset?.id !== previous?.id) applyLayer(map, asset)
})
watch(compareAsset, (asset, previous) => { if (asset?.id !== previous?.id && comparing.value) applyLayer(otherMap, asset) })
watch(comparing, async enabled => { await nextTick(); map?.resize(); if (enabled && compareEl.value && !otherMap) { otherMap = createMap(compareEl.value, true); resizeObserver?.observe(compareEl.value) }; otherMap?.resize(); if (map) sync(map, otherMap); void loadCoverage() })
watch(opacity, () => {
  for (const target of [map, otherMap]) if (target) {
    if (target.getLayer('published-layer')) target.setPaintProperty('published-layer', selected.value?.geojson_url ? 'circle-opacity' : 'raster-opacity', opacity.value / 100)
    if (target.getLayer('published-cells')) target.setPaintProperty('published-cells', 'fill-opacity', opacity.value * .48 / 100)
    if (target.getLayer('published-grid-outline')) target.setPaintProperty('published-grid-outline', 'line-opacity', opacity.value * .85 / 100)
  }
})
watch(currentPalette, palette => {
  for (const target of [map, otherMap]) if (target) {
    if (target.getLayer('published-layer')?.type === 'circle') target.setPaintProperty('published-layer', 'circle-color', paletteExpression(variable.value))
    if (target.getLayer('published-cells')) target.setPaintProperty('published-cells', 'fill-color', paletteExpression(variable.value))
    if (target.getLayer('published-grid-outline')) target.setPaintProperty('published-grid-outline', 'line-color', palette.colors[palette.colors.length - 1])
  }
}, { deep: true })
onMounted(async () => {
  if (window.matchMedia('(max-width: 760px)').matches) controlsOpen.value = false
  await Promise.all([
    (async () => { try { aoi = await json('/api/v1/study-area'); bounds = studyBounds(aoi) } catch { aoi = null; boundaryError.value = '研究区边界加载失败，可点击“重新加载”重试。' } })(),
    (async () => { try { geographicContext = await json(contextUrl) } catch { basemapError.value = '地理底图暂未加载，遥感数据仍可使用。' } })(),
  ])
  if (!mounted) return
  try { map = createMap(mapEl.value!) } catch { mapError.value = '浏览器无法创建地图，请启用硬件加速或更换浏览器。' }
  resizeObserver = new ResizeObserver(() => { if (mapEl.value?.clientWidth) { if (manualView) map?.resize(); else fitStudyArea(map, bounds, 0) }; if (comparing.value && compareEl.value?.clientWidth) { otherMap?.resize(); if (map) sync(map, otherMap) } })
  if (mapEl.value) resizeObserver.observe(mapEl.value)
  await loadCatalog()
})
onBeforeUnmount(() => { mounted = false; stopPlayback(); qualityEpoch++; qualityController?.abort(); resizeObserver?.disconnect(); inspectController?.abort(); coverageController?.abort(); statsController?.abort(); map?.remove(); otherMap?.remove() })
watch(qualityOpen, () => { void loadQuality() })
function retryMap() { window.location.reload() }
</script>

<template>
  <div class="workspace">
    <header class="app-header">
      <div class="brand"><span class="eyebrow">SGP</span><h1>地表温度、植被与土壤湿度分析</h1><p class="brand-period">美国南部大平原</p></div>
      <div class="header-meta"><span>{{ assets.length }} 个已发布图层</span><span>保留质量筛选与真实缺测</span></div>
      <div class="header-actions"><button class="source-button" @click="sourcesOpen = true">数据与方法</button><nav class="panel-toggles" aria-label="面板显示"><button :aria-expanded="controlsOpen" aria-controls="exploration-controls" @click="controlsOpen = !controlsOpen">{{ controlsOpen ? '收起日期面板' : '展开变量与日期' }}</button><button :aria-expanded="inspectorOpen" aria-controls="analysis-results" @click="inspectorOpen = !inspectorOpen">{{ inspectorOpen ? '收起分析结果' : '展开分析结果' }}</button></nav></div>
    </header>
    <main :class="['workbench', { 'controls-collapsed': !controlsOpen, 'inspector-collapsed': !inspectorOpen }]">
      <aside v-show="controlsOpen" id="exploration-controls" class="controls panel">
        <div class="section-heading"><span class="section-index">01</span><h2>观测时间</h2></div><p class="muted intro">{{ labels[variable] }} · 按月浏览有效观测</p>
        <div v-if="catalogLoading" class="empty-state" role="status">正在读取已发布目录…</div><div v-else-if="catalogError" class="error" role="alert">{{ catalogError }} <button class="text-button" @click="loadCatalog">重试</button></div><div v-else-if="!assets.length" class="empty-state">暂无已发布数据，请先导入真实处理结果。</div>
        <template v-if="selected">
          <DateCalendar :assets="variableAssets" :selected="selectedId" :variable="variable" :version="release" :unavailable="unavailableDates" @select="selectedId = $event" />
          <section class="coverage-card"><div class="card-label">{{ variable === 'soil_moisture' ? '原生格点有效比例' : '研究区有效覆盖率' }}<span>{{ coverageLoading ? '读取中' : coverage && coverage.valid_fraction != null && coverage.valid_fraction < .3 ? '数据覆盖较少' : 'QA 后' }}</span></div><div class="coverage-number">{{ coverageLoading ? '…' : percent(coverage?.valid_fraction) }}</div><div class="progress-track"><span :style="{ width: percent(coverage?.valid_fraction ?? 0) }"></span></div><p v-if="coverage">{{ format(coverage.valid_pixels, 0) }} / {{ format(coverage.total_pixels, 0) }} {{ variable === 'soil_moisture' ? '定位格点' : '候选像元' }}</p><p v-if="coverageError" class="error">{{ coverageError }} <button class="text-button" @click="loadCoverage">重试</button></p><details v-if="coverage"><summary>覆盖率与缺测口径</summary><p>{{ coverage.coverage_note }}</p><p>透明区域保持缺测，不补零、不填充。</p></details></section>
          <div class="opacity-heading"><label for="opacity">图层透明度</label><span>{{ opacity }}%</span></div><input id="opacity" v-model.number="opacity" type="range" min="0" max="100" />
          <section class="comparison-control"><div class="toggle-row"><span>日期对比</span><button role="switch" :aria-checked="comparing" aria-label="日期对比" :disabled="variableAssets.length < 2" :class="['switch', { checked: comparing }]" @click="comparing = !comparing"><i></i></button></div><template v-if="comparing"><label for="compare-date">对照日期</label><select id="compare-date" v-model="compareId"><option v-for="asset in variableAssets" :key="asset.id" :value="asset.id">{{ asset.date }}</option></select><p class="date-hint">两图同步位置，使用同一色标。各日期有效范围可能不同。</p><p v-if="compareId === selectedId" class="warning">当前两个日期相同，请选择另一个日期。</p></template></section>
          <section class="method-note"><span class="tiny-label">数据解读</span><p>{{ hints[variable] }}</p><p v-if="selected.grid_url" class="grid-note">浅色格面按约{{ gridSpacing }}公里产品网格间距绘制；不代表独立空间分辨率，查询保留格点中心。</p></section><a class="file-download" :href="selected.download_url">下载当前结果 <span>{{ selected.geojson_url ? 'GeoJSON ↓' : 'GeoTIFF ↓' }}</span></a>
        </template><div class="controls-footer"><span class="status-dot"></span>本地处理结果 · UTC 产品日期</div>
      </aside>
      <section class="center-column" aria-label="地图与时序分析">
        <div class="variable-toolbar"><span class="toolbar-label">观测变量</span><div class="variable-grid" aria-label="显示变量"><button v-for="v in availableVariables" :key="v" :class="['variable-button', { active: variable === v }]" :aria-pressed="variable === v" @click="variable = v"><span class="variable-symbol" :class="v">{{ shortLabels[v] }}</span><span>{{ labels[v] }}</span><span v-if="v === 'ef'" class="derived-tag">派生</span></button></div></div>
        <div class="presentation-tools" aria-label="演示与地图导出">
          <div class="playback-controls"><button class="play-button" :disabled="variableAssets.length < 2 || exportBusy" :aria-pressed="playing" @click="togglePlayback">{{ playing ? '暂停播放' : '按日期播放' }}</button><label>速度<select v-model.number="playbackDelay" aria-label="播放速度"><option :value="900">快 · 0.9秒</option><option :value="1800">中 · 1.8秒</option><option :value="3500">慢 · 3.5秒</option></select></label><label><input v-model="playbackLoop" type="checkbox">循环</label></div>
          <span class="playback-status" role="status">{{ selected?.date }} · {{ coverageLoading ? '覆盖率读取中…' : '有效比例 ' + percent(coverage?.valid_fraction) }}</span>
          <div class="map-export-actions"><button :disabled="!selected || exportBusy || coverageLoading || qualityLoading" @click="downloadMap('png')">{{ exportBusy ? '正在生成…' : '导出地图 PNG' }}</button><button :disabled="!selected || exportBusy || coverageLoading || qualityLoading" @click="downloadMap('pdf')">导出地图 PDF</button><button :aria-expanded="playbackHelpOpen" aria-controls="playback-help" @click="playbackHelpOpen = !playbackHelpOpen">播放说明</button></div>
          <p v-if="playbackHelpOpen" id="playback-help" class="presentation-note playback-help">仅按已发布日期播放，未发布日期不生成观测；等待地图与当天覆盖率加载后继续。</p><p v-if="playbackNotice" class="presentation-note" role="status">{{ playbackNotice }}</p><p v-if="exportNotice" class="export-notice" role="status">{{ exportNotice }}</p>
        </div>
        <div class="map-toolbar"><div><span class="section-index">02</span><strong>空间浏览</strong><span class="small muted">{{ labels[variable] }} · {{ unit }}</span></div><div class="map-actions"><button :disabled="!selected" :class="{ 'is-drawing': drawing && drawingType === 'rectangle' }" @click="toggleDraw('rectangle')">{{ drawing && drawingType === 'rectangle' ? '取消绘制' : '矩形统计' }}</button><button :disabled="!selected" :class="{ 'is-drawing': drawing && drawingType === 'polygon' }" @click="toggleDraw('polygon')">{{ drawing && drawingType === 'polygon' ? '取消绘制' : '多边形统计' }}</button><button :aria-pressed="qualityOpen" :class="{ 'is-drawing': qualityOpen }" @click="qualityOpen = !qualityOpen">质量与缺测</button><button @click="resetView">返回研究区</button></div></div>
        <div v-if="drawing" class="draw-instruction" role="status">{{ drawingType === 'rectangle' ? corner ? '再点击矩形的另一个角，完成统计。' : '在地图上依次点击矩形两个对角。' : '依次点击多边形顶点，双击结束绘制。' }} 最多100万候选源像元。</div><div v-if="mapError || boundaryError" class="map-error" role="alert">{{ mapError || boundaryError }} <button class="text-button" @click="retryMap">重新加载</button></div>
        <div v-if="basemapError" class="basemap-warning" role="status">{{ basemapError }} <button class="text-button" @click="retryMap">重新加载</button></div>
        <div :class="['maps', { comparing }]"><div class="map-pane"><div ref="mapEl" class="map"></div><div class="map-badge"><span>{{ comparing ? 'A / 当前日期' : '当前图层' }}</span><strong>{{ selected?.date ?? '等待目录' }}</strong><small>{{ labels[variable] }}</small></div>
          <div v-if="!coverageLoading && coverage?.valid_fraction != null && coverage.valid_fraction < .3" class="map-coverage-alert" role="status"><strong>数据覆盖较少 · {{ percent(coverage.valid_fraction) }}</strong><span>{{ variable === 'soil_moisture' ? '此为有效格点比例，非面积覆盖率。' : '研究区内仅部分像元有效。' }} 底图透出的位置可能缺测或未通过质量筛选。</span></div>
          <div v-if="point && !point.valid && !pointLoading" class="map-query-alert" role="status"><strong>所选位置无有效值</strong><span>{{ sampleReason(point) }}</span></div>
        </div><div v-show="comparing" class="map-pane"><div ref="compareEl" class="map"></div><div class="map-badge"><span>B / 对照日期</span><strong>{{ compareAsset?.date ?? '—' }}</strong><small>有效比例 {{ percent(compareCoverage?.valid_fraction) }}</small></div><div v-if="compareCoverage?.valid_fraction != null && compareCoverage.valid_fraction < .3" class="map-coverage-alert"><strong>数据覆盖较少 · {{ percent(compareCoverage.valid_fraction) }}</strong><span>两个日期使用相同色标，缺测保留透明。</span></div></div>
          <div class="map-credit">Natural Earth · 本地地理底图</div><div class="map-footnote">{{ variable === 'soil_moisture' ? '约' + gridSpacing + 'km产品网格 · 中心点可查询' : '统一色标 · 区外淡化显示' }}</div>
        </div>
        <div class="map-caption"><div class="map-location" role="status"><span class="location-region">SGP · 美国南部大平原</span><div ref="scaleEl" class="map-scale-host" aria-label="地图比例尺"></div><span>{{ pointer ? format(pointer.lon, 4) + '°, ' + format(pointer.lat, 4) + '°' : pinned ? '选中：' + format(pinned.lon, 4) + '°, ' + format(pinned.lat, 4) + '°' : '点击地图读取位置与时序' }}</span></div>
        <div class="map-legend" aria-label="图层色标与缺测说明"><div class="numeric-legend"><div class="legend-heading"><strong>{{ shortLabels[variable] }}</strong><span>{{ currentPalette.label }} · {{ unit }}</span></div><div class="color-ramp" :style="{ background: 'linear-gradient(to right, ' + paletteStops + ')' }"></div><div class="range-labels"><span>{{ format(sharedRange[0], 2) }}</span><span>{{ format(sharedRange[1], 2) }}</span></div></div><div class="legend-context"><span><i class="key-boundary" aria-hidden="true"></i>研究区边界</span><span class="nodata-key"><i class="key-nodata" aria-hidden="true"></i>缺测／未通过质量筛选</span><small>研究区内缺测处保留地理底图；不以观测色填充</small></div></div>
        <section v-if="qualityOpen" class="quality-panel" aria-label="质量与缺测诊断"><h3>空白位置的来源诊断 <span>仅叠加主日期图面</span></h3><p v-if="qualityLoading" role="status">正在读取原始质量标记…</p><p v-if="qualityError" class="error" role="alert">{{ qualityError }} <button class="text-button" @click="loadQuality">重试</button></p><template v-if="qualityData"><div class="quality-legend"><span v-for="key in qualityData.legend" :key="key.code"><i :style="{ background: key.color }"></i>{{ key.label }} <strong>{{ format(key.count, 0) }}</strong></span></div><p v-for="note in qualityData.notes" :key="note">{{ note }}</p><p>{{ qualityData.scope }}</p><p v-if="qualityData.source_dates?.length">质量依据日期：{{ qualityData.source_dates.join('、') }}</p><p v-if="qualityPoint" class="quality-point"><strong>选中位置：{{ qualityPoint.label }}</strong><span v-if="qualityPoint.source_dates?.length"> · 依据 {{ qualityPoint.source_dates.join('、') }}</span></p><p v-else>点击地图可查看该位置的分类；灰色缺测不等同于云。</p></template></section>
        <div class="map-data-status"><span>{{ selected?.date ?? '等待目录' }} · {{ labels[variable] }} · {{ unit }}</span><span :class="{ 'low-coverage-text': coverage?.valid_fraction != null && coverage.valid_fraction < .3 }">{{ coverageLoading ? '正在计算有效比例…' : '有效' + (variable === 'soil_moisture' ? '格点比例 ' : '覆盖率 ') + percent(coverage?.valid_fraction) }} · 缺测保持透明</span></div></div>
        <section class="series-panel" aria-labelledby="series-title"><div class="series-heading"><div><span class="section-index">03</span><h2 id="series-title">点位时序</h2><span class="small muted">{{ labels[variable] }} · {{ unit }}</span></div><button class="subtle-button" :aria-pressed="multiMode" @click="multiMode = !multiMode">{{ multiMode ? '返回单变量' : '同点三变量' }}</button><button v-if="!multiMode" class="subtle-button" :disabled="!series.length || seriesLoading" @click="exportSeries">导出 CSV ↓</button></div>
          <form class="series-range" @submit.prevent="applyRange"><label>开始日期<input v-model="rangeStart" type="date" :min="dateLimits.min" :max="dateLimits.max" required aria-label="时序开始日期"></label><label>结束日期<input v-model="rangeEnd" type="date" :min="dateLimits.min" :max="dateLimits.max" required aria-label="时序结束日期"></label><button type="submit" :disabled="seriesLoading || !assets.length">应用时段</button><button type="button" :disabled="seriesLoading || !assets.length" @click="resetRange(); applyRange()">全部时段</button></form>
          <p v-if="rangeError" class="error" role="alert">{{ rangeError }}</p><p v-if="rangePending" class="date-hint">已调整日期，请点击“应用时段”；当前图表与导出仍使用 {{ appliedStart }} 至 {{ appliedEnd }}。</p>
          <MultiSeries v-if="multiMode && pinned" :position="pinned" :start="appliedStart" :end="appliedEnd" :assets="assets" :version="release" @export="downloadCsv('同点三变量-' + appliedStart + '-' + appliedEnd, $event)" />
          <p v-else-if="multiMode" class="series-empty">先点击地图选定同一查询坐标，再同时查看 LST、NDVI 和 SMAP。</p>
          <template v-if="!multiMode">
          <p v-if="variable === 'ndvi'" class="temporal-note"><strong>合成期插值</strong> · MOD13A2 合成期间的线性插值，不代表每天一次独立观测。</p>
          <p v-if="variable === 'lst'" class="temporal-note"><strong>摄氏度发布</strong> · °C = K − 273.15；地图、查询、统计与下载使用同一单位，缺测不参与换算。</p>
          <p v-if="!pinned" class="series-empty">点击地图选择位置，查看该变量的逐日变化。缺测日期保留为空。</p><p v-else-if="seriesLoading" class="series-empty" role="status">正在读取同一位置的逐日数值…</p><p v-else-if="seriesError" class="error" role="alert">{{ seriesError }} <button class="text-button" @click="inspect">重试</button></p>
          <template v-else><div class="series-meta"><span>{{ format(pinned.lon, 4) }}°, {{ format(pinned.lat, 4) }}°</span><span>{{ validSeries.length }} 个有效日 / {{ series.length }} 个日历日</span></div><SeriesChart v-if="series.length" :points="series" :unit="unit" :label="labels[variable]" :reason="sampleReason" /><p v-if="series.length" class="chart-footnote">横轴为产品日期（UTC），纵轴为{{ labels[variable] }}（{{ unit }}）。缺测处断线，不补零、不跨缺测连线。</p><p v-if="!validSeries.length" class="empty-state">该位置在所选时间范围内均无有效值；缺测日期保留为空，未生成数值曲线。</p><details v-if="series.length" class="series-data"><summary>查看每日数值 · 缺测不补零</summary><table><thead><tr><th>日期</th><th>数值（{{ unit }}）</th><th>状态</th></tr></thead><tbody><tr v-for="p in series" :key="p.date"><td>{{ p.date }}</td><td>{{ p.valid ? format(p.value, 4) : '—' }}</td><td>{{ p.valid ? '有效' : sampleReason(p) }}</td></tr></tbody></table></details></template>
          </template>
        </section>
      </section>
      <aside v-show="inspectorOpen" id="analysis-results" class="inspector panel" aria-label="位置与区域分析结果"><div class="section-heading"><span class="section-index">04</span><h2>分析结果</h2><button class="text-button close-inspector" @click="inspectorOpen = false">收起</button></div>
        <section class="point-panel"><div class="inspector-heading"><h3>位置查询</h3><span class="tiny-label">{{ shortLabels[variable] }}</span></div><div v-if="pinned" class="coordinate-grid"><div><span>经度</span><strong>{{ format(pinned.lon, 5) }}</strong></div><div><span>纬度</span><strong>{{ format(pinned.lat, 5) }}</strong></div></div><p v-else class="empty-state">点击地图，读取原始数值与质量信息。</p><p v-if="pointLoading" class="empty-state" role="status">正在查询…</p><p v-else-if="pointError" class="error" role="alert">{{ pointError }} <button class="text-button" @click="inspect">重试</button></p><div v-else-if="point" :class="['point-value-card', { missing: !point.valid }]"><div class="card-label">{{ labels[variable] }}<span>{{ selected?.date }}</span></div><div class="point-number">{{ point.valid ? format(point.value, 4) : '—' }} <small>{{ unit }}</small></div><p>{{ point.valid ? '有效值 · 源数据查询' : sampleReason(point) }}</p></div>
          <dl v-if="point?.valid" class="details-list"><template v-if="point.source_pixel"><dt>源像元行 / 列</dt><dd>{{ point.source_pixel.row }} / {{ point.source_pixel.col }}</dd><dt>源像元中心</dt><dd>{{ format(point.source_pixel.lon, 4) }}, {{ format(point.source_pixel.lat, 4) }}</dd></template><template v-if="point.distance_m != null"><dt>距匹配格点</dt><dd>{{ format(point.distance_m / 1000, 2) }} km</dd><dt>原生格点中心</dt><dd>{{ format(point.matched_coordinates?.[0], 4) }}, {{ format(point.matched_coordinates?.[1], 4) }}</dd><dt>质量标识</dt><dd>QA = {{ point.quality_flag }}</dd></template></dl><button class="wide-export" :disabled="!point || pointLoading" @click="exportPoint">导出位置查询 CSV <span>↓</span></button>
        </section>
        <section class="statistics-panel"><div class="inspector-heading"><h3>区域统计</h3><button v-if="statistics || selectionGeometry" class="text-button" @click="clearStatistics">清除</button><span v-else class="tiny-label">矩形选择</span></div><p v-if="!statistics && !statsLoading && !statsError" class="empty-state">使用地图上方的“矩形统计”，依次点击两个对角。</p><p v-if="statsLoading" class="empty-state" role="status">正在按源网格计算统计…</p><p v-if="statsError" class="error" role="alert">{{ statsError }} <button v-if="selectionGeometry" class="text-button" @click="computeStatistics(selectionGeometry)">重试</button></p><template v-if="statistics"><div class="stats-grid"><div><span>有效{{ variable === 'soil_moisture' ? '格点' : '像元' }}</span><strong>{{ format(statistics.valid_pixels, 0) }}</strong></div><div><span>有效比例</span><strong>{{ percent(statistics.valid_fraction) }}</strong></div><div><span>均值</span><strong>{{ format(statistics.mean) }}</strong></div><div><span>标准差</span><strong>{{ format(statistics.std) }}</strong></div><div><span>最小值</span><strong>{{ format(statistics.min, 4) }}</strong></div><div><span>最大值</span><strong>{{ format(statistics.max, 4) }}</strong></div></div><p class="stats-policy">{{ statistics.coverage_note ?? '研究区内像元中心落入矩形；仅统计文件掩膜通过且数值有限的像元；等像元权重，标准差为总体标准差。' }}</p><p v-if="statistics.valid_pixels === 0" class="warning">所选区域没有有效值，统计结果保留为空。</p></template><button class="wide-export" :disabled="!statistics || statsLoading" @click="exportStatistics">导出区域统计 CSV <span>↓</span></button></section>
        <section class="provenance-card"><span class="tiny-label">来源与处理版本</span><strong>{{ selected?.method_version ?? '等待目录' }}</strong><p>{{ release || '尚未发布' }}</p><button class="text-button" @click="sourcesOpen = true">查看来源与使用限制 ↗</button></section>
      </aside>
    </main><footer class="app-footer"><span>真实样本 · 保留 QA 与缺测 · 不进行多源逐像元融合</span><span>SGP</span></footer>
    <div v-if="sourcesOpen" class="modal-backdrop" @click.self="sourcesOpen = false" @keydown.esc="sourcesOpen = false"><section ref="sourceDialog" class="source-dialog" role="dialog" @keydown="dialogKeydown" aria-modal="true" aria-labelledby="source-title"><div class="dialog-heading"><h2 id="source-title">数据来源与使用说明</h2><button autofocus aria-label="关闭来源说明" @click="sourcesOpen = false">×</button></div><p>当前发布版本：<strong>{{ release || '尚未发布' }}</strong>，共 {{ assets.length }} 个图层。数量与可用日期以发布目录为准。</p><div v-for="v in availableVariables" :key="v" class="source-item"><strong>{{ labels[v] }}</strong><p>{{ hints[v] }}</p><code>{{ assets.find(a => a.variable === v)?.method_version }}</code></div><p>各变量并排查看不等于数据融合。地图不依赖外部底图；透明区域表示缺测或质量筛选后的空值。CSV 保留单位、方法、版本、样本数和 UTC 导出时间。</p><div class="dialog-links"><a v-if="reportUrl" :href="reportUrl" target="_blank" rel="noreferrer">逐日处理报告 ↗</a><a href="/api/docs" target="_blank" rel="noreferrer">接口说明 ↗</a></div></section></div>
  </div>
</template>
