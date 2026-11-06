/**
 * 端点函数层：每个函数对应契约 v1.2 §5 的一个端点，参数默认值按 §6 全量表显式书写。
 * version 语义请求一律显式传 version（§2.8）；列表参数走 JSON 真数组（§2.4）。
 */
import { get, post, postLong, buildQuery } from './client'
export { frameUrl, mosaicUrl } from './client'
import type {
  HealthResp, WorkspaceRow, DatasetRow, VersionRow, LabelDef, ItemRow,
  StatsResp, ProgressResp, ScanResp, ThumbsResp, TaskRow, AnnRow,
  CollectResp, FrameMeta, PreAnnotateResp,
  CleanPreviewResp, CleanRollbackResp, DroppedRow,
  AugRecipesResp, AugSuggestResp, AugCheckResp, AugRunResp, AugReplayResp, AugRecipe,
  AugLineageRow,
} from './types'

// ---- 5.1 元信息 ----
export const getHealth = () => get<HealthResp>('/health')
export const getWorkspaces = () => get<{ workspaces: WorkspaceRow[] }>('/workspaces')
export const rescanWorkspaces = () => post<{ n: number; workspaces: WorkspaceRow[] }>('/workspaces/rescan')
export const getProbe = () => get<Record<string, unknown>>('/probe')
export const getOperators = (kind: 'analysis' | 'clean' | 'augment' | 'export' = 'analysis') =>
  get<Record<string, unknown>>('/operators', { kind })

// ---- 5.2 数据集与条目 ----
export const getDatasets = () => get<{ datasets: DatasetRow[] }>('/datasets')
export const createDataset = (name: string, root: string, type = 'detection',
  category = '', labels: (string | LabelDef)[] = []) =>
  post<{ dataset_id: string; version_id: string; detail: Record<string, unknown> }>(
    '/datasets', { name, root, type, category, labels })
export const getDataset = (dataset: string) =>
  get<Record<string, unknown>>('/dataset', { dataset })
export const setLabels = (dataset: string, labels: (string | LabelDef)[]) =>
  post<{ labels: LabelDef[] }>('/labels', { dataset, labels })
export const getVersions = (dataset: string) =>
  get<{ versions: VersionRow[]; head: string }>('/versions', { dataset })
export const getManifest = (dataset: string) =>
  get<Record<string, unknown>>('/manifest', { dataset })
export const scanRoot = (root: string, recursive = true, limit = 2000,
  signal?: AbortSignal) =>
  get<ScanResp>('/scan', { root, recursive, limit }, { signal })
export const getItems = (dataset: string, version: string,
  filter: { state?: string; modality?: string } = {}, signal?: AbortSignal) =>
  get<{ items: ItemRow[]; n: number }>('/items',
    { dataset, version, state: filter.state ?? '', modality: filter.modality ?? '' },
    { signal })
export const getStats = (dataset: string, version: string) =>
  get<StatsResp>('/stats', { dataset, version })

// ---- 5.3 出图 ----
export const getFrameMeta = (uri: string, params: Record<string, unknown> = {}) =>
  get<FrameMeta>('/frame/meta', { uri, ...params })
export const getThumbs = (uris: string[], cell: [number, number],
  workers = 0, signal?: AbortSignal) =>
  get<ThumbsResp>('/thumbs', { uris, cell: `${cell[0]},${cell[1]}`, workers }, { signal })
/** 拼图 PNG 直连 URL（GET，<img> 用） */
export const mosaicSrc = (uris: string[], cell: [number, number] = [320, 200],
  params: Record<string, unknown> = {}) =>
  `/api/mosaic${buildQuery({ uris, cell: `${cell[0]},${cell[1]}`, ...params })}`

// ---- 5.5 标注与审核 ----
export const getAnnotations = (version: string, uri = '', signal?: AbortSignal) =>
  get<{ annotations: AnnRow[]; n: number }>('/annotations', { version, uri }, { signal })
export const putAnnotations = (version: string, uri: string, anns: unknown[],
  role: string, state = 'draft', replace = true) =>
  post<{ version_id: string; uri: string; ids: string[]; removed: number; n: number }>(
    '/annotations', { version, uri, anns, role, state, replace })
export const setAnnState = (annotation_id: string, role: string, action: string, reason = '') =>
  post<{ annotation_id: string; state: string; role: string; action: string }>(
    '/annotations/state', { annotation_id, role, action, reason })
export const submitItem = (version: string, uri: string, role = 'annotator') =>
  post<{ uri: string; n_submitted: number }>('/annotations/submit', { version, uri, role })
/** action 必须显式传，禁止吃服务端默认 approve（契约 §5.5） */
export const reviewBulk = (version: string, action: 'approve' | 'reject',
  role = 'reviewer', reason = '', max_confidence?: number | null) =>
  post<{ n: number; action: string }>('/annotations/review',
    { version, role, action, reason, max_confidence: max_confidence ?? null })
export const getQueue = (version: string, role = 'reviewer') =>
  get<{ version_id: string; role: string; queue: string[] }>('/queue', { version, role })
export const getProgress = (version: string) =>
  get<ProgressResp>('/progress', { version })
export const getReviewLog = (version = '', annotation_id = '') =>
  get<{ log: Record<string, unknown>[] }>('/review_log', { version, annotation_id })
export const getDiff = (v1: string, v2: string) =>
  get<Record<string, unknown>>('/diff', { v1, v2 })
export const getLineage = (version: string) =>
  get<{ lineage: Record<string, unknown>[] }>('/lineage', { version })

// ---- 5.10 收录与任务 ----
export const collect = (uris: string[], target: string, mode = 'copy', flatten = false) =>
  post<CollectResp>('/collect', { uris, target, mode, flatten })
export const collectUndo = (manifest: string, keep_dirs = true, force = false) =>
  post<Record<string, unknown>>('/collect/undo', { manifest, keep_dirs, force })
export const collectLatest = (target: string) =>
  get<Record<string, unknown>>('/collect/latest', { target })
export const getTasks = (kind = '', dataset = '') =>
  get<{ tasks: TaskRow[]; n: number }>('/tasks', { kind, dataset })

// ---- 长任务（同步阻塞，postLong：≥300s + 可取消） ----
export const runCleanApply = (version: string, specs: unknown[], uris: string[],
  calibration: unknown = null, write_transformed = false, author = '', signal?: AbortSignal) =>
  postLong<Record<string, unknown>>('/clean/apply',
    { version, specs, uris, calibration, write_transformed, author }, signal)
/** 预览与执行同一条 evaluate()（契约 §5.7）；条目多时走 POST，避免查询串过长。 */
export const cleanPreview = (uris: string[], specs: unknown[], calibration = '', signal?: AbortSignal) =>
  post<CleanPreviewResp>('/clean/preview', { uris, specs, calibration }, { signal })
/** version 必填（契约 §5.7：回退目标是哪个清洗版本，不吃当前上下文默认） */
export const cleanRollback = (version: string, signal?: AbortSignal) =>
  postLong<CleanRollbackResp>('/clean/rollback', { version }, signal)
export const cleanDropped = (version: string) =>
  get<{ dropped: DroppedRow[] }>('/clean/dropped', { version })
export const runAugment = (version: string, recipe: AugRecipe,
  uris: string[], author = '', signal?: AbortSignal) =>
  postLong<AugRunResp>('/augment/run', { version, recipe, uris, author }, signal)

// ---- 5.7 增强（配方元数据 / 推荐 / 告警检查 / 谱系 / 复现）----
/** 三 Tab 的唯一数据源：ops（算子表）+ presets（学科配方）+ task_presets（任务类型→预设） */
export const getAugmentRecipes = (signal?: AbortSignal) =>
  get<AugRecipesResp>('/augment/recipes', {}, { signal })
/** dataset 显式传（契约 §2.8）；返回可直接用的 preset + recipe + 为什么这么配 */
export const augmentSuggest = (dataset: string, signal?: AbortSignal) =>
  post<AugSuggestResp>('/augment/suggest', { dataset }, { signal })
/** 执行前强制发生的告警检查：category 传数据集学科，才能命中"星图上下镜像"这类学科约束 */
export const augmentCheck = (uris: string[], recipe: AugRecipe, category = '',
  signal?: AbortSignal) =>
  post<AugCheckResp>('/augment/check', { uris, recipe, category }, { signal })
/** 谱系逐行 = run 落盘的 written 记录（含 written 相对 uri，after 帧直接走 /api/frame） */
export const augmentLineage = (version: string, signal?: AbortSignal) =>
  get<{ lineage: AugLineageRow[] }>('/augment/lineage', { version }, { signal })
/** version 必填（不吃上下文默认）；校验的是 seed 谱系链，不是重算像素 */
export const augmentReplay = (version: string, signal?: AbortSignal) =>
  postLong<AugReplayResp>('/augment/replay', { version }, signal)
export const runExport = (version: string, format: string, params: Record<string, unknown> = {},
  out_dir = '', scripts = false, signal?: AbortSignal) =>
  postLong<Record<string, unknown>>('/export',
    { version, format, params, out_dir, scripts }, signal)
export const runBench = (root: string, categories = '', only: string[] = [],
  limit = 4, stubs = false, signal?: AbortSignal) =>
  postLong<Record<string, unknown>>('/bench',
    { root, categories, only, limit, stubs }, signal)
export const runActiveRun = (version: string, uris: string[],
  params: Record<string, unknown> = {}, signal?: AbortSignal) =>
  postLong<Record<string, unknown>>('/smart/active_run', { version, uris, ...params }, signal)

// ---- 5.8 智能标注（§7 参数链：apply 返回新 version_id，undo 绑定它）----
export const smartSameImage = (uri: string, params: Record<string, unknown> = {}) =>
  post<{ uri: string; anns: AnnRow['ann'][]; n: number }>('/smart/same_image', { uri, ...params })
export const smartPreannotate = (uris: string[], params: Record<string, unknown> = {}) =>
  post<PreAnnotateResp>('/smart/preannotate', { uris, ...params })
/** apply 落新版本，响应 version_id 必须保留供 undo 绑定（契约 §7） */
export const smartApply = (version: string, uris: string[], params: Record<string, unknown> = {}) =>
  postLong<PreAnnotateResp>('/smart/apply', { version, uris, ...params })
export const smartUndo = (version: string, role = 'annotator') =>
  post<Record<string, unknown>>('/smart/undo', { version, role })
export const smartSpeedup = (uris: string[], params: Record<string, unknown> = {}, signal?: AbortSignal) =>
  get<Record<string, unknown>>('/smart/speedup', { uris, ...params }, { signal })
export const smartHard = (version: string, uris: string[], params: Record<string, unknown> = {}) =>
  get<Record<string, unknown>>('/smart/hard', { version, uris, ...params })
export const smartActiveRound = (version: string, uris: string[], params: Record<string, unknown> = {}) =>
  post<Record<string, unknown>>('/smart/active_round', { version, uris, ...params })
export const smartHistory = (version: string) =>
  get<{ rounds: Record<string, unknown>[] }>('/smart/history', { version })
