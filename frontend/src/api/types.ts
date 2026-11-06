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
