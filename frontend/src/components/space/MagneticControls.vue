<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { get,post } from '@/api/client'
import type { MagneticModel } from './products'
import type { ModelStatus } from './guide'
const emit=defineEmits<{model:[value:MagneticModel];status:[value:ModelStatus]}>()
const mode=ref<'observed'|'manual'>('observed'),open=ref(false),busy=ref(false),error=ref(''),job=ref('')
const time=ref(new Date(Math.floor(Date.now()/600000)*600000).toISOString().slice(0,16))
const pressure=ref(2),dst=ref(-20),by=ref(2),bz=ref(-4),result=ref<MagneticModel>()
let generation=0,timer:ReturnType<typeof setTimeout>|undefined
async function calculate(){
  const n=++generation;clearTimeout(timer);busy.value=true;error.value=''
  emit('status',{state:'running'})
  try{
    const started=await post<{id:string;state:string}>('/sw/magnetosphere/model',{mode:mode.value,time:time.value+':00Z',pressure:pressure.value,dst:dst.value,by:by.value,bz:bz.value})
    if(n!==generation)return
    job.value=started.id
    const poll=async()=>{
      try{
        const task=await get<{state:string;result:MagneticModel;error:string}>('/sw/magnetosphere/model',{id:started.id})
        if(n!==generation)return
        if(task.state==='running'){timer=setTimeout(()=>void poll(),1800);return}
        if(task.state!=='done')throw new Error(task.error||'磁场计算未完成')
        result.value=task.result;emit('model',task.result);busy.value=false
        emit('status',{state:'ready'})
        const p=task.result.parameters;pressure.value=Number(p.pressure.toFixed(3));dst.value=p.dst;by.value=p.by;bz.value=p.bz
      }catch(e){if(n===generation){error.value=String(e);busy.value=false;emit('status',{state:'error',message:error.value})}}
    }
    await poll()
  }catch(e){if(n===generation){error.value=String(e);busy.value=false;emit('status',{state:'error',message:error.value})}}
}
function current(){mode.value='observed';time.value=new Date(Math.floor(Date.now()/600000)*600000).toISOString().slice(0,16);void calculate()}
defineExpose({current})
onMounted(()=>void calculate())
onBeforeUnmount(()=>{generation++;clearTimeout(timer)})
</script>
<template>
  <div class="magnetic-controls">
    <button class="model-toggle" @click="open=!open">{{open?'收起':'磁层参数'}} · {{busy?'计算中':result?.model||'等待驱动数据'}}</button>
    <section v-if="open" aria-label="磁层模型参数">
      <b>T96 磁层场 + IGRF-14</b><p>数值追踪磁力线；Shue 1998 独立边界估计。范围 X −50～20 Rₑ，未绘制区域仍有磁场。</p>
      <label>参数来源<select v-model="mode"><option value="observed">观测驱动快照</option><option value="manual">手动条件实验</option></select></label>
      <label>模型时间 UTC<input v-model="time" type="datetime-local"/></label>
      <div class="drivers"><label>动压 nPa（观测仅质子）<input v-model.number="pressure" type="number" min="0.1" max="50" step="0.1" :disabled="mode==='observed'"/></label><label>Dst nT<input v-model.number="dst" type="number" min="-300" max="50" :disabled="mode==='observed'"/></label><label>IMF By nT<input v-model.number="by" type="number" min="-50" max="50" step="0.1" :disabled="mode==='observed'"/></label><label>IMF Bz nT<input v-model.number="bz" type="number" min="-50" max="50" step="0.1" :disabled="mode==='observed'"/></label></div>
      <button :disabled="busy" @click="calculate">{{busy?'磁力线追踪中…':'计算此条件的磁层'}}</button><button :disabled="busy" @click="current">读取最新驱动</button>
      <p v-if="busy">后台任务 {{job.slice(0,8)}}；约需30秒。关闭面板不会取消计算。</p><p v-if="error" class="failure" role="alert">{{error}} <button :disabled="busy" @click="calculate">重试</button></p>
      <template v-if="result"><p>已显示：{{new Date(result.parameters.time*1000).toISOString()}} · {{result.driver_mode==='manual'?'手动实验':'观测驱动经验估计'}}。迎日点估计 {{result.boundary.r0.toFixed(2)}} Rₑ。</p><p>{{result.driver_note}}</p><p v-for="warning in result.warnings" :key="warning" class="failure">{{warning}}</p></template>
      <a href="https://ccmc.gsfc.nasa.gov/models/Tsyganenko%20Magnetic%20Field~T96/" target="_blank" rel="noreferrer">NASA CCMC 模型定义与限制 ↗</a>
    </section>
  </div>
</template>
<style scoped>
button{background:#102737;color:#d1e7f2;border:1px solid #36576c;border-radius:4px;padding:6px 9px;cursor:pointer;font:11px 'Segoe UI','Microsoft YaHei',sans-serif}button:hover{border-color:#80bdcc;background:#173c50}button:disabled{opacity:.4;cursor:default}select{background:#102737;color:#d1e7f2;border:1px solid #36576c;padding:6px;font-size:11px}a{line-height:1.7}

.magnetic-controls{position:absolute;left:18px;top:auto;bottom:195px;z-index:5;width:290px;max-width:calc(100% - 150px);font-size:11px}.magnetic-controls section{position:absolute;left:0;bottom:36px;width:100%;box-sizing:border-box;margin-top:7px;background:#091723f5;border:1px solid #3e5b70;padding:12px;border-radius:5px;max-height:180px;overflow:auto}.magnetic-controls p{font-size:10px;line-height:1.7;color:#9db5c8}.magnetic-controls label{display:flex;justify-content:space-between;gap:5px;margin:8px 0;align-items:center}.magnetic-controls input,.magnetic-controls select{width:162px;min-width:0;background:#0d2232;border:1px solid #3b586a;color:#d9eaf2;padding:5px;font-size:10px}.drivers{display:grid;grid-template-columns:1fr 1fr;gap:8px}.drivers label{display:block}.drivers input{display:block;width:100%;box-sizing:border-box;margin-top:4px}.magnetic-controls .failure{color:#e9b47d}.magnetic-controls a{color:#87c2db;font-size:10px}.magnetic-controls button{font-size:10px;margin:2px 4px 2px 0}.magnetic-controls input:disabled{opacity:.5} :global(.solar-studio.expanded) .magnetic-controls section{max-height:330px}@media(max-width:600px){.magnetic-controls{top:auto;bottom:205px;left:10px;width:230px;max-width:calc(100% - 110px)}.magnetic-controls section{max-height:180px}.magnetic-controls input,.magnetic-controls select{max-width:130px}}
</style>
