<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { buildQuery, get } from '@/api/client'
import { channelNames, duration, stateNames, utc, type Photo, type Row } from './projectTypes'

const props = defineProps<{ project: string; photo: Photo; row?: Row }>()
const emit = defineEmits<{ download: []; close: []; retry: [] }>()
interface Region {
  component_id: number
  area_pixels: number
  bbox: [number, number, number, number]
  segmentation: { counts: number[] }
}
const regions = ref<Region[]>([]),
  loading = ref(false),
  active = ref(0),
  overlay = ref(true),
  opacity = ref(0.55),
  error = ref('')
const original = computed(
  () => '/api/solar/project/image' + buildQuery({ project: props.project, id: props.photo.id }),
)
const mask = computed(() =>
  props.row
    ? '/api/solar/inference/assets' +
      buildQuery({
        id: props.row.job,
        asset: 'image-' + String(props.row.index).padStart(6, '0') + '/candidate-overlay.png',
      })
    : '',
)
const forecast = computed(() => props.row?.forecast)
watch(
  () => [props.photo.id, props.row?.job, props.row?.state],
  async (_, __, onCleanup) => {
    let current = true
    onCleanup(() => {
      current = false
    })
    active.value = 0
    regions.value = []
    error.value = ''
    loading.value = props.row?.state === 'completed'
    if (props.row?.state !== 'completed') return
    try {
      const r = await get<{ regions: Region[] }>('/solar/project/regions', {
        id: props.row.job,
        index: props.row.index,
      })
      if (current) regions.value = r.regions
    } catch (e) {
      if (current) error.value = String(e)
    } finally { if (current) loading.value = false }
  },
  { immediate: true },
)
function pick(e: MouseEvent) {
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  const x = Math.floor(((e.clientX - rect.left) / rect.width) * props.photo.width)
  const y = Math.floor(((e.clientY - rect.top) / rect.height) * props.photo.height)
  const offset = x * props.photo.height + y
  active.value =
    regions.value.find((r) => {
      const [left, top, w, h] = r.bbox
      if (x < left || y < top || x >= left + w || y >= top + h) return false
      let end = 0
      for (const [index, n] of r.segmentation.counts.entries()) {
        end += n
        if (offset < end) return index % 2 === 1
      }
      return false
    })?.component_id || 0
}
const referenceAvailable = ref(false),
  referenceOn = ref(false)
watch(
  () => props.photo.reference_uri,
  async (uri, _, onCleanup) => {
    let current = true
    onCleanup(() => {
      current = false
    })
    referenceOn.value = false
    referenceAvailable.value = false
    if (!uri) return
    try {
      const ref = await get<{ available: boolean }>('/reference', { uri })
      if (current) referenceAvailable.value = ref.available
    } catch {
      /* Optional reference may be outside this workspace. */
    }
  },
  { immediate: true },
)
const selected = computed(() => regions.value.find((r) => r.component_id === active.value))
</script>
<template>
  <article class="photo-detail">
    <header>
      <button @click="emit('close')">← 返回照片</button><strong>{{ photo.name }}</strong>
    </header>
    <div class="image" @click="pick">
      <img :src="original" :alt="photo.name" />
      <img
        v-if="overlay && row?.state === 'completed'"
        class="layer"
        :src="mask"
        :style="{ opacity }"
        alt="模型识别范围"
      />
      <img
        v-if="referenceOn && referenceAvailable"
        class="layer"
        style="opacity: 0.45"
        :src="
          '/api/reference/overlay' +
          buildQuery({ uri: photo.reference_uri, width: photo.width, height: photo.height })
        "
        alt="专家参考范围"
      />
      <svg v-if="selected" class="layer" :viewBox="`0 0 ${photo.width} ${photo.height}`">
        <rect
          :x="selected.bbox[0]"
          :y="selected.bbox[1]"
          :width="selected.bbox[2]"
          :height="selected.bbox[3]"
          fill="none"
          stroke="#ffda78"
          stroke-width="2"
          vector-effect="non-scaling-stroke"
        />
      </svg>
    </div>
    <p>
      {{ channelNames[row?.category || photo.category] }} ·
      {{ utc(row?.observed_utc || photo.observed_utc) }}
    </p>
    <section v-if="row?.state === 'completed'">
      <div class="controls">
        <label><input v-model="overlay" type="checkbox" /> 自动范围</label
        ><label v-if="referenceAvailable"><input v-model="referenceOn" type="checkbox" /> 专家参考</label
        ><label
          >透明度 <input v-model.number="opacity" type="range" min="0.1" max="1" step=".05" /></label
        ><button @click="emit('download')">下载本图结果</button>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-else-if="loading">正在读取区域…</p>
      <p v-else>{{ regions.length ? '候选区域 ' + regions.length + ' 处' : '本图未检出候选范围' }}</p>
      <div class="regions">
        <button
          v-for="r in regions"
          :key="r.component_id"
          :aria-pressed="active === r.component_id"
          @click="active = r.component_id"
        >
          区域 {{ r.component_id }} · {{ r.area_pixels }} 像素
        </button>
      </div>
      <section class="forecast">
        <h3>事件试算</h3>
        <template v-if="forecast?.state === 'ready'"
          ><strong>{{ ((forecast.probability || 0) * 100).toFixed(2) }}%</strong> · {{ forecast.target }}
          <p>{{ utc(forecast.observed_utc) }} → {{ utc(forecast.end_utc) }}</p>
          <small
            >{{ forecast.time_basis === 'user_supplied' ? '观测时间由用户提供 · ' : '' }}{{ forecast.source }} · 卫星 {{ forecast.satellite }} · 资料截至
            {{ utc(forecast.cutoff_utc) }} · 研究结果</small
          ></template
        >
        <p v-else>{{ forecast?.message || '正在匹配 GOES 观测…' }}</p>
        <button
          v-if="
            row.category === 'AR' &&
            row.observed_utc &&
            (forecast?.state === 'failed' || forecast?.state === 'unavailable')
          "
          @click="emit('retry')"
        >
          重新匹配观测
        </button>
      </section>
      <small>{{ row.hardware || row.device?.toUpperCase() }} · 逐图处理 {{ duration(row.seconds || 0) }}</small>
    </section>
    <p v-else-if="row">{{ stateNames[row.state] }} {{ row.error }}</p>
  </article>
</template>
<style scoped>
.photo-detail {
  padding: 16px;
  min-width: 0;
}
.photo-detail header,
.controls,
.regions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.photo-detail header strong {
  overflow-wrap: anywhere;
}
.image {
  position: relative;
  background: #090d13;
  max-width: 650px;
  margin: 12px auto;
  cursor: crosshair;
}
.image img {
  width: 100%;
  display: block;
}
.layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.photo-detail p,
.photo-detail small {
  color: var(--text-secondary);
  line-height: 1.6;
}
.regions {
  max-height: 135px;
  overflow: auto;
}
.forecast {
  margin: 16px 0;
  padding: 14px;
  background: var(--bg-elevated);
  border-radius: 8px;
}
.forecast strong {
  font-size: 24px;
}
.forecast h3 {
  margin: 0 0 8px;
}
.photo-detail button {
  cursor: pointer;
  background: var(--bg-panel);
  color: inherit;
  border: 1px solid var(--border);
  padding: 7px 10px;
  border-radius: 6px;
}
.photo-detail button[aria-pressed='true'] {
  border-color: var(--accent);
}
</style>
