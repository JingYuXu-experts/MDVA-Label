<script setup lang="ts">
import { computed } from 'vue'
import { MousePointer2, Square, Hexagon, MapPin, AlignHorizontalJustifyCenter, Rows3, Undo2, Redo2, Save, Send } from 'lucide-vue-next'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import { useCanvasStore, type Mode } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'

const props = defineProps<{ labelOptions: string[] }>()

const store = useCanvasStore()
const ctx = useCtxStore()

const MODE_OPTIONS = computed(() => {
  const base: { value: Mode; label: string; icon: unknown; title: string }[] = [
    { value: 'select', label: '选择', icon: MousePointer2, title: '1 选择/移动/缩放' },
    { value: 'box', label: '框', icon: Square, title: '2 矩形（拖拽）' },
    { value: 'poly', label: '多边形', icon: Hexagon, title: '3 多边形（逐点，双击/Enter 闭合）' },
    { value: 'point', label: '点', icon: MapPin, title: '4 点（单击）' },
    { value: 'interval', label: '时段', icon: AlignHorizontalJustifyCenter, title: '5 时段（时序拖拽）' },
    { value: 'level', label: '水平带', icon: Rows3, title: '6 水平带（时序拖拽）' },
  ]
  return base
})

const labelOpts = computed(() => props.labelOptions.map((l) => ({ value: l, label: l })))
const canSubmit = computed(() => store.currentUri && ctx.role !== 'reviewer' && store.draftAnns.length > 0)
const submitWhy = computed(() =>
  ctx.role === 'reviewer' ? 'reviewer 不能提交，请切换角色/管理员'
    : !store.draftAnns.length ? '当前条目无标注' : '')
</script>

<template>
  <div class="toolbar">
    <SegmentedControl
      :model-value="store.mode"
      :options="MODE_OPTIONS"
      aria-label="绘制模式"
      @update:model-value="store.setMode($event)"
    />
    <MSelect
      :model-value="store.label"
      :options="labelOpts"
      label="类别"
      width="150px"
      @update:model-value="store.label = $event"
    />
    <div class="toolbar__spacer" />
    <MButton variant="ghost" size="sm" :disabled="!store.canUndo" title="Ctrl+Z" @click="store.undo()">
      <Undo2 :size="14" /> 撤销
    </MButton>
    <MButton variant="ghost" size="sm" :disabled="!store.canRedo" title="Ctrl+Shift+Z" @click="store.redo()">
      <Redo2 :size="14" /> 重做
    </MButton>
    <MButton variant="primary" size="sm" :loading="store.saving" title="Ctrl+S" @click="store.save()">
      <Save :size="14" /> 保存<span v-if="store.dirty" class="dot" aria-label="有未保存修改" />
    </MButton>
    <MButton variant="secondary" size="sm" :disabled="!canSubmit" :title="canSubmit ? '整条提交审核' : submitWhy" @click="store.submitItem()">
      <Send :size="14" /> 提交审核
    </MButton>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); background: var(--bg-panel); border-bottom: 1px solid var(--border); flex: none; }
.toolbar__spacer { flex: 1; }
.dot { display: inline-block; width: 6px; height: 6px; margin-left: 5px; border-radius: 50%; background: var(--warning); vertical-align: middle; }
</style>
