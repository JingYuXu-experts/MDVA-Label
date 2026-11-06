<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { RefreshCw, ListTodo, Activity } from 'lucide-vue-next'
import MSelect from '@/components/ui/MSelect.vue'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import MButton from '@/components/ui/MButton.vue'
import { useCtxStore } from '@/stores/ctx'
import { useTasksStore } from '@/stores/tasks'
import { useToast } from '@/stores/toast'
import type { Role } from '@/api/types'

const ctx = useCtxStore()
const tasks = useTasksStore()
const toast = useToast()

const NAV = [
  { to: '/console', label: '管理台' },
  { to: '/annotate', label: '标注' },
  { to: '/clean', label: '清洗' },
  { to: '/augment', label: '增强' },
  { to: '/export', label: '导出' },
  { to: '/backends', label: '后端' },
]

const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: 'admin', label: 'admin' },
  { value: 'annotator', label: 'annotator' },
  { value: 'reviewer', label: 'reviewer' },
]

const wsOptions = computed(() =>
  ctx.workspaces.map(w => ({ value: w.name, label: w.name })))

const datasetOptions = computed(() =>
  ctx.datasets.map(d => ({ value: d.id, label: d.name, hint: d.type })))

function fmtTime(created: number | null | undefined): string {
  if (!created) return ''
  return new Date(created * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

const versionOptions = computed(() =>
  ctx.versions.map(v => ({
    value: v.id,
    label: `${v.op} · ${fmtTime(v.created)}`,
    hint: v.id === ctx.head ? '当前' : `${v.n_annotations} 条标注`,
  })))

const healthOpen = ref(false)
const rescanning = ref(false)

async function onRescan() {
  rescanning.value = true
  try {
    await ctx.rescan()
    toast.success('目录已重扫')
  } catch (err) {
    toast.danger(`重扫失败：${err instanceof Error ? err.message : String(err)}`)
  } finally {
    rescanning.value = false
  }
}
</script>

<template>
  <header class="topbar">
    <span class="topbar__logo">MDVA</span>
    <nav class="topbar__nav" aria-label="视图导航">
      <RouterLink v-for="item in NAV" :key="item.to" :to="item.to" class="topbar__navitem"
        active-class="is-active">
        {{ item.label }}
      </RouterLink>
    </nav>

    <div class="topbar__ctx">
      <MSelect :model-value="ctx.ws" :options="wsOptions" width="110px"
        placeholder="工作区" @update:model-value="ctx.setWs" />
      <MSelect :model-value="ctx.dataset" :options="datasetOptions" width="150px"
        placeholder="数据集" @update:model-value="ctx.setDataset" />
      <MSelect :model-value="ctx.version" :options="versionOptions" width="170px"
        placeholder="版本" @update:model-value="ctx.setVersion" />
    </div>

    <SegmentedControl class="topbar__role" :model-value="ctx.role" :options="ROLE_OPTIONS"
      aria-label="角色切换" @update:model-value="ctx.setRole" />

    <div class="topbar__tools">
      <MButton variant="ghost" size="sm" :loading="rescanning" title="重扫目录"
        @click="onRescan">
        <RefreshCw :size="14" aria-hidden="true" />
      </MButton>

      <button type="button" class="topbar__task" :title="'任务清单'"
        :aria-label="'打开任务清单'" @click="tasks.openDrawer()">
        <span v-if="tasks.inflight.length" class="topbar__task-spinner" aria-hidden="true" />
        <ListTodo v-else :size="16" aria-hidden="true" />
      </button>

      <div class="topbar__health">
        <button type="button" class="healthdot" :class="ctx.healthOk ? 'is-ok' : 'is-down'"
          :aria-label="ctx.healthOk ? '服务正常' : '服务失联'" :title="ctx.healthOk ? '服务正常' : '服务失联'"
          @click="healthOpen = !healthOpen">
          <Activity :size="14" aria-hidden="true" />
        </button>
        <div v-if="healthOpen" class="healthpop">
          <template v-if="ctx.healthOk && ctx.health">
            <p class="healthpop__line">{{ ctx.health.app }} v{{ ctx.health.version }}</p>
            <p class="healthpop__line mono">{{ ctx.health.root }}</p>
            <p class="healthpop__line">离线模式：{{ ctx.health.offline ? '是' : '否' }}</p>
          </template>
          <template v-else>
            <p class="healthpop__line is-error">服务失联</p>
            <p v-if="ctx.healthError" class="healthpop__line mono">{{ ctx.healthError }}</p>
            <MButton size="sm" variant="secondary" @click="ctx.pollHealth(); healthOpen = false">
              重试
            </MButton>
          </template>
        </div>
      </div>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  height: var(--topbar-h); flex: none;
  display: flex; align-items: center; gap: var(--sp-3);
  padding: 0 var(--sp-3);
  background: var(--bg-panel); border-bottom: 1px solid var(--border);
}
.topbar__logo {
  font-size: var(--fs-lg); font-weight: 700; color: var(--accent);
  letter-spacing: 0.5px; flex: none;
}
.topbar__nav { display: flex; align-items: center; gap: 2px; flex: none; }
.topbar__navitem {
  padding: 0 var(--sp-2); height: 26px;
  display: inline-flex; align-items: center;
  color: var(--text-secondary); font-size: var(--fs-ui);
  border-radius: var(--radius-control); text-decoration: none;
}
.topbar__navitem:hover { background: var(--bg-hover); color: var(--text-primary); text-decoration: none; }
.topbar__navitem.is-active { background: var(--accent-soft); color: var(--accent); }
.topbar__ctx { display: flex; align-items: center; gap: var(--sp-2); flex: 1; min-width: 0; }
.topbar__role { flex: none; }
.topbar__tools { display: flex; align-items: center; gap: var(--sp-1); flex: none; }
.topbar__task {
  position: relative; display: flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; padding: 0;
  background: transparent; border: none; border-radius: var(--radius-control);
  color: var(--text-secondary); cursor: pointer;
}
.topbar__task:hover { background: var(--bg-hover); color: var(--text-primary); }
.topbar__task-spinner {
  width: 12px; height: 12px; border-radius: 50%;
  border: 2px solid var(--accent-soft); border-top-color: var(--accent);
  animation: spin 0.9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.topbar__health { position: relative; }
.healthdot {
  display: flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; padding: 0;
  background: transparent; border: none; border-radius: var(--radius-control);
  cursor: pointer;
}
.healthdot.is-ok { color: var(--success); }
.healthdot.is-down { color: var(--danger); }
.healthdot:hover { background: var(--bg-hover); }
.healthpop {
  position: absolute; top: calc(100% + 4px); right: 0; z-index: 80;
  width: 260px; padding: var(--sp-3);
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: var(--radius-pop); box-shadow: var(--shadow-pop);
}
.healthpop__line { margin: 0 0 var(--sp-1); font-size: var(--fs-xs); color: var(--text-secondary); word-break: break-all; }
.healthpop__line.is-error { color: var(--danger); }
</style>
