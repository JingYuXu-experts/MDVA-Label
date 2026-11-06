<script setup lang="ts">
import { computed } from 'vue'
import { Cpu, HardDrive, Layers } from 'lucide-vue-next'
import { useBackendsStore } from '@/stores/backends'

/**
 * 顶部条：本机能力（GET /api/probe）。推荐分的"延迟预算"按档位取值，
 * 所以档位必须常驻可见——换一个机器，同一张实测表的结论就不同。
 */
const store = useBackendsStore()

const tierLabel = computed(() => (store.probe ? store.probe.tier : ''))
const cells = computed(() => {
  const p = store.probe
  if (!p) return []
  return [
    { icon: Cpu, key: 'CPU', val: `${p.cpu_count} 核`, sub: p.machine },
    { icon: HardDrive, key: '内存', val: `${p.ram_gb} GB`, sub: `空闲磁盘 ${p.free_disk_gb} GB` },
    { icon: Layers, key: '显卡', val: p.has_gpu ? '检测到' : '无（本平台不接入需权重后端）', sub: `Python ${p.python}` },
  ]
})
</script>

<template>
  <div class="probep" data-testid="probe-bar">
    <p v-if="store.probeError" class="probep__err" role="alert" data-probe="failed">
      能力探测失败：{{ store.probeError }}
    </p>
    <span v-else-if="tierLabel" class="probep__tier num" data-probe="ok">{{ tierLabel }}</span>
    <span v-else class="probep__tier probep__tier--unknown num" data-probe="unknown">档位未知</span>
    <div v-for="c in cells" :key="c.key" class="probep__cell">
      <component :is="c.icon" :size="13" class="probep__icon" aria-hidden="true" />
      <span class="probep__k">{{ c.key }}</span>
      <span class="probep__v num">{{ c.val }}</span>
      <span class="probep__sub">{{ c.sub }}</span>
    </div>
    <span v-if="store.probe?.note" class="probep__note">{{ store.probe.note }}</span>
    <span class="probep__spacer" />
    <span class="probep__count num" :data-n-registered="store.probe?.registered?.length ?? 0">
      已注册 {{ store.probe?.registered?.length ?? 0 }} 个后端
    </span>
  </div>
</template>

<style scoped>
.probep {
  display: flex; align-items: center; gap: var(--sp-4); flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-4); background: var(--bg-panel);
  border-bottom: 1px solid var(--border); font-size: var(--fs-xs);
}
.probep__tier {
  padding: 2px var(--sp-2); border: 1px solid var(--accent); border-radius: var(--radius-control);
  color: var(--accent); font-weight: 600; letter-spacing: .04em;
}
.probep__tier--unknown { border-color: var(--border-strong); color: var(--text-secondary); }
/* 探测请求本身失败＝失败态（danger + 后端原文），不得降级成"档位未知"那句 neutral */
.probep__err {
  margin: 0; font-size: var(--fs-xs); color: var(--danger);
  font-variant-numeric: tabular-nums; word-break: break-all;
}
.probep__cell { display: flex; align-items: baseline; gap: 6px; }
.probep__icon { color: var(--text-muted); align-self: center; }
.probep__k { color: var(--text-muted); }
.probep__v { color: var(--text-primary); font-variant-numeric: tabular-nums; }
.probep__sub { color: var(--text-muted); }
.probep__note { color: var(--text-secondary); }
.probep__spacer { flex: 1; }
.probep__count { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
</style>
