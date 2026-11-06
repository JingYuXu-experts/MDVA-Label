<script setup lang="ts">
withDefaults(defineProps<{
  /** 0-100；undefined/负数 = indeterminate（未知总量，长任务用） */
  value?: number
  label?: string
}>(), { value: -1, label: '' })
</script>

<template>
  <div class="progress" role="progressbar" :aria-label="label || '进度'"
    :aria-valuenow="value >= 0 ? value : undefined">
    <div class="progress__track">
      <div class="progress__bar" :class="{ 'is-indeterminate': value < 0 }"
        :style="value >= 0 ? { width: value + '%' } : undefined" />
    </div>
  </div>
</template>

<style scoped>
.progress { width: 100%; }
.progress__track { height: 3px; background: var(--bg-raised); border-radius: 2px; overflow: hidden; }
.progress__bar { height: 100%; background: var(--accent); border-radius: 2px; transition: width 0.2s; }
.progress__bar.is-indeterminate {
  width: 40%;
  animation: slide 1.2s ease-in-out infinite;
}
@keyframes slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(350%); }
}
</style>
