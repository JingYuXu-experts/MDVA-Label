<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref} from 'vue'
import {get} from '@/api/client'

interface Arm {phase:string;state:string;step:number;total:number;updated_utc:string;train_dice?:number|null;last_train_dice?:number;validation_dice?:number;gate_passed?:boolean;error?:string;resource?:{peak_reserved_gib?:number;peak_rss_gib?:number}}
interface Study {id:string;updated_utc:string;active_arm:string;phase:string;state:string;arms:Record<string,Arm>;stage_context?:Record<string,{name:string;starting_epoch:number;target_epoch:number;additional_budget:number}>}
interface Entry {details?:{architecture_study?:Study}}
const progress=ref<Study>(),error=ref(''),clock=ref(Date.now())
let timer:ReturnType<typeof setInterval>|undefined,controller:AbortController|undefined,generation=0
const names:Record<string,string>={batch:'SCSS · BatchNorm基线',group:'SCSS · GroupNorm改进候选',group_keep_dropout:'SCSS GN · 保留50% Dropout',group_no_dropout:'SCSS GN · 关闭Dropout对照','GN-lr0.001':'GN · 学习率0.001对照','GN-lr0.0001':'GN · 学习率0.0001候选'}
const states:Record<string,string>={running:'运行记录',completed:'本阶段已完成',paused:'检查点已暂停',failed:'失败待排查',resource_failed:'资源未达标'}
const latestAge=computed(()=>progress.value?(clock.value-Date.parse(progress.value.updated_utc))/1000:0)
const stale=computed(()=>progress.value?.state==='running'&&latestAge.value>90)
const regularizationControl=computed(()=>progress.value?.phase.includes('正则化'))
const rateControl=computed(()=>progress.value?.phase.includes('学习率'))
const fullTraining=computed(()=>progress.value?.phase.includes('完整训练'))
const stage=computed(()=>progress.value?.stage_context?.['SCSS-GN-main'])
const rows=computed(()=>{
 const ids=progress.value?.phase.includes('完整训练')?['SCSS-GN-main']:rateControl.value?['GN-lr0.001','GN-lr0.0001']:regularizationControl.value?['group_keep_dropout','group_no_dropout']:['batch','group']
 return ids.map(id=>({id,value:progress.value?.arms[id]}))
})
const previous=computed(()=>Object.entries(progress.value?.arms||{}).filter(([id])=>!rows.value.some(row=>row.id===id)))
const percent=(n?:number|null)=>n==null||n<0?'—':`${(n*100).toFixed(2)}%`
const memory=(n?:number)=>n==null?'—':`${n.toFixed(2)} GiB`
function time(s:string){return new Date(s).toLocaleString('zh-CN',{hour12:false})}
async function refresh(){
 controller?.abort();controller=new AbortController();const signal=controller.signal,current=++generation
 try{
  const data=await get<{entries:Entry[]}>('/training/journal',{page:1,page_size:50,kind:'development'},{signal})
  if(current!==generation)return
  const found=data.entries.map(r=>r.details?.architecture_study).find(s=>s?.id==='scss-normalization-20261116')
  if(found){progress.value=found;clock.value=Date.now()}error.value=''
 }catch(e){if(!signal.aborted&&current===generation)error.value=String(e)}
}
onMounted(()=>{void refresh();timer=setInterval(()=>{clock.value=Date.now();void refresh()},10000)})
onBeforeUnmount(()=>{generation++;clearInterval(timer);controller?.abort()})
</script>

<template>
 <section v-if="progress" class="architecture-study" aria-label="SCSS独立架构实验进度">
  <header><h2>架构改进 · SCSS受控实验</h2><button @click="refresh">刷新新实验</button></header>
  <p v-if="fullTraining">固定SCSS-GN配置，使用2410张训练图，每轮完整评价526张验证图。{{stage?`${stage.name}：从第${stage.starting_epoch}轮检查点继续，累计目标第${stage.target_epoch}轮。`:'先检查8轮预算。'}}尚未替换应用模型。</p>
  <p v-else-if="rateControl">当前为固定训练样本的学习率／收敛预算检查；阶段名称和保存步数对应当前独立协议。</p>
  <p v-else-if="regularizationControl">当前控制实验从同一个GroupNorm检查点出发，使用相同图像和预算，只改变Dropout。此前归一化检查保留在下方。</p>
  <p v-else>归一化对照使用相同图像、初始卷积权重和训练预算。新实验与原登记39项分别记录。</p>
  <p>最近记录：{{time(progress.updated_utc)}} · {{progress.phase}}。{{stale?'记录超过90秒未更新，请核对本机进程；页面不把旧记录当作仍在训练。':'每10秒读取本地进度记录。'}}</p>
  <p v-if="progress.phase.includes('16图')" class="scope">当前是16张训练图的可学习性检查；这些Dice只能说明能否学会样本，尚无泛化结论。</p>
  <p v-else class="scope">当前为独立训练／开发验证，保留原模型包；正式评价与应用发布另设门槛。</p>
  <div class="arms"><article v-for="row in rows" :key="row.id">
   <h3>{{names[row.id]||row.id}}</h3>
   <template v-if="row.value">
   <p>{{fullTraining&&stage?stage.name:row.value.phase}} · {{stale&&row.value.state==='running'?'进程待核对':states[row.value.state]||row.value.state}}</p>
    <p>已完成 {{row.value.step}} / {{fullTraining&&stage?stage.target_epoch:row.value.total}} {{row.value.phase.includes('16图')?'次优化更新':'轮'}}</p>
    <progress :value="row.value.step" :max="Math.max(1,fullTraining&&stage?stage.target_epoch:row.value.total)" :aria-label="`${names[row.id]}进度`"/>
    <p v-if="row.value.phase.includes('16图')">训练样本Dice：<b>{{percent(row.value.last_train_dice??row.value.train_dice)}}</b></p>
    <p v-else>当前最佳验证Dice：<b>{{percent(row.value.validation_dice)}}</b></p>
    <p>显存预留峰值 {{memory(row.value.resource?.peak_reserved_gib)}} · 内存峰值 {{memory(row.value.resource?.peak_rss_gib)}}</p>
    <p v-if="row.value.gate_passed!=null">继续门槛：{{row.value.gate_passed?'通过本阶段检查':'尚未通过，不自动扩展'}}</p>
    <p v-if="row.value.error" role="alert">{{row.value.error}}</p>
   </template><p v-else>等待同一GPU顺序执行。</p>
  </article></div>
  <details v-if="previous.length"><summary>此前阶段记录（{{previous.length}}项）</summary><p v-for="[id,item] in previous" :key="id">{{names[id]||id}} · {{item.phase}} · {{states[item.state]||item.state}} · 训练样本Dice {{percent(item.last_train_dice??item.train_dice)}}</p></details>
  <p v-if="error" role="alert">读取失败：{{error}}；已有记录保留，可重试。</p>
 </section>
</template>

<style scoped>
.architecture-study{padding:20px;border:1px solid #30888a;border-radius:12px;background:var(--bg-panel);margin:16px 0;line-height:1.6}.architecture-study header{display:flex;align-items:center;justify-content:space-between;gap:12px}.architecture-study h2{font-size:19px;margin:0}.architecture-study h3{font-size:16px;margin:0}.architecture-study button{padding:7px 12px;background:var(--bg-elevated);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;cursor:pointer}.scope{font-weight:600;color:var(--text-secondary)}.arms{display:grid;grid-template-columns:1fr 1fr;gap:16px}.arms article{padding:14px;border:1px solid var(--border);border-radius:8px}.arms p{margin:8px 0;font-size:13px}.arms progress{width:100%;accent-color:#269fa5}@media(max-width:800px){.arms{grid-template-columns:1fr}.architecture-study{padding:12px}}
</style>
