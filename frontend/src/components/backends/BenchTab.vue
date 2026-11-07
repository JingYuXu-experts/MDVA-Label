<script setup lang="ts">
import { computed, ref } from 'vue'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import MSwitch from '@/components/ui/MSwitch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import MProgress from '@/components/ui/MProgress.vue'
import MTable from '@/components/ui/MTable.vue'
import { useBackendsStore } from '@/stores/backends'
import { useCtxStore } from '@/stores/ctx'
import type { BenchRow } from '@/api/types'
import type { TableColumn } from '@/components/ui/MTable.vue'

/**
 * 实测（§5.6）：POST /api/bench 是同步分钟级长请求，按契约 §3.2-5 处理——
 * 按钮 loading + indeterminate Progress + 可取消，取消后不谎报"已停止"，
 * 而是提示去任务清单核对（后端已开始的那次会继续落表）。
 */
const store = useBackendsStore()
const ctx = useCtxStore()

const root = ref('')
const limit = ref(4)
const stubs = ref(false)

const rootOpts = computed(() => ctx.datasets.map((d) => ({ value: d.root, label: `${d.name} · ${d.root}` })))
const chosenRoot = computed({
  get: () => root.value || ctx.datasets[0]?.root || '',
  set: (v: string) => { root.value = v },
})

const cols: TableColumn<BenchRow>[] = [
  { key: 'backend', label: '后端', mono: true, sortable: true },
  { key: 'task', label: '任务', mono: true, sortable: true },
  { key: 'category', label: '类别', mono: true },
  { key: 'precision', label: 'P', align: 'right', mono: true, sortable: true },
  { key: 'recall', label: 'R', align: 'right', mono: true, sortable: true },
  { key: 'f1', label: 'F1', align: 'right', mono: true, sortable: true },
  { key: 'latency_ms', label: '延迟 ms', align: 'right', mono: true, sortable: true },
  { key: 'n_items', label: '路数', align: 'right', mono: true },
  { key: 'kind', label: '口径', mono: true },
  { key: 'note', label: '备注' },
]

async function start(): Promise<void> {
  await store.runBench(chosenRoot.value, limit.value, stubs.value)
}
</script>

<template>
  <div class="bench" data-testid="bench-tab">
    <div class="bench__form" data-testid="bench-form">
      <div class="bench__field bench__field--wide">
        <span class="bench__lbl">数据集 root</span>
        <MSelect v-model="chosenRoot" :options="rootOpts" width="100%"
          :disabled="store.benching || !rootOpts.length" placeholder="当前工作区没有数据集" />
      </div>
      <NumberInput v-model="limit" label="limit（每类取几路）" :min="1" :max="64" :step="1"
        :disabled="store.benching" />
      <MSwitch v-model="stubs" label="含占位后端（--include-stubs）" :disabled="store.benching" />
      <MButton variant="primary" :loading="store.benching" :disabled="!chosenRoot" @click="start">
        开始实测
      </MButton>
      <MButton v-if="store.benching" variant="ghost" @click="store.cancelBench()">
        取消等待
      </MButton>
    </div>

    <div v-if="store.benching" class="bench__running">
      <MProgress :value="-1" label="实测进行中（同步长请求，分钟级）" />
      <p class="bench__runote">
        实测是同步长请求：取消只是不再等待结果，后端这次评测会继续跑完并落表，
        可稍后在任务清单核对结果。
      </p>
    </div>

    <p v-if="store.benchError" class="bench__err" role="alert">实测失败：{{ store.benchError }}</p>
    <p v-if="store.benchTablePath" class="bench__path mono" :data-table="store.benchTablePath">
      实测表：{{ store.benchTablePath }}
    </p>

    <MTable :rows="store.benchRows" :columns="cols" :row-key="(r) => `${r.backend}|${r.task}|${r.category}`"
      empty-text="还没有实测表：点「开始实测」跑一次，或在命令行执行 mdva bench 后回到本页刷新。" />
  </div>
</template>

<style scoped>
.bench { display: flex; flex-direction: column; gap: var(--sp-4); padding: var(--sp-4) var(--sp-5); }
.bench__form { display: flex; align-items: flex-end; gap: var(--sp-3); flex-wrap: wrap; }
.bench__field { display: flex; flex-direction: column; gap: 4px; }
.bench__field--wide { min-width: 260px; }
.bench__lbl { font-size: var(--fs-xs); color: var(--text-secondary); }
.bench__running { display: flex; flex-direction: column; gap: var(--sp-2); }
.bench__runote { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.bench__err { margin: 0; font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.bench__path { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); word-break: break-all; }
</style>
