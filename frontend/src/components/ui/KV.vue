<script setup lang="ts">
withDefaults(defineProps<{
  entries: { key: string; value: string | number | null | undefined; mono?: boolean }[]
  dense?: boolean
}>(), { dense: false })
</script>

<template>
  <dl class="kv" :class="{ 'is-dense': dense }">
    <template v-for="entry in entries" :key="entry.key">
      <dt class="kv__key">{{ entry.key }}</dt>
      <dd class="kv__value" :class="{ mono: entry.mono !== false }">
        <slot :name="`value-${entry.key}`">{{ entry.value ?? '—' }}</slot>
      </dd>
    </template>
  </dl>
</template>

<style scoped>
.kv {
  display: grid; grid-template-columns: minmax(64px, auto) 1fr;
  gap: var(--sp-1) var(--sp-3); margin: 0;
}
.kv.is-dense { gap: 2px var(--sp-2); }
.kv__key { font-size: var(--fs-ui); color: var(--text-secondary); }
.kv__value {
  margin: 0; font-size: var(--fs-ui); color: var(--text-primary);
  font-family: var(--font-mono); word-break: break-all;
}
</style>
