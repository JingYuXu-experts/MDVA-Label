<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { buildQuery, get, post } from '@/api/client'
import MButton from '@/components/ui/MButton.vue'
import SolarProjects from '@/components/solar/SolarProjects.vue'
import SolarInputGuide from '@/components/SolarInputGuide.vue'
import { useCtxStore } from '@/stores/ctx'

interface Observation {uri:string; product:string; day:string; clock:string; instrument:string; name:string; stem:string}
interface DayData {day:string; images:Observation[]; regions:Record<string,string>[]; events:{start:string;peak:string;class:string;noaa_report:string}[]; comparison_note:string}
interface Reference {available:boolean; category?:string; segmentation_kind?:string; instances:{id:number;pixels:number;noaa:string|null}[]}
interface RegionData {noaa:string|null; parameters:Record<string,string>|null; labels:Record<string,{positive:boolean;values:number[]}>;note:string}
interface Catalog {images:number; days:number; events:number; unknown_dates:number; products:Record<string,number>}
interface Baseline {id:string;task:string;name:string;paper:string;code:string;weights:string;inputs:string;fit:string;state:string}
const ctx = useCtxStore()
const router = useRouter()
const catalog = ref<Catalog|null>(null)
const days = ref<{day:string;n:number}[]>([])
const dayTotal = ref(0)
const page = ref(1)
const dateSearch = ref('')
const selectedDay = ref('')
const data = ref<DayData|null>(null)
const selected = ref<Observation|null>(null)
const compare = ref<Observation|null>(null)
const reference = ref<Reference|null>(null)
const region = ref<RegionData|null>(null)
const instance = ref(0)
const overlay = ref(true)
const opacity = ref(.35)
const error = ref('')
const imageError = ref(false)
const busy = ref(false)
const indexing = ref(false)
const indexStatus = ref('')
const tab = ref('projects')
const catalogUri = ref('')
function analyzeSelected() {
  if(selected.value){catalogUri.value=selected.value.uri;tab.value='projects'}
}
const baselines = ref<Baseline[]>([])
const policy = ref('')
let generation=0
let referenceGeneration=0
let timer: ReturnType<typeof setTimeout>|undefined
let disposed=false
const imageUrl=(uri:string)=>'/api/solar/image'+buildQuery({uri})
const overlayUrl=computed(()=>selected.value ? '/api/reference/overlay'+buildQuery({uri:selected.value.uri,width:1024,height:1024,instance:instance.value}) : '')
const groups = computed(()=>Array.from(new Set(data.value?.images.map(i=>i.product)||[])))
const productName=(name:string)=>({structure:'结构标注图像',observation:'标准化多源观测'}[name]||name)
async function loadDays() {
  const result=await get<{days:typeof days.value;total:number}>('/solar/days',{page:page.value,search:dateSearch.value})
  days.value=result.days;dayTotal.value=result.total
}
async function loadCatalog() {
  catalog.value=await get<Catalog>('/solar/catalog')
  await loadDays()
  if(!selectedDay.value && days.value.length) await chooseDay(days.value[0].day)
}
async function chooseDay(day:string) {
  const run=++generation
  selectedDay.value=day;busy.value=true;error.value='';data.value=null
  selected.value=null;compare.value=null;reference.value=null;region.value=null
  ++referenceGeneration
  try {
    const result=await get<DayData>('/solar/day',{day})
    if(run!==generation)return
    data.value=result
    const first=result.images.find(i=>i.product==='structure' && i.uri.includes('/AR/')) || result.images[0]
    if(first) await chooseImage(first)
  } catch(err) {if(run===generation)error.value=String(err)}
  finally {if(run===generation)busy.value=false}
}
async function chooseImage(image:Observation) {
  const run=++referenceGeneration
  selected.value=image;reference.value=null;region.value=null;instance.value=0;imageError.value=false
  try {
    const result=await get<Reference>('/reference',{uri:image.uri})
    if(run!==referenceGeneration)return
    reference.value=result
  } catch(err) {if(run===referenceGeneration)error.value=String(err)}
}
async function chooseRegion(id:number) {
  if(!selected.value)return
  const uri=selected.value.uri, run=referenceGeneration
  instance.value=id;region.value=null
  if(reference.value?.category!=='AR')return
  try {
    const result=await get<RegionData>('/solar/region',{uri,instance:id})
    if(run===referenceGeneration && id===instance.value)region.value=result
  } catch(err){if(run===referenceGeneration)error.value=String(err)}
}
async function chooseParameter(row:Record<string,string>) {
  const image=data.value?.images.find(i=>i.product==='structure' && i.stem===row.stem)
  if(!image){error.value='该参数表没有同名结构标注图像；不会把同日图像误作精确关联。';return}
  await chooseImage(image)
  await chooseRegion(Number(row.Num))
}
async function annotate() {
  if(!selected.value)return
  const uri=selected.value.uri
  const dataset=ctx.datasets.find(d=>d.meta?.source_id==='ssafd' && d.n_items>1000)
  if(selected.value.product!=='structure' || !dataset){error.value='请先选择结构标注图像；标准化观测目前用于对照，不会自动套用另一时刻的标注。';return}
  await ctx.setDataset(dataset.id)
  await router.push({path:'/annotate',query:{ws:ctx.ws,dataset:ctx.dataset,version:ctx.version,uri}})
}
async function indexAll() {
  indexing.value=true;error.value='';indexStatus.value='正在登记完整数据…'
  try {
    const result=await post<{task_id:string}>('/solar/index',{})
    const poll=async()=>{
      if(disposed)return
      try {
        const list=await get<{tasks:{id:string;state:string;report:Record<string,unknown>}[]}>('/tasks',{kind:'solar-index'})
        const task=list.tasks.find(t=>t.id===result.task_id)
        if(task?.state==='done'){indexing.value=false;indexStatus.value='完整索引已更新';await loadCatalog();return}
        if(task?.state==='failed'){indexing.value=false;error.value=String(task.report.error);return}
        indexStatus.value=`正在登记 ${String(task?.report.phase||'数据表')} · 已读取 ${String(task?.report.files_seen||0)} 个文件`
        timer=setTimeout(()=>void poll(),1000)
      }catch(err){indexing.value=false;error.value=String(err)}
    }
    await poll()
  }catch(err){indexing.value=false;error.value=String(err)}
}
async function changePage(delta:number){page.value+=delta;try{await loadDays()}catch(err){error.value=String(err)}}
async function searchDays(){page.value=1;try{await loadDays()}catch(err){error.value=String(err)}}
watch(tab,async current=>{
  try {
    if(current==='observations' && !catalog.value) await loadCatalog()
    if(current==='baselines' && !baselines.value.length){
      const result=await get<{baselines:Baseline[];selection_policy:string}>('/solar/baselines')
      baselines.value=result.baselines;policy.value=result.selection_policy
    }
  }catch(err){error.value=String(err)}
})
onBeforeUnmount(()=>{disposed=true;generation++;referenceGeneration++;clearTimeout(timer)})
</script>

<template>
  <div class="solar-workbench">
    <header class="solar-head">
      <div><h1>太阳科学工作台</h1><p>项目照片、太阳活动范围与事件试算</p></div>
      <div class="head-actions"><MButton :variant="tab==='projects'?'primary':'secondary'" @click="tab='projects'">项目识别</MButton><MButton :variant="tab==='observations'?'primary':'secondary'" @click="tab='observations'">观测与标注</MButton><MButton :variant="tab==='baselines'?'primary':'secondary'" @click="tab='baselines'">模型复现依据</MButton><MButton v-if="tab==='observations'" :disabled="indexing" @click="indexAll">{{ indexing?'正在登记…':'更新完整数据索引' }}</MButton></div>
    </header>
    <SolarProjects v-if="tab==='projects'" :catalog-uri="catalogUri" @imported="catalogUri=''" />
    <template v-else>
    <SolarInputGuide />
    <p v-if="error" role="alert" class="error">{{ error }} <MButton size="sm" @click="loadCatalog().catch(e=>error=String(e))">重试</MButton></p>
    <div class="summary"><span>图像文件 <b>{{ catalog?.images.toLocaleString()||'0' }}</b></span><span>观测日期 <b>{{ catalog?.days.toLocaleString()||'0' }}</b></span><span>耀斑事件 <b>{{ catalog?.events.toLocaleString()||'0' }}</b></span><span>日期待核验 {{ catalog?.unknown_dates||0 }}</span><span>{{ indexStatus }}</span><small>文件数含不同数据产品中的副本，不代表独立样本数；原始数据只读。</small></div>
    <div v-if="tab==='baselines'" class="baseline-pane">
      <h2>先复现，再选型</h2><p>{{ policy }}</p>
      <article v-for="model in baselines" :key="model.id" class="baseline-card">
        <div class="baseline-title"><h3>{{ model.name }}</h3><span>{{ model.task }} · {{ model.state }}</span></div>
        <dl><dt>原始输入</dt><dd>{{ model.inputs }}</dd><dt>本地适配</dt><dd>{{ model.fit }}</dd><dt>权重核验</dt><dd>{{ model.weights }}</dd></dl>
        <a :href="model.paper" target="_blank" rel="noreferrer">查看论文</a> <a v-if="model.code" :href="model.code" target="_blank" rel="noreferrer">作者代码</a>
      </article>
      <p>原文结果、原条件复现结果和本地适配结果分开记录。未通过复现验收的模型不提供“自动批准”或虚构预测概率。</p>
    </div>
    <div v-else class="workspace">
      <aside class="dates"><h2>观测日期</h2><form @submit.prevent="searchDays"><input v-model="dateSearch" placeholder="筛选年份或年月，如 2024-05" aria-label="筛选观测日期"/><MButton size="sm" type="submit">筛选</MButton></form><div class="day-list"><button v-for="day in days" :key="day.day" :class="{active:day.day===selectedDay}" @click="chooseDay(day.day)"><span>{{ day.day }}</span><small>{{ day.n }} 幅</small></button></div><footer><MButton size="sm" :disabled="page===1" @click="changePage(-1)">上一页</MButton><span>{{ page }}/{{ Math.max(1,Math.ceil(dayTotal/80)) }}</span><MButton size="sm" :disabled="page*80>=dayTotal" @click="changePage(1)">下一页</MButton></footer></aside>
      <main class="observations">
        <p v-if="!catalog?.images" class="empty">尚未登记完整观测。点击右上角更新完整数据索引；只读取文件信息与数据表，不执行模型或修改原件。</p>
        <p v-if="busy" role="status">正在读取 {{ selectedDay }} 的观测与关联…</p>
        <template v-if="data">
          <div class="day-title"><h2>{{ selectedDay }}</h2><span>{{ data.images.length }} 幅图像 · {{ data.regions.length }} 条活动区参数 · {{ data.events.length }} 条事件</span></div>
          <p class="note">{{ data.comparison_note }}</p>
          <div class="band-groups"><div v-for="group in groups" :key="group"><strong>{{ productName(group) }}</strong><div class="band-options"><button v-for="image in data.images.filter(i=>i.product===group)" :key="image.uri" :class="{active:selected?.uri===image.uri}" @click="chooseImage(image)">{{ image.instrument }} <small>{{ image.clock||'时刻待核验' }}</small></button></div></div></div>
          <div v-if="selected" class="image-tools"><MButton size="sm" @click="analyzeSelected">在项目中分析此图</MButton><span :title="selected.name">{{ selected.name }}</span><label v-if="reference?.available"><input v-model="overlay" type="checkbox"/>专家参考区域</label><input v-if="reference?.available" v-model.number="opacity" type="range" min="0" max="1" step="0.05" aria-label="参考区域透明度"/><MButton size="sm" :disabled="selected.product!=='structure'" @click="annotate">进入标注与修订</MButton><select aria-label="选择同日对照图像" @change="compare=data.images.find(i=>i.uri===($event.target as HTMLSelectElement).value)||null"><option value="">添加同日对照</option><option v-for="image in data.images.filter(i=>i.uri!==selected?.uri)" :key="image.uri" :value="image.uri">{{ image.instrument }} {{ image.clock }}</option></select></div>
          <div v-if="selected" class="image-pair" :class="{compared:compare}"><figure><div class="solar-image"><img :src="imageUrl(selected.uri)" :alt="selected.name" @error="imageError=true" @load="imageError=false"/><img v-if="reference?.available&&overlay" class="reference-overlay" :src="overlayUrl" :style="{opacity}" alt="原始参考区域"/></div><figcaption>{{ selected.instrument }} · {{ selected.clock }} <span v-if="imageError" class="error">图像载入失败，请重试</span></figcaption></figure><figure v-if="compare"><div class="solar-image"><img :src="imageUrl(compare.uri)" :alt="compare.name"/></div><figcaption>{{ compare.instrument }} · {{ compare.clock }}（独立观测，未配准）</figcaption></figure></div>
          <section v-if="reference?.available" class="object-section"><h3>{{ reference.category==='AR'?'原始活动区实例':'原始'+reference.category+'目标区域' }}</h3><p v-if="reference.segmentation_kind==='semantic'" class="note">此参考为类别区域图，不把像素值 1 当成一个独立对象；对象分组需按科学规则另行验证。</p><div class="object-buttons"><MButton size="sm" :variant="instance===0?'primary':'secondary'" @click="instance=0;region=null">全部区域</MButton><MButton v-for="item in reference.instances" :key="item.id" size="sm" :variant="instance===item.id?'primary':'secondary'" @click="chooseRegion(item.id)">{{ reference.category==='AR'?`区域 ${item.id}`:'类别区域' }}{{ item.noaa?' · NOAA '+item.noaa:'' }} · {{ item.pixels.toLocaleString() }} 像素</MButton></div><div v-if="region" class="region-detail"><h3>NOAA {{ region.noaa||'未匹配' }} · 原始实例 {{ instance }}</h3><dl v-if="region.parameters"><template v-for="(value,key) in region.parameters" :key="key"><dt>{{ key }}</dt><dd>{{ value||'未提供' }}</dd></template></dl><p v-else>该观测未提供可匹配参数表。</p><div class="label-badges"><span v-for="(value,key) in region.labels" :key="key">{{ key }}：{{ value.positive?'标签为阳性':'标签为阴性' }}</span></div><p class="note">{{ region.note }}</p></div></section>
          <section class="tables"><h3>活动区物理参数 · 数据集原表</h3><div class="table-scroll"><table><thead><tr><th>实例</th><th>NOAA</th><th>总无符号磁通量</th><th>面积（原表）</th><th>R</th><th>Hale</th><th>McIntosh</th><th></th></tr></thead><tbody><tr v-for="(row,index) in data.regions" :key="index"><td>{{ row.Num }}</td><td>{{ row.NOAA_AR }}</td><td>{{ row.Total_Unsigned_Flux }}</td><td>{{ row.Area }}</td><td>{{ row.R }}</td><td>{{ row.Hale||'—' }}</td><td>{{ row.McIntosh||'—' }}</td><td><MButton size="sm" @click="chooseParameter(row)">定位活动区</MButton></td></tr></tbody></table><p v-if="!data.regions.length" class="note">此日期未提供活动区参数表，不补造数值。</p></div><h3>当日耀斑事件 · 原始报告</h3><p class="note">当日事件列表与未来 24 小时标签的窗口可能不同；NOAA 保留原表写法，未强制补位或自动归属。</p><table><thead><tr><th>开始</th><th>峰值</th><th>等级</th><th>NOAA 原报告编号</th></tr></thead><tbody><tr v-for="(event,index) in data.events" :key="index"><td>{{ event.start }}</td><td>{{ event.peak }}</td><td>{{ event.class }}</td><td>{{ event.noaa_report }}</td></tr></tbody></table></section>
        </template>
      </main>
    </div>
    </template>
  </div>
</template>

<style scoped>
.solar-workbench{flex:1;min-height:0;display:flex;flex-direction:column;background:var(--bg-base)}h1{font-size:21px;margin:0 0 6px}h2{font-size:16px;margin:0}h3{font-size:14px;margin:0 0 12px}p{margin:0;line-height:1.7}.solar-head{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:20px 24px;border-bottom:1px solid var(--border)}.solar-head p,.note,small,figcaption{color:var(--text-secondary);font-size:12px}.head-actions{display:flex;gap:8px;flex-wrap:wrap}.summary{display:flex;flex-wrap:wrap;gap:20px;padding:12px 24px;border-bottom:1px solid var(--border);font-size:12px}.summary b{color:var(--accent);font-size:17px}.summary small{margin-left:auto}.workspace{display:flex;flex:1;min-height:0}.dates{width:210px;flex:none;border-right:1px solid var(--border);display:flex;flex-direction:column;padding:14px 10px;gap:12px}.dates form{display:flex;gap:4px;flex-wrap:wrap}input:not([type=checkbox]):not([type=range]),select{background:var(--bg-panel);border:1px solid var(--border);color:var(--text-primary);padding:6px;border-radius:4px;min-width:0}.dates input{width:100%;font-size:11px}.day-list{overflow:auto;flex:1;min-height:0}.day-list button,.band-options button{background:transparent;color:var(--text-secondary);border:1px solid var(--border);border-radius:5px;padding:7px 9px;cursor:pointer}.day-list button{display:flex;justify-content:space-between;width:100%;margin-bottom:5px;font-size:12px}.active{background:var(--accent-soft)!important;border-color:var(--accent)!important;color:var(--text-primary)!important}.dates footer{display:flex;align-items:center;justify-content:space-between;font-size:11px}.observations{flex:1;overflow:auto;min-width:0;padding:20px 24px}.day-title{display:flex;gap:20px;align-items:center;margin-bottom:8px}.day-title span{font-size:12px;color:var(--text-secondary)}.band-groups{display:flex;flex-direction:column;gap:12px;margin:18px 0}.band-groups strong{font-size:12px}.band-options{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.band-options button{font-size:12px}.image-tools{display:flex;align-items:center;flex-wrap:wrap;gap:12px;font-size:12px;margin-bottom:10px}.image-tools>span{max-width:370px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.image-tools label{display:flex;gap:5px;align-items:center}.image-tools input[type=range]{width:70px}.image-pair{display:grid;grid-template-columns:minmax(0,600px);gap:16px;justify-content:center;background:#090c11;border:1px solid var(--border);padding:12px;border-radius:8px}.image-pair.compared{grid-template-columns:repeat(2,minmax(0,1fr))}figure{margin:0;min-width:0}.solar-image{position:relative;aspect-ratio:1}.solar-image>img{width:100%;height:100%;object-fit:contain}.reference-overlay{position:absolute;inset:0;pointer-events:none}figcaption{text-align:center;padding-top:8px;overflow-wrap:anywhere}.object-section,.tables{margin-top:18px;padding:16px;background:var(--bg-panel);border:1px solid var(--border);border-radius:7px}.object-buttons,.label-badges{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.region-detail{border-top:1px solid var(--border);padding-top:14px;margin-top:12px}dl{display:grid;grid-template-columns:150px minmax(0,1fr);font-size:12px;gap:6px 14px}dt{color:var(--text-secondary)}dd{margin:0;overflow-wrap:anywhere}.label-badges span{font-size:12px;border:1px solid var(--border);padding:6px;border-radius:4px}.tables h3:not(:first-child){margin-top:22px}table{border-collapse:collapse;width:100%;font-size:12px;text-align:left}th,td{padding:8px;border-bottom:1px solid var(--border);white-space:nowrap}th{color:var(--text-secondary);font-weight:500}.table-scroll{overflow:auto}.error{color:var(--danger);padding:10px 24px;font-size:13px}.empty{padding:32px;color:var(--text-secondary)}.baseline-pane{overflow:auto;padding:24px;max-width:1100px}.baseline-pane>p{font-size:13px;color:var(--text-secondary);margin:12px 0}.baseline-card{padding:20px;border:1px solid var(--border);background:var(--bg-panel);border-radius:8px;margin:16px 0}.baseline-title{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}.baseline-title span{font-size:12px;color:var(--warning)}.baseline-card a{font-size:12px;color:var(--accent);margin-right:18px}@media(max-width:1050px){.solar-head{align-items:flex-start;flex-direction:column}.dates{width:165px}.observations{padding:14px}.summary{gap:12px}.image-pair.compared{grid-template-columns:1fr}}
</style>
