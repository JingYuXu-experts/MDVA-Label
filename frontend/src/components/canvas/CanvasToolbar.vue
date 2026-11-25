<script setup lang="ts">
import { computed,ref } from 'vue'
import { MousePointer2, Square, Hexagon, MapPin, AlignHorizontalJustifyCenter, Rows3, Undo2, Redo2, Save, Send } from 'lucide-vue-next'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import MDialog from '@/components/ui/MDialog.vue'
import MInput from '@/components/ui/MInput.vue'
import * as api from '@/api/endpoints'
import { useCanvasStore, type Mode } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'

const props = defineProps<{ labelOptions: string[] }>()
const emit=defineEmits<{labelsChanged:[]}>()
const labelDialog=ref(false),newLabel=ref(''),labelError=ref(''),savingLabel=ref(false)
async function addLabel(){
  const name=newLabel.value.trim();if(!name){labelError.value='请输入类别名称';return}
  const dataset=ctx.dataset;savingLabel.value=true;labelError.value=''
  try{const row=await api.getDataset(dataset);const labels=(row.labels||[]) as {name:string;color?:string}[];if(labels.some(l=>l.name===name))throw new Error('这个类别已经存在');await api.setLabels(dataset,[...labels,{name}]);if(ctx.dataset!==dataset)return;store.label=name;emit('labelsChanged');labelDialog.value=false;newLabel.value=''}catch(e){labelError.value=String(e)}finally{savingLabel.value=false}
}

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
  return store.modality==='series'?base:base.filter(o=>o.value!=='interval'&&o.value!=='level')
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
    <span v-if="store.saveError" class="save-error" role="alert">保存失败：{{store.saveError}}；修改仍保留。</span>
    <MButton variant="ghost" size="sm" :disabled="!ctx.dataset||store.saving" @click="labelDialog=true">添加类别</MButton>
    <MButton variant="ghost" size="sm" :disabled="!store.canUndo" title="Ctrl+Z" @click="store.undo()">
      <Undo2 :size="14" /> 撤销
    </MButton>
    <MButton variant="ghost" size="sm" :disabled="!store.canRedo" title="Ctrl+Shift+Z" @click="store.redo()">
      <Redo2 :size="14" /> 重做
    </MButton>
    <MButton variant="primary" size="sm" :loading="store.saving" :disabled="store.loading||!store.currentUri" title="Ctrl+S" @click="store.save()">
      <Save :size="14" /> 保存<span v-if="store.dirty" class="dot" aria-label="有未保存修改" />
    </MButton>
    <MButton variant="secondary" size="sm" :disabled="!canSubmit" :title="canSubmit ? '整条提交审核' : submitWhy" @click="store.submitItem()">
      <Send :size="14" /> 提交审核
    </MButton>
    <MDialog :open="labelDialog" title="添加标注类别" @close="labelDialog=false"><p>添加后，这个数据集内的图片都可以使用该类别。已有类别和颜色会保留。</p><MInput v-model="newLabel" label="类别名称" placeholder="例如 AR、CH、FL"/><p v-if="labelError" role="alert">{{labelError}}</p><template #footer><MButton variant="ghost" @click="labelDialog=false">取消</MButton><MButton variant="primary" :loading="savingLabel" @click="addLabel">添加并使用</MButton></template></MDialog>
  </div>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); background: var(--bg-panel); border-bottom: 1px solid var(--border); flex: none; }
.toolbar__spacer { flex: 1; }
.toolbar{flex-wrap:wrap}.save-error{font-size:11px;color:var(--danger);max-width:280px}
.dot { display: inline-block; width: 6px; height: 6px; margin-left: 5px; border-radius: 50%; background: var(--warning); vertical-align: middle; }
</style>
