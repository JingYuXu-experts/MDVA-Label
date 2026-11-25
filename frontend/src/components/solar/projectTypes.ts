export interface Project {
  id: string
  name: string
  managed: boolean
  pinned: boolean
  opened_at: string | null
  opens: number
}
export type Channel = 'AR' | 'CH'
export interface Photo {
  last_batch?: string
  reference_uri?: string
  id: string
  name: string
  width: number
  height: number
  category: Channel | 'unsupported' | ''
  observed_utc: string | null
  instrument: string | null
}
export interface Batch {
  id: string
  created: string
  total: number
}
export interface Forecast {
  state: string
  message?: string
  observed_utc?: string
  end_utc?: string
  target?: string
  probability?: number
  source?: string
  satellite?: number
  time_basis?: string
  cutoff_utc?: string
}
export interface Hardware {
  id: string
  name: string
  device: 'cpu' | 'cuda'
  index?: number
  available: boolean
  reason?: string
}
export interface Estimate {
  seconds: number | null
  finish_utc: string | null
  ahead: number
  devices: {id: string; name: string; ahead: number; remaining: number; seconds: number | null; blocked: boolean}[]
}
export interface Row {
  id: string
  job: string
  index: number
  category: Channel
  state: string
  seconds: number | null
  device?: string
  device_id?: string
  hardware?: string
  error?: string
  observed_utc?: string
  forecast?: Forecast
}
export const channelNames = {
  AR: '活动区 · HMI',
  CH: '冕洞 · AIA 193 Å',
  unsupported: '仅供浏览',
  '': '待确认通道',
}
export const stateNames: Record<string, string> = {
  queued: '排队中',
  waiting_gpu: '等待 GPU',
  running: '识别中',
  completed: '已完成',
  failed: '失败',
}
export function duration(seconds: number) {
  return seconds < 60 ? Math.max(1, Math.ceil(seconds)) + ' 秒' : Math.ceil(seconds / 60) + ' 分钟'
}
export function utc(value?: string | null) {
  return value ? new Date(value).toISOString().slice(0, 19).replace('T', ' ') + ' UTC' : '观测时间未知'
}

export function localFinish(estimate: Estimate) {
  return estimate.finish_utc
    ? new Date(estimate.finish_utc).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit'})
    : ''
}

export function estimateText(value?: Estimate) {
  if (!value) return '正在估算…'
  if (value.devices.some(d => d.blocked)) return '等待显卡释放，暂无法估算'
  return value.seconds == null ? '等待该设备的实测速度' : '约 ' + duration(value.seconds)
}
