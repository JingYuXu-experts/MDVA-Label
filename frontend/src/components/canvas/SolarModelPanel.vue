<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {get,post,getBlob} from '@/api/client'
import {useCanvasStore,type Geom} from '@/stores/canvas'
import {useCtxStore} from '@/stores/ctx'
import {useItemsStore} from '@/stores/items'
import {useToast} from '@/stores/toast'

interface Model {id:string;category:string;condition:string;available:boolean;reason:string;scientific_test_complete:boolean;input:string;test?:{dice:number}}
interface Row {index:number;uri:string;state:string;annotations?:number;skipped?:number;error?:string}
interface Job {id:string;model_id:string;dataset:string;version:string;state:string;completed:number;total:number;results:Row[];message?:string;error?:string}
interface Candidate {model:string;uri:string;overlay_id:string;annotations:{geom:Geom;coords:number[];label:string;source:string;backend:string;confidence:number;note:string}[];skipped:{component:number;reason:string}[];score_scope:string}
const emit=defineEmits<{pick:[uri:string]}>()
const store=useCanvasStore(),ctx=useCtxStore(),items=useItemsStore(),toast=useToast()
const models=ref<Model[]>([]),modelId=ref('AR-A0-s42'),confirmed=ref(false),busy=ref(false),error=ref('')
const job=ref<Job>(),history=ref<Job[]>([]),candidate=ref<Candidate>(),opacity=ref(.45),shown=ref(true)
const panelOpen=ref(false)
const downloading=ref(false)
let generation=0,resultGeneration=0,pollGeneration=0,objectUrl=''
let timer:ReturnType<typeof setInterval>|undefined
const model=computed(()=>models.value.find(m=>m.id===modelId.value))
const title=computed(()=>model.value?.category==='AR'?'HMI显示域磁图':'AIA 193 Å图像')
const selectedCount=computed(()=>items.selected.size)
const names:Record<string,string>={queued:'已排队',waiting_gpu:'等待GPU',running:'候选推理中',completed:'候选生成完成',completed_with_errors:'部分完成，请查看失败项',failed:'任务失败',interrupted:'等待恢复'}
const contextKey=()=>`${ctx.ws}|${ctx.dataset}|${ctx.version}`
function clearOverlay(){if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl='';store.modelOverlay=null}
async function list(){
  const context=contextKey(),run=++generation
  try{
    const [catalog,tasks]=await Promise.all([get<{models:Model[]}>('/solar/models'),get<{jobs:Job[]}>('/solar/inference/jobs')])
    if(run!==generation||context!==contextKey())return
    models.value=catalog.models;history.value=tasks.jobs.filter(j=>j.dataset===ctx.dataset)
    error.value=''
  }catch(e){if(run===generation)error.value=String(e)}
}
async function loadResult(){
  const current=job.value,uri=store.currentUri,context=contextKey(),run=++resultGeneration
  candidate.value=undefined;clearOverlay();error.value=''
  const row=current?.results.find(r=>r.uri===uri&&r.state==='completed')
  if(!current||!row||current.dataset!==ctx.dataset)return
  try{
    const result=await get<Candidate>('/solar/inference/result',{id:current.id,index:row.index})
    const blob=await getBlob('/solar/inference/assets',{id:current.id,asset:result.overlay_id})
    if(run!==resultGeneration||context!==contextKey()||uri!==store.currentUri||current.id!==job.value?.id)return
    candidate.value=result;objectUrl=URL.createObjectURL(blob)
    if(shown.value)store.modelOverlay={uri,src:objectUrl,opacity:opacity.value}
  }catch(e){if(run===resultGeneration)error.value=String(e)}
}
async function poll(){
  const id=job.value?.id,context=contextKey()
  const run=++pollGeneration
  if(!id)return
  try{
    const value=await get<Job>('/solar/inference/jobs',{id})
    if(run!==pollGeneration||context!==contextKey()||id!==job.value?.id)return
    const before=job.value?.results.find(r=>r.uri===store.currentUri&&r.state==='completed')
    job.value=value
    const after=value.results.find(r=>r.uri===store.currentUri&&r.state==='completed')
    if(after&&!before)await loadResult()
    if(['completed','completed_with_errors','failed'].includes(value.state)){clearInterval(timer);timer=undefined;await list()}
  }catch(e){if(context===contextKey())error.value=String(e)}
}
function follow(value:Job){
  if(value.dataset!==ctx.dataset){error.value='该任务属于其他数据集，请先切换数据集。';return}
  job.value=value;modelId.value=value.model_id;panelOpen.value=true;clearInterval(timer);timer=setInterval(()=>{void poll()},2000)
  void loadResult();void poll()
}
async function start(batch:boolean){
  if(!model.value||!confirmed.value)return
  const uris=batch?[...items.selected]:[store.currentUri]
  if(!uris.length)return
  const context=contextKey();busy.value=true;error.value=''
  try{
    const value=await post<Job>('/solar/inference',{model_id:modelId.value,dataset:ctx.dataset,version:ctx.version,
      condition:model.value.condition,uris,request_id:crypto.randomUUID(),acknowledge_research:true})
    if(context===contextKey()){follow(value);toast.info('任务已登记，关闭页面后仍会执行；候选不会自动写入标注。')}
  }catch(e){if(context===contextKey())error.value=String(e)}
  finally{busy.value=false}
}
function accept(){
  if(!candidate.value||candidate.value.uri!==store.currentUri||store.loading||store.saving)return
  let added=0
  for(const ann of candidate.value.annotations){
    if(store.draftAnns.some(a=>a.backend===ann.backend&&a.label===ann.label&&a.geom===ann.geom&&a.coords.join(',')===ann.coords.join(',')))continue
    if(store.addAnn({...ann,note:`${ann.note}；候选任务${job.value?.id}`}))added++
  }
  toast.info(`已将${added}个可转换轮廓加入草稿；请检查后保存，可撤销。`)
}
async function downloadAnnotations(){
  const current=job.value
  if(!current)return
  downloading.value=true;error.value=''
  try{
    const blob=await getBlob('/solar/inference/annotations',{id:current.id})
    const url=URL.createObjectURL(blob),anchor=document.createElement('a')
    anchor.href=url;anchor.download=`${current.model_id}-${current.id.slice(0,8)}-自动标注.zip`;anchor.click()
    setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){error.value=String(e)}
  finally{downloading.value=false}
}
watch(()=>[ctx.ws,ctx.dataset,ctx.version],()=>{generation++;resultGeneration++;job.value=undefined;candidate.value=undefined;clearInterval(timer);clearOverlay();if(ctx.dataset&&ctx.version)void list()},{immediate:true})
watch(()=>store.currentUri,uri=>{resultGeneration++;candidate.value=undefined;clearOverlay();if(uri.startsWith('@ssafd/CH/'))modelId.value='CH-C2-s2026';else if(uri.startsWith('@ssafd/AR/'))modelId.value='AR-A0-s42';void loadResult()})
watch(modelId,()=>{confirmed.value=false})
watch([opacity,shown],()=>{store.modelOverlay=shown.value&&objectUrl?{uri:store.currentUri,src:objectUrl,opacity:opacity.value}:null})
onBeforeUnmount(()=>{generation++;resultGeneration++;clearInterval(timer);clearOverlay()})
</script>

<template>
  <details :open="panelOpen" class="solar-model" aria-label="太阳模型候选标注" @toggle="panelOpen=($event.target as HTMLDetailsElement).open">
    <summary>太阳模型候选标注 <small>AR / CH · 人工辅助</small><span v-if="job"> · {{names[job.state]||job.state}} {{job.completed}}/{{job.total}}</span></summary>
    <div class="model-controls">
      <label>识别对象 <select v-model="modelId" aria-label="选择太阳模型"><option v-for="m in models" :key="m.id" :value="m.id">{{m.category==='AR'?'活动区 AR · HMI磁图':'冕洞 CH · AIA193'}}{{m.available?'':'（不可用）'}}</option></select></label>
      <span>{{model?.scientific_test_complete?'已完成独立测试，仍需人工核对':'研究候选 · 待独立测试'}}</span>
      <span v-if="model?.test">该候选独立测试 Dice {{(model.test.dice*100).toFixed(2)}}%（区域重合指标，非正确概率）</span>
      <p v-if="model&&!model.available">{{model.reason}}</p>
      <label><input v-model="confirmed" type="checkbox" aria-label="确认模型输入条件"/>已确认所选图像是{{title}}，候选需要人工核对</label>
      <button :disabled="busy||!confirmed||!model?.available||!store.currentUri||store.loading" @click="start(false)">识别当前图像</button>
      <button :disabled="busy||!confirmed||!model?.available||selectedCount<1||selectedCount>200" @click="start(true)">识别已选 {{selectedCount}} 张</button>
      <button @click="list">刷新模型与任务</button>
    </div>
    <p class="hint">只输入图像，不使用参考掩膜。每批最多200张。机器结果自动保存在任务中，完成后可整批下载，无需逐张接纳；人工草稿单独保存。AR区域不自动生成NOAA编号。</p>
    <p v-if="error" role="alert" class="error">{{error}}</p>
    <div v-if="job" class="job"><strong>{{names[job.state]||job.state}} · {{job.completed}} / {{job.total}}</strong><progress :value="job.completed" :max="job.total"/><p>{{job.error||job.message}}</p><p>候选输入版本 {{job.version}}；接纳只进入当前草稿，不修改输入版本。</p>
      <button v-if="['completed','completed_with_errors'].includes(job.state)" :disabled="downloading" @click="downloadAnnotations">{{downloading?'正在整理自动标注…':'下载整批自动标注（掩膜 / COCO / 区域位置与面积）'}}</button>
      <details v-if="job.results.length"><summary>逐图结果与失败原因</summary><div class="results"><button v-for="row in job.results" :key="row.index" @click="emit('pick',row.uri)">#{{row.index+1}} {{row.uri.split('/').pop()}} · {{row.state==='completed'?`${row.annotations}个可编辑轮廓 / ${row.skipped}个需人工处理`:row.error}}</button></div></details>
    </div>
    <div v-if="candidate" class="candidate-controls"><label><input v-model="shown" type="checkbox"/>模型栅格叠加</label><label>透明度 <input v-model.number="opacity" type="range" min="0" max="1" step="0.05"/></label><button :disabled="!candidate.annotations.length||store.loading||store.saving" @click="accept">接纳可编辑轮廓到草稿（{{candidate.annotations.length}}）</button><span>{{candidate.score_scope}}</span>
      <p v-if="!candidate.annotations.length&&!candidate.skipped.length">模型未产生前景区域；不代表科学上确定不存在目标。</p>
      <p v-for="(skip,i) in candidate.skipped" :key="i">需人工处理：{{skip.reason}}。栅格候选完整保留。</p>
    </div>
    <details v-if="history.length"><summary>最近候选任务（关闭页面后可在这里重新打开）</summary><button v-for="j in history" :key="j.id" class="history-row" @click="follow(j)">{{j.model_id}} · {{names[j.state]||j.state}} · {{j.completed}}/{{j.total}} · 输入版本 {{j.version.slice(0,8)}}</button></details>
  </details>
</template>

<style scoped>
.solar-model{padding:12px 16px;border:1px solid #287d86;border-radius:10px;margin:8px 0;background:#112c3620}.solar-model summary{cursor:pointer;font-weight:600}.solar-model small,.hint{font-size:12px;opacity:.8}.model-controls,.candidate-controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}.solar-model button,.solar-model select{padding:6px 9px;border:1px solid #607785;border-radius:6px;background:var(--bg-elevated,#e9f1f4);color:var(--text-primary,#243642);cursor:pointer}.solar-model button:disabled{opacity:.5;cursor:default}.job{margin-top:12px}.job progress{display:block;width:100%;margin:7px 0}.results{display:grid;max-height:180px;overflow:auto;gap:5px}.history-row{display:block;margin-top:5px;max-width:100%;overflow-wrap:anywhere}.error{color:#df5c68}.candidate-controls p{width:100%;margin:3px 0}
</style>
