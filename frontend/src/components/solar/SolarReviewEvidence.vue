<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { get, getBlob } from '@/api/client'

interface Observation {
  channel: string
  observed_utc: string | null
  url: string | null
}

interface Neighbor {
  offset_hours: number
  nominal_utc: string
  state: string
  channels: Observation[]
}

interface Candidate {
  id: string
  observed_utc: string
  ratios: Record<'193' | '211', number>
  dominant_polarity: string
  dominant_share: number
  significant_share: number
  neighbors: Neighbor[]
}

interface Report {
  available: boolean
  status?: string
  asset_id?: string
  images?: number
  remaining_review?: number
  existing_match_count?: number
  existing_gate_pass_count?: number
  cases?: Candidate[]
}

const report = ref<Report>()
const image = ref('')
const loading = ref(false)
const error = ref('')
let pending: AbortController | undefined

const stateLabels: Record<string, string> = {
  darkness_persists: '两通道均支持',
  alignment_uncertain: '对齐未通过',
  darkness_does_not_persist_at_cutoffs: '暗区证据未延续',
  source_missing: '观测资料缺失',
  channels_not_coincident: '通道时间或坐标不一致',
  insufficient_texture: '周边纹理不足',
  insufficient_common_support: '可比较范围不足',
  off_grid: '候选移出可比范围',
}

function percent(value: number) {
  return (value * 100).toFixed(1) + '%'
}

function utc(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(new Date(value)) + ' UTC'
}

async function load() {
  if (loading.value) return
  const controller = new AbortController()
  pending = controller
  loading.value = true
  error.value = ''
  try {
    const value = await get<Report>('/solar/review/evidence', {}, { signal: controller.signal })
    report.value = value
    if (value.available && value.asset_id && !image.value) {
      const blob = await getBlob('/solar/review/asset', { id: value.asset_id })
      if (!controller.signal.aborted) image.value = URL.createObjectURL(blob)
    }
  } catch (cause) {
    if (!controller.signal.aborted) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (pending === controller) {
      pending = undefined
      loading.value = false
    }
  }
}

function onToggle(event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open && !report.value && !loading.value) void load()
}

onBeforeUnmount(() => {
  pending?.abort()
  if (image.value) URL.revokeObjectURL(image.value)
})
</script>

<template>
  <details class="review-panel" @toggle="onToggle">
    <summary>查看冕洞多图复核案例</summary>
    <p v-if="loading" role="status">正在读取本地观测证据…</p>
    <div v-if="error" class="feedback" role="alert">
      <p>{{ error }}</p>
      <button type="button" @click="load">重试读取</button>
    </div>
    <div v-else-if="report && !report.available" class="feedback">
      <p>{{ report.status }}</p>
      <button type="button" @click="load">刷新案例</button>
    </div>
    <div v-if="report?.available" class="review-content">
      <p>{{ report.images }} 张历史观测中，{{ report.cases?.length }} 处列为「高度疑似」，{{ report.remaining_review }} 处待核验。已有的 {{ report.existing_match_count }} 个合格匹配里，只有 {{ report.existing_gate_pass_count }} 个满足这组额外证据条件，因此常规识别范围继续保留。</p>
      <div class="review-cases">
        <article v-for="candidate in report.cases" :key="candidate.id" class="review-case">
          <h3>高度疑似冕洞</h3>
          <p class="observed">{{ utc(candidate.observed_utc) }} · 待独立核验</p>
          <p>区域亮度相当于附近背景的 {{ percent(candidate.ratios['193']) }}（193 Å）和 {{ percent(candidate.ratios['211']) }}（211 Å）。</p>
          <p>达到测量门槛的磁图像素中，{{ candidate.dominant_polarity === 'positive' ? '正极性' : '负极性' }}占 {{ percent(candidate.dominant_share) }}，这些可判断极性的像素占候选范围 {{ percent(candidate.significant_share) }}。</p>
          <ul>
            <li v-for="neighbor in candidate.neighbors" :key="neighbor.offset_hours">
              <strong>{{ neighbor.offset_hours < 0 ? '前' : '后' }} {{ Math.abs(neighbor.offset_hours) }} 小时 · {{ stateLabels[neighbor.state] || '待核验' }}</strong>
              <span>{{ utc(neighbor.nominal_utc) }}</span>
              <a v-for="item in neighbor.channels.filter(channel => channel.url)" :key="item.channel" :href="item.url || undefined"
                target="_blank" rel="noopener noreferrer" :title="item.observed_utc ? utc(item.observed_utc) : undefined">
                {{ item.channel }} Å 原始观测
              </a>
            </li>
          </ul>
        </article>
      </div>
      <figure v-if="image">
        <img :src="image" alt="两处候选在 AIA 193、AIA 211 和 HMI 磁图上的位置对照" width="1504" height="1088"/>
        <figcaption>黄线标出模型的候选范围。三列是同一位置的不同观测。</figcaption>
      </figure>
    </div>
  </details>
</template>

<style scoped>
.review-panel{margin-top:20px;padding:18px 20px;background:var(--bg-panel);border:1px solid var(--border);border-radius:12px}
.review-panel summary{font-weight:650;cursor:pointer}
.review-content,.feedback{margin-top:12px;font-size:14px;line-height:1.65}
.review-content figure{margin:14px 0}
.review-content img{display:block;width:min(100%,940px);height:auto;border-radius:8px;border:1px solid var(--border)}
.review-content figcaption,.observed,.review-case li span{color:var(--text-secondary)}
.review-cases{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:12px;margin-top:14px}
.review-case{padding:14px;border:1px solid var(--border);border-radius:8px}
.review-case h3{font-size:16px;margin:0}
.review-case p{margin:6px 0}
.review-case ul{margin:8px 0;padding-left:1.25em}
.review-case li{margin:6px 0}
.review-case li span,.review-case li a{display:inline-block;margin-left:8px}
.feedback button{padding:6px 12px;border:1px solid var(--border);color:inherit;background:var(--bg-elevated);border-radius:6px;cursor:pointer}
@media(max-width:640px){.review-panel{padding:14px}.review-case li span{display:block;margin-left:0}.review-case li a{margin-left:0;margin-right:8px}}
</style>
