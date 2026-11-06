<script setup lang="ts">
import { ref, watch } from 'vue'
import MDialog from '@/components/ui/MDialog.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import { useCtxStore } from '@/stores/ctx'
import { useToast } from '@/stores/toast'
import * as api from '@/api/endpoints'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const ctx = useCtxStore()
const toast = useToast()

const v1 = ref('')
const v2 = ref('')
const diff = ref<Record<string, unknown> | null>(null)
const lineage = ref<Record<string, unknown>[]>([])
const loading = ref(false)

const versionOpts = () => ctx.versions.map((v) => ({ value: v.id, label: `${v.op} · ${v.id.slice(1, 7)}` }))

watch(() => props.open, (o) => {
  if (o) {
    v1.value = ctx.versions.length > 1 ? ctx.versions[ctx.versions.length - 2].id : (ctx.version || '')
    v2.value = ctx.version || ctx.head || ''
    run()
  }
})

async function run(): Promise<void> {
  if (!v1.value || !v2.value) return
  loading.value = true
  diff.value = null
  try {
    const [d, l] = await Promise.all([api.getDiff(v1.value, v2.value), api.getLineage(v2.value)])
    diff.value = d
    lineage.value = l.lineage
  } catch (e) {
    toast.danger(`对比失败：${e instanceof Error ? e.message : String(e)}`)
  } finally { loading.value = false }
}

function list(name: string): Record<string, unknown>[] {
  const v = diff.value?.[name]
  return Array.isArray(v) ? v as Record<string, unknown>[] : []
}
</script>

<template>
  <MDialog :open="props.open" title="版本对比" :width="640" @close="emit('close')">
    <div class="cmp__bar">
      <MSelect :model-value="v1" :options="versionOpts()" label="父版本 v1" width="200px" @update:model-value="v1 = String($event)" />
      <span class="cmp__arrow">→</span>
      <MSelect :model-value="v2" :options="versionOpts()" label="子版本 v2" width="200px" @update:model-value="v2 = String($event)" />
      <MButton size="sm" variant="secondary" :loading="loading" @click="run">对比</MButton>
    </div>
    <nav v-if="lineage.length" class="cmp__crumbs" aria-label="父链">
      <span v-for="(v, i) in lineage" :key="i" class="cmp__crumb mono">
        {{ (v.op as string) || (v.id as string || '').slice(1, 7) }}<span v-if="i < lineage.length - 1"> › </span>
      </span>
    </nav>
    <div v-if="diff" class="cmp__grid">
      <section class="cmp__col is-add"><h4>新增 {{ list('added').length }}</h4>
        <ul><li v-for="(r, i) in list('added')" :key="i" class="mono">{{ r.uri || r.item_uri || r.id }}</li></ul></section>
      <section class="cmp__col is-del"><h4>删除 {{ list('removed').length }}</h4>
        <ul><li v-for="(r, i) in list('removed')" :key="i" class="mono">{{ r.uri || r.item_uri || r.id }}</li></ul></section>
      <section class="cmp__col is-chg"><h4>修改 {{ list('changed').length }}</h4>
        <ul><li v-for="(r, i) in list('changed')" :key="i" class="mono">{{ r.uri || r.item_uri || r.id }}</li></ul></section>
    </div>
    <p v-else-if="!loading" class="cmp__hint">选择两个版本后点“对比”。三色列表来自 GET /api/diff。</p>
    <template #footer>
      <MButton variant="ghost" @click="emit('close')">关闭</MButton>
    </template>
  </MDialog>
</template>

<style scoped>
.cmp__bar { display: flex; align-items: flex-end; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.cmp__arrow { color: var(--text-muted); padding-bottom: 6px; }
.cmp__crumbs { display: flex; flex-wrap: wrap; gap: 2px; font-size: var(--fs-xs); color: var(--text-secondary); margin-bottom: var(--sp-3); }
.cmp__grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sp-2); }
.cmp__col { border: 1px solid var(--border); border-radius: var(--radius-panel); padding: var(--sp-2); min-height: 60px; }
.cmp__col h4 { margin: 0 0 var(--sp-1); font-size: var(--fs-ui); }
.cmp__col.is-add { border-top: 2px solid var(--success); }
.cmp__col.is-del { border-top: 2px solid var(--danger); }
.cmp__col.is-chg { border-top: 2px solid var(--warning); }
.cmp__col ul { margin: 0; padding-left: var(--sp-4); font-size: var(--fs-xs); color: var(--text-secondary); max-height: 160px; overflow: auto; }
.cmp__hint { font-size: var(--fs-ui); color: var(--text-muted); }
</style>
