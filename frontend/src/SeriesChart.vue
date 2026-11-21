<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
type Sample = { date?: string; value?: number | null; valid?: boolean; reason?: string }
const props = defineProps<{ points: Sample[]; unit: string; label: string; reason: (sample: Sample) => string }>()
const active = ref<number | null>(null)
const frame = ref<HTMLElement | null>(null), width = ref(610)
let observer: ResizeObserver | null = null
onMounted(() => {
  observer = new ResizeObserver(([entry]) => { width.value = Math.max(280, entry.contentRect.width) })
  if (frame.value) observer.observe(frame.value)
})
onBeforeUnmount(() => observer?.disconnect())
const format = (v: number) => v.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
const chart = computed(() => {
  const values = props.points.filter(p => p.valid && p.value != null).map(p => p.value!)
  const min = values.length ? Math.min(...values) : 0, max = values.length ? Math.max(...values) : 1
  const spread = max - min || Math.max(Math.abs(max) * .1, .1), lo = min - spread * .15, hi = max + spread * .15
  const tickCount = Math.min(width.value < 500 ? 3 : 4, props.points.length)
  const tickIndices = new Set(Array.from({ length: tickCount }, (_, i) => Math.round(i * (props.points.length - 1) / Math.max(1, tickCount - 1))))
  const dots = props.points.map((p, i) => ({ ...p, showTick: tickIndices.has(i), x: 64 + i * (width.value - 108) / Math.max(1, props.points.length - 1), y: p.valid && p.value != null ? 172 - (p.value - lo) / (hi - lo) * 124 : null }))
  const paths: string[] = []; let segment = ''
  for (const p of dots) { if (p.y === null) { if (segment) paths.push(segment); segment = ''; continue }; segment += (segment ? ' L ' : 'M ') + p.x + ' ' + p.y }
  if (segment) paths.push(segment)
  return { dots, paths, lo, hi, hasValues: values.length > 0, tickDigits: Math.max(2, Math.min(6, Math.ceil(-Math.log10((hi - lo) / 2)) + 1)), ticks: [0, .5, 1].map(t => ({ y: 172 - t * 124, value: lo + t * (hi - lo) })) }
})
const focused = computed(() => active.value == null ? null : chart.value.dots[active.value])
function move(event: PointerEvent) { const box = (event.currentTarget as SVGSVGElement).getBoundingClientRect(); active.value = Math.max(0, Math.min(props.points.length - 1, Math.round(((event.clientX - box.left) / box.width * width.value - 64) / (width.value - 108) * Math.max(1, props.points.length - 1)))) }
function key(event: KeyboardEvent) { if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); active.value = Math.max(0, Math.min(props.points.length - 1, (active.value ?? 0) + (event.key === 'ArrowRight' ? 1 : -1))) } }
watch(() => props.points, () => { active.value = null })
</script>
<template>
  <div ref="frame" class="chart-wrap">
    <svg class="series-chart" :viewBox="'0 0 ' + width + ' 242'" tabindex="0" role="img" :aria-label="label + '（' + unit + '）时序图，横轴为产品日期（UTC），缺测处断开。左右方向键查看每日数值。'" @pointerdown="move" @pointermove="move" @pointerleave="active = null" @focus="active = 0" @blur="active = null" @keydown="key">
      <text x="64" y="23" class="axis-title">{{ label }}（{{ unit }}）</text>
      <g v-for="tick in chart.ticks" :key="tick.y"><line x1="64" :x2="width - 44" :y1="tick.y" :y2="tick.y" class="chart-grid"/><text x="52" :y="tick.y + 4" text-anchor="end" class="axis-label">{{ chart.hasValues ? tick.value.toLocaleString('zh-CN', { maximumFractionDigits: chart.tickDigits }) : '—' }}</text></g>
      <path :d="'M 64 44 V 172 H ' + (width - 44)" class="chart-axis"/>
      <path v-for="(path, i) in chart.paths" :key="i" :d="path" class="chart-line"/>
      <g v-for="dot in chart.dots" :key="dot.date"><circle v-if="dot.y !== null" :cx="dot.x" :cy="dot.y" r="2.7" class="chart-dot"/><template v-if="dot.showTick"><line :x1="dot.x" :x2="dot.x" y1="172" y2="178" class="chart-axis"/><text :x="dot.x" y="200" text-anchor="middle" class="axis-label date-tick">{{ dot.date }}</text></template><line v-if="dot.y === null" :x1="dot.x" :x2="dot.x" y1="183" y2="187" class="missing-mark"/></g>
      <text :x="width / 2" y="233" text-anchor="middle" class="axis-title">产品日期（UTC）</text>
      <template v-if="focused"><line :x1="focused.x" :x2="focused.x" y1="44" y2="172" class="chart-crosshair"/><circle v-if="focused.y !== null" :cx="focused.x" :cy="focused.y" r="5" class="chart-dot active-dot"/></template>
    </svg>
    <div class="chart-tooltip" role="status"><template v-if="focused"><strong>{{ focused.date }}</strong><span>{{ focused.valid && focused.value != null ? format(focused.value) + ' ' + unit : '缺测 · ' + reason(focused) }}</span></template><template v-else>悬停、轻触或使用左右方向键查看完整日期与数值；横轴下的短线标记缺测日期。</template></div>
  </div>
</template>
