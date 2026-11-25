<script setup lang="ts">
import { computed,onBeforeUnmount,onMounted,ref,watch } from 'vue'
import { get,post } from '@/api/client'
import type { SolarImage } from './products'
const emit=defineEmits<{image:[value:SolarImage]}>()
const props=defineProps<{active?:boolean}>()
const channels=[
  {id:'304',label:'AIA 304 · 日珥 / 暗条',about:'看日缘日珥和日面暗条；红橙色为该通道的显示配色。'},
  {id:'171',label:'AIA 171 · 日冕环',about:'查看日冕环等结构，与304通道显示的物理信息不同。'},
  {id:'193',label:'AIA 193 · 日冕结构',about:'查看日冕结构及暗区；暗区需要进一步分析，不能只凭颜色认定类别。'},
  {id:'hmi',label:'HMI · 视向磁图',about:'查看视向磁场分布；黑白显示不同磁极性，不是普通可见光照片。'},
  {id:'c2',label:'LASCO C2 · 外日冕',about:'遮住明亮日面后观察外日冕，遮挡圆盘属于仪器视野。'},
  {id:'c3',label:'LASCO C3 · 广角日冕',about:'更大范围的外日冕视野，可逐帧观察物质向外运动。'},
]
const channel=ref('304'),date=ref(''),step=ref(5),viewMode=ref<'recent'|'history'|'example'>('recent')
const current=ref<SolarImage>(),frames=ref<SolarImage[]>([]),index=ref(0),busy=ref(false),preparing=ref(false),error=ref(''),progress=ref(''),playing=ref(false),imageReady=ref(false)
const zoom=ref(1),pan=ref({x:0,y:0}),now=ref(Date.now()),started=ref(0),checked=ref(0),automatic=ref(true),pinBusy=ref(false),pinNotice=ref('')
const selectedChannel=computed(()=>channels.find(c=>c.id===channel.value)!)
const displayedName=computed(()=>channels.find(c=>c.id===current.value?.channel)?.label||'等待影像')
const age=computed(()=>current.value?Math.max(0,Math.floor((now.value-Date.parse(current.value.observed_at))/60000)):null)
const ageLabel=computed(()=>{const m=age.value||0;return m<60?`${m}分钟`:m<1440?`${Math.floor(m/60)}小时${m%60}分`:`${Math.floor(m/1440)}天${Math.floor(m%1440/60)}小时`})
const elapsed=computed(()=>Math.max(0,Math.floor((now.value-started.value)/1000)))
const asset=(image:SolarImage)=>'/api/sw/assets?id='+encodeURIComponent(image.id)
const format=(value:string)=>value.replace('T',' ').replace(/\.\d+Z$/,'Z').replace('Z',' UTC')
let generation=0,displayGeneration=0,request:AbortController|undefined,timer:ReturnType<typeof setInterval>|undefined,refresh:ReturnType<typeof setInterval>|undefined,clock:ReturnType<typeof setInterval>|undefined,lastSequence=false
let dragging=false,dragStart={x:0,y:0,px:0,py:0}
function reset(){zoom.value=1;pan.value={x:0,y:0}}
function startDrag(e:PointerEvent){if(zoom.value<=1)return;dragging=true;dragStart={x:e.clientX,y:e.clientY,px:pan.value.x,py:pan.value.y};(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)}
function drag(e:PointerEvent){if(!dragging)return;const el=e.currentTarget as HTMLElement;const x=el.clientWidth*(zoom.value-1)/2,y=el.clientHeight*(zoom.value-1)/2;pan.value={x:Math.max(-x,Math.min(x,dragStart.px+e.clientX-dragStart.x)),y:Math.max(-y,Math.min(y,dragStart.py+e.clientY-dragStart.y))}}
async function show(image:SolarImage,owner=generation){
  const n=++displayGeneration;preparing.value=true;imageReady.value=false
  try{
    const bitmap=new Image();bitmap.src=asset(image);await bitmap.decode()
    if(owner!==generation||n!==displayGeneration)return
    current.value=image;imageReady.value=true;emit('image',image)
  }catch{if(owner===generation&&n===displayGeneration)throw new Error('图像读取或解码失败；请重试，未把上一通道当作本通道显示')}
  finally{if(owner===generation&&n===displayGeneration)preparing.value=false}
}
async function load(sequence=false,background=false){
  request?.abort();const controller=new AbortController();request=controller
  const n=++generation;displayGeneration++;busy.value=true;error.value='';progress.value='';pinNotice.value='';playing.value=false;started.value=Date.now();lastSequence=sequence
  const wantedChannel=channel.value,isLatest=viewMode.value==='recent'&&!sequence
  const start=Date.parse(date.value+':00Z'),interval=Number(step.value)*60000
  if(!background){current.value=undefined;frames.value=[];index.value=0;imageReady.value=false;reset()}
  if(sequence){viewMode.value=viewMode.value==='example'?'example':'history';automatic.value=false}
  let completed=0,duplicates=0;const failures:string[]=[]
  try{
    if(!isLatest&&!Number.isFinite(start))throw new Error('请选择有效的UTC时间')
    const count=sequence?Math.min(13,Math.max(0,Math.floor((Date.now()-start)/interval)+1)):1
    if(!count)throw new Error('起始时间晚于当前时刻，无法读取未来的实测图像')
    const read=async(i:number)=>{
      try{
        const frame=await get<SolarImage>('/sw/solar/image',isLatest?{channel:wantedChannel,latest:true}:{channel:wantedChannel,time:new Date(start+i*interval).toISOString()},{signal:controller.signal,timeout:60000})
        if(n!==generation)return
        if(frame.channel!==wantedChannel||!Number.isFinite(Date.parse(frame.observed_at)))throw new Error('返回的通道或观测时间不匹配，已拒绝显示')
        if(isLatest){
          const same=current.value?.id===frame.id
          frames.value=[frame];index.value=0
          if(same){duplicates++;current.value=frame}else{await show(frame,n);if(n!==generation)return}
        }else if(frames.value.some(f=>f.image_id===frame.image_id)){duplicates++}else{
          const next=[...frames.value.filter(f=>f.channel===wantedChannel),frame].sort((a,b)=>Date.parse(a.observed_at)-Date.parse(b.observed_at))
          frames.value=next
          if(!current.value||!sequence){await show(frame,n);if(n!==generation)return}
          index.value=Math.max(0,next.findIndex(f=>f.id===current.value?.id))
        }
        if(isLatest){
          checked.value=Date.now()
          date.value=new Date(Date.parse(frame.observed_at)-12*interval).toISOString().slice(0,16)
        }
      }catch(e){if(n===generation&&!controller.signal.aborted){const message=e instanceof Error?e.message:String(e);failures.push(/timeout|timed out/i.test(message)?'公开信源请求超时，请重试':message)}}
      finally{if(n===generation){completed++;progress.value=`已检查 ${completed}/${count} 个时刻 · ${frames.value.length} 张不同观测`}}
    }
    progress.value='正在获取'+selectedChannel.value.label
    await read(0)
    // First image is visible before fetching the remaining frames. Two bounded
    // requests avoid a long serial wait and keep upstream load modest.
    for(let i=1;i<count;i+=2){if(n!==generation)return;await Promise.all([read(i),i+1<count?read(i+1):Promise.resolve()])}
    if(n!==generation)return
    if(failures.length)error.value=`${failures.length} 个时刻读取失败：${failures[0]}${frames.value.length?'；已成功的观测仍可查看。':''}`
    if(sequence&&frames.value.length<2)error.value=error.value||'只有一张不同观测，暂不能播放；请增大采样间隔或换一个时间窗口。'
    if(duplicates)progress.value+=`；${duplicates} 个时刻对应同一影像，已合并`
    if(isLatest&&current.value)progress.value=background&&duplicates?'上游尚无新图，继续显示已注明时间的观测。':'已读取近期可用观测；实际延迟见画面时间。'
  }catch(e){if(n===generation)error.value=e instanceof Error?e.message:String(e)}
  finally{if(n===generation){busy.value=false;preparing.value=false}}
}
function stop(){generation++;displayGeneration++;request?.abort();busy.value=false;preparing.value=false;playing.value=false;progress.value=`已停止等待，保留 ${frames.value.length} 张已载入观测`;imageReady.value=!!current.value}
function changedChannel(){if(viewMode.value==='example')viewMode.value='history';void load()}
function changedDate(){viewMode.value='history';automatic.value=false;void load()}
function changedStep(){if(frames.value.length>1)void load(true)}
function latest(){viewMode.value='recent';automatic.value=true;void load()}
function example(){channel.value='304';viewMode.value='example';automatic.value=false;date.value='2012-08-31T20:15';step.value=5;void load(true)}
async function seek(value:number){
  const next=Math.max(0,Math.min(frames.value.length-1,value)),frame=frames.value[next]
  if(!frame)return
  index.value=next
  try{await show(frame)}catch(e){error.value=String(e);playing.value=false}
}
function playback(){if(index.value>=frames.value.length-1){void seek(0)}playing.value=!playing.value}
async function pin(){pinBusy.value=true;pinNotice.value='';try{for(const frame of [...frames.value])await post('/sw/pin',{run:'solar-'+frame.image_id});pinNotice.value=`已固定${frames.value.length}张观测，可离线回放。`}catch(e){pinNotice.value='固定失败：'+String(e)}finally{pinBusy.value=false}}
onMounted(()=>{
  void load()
  timer=setInterval(()=>{if(props.active!==false&&playing.value&&imageReady.value){if(index.value>=frames.value.length-1)playing.value=false;else void seek(index.value+1)}},800)
  clock=setInterval(()=>now.value=Date.now(),1000)
  refresh=setInterval(()=>{if(props.active!==false&&automatic.value&&viewMode.value==='recent'&&!busy.value&&!playing.value&&!document.hidden)void load(false,true)},60000)
})
watch(()=>props.active,active=>{if(active===false)playing.value=false})
onBeforeUnmount(()=>{generation++;displayGeneration++;request?.abort();clearInterval(timer);clearInterval(refresh);clearInterval(clock)})
</script>
<template>
  <section class="solar-observation" :aria-busy="busy" :data-selected-channel="channel" :data-displayed-channel="current?.channel||''">
    <div class="observation-view" @pointerdown="startDrag" @pointermove="drag" @pointerup="dragging=false" @pointercancel="dragging=false" @wheel.prevent="zoom=Math.min(4,Math.max(1,zoom+($event.deltaY<0?.15:-.15)));if(zoom===1)reset()">
      <img v-if="current" :key="current.id" :src="asset(current)" :style="{transform:`translate(${pan.x}px,${pan.y}px) scale(${zoom})`}" :alt="displayedName+' · 实测 '+current.observed_at" draggable="false"/>
      <div v-if="!current" class="observation-empty" role="status"><b>{{busy?'正在载入 '+selectedChannel.label:error?'该观测暂未取得':'选择观测通道和时间'}}</b><span>{{busy?`已等待 ${elapsed} 秒，首次获取需从公开信源下载。`:error||'载入后显示真实观测影像'}}</span></div>
      <div class="observation-stamp"><b>{{displayedName}}</b><time v-if="current">{{format(current.observed_at)}}</time><span>{{viewMode==='recent'?'近期真实观测':viewMode==='example'?'2012教学案例 · 历史观测':'历史观测回放'}} · 单视角</span><span v-if="current&&viewMode==='recent'" :class="{stale:age!==null&&age>30}">观测距现在 {{ageLabel}}{{age!==null&&age>30?' · 上游延迟，非当前状态':''}}</span></div>
      <div v-if="current&&(busy||preparing)" class="image-progress" role="status">{{preparing?'正在读取图像':progress}} · 已等待 {{elapsed}} 秒</div>
    </div>
    <aside>
      <b>太阳活动观测</b><p class="purpose">切换的是观测通道；各通道看见的结构不同。这里查看真实图像与演变，尚未叠加自动识别结果。</p>
      <label>观测通道<select v-model="channel" aria-label="太阳观测通道" @change="changedChannel"><option v-for="item in channels" :key="item.id" :value="item.id">{{item.label}}</option></select></label><p class="channel-help">{{selectedChannel.about}} 切换后自动读取。</p>
      <div class="mode-status"><b>{{viewMode==='recent'?'近期观测':viewMode==='example'?'教学案例':'历史回放'}}</b><p>{{viewMode==='recent'?'按实际可用影像更新，不是连续直播。':'按选定UTC时间逐帧回看，不随当前时间刷新。'}}</p><label v-if="viewMode==='recent'" class="auto-check"><input v-model="automatic" type="checkbox"/>每分钟检查新图</label><small v-if="viewMode==='recent'&&checked">上次检查 {{new Date(checked).toLocaleTimeString('zh-CN',{hour12:false})}}</small><button @click="latest">查看近期观测</button></div>
      <label>回放起点 UTC<input v-model="date" aria-label="太阳观测起始时间 UTC" type="datetime-local" @input="automatic=false" @change="changedDate"/></label>
      <label>时序采样间隔<select v-model.number="step" aria-label="太阳观测采样间隔" @change="changedStep"><option :value="1">1分钟</option><option :value="5">5分钟</option><option :value="15">15分钟</option></select></label>
      <div class="observation-actions"><button :disabled="busy" @click="viewMode='history';automatic=false;load()">读取此时刻</button><button :disabled="busy" @click="load(true)">载入时序（最多13帧）</button><button v-if="busy" @click="stop">停止等待</button></div>
      <div class="load-status" role="status"><span v-if="busy">{{progress}} · 已等待 {{elapsed}} 秒；首次下载可能较慢。</span><span v-else>{{progress}}</span></div>
      <p v-if="error" class="failure" role="alert">{{error}} <button :disabled="busy" @click="load(lastSequence)">重试此次读取</button></p>
      <div class="playback-section"><b>观测回放</b><p v-if="frames.length<2">先载入时序，取得至少两张不同观测，才能播放和前后切换。</p><div class="observation-actions"><button :disabled="frames.length<2||preparing" @click="playback">{{playing?'暂停回放':'播放观测'}}</button><button :disabled="!index||preparing" @click="seek(index-1)">上一帧</button><button :disabled="index>=frames.length-1||!frames.length||preparing" @click="seek(index+1)">下一帧</button></div>
      <input class="sequence-range" type="range" min="0" :max="Math.max(0,frames.length-1)" :value="index" :disabled="frames.length<2" aria-label="太阳观测帧" @input="seek(Number(($event.target as HTMLInputElement).value))"/><p>{{frames.length?index+1:0}} / {{frames.length}} 帧 · 每帧使用实际观测时间。</p><button :disabled="!frames.length||busy||pinBusy" @click="pin">{{pinBusy?'正在固定…':'固定离线时序'}}</button><p v-if="pinNotice" role="status">{{pinNotice}}</p></div>
      <label>放大 {{zoom.toFixed(1)}}×<input v-model.number="zoom" aria-label="太阳观测放大" type="range" min="1" max="4" step="0.1" @input="zoom===1&&reset()"/></label><button @click="reset">恢复完整视野</button><p>滚轮放大，拖动画面查看局部；放大不触发识别。</p>
      <button @click="example">2012日珥喷发案例</button><p>历史演示，可载入真实喷发时序；不是今天的太阳。</p>
      <p v-if="current">{{current.cache_hit?'读取自本地缓存。':''}}请求与实际观测相差 {{current.offset_seconds.toFixed(0)}} 秒。{{current.note}}</p>
      <a v-if="current" :href="current.source_url" target="_blank" rel="noreferrer">当前图像的原始元数据 ↗</a><a href="https://api.helioviewer.org/docs/v2/appendix/data_sources.html" target="_blank" rel="noreferrer">Helioviewer 通道与仪器说明 ↗</a><a href="https://svs.gsfc.nasa.gov/3999/" target="_blank" rel="noreferrer">NASA：2012-08-31案例来源 ↗</a>
    </aside>
  </section>
</template>
<style scoped>
button,select{background:#102737;color:#d1e7f2;border:1px solid #36576c;border-radius:4px;padding:6px 9px;cursor:pointer;font:11px 'Segoe UI','Microsoft YaHei',sans-serif}button:hover{border-color:#80bdcc;background:#173c50}button:disabled,input:disabled{opacity:.45;cursor:default}
.solar-observation{position:absolute;inset:85px 0 0;display:grid;grid-template-columns:minmax(0,1fr) 285px;background:#02060b;z-index:6;min-height:0}.observation-view{touch-action:none;cursor:grab;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;min-height:0;min-width:0;background:black}.observation-view img{pointer-events:none;user-select:none;max-height:100%;max-width:100%;object-fit:contain;transition:transform .15s}.observation-stamp{pointer-events:none;position:absolute;top:12px;left:14px;right:10px;font:11px/1.8 Consolas,monospace;color:#d3e5ee;text-shadow:0 1px 4px black}.observation-stamp>*{display:block}.observation-stamp .stale{color:#f0bf78}.observation-empty{padding:25px;display:flex;gap:12px;flex-direction:column;text-align:center;color:#9eb8ca;font-size:12px;line-height:1.8}.image-progress{position:absolute;bottom:12px;left:12px;right:12px;background:#122a3ce8;padding:9px;border-radius:4px;color:#d1e7f2;font-size:11px}.solar-observation aside{overflow:auto;min-height:0;min-width:0;border-left:1px solid #294454;background:#0a1721;padding:14px;font-size:11px}.solar-observation p{font-size:11px;line-height:1.7;color:#9cb0c1;margin:7px 0}.solar-observation label{display:block;margin:10px 0}.solar-observation select,.solar-observation input[type=datetime-local]{display:block;background:#102838;color:#d8e8f4;border:1px solid #3b596c;padding:7px;margin-top:5px;width:100%;min-width:0;box-sizing:border-box;font-size:11px}.observation-actions{display:flex;flex-wrap:wrap;gap:6px;margin:9px 0}.solar-observation a{display:block;color:#87c1d8;margin:10px 0;font-size:10px}.solar-observation .failure{color:#eaba83;background:#372b18;padding:9px;border-radius:4px}.sequence-range{width:100%}.mode-status,.playback-section{border:1px solid #294454;border-radius:5px;padding:9px;margin:10px 0}.mode-status>b{color:#89d6d4}.mode-status small{display:block;margin:6px 0;color:#9cb0c1}.solar-observation .auto-check{display:flex;gap:6px;align-items:center}.load-status{color:#8dcdd5;font-size:11px;line-height:1.7;overflow-wrap:anywhere}.purpose,.channel-help{color:#b0c8d8!important}.solar-observation input[type=range]{accent-color:#78b8c9;width:100%;margin-top:7px}
@media(max-width:750px){.solar-observation{grid-template-columns:1fr;grid-template-rows:minmax(230px,1fr) minmax(255px,44%)}.solar-observation aside{padding:10px;border-left:0;border-top:1px solid #294454}.solar-observation aside>.purpose{display:none}.observation-actions{margin:6px 0}.observation-stamp{font-size:10px}}
</style>
