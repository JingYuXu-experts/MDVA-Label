<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { get, getBlob } from '@/api/client'
import MDialog from '@/components/ui/MDialog.vue'
import type { FilePreview, JournalEntry, TrainingFile, TrainingProgress } from './types'

const props = defineProps<{ progress: TrainingProgress; experiment: string }>()
const records = ref<JournalEntry[]>([]), files = ref<TrainingFile[]>([])
const total = ref(0), page = ref(1), kind = ref('all'), loading = ref(false), error = ref('')
const openFile = ref(''), preview = ref<FilePreview>(), previewLoading = ref(false), previewError = ref('')
const copyMessage = ref(''), downloadBusy = ref('')
let controller: AbortController | undefined
let generation = 0, previewGeneration = 0
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / 8)))
const active = computed(() => props.progress.active)
const lastRun = computed(() => props.progress.last_run)
const stateNames: Record<string,string> = {paused:'已暂停',failed:'失败',resource_failed:'资源异常',completed:'已完成',needs_verification:'进程待核实'}
const batchPercent = computed(() => Math.min(100, (active.value?.images_processed || 0) / Math.max(1, active.value?.train_images || 0) * 100))
const kinds: Record<string, string> = { epoch: '训练轮次', development: '开发记录', transition: '阶段与模型', error: '异常', handoff: '接续记录' }
function time(utc?: string | null) {
  if (!utc) return '尚未生成'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(utc))
}
function duration(seconds?: number | null) {
  if (seconds == null) return '等待实测'
  return seconds < 3600 ? `约 ${Math.ceil(seconds / 60)} 分钟` : `约 ${(seconds / 3600).toFixed(1)} 小时`
}
async function load() {
  controller?.abort(); controller = new AbortController()
  const signal = controller.signal, current = ++generation
  loading.value = true
  try {
    const [journal, catalog] = await Promise.all([
      get<{ entries: JournalEntry[]; total: number }>('/training/journal', { page: page.value, page_size: 8, kind: kind.value }, { signal }),
      get<{ files: TrainingFile[] }>('/training/files', undefined, { signal }),
    ])
    if (current !== generation) return
    records.value = journal.entries; total.value = journal.total; files.value = catalog.files; error.value = ''
    if (page.value > pageCount.value) page.value = pageCount.value
  } catch (e) { if (current === generation && !signal.aborted) error.value = String(e) }
  finally { if (current === generation) loading.value = false }
}
async function inspect(id: string) {
  openFile.value = id; preview.value = undefined; previewError.value = ''; previewLoading.value = true
  const current = ++previewGeneration
  try { const value = await get<FilePreview>('/training/file', { id }); if (current === previewGeneration) preview.value = value }
  catch (e) { if (current === previewGeneration) previewError.value = String(e) }
  finally { if (current === previewGeneration) previewLoading.value = false }
}
function closePreview() { openFile.value = ''; previewGeneration++ }
async function download(file: TrainingFile) {
  downloadBusy.value = file.id; previewError.value = ''
  try {
    const blob = await getBlob('/training/file', { id: file.id, download: 1 })
    const url = URL.createObjectURL(blob), anchor = document.createElement('a')
    anchor.href = url; anchor.download = file.filename; anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { previewError.value = String(e); if (!openFile.value) error.value = String(e) }
  finally { downloadBusy.value = '' }
}
async function copy(command: string) {
  try { await navigator.clipboard.writeText(command); copyMessage.value = '命令已复制' }
  catch { copyMessage.value = '请选中下方命令手动复制' }
}
watch(() => props.progress, () => { void load() })
watch([page, kind], () => { void load() })
function changeKind() { page.value = 1 }
onMounted(() => { void load() })
onBeforeUnmount(() => { controller?.abort(); generation++; previewGeneration++ })
</script>

<template>
  <section class="progress-panel" aria-label="训练进度与接续日志">
    <header class="heading"><div><h2>训练进度与接续日志</h2><p>本地保存记录，关闭页面后仍可重新查看。时间均为北京时间。</p></div><span class="badge" :class="{ enabled: progress.automatic_continuation }">{{ progress.automatic_continuation ? '自动接续已开启' : progress.state === 'completed' ? '本轮训练已完成' : progress.state === 'evaluating' ? '独立测试推理中' : progress.state === 'paused' ? '训练已暂停' : progress.state === 'pausing' ? '等待保存后暂停' : progress.state === 'needs_attention' ? '异常待处理' : '接续状态待确认' }}</span></header>
    <div class="live-grid"><article class="live">
      <div class="live-title"><b>{{ progress.phase }} · {{ progress.headline }}</b><span>{{ time(progress.coordinator.updated_utc) }}</span></div>
      <p class="next">{{ progress.next_step }}</p>
      <p v-if="active" class="model-status"><b>当前训练：{{active.id}}</b></p>
      <div v-else-if="lastRun" class="saved-run"><b>最近训练：{{lastRun.id}} · {{stateNames[lastRun.state]||lastRun.state}}</b><p>已完成 {{lastRun.completed_epoch}} 轮 · 最佳验证 Dice {{lastRun.best_validation_dice == null || lastRun.best_validation_dice < 0 ? '—' : (lastRun.best_validation_dice*100).toFixed(2)+'%'}}</p><p v-if="lastRun.error" class="journal-error">停止原因：{{lastRun.error}}</p><p>已保存的指标、曲线和日志仍可查看。</p></div>
      <div class="bar-label"><span>登记实验完成进度</span><b>{{ progress.completed_runs }} / {{ progress.total_runs }}</b></div>
      <progress :value="progress.completed_runs" :max="Math.max(1, progress.total_runs)" aria-label="登记实验完成进度" />
      <template v-if="active">
        <div class="bar-label"><span>当前模型：已完成 {{ active.completed_epoch }} 轮 / 最多 {{ progress.epoch_budget?.maximum }} 轮，可早停</span><b>当前第 {{ active.active_epoch || active.completed_epoch }} 轮</b></div>
        <progress :value="progress.epoch_budget?.completed || 0" :max="progress.epoch_budget?.maximum || 80" aria-label="当前模型轮次预算进度" />
        <div v-if="active.train_images" class="bar-label"><span>本轮已读取 {{ active.images_processed || 0 }} / {{ active.train_images }} 张</span><b>{{ batchPercent.toFixed(0) }}%</b></div>
        <progress v-if="active.train_images" :value="active.images_processed || 0" :max="active.train_images" aria-label="当前轮图像处理进度" />
        <p v-if="progress.epoch_budget" class="estimate">最近单轮 {{ duration(progress.epoch_budget.recent_epoch_seconds) }}。若后续不再改善，最早还需 {{ progress.epoch_budget.earliest_additional_epochs_if_no_improvement }} 轮触发早停；跑满预算还需 {{ duration(progress.epoch_budget.remaining_upper_seconds) }}。这些是条件估算。</p>
      </template>
      <div class="checkpoint"><b>{{ progress.checkpoint?.exists ? `已保存至第 ${progress.checkpoint.completed_epoch} 轮` : '等待下一次完整检查点' }}</b><span>{{ time(progress.checkpoint?.saved_utc) }}</span><code v-if="progress.checkpoint?.exists">{{ progress.checkpoint.filename }}</code></div>
    </article><aside class="commands"><h3>下一步与接续命令</h3><p>在已激活 <code>mdva311</code> 的项目终端执行。运行中的实验无需重复启动。</p><div v-for="command in progress.next_commands" :key="command.label" class="command"><div><b>{{ command.label }}</b><button @click="copy(command.command)">复制命令</button></div><pre>{{ command.command }}</pre></div><p v-if="progress.next_run_id">下一项：<b>{{ progress.next_run_id }}</b></p><p class="copy-message" role="status">{{ copyMessage }}</p></aside></div>
    <div class="history-grid"><section class="history"><div class="section-heading"><h3>本地进度日志</h3><div><select v-model="kind" aria-label="日志分类" @change="changeKind"><option value="all">全部记录</option><option v-for="(label, value) in kinds" :key="value" :value="value">{{ label }}</option></select><button :disabled="loading" @click="load">刷新日志</button></div></div><p v-if="error" class="journal-error" role="alert">{{ error }}；已有日志保留，可重试。</p>
      <ol class="timeline"><li v-for="record in records" :key="record.id" :class="record.kind"><div><span class="kind">{{ kinds[record.kind] || record.kind }}</span><time>{{ time(record.recorded_utc) }}</time></div><b>{{ record.title }}</b><p>{{ record.message }}</p></li></ol><p v-if="!records.length && !loading">这一分类尚无记录。训练轮次、阶段变化和异常会写入本地日志。</p>
      <footer><span>共 {{ total }} 条 · 第 {{ page }} / {{ pageCount }} 页</span><button :disabled="page <= 1 || loading" @click="page--">上一页</button><button :disabled="page >= pageCount || loading" @click="page++">下一页</button></footer>
    </section><section class="documents"><h3>本地文件与交接资料</h3><p>直接查看或下载；文件保存在本机实验目录，下一次工作可以从这些记录继续。</p><article v-for="file in files" :key="file.id" class="document"><b>{{ file.title }}</b><small>{{ file.description }}</small><code>{{ file.filename }}</code><div><span>{{ file.available ? time(file.updated_utc) : '尚未生成' }}</span><button :disabled="!file.available" :aria-label="`查看${file.title}`" @click="inspect(file.id)">查看</button><button :disabled="!file.available || downloadBusy === file.id" :aria-label="`下载${file.title}`" @click="download(file)">下载</button></div></article></section></div>
    <MDialog :open="!!openFile" :title="preview?.title || '本地文件预览'" :width="800" @close="closePreview"><p v-if="previewLoading">正在读取本地文件…</p><p v-if="previewError" class="journal-error" role="alert">{{ previewError }}</p><template v-if="preview"><p>{{ preview.filename }} · {{ time(preview.updated_utc) }}</p><p v-if="preview.truncated">仅预览文件末尾；下载可获得完整文件。</p><pre class="file-preview">{{ preview.text }}</pre></template><template #footer><button v-if="preview" :disabled="downloadBusy === preview.id" @click="download(preview)">下载完整文件</button><button @click="closePreview">关闭预览</button></template></MDialog>
  </section>
</template>

<style scoped>
.progress-panel{border:1px solid var(--border);border-radius:10px;padding:18px;background:var(--bg-panel);color:var(--text-primary)}h2{margin:0;font-size:17px}h3{font-size:14px;margin:0 0 8px}p{font-size:12px;color:var(--text-secondary);line-height:1.7;margin:7px 0}button,select{background:var(--bg-base);color:var(--text-primary);border:1px solid var(--border);border-radius:5px;padding:5px 8px;cursor:pointer;font-size:11px}button:disabled{opacity:.4;cursor:default}.heading,.live-title,.bar-label,.section-heading,.command>div,.document>div,footer{display:flex;align-items:center;justify-content:space-between;gap:10px}.heading{align-items:flex-start}.badge{font-size:12px;background:#8e775e20;color:#d9bc87;border:1px solid #8d754944;border-radius:16px;padding:6px 11px;white-space:nowrap}.badge.enabled{color:#6ee2bf;background:#41b59615;border-color:#41b59644}.live-grid{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:20px;margin-top:18px}.live,.commands{min-width:0}.live-title b{font-size:15px}.live-title span,.bar-label,.checkpoint,.command b{font-size:12px}.live-title span{color:var(--text-secondary);white-space:nowrap}.next{border-left:3px solid #41b596;padding-left:10px}.bar-label{margin:12px 0 4px}progress{width:100%;height:8px;appearance:none;border:0;border-radius:6px;overflow:hidden;display:block;background:#66809b25;accent-color:#44c5ad}progress::-webkit-progress-bar{background:#66809b25}progress::-webkit-progress-value{background:#44c5ad;border-radius:6px}.estimate{font-size:11px}.checkpoint{background:#44687b18;padding:10px;display:flex;gap:8px;flex-wrap:wrap;border-radius:6px}.checkpoint span{color:var(--text-secondary)}.checkpoint code{width:100%;overflow-wrap:anywhere}.commands{border-left:1px solid var(--border);padding-left:20px}.command{margin:10px 0}.command pre{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg-base);padding:10px;border-radius:5px;margin:6px 0}.copy-message{min-height:16px;color:#6ee2bf}.history-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:20px;border-top:1px solid var(--border);margin-top:16px;padding-top:16px}.section-heading>div{display:flex;gap:6px}.history,.documents{min-width:0}.timeline{list-style:none;padding:0;max-height:410px;overflow:auto;margin:8px 0}.timeline li{padding:10px 8px 10px 13px;border-left:2px solid #6384a6;border-bottom:1px solid var(--border)}.timeline li.error{border-left-color:#e88383}.timeline li.development,.timeline li.handoff{border-left-color:#48b89d}.timeline li>div{display:flex;gap:10px;color:var(--text-secondary);font-size:11px;margin-bottom:5px}.timeline li>b{font-size:12px}.timeline p{font-size:11px;margin:4px 0;overflow-wrap:anywhere}.kind{color:#85bbd9}footer{justify-content:flex-end;font-size:11px}footer>span{margin-right:auto;color:var(--text-secondary)}.documents{max-height:500px;overflow:auto}.document{border-bottom:1px solid var(--border);padding:9px 0;display:flex;flex-direction:column;gap:5px}.document>b{font-size:12px}.document small,.document code,.document>div>span{font-size:11px;color:var(--text-secondary);overflow-wrap:anywhere}.document>div{justify-content:flex-end}.document>div>span{margin-right:auto}.file-preview{font-size:12px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;margin:0}.journal-error{color:#ed9999}@media(max-width:900px){.live-grid,.history-grid{grid-template-columns:1fr}.commands{border-left:0;border-top:1px solid var(--border);padding:14px 0 0}.documents{max-height:none}}@media(max-width:500px){.progress-panel{padding:12px}.heading,.live-title{flex-wrap:wrap}.bar-label{align-items:flex-start;flex-wrap:wrap}.section-heading{align-items:flex-start;flex-wrap:wrap}.badge{padding:4px 8px}.timeline{max-height:360px}}
</style>
