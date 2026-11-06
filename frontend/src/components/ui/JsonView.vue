<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ data: unknown }>()

const text = computed(() => {
  try {
    return JSON.stringify(props.data, null, 2)
  } catch {
    return String(props.data)
  }
})
</script>

<template>
  <pre class="jsonview mono">{{ text }}</pre>
</template>

<style scoped>
.jsonview {
  margin: 0; padding: var(--sp-2);
  background: var(--bg-base); border: 1px solid var(--border);
  border-radius: var(--radius-control);
  font-size: var(--fs-xs); color: var(--text-secondary);
  overflow: auto; max-height: 320px; white-space: pre-wrap; word-break: break-all;
}
</style>
