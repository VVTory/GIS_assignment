<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
type Item = { id: string; date: string }
type Coverage = { valid_fraction: number | null }
const props = defineProps<{ assets: Item[]; selected: string; version: string; variable: string; unavailable: Record<string, string> }>()
const emit = defineEmits<{ select: [id: string] }>()
const month = ref(''), highOnly = ref(false), threshold = ref(70), notice = ref(''), loading = ref(false)
const ratios = ref<Record<string, number | null>>({}), errors = ref<string[]>([])
const cache = new Map<string, Coverage>()
let epoch = 0, controller: AbortController | null = null
const months = computed(() => [...new Set(props.assets.map(a => a.date.slice(0, 7)))].sort())
const index = computed(() => months.value.indexOf(month.value))
const monthly = computed(() => props.assets.filter(a => a.date.startsWith(month.value)))
const qualifying = computed(() => monthly.value.filter(a => ratios.value[a.id] != null && ratios.value[a.id]! * 100 >= threshold.value))
const cells = computed(() => {
  if (!month.value) return []
  const [year, m] = month.value.split('-').map(Number)
  const offset = (new Date(year, m - 1, 1).getDay() + 6) % 7
  return [...Array(offset).fill(null), ...Array.from({ length: new Date(year, m, 0).getDate() }, (_, i) => {
    const date = month.value + '-' + String(i + 1).padStart(2, '0')
    const asset = monthly.value.find(a => a.date === date), ratio = asset ? ratios.value[asset.id] : undefined
    const filtered = !!asset && highOnly.value && (ratio == null || ratio * 100 < threshold.value)
    const reason = !asset ? props.unavailable[date] || '当天没有已发布数据；可能未下载或不在产品可用时段。' : filtered ? ratio == null ? '覆盖率尚不可用，暂不纳入高覆盖筛选。' : '低于当前高覆盖阈值；可关闭筛选查看。' : ''
    return { date, day: i + 1, asset, ratio, filtered, reason }
  })]
})
const percent = (ratio: number | null | undefined) => ratio == null ? '待查' : ratio > 0 && ratio < .001 ? '<0.1%' : (ratio * 100).toFixed(1) + '%'
function choose(cell: NonNullable<typeof cells.value[number]>) {
  if (!cell.asset || cell.filtered) { notice.value = cell.date + '：' + cell.reason; return }
  notice.value = ''; emit('select', cell.asset.id)
}
async function loadMonth() {
  controller?.abort(); controller = new AbortController(); const signal = controller.signal, request = ++epoch
  loading.value = true; errors.value = []; notice.value = ''
  const queue = [...monthly.value], failed: string[] = []
  await Promise.all(Array.from({ length: Math.min(4, queue.length) }, async () => {
    while (queue.length && !signal.aborted) {
      const asset = queue.shift()!, key = props.version + '/' + asset.id
      try {
        let result = cache.get(key)
        if (!result) { const response = await fetch('/api/v1/assets/' + encodeURIComponent(asset.id) + '/coverage', { signal }); if (!response.ok) throw new Error('coverage'); result = await response.json() as Coverage; cache.set(key, result) }
        if (request === epoch) ratios.value[asset.id] = result.valid_fraction
      } catch { if (!signal.aborted) failed.push(asset.id) }
    }
  }))
  if (request === epoch) { errors.value = failed; loading.value = false }
}
watch(() => [props.selected, props.variable], () => {
  const selected = props.assets.find(a => a.id === props.selected)
  if (selected) month.value = selected.date.slice(0, 7)
  else if (!months.value.includes(month.value)) month.value = months.value[0] ?? ''
}, { immediate: true })
watch(() => props.version, () => { ratios.value = {}; cache.clear() })
watch([highOnly, threshold], () => { notice.value = '' })
watch(() => [month.value, props.variable, props.version], () => { void loadMonth() }, { immediate: true })
onBeforeUnmount(() => { epoch++; controller?.abort() })
</script>

<template>
  <section class="date-calendar" aria-label="按月选择数据日期">
    <div class="calendar-heading"><h3>观测日历</h3><span>{{ assets.length }} 个已发布日期</span></div>
    <p class="calendar-current">当前图层：{{ assets.find(a => a.id === selected)?.date ?? '—' }}</p>
    <div class="month-navigation"><button aria-label="上一个有数据的月份" :disabled="index <= 0" @click="month = months[index - 1]">‹</button><select v-model="month" aria-label="浏览月份"><option v-for="m in months" :key="m" :value="m">{{ m.replace('-', ' 年 ') }} 月</option></select><button aria-label="下一个有数据的月份" :disabled="index >= months.length - 1" @click="month = months[index + 1]">›</button></div>
    <label class="high-coverage-toggle"><input v-model="highOnly" type="checkbox">优先显示高覆盖日期</label>
    <div v-if="highOnly" class="coverage-threshold"><label for="threshold">最低{{ variable === 'soil_moisture' ? '有效格点比例' : '覆盖率' }}</label><select id="threshold" v-model.number="threshold"><option :value="30">30%</option><option :value="50">50%</option><option :value="70">70%</option><option :value="90">90%</option></select></div>
    <div class="calendar-grid"><span v-for="day in ['一','二','三','四','五','六','日']" :key="day" class="weekday">{{ day }}</span><template v-for="(cell, i) in cells" :key="cell?.date ?? i"><span v-if="!cell"></span><button v-else :class="['calendar-day', { available: cell.asset, selected: cell.asset?.id === selected, filtered: cell.filtered, unavailable: !cell.asset, low: cell.ratio != null && cell.ratio < .3, high: cell.ratio != null && cell.ratio >= .7 }]" :data-date="cell.date" :aria-pressed="cell.asset?.id === selected" :aria-disabled="!cell.asset || cell.filtered" :aria-label="cell.date + '，' + (cell.reason || (variable === 'soil_moisture' ? '有效格点比例 ' : '覆盖率 ') + percent(cell.ratio))" :title="cell.date + '：' + (cell.reason || percent(cell.ratio))" @click="choose(cell)"><strong>{{ cell.day }}</strong><small>{{ cell.asset ? percent(cell.ratio) : '—' }}</small></button></template></div>
    <div class="calendar-key" aria-label="有效比例颜色说明"><span class="high-key">≥70% 高覆盖</span><span class="low-key">&lt;30% 低覆盖</span><span>无数据</span></div>
    <p class="calendar-status" role="status">{{ loading ? '正在读取本月覆盖率…' : highOnly ? '本月 ' + qualifying.length + ' 天达到阈值；地图保留当前所选日期。' : '日期下方为有效比例，边框标记当前日期。' }}</p>
    <p v-if="highOnly && !loading && !qualifying.length" class="warning">本月没有达到阈值的日期，可降低阈值或切换月份。</p>
    <p v-if="notice" class="date-notice" role="status">{{ notice }}</p>
    <p v-if="errors.length" class="error">{{ errors.length }} 天覆盖率读取失败，未视为零。<button class="text-button" @click="loadMonth">重试</button></p>
    <p v-if="variable === 'soil_moisture'" class="date-hint">SMAP 显示原生格点有效比例，不代表面积覆盖率。</p>
  </section>
</template>
