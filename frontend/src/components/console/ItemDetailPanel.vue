<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Pencil, PackagePlus } from 'lucide-vue-next'
import KV from '@/components/ui/KV.vue'
import MTag from '@/components/ui/MTag.vue'
import MButton from '@/components/ui/MButton.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import { frameUrl, getFrameMeta } from '@/api/endpoints'
import type { ItemRow, FrameMeta } from '@/api/types'
import { bucketOf } from '@/stores/items'
import { useCtxStore } from '@/stores/ctx'

const props = defineProps<{ item: ItemRow | null }>()
const emit = defineEmits<{ annotate: [uri: string]; collect: [uri: string] }>()

const ctx = useCtxStore()
const meta = ref<FrameMeta | null>(null)

watch(() => props.item?.uri, async (uri) => {
  meta.value = null
  if (!uri) return
  try {
    meta.value = await getFrameMeta(uri, { width: 320 })
  } catch {
    meta.value = null
  }
})

const STATE_TEXT: Record<string, string> = {
  draft: '草稿', submitted: '待审', approved: '已批', rejected: '驳回',
}
const STATE_KIND: Record<string, 'neutral' | 'warning' | 'success' | 'danger'> = {
  draft: 'neutral', submitted: 'warning', approved: 'success', rejected: 'danger',
}

const entries = computed(() => {
  const it = props.item
  if (!it) return []
  const m = meta.value
  return [
    { key: 'uri', value: it.uri, mono: true },
    { key: '模态', value: it.modality || '未知（服务端安全回退）' },
    { key: '尺寸', value: m ? `${m.width}×${m.height}` : '—' },
    { key: '时间范围', value: m && 't_span' in m ? String(m['t_span']) : '—' },
    { key: '标注数', value: it.n_annotations },
    { key: '版本', value: ctx.version || '—', mono: true },
    { key: '来源', value: it.sources.length ? it.sources.join('、') : '—' },
  ]
})

const stateTags = computed(() => {
  const it = props.item
  if (!it) return []
  return (Object.entries(it.states || {}) as [string, number][])
    .filter(([, n]) => n > 0)
    .map(([state, n]) => ({ state, n }))
})

const bucket = computed(() => props.item ? bucketOf(props.item) : 'unlabeled')
</script>

<template>
  <aside class="detail" aria-label="选中条目详情">
    <MEmpty v-if="!item" text="点击左侧条目查看详情。" />
    <template v-else>
      <div class="detail__preview">
        <img :src="frameUrl(item.uri, { width: 320 })" :alt="item.name" />
      </div>
      <div class="detail__tags">
        <MTag :kind="bucket === 'approved' ? 'success' : bucket === 'submitted' ? 'warning' : 'neutral'"
          :text="bucket === 'unlabeled' ? '未标注' : STATE_TEXT[bucket] || bucket" />
        <MTag v-if="item.modality" kind="modality" :text="String(item.modality)" />
        <MTag v-for="tag in stateTags" :key="tag.state" :kind="STATE_KIND[tag.state] || 'neutral'"
          :text="`${STATE_TEXT[tag.state] || tag.state} ${tag.n}`" />
        <MTag v-if="item.machine_only" kind="accent" text="仅机器标注" />
      </div>
      <KV :entries="entries" dense />
      <p v-if="item.labels.length" class="detail__labels">
        类别：<MTag v-for="label in item.labels" :key="label" :text="label" />
      </p>
      <div class="detail__actions">
        <MButton variant="primary" size="sm" @click="emit('annotate', item.uri)">
          <Pencil :size="14" aria-hidden="true" /> 去标注
        </MButton>
        <MButton variant="secondary" size="sm" @click="emit('collect', item.uri)">
          <PackagePlus :size="14" aria-hidden="true" /> 收录到…
        </MButton>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.detail {
  width: 320px; flex: none; overflow-y: auto;
  background: var(--bg-panel); border-left: 1px solid var(--border);
  padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-3);
}
.detail__preview {
  background: var(--bg-base); border: 1px solid var(--border);
  border-radius: var(--radius-panel); min-height: 120px;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.detail__preview img { max-width: 100%; display: block; }
.detail__tags { display: flex; flex-wrap: wrap; gap: var(--sp-1); }
.detail__labels { margin: 0; font-size: var(--fs-ui); color: var(--text-secondary); display: flex; flex-wrap: wrap; gap: var(--sp-1); align-items: center; }
.detail__actions { display: flex; gap: var(--sp-2); margin-top: auto; }
</style>
