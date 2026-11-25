<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, Image as ImageIcon, Search } from 'lucide-vue-next'
import { buildQuery, get } from '@/api/client'
import MDialog from '@/components/ui/MDialog.vue'

interface SourcePhoto { id: string; name: string }
const props = defineProps<{ open: boolean; project: string }>()
const emit = defineEmits<{ close: []; import: [ids: string[]] }>()
const search = ref('')
const appliedSearch = ref('')
const photos = ref<SourcePhoto[]>([])
const selected = ref(new Set<string>())
const failed = ref(new Set<string>())
const loading = ref(false)
const selecting = ref(false)
const error = ref('')
const page = ref(1)
const total = ref(0)
const anchor = ref('')
const pageSize = 60
let generation = 0

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const allOnPage = computed(() => photos.value.length > 0 && photos.value.every(photo => selected.value.has(photo.id)))
const thumbnail = (id: string) => '/api/solar/project/source-thumbnail' + buildQuery({ project: props.project, id })

async function browse(nextPage: number, refresh = false) {
  const version = ++generation
  loading.value = true
  error.value = ''
  try {
    const data = await get<{ images: SourcePhoto[]; total: number }>('/solar/project/sources', {
      project: props.project, page: nextPage, search: appliedSearch.value, refresh,
    })
    if (version !== generation) return
    page.value = nextPage
    photos.value = data.images
    total.value = data.total
    anchor.value = ''
  } catch (e) {
    if (version === generation) error.value = String(e)
  } finally {
    if (version === generation) loading.value = false
  }
}

function searchPhotos() {
  appliedSearch.value = search.value.trim()
  void browse(1)
}

function toggle(id: string, event: MouseEvent) {
  if (event.shiftKey) {
    const ids = photos.value.map(p => p.id)
    const first = ids.indexOf(anchor.value)
    if (first >= 0) {
      const last = ids.indexOf(id)
      for (const key of ids.slice(Math.min(first, last), Math.max(first, last) + 1)) selected.value.add(key)
      return
    }
  }
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
  anchor.value = id
}

function togglePage() {
  const remove = allOnPage.value
  for (const photo of photos.value) {
    if (remove) selected.value.delete(photo.id)
    else selected.value.add(photo.id)
  }
}

async function selectMatches() {
  const version = generation
  selecting.value = true
  error.value = ''
  try {
    const { ids } = await get<{ ids: string[] }>('/solar/project/sources', {
      project: props.project, search: appliedSearch.value, ids_only: true,
    })
    if (version === generation) selected.value = new Set([...selected.value, ...ids])
  } catch (e) {
    if (version === generation) error.value = String(e)
  } finally {
    if (version === generation) selecting.value = false
  }
}

watch(() => [props.open, props.project] as const, ([open]) => {
  generation++
  if (!open) return
  search.value = ''
  appliedSearch.value = ''
  photos.value = []
  selected.value.clear()
  failed.value.clear()
  selecting.value = false
  total.value = 0
  void browse(1, true)
}, { immediate: true })
</script>

<template>
  <MDialog :open="open" title="从项目选取照片" :width="800" @close="emit('close')">
    <form class="search" @submit.prevent="searchPhotos">
      <label><Search :size="17" /><input v-model="search" type="search" placeholder="按文件名筛选" aria-label="筛选已有照片"></label>
      <button type="submit" :disabled="loading || selecting">搜索</button>
    </form>
    <div class="selection">
      <span>{{ total.toLocaleString() }} 张照片 · 已选 {{ selected.size.toLocaleString() }} 张</span>
      <div>
        <button :disabled="loading || selecting || !photos.length" @click="togglePage">{{ allOnPage ? '取消本页选择' : '全选本页' }}</button>
        <button v-if="total > pageSize" :disabled="loading || selecting" @click="selectMatches">{{ appliedSearch ? '全选搜索结果' : '全选全部照片' }}</button>
        <button :disabled="!selected.size || selecting" @click="selected.clear()">清除</button>
      </div>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在读取照片…</p>
    <p v-else-if="!photos.length">没有找到符合条件的照片。</p>
    <div v-else class="preview-grid">
      <button v-for="photo in photos" :key="photo.id" class="preview" type="button" :aria-pressed="selected.has(photo.id)" :aria-label="'选择 ' + photo.name" :disabled="selecting" @click="toggle(photo.id, $event)">
        <div class="preview-image">
          <img v-if="!failed.has(photo.id)" :src="thumbnail(photo.id)" loading="lazy" alt="" @error="failed.add(photo.id)">
          <span v-else class="unavailable"><ImageIcon :size="20" /> 暂无缩略图</span>
          <span class="check" :class="{ checked: selected.has(photo.id) }"><Check v-if="selected.has(photo.id)" :size="13" /></span>
        </div>
        <span class="filename" :title="photo.name">{{ photo.name }}</span>
      </button>
    </div>
    <template #footer>
      <div class="footer">
        <div class="pagination">
          <button :disabled="page <= 1 || loading || selecting" @click="browse(page - 1)">上一页</button>
          <small>{{ page }} / {{ pageCount }}</small>
          <button :disabled="page >= pageCount || loading || selecting" @click="browse(page + 1)">下一页</button>
        </div>
        <button class="import" :disabled="!selected.size || selecting" @click="emit('import', [...selected])">导入所选 {{ selected.size.toLocaleString() }} 张</button>
      </div>
    </template>
  </MDialog>
</template>

<style scoped>
.search, .selection, .pagination, .footer { display: flex; align-items: center; gap: 8px; }
.search label { flex: 1; min-width: 0; display: flex; align-items: center; gap: 7px; padding: 8px 10px; color: var(--text-secondary); border: 1px solid var(--border); border-radius: 8px; }
.search input { font: inherit; width: 100%; min-width: 0; color: var(--text-primary); background: transparent; border: 0; outline: 0; font-size: 13px; }
.search label:focus-within { border-color: var(--accent); }
button { font: inherit; cursor: pointer; font-size: 12px; color: var(--text-primary); border: 1px solid var(--border); border-radius: 7px; background: var(--bg-elevated); padding: 7px 9px; }
button:disabled { opacity: .45; cursor: default; }
.selection { justify-content: space-between; flex-wrap: wrap; margin: 12px 0; color: var(--text-secondary); font-size: 12px; }
.selection > div { display: flex; gap: 5px; flex-wrap: wrap; }
.preview-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(112px, 1fr)); gap: 9px; overflow: auto; max-height: 53vh; padding: 2px; }
.preview { display: grid; padding: 0; overflow: hidden; min-width: 0; text-align: left; }
.preview[aria-pressed="true"] { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.preview-image { aspect-ratio: 1.2; position: relative; display: grid; place-items: center; background: #10171c; min-width: 0; }
.preview-image img { position: absolute; width: 100%; height: 100%; object-fit: contain; }
.check { position: absolute; top: 6px; left: 6px; width: 19px; height: 19px; display: grid; place-items: center; color: #fff; background: #111b; border: 1px solid #b4c2d2; border-radius: 4px; }
.check.checked { background: #198187; border-color: #49b2b6; }
.filename { padding: 7px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unavailable { display: grid; place-items: center; gap: 4px; color: #a0afbd; font-size: 11px; }
.footer { width: 100%; justify-content: space-between; flex-wrap: wrap; }
.pagination small { color: var(--text-secondary); }
.import { background: #186a70; border-color: #33868a; color: white; padding-inline: 13px; }
.error { color: var(--danger); }
</style>
