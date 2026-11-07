<script setup lang="ts">
import { ref, watch } from 'vue'
import MDialog from '@/components/ui/MDialog.vue'
import MButton from '@/components/ui/MButton.vue'
import MSelect from '@/components/ui/MSelect.vue'
import { get, postLong } from '@/api/client'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; created: [id: string] }>()
interface Report {
  scan_id: string; valid_images: number; elapsed_ms: number
  totals: Record<string, number>; categories: Record<string, Record<string, number>>
  issues: { kind: string; uri?: string; reason?: string; path?: string }[]
}
const sources = ref<{ value: string; label: string }[]>([])
const source = ref('')
const report = ref<Report | null>(null)
const busy = ref(false)
const error = ref('')
const sample = ref(0)
watch(() => props.open, async (open) => {
  if (!open) return
  error.value = ''
  try {
    const resp = await get<{ sources: { id: string; available: boolean }[] }>('/sources')
    sources.value = resp.sources.filter(s => s.available).map(s => ({ value: s.id, label: `${s.id} · 只读` }))
    source.value = sources.value[0]?.value || ''
  } catch (err) { error.value = String(err) }
})
watch(source, () => { report.value = null })
async function preview() {
  busy.value = true; error.value = ''; report.value = null
  try { report.value = await postLong<Report>('/imports/preview', { source_id: source.value }) }
  catch (err) { error.value = String(err) }
  finally { busy.value = false }
}
async function execute() {
  if (!report.value) return
  busy.value = true; error.value = ''
  try {
    const result = await postLong<{ dataset_id: string }>('/imports', {
      scan_id: report.value.scan_id, sample_per_category: sample.value, seed: 20261107,
      name: sample.value ? 'SSAFD · 90 张验收样本' : 'SSAFD · 太阳结构分割',
    })
    emit('created', result.dataset_id); emit('close'); report.value = null
  } catch (err) { error.value = String(err) }
  finally { busy.value = false }
}
</script>

<template>
  <MDialog :open="open" title="导入太阳数据 · 原件只读" :width="800" @close="!busy && emit('close')">
    <div class="import">
      <p>只建立图像、实例掩膜和 NOAA 对应表的索引。类别取 AR / CH / FL 目录，非零像素值是实例编号。</p>
      <MSelect v-model="source" :options="sources" label="已登记数据源" />
      <p v-if="!sources.length">未登记数据源。请使用项目的 run.bat 启动，或在启动命令中添加 --source 名称=目录。</p>
      <MButton variant="secondary" :disabled="!source || busy" :loading="busy" @click="preview">预检配对与尺寸</MButton>
      <template v-if="report">
        <div class="summary">{{ report.totals.images }} 张图像 · {{ report.totals.paired }} 对掩膜 · {{ report.totals.image_only }} 张未标注图像 · {{ report.totals.mask_only }} 个孤立掩膜</div>
        <table><thead><tr><th>类别</th><th>图像</th><th>掩膜</th><th>配对</th><th>仅图像</th><th>仅掩膜</th></tr></thead>
          <tbody><tr v-for="(c, name) in report.categories" :key="name"><td>{{ name }}</td><td>{{ c.images }}</td><td>{{ c.masks }}</td><td>{{ c.paired }}</td><td>{{ c.image_only }}</td><td>{{ c.mask_only }}</td></tr></tbody>
        </table>
        <p>可导入 {{ report.valid_images }} 张。无掩膜图像保留为未标注样本；孤立掩膜和无效文件记录在任务报告中。</p>
        <details><summary>异常与缺失清单（{{ report.issues.length }} 条，显示前 100 条）</summary>
          <p v-for="(issue, i) in report.issues.slice(0, 100)" :key="i" class="issue">{{ issue.kind }} · {{ issue.uri || issue.path }} {{ issue.reason }}</p>
        </details>
        <MSelect v-model="sample" :options="[{value:0,label:'全量索引'}, {value:30,label:'各类别 30 对 · 固定种子验收'}]" label="导入范围" />
      </template>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <p v-if="busy" role="status">正在本机处理，请等待；关闭浏览器不会终止服务端任务。</p>
    </div>
    <template #footer><MButton variant="ghost" :disabled="busy" @click="emit('close')">关闭</MButton>
      <MButton variant="primary" :disabled="!report || busy" @click="execute">建立数据集索引</MButton></template>
  </MDialog>
</template>

<style scoped>
.import { display:flex; flex-direction:column; gap:12px; max-height:65vh; overflow:auto; font-size:13px; }
p { margin:0; color:var(--text-secondary); line-height:1.7; }
.summary { color:var(--text-primary); font-variant-numeric:tabular-nums; }
table { border-collapse:collapse; text-align:left; } th,td { padding:7px 10px; border-bottom:1px solid var(--border); }
.issue { font-size:11px; overflow-wrap:anywhere; } .error { color:var(--danger); }
</style>
