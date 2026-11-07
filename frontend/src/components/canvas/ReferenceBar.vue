<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MSelect from '@/components/ui/MSelect.vue'
import { get } from '@/api/client'
import { useCanvasStore, type Geom } from '@/stores/canvas'
import { useToast } from '@/stores/toast'

const store = useCanvasStore()
const toast = useToast()
const available = ref(false)
const instances = ref<{id:number; pixels:number; noaa:string|null}[]>([])
const error = ref('')
const busy = ref(false)
let generation = 0
const options = computed(() => [{value:0,label:'全部实例'}, ...instances.value.map(i => ({value:i.id,label:`实例 ${i.id}${i.noaa ? ' · NOAA '+i.noaa : ''} · ${i.pixels} 像素`}))])
watch(() => store.currentUri, async (uri) => {
  const run = ++generation
  available.value = false; error.value = ''; store.referenceShown = false; store.referenceInstance = 0
  if (!uri) return
  try {
    const resp = await get<{available:boolean;instances:typeof instances.value}>('/reference', {uri})
    if (run !== generation) return
    available.value = resp.available; instances.value = resp.instances; store.referenceShown = resp.available
  } catch (err) { if (run === generation) error.value = String(err) }
}, {immediate:true})
async function copyContours() {
  busy.value = true
  const uri = store.currentUri
  try {
    const resp = await get<{anns:{geom:Geom;coords:number[];label:string;source:string;note:string}[]}>('/reference/polygons', {uri,instance:store.referenceInstance})
    if (uri !== store.currentUri) return
    for (const ann of resp.anns) store.addAnn(ann)
    toast.info('轮廓已加入人工草稿。轮廓为近似，孔洞与精确像素仍以原始掩膜为准。')
  } catch (err) { toast.danger(String(err)) }
  finally { busy.value = false }
}
</script>

<template>
  <div v-if="available" class="reference" aria-label="原始掩膜参考">
    <label><input v-model="store.referenceShown" type="checkbox" /> 原始掩膜 · 只读</label>
    <label>透明度 <input v-model.number="store.referenceOpacity" type="range" min="0" max="1" step="0.05" aria-label="掩膜透明度" /></label>
    <MSelect v-model="store.referenceInstance" :options="options" width="230px" aria-label="原始实例" />
    <MButton size="sm" variant="secondary" :disabled="busy" @click="copyContours">复制轮廓到人工草稿</MButton>
    <span>取消勾选可对照原图</span>
  </div>
  <p v-if="error" role="alert" class="error">原始掩膜读取失败：{{ error }}</p>
</template>

<style scoped>
.reference { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding:8px 12px; background:var(--bg-panel); border-bottom:1px solid var(--border); font-size:12px; }
label { display:flex; align-items:center; gap:6px; } input[type=range] { width:90px; } span { color:var(--text-muted); } .error {color:var(--danger);margin:8px;}
</style>
