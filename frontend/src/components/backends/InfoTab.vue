<script setup lang="ts">
import { computed } from 'vue'
import KV from '@/components/ui/KV.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import type { BackendRow, OpParamSpec } from '@/api/types'

/**
 * 清单详情：covers / params / reason 三段全部转写后端元数据，
 * 前端不做"能不能用"的二次判断——可用性由 /backends 的 available 说，reason 原文照抄。
 */
const props = defineProps<{ row: BackendRow | null }>()

const yesNo = (v: boolean): string => (v ? '是' : '否')

const entries = computed(() => {
  const b = props.row
  if (!b) return []
  return [
    { key: '后端名', value: b.name, mono: true },
    { key: '家族', value: b.family, mono: true },
    { key: '覆盖任务', value: (b.tasks || []).join(' · ') || '不限', mono: true },
    { key: '覆盖模态', value: (b.modalities || []).join(' · ') || '不限', mono: true },
    { key: '学科', value: (b.domains || []).join(' · ') || '*', mono: true },
    { key: '支持提示词', value: `${yesNo(b.supports_prompt)}${b.prompt_kinds?.length ? `（${b.prompt_kinds.join(' / ')}）` : ''}` },
    { key: '需显卡', value: yesNo(b.needs_gpu) },
    { key: '需权重下载', value: yesNo(b.needs_weights) },
    { key: '许可', value: b.license },
    { key: '可用性', value: b.available ? '可用' : '不可用' },
  ]
})

const paramRows = computed(() => {
  const schema = props.row?.params || {}
  return Object.entries(schema).map(([key, spec]) => ({
    key,
    type: (spec as OpParamSpec).type || '',
    def: (spec as OpParamSpec).default ?? '',
    desc: (spec as OpParamSpec).description || '',
  }))
})
</script>

<template>
  <div class="info" data-testid="backend-info">
    <MEmpty v-if="!props.row" text="左侧清单里选一个后端，这里显示它的元数据。" />
    <template v-else>
      <KV :entries="entries" dense />
      <p v-if="props.row.note" class="info__note">{{ props.row.note }}</p>
      <p v-if="!props.row.available" class="info__reason" role="status" data-testid="backend-reason">
        不可用原因（后端原文）：{{ props.row.reason }}
      </p>
      <h3 class="info__h">参数（schema 来自元数据）</h3>
      <p v-if="!paramRows.length" class="info__none">该后端不接受参数。</p>
      <table v-else class="info__tbl">
        <thead>
          <tr><th>参数</th><th>类型</th><th>默认</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in paramRows" :key="r.key">
            <td class="mono">{{ r.key }}</td>
            <td>{{ r.type }}</td>
            <td class="mono">{{ r.def }}</td>
            <td class="info__desc">{{ r.desc }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.info { display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-4) var(--sp-5); }
.info__h { margin: var(--sp-3) 0 0; font-size: var(--fs-md); color: var(--text-primary); font-weight: 600; }
.info__note { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.info__reason { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.info__none { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.info__tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.info__tbl th, .info__tbl td { text-align: left; padding: 5px var(--sp-2); border-bottom: 1px solid var(--border); }
.info__tbl thead th { color: var(--text-muted); font-weight: 500; }
.info__tbl td { color: var(--text-primary); }
.info__desc { color: var(--text-secondary); }
</style>
