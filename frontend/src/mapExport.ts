import type maplibregl from 'maplibre-gl'
type Key = { label: string; color: string; code: number }
export type MapExport = {
  title: string; date: string; variable: string; unit: string; source: string; version: string;
  colors: string[]; range: number[]; coverage: string; scale: { label: string; pixels: number };
  quality: Key[]; qualityNotes: string[]; boundaryAvailable: boolean
}
function waitMap(map: maplibregl.Map) {
  return new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout>
    const cleanup = () => { clearTimeout(timer); map.off('idle', done); map.off('error', failed) }
    const done = () => { if (!map.loaded() || !map.areTilesLoaded()) return; cleanup(); resolve() }
    const failed = () => { cleanup(); reject(new Error('地图尚有图层加载失败，请重试后再导出。')) }
    map.on('idle', done); map.on('error', failed)
    timer = setTimeout(() => { cleanup(); reject(new Error('地图仍在加载，未导出不完整图面，请稍后重试。')) }, 30000)
    map.triggerRepaint(); done()
  })
}
export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob), link = document.createElement('a')
  link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 2000)
}
function pdfImage(canvas: HTMLCanvasElement) {
  const jpeg = Uint8Array.from(atob(canvas.toDataURL('image/jpeg', .95).split(',')[1]), c => c.charCodeAt(0))
  const landscape = canvas.width > canvas.height, w = landscape ? 841.89 : 595.28, h = landscape ? 595.28 : 841.89
  const factor = Math.min((w - 32) / canvas.width, (h - 32) / canvas.height)
  const iw = canvas.width * factor, ih = canvas.height * factor
  const content = 'q\n' + iw + ' 0 0 ' + ih + ' ' + ((w - iw) / 2) + ' ' + ((h - ih) / 2) + ' cm\n/Im0 Do\nQ'
  const encoder = new TextEncoder(), parts: Uint8Array[] = [], offsets = [0]
  let length = 0
  const append = (value: string | Uint8Array) => { const bytes = typeof value === 'string' ? encoder.encode(value) : value; parts.push(bytes); length += bytes.length }
  append('%PDF-1.4\n')
  const object = (n: number, body: string, binary?: Uint8Array) => {
    offsets[n] = length; append(n + ' 0 obj\n' + body)
    if (binary) { append('\nstream\n'); append(binary); append('\nendstream') }
    append('\nendobj\n')
  }
  object(1, '<< /Type /Catalog /Pages 2 0 R >>')
  object(2, '<< /Type /Pages /Kids [3 0 R] /Count 1 >>')
  object(3, '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ' + w + ' ' + h + '] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>')
  object(4, '<< /Type /XObject /Subtype /Image /Width ' + canvas.width + ' /Height ' + canvas.height + ' /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ' + jpeg.length + ' >>', jpeg)
  object(5, '<< /Length ' + encoder.encode(content).length + ' >>', encoder.encode(content))
  const xref = length
  append('xref\n0 6\n0000000000 65535 f \n' + offsets.slice(1).map(n => String(n).padStart(10, '0') + ' 00000 n \n').join(''))
  append('trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + xref + '\n%%EOF\n')
  return new Blob(parts as BlobPart[], {type: 'application/pdf'})
}
export async function exportMap(map: maplibregl.Map, options: MapExport, type: 'png' | 'pdf', isCurrent = () => true) {
  if (!options.boundaryAvailable) throw new Error('研究区边界尚未加载，暂不能生成完整地图成果。')
  await waitMap(map)
  await new Promise<void>(resolve => { map.once('render', () => resolve()); map.triggerRepaint() })
  if (!isCurrent()) throw new Error('导出等待期间日期、图层或视图已变化，请在地图稳定后重新导出。')
  const original = map.getCanvas(), width = 1600, margin = 60, mapWidth = width - margin * 2
  const mapHeight = Math.round(mapWidth * original.height / original.width)
  const top = 160, footer = 440 + options.quality.length * 20 + options.qualityNotes.length * 38
  const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = top + mapHeight + footer
  const ctx = canvas.getContext('2d')!
  ctx.fillStyle = '#fcfbf7'; ctx.fillRect(0, 0, width, canvas.height)
  const font = (size: number, weight = 400) => { ctx.font = weight + ' ' + size + 'px Microsoft YaHei, Segoe UI, sans-serif'; ctx.textBaseline = 'top' }
  ctx.fillStyle = '#243d35'; font(34, 650); ctx.fillText(options.title, margin, 30)
  font(22); ctx.fillText(options.date + ' · ' + options.variable + '（' + options.unit + '） · 有效比例 ' + options.coverage, margin, 92)
  ctx.drawImage(original, margin, top, mapWidth, mapHeight)
  ctx.strokeStyle = '#8a9a88'; ctx.lineWidth = 1; ctx.strokeRect(margin, top, mapWidth, mapHeight)
  let y = top + mapHeight + 24
  font(21, 600); ctx.fillStyle = '#243d35'; ctx.fillText(options.variable + ' · ' + options.unit, margin, y)
  const scale = options.scale, scaleWidth = scale.pixels * mapWidth / map.getContainer().clientWidth
  if (!scale.label || scaleWidth <= 0) throw new Error('比例尺尚未就绪，请稍后重试。')
  const sx = width - margin - Math.max(scaleWidth, 130)
  ctx.strokeStyle = '#243d35'; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(sx, y + 20); ctx.lineTo(sx, y + 36); ctx.lineTo(sx + scaleWidth, y + 36); ctx.lineTo(sx + scaleWidth, y + 20); ctx.stroke()
  font(20); ctx.fillText(scale.label, sx, y - 3)
  const gradient = ctx.createLinearGradient(margin, 0, margin + 650, 0)
  options.colors.forEach((c, i) => gradient.addColorStop(i / (options.colors.length - 1), c))
  y += 44; ctx.fillStyle = gradient; ctx.fillRect(margin, y, 650, 14); ctx.fillStyle = '#243d35'; font(20)
  y += 24; ctx.fillText(options.range[0].toFixed(2), margin, y); ctx.textAlign = 'right'; ctx.fillText(options.range[1].toFixed(2), margin + 650, y); ctx.textAlign = 'left'
  ctx.strokeStyle = '#526f67'; ctx.lineWidth = 2; ctx.strokeRect(820, y - 20, 28, 18); ctx.fillText('研究区边界', 861, y - 23)
  const wrap = (text: string, x: number, at: number, maxWidth: number, lineHeight = 31) => {
    let line = '', lineY = at
    for (const char of text) { if (ctx.measureText(line + char).width > maxWidth && line) { ctx.fillText(line, x, lineY); line = char; lineY += lineHeight } else line += char }
    if (line) ctx.fillText(line, x, lineY)
    return lineY + lineHeight
  }
  y += 42; font(20)
  y = wrap('透明处为缺测或未通过质量筛选；保留底图，不以观测色填充。', margin, y, mapWidth)
  if (options.quality.length) {
    y = wrap('当前叠加质量诊断：', margin, y + 4, mapWidth)
    let x = margin
    for (const item of options.quality.filter(k => k.code !== 1)) {
      const labelWidth = ctx.measureText(item.label).width + 55
      if (x + labelWidth > width - margin) { x = margin; y += 35 }
      ctx.fillStyle = item.color; ctx.fillRect(x, y, 24, 18); ctx.fillStyle = '#243d35'; ctx.fillText(item.label, x + 33, y - 2); x += labelWidth
    }
    y += 39
    for (const note of options.qualityNotes) y = wrap(note, margin, y, mapWidth)
  }
  y = wrap('数据来源：' + options.source + '；地理底图：Natural Earth。', margin, y + 5, mapWidth)
  y = wrap('数据版本：' + options.version, margin, y + 4, mapWidth)
  const b = map.getBounds()
  y = wrap('图面范围（WGS84）：' + [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => v.toFixed(4)).join(', ') + '；比例尺对应当前视图。', margin, y + 4, mapWidth)
  y = wrap('SGP · 美国南部大平原 · UTC 产品日期；导出时间 ' + new Date().toISOString(), margin, y + 4, mapWidth)
  // Trim unused paper instead of leaving a large blank footer.
  const trimmed = document.createElement('canvas'); trimmed.width = width; trimmed.height = Math.min(canvas.height, y + 35)
  trimmed.getContext('2d')!.drawImage(canvas, 0, 0)
  const name = options.variable + '-' + options.date
  if (type === 'pdf') saveBlob(pdfImage(trimmed), name + '.pdf')
  else {
    const blob = await new Promise<Blob>((resolve, reject) => trimmed.toBlob(b => b ? resolve(b) : reject(new Error('PNG生成失败')), 'image/png'))
    saveBlob(blob, name + '.png')
  }
}
