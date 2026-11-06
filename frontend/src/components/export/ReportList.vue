<script setup lang="ts">
/**
 * 历史报告（§5.5 Step④ 后半）：GET /api/export/report 的任务留痕。
 * 每行显示"导过什么格式、什么状态、落在哪、出了多少"，数字全部来自任务表里存的报告原文。
 */
import { computed } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MTag from '@/components/ui/MTag.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import JsonView from '@/components/ui/JsonView.vue'
import type { TaskRow } from '@/api/types'
import { useExportStore } from '@/stores/export'
import { useCtxStore } from '@/stores/ctx'

const store = useExportStore()
const ctx = useCtxStore()

interface Row {
  id: string
  format: string
  state: string
  outDir: string
  files: number
  images: string | number
  anns: string | number
  when: string
  report: Record<string, unknown>
  raw: TaskRow
}

const pad = (n: number) => String(n).padStart(2, '0')
function whenOf(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const rows = computed<Row[]>(() => store.reports.map(t => {
  const rep = (t.report || {}) as Record<string, unknown>
  return {
    id: t.id,
    format: String(rep.format || t.params?.format || '?'),
    state: String(t.state || ''),
    outDir: String(rep.out_dir || ''),
    files: Array.isArray(rep.files) ? rep.files.length : 0,
    images: (rep.n_images as number | undefined) ?? '—',
    anns: (rep.n_annotations as number | undefined) ?? '—',
    when: whenOf(typeof t.created === 'number' ? t.created : undefined),
    report: rep,
    raw: t,
  }
}))

/** 拿历史某次的目录再校验一次：校验目标是"那次导出的产物"，不是当前界面填的值 */
function revalidate(row: Row) {
  if (row.outDir) void store.validate(ctx.version, row.outDir)
}
</script>

<template>
  <div class="hist" data-el="report-list" :data-rows="String(rows.length)">
    <div class="hist__head">
      <h3 class="hist__h">导出历史（本版本留痕）</h3>
      <MButton variant="ghost" size="sm" :loading="store.reportsLoading" @click="store.loadReports(ctx.version)">
        刷新
      </MButton>
    </div>
    <p v-if="store.reportsError" class="hist__err" role="alert">{{ store.reportsError }}</p>
    <MEmpty v-else-if="!rows.length" text="这个版本还没有导出记录" />
    <table v-else class="hist__table">
      <caption class="sr-only">每次导出的格式、状态、落盘路径与条数</caption>
      <thead>
        <tr>
          <th scope="col">时间</th><th scope="col">格式</th><th scope="col">状态</th>
          <th scope="col">样本</th><th scope="col">标注</th>
          <th scope="col" title="任务表 report 里存的是绝对路径原文；导出接口出站的同名字段被后端 relativize 成相对路径，两处都以接口返回为准，前端不重新拼">
            落盘路径（任务表原文·绝对）
          </th>
          <th scope="col">动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="num">{{ r.when || '—' }}</td>
          <td class="mono">{{ r.format }}</td>
          <td><MTag :kind="r.state === 'done' ? 'success' : r.state === 'failed' ? 'danger' : 'warning'" :text="r.state" /></td>
          <td class="num">{{ r.images }}</td>
          <td class="num">{{ r.anns }}</td>
          <td class="hist__path mono" :title="r.outDir">{{ r.outDir || '—' }}</td>
          <td>
            <MButton
              variant="ghost"
              size="sm"
              :disabled="!r.outDir || store.validating"
              :aria-label="`校验 ${r.format} 这次导出的目录`"
              @click="revalidate(r)"
            >
              校验这次
            </MButton>
          </td>
        </tr>
      </tbody>
    </table>
    <details v-if="rows.length" class="hist__more">
      <summary class="hist__summary">最新一次的报告原文（任务表 report 字段）</summary>
      <JsonView :data="rows[0].report" />
    </details>
  </div>
</template>

<style scoped>
.hist { display: flex; flex-direction: column; gap: var(--sp-2); }
.hist__head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.hist__h { margin: 0; font-size: var(--fs-md); color: var(--text-primary); }
.hist__err { margin: 0; font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.hist__table { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.hist__table th, .hist__table td { text-align: left; padding: 6px var(--sp-2); border-bottom: 1px solid var(--border); }
.hist__table thead th { color: var(--text-muted); font-weight: 500; white-space: nowrap; }
.hist__table td.num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.hist__path { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); }
.hist__more { display: flex; flex-direction: column; gap: var(--sp-2); }
.hist__summary { font-size: var(--fs-xs); color: var(--text-secondary); cursor: pointer; }
.sr-only {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
</style>
