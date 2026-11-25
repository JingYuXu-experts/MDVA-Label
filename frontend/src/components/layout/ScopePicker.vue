<script setup lang="ts">
import { ref, watch } from 'vue'
import MSelect from '@/components/ui/MSelect.vue'
import { useCtxStore } from '@/stores/ctx'
import { useItemsStore } from '@/stores/items'
import { get } from '@/api/client'
withDefaults(defineProps<{disabled?:boolean}>(),{disabled:false})
const emit = defineEmits<{change:[uris:string[]]}>()
const ctx = useCtxStore()
const items = useItemsStore()
const mode = ref('page')
const count = ref(0)
const error = ref('')
const busy=ref(false)
let generation=0
watch(() => [mode.value, ctx.dataset, ctx.version, items.items, items.selected], async () => {
  const run=++generation
  busy.value=true
  error.value=''
  emit('change',[]); count.value=0
  if (!ctx.dataset || !ctx.version) {busy.value=false;return}
  try {
    const uris=mode.value==='all'
      ? (await get<{uris:string[]}>('/items/uris',{dataset:ctx.dataset,version:ctx.version})).uris
      : mode.value==='selected' ? [...items.selected] : items.items.map(i=>i.uri)
    if(run!==generation)return
    count.value=uris.length; emit('change',uris)
  } catch(err){if(run===generation)error.value=String(err)}finally{if(run===generation)busy.value=false}
}, {immediate:true})
</script>
<template><div class="scope">
  <span class="scope-step">01 处理哪些图片？</span><span>输入：{{ ctx.currentDataset?.name }} · {{ ctx.version.slice(0,9) }}</span>
  <MSelect v-model="mode" :disabled="disabled" :options="[{value:'page',label:'当前页图片'}, {value:'selected',label:'已选条目'}, {value:'all',label:'整个版本'}]" label="处理范围" width="160px" />
  <strong role="status">{{busy?'正在读取范围…':count+' 张'}}</strong><span>新版本只包含本次范围的结果；原文件和输入版本保留</span>
  <span v-if="error" role="alert">{{ error }}</span>
</div></template>
<style scoped>.scope{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid var(--border);background:var(--bg-panel);font-size:12px;flex:none}.scope>span{color:var(--text-secondary)}strong{font-variant-numeric:tabular-nums}</style>
