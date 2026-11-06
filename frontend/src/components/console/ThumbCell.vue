<script setup lang="ts">
import { computed } from 'vue'
import { ImageOff } from 'lucide-vue-next'
import MCheckbox from '@/components/ui/MCheckbox.vue'
import type { ItemRow, ThumbRow } from '@/api/types'
import { bucketOf } from '@/stores/items'

const props = defineProps<{
  item: ItemRow
  thumb?: ThumbRow
  loading?: boolean
  selected?: boolean
  active?: boolean
}>()

const emit = defineEmits<{ click: []; 'toggle-select': [] }>()

const bucket = computed(() => bucketOf(props.item))

const imgSrc = computed(() =>
  props.thumb && props.thumb.png ? `data:image/png;base64,${props.thumb.png}` : '')

const subtitle = computed(() => {
  if (props.thumb?.error) return '无法打开'
  if (props.thumb?.subtitle) return props.thumb.subtitle
  const mods = props.item.modality ? String(props.item.modality) : '未知'
  return `${mods} · ${props.item.n_annotations} 标注`
})
</script>

<template>
  <figure class="cell" :class="[`is-${bucket}`, { 'is-active': active }]"
    @click="emit('click')">
    <div class="cell__img">
      <img v-if="imgSrc" :src="imgSrc" :alt="item.name" loading="lazy" />
      <span v-else-if="thumb?.error" class="cell__broken" :title="thumb.error">
        <ImageOff :size="20" aria-hidden="true" />
      </span>
      <span v-else-if="loading" class="cell__loading" role="status" aria-label="缩略图加载中" />
      <span class="cell__badge" :aria-label="`标注状态：${bucket}`" />
      <span class="cell__check" @click.stop>
        <MCheckbox :model-value="selected ?? false" :aria-label="`选择 ${item.name}`"
          @update:model-value="emit('toggle-select')" />
      </span>
    </div>
    <figcaption class="cell__name" :title="item.uri">{{ item.name }}</figcaption>
    <p class="cell__sub">{{ subtitle }}</p>
  </figure>
</template>

<style scoped>
.cell {
  margin: 0; padding: 0; cursor: pointer;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius-panel); overflow: hidden;
  display: flex; flex-direction: column; width: 160px;
}
.cell:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.cell.is-active { border-color: var(--accent); }
.cell__img { position: relative; width: 160px; height: 100px; background: var(--bg-base); }
.cell__img img { width: 100%; height: 100%; object-fit: contain; display: block; }
.cell__broken,
.cell__loading {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-muted);
}
.cell__loading::after {
  content: ''; width: 12px; height: 12px; border-radius: 50%;
  border: 2px solid var(--accent-soft); border-top-color: var(--accent);
  animation: spin 0.9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
/* 角标=标注状态色（UI规格 §5.1）：未标注灰、草稿灰、待审黄、已批绿 */
.cell__badge {
  position: absolute; top: 4px; left: 4px; width: 8px; height: 8px; border-radius: 2px;
  background: var(--text-muted);
}
.cell.is-submitted .cell__badge { background: var(--warning); }
.cell.is-approved .cell__badge { background: var(--success); }
.cell.is-unlabeled .cell__badge { background: transparent; border: 1px solid var(--text-muted); }
.cell__check {
  position: absolute; top: 2px; right: 4px;
  opacity: 0; transition: opacity 0.1s;
  background: rgba(20, 22, 26, 0.6); border-radius: var(--radius-control); padding: 1px;
}
.cell:hover .cell__check, .cell__check:focus-within { opacity: 1; }
.cell:has(.cell__check input:checked) .cell__check { opacity: 1; }
.cell__name {
  padding: 3px var(--sp-1) 0; font-size: var(--fs-xs);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.cell__sub {
  margin: 0; padding: 0 var(--sp-1) 4px;
  font-size: var(--fs-xs); color: var(--text-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
</style>
