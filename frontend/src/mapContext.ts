import maplibregl from 'maplibre-gl'

export type GeographicContext = { type: 'FeatureCollection'; bbox?: [number, number, number, number]; features: any[] }
export const emptyContext = (): GeographicContext => ({ type: 'FeatureCollection', features: [] })
export const contextUrl = import.meta.env.BASE_URL + 'basemap/sgp-context.geojson'
// Scientific layers stay below place labels, the mask and analysis overlays.
export const dataLayerAnchor = 'context-place-dots'

function polygons(document: GeographicContext): number[][][][] {
  return document.features.flatMap(feature => {
    const geometry = feature.geometry
    if (geometry?.type === 'Polygon') return [geometry.coordinates]
    if (geometry?.type === 'MultiPolygon') return geometry.coordinates
    return []
  })
}

export function studyBounds(document: GeographicContext) {
  const result = new maplibregl.LngLatBounds()
  for (const polygon of polygons(document)) for (const position of polygon[0]) result.extend([position[0], position[1]])
  if (result.isEmpty()) throw new Error('研究区中没有可用的多边形边界。')
  return result
}

function orientedRing(ring: number[][], clockwise: boolean) {
  const area = ring.reduce((sum, point, index) => {
    const next = ring[(index + 1) % ring.length]
    return sum + point[0] * next[1] - next[0] * point[1]
  }, 0)
  return (area < 0) === clockwise ? ring : [...ring].reverse()
}

export function outsideStudyArea(document: GeographicContext) {
  const studyPolygons = polygons(document)
  const world = [[-180, -85.05112878], [180, -85.05112878], [180, 85.05112878], [-180, 85.05112878], [-180, -85.05112878]]
  return {
    type: 'Feature' as const, properties: {},
    geometry: {
      type: 'MultiPolygon' as const,
      coordinates: [
        [world, ...studyPolygons.map(polygon => orientedRing(polygon[0], true))],
        ...studyPolygons.flatMap(polygon => polygon.slice(1).map(hole => [orientedRing(hole, false)])),
      ],
    },
  }
}

export function fitStudyArea(target: maplibregl.Map | null, bounds: maplibregl.LngLatBoundsLike, duration = 400) {
  if (!target) return
  target.resize()
  const element = target.getContainer()
  const padding = Math.max(22, Math.min(34, Math.round(Math.min(element.clientWidth, element.clientHeight) * .06)))
  target.fitBounds(bounds, { padding, duration, maxZoom: 10 })
}

function labelImage(name: string, state: boolean) {
  // Local fonts keep Chinese and Latin names available without a glyph service.
  // MapLibre still places symbols and resolves collisions.
  const canvas = document.createElement('canvas')
  const context = canvas.getContext('2d')!
  const size = state ? 14 : 13, ratio = 2
  const font = (state ? '600 ' : '500 ') + size + 'px Segoe UI, Microsoft YaHei, sans-serif'
  context.font = font
  const lines = name.split(String.fromCharCode(10)), width = Math.ceil(Math.max(...lines.map(line => context.measureText(line).width))) + 10
  const height = lines.length * (size + 4) + 6
  canvas.width = width * ratio; canvas.height = height * ratio
  context.scale(ratio, ratio); context.font = font
  context.textBaseline = 'top'; context.textAlign = state ? 'center' : 'left'
  context.lineJoin = 'round'; context.lineWidth = 3.5; context.strokeStyle = 'rgba(255,255,255,.94)'
  context.fillStyle = state ? '#69767d' : '#52636c'
  lines.forEach((line, index) => {
    const x = state ? width / 2 : 5, y = 3 + index * (size + 4)
    context.strokeText(line, x, y); context.fillText(line, x, y)
  })
  return context.getImageData(0, 0, canvas.width, canvas.height)
}

export function addGeographicContext(target: maplibregl.Map, data: GeographicContext, study: GeographicContext | null) {
  const features = data.features.map((feature, index) => {
    if (!['place', 'state-label'].includes(feature.properties.kind)) return feature
    const image = 'context-label-' + index
    target.addImage(image, labelImage(feature.properties.label || feature.properties.name, feature.properties.kind === 'state-label'), { pixelRatio: 2 })
    return { ...feature, properties: { ...feature.properties, label_image: image } }
  })
  target.addSource('geographic-context', { type: 'geojson', data: { type: 'FeatureCollection', features }, attribution: 'Natural Earth · 本地地理底图' })
  target.addLayer({ id: 'context-lakes', type: 'fill', source: 'geographic-context', filter: ['==', 'kind', 'lake'], paint: { 'fill-color': '#c8dfe9', 'fill-opacity': 1 } })
  target.addLayer({ id: 'context-rivers', type: 'line', source: 'geographic-context', filter: ['==', 'kind', 'river'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': '#b9d6e4', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, .6, 8, 1.2, 12, 2.2] } })
  target.addLayer({ id: 'context-administration', type: 'line', source: 'geographic-context', filter: ['==', 'kind', 'administration'], paint: { 'line-color': '#bfc7cc', 'line-width': 1, 'line-opacity': .8 } })
  target.addLayer({ id: dataLayerAnchor, type: 'circle', source: 'geographic-context', minzoom: 5, filter: ['==', 'kind', 'place'], paint: { 'circle-radius': 2, 'circle-color': '#73838c', 'circle-stroke-color': '#fff', 'circle-stroke-width': 1 } })
  target.addLayer({ id: 'context-state-labels', type: 'symbol', source: 'geographic-context', filter: ['==', 'kind', 'state-label'], layout: { 'icon-image': ['get', 'label_image'], 'icon-padding': 18, 'symbol-sort-key': ['get', 'rank'] }, paint: { 'icon-opacity': .88 } })
  target.addLayer({ id: 'context-place-labels', type: 'symbol', source: 'geographic-context', minzoom: 5, filter: ['==', 'kind', 'place'], layout: { 'icon-image': ['get', 'label_image'], 'icon-anchor': 'left', 'icon-offset': [6, 0], 'icon-padding': 8, 'symbol-sort-key': ['get', 'rank'] } })
  if (study) {
    target.addSource('study', { type: 'geojson', data: study })
    target.addSource('outside-study', { type: 'geojson', data: outsideStudyArea(study) })
    target.addLayer({ id: 'outside-study-mask', type: 'fill', source: 'outside-study', paint: { 'fill-color': '#ffffff', 'fill-opacity': .5, 'fill-antialias': false } })
    target.addLayer({ id: 'study-halo', source: 'study', type: 'line', layout: { 'line-join': 'round' }, paint: { 'line-color': '#ffffff', 'line-width': 4.5, 'line-opacity': .6, 'line-blur': 1 } })
    target.addLayer({ id: 'study-outline', source: 'study', type: 'line', layout: { 'line-join': 'round' }, paint: { 'line-color': '#526f67', 'line-width': 1.1, 'line-opacity': .95 } })
  }
}
