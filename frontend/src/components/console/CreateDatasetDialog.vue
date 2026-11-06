<script setup lang="ts">
import { computed, ref } from 'vue'
import MDialog from '@/components/ui/MDialog.vue'
import MInput from '@/components/ui/MInput.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MTextarea from '@/components/ui/MTextarea.vue'
import MButton from '@/components/ui/MButton.vue'
import * as api from '@/api/endpoints'
import type { ScanResp } from '@/api/types'
import { useToast } from '@/stores/toast'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; created: [datasetId: string] }>()

const toast = useToast()

const TYPE_OPTIONS = [
  { value: 'detection', label: 'detection' },
  { value: 'classification', label: 'classification' },
  { value: 'segmentation', label: 'segmentation' },
  { value: 'series_interval', label: 'series_interval' },
  { value: 'event_level', label: 'event_level' },
  { value: 'mixed', label: 'mixed' },
]

const name = ref('')
const type = ref('detection')
const root = ref('')
const labelsText = ref('')
const scanning = ref(false)
const submitting = ref(false)
const scanResult = ref<ScanResp | null>(null)
const error = ref('')

const scanSummary = computed(() => {
  const s = scanResult.value
  if (!s) return ''
  const mods = Object.entries(s.modalities).map(([k, v]) => `${k}×${v}`).join('、')
  return `发现 ${s.n_items} 路，${Object.keys(s.modalities).length} 种模态（${mods}），耗时 ${s.elapsed_ms}ms`
})

function reset() {
  error.value = ''
  scanResult.value = null
}

async function onScan() {
  if (!root.value.trim()) { error.value = '先填写数据目录'; return }
  scanning.value = true
  error.value = ''
  try {
    scanResult.value = await api.scanRoot(root.value.trim())
  } catch (err) {
    error.value = `探测失败：${err instanceof Error ? err.message : String(err)}`
    scanResult.value = null
  } finally {
    scanning.value = false
  }
}

async function onSubmit() {
  error.value = ''
  if (!name.value.trim()) { error.value = '缺少数据集名称'; return }
  if (!root.value.trim()) { error.value = '缺少数据目录'; return }
  const labels = labelsText.value.split('\n').map(s => s.trim()).filter(Boolean)
  submitting.value = true
  try {
    const resp = await api.createDataset(name.value.trim(), root.value.trim(), type.value, '', labels)
    toast.success(`数据集已创建：${name.value.trim()}`)
    emit('created', resp.dataset_id)
    name.value = ''; root.value = ''; labelsText.value = ''; reset()
    emit('close')
  } catch (err) {
    error.value = `建集失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <MDialog :open="open" title="新建数据集" :width="640" @close="emit('close')">
    <div class="form">
      <div class="form__row">
        <MInput v-model="name" label="名称" placeholder="如：遥感检测样例" class="grow" />
        <MSelect v-model="type" :options="TYPE_OPTIONS" label="类型" width="160px" />
      </div>
      <MInput v-model="root" label="数据目录（磁盘绝对路径）" mono
        placeholder="如：F:\\data\\samples" />
      <div class="form__row form__row--tight">
        <MButton variant="secondary" size="sm" :loading="scanning" @click="onScan">
          只读探测
        </MButton>
        <span v-if="scanSummary" class="scan-summary">{{ scanSummary }}</span>
      </div>
      <MTextarea v-model="labelsText" label="类别表（每行一个类别名，可留空）" :rows="4"
        placeholder="car&#10;person&#10;airplane" />
      <p v-if="error" class="form__error" role="alert">{{ error }}</p>
    </div>
    <template #footer>
      <MButton variant="ghost" @click="emit('close')">取消</MButton>
      <MButton variant="primary" :loading="submitting" @click="onSubmit">创建</MButton>
    </template>
  </MDialog>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: var(--sp-3); }
.form__row { display: flex; gap: var(--sp-3); align-items: flex-end; }
.form__row--tight { align-items: center; gap: var(--sp-2); }
.grow { flex: 1; }
.scan-summary { font-size: var(--fs-xs); color: var(--text-secondary); }
.form__error { margin: 0; font-size: var(--fs-ui); color: var(--danger); }
</style>
