<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { get } from '@/api/client'
import TrainingProgressPanel from '@/components/training/TrainingProgressPanel.vue'
import DiagnosticPanel from '@/components/training/DiagnosticPanel.vue'
import ArchitectureStudyPanel from '@/components/training/ArchitectureStudyPanel.vue'
import FlLearningStudyPanel from '@/components/training/FlLearningStudyPanel.vue'
import type { TrainingProgress } from '@/components/training/types'

interface Resource { peak_reserved_gib?: number; peak_rss_gib?: number; available_ram_gib?: number }
interface Run { id: string; state: string; completed_epoch: number; active_epoch?: number; best_epoch?: number; best_validation_dice?: number; error?: string; updated_utc?: string; images_processed?: number; train_images?: number; resource?: Resource }
interface Phase { id: string; name: string; state: string; gate: string }
interface Forecast { id: string; model: string; validation?: { pr_auc_ap: number; tss: number; brier: number }; test?: { pr_auc_ap: number; tss: number; brier: number; n: number }; state: string; mode?: string }
interface Summary { improvement_study?:ImprovementStudy; localization_audit?:LocalizationAudit; test_results?:TestResults; available: boolean; experiment?: string; phases?: Phase[]; runs: Run[]; completed?: number; total?: number; approved_completed?:number; approved_total?:number; deferred_total?:number; test_unlocked?: boolean; forecast?: Forecast[]; forecast_revision?: string; gates?: Record<string, { state: string; reason: string }>; notice?: string; progress?: TrainingProgress }
interface Metric { epoch: number; train_loss: number; validation: { dice: number; loss: number }; seconds: number }
interface TestResults {groups:{group:string;seeds:number;dice_mean:number;dice_sd:number;precision_mean:number;recall_mean:number;images:number}[]}
interface LocalizationAudit { models:Record<string,{images:number;reference_coverage:{reference:number;zero_overlap:number;half_covered_fraction:number};'box_iou_0.5':{precision:number;recall:number};predicted_components_without_reference_overlap:number}> }
interface StudyDecision {gate_passed:boolean;models:Record<string,{metrics:{dice:number};localization:{reference_coverage:{reference:number;zero_overlap:number;half_covered_fraction:number}}}>}
interface ImprovementStudy {decision?:StudyDecision;title:string;scope:string;evaluation_label?:string;active_run:string;budget:string;next_gate:string;status:{state:string;completed_epoch?:number;best_validation_dice?:number;reason?:string;error?:string};history:{epoch:number;train_loss:number;dice:number;seconds:number}[]}
interface Detail { config: Record<string, unknown>; metrics: Metric[]; status: Run }
const data = ref<Summary>(), detail = ref<Detail>(), error = ref(''), loading = ref(false)
const category = ref('all'), filter = ref('all'), selected = ref('')
const followCurrent = ref(true)
const labels: Record<string, string> = { deferred:'已暂缓', not_started: '待运行', running: '运行中', completed: '已完成', paused: '已暂停', failed: '失败', resource_failed: '资源未达标', needs_verification: '需核对进程', partial: '部分完成', pending: '待推进', blocked: '等待条件', ready: '已就绪', research_only: '仅限研究' }
let timer: ReturnType<typeof setInterval> | undefined
let controller: AbortController | undefined
let detailGeneration = 0
const visible = computed(() => (data.value?.runs || []).filter(r => (category.value === 'all' || r.id.startsWith(category.value)) && (filter.value === 'all' || r.state === filter.value)))
const active = computed(() => data.value?.runs.find(r => r.state === 'running'))
const preferredRun = computed(() => active.value?.id || data.value?.progress?.last_run?.id || data.value?.runs.find(r => r.completed_epoch > 0)?.id || data.value?.runs[0]?.id || '')
function selectRun(id: string) { followCurrent.value = false; void loadDetail(id) }
function followLatest() { if (followCurrent.value && preferredRun.value) void loadDetail(preferredRun.value) }
function showCurve() { followCurrent.value = true; followLatest(); document.querySelector('.details')?.scrollIntoView({block:'start',behavior:'smooth'}) }
const points = computed(() => (detail.value?.metrics || []).map((m, i, a) => `${20+i*600/Math.max(1,a.length-1)},${160-m.validation.dice*140}`).join(' '))
function percent(n?: number) { return n == null || n < 0 ? '—' : `${(n*100).toFixed(2)}%` }
function number(n?: number) { return n == null ? '—' : n.toFixed(2) }
async function loadDetail(id: string) {
  if (selected.value !== id) detail.value = undefined
  selected.value = id
  const generation = ++detailGeneration
  try { const result = await get<Detail>('/training/run', { id }); if (generation === detailGeneration) detail.value = result }
  catch (e) { if (generation === detailGeneration) error.value = String(e) }
}
async function refresh() {
  controller?.abort(); controller = new AbortController(); loading.value = true
  try { data.value = await get<Summary>('/training/summary', undefined, { signal: controller.signal }); error.value = ''; if (data.value.available) { const id = followCurrent.value || !selected.value ? preferredRun.value : selected.value; if (id) await loadDetail(id) } }
  catch (e) { if (!(e instanceof DOMException && e.name === 'AbortError')) error.value = String(e) }
  finally { loading.value = false }
}
onMounted(() => { void refresh(); timer = setInterval(() => { void refresh() }, 10000) })
onBeforeUnmount(() => { clearInterval(timer); controller?.abort(); detailGeneration++ })
</script>

<template>
  <div class="training">
    <header><div><h1>本地模型实验</h1><p>{{ data?.experiment || '实验协议' }} · 使用 Conda mdva311 · 单 GPU 顺序运行 · 每 10 秒读取本地真实进度</p></div><div class="header-actions"><button v-if="preferredRun" @click="showCurve">查看当前或最近训练曲线</button><button :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '刷新进度' }}</button></div></header>
    <p v-if="error" class="error" role="alert">读取失败：{{ error }}。已有结果仍保留，可点击刷新重试。</p>
    <FlLearningStudyPanel/>
    <ArchitectureStudyPanel/>
    <p v-if="data && !data.available">{{ data.notice }}</p>
    <template v-if="data?.available">
      <section v-if="data.improvement_study" class="diagnostic-fold" aria-label="独立改进实验进度">
        <h2>{{data.improvement_study.title}}</h2><p>{{data.improvement_study.scope}}</p><p>{{data.improvement_study.status.state==='completed_budget'?'本阶段预算已完成':labels[data.improvement_study.status.state] || data.improvement_study.status.state || '初始化'}} · 已保存 {{data.improvement_study.status.completed_epoch || 0}} 轮 · 最佳{{data.improvement_study.evaluation_label || '训练样本Dice'}} {{percent(data.improvement_study.status.best_validation_dice)}}</p>
        <p>独立于原39项。当前16图检查在同一训练样本评价，不能当作验证或独立测试成绩；不更换当前模型包。</p>
        <p>{{data.improvement_study.budget}}</p><p>{{data.improvement_study.status.error || data.improvement_study.status.reason || data.improvement_study.next_gate}}</p>
        <div v-if="data.improvement_study.decision"><p><strong>{{data.improvement_study.decision.gate_passed?'达到继续验证门槛，尚未发布':'未达到继续门槛：保留现有模型，不扩展该配置'}}</strong></p>
          <table><thead><tr><th>固定第3轮</th><th>验证Dice</th><th>完全未覆盖的参考区域</th><th>至少半数像素被覆盖的区域比例</th></tr></thead><tbody><tr v-for="(row,name) in data.improvement_study.decision.models" :key="name"><td>{{name}}</td><td>{{percent(row.metrics.dice)}}</td><td>{{row.localization.reference_coverage.zero_overlap}} / {{row.localization.reference_coverage.reference}}</td><td>{{percent(row.localization.reference_coverage.half_covered_fraction)}}</td></tr></tbody></table><p>这里的区域是8连通参考区域，不等于独立物理冕洞。验证集用于开发，不能代替新的独立测试。</p>
        </div>
        <details><summary>逐轮学习记录</summary><table><thead><tr><th>轮数</th><th>训练损失</th><th>{{data.improvement_study.evaluation_label || '训练样本Dice'}}</th><th>轮耗时（秒）</th></tr></thead><tbody><tr v-for="row in data.improvement_study.history.slice(-20)" :key="row.epoch"><td>{{row.epoch}}</td><td>{{number(row.train_loss)}}</td><td>{{percent(row.dice)}}</td><td>{{number(row.seconds)}}</td></tr></tbody></table></details>
      </section>
      <TrainingProgressPanel v-if="data.progress" :progress="data.progress" :experiment="data.experiment || ''" />
      <details v-if="data.localization_audit" class="diagnostic-fold"><summary>自动识别质量：大致范围定位检查（完整验证集）</summary>
        <p>框IoU≥0.5的一对一匹配检验范围位置；“覆盖至少一半”检验参考区域像素被找到的程度。两者含义不同，不能当作准确率或独立测试成绩。验证集已用于开发。</p>
        <table><thead><tr><th>模型 / 图像数</th><th>定位召回 / 精确率</th><th>参考区域至少一半被覆盖</th><th>完全未覆盖的参考区域</th><th>无参考重合的预测区域</th></tr></thead><tbody>
          <tr v-for="(row,name) in data.localization_audit.models" :key="name"><td>{{name}} / {{row.images}}</td><td>{{percent(row['box_iou_0.5'].recall)}} / {{percent(row['box_iou_0.5'].precision)}}</td><td>{{percent(row.reference_coverage.half_covered_fraction)}}</td><td>{{row.reference_coverage.zero_overlap}} / {{row.reference_coverage.reference}}</td><td>{{row.predicted_components_without_reference_overlap}}</td></tr>
        </tbody></table><p>连通区域不等于独立太阳物理实例。无参考重合并不能证明物理目标不存在；当前没有独立无目标图像误报率。完整逐图数据见本地out/model-localization目录。</p>
      </details>
      <details v-if="data.test_results" class="diagnostic-fold"><summary>AR/CH首次独立测试结果 · 12项固定模型</summary><p>表中为三个种子的均值，±为标准差（百分点）。测试表现低于验证，模型仅作人工辅助研究候选；不根据测试分数更换模型或阈值。</p><table><thead><tr><th>配置</th><th>测试Dice（区域重合）</th><th>Precision</th><th>Recall</th><th>每种子图像数</th></tr></thead><tbody><tr v-for="row in data.test_results.groups" :key="row.group"><td>{{row.group}}</td><td>{{percent(row.dice_mean)}} ± {{(row.dice_sd*100).toFixed(2)}}</td><td>{{percent(row.precision_mean)}}</td><td>{{percent(row.recall_mean)}}</td><td>{{row.images}}</td></tr></tbody></table><p>详细配对区间、逐模型CSV和限制见下方文件列表中的“AR/CH首次独立测试报告”。</p></details>
      <section class="phases"><article v-for="phase in data.phases" :key="phase.id" :class="phase.state"><b>{{ phase.id }} {{ phase.name }}</b><span>{{ labels[phase.state] || phase.state }}</span><small>{{ phase.gate }}</small></article></section>
      <section class="stats"><article><strong>{{ data.approved_completed ?? data.completed }} / {{ data.approved_total ?? data.total }}</strong><span>本轮完成；原登记 {{data.total}} 项，暂缓 {{data.deferred_total || 0}} 项</span></article><article><strong>{{ active?.id || '暂无运行中的识别任务' }}</strong><span v-if="active">第 {{ active.active_epoch || active.completed_epoch }} 轮 · 本轮 {{ active.images_processed || 0 }} / {{ active.train_images || '—' }} 张</span><span v-else>暂停、失败和完成分别记录</span></article><article><strong>{{ number(active?.resource?.peak_reserved_gib) }} GiB</strong><span>PyTorch 预留显存峰值 / 上限 6 GiB</span></article><article><strong>{{ number(active?.resource?.peak_rss_gib) }} GiB</strong><span>进程树内存峰值 / 上限 20 GiB</span></article></section>
      <div class="columns"><section class="runs"><div class="filters"><h2>识别实验</h2><select v-model="category" aria-label="筛选类别"><option value="all">全部类别</option><option>AR</option><option>CH</option><option>FL</option></select><select v-model="filter" aria-label="筛选状态"><option value="all">全部状态</option><option value="running">运行中</option><option value="completed">已完成</option><option value="not_started">待运行</option><option value="deferred">已暂缓</option><option value="failed">失败</option><option value="paused">已暂停</option></select></div>
        <p class="notice">{{ data.test_unlocked ? '正式测试已解锁；模型与配置不得根据测试分数调整。' : '测试集保持封存：获准实验完成、选型锁定并确认后，才正式评价；暂缓项目不自动启动。' }}</p>
        <div class="table"><table><thead><tr><th>实验编号</th><th>状态</th><th>完成轮数</th><th>最佳验证 Dice</th></tr></thead><tbody><tr v-for="run in visible" :key="run.id" :class="{ selected: selected === run.id }" @click="selectRun(run.id)"><td><button class="link">{{ run.id }}</button></td><td>{{ labels[run.state] || run.state }}</td><td>{{ run.completed_epoch }}</td><td>{{ percent(run.best_validation_dice) }}</td></tr></tbody></table></div>
      </section><section class="details" aria-label="训练模型详情"><h2>{{ selected }}</h2><label class="follow"><input v-model="followCurrent" type="checkbox" aria-label="自动跟随当前或最近训练" @change="followLatest"> 自动跟随当前或最近训练</label><p>{{ labels[detail?.status.state || ''] || '读取中' }} · 最佳轮数 {{ detail?.status.best_epoch || '—' }}</p><p v-if="detail?.status.error" class="error">{{ detail.status.error }}</p><h3>验证 Dice 学习曲线</h3><svg v-if="detail?.metrics.length" viewBox="0 0 640 185" role="img" aria-label="验证Dice随训练轮数变化"><path d="M20 20V160H620" fill="none" stroke="#758299"/><polyline :points="points" fill="none" stroke="#42d3b4" stroke-width="2"/><text x="22" y="15">1.0</text><text x="22" y="178">0</text><text x="570" y="178">epoch</text></svg><p v-else class="notice">尚无完整 epoch；不展示示例曲线。</p><p v-if="detail?.metrics.length">最近一轮 {{ number(detail.metrics[detail.metrics.length-1]?.seconds) }} 秒；这里是开发验证指标，不能当作最终测试结果。</p><details><summary>冻结配置</summary><pre>{{ JSON.stringify(detail?.config || {}, null, 2) }}</pre></details></section></div>
      <section><h2>预测基线与科学门槛 · {{ data.forecast_revision }}</h2><p class="notice">当前仅为原数据参考目录下的回顾性活动区研究。校准采用 2022 年资料，2021 年开发分数不能称为当时可获得的实时预报。完整事件覆盖及历史发布时间尚未通过验收。</p><div class="gates"><article v-for="(gate, name) in data.gates" :key="name"><b>{{ labels[gate.state] || gate.state }}</b><p>{{ gate.reason }}</p></article></div><div class="table"><table><thead><tr><th>预测实验</th><th>开发 PR-AUC（AP）</th><th>开发 TSS</th><th>参考测试 AP</th><th>参考测试 TSS</th><th>参考测试 Brier</th><th>状态</th></tr></thead><tbody><tr v-for="f in data.forecast" :key="f.id"><td>{{ f.id }}</td><td>{{ number(f.validation?.pr_auc_ap) }}</td><td>{{ number(f.validation?.tss) }}</td><td>{{ number(f.test?.pr_auc_ap) }}</td><td>{{ number(f.test?.tss) }}</td><td>{{ number(f.test?.brier) }}</td><td>回顾研究 · {{ labels[f.state] || f.state }}</td></tr></tbody></table></div></section>
    </template>
    <details class="diagnostic-fold"><summary>效果诊断与真实案例（点击展开）</summary><DiagnosticPanel /></details>
  </div>
</template>

<style scoped>
.header-actions{display:flex;gap:8px;flex-wrap:wrap}.follow{display:flex;align-items:center;gap:6px;font-size:12px;margin:8px 0;color:var(--text-secondary)}.diagnostic-fold>summary{cursor:pointer;padding:14px;background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;font-size:14px}.training{overflow:auto;padding:22px;color:var(--text-primary);display:flex;flex-direction:column;gap:18px;background:var(--bg-base)}header,.filters{display:flex;align-items:center;gap:14px;justify-content:space-between}h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 10px}h3{font-size:14px}p,small{color:var(--text-secondary);font-size:12px;line-height:1.7}button,select{background:var(--bg-panel);color:var(--text-primary);border:1px solid var(--border);padding:7px 10px;border-radius:5px;cursor:pointer}.phases{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}.phases article,.stats article,.gates article,.runs,.details{background:var(--bg-panel);border:1px solid var(--border);padding:13px;border-radius:8px}.phases article{display:flex;flex-direction:column;gap:8px;font-size:12px}.phases .completed{border-top:3px solid #42d3b4}.phases .running{border-top:3px solid #659df7}.phases .partial{border-top:3px solid #d3a642}.phases small{font-size:11px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stats article{display:flex;flex-direction:column;gap:8px}.stats strong{font-size:19px}.stats span{font-size:12px;color:var(--text-secondary)}.columns{display:grid;grid-template-columns:1.2fr 1fr;gap:16px}.table{max-height:400px;overflow:auto}table{width:100%;font-size:12px;border-collapse:collapse;text-align:left}td,th{padding:10px;border-bottom:1px solid var(--border);white-space:nowrap}th{position:sticky;top:0;background:var(--bg-panel)}tr.selected{background:#3a7e9120}.link{border:0;padding:0;background:none;color:#74c8ed}svg{width:100%;background:#0d1721}svg text{fill:#9aaabd;font-size:11px}pre{font-size:11px;overflow:auto}.notice{padding:8px 12px;background:#8ca9c512;border-left:3px solid #6384a6}.error{color:#f29494}.gates{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.gates b{font-size:12px}@media(max-width:1100px){.phases{grid-template-columns:repeat(4,1fr)}.columns{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.training{padding:12px}.phases,.gates{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start}.stats{grid-template-columns:1fr}.stats strong{font-size:16px}.filters{flex-wrap:wrap}}
</style>
