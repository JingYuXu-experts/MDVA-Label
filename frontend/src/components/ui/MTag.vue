<script setup lang="ts">
import { computed } from 'vue'

/** 标注四态 + 模态五类固定色（UI规格 §4 Tag/Badge）；状态不只靠颜色，必带文字 */
const props = withDefaults(defineProps<{
  kind?: 'state' | 'modality' | 'neutral' | 'success' | 'warning' | 'danger' | 'accent'
  text?: string
}>(), { kind: 'neutral', text: '' })

const cls = computed(() => `tag tag--${props.kind}`)
</script>

<template>
  <span :class="cls"><slot>{{ text }}</slot></span>
</template>

<style scoped>
.tag {
  display: inline-flex; align-items: center; gap: 3px;
  height: 18px; padding: 0 5px;
  border-radius: var(--radius-control);
  font-size: var(--fs-xs); line-height: 1;
  white-space: nowrap;
}
.tag--neutral { background: var(--bg-raised); color: var(--text-secondary); border: 1px solid var(--border); }
/* draft=中性灰、submitted=warning、approved=success、rejected=danger（tokens §2.1） */
.tag--state { background: var(--bg-raised); color: var(--text-secondary); border: 1px solid var(--border); }
.tag--modality { background: var(--accent-soft); color: var(--accent); }
.tag--success { background: rgba(63, 178, 127, 0.14); color: var(--success); }
.tag--warning { background: rgba(217, 160, 63, 0.14); color: var(--warning); }
.tag--danger { background: rgba(224, 82, 82, 0.14); color: var(--danger); }
.tag--accent { background: var(--accent-soft); color: var(--accent); }
</style>
