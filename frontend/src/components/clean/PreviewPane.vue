<script setup lang="ts">
/**
 * 中央预览区：命中统计条 + 命中清单 + 分算子明细。
 * 数字全部来自 POST /api/clean/preview——与 apply 走后端同一条 evaluate()，
 * 界面不做任何本地估算，这是"滑块上的数字 = 实际执行条数"的实现口径。
 */
import { computed } from 'vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MSkeleton from '@/components/ui/MSkeleton.vue'
import MTag from '@/components/ui/MTag.vue'
import { useCleanStore } from '@/stores/clean'

const store = useCleanStore()

const pad = (n: number) => String(n).padStart(2, '0')
const previewedAtText = computed(() => {
  if (!store.previewedAt) return ''
  const d = new Date(store.previewedAt)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})
const perOpRows = computed(() => Object.entries(store.stat.perOp).map(([name, v]) => ({ name, ...v })))
/** 未执行时以预览为准；执行后同一份数字来自 apply 的 summary，两者必须相等（验收 1） */
const appliedDelta = computed(() => {
  const a = store.lastApply?.summary
  if (!a || !store.preview) return null
  return { removed: a.n_dropped - store.stat.removed, kept: a.n_kept - store.stat.kept }
})
</script>

<template>
  <section
    class="preview"
    aria-label="清洗预览"
    :data-removed="store.stat.removed"
    :data-total="store.stat.total"
    :data-previewing="String(store.previewing)"
    :data-perop-keys="Object.keys(store.stat.perOp).join(',')"
    :data-previewed-at="String(store.previewedAt)"
  >
    <div class="preview__bar">
      <div class="stat">
        <span class="stat__lead">将移除</span>
        <span class="stat__big num" :class="{ 'is-loading': store.previewing }">{{ store.stat.removed }}</span>
        <span class="stat__of">条 / 共 <b class="num">{{ store.stat.total }}</b> 条</span>
      </div>
      <div class="preview__meta">
        <span class="preview__chip">保留 <b class="num">{{ store.stat.kept }}</b></span>
        <span class="preview__chip">变换 <b class="num">{{ store.stat.transforms }}</b></span>
        <span class="preview__chip">重复组 <b class="num">{{ store.stat.groups }}</b></span>
        <span v-if="store.previewing" class="preview__state" role="status">预览中…</span>
        <span v-else-if="previewedAtText" class="preview__state">预览于 {{ previewedAtText }}</span>
      </div>
    </div>

    <p v-if="appliedDelta && (appliedDelta.removed !== 0 || appliedDelta.kept !== 0)" class="preview__warn">
      上一次执行的 summary 与当前预览数字不一致（移除差 {{ appliedDelta.removed }}、保留差 {{ appliedDelta.kept }}）——
      预览与执行不同源，请报告为缺陷。
    </p>

    <MEmpty
      v-if="!store.hasSelection"
      class="preview__empty"
      text="先在左侧勾选清洗算子"
    />
    <div v-else-if="store.previewError" class="preview__error" role="alert">
      预览失败：{{ store.previewError }}
    </div>
    <MSkeleton v-else-if="store.previewing && !store.preview" :show="true" variant="rows" :count="6" />
    <div v-else class="preview__body">
      <h3 class="preview__h">命中清单</h3>
      <ul v-if="store.hitList.length" class="hits" aria-label="被移除条目">
        <li v-for="row in store.hitList" :key="row.uri" class="hits__row">
          <span class="hits__uri" :title="row.uri">{{ row.uri }}</span>
          <MTag kind="danger" text="移除" />
          <span class="hits__op num">{{ row.op }}</span>
          <span class="hits__reason">{{ row.reason || '' }}</span>
        </li>
      </ul>
      <p v-else class="preview__note">
        预览接口目前只回条数（契约 v1.3 的 summary 形态），逐条命中清单需后端在 preview
        响应里补 <code>dropped</code> 键；补上后本列表自动出现，无需前端改动。
        执行后的逐条丢弃明细见右栏「丢弃明细」。
      </p>

      <h3 class="preview__h">分算子明细</h3>
      <table class="perop">
        <caption class="sr-only">每个算子的参与、移除、变换条数</caption>
        <thead>
          <tr><th scope="col">算子</th><th scope="col">参与</th><th scope="col">移除</th><th scope="col">变换</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in perOpRows" :key="r.name">
            <th scope="row" class="num">{{ r.name }}</th>
            <td class="num">{{ r.n }}</td>
            <td class="num" :class="{ 'is-hit': r.dropped > 0 }">{{ r.dropped }}</td>
            <td class="num">{{ r.transformed }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.preview { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
.preview__bar {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5); border-bottom: 1px solid var(--border); background: var(--bg-raised);
}
.stat { display: flex; align-items: baseline; gap: var(--sp-2); }
.stat__lead { color: var(--text-secondary); font-size: var(--fs-md); }
.stat__big { font-family: var(--font-mono); font-size: 30px; font-weight: 700; color: var(--warning); font-variant-numeric: tabular-nums; }
.stat__big.is-loading { opacity: .45; }
.stat__of { color: var(--text-muted); font-size: var(--fs-xs); }
.preview__meta { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.preview__chip { font-size: var(--fs-xs); color: var(--text-secondary); }
.preview__chip b { color: var(--text-primary); font-family: var(--font-mono); margin-left: 2px; }
.preview__state { font-size: var(--fs-xs); color: var(--text-muted); }
.preview__warn {
  margin: 0; padding: var(--sp-2) var(--sp-5); background: var(--bg-raised);
  border-bottom: 1px solid var(--border); color: var(--danger); font-size: var(--fs-xs);
}
.preview__error { margin: var(--sp-4) var(--sp-5); color: var(--danger); font-size: var(--fs-md); }
.preview__empty { flex: 1; }
.preview__body { flex: 1; min-height: 0; overflow-y: auto; padding: var(--sp-4) var(--sp-5); }
.preview__h { margin: var(--sp-4) 0 var(--sp-2); font-size: var(--fs-md); color: var(--text-primary); }
.preview__h:first-child { margin-top: 0; }
.preview__note {
  margin: 0; padding: var(--sp-3); border: 1px dashed var(--border-strong); border-radius: var(--radius-control);
  color: var(--text-secondary); font-size: var(--fs-xs); line-height: 1.6;
}
.preview__note code { font-family: var(--font-mono); color: var(--accent); }
.hits { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.hits__row { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1) 0; border-bottom: 1px solid var(--border); font-size: var(--fs-xs); }
.hits__uri { font-family: var(--font-mono); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 46%; }
.hits__op { color: var(--text-secondary); }
.hits__reason { color: var(--text-muted); }
.perop { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.perop th, .perop td { text-align: right; padding: 6px var(--sp-2); border-bottom: 1px solid var(--border); }
.perop thead th { color: var(--text-muted); font-weight: 500; }
.perop tbody th { text-align: left; color: var(--text-primary); font-weight: 500; }
.perop td.num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.perop td.is-hit { color: var(--danger); font-weight: 600; }
.sr-only {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
</style>
