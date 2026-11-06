/**
 * ctx store：工作区/数据集/版本/角色/健康（UI规格 §3.1、§3.3）。
 * 三者变更全局生效并写入 URL query（?ws=&dataset=&version=），刷新/分享不丢上下文。
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Router } from 'vue-router'
import * as api from '@/api/endpoints'
import type { DatasetRow, VersionRow, WorkspaceRow, HealthResp } from '@/api/types'
import type { Role } from '@/api/types'

export const useCtxStore = defineStore('ctx', () => {
  const workspaces = ref<WorkspaceRow[]>([])
  const datasets = ref<DatasetRow[]>([])
  const versions = ref<VersionRow[]>([])
  const ws = ref('')
  const dataset = ref('')
  const version = ref('')
  const role = ref<Role>('admin')
  const health = ref<HealthResp | null>(null)
  const healthOk = ref(false)
  const healthError = ref('')
  const loading = ref(false)
  const ctxError = ref('')

  const head = computed(() => versions.value.length
    ? versions.value[versions.value.length - 1].id : '')
  const currentDataset = computed(() =>
    datasets.value.find(d => d.id === dataset.value) || null)

  let healthTimer: ReturnType<typeof setInterval> | null = null
  let router: Router | null = null

  function setRouter(r: Router) {
    router = r
  }

  /** 上下文写入 URL query（?ws=&dataset=&version=），刷新/分享不丢 */
  function writeQuery() {
    if (!router) return
    void router.replace({
      query: { ...router.currentRoute.value.query, ws: ws.value, dataset: dataset.value, version: version.value },
    })
  }

  function readQuery() {
    const url = new URL(window.location.href)
    ws.value = url.searchParams.get('ws') || ''
    dataset.value = url.searchParams.get('dataset') || ''
    version.value = url.searchParams.get('version') || ''
  }

  async function pollHealth() {
    try {
      health.value = await api.getHealth()
      healthOk.value = health.value.ok === true
      healthError.value = ''
    } catch (err) {
      healthOk.value = false
      healthError.value = err instanceof Error ? err.message : String(err)
    }
  }

  function startHealthPolling() {
    if (healthTimer) return
    void pollHealth()
    healthTimer = setInterval(() => void pollHealth(), 30_000)
  }

  function stopHealthPolling() {
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null }
  }

  async function loadWorkspaces() {
    const resp = await api.getWorkspaces()
    workspaces.value = resp.workspaces
    if (!ws.value) ws.value = health.value?.default || resp.workspaces[0]?.name || ''
  }

  async function loadDatasets() {
    const resp = await api.getDatasets()
    datasets.value = resp.datasets
    // 当前选择失效时回退到第一个
    if (!datasets.value.some(d => d.id === dataset.value)) {
      dataset.value = datasets.value[0]?.id || ''
    }
  }

  async function loadVersions() {
    if (!dataset.value) { versions.value = []; version.value = ''; return }
    const resp = await api.getVersions(dataset.value)
    versions.value = resp.versions
    if (!versions.value.some(v => v.id === version.value)) {
      version.value = resp.head || resp.versions[resp.versions.length - 1]?.id || ''
    }
  }

  /** 全量刷新：健康 → 工作区 → 数据集 → 版本（启动与 rescan 后用） */
  async function refresh() {
    loading.value = true
    ctxError.value = ''
    try {
      readQuery()
      await pollHealth()
      await loadWorkspaces()
      await loadDatasets()
      await loadVersions()
      writeQuery()
    } catch (err) {
      ctxError.value = err instanceof Error ? err.message : String(err)
      throw err
    } finally {
      loading.value = false
    }
  }

  async function setWs(name: string) {
    ws.value = name
    dataset.value = ''
    version.value = ''
    await loadDatasets()
    await loadVersions()
    writeQuery()
  }

  async function setDataset(id: string) {
    dataset.value = id
    version.value = ''
    await loadVersions()
    writeQuery()
  }

  function setVersion(id: string) {
    version.value = id
    writeQuery()
  }

  function setRole(r: Role) {
    role.value = r
  }

  async function rescan() {
    await api.rescanWorkspaces()
    await loadWorkspaces()
    await loadDatasets()
    await loadVersions()
    writeQuery()
  }

  return {
    workspaces, datasets, versions, ws, dataset, version, role,
    health, healthOk, healthError, loading, ctxError, head, currentDataset,
    refresh, setWs, setDataset, setVersion, setRole, rescan, setRouter,
    loadWorkspaces, loadDatasets, loadVersions,
    startHealthPolling, stopHealthPolling, pollHealth, writeQuery,
  }
})
