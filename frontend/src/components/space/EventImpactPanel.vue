<script setup lang="ts">
import { computed } from 'vue'
import { earthImpact, upstreamIntake, type PropagationModel, type SolarEvent } from './impact'
import { usableAnalysis } from './sceneMath'
import type { WindSample } from './physics'
const props = defineProps<{ event: SolarEvent | null; events: SolarEvent[]; simulations: PropagationModel[]; now: number; wind: WindSample }>()
const emit = defineEmits<{ select: [id: string]; hide: [] }>()
const impact = computed(() => earthImpact(props.event, props.simulations, props.now))
const intake = computed(() => upstreamIntake(props.wind, props.now))
const analysis = computed(() => usableAnalysis(props.event))
const format = (value?: string | number | null) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '来源未提供'
const eventName = (event: SolarEvent) => `${format(event.startTime)} 喷发${event.sourceLocation ? ' · ' + event.sourceLocation : ''}`
const headline = computed(() => {
  if (impact.value.state === 'unselected') return '尚未选择事件'
  if (impact.value.state === 'awaiting_confirmation') return '待观测确认'
  if (impact.value.state === 'earth_arrival') return impact.value.title.includes('擦过') ? '模型预计擦过地球' : '模型预计到达地球'
  return '尚不能确定'
})
const note = computed(() => {
  if (impact.value.state === 'unselected') return '在上方选择一次太阳喷发'
  if (impact.value.state === 'awaiting_confirmation') return '预计时刻已过，不代表已确认到达'
  if (impact.value.state === 'earth_arrival') return 'NASA 研究模拟 · 不是已确认影响'
  return '资料不足，不代表没有影响'
})
const remaining = computed(() => {
  if (!impact.value.arrival) return props.event ? '来源未提供地球到达时间' : '选择事件后查看'
  const hours = (Date.parse(impact.value.arrival) / 1000 - props.now) / 3600
  return hours <= 0 ? '预计时刻已过 · 待观测核对' : `距当前时刻约 ${hours.toFixed(1)} 小时`
})
</script>
<template>
  <section class="impact-brief" aria-label="地球影响摘要">
    <header class="impact-toolbar">
      <label>喷发事件 <select :value="event?.activityID || ''" aria-label="影响摘要的喷发事件" @change="emit('select', ($event.target as HTMLSelectElement).value)"><option value="">选择一次喷发 · 北京时间</option><option v-for="item in events" :key="item.activityID" :value="item.activityID">{{ eventName(item) }}</option></select></label>
      <button class="hide-impact" @click="emit('hide')">收起摘要</button>
    </header>
    <div class="impact-overview">
      <article class="impact-domains">
        <small>可能影响的领域 <span>科普说明</span></small>
        <div class="domain-tags"><b>卫星导航</b><b>短波通信</b><b>卫星运行</b></div>
        <p>强地磁扰动可能影响这些领域；本次是否发生尚需核验。</p>
        <a href="https://www.spaceweather.gov/noaa-scales-explanation" target="_blank" rel="noreferrer">NOAA 影响等级与适用范围 ↗</a>
      </article>
      <article class="impact-verdict" :data-impact-state="impact.state">
        <small>是否波及地球</small><strong>{{ headline }}</strong><p>{{ note }}</p>
      </article>
      <article class="impact-arrival">
        <small>预计到达 · 北京时间</small><strong>{{ impact.arrival ? format(impact.arrival) : '暂无估计' }}</strong><p>{{ remaining }}</p>
      </article>
    </div>
    <div class="impact-details">
      <details class="event-evidence"><summary>事件依据与不确定性</summary><div class="detail-content">
        <p v-if="event">事件：{{ event.activityID }} · 开始于 {{ format(event.startTime) }}（北京时间）</p>
        <p>{{ impact.title }}。{{ impact.detail }}</p>
        <p>命中地球的概率：来源未给出量化概率。持续多久：没有已核验的作用时长。</p>
        <p v-if="analysis">来源拟合方向：经度 {{ analysis.longitude }}° / 纬度 {{ analysis.latitude }}°，速度 {{ analysis.speed }} km/s。紫色箭头表示此方向，不表示命中概率。</p>
        <p v-if="impact.model">最新模型完成：{{ format(impact.model.modelCompletionTime) }} · 明确关联 {{ impact.models.length }} 个运行。</p>
        <a v-if="impact.model?.link" :href="impact.model.link" target="_blank" rel="noreferrer">查看对应模型与修订 ↗</a>
        <p>上方领域是一般影响说明，不是本事件的预警结论。具体强度、地区和时段需结合 NOAA 通报与近地观测；耀斑无线电影响和 CME 后续影响不能混用。</p>
      </div></details>
      <details class="wind-evidence"><summary>上游实测与图像解读</summary><div class="detail-content">
        <b>{{ intake.valid ? `质子密度约 ${wind.density?.toPrecision(3)} / cm³` : '当前上游测量缺失、过期或不匹配' }}</b>
        <p>速度：{{ intake.valid ? `${wind.speed?.toPrecision(3)} km/s` : '—' }} · 仅质子动压：{{ intake.valid ? `${intake.pressureNpa!.toFixed(2)} nPa` : '—' }}</p>
        <p>迎风截面通量：{{ intake.valid ? `${intake.fluxPerSquareMetreSecond!.toExponential(2)} 质子 / m² / 秒` : '—' }}。NOAA L1 单点采样 {{ format(wind.observedAt) }}（北京时间）。</p>
        <p>通量按 n×v 换算，不是落到地表的粒子数，也不是整个迎风面的密度地图。金色线条表示向外吹的方向，数量不代表实测密度。</p>
      </div></details>
    </div>
  </section>
</template>
<style scoped>
.impact-brief{flex:none;background:#101f2b;color:#d9e8ee;border-bottom:1px solid #345369;padding:12px 16px;font:12px/1.6 'Microsoft YaHei',sans-serif}
.impact-toolbar{display:flex;align-items:center;gap:12px;margin-bottom:10px}.impact-toolbar label{display:flex;align-items:center;gap:9px;min-width:0;flex:1;color:#9cb8cb}.impact-toolbar select{min-width:0;max-width:390px;width:100%;background:#091722;border:1px solid #395469;border-radius:5px;padding:6px 9px;color:#dfebf3;font:12px 'Microsoft YaHei',sans-serif}.impact-toolbar .hide-impact{flex:none;background:transparent;border:0;color:#a8c4d5;cursor:pointer;font:11px 'Microsoft YaHei',sans-serif;padding:5px}
.impact-overview{display:grid;grid-template-columns:1.35fr 1fr 1fr;gap:16px}.impact-overview article{min-width:0}.impact-overview article+article{border-left:1px solid #2d4556;padding-left:16px}.impact-overview small{display:block;color:#9fb7c8;font-size:11px}.impact-domains small{color:#a5e1df}.impact-domains small span{margin-left:6px;font-size:10px;color:#99b2c4}.domain-tags{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}.domain-tags b{font-size:13px;font-weight:500;color:#d1f5ef;background:#1b3a42;border-radius:4px;padding:2px 7px}.impact-overview strong{display:block;font-size:19px;line-height:1.4;font-weight:600;color:#e2c993;margin:7px 0 5px}.impact-arrival strong{color:#e3edf5;font-variant-numeric:tabular-nums}.impact-overview p,.detail-content p{font-size:11px;line-height:1.6;color:#a7bdce;margin:4px 0}.impact-brief a{color:#89dbd6;text-decoration:none;font-size:11px}.impact-brief a:hover{text-decoration:underline}
.impact-details{display:flex;flex-wrap:wrap;gap:4px 22px;border-top:1px solid #29404f;margin-top:10px;padding-top:7px}.impact-details details{min-width:0}.impact-details details[open]{flex-basis:100%}.impact-details summary{cursor:pointer;color:#99b3c6;font-size:11px;list-style:revert}.detail-content{padding:6px 2px;max-height:180px;overflow:auto;overflow-wrap:anywhere}.detail-content b{font-size:12px;color:#d1e4eb}
@media(max-width:760px){.impact-brief{padding:10px 12px}.impact-overview{grid-template-columns:1fr 1fr;gap:10px 12px}.impact-domains{grid-column:1/-1;border-bottom:1px solid #29404f;padding-bottom:8px}.impact-overview .impact-verdict{border-left:0;padding-left:0}.impact-overview .impact-arrival{padding-left:12px}.impact-overview strong{font-size:17px;margin:4px 0}.domain-tags{margin:4px 0}.impact-toolbar{gap:5px}.impact-toolbar label{font-size:11px;gap:5px}.impact-toolbar select{font-size:11px;padding:5px;flex:1}.detail-content{max-height:160px}}
</style>
