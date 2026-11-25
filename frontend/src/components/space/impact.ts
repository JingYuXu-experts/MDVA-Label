import type { CmeAnalysis, SceneEvent } from './sceneMath'
import type { WindSample } from './physics'

export interface PropagationModel {
  simulationID?: string
  modelCompletionTime?: string | null
  estimatedShockArrivalTime?: string | null
  isEarthGB?: boolean
  isEarthMinorImpact?: boolean
  estimatedDuration?: number | null
  cmeInputs?: { CMEID?: string; activityID?: string }[] | null
  cmeIDs?: string[] | null
  impactList?: { location?: string; arrivalTime?: string | null; isGlancingBlow?: boolean; isMinorImpact?: boolean }[] | null
  link?: string
}
export interface SolarEvent extends SceneEvent {
  cmeAnalyses?: (CmeAnalysis & { enlilList?: PropagationModel[] | null })[] | null
  sourceLocation?: string | null
  activeRegionNum?: number | null
  note?: string
  link?: string
}
export function associatedModels(event: SolarEvent | null, simulations: PropagationModel[]) {
  if (!event) return []
  const matches = simulations.filter(s => s.cmeInputs?.some(c => (c.CMEID || c.activityID) === event.activityID) || s.cmeIDs?.includes(event.activityID))
  const embedded = event.cmeAnalyses?.flatMap(a => a.enlilList || []) || []
  const unique = new Map<string, PropagationModel>()
  for (const model of [...embedded, ...matches]) {
    const key = /WSA-ENLIL\/(\d+)/.exec(model.simulationID || model.link || '')?.[1] || `${model.modelCompletionTime}|${model.link}`
    const previous = unique.get(key)
    if (!previous || (Date.parse(model.modelCompletionTime || '') || 0) >= (Date.parse(previous.modelCompletionTime || '') || 0)) unique.set(key, model)
  }
  return [...unique.values()].sort((a,b) => (Date.parse(b.modelCompletionTime || '') || 0) - (Date.parse(a.modelCompletionTime || '') || 0))
}
export function earthArrival(model: PropagationModel): string | null {
  const earth = model.impactList?.find(i => i.location?.trim().toLowerCase() === 'earth')
  const candidate = model.estimatedShockArrivalTime || earth?.arrivalTime
  return candidate && Number.isFinite(Date.parse(candidate)) ? candidate : null
}
export function earthImpact(event: SolarEvent | null, simulations: PropagationModel[], now: number) {
  const models = associatedModels(event, simulations)
  const model = models[0]
  if (!event) return { state: 'unselected', title: '先选择一个喷发事件', detail: '背景太阳风一直存在；具体CME是否影响地球，要查看对应传播模拟。', models, arrival: null, model: undefined, probability: null }
  if (!model) return { state: 'unknown', title: '影响地球与否：资料不足', detail: '尚未取得明确关联的传播模拟；不能用同一天发生或朝向看起来接近来代替关联。', models, arrival: null, model: undefined, probability: null }
  const earth = model.impactList?.find(i => i.location?.trim().toLowerCase() === 'earth')
  // In DONKI's WSA–ENLIL product the top-level estimate is for Earth;
  // arrivalTime inside impactList belongs ONLY to its explicitly named target.
  const arrival = earthArrival(model)
  if (arrival) return { state: Date.parse(arrival)/1000 <= now ? 'awaiting_confirmation' : 'earth_arrival',
    title: model.isEarthGB || earth?.isGlancingBlow ? '研究模拟：可能擦过地球' : '研究模拟：给出了地球到达估计',
    detail: Date.parse(arrival)/1000 <= now ? '预计时刻已经过去，仍需用近地观测核对，不能自动宣布已到达。' : '这是模型的冲击到达估计；不等同于已确认影响，也不是具体设备故障时间。',
    models, arrival, model, probability: null }
  return { state: 'no_earth_estimate', title: '该模拟未给出地球到达',
    detail: '不表示已经证明“完全没有影响”；可能偏离地球，也可能分析资料不足。不能把其他行星或探测器的到达时间用给地球。',
    models, arrival: null, model, probability: null }
}

export function upstreamIntake(wind: WindSample, now: number) {
  const age = wind.observedAt == null ? Infinity : now-wind.observedAt
  const fresh = age >= -120 && age <= 900
  const valid = fresh && Number.isFinite(wind.speed) && wind.speed! > 0 && Number.isFinite(wind.density) && wind.density! >= 0
  return { fresh, valid, fluxPerSquareMetreSecond: valid ? wind.density!*wind.speed!*1e9 : null,
    pressureNpa: valid ? 1.67262192369e-6*wind.density!*wind.speed!**2 : null }
}
