<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { X, RefreshCw, Ban } from 'lucide-vue-next'
import MButton from '@/components/ui/MButton.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import { useTasksStore } from '@/stores/tasks'
import { useToast } from '@/stores/toast'

const tasks = useTasksStore()
const toast = useToast()
const router = useRouter()

const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null
onMounted(() => { ticker = setInterval(() => { now.value = Date.now() }, 1000) })
onBeforeUnmount(() => { if (ticker) clearInterval(ticker) })

const KIND_OPTIONS = [
  { value: '', label: '全部类型' },
  { value: 'clean', label: 'clean' },
  { value: 'augment', label: 'augment' },
  { value: 'export', label: 'export' },
  { value: 'bench', label: 'bench' },
  { value: 'smart', label: 'smart' },
  { value: 'collect', label: 'collect' },
]

const KIND_ROUTE: Record<string, string> = {
  clean: '/clean', augment: '/augment', export: '/export', bench: '/backends', smart: '/annotate',
}

const records = computed(() => tasks.records)

// 手动筛选后重新拉取一次（仍然禁止轮询）
watch(() => [tasks.kindFilter, tasks.datasetFilter], () => { void onRefresh() })

function elapsed(startedAt: number): string {
  const s = Math.max(0, Math.floor((now.value - startedAt) / 1000))
  return s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`
}

function fmt(created: number | undefined): string {
  if (!created) return '—'
  return new Date(created * 1000).toLocaleString('zh-CN')
}

function jump(kind: string) {
  const target = KIND_ROUTE[kind]
  tasks.closeDrawer()
  if (target) void router.push(target)
}

function onCancel(id: string) {
  tasks.cancel(id)
  // 取消只断开前端连接；服务端会继续执行并落库（契约 v1.2 §4-3）
  toast.info('已断开该请求；任务可能仍在服务端执行，可稍后在任务清单核对结果')
}

async function onRefresh() {
  await tasks.refreshRecords()
  if (tasks.recordsError) toast.danger(`刷新失败：${tasks.recordsError}`)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="tasks.drawerOpen" class="drawer__mask" @click.self="tasks.closeDrawer()">
      <aside class="drawer" aria-label="任务清单">
        <header class="drawer__head">
          <h2 class="drawer__title">任务清单</h2>
          <div class="drawer__actions">
            <MButton variant="ghost" size="sm" title="刷新" :loading="tasks.recordsLoading"
              @click="onRefresh">
              <RefreshCw :size="14" aria-hidden="true" />
            </MButton>
            <button type="button" class="drawer__close" aria-label="关闭任务清单"
              @click="tasks.closeDrawer()">
              <X :size="14" />
            </button>
          </div>
        </header>

        <section v-if="tasks.inflight.length" class="drawer__section">
          <h3 class="drawer__subtitle">执行中（本会话）</h3>
          <ul class="drawer__list">
            <li v-for="task in tasks.inflight" :key="task.id" class="row is-inflight">
              <span class="row__spinner" aria-hidden="true" />
              <span class="row__main">
                <span class="row__label">{{ task.label }}</span>
                <span class="row__sub num">已耗时 {{ elapsed(task.startedAt) }}</span>
              </span>
              <MButton variant="ghost" size="sm" title="取消（服务端可能继续执行）"
                @click="onCancel(task.id)">
                <Ban :size="14" aria-hidden="true" />
              </MButton>
            </li>
          </ul>
        </section>
        <hr v-if="tasks.inflight.length" class="drawer__divider" />

        <section class="drawer__section drawer__section--grow">
          <div class="drawer__filters">
            <MSelect v-model="tasks.kindFilter" :options="KIND_OPTIONS" width="110px"
              aria-label="任务类型筛选" />
            <span class="drawer__hint">落库留痕，打开时拉取一次；非实时进度</span>
          </div>
          <p v-if="tasks.recordsError" class="drawer__error">{{ tasks.recordsError }}</p>
          <ul v-if="records.length" class="drawer__list">
            <li v-for="row in records" :key="row.id" class="row is-clickable"
              @click="jump(row.kind)">
              <span class="row__main">
                <span class="row__label">{{ row.kind }}<template v-if="row.version_out"> → {{ row.version_out }}</template></span>
                <span class="row__sub mono">{{ row.id }} · {{ fmt(row.created) }}</span>
              </span>
            </li>
          </ul>
          <MEmpty v-else-if="!tasks.recordsLoading" text="暂无任务留痕；执行清洗/增强/导出/实测后会出现在这里。" />
        </section>
      </aside>
    </div>
  </Teleport>
</template>

<style scoped>
.drawer__mask { position: fixed; inset: 0; z-index: 90; background: rgba(0, 0, 0, 0.35); }
.drawer {
  position: absolute; top: 0; right: 0; bottom: 0; width: 380px; max-width: 92vw;
  display: flex; flex-direction: column;
  background: var(--bg-panel); border-left: 1px solid var(--border-strong);
  box-shadow: var(--shadow-pop);
}
.drawer__head {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-3); border-bottom: 1px solid var(--border); flex: none;
}
.drawer__title { margin: 0; font-size: var(--fs-md); }
.drawer__actions { display: flex; align-items: center; gap: var(--sp-1); }
.drawer__close {
  display: flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0;
  background: transparent; border: none; border-radius: var(--radius-control);
  color: var(--text-secondary); cursor: pointer;
}
.drawer__close:hover { background: var(--bg-hover); color: var(--text-primary); }
.drawer__section { padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-2); }
.drawer__section--grow { flex: 1; min-height: 0; overflow-y: auto; }
.drawer__subtitle { margin: 0; font-size: var(--fs-ui); color: var(--text-secondary); }
.drawer__divider { border: none; border-top: 1px solid var(--border); margin: 0; }
.drawer__filters { display: flex; align-items: center; gap: var(--sp-2); }
.drawer__hint { font-size: var(--fs-xs); color: var(--text-muted); }
.drawer__error { margin: 0; font-size: var(--fs-xs); color: var(--danger); }
.drawer__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.row {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2); border-radius: var(--radius-control);
}
.row:hover { background: var(--bg-hover); }
.row.is-clickable { cursor: pointer; }
.row__spinner {
  flex: none; width: 12px; height: 12px; border-radius: 50%;
  border: 2px solid var(--accent-soft); border-top-color: var(--accent);
  animation: spin 0.9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.row__main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.row__label { font-size: var(--fs-ui); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row__sub { font-size: var(--fs-xs); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
