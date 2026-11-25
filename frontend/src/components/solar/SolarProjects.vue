<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { buildQuery, get, getBlob, LONG_TIMEOUT, post } from '@/api/client'
import { useCtxStore } from '@/stores/ctx'
import { FolderOpen, Images, Upload } from 'lucide-vue-next'
import ProjectNavigator from './ProjectNavigator.vue'
import ProjectSourcePicker from './ProjectSourcePicker.vue'
import MDialog from '@/components/ui/MDialog.vue'
import ProjectPhoto from './ProjectPhoto.vue'
import BatchRecognitionDialog from './BatchRecognitionDialog.vue'
import {
  channelNames,
  duration,
  localFinish,
  estimateText,
  type Estimate,
  stateNames,
  utc,
  type Batch,
  type Photo,
  type Project,
  type Row,
} from './projectTypes'

const props = defineProps<{ catalogUri?: string }>()
const emit = defineEmits<{ imported: [] }>()
const ctx = useCtxStore()
const projects = ref<Project[]>([]),
  project = ref(''),
  photos = ref<Photo[]>([]),
  batches = ref<Batch[]>([])
const selected = ref<string[]>([]),
  anchor = ref(''),
  detail = ref(''),
  batch = ref(''),
  rows = ref<Row[]>([])
const sourcesOpen = ref(false)
const currentProject = computed(() => projects.value.find((p) => p.id === project.value))
const managed = computed(() => currentProject.value?.managed)
const projectPinsSaving = new Set<string>()
const previews = ref<{ url: string; name: string }[]>([])
const dragDepth = ref(0)
const search = ref(''),
  error = ref(''),
  notice = ref(''),
  importing = ref(false),
  errors = ref<string[]>([])
const projectLoading = ref(false),
  newOpen = ref(false),
  newName = ref(''),
  saving = ref(false),
  confirming = ref(false)
const snapshot = ref<Photo[]>([])
const batchEstimate = ref<Estimate>()
const initialDevice = ref('auto')
const batchMode = ref('auto')
const picker = ref<HTMLInputElement>(),
  folderPicker = ref<HTMLInputElement>(),
  downloadBusy = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined,
  generation = 0,
  pollVersion = 0
const workbench = ref<HTMLElement>()
const library = ref<HTMLElement>()
let savedScroll = 0

function scrollContainer() {
  return window.matchMedia('(max-width: 950px)').matches ? workbench.value : library.value
}
const key = () => 'mdva.solar.project.' + (ctx.ws || '.')
const visible = computed(() =>
  photos.value.filter((p) => p.name.toLowerCase().includes(search.value.toLowerCase())),
)
const focus = computed(() => photos.value.find((p) => p.id === detail.value))
const byPhoto = computed(() => new Map(rows.value.map((r) => [r.id, r])))
const byId = computed(() => new Map(photos.value.map((p) => [p.id, p])))
const done = computed(
  () => rows.value.filter((r) => r.state === 'completed' || r.state === 'failed').length,
)
const failures = computed(() => rows.value.filter((r) => r.state === 'failed'))
const activeRows = computed(() => rows.value.filter((r) => r.state === 'running'))
const related = computed(() => {
  const current = focus.value
  if (!current?.observed_utc || !byPhoto.value.has(current.id)) return []
  return rows.value.filter(r => r.id !== current.id && byId.value.get(r.id)?.observed_utc === current.observed_utc)
})
const timeline = computed(() => {
  const items = new Map<string, Row>()
  for (const r of rows.value) {
    if (r.forecast?.state !== 'ready' || !r.observed_utc) continue
    const key = [
      r.observed_utc,
      r.forecast.cutoff_utc,
      r.forecast.satellite,
      r.forecast.probability,
    ].join('|')
    if (!items.has(key)) items.set(key, r)
  }
  return Array.from(items.values()).sort((a, b) =>
    String(a.observed_utc).localeCompare(String(b.observed_utc)),
  )
})
function reset() {
  generation++
  pollVersion++
  clearTimeout(timer)
  photos.value = []
  batches.value = []
  rows.value = []
  selected.value = []
  dragDepth.value = 0
  sourcesOpen.value = false
  detail.value = ''
  batch.value = ''
  anchor.value = ''
  confirming.value = false
  error.value = ''
  notice.value = ''
  errors.value = []
  search.value = ''
}
async function loadProjects() {
  const ws = ctx.ws || '.'
  project.value = ''
  reset()
  try {
    const data = await get<{ projects: Project[] }>('/solar/projects')
    if (ws !== (ctx.ws || '.')) return
    projects.value = data.projects
    const saved = localStorage.getItem(key())
    const lastOpened = [...data.projects].sort((a, b) => (b.opened_at || '').localeCompare(a.opened_at || ''))[0]
    const initial = data.projects.find((p) => p.id === saved) || lastOpened
    project.value = initial?.id || ''
    if (initial && !initial.opens) void recordProject(initial.id, 'open')
  } catch (e) {
    if (ws === (ctx.ws || '.')) error.value = String(e)
  }
}
async function loadProject() {
  reset()
  const id = project.value,
    version = generation
  projectLoading.value = Boolean(id)
  if (!id) return
  localStorage.setItem(key(), id)
  try {
    const data = await get<{ photos: Photo[]; batches: Batch[] }>('/solar/project', { project: id })
    if (version !== generation) return
    photos.value = data.photos
    batches.value = data.batches
    if (data.batches[0]) await follow(data.batches[0].id)
    if (props.catalogUri) await importCatalog()
  } catch (e) {
    if (version === generation) error.value = String(e)
  } finally {
    if (version === generation) projectLoading.value = false
  }
}
async function recordProject(id: string, action: 'open' | 'pin', pinned?: boolean) {
  const workspace = ctx.ws || '.'
  try {
    const updated = await post<Project>('/solar/projects/preferences', { project: id, action, pinned })
    if ((ctx.ws || '.') !== workspace) return
    projects.value = projects.value.map(p => {
      if (p.id !== id) return p
      if (action === 'pin') return { ...p, pinned: updated.pinned }
      return updated.opens >= p.opens ? { ...p, opens: updated.opens, opened_at: updated.opened_at } : p
    })
  } catch (e) {
    if ((ctx.ws || '.') === workspace) error.value = String(e)
  }
}

function openProject(id: string) {
  if (importing.value || id === project.value) return
  project.value = id
  void recordProject(id, 'open')
}

async function pinProject(item: Project) {
  if (projectPinsSaving.has(item.id)) return
  projectPinsSaving.add(item.id)
  try { await recordProject(item.id, 'pin', !item.pinned) }
  finally { projectPinsSaving.delete(item.id) }
}

async function create() {
  if (!newName.value.trim() || saving.value) return
  saving.value = true
  try {
    const p = await post<Project>('/solar/projects', { name: newName.value.trim() })
    projects.value.unshift(p)
    newOpen.value = false
    newName.value = ''
    openProject(p.id)
  } catch (e) {
    error.value = String(e)
  } finally {
    saving.value = false
  }
}
function append(p: Photo) {
  if (!photos.value.some((old) => old.id === p.id)) photos.value.push(p)
}
function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(Error('读取失败'))
    reader.readAsDataURL(file)
  })
}
function releasePreviews() {
  for (const preview of previews.value) URL.revokeObjectURL(preview.url)
  previews.value = []
}

function dragEnter(e: DragEvent) {
  if (e.dataTransfer?.types.includes('Files')) dragDepth.value++
}

function dropPhotos(e: DragEvent) {
  dragDepth.value = 0
  void upload(e.dataTransfer?.files || null)
}

function finishImport(added: string[], imported: number, duplicates: number) {
  detail.value = ''
  search.value = ''
  selected.value = [...new Set(added)]
  notice.value = `新增 ${imported} 张 · 已有 ${duplicates} 张 · 失败 ${errors.value.length} 张`
}

async function upload(files: FileList | null) {
  if (!files?.length || projectLoading.value || saving.value || importing.value || !project.value) return
  const list = Array.from(files).filter(file => /\.(jpe?g|png)$/i.test(file.name))
  if (picker.value) picker.value.value = ''
  if (folderPicker.value) folderPicker.value.value = ''
  if (!list.length) {
    notice.value = '请选择 JPG、JPEG 或 PNG 照片'
    return
  }
  const id = project.value
  const version = generation
  importing.value = true
  errors.value = []
  error.value = ''
  detail.value = ''
  previews.value = list.slice(0, 8).map(file => ({ name: file.name, url: URL.createObjectURL(file) }))
  const added: string[] = []
  let imported = 0, duplicates = 0
  try {
    for (const [i, file] of list.entries()) {
      if (generation !== version) break
      notice.value = `正在批量导入 ${i + 1} / ${list.length} 张`
      try {
        if (file.size > 4 * 1024 * 1024) throw Error('超过 4 MiB')
        const response = await post<{ photo: Photo; duplicate: boolean }>('/solar/project/import', {
          project: id, filename: file.name, image_data: await readFile(file),
        })
        if (generation !== version) break
        append(response.photo)
        added.push(response.photo.id)
        if (response.duplicate) duplicates++
        else imported++
      } catch (e) {
        if (generation === version) errors.value.push(file.name + ' · ' + String(e))
      }
    }
    if (generation === version) finishImport(added, imported, duplicates)
  } finally {
    releasePreviews()
    importing.value = false
  }
}

async function addSources(ids: string[]) {
  if (!ids.length || importing.value) return
  const version = generation
  const id = project.value
  sourcesOpen.value = false
  importing.value = true
  errors.value = []
  error.value = ''
  const added: string[] = []
  let imported = 0, duplicates = 0
  try {
    for (let start = 0; start < ids.length && generation === version; start += 16) {
      const chunk = ids.slice(start, start + 16)
      notice.value = `正在载入照片 ${start} / ${ids.length} 张`
      try {
        const { results } = await post<{results: { photo?: Photo; duplicate?: boolean; name?: string; error?: string }[]}>(
          '/solar/project/import-sources', { project: id, ids: chunk }, { timeout: LONG_TIMEOUT },
        )
        if (version !== generation) break
        for (const item of results) {
          if (!item.photo) {
            errors.value.push((item.name || '照片') + ' · ' + item.error)
            continue
          }
          append(item.photo)
          added.push(item.photo.id)
          if (item.duplicate) duplicates++
          else imported++
        }
      } catch (e) {
        if (version === generation)
          chunk.forEach((_, offset) => errors.value.push(`第 ${start + offset + 1} 张 · ${String(e)}`))
      }
    }
    if (version === generation) finishImport(added, imported, duplicates)
  } finally {
    importing.value = false
  }
}
async function importCatalog() {
  if (!props.catalogUri || !project.value || importing.value) return
  const version = generation
  importing.value = true
  try {
    const r = await post<{ photo: Photo }>('/solar/project/import', {
      project: project.value,
      catalog_uri: props.catalogUri,
    })
    if (version === generation) {
      append(r.photo)
      selected.value = [r.photo.id]
      notice.value = '已选中目录照片'
      emit('imported')
    }
  } catch (e) {
    error.value = String(e)
  } finally {
    importing.value = false
  }
}
function select(p: Photo, e: MouseEvent, toggle = false) {
  if (e.shiftKey && anchor.value) {
    const ids = visible.value.map((p) => p.id),
      start = ids.indexOf(anchor.value),
      end = ids.indexOf(p.id)
    if (start >= 0) {
      selected.value = [
        ...new Set([
          ...(e.ctrlKey || e.metaKey ? selected.value : []),
          ...ids.slice(Math.min(start, end), Math.max(start, end) + 1),
        ]),
      ]
      return
    }
  }
  if (toggle || e.ctrlKey || e.metaKey)
    selected.value = selected.value.includes(p.id)
      ? selected.value.filter((v) => v !== p.id)
      : [...selected.value, p.id]
  else selected.value = [p.id]
  anchor.value = p.id
}
async function follow(id: string) {
  clearTimeout(timer)
  const run = ++pollVersion,
    version = generation
  batch.value = id
  batchEstimate.value = undefined
  rows.value = []
  async function poll() {
    try {
      const data = await get<{ rows: Row[]; estimate?: Estimate; mode: string }>('/solar/project/batch', {
        project: project.value, id,
      })
      if (run !== pollVersion || version !== generation) return
      rows.value = data.rows
      batchEstimate.value = data.estimate
      batchMode.value = data.mode
      if (
        data.rows.every(
          (r) => r.state === 'failed' || (r.state === 'completed' && r.forecast?.state !== 'pending'),
        )
      )
        return
    } catch (e) {
      if (run === pollVersion) error.value = String(e)
    }
    if (run === pollVersion) timer = setTimeout(poll, 1200)
  }
  await poll()
}
function confirm(ids = selected.value, mode = 'auto') {
  const set = new Set(ids)
  snapshot.value = photos.value.filter(p => set.has(p.id))
  initialDevice.value = mode
  confirming.value = true
}

async function followQueued(id: string, ids: string[]) {
  if (!confirming.value) return
  confirming.value = false
  const queued = new Set(ids)
  for (const photo of snapshot.value) {
    if (queued.has(photo.id)) photo.last_batch = id
  }
  batches.value.unshift({ id, created: new Date().toISOString(), total: ids.length })
  await follow(id)
}

async function openPhoto(id: string) {
  if (!detail.value) savedScroll = scrollContainer()?.scrollTop ?? 0
  detail.value = id
  await nextTick()
  scrollContainer()?.scrollTo({ top: 0 })

  const latest = byId.value.get(id)?.last_batch
  if (!byPhoto.value.has(id) && latest) await follow(latest)
}
async function closePhoto() {
  detail.value = ''
  await nextTick()
  scrollContainer()?.scrollTo({ top: savedScroll })
}
async function retryForecast(id: string) {
  try {
    await post('/solar/project/forecast/retry', { project: project.value, id: batch.value, photo: id })
    await follow(batch.value)
  } catch (e) {
    error.value = String(e)
  }
}
async function download(photo = '') {
  downloadBusy.value = true
  try {
    const blob = await getBlob('/solar/project/download', {
      project: project.value,
      id: batch.value,
      photo,
    }, LONG_TIMEOUT)
    const url = URL.createObjectURL(blob),
      link = document.createElement('a')
    link.href = url
    link.download = (photo ? byId.value.get(photo)?.name : '批量识别结果') + '.zip'
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 3000)
  } catch (e) {
    error.value = String(e)
  } finally {
    downloadBusy.value = false
  }
}
watch(() => ctx.ws || '.', loadProjects, { immediate: true })
watch(project, loadProject)
watch(
  () => props.catalogUri,
  () => {
    if (project.value) void importCatalog()
  },
)
onBeforeUnmount(() => {
  generation++
  pollVersion++
  clearTimeout(timer)
  releasePreviews()
})
</script>
<template>
  <div
    ref="workbench" class="project-workbench"
    @dragenter.prevent="dragEnter"
    @dragleave.prevent="dragDepth = Math.max(0, dragDepth - 1)"
    @dragover.prevent
    @drop.prevent="dropPhotos"
  >
    <aside class="projects">
      <ProjectNavigator :projects="projects" :current="project" :busy="importing || saving"
        @open="openProject" @pin="pinProject" @create="newOpen = true" />
      <h3>识别批次</h3>
      <small v-if="!batches.length">提交后可在这里查看进度</small>
      <select
        v-if="batches.length"
        class="compact-batches"
        aria-label="选择识别批次"
        :value="batch"
        @change="follow(($event.target as HTMLSelectElement).value)"
      >
        <option v-for="b in batches" :key="b.id" :value="b.id">
          {{ b.total }} 张 · {{ new Date(b.created).toLocaleString() }}
        </option>
      </select>
      <button
        v-for="b in batches"
        :key="b.id"
        class="batch-link"
        :aria-pressed="b.id === batch"
        @click="follow(b.id)"
      >
        <strong>{{ b.total }} 张照片</strong><small>{{ new Date(b.created).toLocaleString() }}</small>
      </button>
    </aside>
    <main ref="library" class="library">
      <div class="section-head">
        <div>
          <h2>项目照片</h2>
          <small v-if="currentProject" class="project-context">{{ currentProject.name }} · {{ photos.length.toLocaleString() }} 张</small>
        </div>
      </div>
      <div v-if="project" class="import-panel" :class="{ dragging: dragDepth > 0 }">
        <div class="import-description"><Upload :size="20" /><span><strong>批量导入照片</strong><small>多选 JPG、JPEG、PNG，或直接拖放到此处</small></span></div>
        <div class="import-actions">
          <button class="accent" :disabled="importing || saving || projectLoading" @click="picker?.click()"><Images :size="16" /> 选择多张照片</button>
          <button :disabled="importing || saving || projectLoading" @click="folderPicker?.click()"><FolderOpen :size="16" /> 导入文件夹</button>
          <button v-if="!managed" :disabled="importing || projectLoading" @click="sourcesOpen = true">从项目选取</button>
        </div>
      </div>
      <input ref="picker" type="file" multiple accept=".jpg,.jpeg,.png" aria-label="选择多张照片" hidden
        @change="upload(($event.target as HTMLInputElement).files)" />
      <input ref="folderPicker" type="file" webkitdirectory multiple aria-label="导入整个文件夹" hidden
        @change="upload(($event.target as HTMLInputElement).files)" />
      <p v-if="notice" role="status">{{ notice }}</p>
      <div v-if="previews.length" class="import-previews" aria-label="正在导入的照片预览">
        <img v-for="item in previews" :key="item.url" :src="item.url" :alt="item.name" :title="item.name">
      </div>
      <details v-if="errors.length">
        <summary>查看 {{ errors.length }} 项导入问题</summary>
        <p v-for="msg in errors" :key="msg">{{ msg }}</p>
      </details>
      <ProjectPhoto
        v-if="focus"
        :key="focus.id"
        :photo="focus"
        :row="byPhoto.get(focus.id)"
        :project="project"
        @close="closePhoto"
        @download="download(focus.id)"
        @retry="retryForecast(focus.id)"
      />
      <template v-else>
        <div v-if="photos.length" class="mobile-selection">
          <span>已选 {{ selected.length }} 张</span>
          <button class="accent" :disabled="!selected.length" @click="confirm()">批量自动识别</button>
        </div>
        <div v-if="photos.length" class="tools">
          <input v-model="search" type="search" placeholder="筛选文件名" aria-label="筛选照片" /><button
            @click="selected = visible.map((p) => p.id)"
          >
            全选当前结果</button
          ><button @click="selected = []">清除选择</button>
        </div>
        <div v-if="!photos.length && !importing && !projectLoading" class="drop">
          <Images :size="28" />
          <h3>{{ project ? '此项目还没有照片' : '创建或打开图像项目' }}</h3>
          <button v-if="!project" @click="newOpen = true">新建项目</button>
        </div>
        <div class="grid">
          <div
            v-for="p in visible"
            :key="p.id"
            class="tile"
            :class="{ selected: selected.includes(p.id) }"
          >
            <button
              class="thumb"
              :aria-label="'选择 ' + p.name"
              :aria-pressed="selected.includes(p.id)"
              @click="select(p, $event)"
              @dblclick="openPhoto(p.id)"
            >
              <img
                loading="lazy"
                :src="'/api/solar/project/image' + buildQuery({ project, id: p.id, thumb: true })"
                alt=""
              /><strong>{{ p.name }}</strong
              ><small
                >{{ channelNames[p.category] }}
                {{ byPhoto.get(p.id) ? ' · ' + stateNames[byPhoto.get(p.id)!.state] : '' }}</small>
              <small v-if="p.observed_utc">{{ utc(p.observed_utc) }}</small></button
            ><input
              type="checkbox"
              :checked="selected.includes(p.id)"
              :aria-label="'勾选 ' + p.name"
              @click="select(p, $event, true)"
            />
          </div></div
      ></template>
    </main>
    <aside class="operations">
      <h2>已选 {{ selected.length }} 张</h2>
      <p>Ctrl 点选多张，Shift 连选一段。</p>
      <button class="accent wide desktop-submit" :disabled="!selected.length" @click="confirm()">批量自动识别</button
      ><button v-if="selected.length === 1" @click="openPhoto(selected[0] || '')">查看选中照片</button>
      <section v-if="related.length"><h3>同一时刻的观测</h3>
        <button v-for="r in related" :key="r.id" @click="openPhoto(r.id)">{{channelNames[r.category]}} · {{byId.get(r.id)?.name}}</button>
      </section>
      <section v-if="batch">
        <h3>批次进度</h3>
        <progress :max="rows.length || 1" :value="done" />
        <p>
          {{ done }} / {{ rows.length }} 张已处理
          <span v-if="rows.length && done < rows.length">· 预计剩余 {{ estimateText(batchEstimate) }}</span>
          <small v-if="batchEstimate?.finish_utc"> · 预计 {{ localFinish(batchEstimate) }} 完成</small>
        </p>
        <p v-for="r in activeRows" :key="r.id">
          {{ byId.get(r.id)?.name }} · {{ r.hardware || r.device?.toUpperCase() }}
        </p>
        <ul v-if="batchEstimate && batchEstimate.devices.length > 1" class="device-times">
          <li v-for="d in batchEstimate.devices" :key="d.id">
            {{ d.id }} · {{ d.name }} · 剩 {{ d.remaining }} 张 · {{ d.seconds != null ? duration(d.seconds) : '等待估算' }}
          </li>
        </ul>
        <div class="row-list">
          <button v-for="r in rows" :key="r.id" @click="detail = r.id">
            <span>{{ byId.get(r.id)?.name }}</span
            ><small>{{ stateNames[r.state] }}</small>
          </button>
        </div>
        <button v-if="rows.length && done === rows.length" :disabled="downloadBusy" @click="download()">
          下载整批结果</button
        ><button v-if="failures.length" @click="confirm(failures.map((r) => r.id), batchMode)">
          重试失败的 {{ failures.length }} 张
        </button>
      </section>
      <section v-if="timeline.length">
        <h3>耀斑风险时间对照</h3>
        <button v-for="r in timeline" :key="r.id" class="timeline" @click="detail = r.id">
          <small>{{ utc(r.observed_utc) }}</small
          ><strong>{{ ((r.forecast?.probability || 0) * 100).toFixed(2) }}%</strong></button
        ><small>全日面 · 未来 24 小时 · 研究试算</small>
      </section>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </aside>
    <ProjectSourcePicker :open="sourcesOpen" :project="project" @close="sourcesOpen = false" @import="addSources" />
    <MDialog :open="newOpen" title="新建图像项目" :width="480" @close="newOpen = false"
      ><label
        >项目名称
        <input
          v-model.trim="newName"
          maxlength="120"
          placeholder="如：十一月太阳观测"
          @keyup.enter="create"
      /></label>
      <p v-if="error" role="alert">{{ error }}</p>
      <template #footer
        ><button @click="newOpen = false">取消</button
        ><button class="accent" :disabled="!newName || saving" @click="create">
          创建项目
        </button></template
      ></MDialog
    >
    <BatchRecognitionDialog :open="confirming" :project="project" :photos="snapshot"
      :initial-device="initialDevice" @close="confirming = false" @queued="followQueued" />
  </div>
</template>
<style scoped>
.compact-batches {
  display: none;
}
@media (max-width: 950px) {
  .projects .batch-link {
    display: none;
  }
  .projects .compact-batches {
    display: block;
  }
}
input[type="file"][hidden] { display: none; }
.import-panel { display: flex; justify-content: space-between; align-items: center; gap: 12px 16px; flex-wrap: wrap; margin: 14px 0 10px; padding: 14px; border: 1px dashed var(--border); border-radius: 10px; background: var(--bg-panel); }
.import-panel.dragging { border-color: var(--accent); background: var(--accent-soft, var(--bg-elevated)); }
.import-description, .import-actions { display: flex; align-items: center; gap: 8px; }
.import-description > svg { color: var(--accent); }
.import-description span { display: grid; gap: 3px; }
.import-description strong { font-size: 14px; }
.import-actions { flex-wrap: wrap; }
.import-actions button { display: inline-flex; align-items: center; gap: 6px; }
.import-previews { display: grid; grid-template-columns: repeat(auto-fill, minmax(60px, 82px)); gap: 6px; }
.import-previews img { width: 100%; aspect-ratio: 1; object-fit: contain; background: #10171c; border-radius: 6px; }
.project-context { overflow-wrap: anywhere; }
.projects > nav { flex-basis: 100%; width: 100%; }
.drop svg { color: var(--text-secondary); margin-bottom: 5px; }
.project-workbench {
  display: grid;
  grid-template-columns: minmax(208px, 238px) minmax(0, 1fr) minmax(215px, 290px);
  flex: 1;
  min-height: 0;
  overflow: hidden;
  color: var(--text-primary);
}
.projects,
.operations,
.library {
  padding: 18px;
  overflow: auto;
  min-width: 0;
}
.projects,
.operations {
  background: var(--bg-panel);
}
.projects {
  border-right: 1px solid var(--border);
}
.operations {
  border-left: 1px solid var(--border);
}
h2,
h3 {
  margin: 0 0 12px;
  font-size: 17px;
}
.projects h3,
.operations h3 {
  margin-top: 24px;
  font-size: 14px;
}
small,
p {
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
}
button,
select,
input:not([type='checkbox']) {
  font: inherit;
  color: inherit;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 8px 10px;
  max-width: 100%;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.45;
  cursor: default;
}
.accent {
  background: #186a70;
  border-color: #33868a;
  color: #fff;
  font-weight: 600;
}
.wide,
.projects select,
.batch-link,
.timeline {
  width: 100%;
}
.projects select,
.operations button {
  margin-bottom: 7px;
}
.section-head,
.tools,
.row-list button {
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-head {
  justify-content: space-between;
}
.section-head h2 {
  margin: 0;
}
.tools {
  margin: 18px 0;
  flex-wrap: wrap;
}
.tools input {
  min-width: 100px;
  flex: 1;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(138px, 1fr));
  gap: 12px;
}
.tile {
  position: relative;
  border: 2px solid transparent;
  border-radius: 10px;
  min-width: 0;
}
.tile.selected {
  border-color: var(--accent, #4bb6b3);
}
.tile input {
  position: absolute;
  left: 8px;
  top: 8px;
  width: 17px;
  height: 17px;
  accent-color: #328e91;
}
.thumb {
  width: 100%;
  padding: 0 0 8px;
  overflow: hidden;
  text-align: left;
}
.thumb img {
  width: 100%;
  aspect-ratio: 1.2;
  object-fit: contain;
  background: #10161d;
}
.thumb strong,
.thumb small,
.batch-link small,
.timeline small {
  display: block;
}
.thumb strong,
.thumb small {
  padding: 3px 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.thumb strong {
  font-size: 12px;
}
.drop {
  border: 1px dashed var(--border);
  border-radius: 12px;
  min-height: 200px;
  display: grid;
  align-content: center;
  justify-items: center;
  margin-top: 20px;
}
.drop h3 {
  margin: 0;
}
.batch-link,
.timeline {
  text-align: left;
  margin-bottom: 6px;
}
.batch-link[aria-pressed='true'] {
  border-color: var(--accent);
}
.row-list {
  max-height: 32vh;
  overflow: auto;
}
.row-list button {
  width: 100%;
  justify-content: space-between;
  text-align: left;
  font-size: 12px;
}
.row-list span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-list small {
  flex: none;
}
.operations progress {
  width: 100%;
  accent-color: #358f94;
}
.error {
  color: var(--danger);
}
@media (max-width: 950px) {
  .project-workbench {
    grid-template-columns: 1fr minmax(190px, 30%);
    overflow: auto;
    align-content: start;
  }
  .projects {
    grid-column: 1/-1;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    border-bottom: 1px solid var(--border);
  }
  .projects select,
  .projects button {
    width: auto;
    max-width: 180px;
  }
  .projects h2,
  .projects h3 {
    margin: 0;
  }
  .library,
  .operations {
    overflow: visible;
  }
}
.mobile-selection {
  display: none;
}
@media (max-width: 650px) {
  .desktop-submit {
    display: none;
  }
  .mobile-selection {
    position: sticky;
    z-index: 2;
    top: 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    padding: 8px 0;
    background: var(--bg-base);
    border-bottom: 1px solid var(--border);
  }
  .project-workbench {
    display: block;
  }
  .operations {
    border: 0;
    border-top: 1px solid var(--border);
  }
  .grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
