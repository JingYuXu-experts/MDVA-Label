<script setup lang="ts">
import { Inbox } from 'lucide-vue-next'
import MButton from './MButton.vue'

withDefaults(defineProps<{
  text: string
  actionLabel?: string
  icon?: unknown
}>(), { text: '', actionLabel: '', icon: undefined })

const emit = defineEmits<{ action: [] }>()
</script>

<template>
  <div class="empty">
    <component :is="icon || Inbox" :size="32" class="empty__icon" aria-hidden="true" />
    <p class="empty__text">{{ text }}</p>
    <MButton v-if="actionLabel" variant="secondary" size="sm" @click="emit('action')">
      {{ actionLabel }}
    </MButton>
    <slot />
  </div>
</template>

<style scoped>
.empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: var(--sp-2); padding: var(--sp-6) var(--sp-4); text-align: center;
}
.empty__icon { color: var(--text-muted); }
.empty__text { margin: 0; color: var(--text-secondary); font-size: var(--fs-ui); max-width: 320px; }
</style>
