<script setup lang="ts">
/**
 * 右栏：执行 / 历史 / 回退 / 丢弃明细（§5.3，320px）。
 * 文案硬约束：全流程不出现"删除原件"的入口——回退只删后端登记在册且字节对得上的产物，
 * 这一点由后端裁决，界面只复述后端给出的原因原文。
 */
import { computed, ref } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MDialog from '@/components/ui/MDialog.vue'
import MSwitch from '@/components/ui/MSwitch.vue'
import MTag from '@/components/ui/MTag.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import { useCleanStore } from '@/stores/clean'
import { useCtxStore } from '@/stores/ctx'
import type { TaskRow } from '@/api/types'

const store = useCleanStore()
const ctx = useCtxStore()

const confirmApply = ref(false)
const confirmRollback = ref(false)
const picked = ref<TaskRow | null>(null)

const chain = computed(() => store.selectedOps.map(o => o.name).join(' → '))
/** 历史里"产出版本"的清洗任务才可回退；非清洗产出的版本后端会直接拒绝，界面不猜 */
const rollbackTarget = computed(() => picked.value?.version_out || '')
const canApply = computed(() => store.hasSelection && !!ctx.version && !store.applying)
const applyWhy = computed(() => {
  if (store.applying) return '正在执行'
  if (!store.hasSelection) return '先在左侧勾选清洗算子'
  if (!ctx.version) return '未选定版本'
  return ''
})
const canRollback = computed(() => !!rollbackTarget.value && !store.rollingBack && !store.applying)
const rollbackWhy = computed(() => {
  if (store.rollingBack) return '正在回退'
  if (!rollbackTarget.value) return '在下方历史里选一条清洗产出'
  return ''
})
const fmtTime = (t?: number) => {
  if (!t) return ''
  const d = new Date(t * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function pick(row: TaskRow) {
  picked.value = picked.value?.id === row.id ? null : row
}
const opsOf = (row: TaskRow): string => {
  const ops = row.params?.ops
  return Array.isArray(ops) && ops.length ? ops.map(String).join(' → ') : row.kind
}
const timeOf = (row: TaskRow): string =>
  typeof row.created === 'number' ? fmtTime(row.created) : ''
const skippedOf = (row: Record<string, unknown> | null): unknown[] => {
  const list = row?.skipped_files
  return Array.isArray(list) ? list : []
}
async function doApply() {
  confirmApply.value = false
  const v = await store.apply(ctx.version, ctx.role)
  if (v) { await ctx.loadVersions(); ctx.setVersion(v); await store.loadDropped(v, true) }
}
async function doRollback() {
  confirmRollback.value = false
  const landed = await store.rollback(rollbackTarget.value)
  if (!landed) return
  /** 版本列表与条目列表即时反映回退结果：切到回退落点后由 CleanView 的 ctx 监听重拉 */
  await ctx.loadVersions()
  ctx.setVersion(landed)
  picked.value = null
}
</script>

<template>
  <aside class="exec" aria-label="清洗执行面板">
    <div class="exec__apply">
      <MSwitch
        :model-value="store.writeTransformed"
        label="写出变换后的产物"
        @update:model-value="store.writeTransformed = Boolean($event)"
      />
      <p class="exec__note">关闭时只做筛选与登记，不写任何派生文件；两种情况下原始数据都不动。</p>
      <MButton
        variant="primary"
        :loading="store.applying"
        :disabled="!canApply"
        :title="applyWhy || `用 ${chain || '（未选算子）'} 清洗当前版本`"
        @click="confirmApply = true"
      >
        执行清洗
      </MButton>
      <p v-if="store.applyError" class="exec__err" role="alert">{{ store.applyError }}</p>
      <MButton
        v-if="store.applying"
        variant="ghost"
        size="sm"
        @click="store.cancelInflight()"
      >
        取消在途请求
      </MButton>
    </div>

    <div class="exec__history">
      <h3 class="exec__h">
        执行历史
        <MButton variant="ghost" size="sm" :loading="store.historyLoading" @click="store.loadHistory()">刷新</MButton>
      </h3>
      <MEmpty v-if="!store.history.length" text="本数据集还没有清洗执行记录" />
      <ul v-else class="tasks" role="list">
        <li v-for="row in store.history" :key="row.id">
          <button
            type="button"
            class="tasks__row"
            :class="{ 'is-picked': picked?.id === row.id }"
            :aria-pressed="picked?.id === row.id"
            @click="pick(row)"
          >
            <span class="tasks__op num">{{ opsOf(row) }}</span>
            <span class="tasks__time num">{{ timeOf(row) }}</span>
            <MTag :kind="row.state === 'done' ? 'success' : 'warning'" :text="String(row.state || '')" />
            <span class="tasks__ver num" :title="String(row.version_out || '')">
              {{ row.version_out ? `→ ${row.version_out}` : '（未产版本）' }}
            </span>
          </button>
        </li>
      </ul>
      <p class="exec__note">选中一条可把它产出的版本回退；再次点击取消选中。</p>
      <MButton
        variant="danger"
        size="sm"
        :loading="store.rollingBack"
        :disabled="!canRollback"
        :title="rollbackWhy || `回退 ${rollbackTarget}`"
        @click="confirmRollback = true"
      >
        回退该次清洗
      </MButton>
      <p v-if="store.rollbackError" class="exec__err" role="alert">{{ store.rollbackError }}</p>
      <ul v-if="skippedOf(store.lastRollback).length" class="tasks__skipped">
        <li v-for="(s, i) in skippedOf(store.lastRollback)" :key="i" class="num">
          未删：{{ JSON.stringify(s) }}
        </li>
      </ul>
    </div>

    <div v-if="store.dropped.length || store.droppedOf" class="exec__dropped">
      <h3 class="exec__h">丢弃明细（版本 {{ store.droppedOf }}）</h3>
      <MEmpty v-if="!store.dropped.length" text="这个版本没有丢弃任何条目" />
      <ul v-else class="drops" aria-label="被丢弃条目">
        <li v-for="d in store.dropped" :key="d.uri" class="drops__row">
          <span class="drops__uri" :title="d.uri">{{ d.uri }}</span>
          <MTag kind="danger" :text="d.op" />
          <span class="drops__reason">{{ d.reason || '' }}</span>
        </li>
      </ul>
    </div>

    <MDialog :open="confirmApply" title="确认执行清洗？" @close="confirmApply = false">
      <p class="dlg__line">
        将产<b>新版本</b>，按当前预览<b>移除 {{ store.stat.removed }} 条</b>（共 {{ store.stat.total }} 条），
        <b>原件不动</b>。
      </p>
      <p class="dlg__line num">算子链：{{ chain }}</p>
      <p class="dlg__line">写出派生文件：{{ store.writeTransformed ? '是' : '否' }}</p>
      <template #footer>
        <MButton variant="ghost" @click="confirmApply = false">取消</MButton>
        <MButton variant="primary" @click="doApply">执行</MButton>
      </template>
    </MDialog>

    <MDialog :open="confirmRollback" title="确认回退这次清洗？" @close="confirmRollback = false">
      <p class="dlg__line">
        回退<b>只删除登记在册且字节校验一致</b>的产物文件；文件缺失或字节被改过的一律不删，
        后端会逐条给出原因。原始数据从来不在登记清单里，这条路径删不到原件。
      </p>
      <p class="dlg__line num">目标版本：{{ rollbackTarget }}</p>
      <template #footer>
        <MButton variant="ghost" @click="confirmRollback = false">取消</MButton>
        <MButton variant="danger" @click="doRollback">回退</MButton>
      </template>
    </MDialog>
  </aside>
</template>

<style scoped>
.exec {
  width: 320px; flex: 0 0 320px; display: flex; flex-direction: column; gap: var(--sp-4);
  padding: var(--sp-4); overflow-y: auto; background: var(--bg-panel);
  border-left: 1px solid var(--border);
}
.exec__apply, .exec__history, .exec__dropped { display: flex; flex-direction: column; gap: var(--sp-2); }
.exec__h {
  margin: 0 0 var(--sp-1); font-size: var(--fs-md); color: var(--text-primary);
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2);
}
.exec__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; }
.exec__err { margin: 0; font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.tasks { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.tasks__row {
  width: 100%; display: flex; align-items: center; gap: var(--sp-2); text-align: left;
  padding: 6px var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-control);
  background: var(--bg-raised); color: var(--text-primary); font-size: var(--fs-xs); cursor: pointer;
}
.tasks__row:hover { background: var(--bg-hover); }
.tasks__row.is-picked { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent-soft); }
.tasks__op { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tasks__time, .tasks__ver { color: var(--text-muted); font-family: var(--font-mono); }
.tasks__ver { max-width: 92px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tasks__skipped { margin: var(--sp-1) 0 0; padding-left: var(--sp-4); font-size: var(--fs-xs); color: var(--warning); word-break: break-all; }
.drops { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-1); }
.drops__row { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-xs); }
.drops__uri { flex: 1; min-width: 0; font-family: var(--font-mono); color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.drops__reason { color: var(--text-muted); }
.dlg__line { margin: 0 0 var(--sp-2); font-size: var(--fs-md); line-height: 1.6; }
.dlg__line b { color: var(--accent); }
</style>
