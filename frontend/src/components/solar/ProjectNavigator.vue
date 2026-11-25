<script setup lang="ts">
import { computed, ref } from 'vue'
import { FolderOpen, Plus, Search, Star } from 'lucide-vue-next'
import type { Project } from './projectTypes'

const props = defineProps<{ projects: Project[]; current: string; busy: boolean }>()
const emit = defineEmits<{ open: [id: string]; pin: [project: Project]; create: [] }>()
const query = ref('')
const filter = ref<'all' | 'recent' | 'frequent'>('all')
const filters = [
  { value: 'all', label: '全部' },
  { value: 'recent', label: '最近' },
  { value: 'frequent', label: '常用' },
] as const

function recent(a: Project, b: Project) {
  return (b.opened_at || '').localeCompare(a.opened_at || '') || a.name.localeCompare(b.name, 'zh-CN')
}
const matching = computed(() => props.projects.filter(p => p.name.toLowerCase().includes(query.value.trim().toLowerCase())))
const pinned = computed(() => matching.value.filter(p => p.pinned).sort(recent))
const unpinned = computed(() => {
  const projects = matching.value.filter(p => !p.pinned && (filter.value === 'all' || p.opens > 0))
  return projects.sort((a, b) => filter.value === 'frequent' ? b.opens - a.opens || recent(a, b) : recent(a, b))
})
const sections = computed(() => [
  { name: '收藏置顶', list: pinned.value },
  { name: filter.value === 'all' ? '所有项目' : filter.value === 'recent' ? '最近打开' : '最常用', list: unpinned.value },
])
</script>

<template>
  <nav class="project-nav" aria-label="图像项目">
    <div class="heading">
      <h2>图像项目 <span>{{ projects.length }}</span></h2>
      <button class="new-project" type="button" :disabled="busy" @click="emit('create')"><Plus :size="15" /> 新建项目</button>
    </div>
    <label class="search"><Search :size="15" /><input v-model="query" type="search" placeholder="搜索项目" aria-label="搜索项目"></label>
    <div class="filters" aria-label="项目排序">
      <button v-for="tab in filters" :key="tab.value" type="button" :aria-pressed="tab.value === filter" @click="filter = tab.value">{{ tab.label }}</button>
    </div>
    <div class="project-list">
      <section v-for="group in sections.filter(g => g.list.length)" :key="group.name" :aria-label="group.name">
        <h3>{{ group.name }}</h3>
        <div v-for="p in group.list" :key="p.id" class="project-entry" :class="{ current: current === p.id }">
          <button class="open-project" type="button" :disabled="busy" :aria-current="current === p.id ? 'true' : undefined" @click="emit('open', p.id)">
            <FolderOpen :size="17" /><span><strong :title="p.name">{{ p.name }}</strong><small v-if="p.opens">{{ p.opens }} 次打开</small></span>
          </button>
          <button class="pin-project" type="button" :aria-pressed="p.pinned" :aria-label="(p.pinned ? '取消置顶 ' : '置顶 ') + p.name" :title="p.pinned ? '取消置顶' : '收藏置顶'" @click="emit('pin', p)">
            <Star :size="16" :fill="p.pinned ? 'currentColor' : 'none'" />
          </button>
        </div>
      </section>
      <small v-if="!pinned.length && !unpinned.length" class="empty">{{ query.trim() ? '没有匹配的项目' : filter === 'all' ? '暂无项目' : '打开项目后将在这里显示' }}</small>
    </div>
  </nav>
</template>

<style scoped>
.project-nav { display: grid; gap: 12px; min-width: 0; }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
h2 { margin: 0; font-size: 16px; white-space: nowrap; }
h2 span { color: var(--text-secondary); font-size: 11px; font-weight: 400; }
button { cursor: pointer; font: inherit; color: var(--text-primary); }
button:disabled { cursor: default; opacity: .5; }
.new-project { display: inline-flex; align-items: center; gap: 3px; flex: none; padding: 6px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-elevated); font-size: 12px; }
.search { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); border: 1px solid var(--border); border-radius: 7px; padding: 7px 8px; }
.search input { width: 100%; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--text-primary); font-size: 12px; }
.search:focus-within { border-color: var(--accent); }
.filters { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; padding: 3px; border-radius: 8px; background: var(--bg-base); }
.filters button { border: 0; background: transparent; padding: 5px 3px; border-radius: 5px; font-size: 12px; }
.filters button[aria-pressed="true"] { background: var(--bg-elevated); color: var(--accent); font-weight: 600; }
.project-list { display: grid; gap: 12px; max-height: 42vh; overflow: auto; scrollbar-width: thin; }
h3 { margin: 0 0 6px; color: var(--text-secondary); font-size: 12px; font-weight: 500; }
.project-entry { display: flex; align-items: stretch; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 5px; overflow: hidden; background: var(--bg-panel); }
.project-entry.current { border-color: var(--accent); background: var(--accent-soft, var(--bg-elevated)); }
.open-project { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; padding: 8px 5px 8px 8px; border: 0; background: transparent; text-align: left; }
.open-project svg { flex: none; color: var(--accent); }
.open-project span { display: grid; gap: 3px; min-width: 0; }
strong { font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
small { color: var(--text-secondary); font-size: 11px; }
.pin-project { background: transparent; color: var(--text-secondary); padding: 0 7px; border: 0; border-left: 1px solid var(--border); }
.pin-project[aria-pressed="true"] { color: #e7b95c; }
.open-project:hover, .pin-project:hover { background: var(--bg-hover); }
.open-project:focus-visible, .pin-project:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (max-width: 950px) { .project-list { max-height: 22vh; } }
</style>
