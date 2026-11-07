<script setup lang="ts">
import { watch } from 'vue'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import { useBackendsStore } from '@/stores/backends'

/**
 * 左栏筛选（§5.6）：三个下拉全部映射到 GET /api/backends 的 query，
 * 空值＝"不限"由后端 covers() 解释——前端不做二次过滤，否则验收 4
 * （按 task 筛选不得返回空列表）就变成前端说了算。
 */
const store = useBackendsStore()

watch(() => [store.domain, store.task, store.modality], () => { void store.applyFilters() })

function clearAll(): void {
  store.domain = ''
  store.task = ''
  store.modality = ''
}
</script>

<template>
  <div class="fpanel" data-testid="backends-filter">
    <h2 class="fpanel__h">筛选</h2>
    <MSelect v-model="store.domain" label="domain（学科）" :options="store.domainOpts" width="100%" />
    <MSelect v-model="store.task" label="task（任务）" :options="store.taskOpts" width="100%" />
    <MSelect v-model="store.modality" label="modality（模态）" :options="store.modalityOpts" width="100%" />
    <p class="fpanel__hint">
      候选值取自未过滤的全量清单：后端加一种 domain/task，这里自动多一项，界面不写死枚举。
    </p>
    <div class="fpanel__row">
      <MButton size="sm" variant="ghost" :disabled="!store.domain && !store.task && !store.modality"
        @click="clearAll">
        清空筛选
      </MButton>
      <span class="fpanel__sum num" :data-n="store.backends.length" :data-available="store.nAvailable">
        {{ store.backends.length }} / {{ store.all.length }} 条 · 可用 {{ store.nAvailable }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.fpanel { display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-4); flex: none; border-bottom: 1px solid var(--border); }
.fpanel__h { margin: 0; font-size: var(--fs-md); color: var(--text-primary); font-weight: 600; }
.fpanel__hint { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.55; }
.fpanel__row { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); flex-wrap: wrap; }
.fpanel__sum { font-size: var(--fs-xs); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
</style>
