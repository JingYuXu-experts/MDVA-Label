<script setup lang="ts">
import { computed, ref } from 'vue'
import MDialog from '@/components/ui/MDialog.vue'
import MInput from '@/components/ui/MInput.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MSwitch from '@/components/ui/MSwitch.vue'
import MCheckbox from '@/components/ui/MCheckbox.vue'
import MButton from '@/components/ui/MButton.vue'
import * as api from '@/api/endpoints'
import { useToast } from '@/stores/toast'

const props = defineProps<{ open: boolean; uris: string[] }>()
const emit = defineEmits<{ close: [] }>()

const toast = useToast()

const MODE_OPTIONS: { value: 'copy' | 'move'; label: string }[] = [
  { value: 'copy', label: 'copy（复制，原件不动）' },
  { value: 'move', label: 'move（移动）' },
]

const target = ref('')
const mode = ref<'copy' | 'move'>('copy')
const flatten = ref(false)
const submitting = ref(false)
const error = ref('')

const lastManifest = ref('')
const latestLoading = ref(false)
const latestInfo = ref('')
const undoing = ref(false)
const force = ref(false)

const canSubmit = computed(() => props.uris.length > 0 && target.value.trim() !== '')

async function onSubmit() {
  error.value = ''
  submitting.value = true
  try {
    const resp = await api.collect(props.uris, target.value.trim(), mode.value, flatten.value)
    lastManifest.value = resp.manifest || ''
    toast.success(`收录完成：${props.uris.length} 个条目 → ${target.value.trim()}`)
    emit('close')
  } catch (err) {
    error.value = `收录失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    submitting.value = false
  }
}

async function onQueryLatest() {
  if (!target.value.trim()) { error.value = '先填写目标目录'; return }
  latestLoading.value = true
  latestInfo.value = ''
  error.value = ''
  try {
    const resp = await api.collectLatest(target.value.trim()) as { manifest?: string; n?: number }
    if (resp.manifest) {
      lastManifest.value = resp.manifest
      latestInfo.value = `最近清单：${resp.manifest}${resp.n !== undefined ? `（${resp.n} 个文件）` : ''}`
    } else {
      latestInfo.value = '该目录暂无收录清单'
    }
  } catch (err) {
    error.value = `查询失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    latestLoading.value = false
  }
}

async function onUndo() {
  if (!lastManifest.value) return
  undoing.value = true
  error.value = ''
  try {
    await api.collectUndo(lastManifest.value, true, force.value)
    toast.success('收录已撤销')
    lastManifest.value = ''
    latestInfo.value = ''
  } catch (err) {
    // 目标文件被改过 → 拒绝（400）；force 开关二次确认（UI规格 §6）
    error.value = `撤销失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    undoing.value = false
  }
}
</script>

<template>
  <MDialog :open="open" title="收录到…" :width="640" @close="emit('close')">
    <div class="form">
      <p class="form__count">已选 <span class="num">{{ uris.length }}</span> 个条目</p>
      <MInput v-model="target" label="目标目录（磁盘绝对路径）" mono placeholder="如：F:\\collected\\batch1" />
      <div class="form__row">
        <MSelect v-model="mode" :options="MODE_OPTIONS" label="方式" width="220px" />
        <MSwitch v-model="flatten" label="flatten（拍平目录层级）" />
      </div>

      <div class="latest">
        <MButton variant="ghost" size="sm" :loading="latestLoading" @click="onQueryLatest">
          查询该目录最近清单
        </MButton>
        <span v-if="latestInfo" class="latest__info mono">{{ latestInfo }}</span>
      </div>
      <div v-if="lastManifest" class="undo">
        <MButton variant="danger" size="sm" :loading="undoing" @click="onUndo">
          撤销上次收录
        </MButton>
        <MCheckbox v-model="force" label="force（目标文件被改过也强制撤销）" />
      </div>

      <p v-if="error" class="form__error" role="alert">{{ error }}</p>
    </div>
    <template #footer>
      <MButton variant="ghost" @click="emit('close')">关闭</MButton>
      <MButton variant="primary" :loading="submitting" :disabled="!canSubmit" @click="onSubmit">
        执行收录
      </MButton>
    </template>
  </MDialog>
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: var(--sp-3); }
.form__count { margin: 0; color: var(--text-secondary); }
.form__row { display: flex; gap: var(--sp-4); align-items: flex-end; flex-wrap: wrap; }
.latest { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.latest__info { font-size: var(--fs-xs); color: var(--text-secondary); word-break: break-all; }
.undo { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.form__error { margin: 0; font-size: var(--fs-ui); color: var(--danger); }
</style>
