/**
 * tasks store：任务清单抽屉（UI规格 §6）。
 * GET /api/tasks 是落库留痕清单：打开抽屉拉取一次 + 手动刷新，禁止轮询（契约 v1.2 §4）。
 * 本会话内发起且尚未返回的同步长请求由本地状态显示"执行中"（spinner+已耗时+取消）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '@/api/endpoints'
import type { TaskRow } from '@/api/types'

export interface InflightTask {
  /** 本地 id */
  id: string
  kind: string
  label: string
  startedAt: number
  controller: AbortController
}

export const useTasksStore = defineStore('tasks', () => {
  const drawerOpen = ref(false)
  const records = ref<TaskRow[]>([])
  const recordsLoading = ref(false)
  const recordsError = ref('')
  const kindFilter = ref('')
  const datasetFilter = ref('')
  const inflight = ref<InflightTask[]>([])

  async function refreshRecords() {
    recordsLoading.value = true
    recordsError.value = ''
    try {
      const resp = await api.getTasks(kindFilter.value, datasetFilter.value)
      records.value = resp.tasks
    } catch (err) {
      recordsError.value = err instanceof Error ? err.message : String(err)
    } finally {
      recordsLoading.value = false
    }
  }

  function openDrawer() {
    drawerOpen.value = true
    void refreshRecords()
  }

  function closeDrawer() {
    drawerOpen.value = false
  }

  /** 长任务开始前登记；返回取消函数 */
  function begin(kind: string, label: string): { id: string; signal: AbortSignal; end: () => void } {
    const controller = new AbortController()
    const id = `local-${Date.now()}-${inflight.value.length}`
    inflight.value.push({ id, kind, label, startedAt: Date.now(), controller })
    return {
      id,
      signal: controller.signal,
      end: () => {
        const idx = inflight.value.findIndex(t => t.id === id)
        if (idx >= 0) inflight.value.splice(idx, 1)
      },
    }
  }

  function cancel(id: string) {
    const task = inflight.value.find(t => t.id === id)
    if (task) task.controller.abort()
  }

  return {
    drawerOpen, records, recordsLoading, recordsError, kindFilter, datasetFilter, inflight,
    refreshRecords, openDrawer, closeDrawer, begin, cancel,
  }
})
