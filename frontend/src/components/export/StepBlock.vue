<script setup lang="ts">
/**
 * Step 区块壳：编号 + 标题 + 说明 + 完成态标记。
 * §5.5 要求"单列表单流（四个 Step 区块纵向排列，非向导弹窗）"，所以步骤状态只做视觉提示，
 * 不做强制串行——用户改任何一步都不该被弹窗挡住。
 */
withDefaults(defineProps<{
  no: string
  title: string
  note?: string
  done?: boolean
  warn?: string
  /**
   * 提示的分量（rev8 §5.5）：拦截态（不发请求、按钮禁用）用 danger，
   * 提醒态（信息已如实呈现、不拦动作）用 warning。默认 warning。
   */
  tone?: 'warning' | 'danger'
}>(), { note: '', done: false, warn: '', tone: 'warning' })
</script>

<template>
  <section class="step" :class="{ 'is-done': done }" :data-tone="warn ? tone : ''">
    <header class="step__head">
      <span class="step__no num" :class="{ 'is-done': done }">{{ done ? '✓' : no }}</span>
      <h2 class="step__title">{{ title }}</h2>
      <span v-if="warn" class="step__warn" :class="`step__warn--${tone}`" role="status">{{ warn }}</span>
    </header>
    <p v-if="note" class="step__note">{{ note }}</p>
    <div class="step__body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.step {
  display: flex; flex-direction: column; gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5); border-bottom: 1px solid var(--border);
}
.step.is-done { background: var(--bg-panel); }
.step__head { display: flex; align-items: center; gap: var(--sp-3); }
.step__no {
  flex: 0 0 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--border-strong); border-radius: 50%; font-family: var(--font-mono);
  font-size: var(--fs-xs); color: var(--text-secondary);
}
.step__no.is-done { border-color: var(--success); color: var(--success); }
.step__title { margin: 0; font-size: var(--fs-lg); color: var(--text-primary); font-weight: 600; }
.step__warn { margin-left: auto; font-size: var(--fs-xs); }
.step__warn--warning { color: var(--warning); }
/* 拦截态与滑杆下方的 .split__sum.is-bad 同色同量级，避免同一句话两种分量（rev8 §5.5） */
.step__warn--danger { color: var(--danger); font-weight: 600; }
.step__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.55; }
.step__body { display: flex; flex-direction: column; gap: var(--sp-3); }
</style>
