<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { get } from '@/api/client'

interface Scores { images: number; dice: number | null; iou: number | null; precision: number | null; recall: number | null; boundary_f1: number | null; empty_images?: number }
interface Model { id: string; category?: string; state: string; error?: string; partial_training?: boolean; checkpoint_epoch?: number; validation?: Scores; reproduction_passed?: boolean; reproduction_delta?: number; strata?: { instrument: Record<string, Scores> }; training_diagnostics?: { diagnostic_subset: Record<string, Scores> }; threshold_sensitivity?: {threshold: number; dice: number}[]; performance?: {parameters: number; pure_p95_ms: number; end_to_end_p95_ms: number; resource: {peak_allocated_gib: number}} }
interface Group {group: string; seeds: number; mean_dice: number; sd_dice: number | null; pure_p95_ms: number; peak_allocated_gib: number}
interface Case extends Scores { sample_id: string; run_id: string; uri: string; mask_uri: string; instrument: string; year: string; time_utc: string; category: string; split: string; assets: Record<string, string>; error_types: string[]; area_ratio: number | null; width: number; height: number; diagnostic_subset: string; objects: Record<string, unknown> }
interface CasePage {report_id: string; items: Case[]; total: number; run_id: string; models: string[]; stations: string[]; error_types: string[]}
interface ReviewCase {sample_id: string; run_id: string; category: string; uri: string; dice: number; kinds: string[]}
interface Interpretation {findings: {title: string; detail: string; kind: string}[]; review_cases: ReviewCase[]; next_experiments: {decision: string; target: string; reason: string}[]}
interface Summary {statistics?: {station_intervals: {group: string; station: string; images: number; seeds: number; time_blocks: number; mean_dice: number; ci95: number[] | null}[]}; interpretation?: Interpretation; available: boolean; notice?: string; report_id?: string; reports?: string[]; status?: {task_type?: string; training_stopped?: boolean; state: string; phase: string; message: string; updated_utc: string; run_id?: string; processed?: number; total?: number; models_completed?: number; models_total?: number}; report?: {state: string; models: Model[]; groups: Group[]; limitations: string[]; recommendations: {group: string; decision: string; reason: string}[]; paired_comparisons: {baseline: string; candidate: string; images: number; difference_b_minus_a: number; ci95: number[]; clusters: number}[]; audit_summary?: {checked_pairs: number; all_source_fingerprints_verified: boolean; issues: unknown[]; duplicates: unknown[]; label_version: string; orientation_limit: string}} }
const data = ref<Summary>(), casePage = ref<CasePage>(), error = ref(''), caseError = ref(''), compareError = ref('')
const category = ref('FL'), runId = ref(''), station = ref(''), errorType = ref(''), order = ref('fixed'), subset = ref('validation'), page = ref(1)
const selectedId = ref(''), comparisonRun = ref('FL-F2-s17'), comparison = ref<Case>(), compareBusy = ref(false), casesBusy = ref(false)
const referenceOpacity = ref(.45), predictionOpacity = ref(.5), selectedReport = ref('')
const imageError = ref(''), imageRevision = ref(0), focusSample = ref('')
const activeCase = computed(() => casePage.value?.items.find(c => c.sample_id === selectedId.value))
const activeModel = computed(() => data.value?.report?.models.find(m => m.id === casePage.value?.run_id))
const groups = computed(() => (data.value?.report?.groups || []).filter(g => g.group.startsWith(category.value+'-')))
const pageCount = computed(() => Math.max(1, Math.ceil((casePage.value?.total || 0)/24)))
const pct = (n?: number | null) => n == null ? '—' : `${(n*100).toFixed(2)}%`
const fixed = (n?: number | null, digits=2) => n == null ? '—' : n.toFixed(digits)
const stateNames: Record<string,string> = {running:'诊断推理中', completed:'诊断完成 · 历史快照', completed_with_issues:'诊断完成，有待核查项', failed:'诊断中断'}
const stateLabel = computed(() => data.value?.status?.task_type === 'reviewed_training_queue' ? (data.value.status.state === 'running' ? '修订训练进行中 · 逐项验证' : data.value.status.message) : data.value?.status?.task_type === 'validation_analysis' ? '验证推理中 · 当前模型训练已结束' : stateNames[data.value?.status?.state || ''] || '诊断状态待确认')
const errorNames: Record<string,string> = {empty_prediction:'预测为空', undersegmentation:'漏分严重', oversegmentation:'预测面积过大', boundary_error:'边界偏差', empty_reference_positive:'空参考图上有预测', no_major_flag:'未触发主要错误提示'}
const asset = (name?: string) => name ? `/api/training/diagnostics/assets?report=${encodeURIComponent(data.value?.report_id || '')}&id=${encodeURIComponent(name)}&retry=${imageRevision.value}` : ''
let timer: ReturnType<typeof setInterval> | undefined, disposed=false, summaryGeneration=0, caseGeneration=0, comparisonGeneration=0
async function refresh() {
  const n=++summaryGeneration
  try {
    const value=await get<Summary>('/training/diagnostics', {report:selectedReport.value})
    if(disposed||n!==summaryGeneration)return
    const changed=data.value?.report_id!==value.report_id
    data.value=value;error.value=''
    if(value.available&&(changed||!casePage.value||value.status?.state==='running'))void loadCases()
  } catch(e) {if(!disposed&&n===summaryGeneration)error.value=String(e)}
}
async function loadCases() {
  if(!data.value?.available)return
  const n=++caseGeneration;casesBusy.value=true
  try {
    const value=await get<CasePage>('/training/diagnostics/cases', {report:data.value.report_id,category:category.value,run_id:runId.value,station:station.value,error_type:errorType.value,page:page.value,page_size:24,order:order.value,subset:subset.value,sample_id:focusSample.value})
    if(disposed||n!==caseGeneration)return
    casePage.value=value;caseError.value=''
    if(!value.items.some(r=>r.sample_id===selectedId.value))selectedId.value=value.items[0]?.sample_id||''
    if(value.models.includes(comparisonRun.value)===false){const preferred=['FL-F2-s17','FL-F0-s17','AR-A1-s17','AR-A0-s17','CH-C2-s17'];comparisonRun.value=preferred.find(id=>value.models.includes(id)&&id!==value.run_id)||value.models.find(id=>id!==value.run_id)||value.run_id}
    void loadComparison()
  }catch(e){if(!disposed&&n===caseGeneration)caseError.value=String(e)}
  finally{if(n===caseGeneration)casesBusy.value=false}
}
async function loadComparison() {
  const n=++comparisonGeneration;comparison.value=undefined;compareError.value=''
  if(!activeCase.value||!comparisonRun.value)return
  compareBusy.value=true
  try {
    const value=await get<CasePage>('/training/diagnostics/cases', {report:data.value?.report_id,category:category.value,run_id:comparisonRun.value,sample_id:activeCase.value.sample_id,subset:subset.value})
    if(disposed||n!==comparisonGeneration)return
    comparison.value=value.items[0]
    if(!comparison.value)compareError.value='此模型尚无同一图像的诊断结果，未使用其他图片替代。'
  }catch(e){if(!disposed&&n===comparisonGeneration)compareError.value=String(e)}
  finally{if(n===comparisonGeneration)compareBusy.value=false}
}
async function inspectReview(item: ReviewCase) {
  category.value=item.category
  await nextTick()
  station.value='';errorType.value='';subset.value='validation';runId.value=item.run_id;focusSample.value=item.sample_id
  await loadCases()
  await nextTick()
  document.querySelector('.case-detail')?.scrollIntoView({block:'start'})
}
watch(category,()=>{focusSample.value='';runId.value='';station.value='';errorType.value='';page.value=1;selectedId.value='';casePage.value=undefined;void loadCases()})
watch([runId,station,errorType,order,subset,focusSample],()=>{page.value=1;void loadCases()})
watch(page,()=>void loadCases())
watch([selectedId,comparisonRun],()=>{imageError.value='';void loadComparison()})
watch(selectedReport,()=>{casePage.value=undefined;selectedId.value='';void refresh()})
onMounted(()=>{void refresh();timer=setInterval(()=>void refresh(),10000)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);summaryGeneration++;caseGeneration++;comparisonGeneration++})
</script>
<template>
  <section class="diagnostic-panel" aria-label="识别效果诊断" @error.capture="imageError='部分图片读取失败，已有指标保留，可重试图片。'">
    <header><div><h2>识别效果诊断</h2><p>使用已有权重检查质量；诊断推理不执行训练，也不访问最终测试集。</p></div><div class="actions"><select v-if="data?.reports?.length" v-model="selectedReport" aria-label="诊断报告版本"><option value="">最新报告</option><option v-for="r in data.reports" :key="r">{{r}}</option></select><button @click="refresh">刷新诊断</button></div></header>
    <p v-if="error" class="error" role="alert">读取诊断失败：{{error}} <button @click="refresh">重试</button></p>
    <p v-if="data&&!data.available">{{data.notice}}</p>
    <template v-if="data?.available">
      <div class="diagnostic-status" role="status"><b>{{stateLabel}}</b><span>{{data.status?.phase}} · {{data.status?.message}}</span><span v-if="data.status?.models_total">{{Math.max(data.status.models_completed||0,data.report?.models.length||0)}} / {{data.status.models_total}} 个检查点已处理</span><span v-if="data.status?.total">当前 {{data.status.processed||0}} / {{data.status.total}} 张</span></div>
      <p class="notice">本页为重复使用验证集的开发诊断，分数不是最终测试成绩。未进行人工计时，尚不能宣称节省标注时间。</p>
      <div v-if="data.interpretation" class="findings"><article v-for="finding in data.interpretation.findings.slice(0,3)" :key="finding.title"><b>{{finding.title}}</b><p>{{finding.detail}}</p></article><details><summary>其余诊断结论与待复查案例</summary><article v-for="finding in data.interpretation.findings.slice(3)" :key="finding.title"><b>{{finding.title}}</b><p>{{finding.detail}}</p></article><article v-for="item in data.interpretation.review_cases" :key="item.run_id+item.sample_id"><button @click="inspectReview(item)">查看复查案例 · {{item.run_id}} · Dice {{pct(item.dice)}}</button><p>{{item.kinds.join('；')}}</p></article><p>位移仅作敏感性探针，未移动任何参考标注；疑似未覆盖不等于专家确认漏标。</p></details></div>
      <div class="report-links" v-if="data.report?.models?.length"><a :href="asset('report.json')" download="诊断报告.json">下载完整报告 JSON</a><a v-if="data.status?.state!=='running'&&data.status?.state!=='failed'" :href="asset('report.md')" download="诊断报告.md">下载文字报告</a><a v-if="data.interpretation" :href="asset('interpretation.md')" download="诊断结论.md">下载诊断结论与后续取舍</a></div>
      <div class="filters"><label>任务 <select v-model="category" aria-label="诊断任务"><option>FL</option><option>AR</option><option>CH</option></select></label><span>{{category==='FL'?'FL：Hα日面暗条':category==='AR'?'AR：磁图活动区':'CH：AIA193冕洞'}}</span></div>
      <div class="table"><table><thead><tr><th>配置</th><th>已完成种子</th><th>平均验证 Dice</th><th>种子标准差</th><th>纯推理 P95</th><th>峰值已分配显存</th></tr></thead><tbody><tr v-for="g in groups" :key="g.group"><td>{{g.group}}</td><td>{{g.seeds}}</td><td>{{pct(g.mean_dice)}}</td><td>{{g.sd_dice==null?'单种子，未知':fixed(g.sd_dice*100)+' 个百分点'}}</td><td>{{fixed(g.pure_p95_ms)}} ms</td><td>{{fixed(g.peak_allocated_gib)}} GiB</td></tr></tbody></table></div>
      <p v-if="!groups.length">此任务的统一复算尚未完成，已有训练成绩保留在下方原实验列表。</p>
      <details class="report-section"><summary>实验取舍建议与证据边界</summary><article v-for="r in data.report?.recommendations" :key="r.group"><b>{{r.group}} · {{r.decision}}</b><p>{{r.reason}}</p></article><p v-for="line in data.report?.limitations" :key="line">{{line}}</p><div class="table"><table><thead><tr><th>基线 → 候选</th><th>配对图像</th><th>27日时间块</th><th>Dice差值</th><th>95%区间</th></tr></thead><tbody><tr v-for="c in data.report?.paired_comparisons" :key="c.baseline+c.candidate"><td>{{c.baseline}} → {{c.candidate}}</td><td>{{c.images}}</td><td>{{c.clusters}}</td><td>{{fixed(c.difference_b_minus_a*100)}} 个百分点</td><td>{{fixed(c.ci95[0]!*100)}}～{{fixed(c.ci95[1]!*100)}}</td></tr></tbody></table></div></details>
      <details v-if="data.statistics" class="report-section"><summary>各台站的不确定性区间</summary><p>27日时间块bootstrap 2000次；三个种子先逐图平均。少于3个时间块不报告区间，小样本和单种子不能用来宣称泛化优势。</p><div class="table"><table><thead><tr><th>配置</th><th>台站</th><th>图像／时间块</th><th>验证Dice</th><th>95%区间</th></tr></thead><tbody><tr v-for="r in data.statistics.station_intervals.filter(r=>r.group.startsWith(category+'-'))" :key="r.group+r.station"><td>{{r.group}}</td><td>{{r.station}}</td><td>{{r.images}} / {{r.time_blocks}}</td><td>{{pct(r.mean_dice)}}</td><td>{{r.ci95?pct(r.ci95[0])+'～'+pct(r.ci95[1]):'时间块不足'}}</td></tr></tbody></table></div><a :href="asset('station-intervals.json')" download="台站区间.json">下载台站区间数据</a></details>
      <details class="report-section"><summary>数据与预处理审计</summary><p>已核验 {{data.report?.audit_summary?.checked_pairs||0}} 对训练／验证文件；来源指纹{{data.report?.audit_summary?.all_source_fingerprints_verified?'全部通过':'尚未全部完成'}}。</p><p>RGB转灰度 → 除以255 → 双线性缩放图像；掩膜最近邻缩放；概率恢复至原始尺寸，主阈值固定0.5。</p><p>{{data.report?.audit_summary?.orientation_limit}}</p><p>标注版本：{{data.report?.audit_summary?.label_version}}</p><p>边框／对比度格式提示 {{data.report?.audit_summary?.issues?.length||0}} 项（不代表异常数量）；重复关系 {{data.report?.audit_summary?.duplicates?.length||0}} 组。提示不等于已确认坏图或漏标，详见下载报告。</p></details>
      <h3>真实案例与同图对照</h3><button v-if="focusSample" @click="focusSample=''">返回全部样本</button>
      <p v-if="imageError" class="error" role="alert">{{imageError}} <button @click="imageError='';imageRevision++">重试图片</button></p>
      <div class="filters">
        <label>模型 <select v-model="runId" aria-label="诊断案例模型"><option value="">默认已完成模型</option><option v-for="r in casePage?.models" :key="r" :value="r">{{r}}{{data.report?.models.find(m=>m.id===r)?.partial_training?' · 未完成参考':''}}</option></select></label>
        <label>样本 <select v-model="subset" aria-label="诊断样本范围"><option value="validation">完整验证集</option><option value="train_fit_sample">固定训练样本 · 非独立评价</option><option value="train_empty_reference">训练空参考图 · 非独立负例测试</option></select></label>
        <label>台站 <select v-model="station" aria-label="诊断台站"><option value="">全部台站</option><option v-for="s in casePage?.stations" :key="s">{{s}}</option></select></label>
        <label>错误 <select v-model="errorType" aria-label="诊断错误类型"><option value="">全部</option><option v-for="e in casePage?.error_types" :key="e" :value="e">{{errorNames[e]||e}}</option></select></label>
        <label>排序 <select v-model="order" aria-label="诊断案例排序"><option value="fixed">固定分层样本</option><option value="worst">Dice较低</option><option value="middle">Dice居中</option><option value="best">Dice较高</option></select></label>
      </div>
      <p v-if="caseError" class="error" role="alert">{{caseError}} <button @click="loadCases">重试案例</button></p>
      <p v-if="casesBusy">读取案例中…</p>
      <div class="case-grid"><button v-for="item in casePage?.items" :key="item.sample_id" :class="{selected:selectedId===item.sample_id}" @click="selectedId=item.sample_id"><img :src="asset(item.assets.original)" loading="lazy" :alt="`${item.instrument} ${item.time_utc}`"/><span>{{item.instrument}} · {{item.year}}</span><b>Dice {{pct(item.dice)}}</b></button></div>
      <p v-if="casePage&&!casePage.items.length&&!casesBusy">暂无符合筛选条件的结果；模型可能尚未完成诊断。</p>
      <div class="pagination"><span>共 {{casePage?.total||0}} 张 · 第 {{page}} / {{pageCount}} 页</span><button :disabled="page<=1" @click="page--">上一页</button><button :disabled="page>=pageCount" @click="page++">下一页</button></div>
      <div v-if="activeCase" class="case-detail"><p v-if="activeModel?.partial_training" class="notice">当前为未完成训练的参考检查点，不属于完整基线；原训练失败状态保留。</p>
        <p class="uri">{{activeCase.uri}}<br/>原尺寸 {{activeCase.width}} × {{activeCase.height}} · {{activeCase.time_utc}} · {{activeCase.split==='train'?'训练样本，不是独立评价':'开发验证样本'}}</p>
        <div class="filters"><label>参考标注透明度 <input v-model.number="referenceOpacity" aria-label="参考标注透明度" type="range" min="0" max="1" step=".05"/></label><label>预测透明度 <input v-model.number="predictionOpacity" aria-label="模型预测透明度" type="range" min="0" max="1" step=".05"/></label><label>右侧对照模型 <select v-model="comparisonRun" aria-label="同图对照模型"><option v-for="r in casePage?.models" :key="r" :value="r">{{r}}{{data.report?.models.find(m=>m.id===r)?.partial_training?' · 未完成参考':''}}</option></select></label></div>
        <div class="image-comparison">
          <figure><figcaption>原图与原始参考 · 绿色</figcaption><div class="image-stack"><img :src="asset(activeCase.assets.original)" alt="原始观测图像"/><img :src="asset(activeCase.assets.reference)" :style="{opacity:referenceOpacity}" alt="原始参考掩膜"/></div></figure>
          <figure><figcaption>模型实际输入 · 灰度与缩放</figcaption><img :src="asset(activeCase.assets.input)" alt="模型实际灰度输入"/></figure>
          <figure><figcaption>{{activeCase.run_id}} · 蓝色预测 · Dice {{pct(activeCase.dice)}}</figcaption><div class="image-stack"><img :src="asset(activeCase.assets.original)" alt="左模型原图"/><img :src="asset(activeCase.assets.reference)" :style="{opacity:referenceOpacity}" alt="左模型参考"/><img :src="asset(activeCase.assets.prediction)" :style="{opacity:predictionOpacity}" alt="左模型预测叠加"/></div></figure>
          <figure><figcaption>{{comparisonRun}} · 同一图像 · Dice {{pct(comparison?.dice)}}</figcaption><div v-if="comparison" class="image-stack"><img :src="asset(comparison.assets.original)" alt="右模型同图"/><img :src="asset(comparison.assets.reference)" :style="{opacity:referenceOpacity}" alt="右模型参考"/><img :src="asset(comparison.assets.prediction)" :style="{opacity:predictionOpacity}" alt="右模型预测叠加"/></div><p v-else>{{compareBusy?'读取同图对照中…':compareError}}</p><button v-if="compareError" @click="loadComparison">重试同图</button></figure>
          <figure><figcaption>错分区域 · 红：多圈 · 黄：漏圈 · 白：重合</figcaption><div class="image-stack"><img :src="asset(activeCase.assets.original)" alt="错分对照原图"/><img :src="asset(activeCase.assets.errors)" :style="{opacity:predictionOpacity}" alt="红色误分黄色漏分白色重合"/></div></figure>
        </div>
        <p>Dice {{pct(activeCase.dice)}} · IoU {{pct(activeCase.iou)}} · Precision {{pct(activeCase.precision)}} · Recall {{pct(activeCase.recall)}} · 边界F1 {{pct(activeCase.boundary_f1)}} · 预测／参考面积比 {{fixed(activeCase.area_ratio)}}。</p>
        <p>提示：{{activeCase.error_types.map(e=>errorNames[e]||e).join('、')}}。错分指与现有参考不一致，不等于已获得专家对真实结构的判定。</p>
        <details><summary>连通区域／实例对照明细</summary><pre>{{JSON.stringify(activeCase.objects,null,2)}}</pre><p>CH、FL为8连通区域；AR依据原始实例编号。均不自动赋予NOAA身份。</p></details>
      </div>
      <details v-if="activeModel" class="report-section"><summary>当前模型：台站分层、训练样本与阈值敏感性</summary><p v-if="activeModel.partial_training" class="notice">未完成训练的参考检查点，不计入正式完成数或完整基线。</p><p>最佳检查点第 {{activeModel.checkpoint_epoch}} 轮；历史Dice复算差 {{fixed(activeModel.reproduction_delta,8)}} · {{activeModel.reproduction_passed?'一致性通过':'一致性待核查'}}。</p><p>参数量 {{activeModel.performance?.parameters?.toLocaleString()}}；纯推理P95 {{fixed(activeModel.performance?.pure_p95_ms)}} ms；含缓存读图及预处理P95 {{fixed(activeModel.performance?.end_to_end_p95_ms)}} ms。</p><div class="table"><table><thead><tr><th>验证台站</th><th>图像数</th><th>Dice</th><th>Precision</th><th>Recall</th><th>边界F1</th></tr></thead><tbody><tr v-for="(s,name) in activeModel.strata?.instrument" :key="name"><td>{{name}}</td><td>{{s.images}}</td><td>{{pct(s.dice)}}</td><td>{{pct(s.precision)}}</td><td>{{pct(s.recall)}}</td><td>{{pct(s.boundary_f1)}}</td></tr></tbody></table></div><p v-for="(s,name) in activeModel.training_diagnostics?.diagnostic_subset" :key="name">{{name}}：{{s.images}} 张，Dice {{pct(s.dice)}}（训练内诊断，不是独立验证）。</p><p>阈值只作敏感性观察，主阈值始终为0.5，不据此自动改部署参数。</p><div class="threshold-values"><span v-for="t in activeModel.threshold_sensitivity" :key="t.threshold">{{t.threshold.toFixed(2)}} → {{pct(t.dice)}}</span></div></details>
      <p v-for="m in data.report?.models.filter(m=>m.state==='failed')" :key="m.id" class="error">{{m.id}} 诊断失败：{{m.error}}。其他模型结果保留。</p>
    </template>
  </section>
</template>
<style scoped>
.diagnostic-panel{background:var(--bg-panel);border:1px solid var(--border);border-radius:8px;padding:18px;min-width:0}.diagnostic-panel header,.actions,.filters,.pagination{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.diagnostic-panel header{justify-content:space-between}.diagnostic-panel h2{font-size:19px;margin:0}.diagnostic-panel h3{font-size:15px;margin-top:18px}.diagnostic-panel p{font-size:12px;line-height:1.7;color:var(--text-secondary)}button,select{background:var(--bg-base);color:var(--text-primary);border:1px solid var(--border);border-radius:5px;padding:6px 8px;font:12px inherit;max-width:100%}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}.diagnostic-status{display:flex;gap:10px;flex-wrap:wrap;background:#39898718;border-left:3px solid #45b9b0;padding:12px;margin-top:12px;font-size:12px}.diagnostic-status b{color:#49bdb2}.notice{padding:8px;border-left:3px solid #dbb261;background:#c9982510}.report-links{display:flex;gap:18px;font-size:12px;margin:10px 0}.report-links a{color:#65b8e1}.filters{font-size:12px;margin:12px 0}.filters label{display:flex;align-items:center;gap:6px}.filters input{width:95px}.table{overflow:auto;max-height:350px}table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}td,th{padding:9px;border-bottom:1px solid var(--border);white-space:nowrap}th{background:var(--bg-panel);position:sticky;top:0}.report-section{border-top:1px solid var(--border);padding:10px 0;margin-top:8px}.report-section summary,.case-detail summary{cursor:pointer;font-size:12px}.report-section article{padding:10px;border-bottom:1px solid var(--border);font-size:12px}.case-grid{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px}.case-grid button{padding:4px;min-width:0;text-align:left}.case-grid button.selected{outline:2px solid #64b8d6}.case-grid img{width:100%;aspect-ratio:1;object-fit:contain;background:black;display:block}.case-grid span,.case-grid b{display:block;font-size:10px;margin-top:4px;overflow-wrap:anywhere}.case-grid b{font-weight:500;color:#65b7cd}.pagination{justify-content:flex-end;font-size:12px;margin-top:10px}.uri{overflow-wrap:anywhere}.image-comparison{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.image-comparison figure{margin:0;min-width:0}.image-comparison figcaption{font-size:11px;min-height:32px;line-height:1.6}.image-comparison img{display:block;width:100%;aspect-ratio:1;object-fit:contain;background:#000}.image-stack{position:relative}.image-stack img+img{position:absolute;inset:0;background:transparent}.threshold-values{display:flex;flex-wrap:wrap;gap:8px;font-size:11px}.threshold-values span{background:var(--bg-base);padding:5px}.error{color:#ef8c83!important;overflow-wrap:anywhere}.diagnostic-panel pre{overflow:auto;font-size:11px}@media(max-width:1100px){.case-grid{grid-template-columns:repeat(6,minmax(0,1fr))}.image-comparison{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.diagnostic-panel{padding:10px}.case-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.image-comparison{grid-template-columns:1fr}.actions{width:100%}.filters label{max-width:100%}.filters select{min-width:0;max-width:230px}}
.findings{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:12px 0}.findings>article{border:1px solid var(--border);border-radius:6px;padding:12px;font-size:13px}.findings>details{grid-column:1/-1;padding:9px;font-size:12px}.findings details article{padding:10px;border-bottom:1px solid var(--border)}.findings summary{cursor:pointer}@media(max-width:650px){.findings{grid-template-columns:1fr}}
</style>
