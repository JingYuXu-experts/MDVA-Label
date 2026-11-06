<script setup lang="ts">
withDefaults(defineProps<{
  label: string
  value: number | string
  hint?: string
  tone?: 'default' | 'success' | 'warning' | 'danger' | 'accent'
}>(), { hint: '', tone: 'default' })
</script>

<template>
  <div class="statcard">
    <span class="statcard__value num" :class="`is-${tone}`">{{ value }}</span>
    <span class="statcard__label">{{ label }}</span>
    <span v-if="hint" class="statcard__hint">{{ hint }}</span>
  </div>
</template>

<style scoped>
.statcard {
  display: flex; flex-direction: column; gap: 2px;
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius-panel); min-width: 88px;
}
.statcard__value { font-size: 20px; line-height: 1.2; color: var(--text-primary); }
.statcard__value.is-success { color: var(--success); }
.statcard__value.is-warning { color: var(--warning); }
.statcard__value.is-danger { color: var(--danger); }
.statcard__value.is-accent { color: var(--accent); }
.statcard__label { font-size: var(--fs-xs); color: var(--text-secondary); }
.statcard__hint { font-size: var(--fs-xs); color: var(--text-muted); }
</style>
