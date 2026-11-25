<script setup lang="ts">
import {computed,onBeforeUnmount,onMounted,ref} from 'vue'
import {get} from '@/api/client'
interface Arm {state:string;step:number;training_dice?:number;error?:string}
interface Study {id:string;state:string;updated_utc:string;active_arm:string;arms:Record<string,Arm>;supplement?:string}
interface Entry {details?:{fl_learning_study?:Study}}
const progress=ref<Study>(),error=ref(''),clock=ref(Date.now())
let timer:ReturnType<typeof setInterval>|undefined,controller:AbortController|undefined,generation=0
const arms=[{id:'none-w1',name:'基础对照'},{id:'none-w10',name:'强调暗条像素'},{id:'batch-w1',name:'加入批次归一化'},{id:'batch-w10',name:'归一化＋强调暗条像素'}]
const states:Record<string,string>={running:'进行中',completed:'该组完成',paused:'已安全暂停',failed:'失败待排查'}
const stale=computed(()=>progress.value?.state==='running'&&clock.value-Date.parse(progress.value.updated_utc)>90000)
const completed=computed(()=>arms.filter(a=>progress.value?.arms[a.id]?.state==='completed').length)
const shownArms=computed(()=>[...arms,...Object.keys(progress.value?.arms||{}).filter(id=>!arms.some(a=>a.id===id)).map(id=>({id,name:id==='batch-w10-lr0.0001'?'作者学习率补充对照':id}))])
const score=(n?:number)=>n==null?'尚无评价':`${(n*100).toFixed(2)}%`
async function refresh(){
 controller?.abort();controller=new AbortController();const signal=controller.signal,current=++generation
 try{
  const result=await get<{entries:Entry[]}>('/training/journal',{page:1,page_size:50,kind:'development'},{signal})
  if(current!==generation)return
  const found=result.entries.map(e=>e.details?.fl_learning_study).find(s=>s?.id==='fl-learning-20261118')
  if(found)progress.value=found
  clock.value=Date.now();error.value=''
 }catch(e){if(!signal.aborted&&current===generation)error.value=String(e)}
}
onMounted(()=>{void refresh();timer=setInterval(()=>void refresh(),10000)})
onBeforeUnmount(()=>{generation++;clearInterval(timer);controller?.abort()})
</script>
<template>
 <section v-if="progress" class="fl-study" aria-label="暗条可学习性对照进度">
  <header><h2>暗条识别：查明为什么学不好</h2><button @click="refresh">刷新进度</button></header>
  <p class="scope">这是固定16张训练图的学习诊断。下方Dice只表示对已见样本的拟合，不能当作验证、测试成绩或实际识别率。</p>
  <p>4组使用相同图像、初始卷积权重和更新顺序，仅比较归一化与目标像素权重。已完成 {{completed}} / 4 组，每组最多300次更新。</p>
  <p v-if="stale" role="status">记录超过90秒未更新，请核对运行状态；页面不把旧记录当作仍在训练。</p>
  <p v-if="progress.supplement">{{progress.supplement}}</p>
  <div class="arms"><article v-for="arm in shownArms" :key="arm.id">
   <h3>{{arm.name}}</h3><p>{{progress.arms[arm.id]?states[progress.arms[arm.id].state]||progress.arms[arm.id].state:'待运行'}} · {{progress.arms[arm.id]?.step||0}} / 300 次更新</p>
   <progress :value="progress.arms[arm.id]?.step||0" :max="300" :aria-label="`${arm.name}更新进度`" />
   <p>最近评价的训练样本Dice：<b>{{score(progress.arms[arm.id]?.training_dice)}}</b></p>
   <p v-if="progress.arms[arm.id]?.error" role="alert">{{progress.arms[arm.id].error}}</p>
  </article></div>
  <p>每10秒读取本地记录。通过学习诊断后，仍须完整开发验证和独立评价，才考虑更换应用模型。</p>
  <p v-if="error" role="alert">读取失败：{{error}}；已有记录保留，可以重试。</p>
 </section>
</template>
<style scoped>
.fl-study{padding:20px;margin:16px 0;border:1px solid #359896;border-radius:12px;background:var(--bg-panel);line-height:1.65}.fl-study header{display:flex;align-items:center;justify-content:space-between;gap:12px}.fl-study h2{font-size:19px;margin:0}.fl-study h3{font-size:15px;margin:0}.scope{font-weight:600;color:var(--text-secondary)}.arms{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.arms article{padding:14px;border:1px solid var(--border);border-radius:8px}.arms p{font-size:13px}progress{width:100%;accent-color:#29a99a}button{padding:7px 12px;border:1px solid var(--border);border-radius:6px;color:var(--text-primary);background:var(--bg-elevated)}@media(max-width:700px){.arms{grid-template-columns:1fr}.fl-study{padding:12px}}
</style>
