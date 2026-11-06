<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import MDialog from '@/components/ui/MDialog.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import MButton from '@/components/ui/MButton.vue'
import { mosaicSrc } from '@/api/endpoints'

const props = defineProps<{ open: boolean; uris: string[] }>()
const emit = defineEmits<{ close: [] }>()

const cellW = ref(320)
const cellH = ref(200)
const loading = ref(false)
const failed = ref(false)
const nonce = ref(0)

const src = computed(() =>
  props.open && props.uris.length >= 2
    ? `${mosaicSrc(props.uris, [cellW.value, cellH.value])}&_ts=${nonce.value}`
    : '')

watch(() => props.open, (value) => {
  if (value) { loading.value = props.uris.length >= 2; failed.value = false }
})

// cell 调整后重新直连（UI规格 v1.2 §5.1）
watch(src, (value, old) => {
  if (value && value !== old) { loading.value = true; failed.value = false }
})

function retry() {
  failed.value = false
  loading.value = true
  nonce.value += 1
}
</script>

<template>
  <MDialog :open="open" title="对比拼图" :width="800" @close="emit('close')">
    <div class="mosaic">
      <div class="mosaic__bar">
        <NumberInput v-model="cellW" label="单格宽" :min="32" :max="1920" :step="16" unit="px" />
        <NumberInput v-model="cellH" label="单格高" :min="32" :max="1080" :step="16" unit="px" />
        <span class="mosaic__hint num">{{ uris.length }} 路 · cell {{ cellW }},{{ cellH }}</span>
        <MButton variant="ghost" size="sm" @click="retry">刷新</MButton>
      </div>
      <div class="mosaic__stage">
        <p v-if="uris.length < 2">至少选择 2 个条目再对比。</p>
        <template v-else>
          <span v-if="loading" class="mosaic__spinner" role="status" aria-label="拼图加载中" />
          <p v-if="failed" class="mosaic__error" role="alert">
            拼图加载失败。<MButton variant="secondary" size="sm" @click="retry">重试</MButton>
          </p>
          <img v-if="src" :src="src" class="mosaic__img" :class="{ 'is-hidden': loading || failed }"
            alt="多路同场景对比拼图" @load="loading = false" @error="loading = false; failed = true" />
        </template>
      </div>
    </div>
  </MDialog>
</template>

<style scoped>
.mosaic { display: flex; flex-direction: column; gap: var(--sp-3); }
.mosaic__bar { display: flex; align-items: flex-end; gap: var(--sp-3); flex-wrap: wrap; }
.mosaic__hint { font-size: var(--fs-xs); color: var(--text-secondary); }
.mosaic__stage {
  position: relative; min-height: 240px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-base); border: 1px solid var(--border);
  border-radius: var(--radius-panel); overflow: auto;
}
.mosaic__img { max-width: 100%; display: block; }
.mosaic__img.is-hidden { opacity: 0; position: absolute; pointer-events: none; }
.mosaic__spinner {
  position: absolute; width: 14px; height: 14px; border-radius: 50%;
  border: 2px solid var(--accent-soft); border-top-color: var(--accent);
  animation: spin 0.9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.mosaic__error { color: var(--danger); display: flex; align-items: center; gap: var(--sp-2); }
</style>
