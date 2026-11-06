/**
 * F1 API client — 按接口约定 v1.2 与 UI 规格 v1.2 §3.2 封装。
 *
 * 硬约定：
 * 1. uri/id/dataset/version 一律查询参数或 JSON body，不做 URL 路径段；
 * 2. 错误体两种形态（处理层 {error,path,method} / 传输层 {error}），统一读 error 字段；
 * 3. 列表参数：POST 走请求体 JSON 真数组；GET 查询串用 JSON 数组写法，禁逗号分隔；
 * 4. cell 固定「宽,高」逗号写法（如 160,100）；
 * 5. version 语义请求一律显式传 version，不依赖 head 缺省；
 * 6. JSON 一律 no-store，不做前端内存缓存跨视图复用；
 * 7. 长任务同步阻塞：timeout ≥ 300s + AbortController（取消只断前端连接，
 *    服务端继续执行落库，文案必须提示"可稍后在任务清单核对结果"）；
 * 8. D7 token：window.__MDVA_TOKEN__ 存在时对 /api 请求加 X-Mdva-Token 头，
 *    /api/health 豁免；401 → token-invalid 事件（全屏阻断页）。
 */

export class ApiError extends Error {
  status: number
  path: string
  method: string

  constructor(status: number, message: string, path = '', method = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.path = path
    this.method = method
  }
}

/** 普通请求超时 */
export const DEFAULT_TIMEOUT = 30_000
/** 长任务超时（契约 v1.2 §4：≥300s） */
export const LONG_TIMEOUT = 300_000

export interface RequestOptions {
  query?: Record<string, unknown>
  body?: Record<string, unknown>
  signal?: AbortSignal
  timeout?: number
}

/** 401 时触发；App 层监听后显示全屏阻断页 */
export const TOKEN_INVALID_EVENT = 'mdva:token-invalid'

function sessionToken(): string {
  const w = window as unknown as { __MDVA_TOKEN__?: string }
  return w.__MDVA_TOKEN__ || ''
}

/** 查询串序列化：数组 → JSON 数组文本（禁逗号拼接），对象 → JSON */
export function buildQuery(query?: Record<string, unknown>): string {
  if (!query) return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    let text: string
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      text = JSON.stringify(value)
    } else if (typeof value === 'object') {
      text = JSON.stringify(value)
    } else if (typeof value === 'boolean') {
      text = value ? 'true' : 'false'
    } else {
      text = String(value)
    }
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(text)}`)
  }
  return parts.length ? `?${parts.join('&')}` : ''
}

async function parseError(res: Response, method: string): Promise<ApiError> {
  let text = `HTTP ${res.status}`
  let path = ''
  try {
    const data = await res.json() as { error?: string; path?: string }
    if (data && typeof data.error === 'string' && data.error) text = data.error
    if (data && typeof data.path === 'string') path = data.path
  } catch {
    // 传输层可能没有 JSON 体；保留状态码文本
  }
  return new ApiError(res.status, text, path, method)
}

/** 统一入口：GET/POST，JSON 响应，no-store */
export async function request<T>(method: 'GET' | 'POST', path: string,
  opts: RequestOptions = {}): Promise<T> {
  const url = `/api${path}${buildQuery(opts.query)}`
  const timeout = opts.timeout ?? DEFAULT_TIMEOUT

  // 超时与外部取消合并到一个 controller
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('timeout')), timeout)
  const onOuterAbort = () => controller.abort()
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort()
    else opts.signal.addEventListener('abort', onOuterAbort, { once: true })
  }

  const headers: Record<string, string> = {}
  const token = sessionToken()
  if (token && path !== '/health') headers['X-Mdva-Token'] = token
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'

  try {
    const res = await fetch(url, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: controller.signal,
      cache: 'no-store',
    })
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent(TOKEN_INVALID_EVENT))
    }
    if (!res.ok) throw await parseError(res, method)
    return await res.json() as T
  } finally {
    clearTimeout(timer)
    if (opts.signal) opts.signal.removeEventListener('abort', onOuterAbort)
  }
}

export function get<T>(path: string, query?: Record<string, unknown>,
  opts: Omit<RequestOptions, 'query' | 'body'> = {}): Promise<T> {
  return request<T>('GET', path, { ...opts, query })
}

export function post<T>(path: string, body?: Record<string, unknown>,
  opts: Omit<RequestOptions, 'query' | 'body'> = {}): Promise<T> {
  return request<T>('POST', path, { ...opts, body: body ?? {} })
}

/** 长任务 POST：≥300s 超时 + 可取消（取消≠终止，服务端会执行完并落库） */
export function postLong<T>(path: string, body?: Record<string, unknown>,
  signal?: AbortSignal): Promise<T> {
  return post<T>(path, body, { timeout: LONG_TIMEOUT, signal })
}

/** PNG 帧直连 URL（<img src>，浏览器缓存 private max-age=30） */
export function frameUrl(uri: string, params: Record<string, unknown> = {}): string {
  return `/api/frame${buildQuery({ uri, ...params })}`
}

/** 拼图 PNG 直连 URL；cell 用「宽,高」逗号写法 */
export function mosaicUrl(uris: string[], params: Record<string, unknown> = {}): string {
  return `/api/mosaic${buildQuery({ uris, ...params })}`
}
