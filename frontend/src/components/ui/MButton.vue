<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  loading?: boolean
  disabled?: boolean
  type?: 'button' | 'submit'
  title?: string
}>(), { variant: 'secondary', size: 'md', loading: false, disabled: false, type: 'button' })

const cls = computed(() => [
  'btn', `btn--${props.variant}`, `btn--${props.size}`,
  { 'is-loading': props.loading, 'is-disabled': props.disabled || props.loading },
])
</script>

<template>
  <button :class="cls" :type="type" :disabled="disabled || loading" :title="title"
    :aria-busy="loading || undefined" :aria-label="title">
    <Loader2 v-if="loading" class="btn__spinner" :size="14" aria-hidden="true" />
    <span class="btn__content"><slot /></span>
  </button>
</template>

<style scoped>
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-1);
  border-radius: var(--radius-control);
  border: 1px solid transparent;
  cursor: pointer;
  font-size: var(--fs-ui);
  white-space: nowrap;
  transition: background-color 0.12s, border-color 0.12s;
}
.btn--md { height: 28px; padding: 0 var(--sp-3); }
.btn--sm { height: 24px; padding: 0 var(--sp-2); }

.btn--primary { background: var(--accent); color: #FFFFFF; }
.btn--primary:hover:not(.is-disabled) { background: var(--accent-hover); }
.btn--primary:active:not(.is-disabled) { background: var(--accent-hover); filter: brightness(0.92); }

.btn--secondary { background: var(--bg-raised); border-color: var(--border); color: var(--text-primary); }
.btn--secondary:hover:not(.is-disabled) { background: var(--bg-hover); border-color: var(--border-strong); }
.btn--secondary:active:not(.is-disabled) { background: var(--bg-hover); filter: brightness(0.95); }

.btn--ghost { background: transparent; color: var(--text-secondary); }
.btn--ghost:hover:not(.is-disabled) { background: var(--bg-hover); color: var(--text-primary); }
.btn--ghost:active:not(.is-disabled) { background: var(--bg-hover); }

.btn--danger { background: transparent; border-color: var(--danger); color: var(--danger); }
.btn--danger:hover:not(.is-disabled) { background: var(--danger); color: #FFFFFF; }
.btn--danger:active:not(.is-disabled) { background: var(--danger); filter: brightness(0.9); }

.is-disabled { color: var(--text-muted); cursor: not-allowed; opacity: 0.7; }
.btn--primary.is-disabled { background: var(--bg-raised); color: var(--text-muted); }
.btn--danger.is-disabled { border-color: var(--border); }

.btn__spinner { animation: spin 0.9s linear infinite; flex: none; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
