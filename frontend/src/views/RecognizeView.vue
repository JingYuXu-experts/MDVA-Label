<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {get,getBlob,post} from '@/api/client'
import {useCtxStore} from '@/stores/ctx'
import SolarReviewEvidence from '@/components/solar/SolarReviewEvidence.vue'

interface Model {id:string;category:string;condition:string;input:string;available:boolean;reason:string}
interface Job {id:string;model_id:string;state:string;completed:number;total:number;message?:string;error?:string;input_kind?:string;input_metadata?:{filename:string;width:number;height:number;observed_utc:string|null};results:{index:number;state:string;error?:string}[]}
interface ReviewedRegion {id:string;bbox_xyxy:[number,number,number,number];ratios:Record<string,number>;dominant_polarity:string;dominant_share:number;significant_share:number;neighbors:{offset_hours:number;state:string;channels:{channel:string;url:string|null}[]}[]}
interface Result {width:number;height:number;foreground_pixels:number;overlay_id:string;annotations:unknown[];skipped:{reason:string}[];self_check?:{state:string;reason_codes:string[];unstable_primary_count:number;unstable_repeat_count:number};physical_review?:{state:string;message:string;cases:ReviewedRegion[]}}
const ctx=useCtxStore(),route=useRoute(),router=useRouter()
const models=ref<Model[]>([]),modelId=ref('AR-A0-s42'),confirmed=ref(false)
const file=ref<File>(),observed=ref(''),busy=ref(false),error=ref(''),requestId=ref('')
const job=ref<Job>(),history=ref<Job[]>([]),result=ref<Result>()
const originalUrl=ref(''),overlayUrl=ref(''),opacity=ref(.5),showOverlay=ref(true),downloading=ref(false)
const showEvidence=ref(true)
const model=computed(()=>models.value.find(m=>m.id===modelId.value))
const finished=computed(()=>job.value&&['completed','completed_with_errors','failed'].includes(job.value.state))
const states:Record<string,string>={queued:'已登记，等待识别',waiting_gpu:'等待GPU任务结束',running:'正在自动识别',completed:'自动范围已生成',completed_with_errors:'部分结果失败',failed:'识别失败',interrupted:'等待恢复'}
const checkReasons:Record<string,string>={empty_candidate:'未发现候选范围',raster_only_regions:'部分区域须查看掩膜',regions_sensitive_to_exposure:'部分范围对亮度变化敏感'}
function percent(value:number){return `${(value*100).toFixed(1)}%`}
let generation=0,polling=false,timer:ReturnType<typeof setInterval>|undefined
const workspace=()=>ctx.ws||'.'
function revoke(){for(const url of [originalUrl.value,overlayUrl.value])if(url)URL.revokeObjectURL(url);originalUrl.value='';overlayUrl.value=''}
function choose(event:Event){
  generation++;clearInterval(timer);job.value=undefined;result.value=undefined;revoke();error.value=''
  file.value=(event.target as HTMLInputElement).files?.[0];requestId.value='';confirmed.value=false
  if(file.value)originalUrl.value=URL.createObjectURL(file.value)
}
function dataUrl(value:File){return new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error('图片读取失败'));reader.onload=()=>resolve(String(reader.result));reader.readAsDataURL(value)})}
async function refresh(){
  const ws=workspace(),version=generation
  try{
    const [catalog,list]=await Promise.all([get<{models:Model[]}>('/solar/models',{ws}),get<{jobs:Job[]}>('/solar/inference/jobs',{ws})])
    if(version!==generation||ws!==workspace())return
    models.value=catalog.models;history.value=list.jobs.filter(j=>j.input_kind==='uploaded_image')
  }catch(e){if(version===generation)error.value=String(e)}
}
async function loadResult(current:Job,version:number,ws:string){
  const value=await get<Result>('/solar/inference/result',{id:current.id,index:0,ws})
  const image=await getBlob('/solar/inference/assets',{id:current.id,asset:value.overlay_id,ws})
  if(version!==generation||ws!==workspace()||current.id!==job.value?.id)return
  if(overlayUrl.value)URL.revokeObjectURL(overlayUrl.value)
  result.value=value;overlayUrl.value=URL.createObjectURL(image)
}
async function poll(){
  const current=job.value,version=generation,ws=workspace()
  if(!current||polling)return
  polling=true
  try{
    const value=await get<Job>('/solar/inference/jobs',{id:current.id,ws})
    if(version!==generation||ws!==workspace()||current.id!==job.value?.id)return
    error.value=''
    job.value=value
    if(['completed','completed_with_errors','failed'].includes(value.state)){
      clearInterval(timer)
      if(value.results.some(r=>r.state==='completed')&&!result.value)await loadResult(value,version,ws)
      await refresh()
    }
  }catch(e){if(version===generation)error.value=String(e)}
  finally{polling=false}
}
async function follow(value:Job){
  const version=++generation,ws=workspace()
  clearInterval(timer);revoke();error.value='';result.value=undefined;job.value=value;modelId.value=value.model_id
  try{
    const image=await getBlob('/solar/inference/input',{id:value.id,index:0,ws})
    if(version!==generation||ws!==workspace())return
    originalUrl.value=URL.createObjectURL(image)
    timer=setInterval(()=>{void poll()},2000);await poll()
  }catch(e){if(version===generation)error.value=String(e)}
}
async function start(){
  if(!file.value||!model.value?.available||!confirmed.value)return
  if(file.value.size>4*1024*1024){error.value='当前单张图像上限4MiB';return}
  busy.value=true;error.value='';const version=generation,ws=workspace()
  try{
    const image=await dataUrl(file.value)
    if(version!==generation)return
    requestId.value ||= crypto.randomUUID()
    const value=await post<Job>('/solar/analyze',{ws,model_id:modelId.value,condition:model.value.condition,
      confirm_input:true,acknowledge_research:true,filename:file.value.name,image_data:image,request_id:requestId.value,
      observed_utc:observed.value?new Date(observed.value).toISOString():''})
    if(version===generation&&ws===workspace()){
      await router.replace({path:'/recognize',query:{ws,job:value.id}});await follow(value)
    }
  }catch(e){if(version===generation)error.value=String(e)}
  finally{busy.value=false}
}
async function download(){
  const current=job.value
  if(!current)return
  downloading.value=true;error.value=''
  try{
    const blob=await getBlob('/solar/inference/annotations',{id:current.id,ws:workspace()})
    const url=URL.createObjectURL(blob),anchor=document.createElement('a')
    anchor.href=url;anchor.download=`${current.model_id}-自动标注.zip`;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){error.value=String(e)}finally{downloading.value=false}
}
watch(modelId,()=>{requestId.value='';confirmed.value=false})
watch(observed,()=>{requestId.value=''})
watch(()=>ctx.ws,async()=>{
  const version=++generation,ws=workspace()
  clearInterval(timer);revoke();job.value=undefined;result.value=undefined;error.value='';requestId.value=''
  await refresh()
  if(version!==generation||ws!==workspace())return
  const id=String(route.query.job||'')
  if(id){try{const value=await get<Job>('/solar/inference/jobs',{id,ws});if(version===generation&&ws===workspace()&&value.input_kind==='uploaded_image')await follow(value)}catch(e){if(version===generation)error.value=String(e)}}
},{immediate:true})
onBeforeUnmount(()=>{generation++;clearInterval(timer);revoke()})
</script>

<template>
  <div class="recognize-page">
    <header><h1>图片自动识别</h1><p>选择一张适配的太阳观测图，自动生成现象范围和标注文件，无需先建数据集或手动画轮廓。</p><p class="note">当前为研究模型，仍可能漏检或误检；输出保留为机器标注。</p></header>
    <div class="recognize-grid">
      <section class="input-card" aria-label="自动识别输入">
        <h2>1 · 输入观测图像</h2>
        <label>识别任务<select v-model="modelId" :disabled="busy" aria-label="识别任务"><option v-for="m in models" :key="m.id" :value="m.id" :disabled="!m.available">{{m.category==='AR'?'活动区 · HMI磁图':'冕洞 · AIA193'}}{{m.available?'':'（不可用）'}}</option></select></label>
        <p>{{model?.input}}</p><p v-if="model&&!model.available" role="alert">{{model.reason}}</p>
        <div v-if="model" class="input-domain" aria-label="当前模型的图像条件">
          <strong>这类图像才能使用当前模型</strong>
          <p v-if="model.category==='CH'">当前验证使用 SSAFD 数据集的标准化 AIA193 灰度图，日面外为白色背景。SDO 官网橙色浏览图的显示方式不同，尚不能直接套用；只转成灰度也不保证一致。</p>
          <p v-else>当前验证使用 SSAFD 数据集的标准化 HMI 视向磁图显示图。原始 FITS、不同磁场分量、色标或数值映射需要另行适配。</p>
          <p>仪器、通道相同，还需要核对方向、日面范围和图像显示方式。当前页面不会自动完成这些转换。</p>
        </div>
        <label class="file-picker">选择JPG / PNG<input type="file" accept="image/jpeg,image/png" :disabled="busy" @change="choose"/></label>
        <p>{{file?.name||'尚未选择图片'}} · 单张≤4MiB，至少256像素，完整日面显示图。</p>
        <label>观测时间（本地时间，可留空）<input v-model="observed" type="datetime-local" :disabled="busy"/></label>
        <label class="confirmation"><input v-model="confirmed" type="checkbox" :disabled="busy"/>我确认图像符合上述仪器、通道和显示方式；不是普通照片或局部截图。</label>
        <button class="primary" :disabled="busy||!file||!model?.available||!confirmed" @click="start">{{busy?'正在提交…':'开始自动识别'}}</button>
        <button @click="refresh">刷新模型与任务</button>
        <p class="note">原图副本与机器结果保存在本机，不上传外部服务。关闭页面后任务继续运行，可从下方历史任务恢复。</p>
      </section>
      <section class="result-card" aria-label="自动识别输出">
        <h2>2 · 查看自动范围</h2>
        <p v-if="job"><strong>{{states[job.state]||job.state}}</strong> · {{job.completed}} / {{job.total}}<br/>{{job.error||job.message}}</p>
        <p v-if="job">{{job.model_id}} · {{job.input_metadata?.filename}}<br/>观测时间：{{job.input_metadata?.observed_utc||'未提供，不能将此结果当作当前太阳状态'}}</p>
        <p v-if="job?.state==='waiting_gpu'">GPU正用于训练或其他任务。图像已登记，释放GPU后自动处理。</p>
        <div v-if="originalUrl" class="image-stage">
          <img :src="originalUrl" alt="自动识别输入原图"/>
          <img v-if="overlayUrl&&showOverlay" :src="overlayUrl" :style="{opacity}" class="prediction" alt="自动输出的活动范围"/>
          <svg v-if="showEvidence&&result?.physical_review?.cases.length" class="review-regions" :viewBox="`0 0 ${result.width} ${result.height}`" role="img" aria-label="多图支持的高度疑似冕洞位置">
            <rect v-for="item in result.physical_review.cases" :key="item.id" :x="item.bbox_xyxy[0]" :y="item.bbox_xyxy[1]" :width="item.bbox_xyxy[2]-item.bbox_xyxy[0]" :height="item.bbox_xyxy[3]-item.bbox_xyxy[1]" vector-effect="non-scaling-stroke"/>
          </svg>
        </div>
        <div v-else class="empty">在左侧选择观测图像，或打开一个历史识别任务。</div>
        <div v-if="result" class="result-controls"><label><input v-model="showOverlay" type="checkbox"/>显示模型范围</label><label>透明度<input v-model.number="opacity" type="range" min="0" max="1" step="0.05"/></label>
          <p>模型前景覆盖图像 {{(100*result.foreground_pixels/(result.width*result.height)).toFixed(2)}}%；这表示像素覆盖，不是事件概率或太阳表面物理面积。</p>
          <p v-if="!result.foreground_pixels">未检出候选范围，不能据此认定没有目标。</p>
          <details v-if="result.self_check" :open="result.self_check.state==='needs_review'">
            <summary>识别自检 · {{result.self_check.state==='needs_review'?'建议复核':'亮度扰动下较稳定'}}</summary>
            <p v-if="result.self_check.reason_codes.length">{{result.self_check.reason_codes.map(code=>checkReasons[code]||'请核对范围').join('、')}}。</p>
            <p v-if="result.self_check.unstable_primary_count||result.self_check.unstable_repeat_count">{{result.self_check.unstable_primary_count}} 处原候选和 {{result.self_check.unstable_repeat_count}} 处扰动后候选发生明显变化。</p>
            <p>自检比较同一图像在小幅调整亮度前后的识别范围。是否漏检或误检仍需对照观测。</p>
          </details>
          <button :disabled="downloading" @click="download">{{downloading?'正在整理…':'下载自动标注（掩膜 / COCO / 区域数据）'}}</button>
          <details v-if="result.skipped.length"><summary>轮廓转换说明（完整掩膜已保留）</summary><p v-for="(item,i) in result.skipped" :key="i">{{item.reason}}</p></details>
        </div>
        <section v-if="result?.physical_review" class="physical-review" aria-label="当前图片的多图复核">
          <h3>当前图片的多图复核</h3>
          <p>{{result.physical_review.message}}</p>
          <label v-if="result.physical_review.cases.length"><input v-model="showEvidence" type="checkbox"/>显示高度疑似位置</label>
          <details v-for="item in result.physical_review.cases" :key="item.id" open>
            <summary>高度疑似冕洞 · 人工参考未覆盖</summary>
            <p>AIA 193 和 211 Å 图像的区域亮度分别为周围区域的 {{percent(item.ratios['193']??0)}} 和 {{percent(item.ratios['211']??0)}}。约 {{percent(item.significant_share)}} 的像素视向磁场达到 5 G，在这些像素中 {{percent(item.dominant_share)}} 为{{item.dominant_polarity==='positive'?'正':'负'}}极性。</p>
            <p v-for="neighbor in item.neighbors" :key="neighbor.offset_hours">
              {{neighbor.offset_hours<0?'早':'晚'}} {{Math.abs(neighbor.offset_hours)}} 小时 · {{neighbor.state==='darkness_persists'?'两个通道均仍可见暗区':neighbor.state==='alignment_uncertain'?'对齐未通过':'未取得可支持的证据'}}
              <template v-for="channel in neighbor.channels" :key="channel.channel"><a v-if="channel.url" :href="channel.url" target="_blank" rel="noopener noreferrer">AIA {{channel.channel}} Å 原始资料</a></template>
            </p>
          </details>
        </section>
        <button v-if="finished&&!result" @click="poll">重新读取结果</button>
        <p v-if="error" role="alert" class="error">{{error}}</p>
      </section>
    </div>
    <SolarReviewEvidence :key="workspace()" />
    <details class="science"><summary>输入条件、模型效果与事件预测边界</summary><p>当前AR/CH模型输出结构范围，仍处于研究候选阶段；对其他仪器、原始FITS、不同显示映射尚未验证。当前图像任务不输出耀斑、CME或地球影响概率。事件风险分析还需要关联活动区及适配的磁场或太阳风等观测，不能从区域面积直接推出“太阳风暴”。</p><RouterLink to="/training">查看实测效果与改进进度</RouterLink> · <RouterLink to="/monitor">查看权威空间天气监测与机构预报</RouterLink></details>
    <section class="history"><h2>最近图片识别任务</h2><p v-if="!history.length">暂无任务。</p><button v-for="item in history" :key="item.id" @click="follow(item)">{{item.input_metadata?.filename||item.id.slice(0,8)}} · {{item.model_id}} · {{states[item.state]||item.state}}</button></section>
  </div>
</template>

<style scoped>
.review-regions{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}.review-regions rect{fill:none;stroke:#ffbe58;stroke-width:2;stroke-dasharray:5 3}.physical-review{margin-top:16px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;font-size:14px;line-height:1.6}.physical-review h3{margin:0;font-size:15px}.physical-review details{margin-top:8px}.physical-review summary,.result-controls summary{cursor:pointer}.physical-review a{display:inline-block;margin-left:.65rem}.result-controls details{width:100%}
.recognize-page{overflow:auto;padding:24px;max-width:1600px;width:100%;margin:0 auto}.recognize-page h1{font-size:24px;margin:0 0 8px}.recognize-page h2{font-size:18px;margin:0 0 16px}.recognize-grid{display:grid;grid-template-columns:minmax(280px,360px) minmax(0,1fr);gap:20px;margin-top:24px}.input-card,.result-card,.history,.science{padding:20px;background:var(--bg-panel);border:1px solid var(--border);border-radius:12px}.input-card label{display:block;margin:14px 0}.input-card select,.input-card input[type=datetime-local]{display:block;max-width:100%;padding:9px;margin-top:8px;background:var(--bg-panel);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;color-scheme:dark}.confirmation{line-height:1.7}.note,.science{font-size:13px;line-height:1.7}.recognize-page button{padding:9px 13px;border:1px solid var(--border);border-radius:7px;background:var(--bg-elevated);color:var(--text-primary);cursor:pointer;margin:6px 8px 6px 0}.recognize-page button:disabled{opacity:.5;cursor:default}.recognize-page .primary{background:#177c84;color:white}.image-stage{position:relative;max-width:680px;margin:16px auto}.image-stage img{display:block;width:100%;height:auto}.image-stage .prediction{position:absolute;inset:0;height:100%}.empty{min-height:240px;display:grid;place-content:center;color:var(--text-secondary)}.result-controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center}.result-controls p{width:100%;margin:0}.error{color:#d75062}.science,.history{margin-top:20px}.science summary{cursor:pointer;font-weight:600}.history button{display:block;max-width:100%;overflow-wrap:anywhere}@media(max-width:800px){.recognize-page{padding:12px}.recognize-grid{grid-template-columns:1fr}.input-card,.result-card{padding:14px}}
</style>

<style scoped>
.input-domain{padding:12px;border-left:3px solid #329da5;background:var(--bg-elevated);border-radius:5px;font-size:13px;line-height:1.65}.input-domain p{margin:8px 0 0}.input-domain strong{font-size:14px}
</style>
