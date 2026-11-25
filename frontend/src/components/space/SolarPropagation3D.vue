<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createScene, type CameraPreset, type RenderStats } from './createScene'
import { usableAnalysis } from './sceneMath'
import { lesson } from './lessons'
import type { Inspection, Topic, WindSample } from './physics'
import MagneticControls from './MagneticControls.vue'
import SolarObservation from './SolarObservation.vue'
import MagnetosphereMovie from './MagnetosphereMovie.vue'
import { get } from '@/api/client'
import type { MagneticModel, SolarImage } from './products'
import EventImpactPanel from './EventImpactPanel.vue'
import { earthImpact, type PropagationModel, type SolarEvent } from './impact'
import SceneGuide from './SceneGuide.vue'
import { guideContent, type GuideStep, type ModelStatus } from './guide'

const props = defineProps<{ time: number; events: SolarEvent[]; selectedId?: string; wind?: WindSample; simulations?: PropagationModel[]; trainingActive?: boolean }>()
const emit = defineEmits<{ time: [value: number]; select: [id: string] }>()
const root = ref<HTMLElement | null>(null)
const viewport = ref<HTMLElement | null>(null)
const expanded = ref(false)
const ready = ref(false)
const error = ref('')
const experience=ref<'guided'|'expert'>('guided')
const guideStep=ref<GuideStep>(props.selectedId?3:1)
const guideVisible=ref(false),summaryVisible=ref(true),sourcesVisible=ref(false),controlsVisible=ref(false)
const expert=computed(()=>experience.value==='expert')
const magneticActivated=ref(false),magneticStatus=ref<ModelStatus>({state:'idle'})
const magneticControls=ref<InstanceType<typeof MagneticControls>|null>(null)
const grid = ref(true)
const field = ref(true)
const flow = ref(true)
const panel=ref<'scene'|'solar'|'mhd'>('scene')
const solarOpened=ref(false)
const magnetic=ref<MagneticModel>()
const solarImage=ref<SolarImage>()
const solarPinned=ref(false),solarError=ref('')
let solarRefresh:ReturnType<typeof setInterval>|undefined
const motionRunning = ref(true)
const timeScale = ref(1)
const inspection = ref<Inspection|null>(null)
const inspectionPinned = ref(false)
const realisticScale = ref(false)
const quality = ref<'high' | 'balanced'>('balanced')
const selectedId = ref(props.selectedId || '')
const sceneTime = ref(props.time)
const elapsedHours = ref(24)
const playing = ref(false)
const rate = ref(2)
const details = ref(false)
const cameraPreset = ref<CameraPreset>('overview')
const stats = ref<RenderStats | null>(null)
let runtime: ReturnType<typeof createScene> | undefined
let clockFrame=0
let previousTick=0
let previousEmit=0
const selected = computed(() => props.events.find(e => e.activityID === selectedId.value) || null)
const analysis = computed(() => usableAnalysis(selected.value))
const visibleField=computed(()=>expert.value?field.value:guideStep.value===2)
const visibleFlow=computed(()=>expert.value?flow.value:guideStep.value>0)
const visibleGrid=computed(()=>expert.value&&grid.value)
const showImpact=computed(()=>panel.value==='scene'&&summaryVisible.value)
const explanation=computed(()=>{
  if(!inspection.value)return null
  const full=lesson(inspection.value.topic,{time:sceneTime.value,now:props.time,timeScale:selected.value?rate.value*3600:timeScale.value,event:selected.value,wind:props.wind||{},hit:inspection.value,magnetic:magnetic.value,solarImage:solarImage.value})
  if(expert.value)return full
  const simple={sun:['太阳','太阳是太阳风和太阳喷发的来源。表面影像有独立的实际观测时间。'],earth:['地球','这是我们关心的目标。图中将地球放大，方便找到位置。'],wind:['太阳风','太阳持续向外释放带电粒子。金色箭头说明流向，不表示已经发生太阳风暴。'],field:['地球周围的磁场','曲线表示磁场方向，不是实体管道。这些线来自模型计算。'],cme:['一次太阳喷发（CME）','这是有来源记录的物质与磁结构抛射，不一定朝地球传播。'],shock:['冲击波','当前没有可验证的三维冲击面，因此没有绘制碰撞动画。']}[inspection.value.topic]
  const step=({sun:0,earth:0,wind:1,field:2,cme:3,shock:3} as const)[inspection.value.topic]
  const readable=guideContent(step,{now:props.time,wind:props.wind||{},magnetic:magnetic.value,modelStatus:magneticStatus.value,event:selected.value,simulations:props.simulations||[]})
  return {...full,title:simple[0]!,explanation:simple[1]!,state:[readable.state],limits:readable.limit}
})
const tooltipStyle=computed(()=>{const width=viewport.value?.clientWidth||400;const height=viewport.value?.clientHeight||540;return {left:Math.max(12,Math.min(width-352,inspection.value?.x||16))+'px',top:Math.max(125,Math.min(height-390,inspection.value?.y||150))+'px'}})
function inspect(value:Inspection|null,pin=false){if(inspectionPinned.value&&!pin)return;inspection.value=value;if(pin)inspectionPinned.value=true}
function inspectTopic(topic:Topic,pin=false){inspect({topic,x:16,y:155},pin)}
function dismiss(){inspectionPinned.value=false;inspection.value=null}
function live(){selectedId.value='';emit('select','');playing.value=false;sceneTime.value=props.time;timeScale.value=1;motionRunning.value=true}
const utc = (seconds: number) => new Date(seconds * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC'
const eventOptions = computed(() => [...props.events].sort((a,b) => b.startTime.localeCompare(a.startTime)))
const earthEvent = computed(() => eventOptions.value.find(event => !!earthImpact(event,props.simulations||[],props.time).arrival))
function pickEarthEvent(){if(!earthEvent.value)return;selectedId.value=earthEvent.value.activityID;chooseEvent()}
const status = computed(() => selected.value?'事件时间查阅 · 三维等离子体形态未重建':timeScale.value===1?'天文参考 · 实时速率':'天文参考 · 教学加速，不是未来预报')

function setHours() {
  if (!selected.value) return
  const start = Date.parse(selected.value.startTime) / 1000
  if (!Number.isFinite(start)) return
  sceneTime.value = start + elapsedHours.value * 3600
  emit('time', sceneTime.value)
}
function chooseEvent() {
  playing.value = false
  elapsedHours.value = 24
  setHours()
  if (!selected.value) sceneTime.value = props.time
  if(selected.value&&!expert.value)guideStep.value=3
  emit('select', selectedId.value)
}
function guideView(step:GuideStep){
  guideStep.value=step;dismiss();playing.value=false
  if(step<3)live()
  preset(step===2?'field':'overview')
}
function beginGuide(){experience.value='guided';dismiss();preset(guideStep.value===2?'field':'overview')}
function selectGuided(id:string){selectedId.value=id;chooseEvent()}
function preset(name: CameraPreset) {
  cameraPreset.value = name
  runtime?.preset(name)
}
function observation(image:SolarImage){if(image.channel==='c2'||image.channel==='c3')return;solarImage.value=image;solarPinned.value=true}
async function refreshSolar(){if(solarPinned.value)return;try{const image=await get<SolarImage>('/sw/solar/image',{channel:'304',latest:true},{timeout:60000});if(!solarPinned.value){solarImage.value=image;solarError.value=''}}catch(e){if(!solarPinned.value)solarError.value=String(e)}}
function model(value:MagneticModel){magnetic.value=value;if(cameraPreset.value==='field'||cameraPreset.value==='earth')runtime?.preset(cameraPreset.value)}
async function expand() {
  if (expanded.value) { await collapse(); return }
  expanded.value = true
  await nextTick()
  try { await root.value?.requestFullscreen() } catch { /* CSS theater remains available. */ }
}
async function collapse() {
  if (document.fullscreenElement === root.value) await document.exitFullscreen()
  expanded.value = false
}
function fullscreenChanged() { if (!document.fullscreenElement) expanded.value = false }
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') { expanded.value = false; playing.value = false }
  if (!expanded.value || /INPUT|SELECT|TEXTAREA/.test((event.target as HTMLElement)?.tagName || '')) return
  if (event.code === 'Space'&&expert.value) { event.preventDefault(); if (analysis.value) playing.value = !playing.value }
}
watch(() => props.time, time => { if (!selected.value && motionRunning.value && timeScale.value===1) sceneTime.value = time })
watch(() => props.selectedId, id => { if (id !== selectedId.value) { selectedId.value = id || ''; chooseEvent() } })
watch(() => props.events, () => {
  if (selectedId.value && !props.events.some(e => e.activityID === selectedId.value)) { selectedId.value = ''; playing.value = false; sceneTime.value = props.time }
})
watch([experience,guideStep],()=>{if(expert.value||guideStep.value===2)magneticActivated.value=true},{immediate:true})
onMounted(() => {
  try {
    if (!viewport.value) return
    runtime = createScene(viewport.value, () => ({time: sceneTime.value, event:expert.value||guideStep.value===3?selected.value:null, grid: visibleGrid.value, realisticScale: expert.value&&realisticScale.value, quality: quality.value, field: visibleField.value,magnetic:magnetic.value,flow:visibleFlow.value,wind:props.wind,solarImage:solarImage.value,trainingActive:props.trainingActive,visible:panel.value==='scene',guided:!expert.value}), value => {stats.value = value; ready.value = true}, message => {error.value = message; playing.value = false}, inspect)
  } catch (reason) {
    error.value = '三维渲染未能启动：' + String(reason) + '。可继续使用官方二维模型图。'
  }
  document.addEventListener('fullscreenchange', fullscreenChanged)
  window.addEventListener('keydown', key)
  const tick=(now:number)=>{
    clockFrame=requestAnimationFrame(tick)
    const dt=previousTick?Math.min(.1,(now-previousTick)/1000):0
    previousTick=now
    if(document.hidden)return
    if(selected.value&&playing.value){elapsedHours.value=Math.min(120,elapsedHours.value+rate.value*dt);sceneTime.value=Date.parse(selected.value.startTime)/1000+elapsedHours.value*3600;if(elapsedHours.value>=120)playing.value=false}
    else if(!selected.value&&motionRunning.value)sceneTime.value+=dt*timeScale.value
    if(now-previousEmit>1000){emit('time',sceneTime.value);previousEmit=now}
  }
  clockFrame=requestAnimationFrame(tick)
  void refreshSolar();solarRefresh=setInterval(()=>void refreshSolar(),900000)
})
onBeforeUnmount(() => {
  cancelAnimationFrame(clockFrame)
  clearInterval(solarRefresh)
  document.removeEventListener('fullscreenchange', fullscreenChanged)
  window.removeEventListener('keydown', key)
  if (document.fullscreenElement === root.value) void document.exitFullscreen()
  runtime?.dispose()
})
</script>

<template>
  <div ref="root" class="solar-studio" :class="{expanded,'has-impact':showImpact,guided:!expert}" :data-experience="experience" :data-guide-step="guideStep" :data-field-visible="visibleField" :data-scene-time="sceneTime" :data-field-model="stats?.fieldModel" :data-solar-observed="stats?.solarObserved" :data-wind-tracers="stats?.windTracers" @mouseleave="!inspectionPinned&&dismiss()" :data-render-state="error?'error':ready?'ready':'loading'">
    <EventImpactPanel v-if="showImpact" :event="selected" :events="eventOptions" :simulations="simulations||[]" :now="time" :wind="wind||{}" @select="selectGuided" @hide="summaryVisible=false"/>
    <div class="scene-stage">
    <div ref="viewport" class="space-viewport" />
    <div class="studio-top">
      <div class="studio-brand"><span class="status-dot"/><div><b>{{expert?'日地空间 · 三维视景':'日地空间'}}</b><small>{{expert?'HELIOPHYSICS / INTERACTIVE 3D':'真实观测 / 方向示意 / 模型快照'}}</small></div></div>
      <div class="top-actions"><span class="render-chip">{{trainingActive?'训练优先 · 节能':'WEBGL'}} · {{stats?.fps||'—'}} FPS</span><button class="expand-button" @click="expand">{{expanded?'退出全屏':'进入三维全屏'}} <span>⛶</span></button></div>
    </div>
    <nav class="studio-tabs" aria-label="三维工作室内容"><button :class="{active:panel==='scene'}" @click="panel='scene'">太阳风怎样到地球</button><button :class="{active:panel==='solar'}" @click="panel='solar';solarOpened=true;dismiss()">太阳活动观测</button><button :class="{active:panel==='mhd'}" @click="panel='mhd';dismiss()">磁层变化回放</button></nav>
    <SolarObservation v-if="solarOpened" v-show="panel==='solar'" :active="panel==='solar'" @image="observation"/>
    <MagnetosphereMovie v-if="panel==='mhd'"/>
    <div v-show="panel==='scene'" class="scene-layer">
    <div class="experience-switch"><button :class="{active:!expert}" @click="beginGuide">简洁浏览</button><button :class="{active:expert}" @click="experience='expert';controlsVisible=true;dismiss()">专业浏览</button><button v-if="!expert" :aria-pressed="guideVisible" @click="guideVisible=!guideVisible">{{guideVisible?'收起图解':'图解说明'}}</button><button v-if="!summaryVisible" @click="summaryVisible=true">显示影响摘要</button><details class="display-settings"><summary>显示设置</summary><div><label><input v-model="summaryVisible" type="checkbox"/>影响摘要</label><label v-if="!expert"><input v-model="guideVisible" type="checkbox"/>四步图解说明</label><label><input v-model="sourcesVisible" type="checkbox"/>观测时间与来源</label><label v-if="expert"><input v-model="controlsVisible" type="checkbox"/>专业控制面板</label><button @click="preset(guideStep===2?'field':'overview')">重新对准画面</button></div></details></div>
    <MagneticControls v-if="magneticActivated" ref="magneticControls" v-show="expert&&controlsVisible" @model="model" @status="value=>magneticStatus=value"/>
    <div v-if="!expert&&sourcesVisible" class="guide-source-note"><span>天体位置：计算 · 尺寸放大显示</span><span v-if="solarImage">太阳影像：实测 {{solarImage.observed_at.replace('T',' ').slice(0,16)}} UTC · 灰蓝网格为未观测的一面</span><span v-else>{{solarError?'太阳观测未取得，表面暂为展示材质':'正在载入太阳观测，表面暂为展示材质'}}</span></div>
    <div v-if="expert&&sourcesVisible" class="scene-caption"><span>{{status}}</span><time>{{utc(sceneTime)}}</time><small v-if="field">{{magnetic?'磁层：T96 + IGRF-14 · X −50～20 Rₑ；Shue独立边界估计':'IGRF-14一阶内源场 · 等待全局磁层计算'}}</small><small v-if="magnetic">磁层快照 {{utc(magnetic.parameters.time)}} · {{magnetic.driver_mode==='manual'?'手动实验':'观测驱动经验估计'}}</small><small v-if="solarImage">太阳观测 {{solarImage.channel}} · {{solarImage.observed_at}} · 可见半球观测投影 · 背面未知</small><small v-else>{{solarError?'AIA载入失败，请到太阳活动观测重试':'正在载入真实AIA观测；暂用静态展示材质'}}</small><small v-if="flow">金色：背景太阳风向各方向外流 · 箭头示方向，不用线条数量表示密度</small><small>影像、磁层快照与天文时钟分别标时；到达估计查看地球影响信息卡。</small></div>
    <button v-if="stats?.sun.visible" class="body-label sun-label" aria-label="了解太阳" @pointerenter="inspectTopic('sun')" @focus="inspectTopic('sun')" @click="inspectTopic('sun',true)" :style="{left:stats.sun.x+'px',top:stats.sun.y+'px'}"><i/><b>太阳</b><small>{{expert?'SUN · 了解自转':'太阳风的来源'}}</small></button>
    <button v-if="stats?.earth.visible" class="body-label earth-label" aria-label="了解地球" @pointerenter="inspectTopic('earth')" @focus="inspectTopic('earth')" @click="inspectTopic('earth',true)" :style="{left:stats.earth.x+'px',top:stats.earth.y+'px'}"><i/><b>地球</b><small>{{expert?'EARTH · 了解公转':'我们所在的位置'}}</small></button>
    <template v-if="!expert&&guideStep===2&&stats?.fieldSides"><span v-if="stats.fieldSides.sunward.visible" class="field-side-label" :style="{left:stats.fieldSides.sunward.x+'px',top:stats.fieldSides.sunward.y+'px'}">迎日侧<small>朝向太阳</small></span><span v-if="stats.fieldSides.tailward.visible" class="field-side-label tail-side" :style="{left:stats.fieldSides.tailward.x+'px',top:stats.fieldSides.tailward.y+'px'}">磁尾侧<small>背向太阳</small></span></template>
    <template v-if="visibleGrid"><span v-for="orbit in stats?.orbits.filter(o=>o.position.visible)" :key="orbit.au" class="orbit-label" :style="{left:orbit.position.x+'px',top:(orbit.position.y+22)+'px'}">{{orbit.au.toFixed(2)}} AU</span></template>
    <aside v-if="expert&&controlsVisible" class="scene-tools">
      <span>观测视角</span>
      <button v-for="view in ([['overview','日地全景'],['earth','地球近景'],['field','全局磁层'],['sun','太阳近景'],['top','黄道俯视']] as const)" :key="view[0]" :class="{active:cameraPreset===view[0]}" @click="preset(view[0])">{{view[1]}}</button>
      <div class="tool-divider"/>
      <label @pointerenter="inspectTopic('field')"><input v-model="field" type="checkbox"/>磁层场与边界</label><label @pointerenter="inspectTopic('wind')"><input v-model="flow" type="checkbox"/>太阳风方向示踪</label><label><input v-model="grid" type="checkbox"/>轨道参照</label>
      <label><input v-model="realisticScale" type="checkbox"/>真实尺寸比例</label>
      <select v-model="quality" aria-label="三维画质"><option value="high">高画质 · 辉光</option><option value="balanced">均衡画质</option></select>
      <button @click="details=!details">{{details?'收起':'显示'}}数据依据</button>
    </aside>
    <aside v-if="details" class="scene-evidence"><b>数据与渲染依据</b><p>天体位置：Astronomy Engine，J2000黄道坐标，按选中时间计算。</p><p>太阳观测按可见半球投影，日缘外保留原观测平面；背面显示未知网格，不能称为三维日珥重建；地球云层与夜景仍为静态材质。</p><p>默认放大天体尺寸以便观察，可切换真实比例。地球姿态使用恒星时、岁差章动变换；太阳按NASA纬度角速度参考律自转。加速时间不是预报。</p><p>紫色箭头仅表示DONKI的HEEQ来源方向，经太阳极轴/地球参考轴变换，不是CME三维形状或冲击面；没有模型网格就不绘制虚构冲击波。磁层接入T96 + IGRF-14数值追踪和Shue独立边界估计，金色示踪仅表示参考太阳风方向；MHD页展示NOAA模型的压缩与磁尾。</p><p>精确模型密度与速度分布请切换“官方二维模型”。</p><a href="https://www.solarsystemscope.com/textures/" target="_blank" rel="noreferrer">纹理：Solar System Scope · CC BY 4.0 ↗</a></aside>
    <aside v-if="explanation" class="lesson-card" :class="{pinned:inspectionPinned}" :style="tooltipStyle" :role="inspectionPinned?'dialog':'tooltip'" :aria-label="explanation.title"><header><b>{{explanation.title}}</b><button aria-label="关闭讲解" @click="dismiss">×</button></header><p>{{explanation.explanation}}</p><h4>当前显示的情况</h4><ul><li v-for="line in explanation.state" :key="line">{{line}}</li></ul><p class="lesson-limit">{{explanation.limits}}</p><a :href="explanation.url" target="_blank" rel="noreferrer">依据：{{explanation.source}} ↗</a><small v-if="!inspectionPinned">点击对象或讲解按钮可固定阅读，并打开来源。</small></aside>
    <div v-if="error" class="scene-error" role="alert">{{error}}</div>
    <div v-else-if="!ready" class="scene-loading">正在初始化三维渲染器…</div>
    <div v-if="expanded&&expert" class="gesture-hint">左键旋转 · 滚轮缩放 · 右键平移 · 空格播放/暂停 · Esc退出</div>
    <SceneGuide v-if="!expert&&guideVisible" :step="guideStep" :now="time" :wind="wind||{}" :magnetic="magnetic" :model-status="magneticStatus" :event="selected" :events="eventOptions" :simulations="simulations||[]" @step="guideView" @select="selectGuided" @retry="magneticControls?.current()" @movie="panel='mhd';dismiss()"/>
    <div v-if="expert&&controlsVisible" class="studio-bottom">
      <div class="education-strip"><span>鼠标悬停 / 点击了解：</span><button v-for="topic in ([['sun','太阳'],['earth','地球'],['field','磁场'],['wind','太阳风'],['cme','CME'],['shock','冲击波']] as const)" :key="topic[0]" :data-lesson="topic[0]" @pointerenter="inspectTopic(topic[0])" @focus="inspectTopic(topic[0])" @click="inspectTopic(topic[0],true)">{{topic[1]}}</button></div><div v-if="!selected" class="motion-controls"><button @click="motionRunning=!motionRunning">{{motionRunning?'暂停天体运动':'继续天体运动'}}</button><label>时间倍率 <select v-model.number="timeScale" aria-label="天体时间倍率"><option :value="1">1× 实时</option><option :value="3600">1小时 / 秒</option><option :value="86400">1天 / 秒</option><option :value="259200">3天 / 秒</option></select></label><button @click="live">返回当前时间</button><span>自转、公转共用时间轴</span></div><div class="event-picker"><label>事件 <select v-model="selectedId" aria-label="三维CME事件" @change="chooseEvent"><option value="">背景日地场景 · 运动可加速</option><option v-for="event in eventOptions" :key="event.activityID" :value="event.activityID">{{event.activityID}}{{usableAnalysis(event)?'':' · 参数不足'}}</option></select></label><button v-if="earthEvent" @click="pickEarthEvent">查看有地球到达估计的事件</button><span v-if="analysis" class="parameter-tag">{{analysis.speed}} km/s · 经度 {{analysis.longitude}}° · 纬度 {{analysis.latitude}}° · 半角 {{analysis.halfAngle}}°</span></div>
      <div v-if="selected" class="scene-timeline"><button :disabled="!analysis" @click="playing=!playing">{{playing?'暂停时间轴':'推进时间轴'}}</button><label class="scene-range"><input v-model.number="elapsedHours" type="range" min="0" max="120" step=".25" aria-label="三维事件回放时间" @input="setHours"/><span>事件后 {{elapsedHours.toFixed(1)}} h / 120 h</span></label><select v-model.number="rate" aria-label="三维回放速率"><option :value=".5">0.5小时/秒</option><option :value="2">2小时/秒</option><option :value="6">6小时/秒</option></select><span class="distance">只改变查看时刻，未播放CME三维撞击过程</span></div>
      <div class="scene-legend"><span><i class="blue"/>天体位置计算</span><span><i class="orange"/>来源参数查阅</span><span class="scale-note">{{realisticScale?'真实天体尺寸 · 行星可能小于一个像素':'增强显示 · 天体尺寸已放大'}} · 轨道参照以AU标记</span></div>
    </div>
    </div>
    </div>
    
  </div>
</template>

<style scoped>
.studio-tabs{position:absolute;top:53px;left:18px;right:18px;z-index:8;display:flex;gap:6px;flex-wrap:wrap}.solar-studio .studio-tabs button{padding:4px 8px;font-size:10px}.scene-layer{position:absolute;inset:0;pointer-events:none}.scene-layer>*{pointer-events:auto}.scene-layer .scene-caption,.scene-layer .orbit-label,.scene-layer .gesture-hint{pointer-events:none}.solar-studio .scene-caption{top:88px;max-width:calc(100% - 170px)}.scene-caption small{font-size:9px;line-height:1.4}.solar-studio .scene-tools{top:95px}.expanded .studio-tabs{top:70px;left:32px}.expanded .solar-observation,.expanded .mhd-movie{top:110px}@media(max-width:600px){.studio-tabs{top:47px;left:8px;right:8px;gap:3px}.solar-studio .studio-tabs button{font-size:8px;padding:4px}.solar-studio .scene-caption{top:90px;max-width:calc(100% - 110px)}.scene-caption small{font-size:8px}.solar-studio .scene-tools{top:130px}.expanded .studio-tabs{top:50px;left:10px}}

.solar-studio{position:relative;height:480px;width:100%;overflow:hidden;background:#02050a;color:#d4e0e7;font:12px 'Segoe UI','Microsoft YaHei',sans-serif;border-top:1px solid #263b49;isolation:isolate}.space-viewport{position:absolute;inset:0;touch-action:none}.space-viewport :deep(canvas){display:block;width:100%;height:100%}.solar-studio.expanded,.solar-studio:fullscreen{position:fixed;inset:0;width:100vw;height:100dvh;z-index:1000;background:#02050a}.studio-top{position:absolute;left:20px;right:20px;top:17px;display:flex;align-items:center;justify-content:space-between;pointer-events:none;gap:12px}.studio-brand{display:flex;align-items:center;gap:10px;text-shadow:0 2px 12px #000}.studio-brand b{font-size:15px;letter-spacing:1px}.studio-brand small{display:block;color:#748796;font-size:8px;letter-spacing:2px;margin-top:5px}.status-dot{height:6px;width:6px;background:#a4d8e5;border-radius:50%;box-shadow:0 0 9px #a4d8e555}.top-actions{display:flex;gap:12px;align-items:center;pointer-events:auto}.render-chip{font:10px Consolas,monospace;color:#7d929f}.solar-studio button,.solar-studio select{color:#c6d6df;background:#10202cbd;border:1px solid #3b536355;backdrop-filter:blur(12px);border-radius:4px;padding:7px 10px;cursor:pointer;font:11px inherit;outline-offset:3px}.solar-studio button:hover,.solar-studio button.active{border-color:#6a9cab;background:#173545bd;color:white}.solar-studio button:disabled{opacity:.35;cursor:default}.solar-studio button.expand-button{padding:9px 13px;letter-spacing:.5px;color:#d5edf3}.expand-button span{margin-left:8px}.scene-caption{position:absolute;left:20px;top:80px;display:flex;flex-direction:column;gap:7px;pointer-events:none;color:#8098a8}.scene-caption>span{font-size:10px;letter-spacing:.5px}.scene-caption time{font:12px Consolas,monospace;color:#c0d0d9}.scene-tools{max-height:calc(100% - 260px);overflow:auto;position:absolute;right:18px;top:85px;width:108px;display:flex;flex-direction:column;gap:5px;background:#07101a88;border:1px solid #23374466;border-radius:6px;padding:10px;backdrop-filter:blur(10px)}.scene-tools>span{font-size:9px;color:#6e8594;letter-spacing:2px;margin-bottom:5px}.scene-tools button{text-align:center;font-size:10px;padding:6px 2px}.scene-tools label{font-size:9px;color:#9fb0ba;display:flex;gap:3px;align-items:center}.scene-tools input{accent-color:#88c9d8}.scene-tools select{font-size:9px;width:100%;padding:5px 2px}.tool-divider{height:1px;background:#263d4c;margin:5px 0}.solar-studio button.body-label{position:absolute;pointer-events:auto;background:transparent;border:0;backdrop-filter:none;padding:4px;cursor:help;transform:translate(-50%,-100%);display:flex;flex-direction:column;align-items:center;text-shadow:0 2px 8px #000;white-space:nowrap}.body-label>b{font-size:12px;letter-spacing:2px;font-weight:500}.body-label>small{font:8px Consolas,monospace;letter-spacing:1px;margin-top:4px;color:#95a6b4}.body-label i{width:3px;height:3px;border-radius:50%;background:#91c9df;margin-bottom:6px}.orbit-label{position:absolute;color:#617f93;pointer-events:none;font:9px Consolas,monospace;transform:translateX(-50%);opacity:.8}.sun-label i{background:#ffb957}.scene-evidence{position:absolute;top:85px;right:150px;max-width:265px;width:33%;max-height:50%;overflow:auto;background:#08141deb;border:1px solid #355064;border-radius:6px;padding:16px;backdrop-filter:blur(20px)}.scene-evidence>b{font-size:12px;color:#cce0e9}.scene-evidence p{font-size:10px;line-height:1.7;color:#91a6b5;margin:8px 0}.scene-evidence a{color:#9cc6d6;font-size:10px}.studio-bottom{position:absolute;bottom:0;left:0;right:0;padding:12px 18px 10px;background:linear-gradient(transparent,#050d16ed 28%,#060d16);border-top:1px solid #1b324333;backdrop-filter:blur(3px)}.event-picker{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.event-picker label{display:flex;align-items:center;gap:10px;color:#8195a4;font-size:10px}.event-picker select{max-width:330px;font-size:10px}.parameter-tag{font:9px Consolas,monospace;color:#d3b48b}.scene-timeline{display:flex;align-items:center;gap:12px;margin-top:12px}.scene-timeline>button{min-width:80px}.scene-range{flex:1;min-width:70px}.scene-range input{width:100%;accent-color:#bd8d54;height:3px;cursor:pointer}.scene-range>span{display:block;color:#7b8b99;font:9px Consolas,monospace;margin-top:7px}.scene-timeline select{font-size:10px}.distance{font:10px Consolas,monospace;color:#d3b48b;white-space:nowrap}.scene-legend{display:flex;gap:18px;align-items:center;margin-top:12px;font-size:9px;color:#6b8291;flex-wrap:wrap}.scene-legend span{display:flex;align-items:center;gap:5px}.scene-legend i{width:4px;height:4px;border-radius:50%}.blue{background:#8fb6cb}.orange{background:#e2a45b}.scale-note{margin-left:auto}.lesson-card{pointer-events:none;position:absolute;z-index:10;width:328px;max-width:calc(100% - 24px);max-height:350px;overflow:auto;background:#101e2af7;border:1px solid #527389;border-radius:7px;padding:13px;box-shadow:0 10px 35px #000b;font-size:11px;color:#c9d8e2}.lesson-card.pinned{pointer-events:auto}.lesson-card small{font-size:10px;color:#8fa5b6}.lesson-card header{display:flex;justify-content:space-between;align-items:center;gap:10px}.lesson-card header b{font-size:13px}.lesson-card h4{font-size:11px;color:#8ed9de;margin:12px 0 5px}.lesson-card p,.lesson-card li{font-size:11px;line-height:1.7;color:#b7c9d5;margin:6px 0}.lesson-card ul{padding-left:17px;margin:0}.lesson-card .lesson-limit{border-top:1px solid #345164;padding-top:9px;color:#dfbd81}.lesson-card a{color:#8ac6dc;font-size:10px;display:block;margin:7px 0}.education-strip,.motion-controls{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:9px;font-size:10px;color:#8ca5b8}.education-strip button{padding:3px 6px;font-size:10px}.motion-controls label{display:flex;align-items:center;gap:5px}.motion-controls select{font-size:10px}.gesture-hint{position:absolute;left:50%;bottom:155px;transform:translateX(-50%);font-size:10px;letter-spacing:1px;color:#647c8d;pointer-events:none;white-space:nowrap}.scene-error,.scene-loading{position:absolute;top:40%;left:20%;right:20%;padding:20px;background:#09131ddf;border:1px solid #415364;border-radius:5px;line-height:1.8;font-size:12px}.expanded .studio-top{left:32px;right:32px;top:27px}.expanded .studio-brand b{font-size:20px}.expanded .studio-brand small{font-size:10px}.expanded .scene-caption{left:32px;top:110px}.expanded .scene-caption>span{font-size:12px}.expanded .scene-caption time{font-size:16px}.expanded .scene-tools{max-height:calc(100% - 380px);top:110px;right:32px;width:132px;padding:14px;gap:8px}.expanded .scene-tools button{font-size:12px;padding:8px 3px}.expanded .scene-tools label{font-size:11px}.expanded .scene-tools select{font-size:11px}.expanded .studio-bottom{padding:22px 32px 20px}.expanded .scene-legend{font-size:11px;margin-top:18px}.expanded .event-picker select{font-size:12px;max-width:520px}.expanded .parameter-tag{font-size:12px}.expanded .body-label>b{font-size:15px}.expanded .body-label>small{font-size:10px}.expanded .scene-evidence{right:200px;top:110px;width:310px;max-width:310px}.expanded .scene-evidence p{font-size:12px}.expanded .scene-timeline{margin-top:18px}.expanded .scene-range>span{font-size:11px}.expanded .distance{font-size:13px}@media(max-width:600px){.solar-studio{height:540px}.studio-top{left:12px;right:12px;top:12px}.studio-brand b{font-size:12px}.studio-brand small{font-size:6px;letter-spacing:1px}.render-chip{display:none}.solar-studio button.expand-button{font-size:10px;padding:7px}.scene-tools{width:88px;right:10px;top:100px;padding:7px}.scene-caption{left:12px;top:70px}.scene-caption time{font-size:10px}.scene-caption>span{max-width:210px}.studio-bottom{padding:12px 10px}.event-picker select{max-width:260px}.parameter-tag{font-size:8px}.scene-legend{gap:8px;font-size:8px}.scale-note{margin-left:0}.scene-timeline{gap:6px;flex-wrap:wrap}.scene-timeline>button{min-width:65px}.distance{font-size:9px}.scene-evidence{top:100px;left:12px;right:110px;width:auto;max-width:240px;padding:10px}.expanded .studio-top{left:12px;right:12px;top:16px}.expanded .studio-brand b{font-size:12px}.expanded .studio-brand small{font-size:7px}.expanded .scene-tools{right:12px;width:95px;padding:8px}.expanded .scene-caption{left:12px;top:75px}.expanded .scene-caption time{font-size:11px}.expanded .studio-bottom{padding:15px 12px}.expanded .event-picker select{font-size:10px;max-width:270px}.expanded .scene-legend{font-size:8px}.expanded .scene-evidence{right:125px;left:12px;width:auto}.gesture-hint{display:none}}
/* Summary precedes the canvas in normal and fullscreen layouts. Details never cover Earth. */
.solar-studio{height:auto;overflow:hidden;display:flex;flex-direction:column}
.scene-stage{position:relative;height:650px;min-width:0;flex:none;overflow:hidden}
.solar-studio.expanded,.solar-studio:fullscreen{display:flex;flex-direction:column;overflow:auto}
.expanded .scene-stage{height:auto;min-height:500px;flex:1 0 500px}
.scene-stage .scene-tools{max-height:calc(100% - 330px)}
.scene-stage .studio-bottom{max-height:240px;overflow:auto}
@media(max-width:600px){.scene-stage{height:620px}.expanded .scene-stage{min-height:560px;flex-basis:560px}.scene-stage .scene-caption{gap:4px;max-width:calc(100% - 110px)}.scene-stage .studio-bottom{max-height:260px}.scene-stage .event-picker select{max-width:235px}.scene-stage .magnetic-controls{bottom:270px}.scene-stage .scene-tools{max-height:calc(100% - 405px)}}
.experience-switch{position:absolute;top:95px;left:18px;right:18px;display:flex;gap:6px;align-items:center;z-index:6}.solar-studio .experience-switch button{font-size:11px;padding:5px 9px}.display-settings{position:relative;margin-left:auto}.display-settings summary{cursor:pointer;border:1px solid #3b5363;border-radius:4px;background:#10202ce8;color:#bfd5e2;padding:5px 9px;font-size:11px}.display-settings>div{position:absolute;right:0;top:35px;min-width:175px;background:#102231f7;border:1px solid #446276;border-radius:6px;padding:12px;display:flex;flex-direction:column;gap:12px;box-shadow:0 12px 25px #0008}.display-settings label{display:flex;align-items:center;gap:7px;white-space:nowrap;color:#c5dae7;font-size:12px}.display-settings input{accent-color:#8edbdc}
.guide-source-note{position:absolute;top:139px;left:18px;right:18px;display:flex;flex-direction:column;gap:5px;font-size:10px;line-height:1.5;color:#95adbf;pointer-events:none}
.solar-studio:not(.guided) .scene-caption{top:140px}.solar-studio:not(.guided) .scene-tools{top:145px;max-height:calc(100% - 395px)}
.field-side-label{position:absolute;transform:translate(-50%,-50%);pointer-events:none;background:#152c3ddc;border:1px solid #47677b;padding:5px 8px;border-radius:4px;font-size:12px;color:#efca87;white-space:nowrap}.field-side-label small{display:block;font-size:10px;color:#a8c4d5}.tail-side{color:#9bd3e6}
.guided .body-label>b{font-size:15px}.guided .body-label>small{font:11px 'Microsoft YaHei',sans-serif;letter-spacing:0}
.guided button.body-label{background:#07131bd9;border:1px solid #345064;border-radius:5px;padding:5px 8px}
.guided .studio-brand small{font-size:9px;letter-spacing:.4px}
.expanded .experience-switch{top:108px;left:32px;right:32px}.expanded .guide-source-note{top:151px;left:32px;font-size:11px}.expanded:not(.guided) .scene-caption{top:155px}.expanded:not(.guided) .scene-tools{top:155px}
@media(max-width:600px){.experience-switch{top:92px;left:10px;right:10px;gap:4px}.solar-studio .experience-switch button{font-size:10px;padding:5px 6px}.guide-source-note{top:132px;left:10px;font-size:9px}.expanded .experience-switch{top:92px;left:10px;right:10px}.expanded .guide-source-note{top:132px;left:10px;font-size:9px}.solar-studio:not(.guided) .scene-caption{top:137px}.solar-studio:not(.guided) .scene-tools{top:180px;max-height:calc(100% - 445px)}.guided .body-label>b{font-size:13px}.guided .body-label>small{font-size:9px}.field-side-label{font-size:10px;padding:4px}}
</style>
