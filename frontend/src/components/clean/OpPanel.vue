<script setup lang="ts">
/** 左栏：清洗算子面板（元数据驱动，勾选 + 参数）。§5.3 布局 280px。 */
import { computed } from 'vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MSkeleton from '@/components/ui/MSkeleton.vue'
import MInput from '@/components/ui/MInput.vue'
import OpItem from './OpItem.vue'
import { useCleanStore } from '@/stores/clean'

const store = useCleanStore()

const chainText = computed(() => store.selectedOps.map(o => o.name).join(' → ') || '未选')
</script>

<template>
  <aside class="oppanel" aria-label="清洗算子面板">
    <header class="oppanel__head">
      <h2 class="oppanel__title">清洗算子</h2>
      <p class="oppanel__hint">勾选后按列表顺序串联执行；参数改动即自动重新预览。</p>
    </header>

    <MSkeleton v-if="store.opsLoading" :show="true" variant="rows" :count="5" />
    <MEmpty v-else-if="store.opsError" :text="`算子表读不出来：${store.opsError}`" />
    <MEmpty v-else-if="!store.ops.length" text="后端未登记任何清洗算子" />

    <div v-else class="oppanel__list" role="group" aria-label="算子清单">
      <OpItem
        v-for="meta in store.ops"
        :key="meta.name"
        :meta="meta"
        :state="store.state[meta.name]"
        @toggle="store.toggleOp(meta.name, $event)"
        @param="(key, value) => store.setParam(meta.name, key, value)"
      />
    </div>

    <footer class="oppanel__foot">
      <MInput
        :model-value="store.calibration"
        label="标定文件（可选）"
        placeholder="留空则不注入标定"
        aria-label="标定文件相对路径"
        @update:model-value="store.calibration = String($event); store.schedulePreview()"
      />
      <p class="oppanel__chain">
        <span class="oppanel__chainlabel">算子链</span>
        <code class="num">{{ chainText }}</code>
      </p>
    </footer>
  </aside>
</template>

<style scoped>
.oppanel {
  width: 280px; flex: 0 0 280px; display: flex; flex-direction: column;
  background: var(--bg-panel); border-right: 1px solid var(--border); overflow: hidden;
}
.oppanel__head { padding: var(--sp-3); border-bottom: 1px solid var(--border); }
.oppanel__title { margin: 0; font-size: var(--fs-md); font-weight: 600; color: var(--text-primary); }
.oppanel__hint { margin: 4px 0 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.45; }
.oppanel__list { flex: 1; min-height: 0; overflow-y: auto; }
.oppanel__foot { padding: var(--sp-3); border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: var(--sp-2); }
.oppanel__chain { margin: 0; display: flex; align-items: baseline; gap: var(--sp-2); font-size: var(--fs-xs); }
.oppanel__chainlabel { color: var(--text-muted); }
.oppanel__chain code { color: var(--text-secondary); word-break: break-all; }
</style>
