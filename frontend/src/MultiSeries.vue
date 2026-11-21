<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import SeriesChart from './SeriesChart.vue'
type Sample = { date?: string; value?: number | null; valid?: boolean; reason?: string; unit?: string }
type Asset = { variable: string; grid_spacing_km?: number; method_version: string }
const props = defineProps<{ position: { lon: number; lat: number }; start: string; end: string; assets: Asset[]; version: string }>()
const emit = defineEmits<{ export: [rows: Record<string, unknown>[]] }>()
const rows = ref<Record<string, Sample[]>>({}), errors = ref<Record<string, string>>({}), loading = ref(false)
const variables = ['lst', 'ndvi', 'soil_moisture']
const names: Record<string, string> = { lst: 'LST 地表温度', ndvi: 'NDVI 植被指数', soil_moisture: 'SMAP 土壤湿度' }
const units: Record<string, string> = { lst: '°C', ndvi: '无量纲', soil_moisture: 'm³/m³' }
const smapSpacing = computed(() => props.assets.find(a => a.variable === 'soil_moisture')?.grid_spacing_km ?? 36)
const methods = computed<Record<string, string>>(() => ({
  lst: 'MOD11A1 · 逐日晴空观测 · 标称 1 km · °C = K − 273.15',
  ndvi: 'MOD13A2 · 16 日合成产品 · 标称 1 km · 当前逐日值为合成期插值',
  soil_moisture: 'SMAP AM 日产品 · 实际有效日期不连续 · ' + smapSpacing.value + ' km 网格间距（不等于独立空间分辨率）',
}))
let controller: AbortController | null = null, epoch = 0
const reason = (p: Sample) => ({ nodata: '源像元缺测或质量筛选未通过', no_published_asset: '当日未发布该变量', no_nearby_valid_smap_cell: '30 km 内没有有效 SMAP 格点', outside_raster: '超出当日栅格范围' }[p.reason ?? ''] ?? p.reason ?? '无有效值')
async function load() {
  controller?.abort(); controller = new AbortController(); const signal = controller.signal, request = ++epoch
  rows.value = {}; errors.value = {}; loading.value = true
  const suffix = new URLSearchParams({lon: String(props.position.lon), lat: String(props.position.lat), start: props.start, end: props.end})
  await Promise.allSettled(variables.map(async variable => {
    try {
      const response = await fetch('/api/v1/series?' + suffix + '&variable=' + variable, {signal})
      if (!response.ok) throw new Error('时序请求失败（' + response.status + '）')
      const doc = await response.json()
      if (request !== epoch) return
      const byDate = new Map<string, Sample>((doc.points ?? []).map((p: Sample) => [p.date!, p]))
      rows.value[variable] = Array.from({length: Math.round((Date.parse(props.end) - Date.parse(props.start)) / 86400000) + 1}, (_, i) => {
        const date = new Date(Date.parse(props.start) + i * 86400000).toISOString().slice(0, 10)
        return byDate.get(date) ?? {date, value: null, valid: false, reason: 'no_published_asset'}
      })
    } catch (error) { if (!signal.aborted && request === epoch) errors.value[variable] = error instanceof Error ? error.message : '时序读取失败' }
  }))
  if (request === epoch) loading.value = false
}
function exportRows() {
  emit('export', variables.flatMap(variable => (rows.value[variable] ?? []).map(p => ({
    ...p,
    longitude: props.position.lon, latitude: props.position.lat, variable, date: p.date, value: p.valid ? p.value : null,
    unit: variable === 'soil_moisture' ? 'm3/m3' : variable === 'ndvi' ? '1' : '°C', valid: p.valid,
    missing_reason: p.valid ? '' : reason(p), sampling_note: methods.value[variable],
    method_version: props.assets.find(a => a.variable === variable)?.method_version,
    data_version: props.version, range_start: props.start, range_end: props.end, exported_at_utc: new Date().toISOString(),
  }))))
}
watch(() => [props.position.lon, props.position.lat, props.start, props.end, props.version], load, {immediate: true})
onBeforeUnmount(() => { epoch++; controller?.abort() })
</script>
<template>
  <section class="multi-series" aria-label="同点多变量时序">
    <div class="multi-heading"><h3>同一位置 · 三种地表变量</h3><button class="subtle-button" :disabled="loading || !Object.keys(rows).length" @click="exportRows">导出三变量 CSV</button></div>
    <p class="multi-note">查询坐标 {{ position.lon.toFixed(4) }}°, {{ position.lat.toFixed(4) }}°；三图共用日期范围，各自保留单位与纵轴。观测频率和分辨率不同，不代表同步观测或逐像元融合。SMAP 每日匹配 30 km 内最近有效原生格点，匹配格点可能随日期变化。</p>
    <section v-for="variable in variables" :key="variable" class="multi-variable" :data-variable="variable">
      <h3>{{ names[variable] }} <span>{{ units[variable] }}</span></h3><p class="multi-method">{{ methods[variable] }}</p>
      <p v-if="errors[variable]" class="error" role="alert">{{ errors[variable] }} <button class="text-button" @click="load">重试</button></p>
      <p v-else-if="!rows[variable]" class="empty-state" role="status">正在读取…</p>
      <template v-else><p class="series-meta">{{ rows[variable].filter(p => p.valid).length }} 个有效日 / {{ rows[variable].length }} 个日历日</p><SeriesChart :points="rows[variable]" :unit="units[variable]" :label="names[variable]" :reason="reason" /></template>
    </section>
    <p class="chart-footnote">缺测处断线；NDVI 合成期插值没有增加独立观测次数。SMAP 网格间距不代表与 MODIS 同等的空间分辨率。</p>
  </section>
</template>
