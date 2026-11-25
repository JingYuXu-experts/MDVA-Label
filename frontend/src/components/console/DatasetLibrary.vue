<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowRight, Database, Folder, FolderOpen, Plus, RefreshCw, Search } from 'lucide-vue-next'
import MButton from '@/components/ui/MButton.vue'
import MInput from '@/components/ui/MInput.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MSkeleton from '@/components/ui/MSkeleton.vue'
import type { DatasetRow } from '@/api/types'

const props = defineProps<{ datasets: DatasetRow[]; loading: boolean; opening: string; error: string }>()
const emit = defineEmits<{ open: [id: string]; create: []; import: []; refresh: [] }>()
const search = ref('')
const sort = ref('size')
const types: Record<string, string> = { segmentation: '图像分割', detection: '目标检测', classification: '图像分类', mixed: '混合数据', series_interval: '时序区间', event_level: '事件等级' }
// 旧目录型数据集的列表接口返回 n_items=0，但并未扫描目录；不能据此认定它为空。
const countKnown = (dataset: DatasetRow) => Boolean(dataset.meta?.indexed) || dataset.n_items > 0
const totalItems = computed(() => props.datasets.reduce((sum, dataset) => sum + (countKnown(dataset) ? dataset.n_items : 0), 0))
const countedCount = computed(() => props.datasets.filter(countKnown).length)
const filtered = computed(() => {
  const needle = search.value.trim().toLocaleLowerCase()
  return props.datasets.filter(dataset => !needle || `${dataset.name} ${types[dataset.type] || dataset.type}`.toLocaleLowerCase().includes(needle))
    .sort((a, b) => sort.value === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : sort.value === 'recent' ? (b.created || 0) - (a.created || 0) : b.n_items - a.n_items || a.name.localeCompare(b.name, 'zh-CN'))
})
const populated = computed(() => filtered.value.filter(dataset => countKnown(dataset) && dataset.n_items > 0))
const other = computed(() => filtered.value.filter(dataset => !countKnown(dataset) || dataset.n_items === 0))
function dateLabel(created: number | null) {
  return created ? new Date(created * 1000).toLocaleDateString('zh-CN') : '未记录日期'
}
</script>

<template>
  <div class="library">
    <header class="library__header">
      <div><p class="library__eyebrow">本地数据管理</p><h1>数据集</h1><p class="library__intro">先选择一个数据集，再浏览其中的图像与标注。</p></div>
      <div class="library__actions">
        <MButton variant="ghost" :loading="loading" title="刷新数据集" @click="emit('refresh')"><RefreshCw :size="15" /> 刷新</MButton>
        <MButton variant="secondary" @click="emit('create')"><Plus :size="15" /> 新建数据集</MButton>
        <MButton variant="primary" @click="emit('import')"><Database :size="15" /> 导入太阳数据</MButton>
      </div>
    </header>

    <section class="library__summary" aria-label="数据集总览">
      <div><strong>{{ datasets.length }}</strong><span>个数据集</span></div>
      <div><strong>{{ countedCount }}</strong><span>个已统计</span></div>
      <div><strong>{{ totalItems.toLocaleString() }}</strong><span>条已统计记录</span></div>
      <p>每个数据集独立打开，图像、标注与处理版本按数据集管理。样本集可能重复收录图像；目录型数据集在打开后读取。</p>
    </section>

    <div class="library__filters">
      <div class="library__search"><Search :size="16" aria-hidden="true" /><MInput v-model="search" placeholder="搜索数据集名称或类型" aria-label="搜索数据集" /></div>
      <span class="library__result">{{ filtered.length }} 个数据集</span>
      <MSelect v-model="sort" :options="[{value:'size',label:'按条目数量'}, {value:'recent',label:'按导入时间'}, {value:'name',label:'按名称'}]" width="140px" aria-label="数据集排序" />
    </div>

    <p v-if="error" class="library__error" role="alert">{{ error }}<MButton size="sm" @click="emit('refresh')">重试</MButton></p>
    <MSkeleton v-if="loading && !datasets.length" :show="true" variant="grid" :count="3" />
    <div v-else-if="!datasets.length" class="library__empty"><FolderOpen :size="36" /><h2>从第一个数据集开始</h2><p>导入太阳数据，或新建数据集接入自己的图像。导入后会在这里生成独立卡片。</p><MButton variant="primary" @click="emit('create')">新建数据集</MButton></div>
    <div v-else-if="!filtered.length" class="library__empty"><Search :size="30" /><h2>没有找到匹配的数据集</h2><p>试试其他名称，或清除搜索条件。</p><MButton @click="search=''">清除搜索</MButton></div>
    <section v-else class="library__content" aria-label="数据集列表">
      <div class="dataset-grid">
        <article v-for="dataset in populated" :key="dataset.id" class="dataset-card" :aria-label="dataset.name">
          <div class="dataset-card__top"><span class="dataset-card__icon"><Folder :size="24" /></span><span class="dataset-card__type">{{ types[dataset.type] || dataset.type }}</span><span v-if="dataset.meta?.indexed" class="dataset-card__source">只读数据源</span></div>
          <h2>{{ dataset.name }}</h2>
          <p class="dataset-card__description">{{ dataset.meta?.indexed ? '太阳结构图像与参考标注 · AR / CH / FL' : '独立管理图像、标注与处理记录' }}</p>
          <div class="dataset-card__count"><strong>{{ dataset.n_items.toLocaleString() }}</strong><span>条目</span></div>
          <div class="dataset-card__meta"><span>{{ dataset.n_versions }} 个版本</span><span>{{ dataset.head_annotations.toLocaleString() }} 条当前标注</span></div>
          <footer><span>{{ dateLabel(dataset.created) }} 导入</span><MButton variant="secondary" :disabled="!!opening" :loading="opening===dataset.id" :title="`打开数据集：${dataset.name}`" @click="emit('open',dataset.id)">打开数据集 <ArrowRight :size="14" /></MButton></footer>
        </article>
      </div>
      <details v-if="other.length" class="empty-datasets" :open="!!search.trim() || !populated.length">
        <summary>目录与空数据集 <span>{{ other.length }}</span><small>展开选择</small></summary>
        <div v-for="dataset in other" :key="dataset.id" class="empty-datasets__row"><Folder :size="18" /><div><strong>{{ dataset.name }}</strong><span>{{ types[dataset.type] || dataset.type }} · {{ countKnown(dataset) ? '0 条目' : '条目数量：打开后统计' }} · {{ dataset.n_versions }} 个版本 · {{ dateLabel(dataset.created) }}</span></div><MButton size="sm" :disabled="!!opening" :loading="opening===dataset.id" :title="`打开数据集：${dataset.name}`" @click="emit('open',dataset.id)">打开</MButton></div>
      </details>
    </section>
  </div>
</template>

<style scoped>
.library { flex:1; min-height:0; overflow:auto; padding:32px clamp(20px,4vw,64px); }
.library__header { display:flex; align-items:center; justify-content:space-between; gap:24px; flex-wrap:wrap; }
.library__eyebrow { margin:0 0 8px; color:var(--accent); font-size:12px; }
h1 { margin:0; font-size:28px; font-weight:650; letter-spacing:-.5px; }
.library__intro { color:var(--text-secondary); margin:10px 0 0; font-size:13px; }
.library__actions { display:flex; gap:10px; flex-wrap:wrap; }
.library__summary { display:flex; gap:32px; align-items:center; flex-wrap:wrap; margin:28px 0; padding:20px 24px; background:var(--bg-panel); border:1px solid var(--border); border-radius:10px; }
.library__summary>div { display:flex; align-items:baseline; gap:8px; }
.library__summary strong { font-size:22px; font-variant-numeric:tabular-nums; font-weight:600; }
.library__summary span { font-size:12px; color:var(--text-secondary); }
.library__summary p { flex:1; min-width:200px; margin:0; line-height:1.7; font-size:12px; color:var(--text-muted); }
.library__filters { display:flex; align-items:center; gap:16px; margin-bottom:20px; }
.library__search { display:flex; align-items:center; gap:8px; color:var(--text-muted); width:320px; max-width:55%; }
.library__search :deep(.field) { flex:1; }
.library__result { margin-left:auto; color:var(--text-muted); font-size:12px; }
.dataset-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,310px),1fr)); gap:20px; }
.dataset-card { display:flex; flex-direction:column; min-width:0; padding:22px; border:1px solid var(--border); border-radius:12px; background:var(--bg-panel); transition:border-color .15s; }
.dataset-card:hover { border-color:var(--border-strong); }
.dataset-card__top { display:flex; align-items:center; gap:10px; }
.dataset-card__icon { display:flex; padding:10px; border-radius:10px; background:var(--accent-soft); color:var(--accent); }
.dataset-card__type { font-size:12px; color:var(--text-secondary); }
.dataset-card__source { margin-left:auto; padding:3px 7px; border:1px solid var(--border); border-radius:4px; color:var(--text-secondary); font-size:10px; }
.dataset-card h2 { margin:20px 0 8px; font-size:16px; line-height:1.6; overflow-wrap:anywhere; }
.dataset-card__description { margin:0; color:var(--text-muted); font-size:12px; line-height:1.7; }
.dataset-card__count { display:flex; align-items:baseline; gap:8px; margin:24px 0 12px; }
.dataset-card__count strong { font-size:32px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-1px; }
.dataset-card__count span { font-size:12px; color:var(--text-secondary); }
.dataset-card__meta { display:flex; gap:18px; color:var(--text-secondary); font-size:12px; margin-bottom:24px; }
.dataset-card footer { display:flex; justify-content:space-between; align-items:center; gap:12px; padding-top:16px; margin-top:auto; border-top:1px solid var(--border); }
.dataset-card footer>span { color:var(--text-muted); font-size:11px; }
.empty-datasets { margin-top:26px; border-top:1px solid var(--border); }
.empty-datasets summary { cursor:pointer; padding:18px 0; font-size:13px; color:var(--text-secondary); }
.empty-datasets summary span { margin-left:6px; }
.empty-datasets summary small { margin-left:14px; color:var(--text-muted); }
.empty-datasets__row { display:flex; gap:14px; align-items:center; padding:14px 0; border-top:1px solid var(--border); color:var(--text-secondary); }
.empty-datasets__row>div { display:flex; flex:1; flex-direction:column; gap:6px; }
.empty-datasets__row strong { font-size:13px; font-weight:500; }
.empty-datasets__row span { color:var(--text-muted); font-size:11px; }
.library__empty { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:16px; padding:70px 24px; text-align:center; color:var(--text-secondary); }
.library__empty h2,.library__empty p { margin:0; }
.library__empty h2 { font-size:18px; }.library__empty p { font-size:13px; max-width:460px; line-height:1.8; }
.library__error { display:flex; gap:12px; align-items:center; color:var(--danger); font-size:13px; }
@media(max-width:640px) { .library { padding:24px 16px; }.library__summary { gap:18px; padding:16px; }.library__filters { gap:8px; flex-wrap:wrap; }.library__search { width:100%; max-width:none; }.library__result { margin-left:0; margin-right:auto; }.dataset-card { padding:18px; } }
</style>
