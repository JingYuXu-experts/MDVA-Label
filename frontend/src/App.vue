<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import TopBar from '@/components/layout/TopBar.vue'
import TaskDrawer from '@/components/layout/TaskDrawer.vue'
import ToastHost from '@/components/ui/ToastHost.vue'
import MButton from '@/components/ui/MButton.vue'
import { useCtxStore } from '@/stores/ctx'
import { TOKEN_INVALID_EVENT } from '@/api/client'

const ctx = useCtxStore()
const tokenInvalid = ref(false)
const bootError = ref('')

function onTokenInvalid() {
  tokenInvalid.value = true
}

async function boot() {
  bootError.value = ''
  try {
    await ctx.refresh()
  } catch (err) {
    bootError.value = err instanceof Error ? err.message : String(err)
  }
}

onMounted(() => {
  window.addEventListener(TOKEN_INVALID_EVENT, onTokenInvalid)
  ctx.startHealthPolling()
  void boot()
})

onBeforeUnmount(() => {
  window.removeEventListener(TOKEN_INVALID_EVENT, onTokenInvalid)
  ctx.stopHealthPolling()
})
</script>

<template>
  <!-- D7：401 全屏阻断页（UI规格 §3.2-3），不静默重试 -->
  <div v-if="tokenInvalid" class="blocker">
    <div class="blocker__box">
      <h1 class="blocker__title">会话令牌无效</h1>
      <p class="blocker__text">请从 mdva serve 打开的入口重新访问。</p>
    </div>
  </div>
  <div v-else class="app">
    <TopBar />
    <main class="app__main">
      <div v-if="bootError" class="boot-error">
        <p>初始化失败：{{ bootError }}</p>
        <MButton variant="primary" @click="boot">重试</MButton>
      </div>
      <RouterView v-else />
    </main>
    <TaskDrawer />
    <ToastHost />
  </div>
</template>

<style scoped>
.app { display: flex; flex-direction: column; height: 100%; }
.app__main { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.blocker {
  height: 100%; display: flex; align-items: center; justify-content: center;
  background: var(--bg-base);
}
.blocker__box {
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius-panel); padding: var(--sp-6); text-align: center;
}
.blocker__title { margin: 0 0 var(--sp-2); font-size: var(--fs-lg); }
.blocker__text { margin: 0; color: var(--text-secondary); }
.boot-error {
  margin: auto; padding: var(--sp-5); text-align: center; color: var(--text-secondary);
  display: flex; flex-direction: column; gap: var(--sp-3); align-items: center;
}
</style>
