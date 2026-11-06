<script setup lang="ts" generic="R extends object">
import { computed, ref } from 'vue'
import { ArrowUp, ArrowDown, ChevronsUpDown } from 'lucide-vue-next'
import MCheckbox from './MCheckbox.vue'

export interface TableColumn<R> {
  key: keyof R & string
  label: string
  width?: string
  align?: 'left' | 'right'
  sortable?: boolean
  mono?: boolean
}

const props = withDefaults(defineProps<{
  rows: R[]
  columns: TableColumn<R>[]
  rowKey: (row: R) => string
  selectable?: boolean
  selectedKeys?: Set<string>
  activeKey?: string
  emptyText?: string
}>(), { selectable: false, emptyText: '暂无数据' })

const emit = defineEmits<{
  'update:selectedKeys': [keys: Set<string>]
  'row-click': [row: R]
}>()

const sortKey = ref('')
const sortAsc = ref(true)

const sorted = computed(() => {
  if (!sortKey.value) return props.rows
  const col = props.columns.find(c => c.key === sortKey.value)
  if (!col) return props.rows
  const factor = sortAsc.value ? 1 : -1
  const key = sortKey.value as keyof R
  return [...props.rows].sort((a, b) => {
    const va = a[key]
    const vb = b[key]
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * factor
    return String(va ?? '').localeCompare(String(vb ?? '')) * factor
  })
})

const allSelected = computed(() =>
  sorted.value.length > 0 && sorted.value.every(r => props.selectedKeys?.has(props.rowKey(r))))

function toggleSort(key: string) {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = true }
}

function toggleAll() {
  const next = new Set(props.selectedKeys || [])
  if (allSelected.value) for (const r of sorted.value) next.delete(props.rowKey(r))
  else for (const r of sorted.value) next.add(props.rowKey(r))
  emit('update:selectedKeys', next)
}

function toggleRow(row: R) {
  const next = new Set(props.selectedKeys || [])
  const key = props.rowKey(row)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  emit('update:selectedKeys', next)
}
</script>

<template>
  <div class="tablewrap">
    <table class="mtable">
      <thead>
        <tr>
          <th v-if="selectable" class="mtable__check">
            <MCheckbox :model-value="allSelected" aria-label="全选" @update:model-value="toggleAll" />
          </th>
          <th v-for="col in columns" :key="col.key"
            :style="col.width ? { width: col.width } : undefined"
            :class="{ 'is-right': col.align === 'right', 'is-sortable': col.sortable }"
            :aria-sort="sortKey === col.key ? (sortAsc ? 'ascending' : 'descending') : undefined"
            @click="col.sortable && toggleSort(col.key)">
            <span class="mtable__th">
              {{ col.label }}
              <template v-if="col.sortable">
                <ArrowUp v-if="sortKey === col.key && sortAsc" :size="12" aria-hidden="true" />
                <ArrowDown v-else-if="sortKey === col.key" :size="12" aria-hidden="true" />
                <ChevronsUpDown v-else :size="12" class="is-idle" aria-hidden="true" />
              </template>
            </span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in sorted" :key="rowKey(row)"
          :class="{ 'is-active': activeKey && rowKey(row) === activeKey }"
          @click="emit('row-click', row)">
          <td v-if="selectable" class="mtable__check" @click.stop>
            <MCheckbox :model-value="selectedKeys?.has(rowKey(row)) ?? false"
              aria-label="选择行" @update:model-value="toggleRow(row)" />
          </td>
          <td v-for="col in columns" :key="col.key"
            :class="{ 'is-right': col.align === 'right', mono: col.mono, num: col.align === 'right' }">
            <slot :name="`cell-${col.key}`" :row="row">{{ row[col.key] ?? '—' }}</slot>
          </td>
        </tr>
        <tr v-if="!sorted.length">
          <td class="mtable__empty" :colspan="columns.length + (selectable ? 1 : 0)">
            {{ emptyText }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.tablewrap { overflow: auto; min-height: 0; }
.mtable { width: 100%; border-collapse: collapse; font-size: var(--fs-ui); }
.mtable thead th {
  position: sticky; top: 0; z-index: 1;
  background: var(--bg-raised); color: var(--text-secondary);
  font-size: var(--fs-ui); font-weight: 500; text-align: left;
  padding: 0 var(--sp-2); height: 28px; white-space: nowrap;
  border-bottom: 1px solid var(--border);
}
.mtable thead th.is-right { text-align: right; }
.mtable thead th.is-sortable { cursor: pointer; user-select: none; }
.mtable thead th.is-sortable:hover { color: var(--text-primary); }
.mtable__th { display: inline-flex; align-items: center; gap: 2px; }
.is-idle { color: var(--text-muted); }
.mtable tbody td {
  padding: 0 var(--sp-2); height: 32px;
  border-bottom: 1px solid var(--border);
  color: var(--text-primary); max-width: 320px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mtable tbody td.is-right { text-align: right; }
.mtable tbody tr:hover td { background: var(--bg-hover); }
.mtable tbody tr { cursor: default; }
.mtable tbody tr.is-active td { background: var(--accent-soft); }
.mtable__check { width: 28px; text-align: center; }
.mtable__empty { text-align: center; color: var(--text-muted); padding: var(--sp-4); }
</style>
