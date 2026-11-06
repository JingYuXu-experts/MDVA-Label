/** 服务端响应类型（依据 services.py / store.py 实测返回结构，契约 v1.2 §5） */

export interface HealthResp {
  ok: boolean
  app: string
  version: string
  root: string
  workspaces: string[]
  default: string
  out_dir: string
  offline: boolean
}

export interface WorkspaceRow {
  name: string
  root?: string
  datasets?: DatasetRow[]
  [key: string]: unknown
}

export interface DatasetRow {
  id: string
  name: string
  type: string
  root: string
  meta: Record<string, unknown>
  created: number | null
  n_versions: number
  head: string
  n_annotations: number
  head_annotations: number
}

export interface VersionRow {
  id: string
  dataset_id: string
  parent_id: string | null
  op: string
  params: Record<string, unknown>
  created: number
  n_annotations: number
  states: Record<string, number>
}

export interface LabelDef {
  name: string
  color?: string
  [key: string]: unknown
}

export type AnnState = 'draft' | 'submitted' | 'approved' | 'rejected'
export type Role = 'admin' | 'annotator' | 'reviewer'
export type Modality = 'image' | 'cube' | 'series' | 'event' | 'video' | ''

export interface ItemRow {
  uri: string
  name: string
  modality: Modality | string
  n_annotations: number
  states: Record<AnnState, number>
  sources: string[]
  labels: string[]
  machine_only: boolean
}

export interface StatsResp {
  version_id: string
  n_annotations: number
  n_items: number
  n_items_on_disk: number
  labeled_ratio: number
  states: Record<string, number>
  sources: Record<string, number>
  labels: Record<string, number>
  n_reviews: number
}

export interface ProgressResp {
  total: number
  by_state: Record<string, number>
  approved: number
  ratio: number
}

export interface ScanResp {
  root: string
  n_items: number
  modalities: Record<string, number>
  t_span: [number, number] | null
  elapsed_ms: number
  items: { uri: string; name: string; modality: string; [key: string]: unknown }[]
}

export interface ThumbRow {
  uri: string
  /** base64 PNG；坏图为空串并带 error */
  png: string
  modality?: string
  subtitle?: string
  error?: string
}

export interface ThumbsResp {
  thumbs: ThumbRow[]
  workers: number
  elapsed_ms: number
  n_errors: number
}

export interface TaskRow {
  id: string
  kind: string
  dataset_id: string | null
  version_in: string | null
  version_out: string | null
  params: Record<string, unknown>
  state?: string
  created?: number
  [key: string]: unknown
}

export interface AnnRow {
  id: string
  item_uri: string
  state: AnnState
  ann: {
    geom: string
    coords: number[]
    label: string
    source: string
    confidence?: number
    [key: string]: unknown
  }
  [key: string]: unknown
}

export interface CollectResp {
  manifest: string
  [key: string]: unknown
}

export interface FrameMeta {
  modality: string
  width: number
  height: number
  cached: boolean | number
  png_bytes?: number
  [key: string]: unknown
}

/** 整批预标注响应（干跑与 apply 共用；apply 追加 version_id/parent/n_written/carried） */
export interface PreAnnotateResp {
  n_items: number
  n_annotations: number
  n_ok: number
  errors: Record<string, unknown>
  speedup: number
  workers: number
  by_uri: Record<string, AnnRow['ann'][]>
  version_id?: string
  parent?: string
  n_written?: number
  deduped?: number
  carried?: number
  task_id?: string
}

// ---- 5.7 清洗（预览与执行同一条 evaluate()）----

/** 算子参数元数据：界面参数面板完全由它驱动，不写死算子表 */
export interface OpParamSpec {
  type: string
  default?: unknown
  min?: number
  max?: number
  enum?: string[]
  description?: string
}
export interface CleanOpMeta {
  name: string
  description: string
  schema: Record<string, OpParamSpec>
}
export interface DroppedRow {
  uri: string
  op: string
  reason?: string
  score?: number | null
}
export interface PerOpStat { n: number; dropped: number; transformed: number }
/**
 * 预览响应 = Result.summary()。
 * 注意：契约 v1.3 的 summary 只有条数，不含命中清单——dropped/transforms 两键是
 * 为「预览态列命中条目」预留的可选字段，后端补上即自动生效，前端不依赖其存在。
 */
export interface CleanPreviewResp {
  n_items: number
  n_kept: number
  n_dropped: number
  n_transforms: number
  per_op: Record<string, PerOpStat>
  groups: number
  dropped?: DroppedRow[]
  transforms?: { uri: string; op: string; params: Record<string, unknown> }[]
}
/** apply 响应带 version_id（§7：新版本 id 供回退/谱系绑定），原件不动 */
export interface CleanApplyResp {
  version?: string
  version_id?: string
  parent?: string
  summary?: CleanPreviewResp
  dropped?: DroppedRow[]
  kept?: string[]
  written?: Record<string, unknown>[]
  n_inherited?: number
  transformed?: number
  task_id?: string
  out_dir?: string
  error?: string
  detail?: string
}
export interface CleanRollbackResp {
  undid: string
  restored: string | null
  new_version: string | null
  op: string
  removed_files: string[]
  skipped_files: { written: string; why: string }[]
  annotations_kept: number
  error?: string
  detail?: string
}

// ---- 5.7 增强（配方 → 告警检查 → 执行 → 谱系与可复现验证）----

/** 增强算子元数据：kind=augment 的 ops，参数控件与清洗共用 OpParamSpec */
export interface AugOpMeta {
  name: string
  description: string
  schema: Record<string, OpParamSpec>
}
/** 配方里的一个算子项（后端 Recipe.ops 的元素，name 必填、params 可省） */
export interface AugOpSpec {
  name: string
  params?: Record<string, unknown>
}
/**
 * 增强配方 = 后端 lineage.Recipe 的线上形态。
 * seed 是可复现的前提（replay 按它重抽），factor=每张原件产几个副本，
 * limit=最多处理多少条，index=体/视频取哪一刀。
 */
export interface AugRecipe {
  seed: number
  factor: number
  ops: AugOpSpec[]
  limit?: number | null
  index?: number
}
export interface AugPreset {
  name: string
  title: string
  reason: string
  recipe: AugRecipe
}
/** GET /api/augment/recipes：三 Tab 的唯一数据源 */
export interface AugRecipesResp {
  ops: AugOpMeta[]
  presets: AugPreset[]
  task_presets: Record<string, string>
}
export interface AugSuggestResp {
  preset: AugPreset
  recipe: AugRecipe
  reason: string
}
/** check 只回告警原文列表（空数组 = 可以放心跑）；抽样参数不在这里回（rev6 A 级口径） */
export interface AugCheckResp {
  warnings: string[]
}
/** 谱系行 = run 落盘的 written 记录：原件 uri → 写出的 uri + 逐条抽样参数链 */
export interface AugLineageRow {
  source: string
  rep: number
  modality: string
  written: string
  bytes: number
  chain: { op: string; params: Record<string, unknown> }[]
  specs?: { name: string; params?: Record<string, unknown> }[]
  source_shape?: number[]
  width?: number
  height?: number
  n_samples?: number
  aux?: Record<string, Record<string, unknown>>
}
export interface AugRunSummary {
  n_sources: number
  n_generated: number
  n_masks: number
  n_series: number
  n_files: number
}
export interface AugRunResp {
  version?: string
  version_id?: string
  parent?: string
  recipe?: AugRecipe
  out_dir?: string
  written?: AugLineageRow[]
  summary?: AugRunSummary
  n_inherited?: number
  task_id?: string
  error?: string
  detail?: string
}
/**
 * replay 校验的是 seed 谱系链（不是重算像素），故界面措辞为"参数链逐条一致"。
 * why 实测三类：参数不一致 / 重放多出了副本 / 重放少了这条。
 */
export interface AugReplayDiff {
  uri: string
  rep: number
  why: string
  stored?: unknown
  replayed?: unknown
}
export interface AugReplayResp {
  identical: boolean
  recipe?: AugRecipe
  n_jobs: number
  n_stored: number
  diff: AugReplayDiff[]
  error?: string
  detail?: string
}

// ---- 5.9 导出（格式 → 划分 → 导出 → 回读校验 → 报告）----

/** 导出格式元数据：GET /api/export/formats 的 formats 项，参数表单完全由 schema 生成 */
export interface ExportFormatMeta {
  name: string
  description: string
  schema: Record<string, OpParamSpec>
}
export interface ExportFormatsResp {
  formats: ExportFormatMeta[]
  /** 后端默认落盘根（界面只读显示，用户不填就用它） */
  out_dir: string
}
/** split.summary()：{n, counts{fold:n}, ratios{fold:0.xxxx}} */
export interface ExportSplitSummary {
  n: number
  counts: Record<string, number>
  ratios: Record<string, number>
}
/**
 * 导出报告 = 落盘的 dataset_report.json 同一份对象（后端 write_report）。
 * 不同格式的计数字段不一样（coco 有 per_split、yolo 有 n_labels_per_fold、voc 有 split_files），
 * 所以界面按"有哪些键就展示哪些"处理，不假设固定形状。
 */
export interface ExportRunResp {
  format: string
  dataset?: string
  dataset_id?: string
  type?: string
  root?: string
  version?: string
  parent?: string | null
  op?: string
  params?: Record<string, unknown>
  counts?: Record<string, unknown>
  issues?: unknown[]
  files?: string[]
  out_dir?: string
  task_id?: string
  n_images?: number
  n_annotations?: number
  n_categories?: number
  n_skipped?: number
  skipped?: Record<string, unknown>[]
  spatial_annotations?: number
  split?: ExportSplitSummary
  per_split?: Record<string, { images: number; annotations: number }>
  scripts?: Record<string, string>
  [key: string]: unknown
}
/**
 * 后端 export.validate.validate() 的原文：一次回读对账的全部结论。
 * issues 最多回传 MAX_ISSUES(50) 条，但 n_issues 是真实累计计数——
 * 界面必须用 n_issues 报条数，否则"51 条问题"会显示成"50 条"假装可控。
 */
export interface ExportValidateCheck {
  path?: string
  format?: string
  version?: string
  ok: boolean
  issues: string[]
  checked: Record<string, unknown>
  counts: Record<string, unknown>
  n_issues?: number
}
/** POST /api/export/validate 的实际形状：{ok, summary, check}，结论与明细都在 check 里 */
export interface ExportValidateResp {
  ok: boolean
  /** 后端给出的一行结论（validate.summarize），原样转述 */
  summary?: string
  check?: ExportValidateCheck
  error?: string
  detail?: string
}
