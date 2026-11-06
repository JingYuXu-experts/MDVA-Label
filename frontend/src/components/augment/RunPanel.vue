<script setup lang="ts">
/**
 * 右 300px 执行与谱系（§5.4）：RunButton（告警检查门控）+ 确认 Dialog 复述告警原文
 * + 谱系逐条展开（B 级：真 before/after 双联 + 逐条抽样参数链）+ 复现校验结果。
 *
 * 措辞红线（rev6）：replay 校验的是 seed 谱系链，不是重算像素，
 * 界面只说"参数链逐条一致/不一致"，不出现"逐字节一致"。
 */
import { computed, ref } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MDialog from '@/components/ui/MDialog.vue'
import MTag from '@/components/ui/MTag.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MProgress from '@/components/ui/MProgress.vue'
import { frameUrl } from '@/api/endpoints'
import { useAugmentStore } from '@/stores/augment'
import { useCtxStore } from '@/stores/ctx'
import { useTasksStore } from '@/stores/tasks'

const store = useAugmentStore()
const ctx = useCtxStore()
const tasks = useTasksStore()

const confirmRun = ref(false)

const chainText = computed(() => store.chain.map(c => c.name).join(' → '))
const runWhy = computed(() => {
  if (store.running) return '正在执行'
  if (!store.hasRecipe) return '先选配方或勾选算子'
  if (!store.scope.length) return '当前版本没有条目'
  if (!store.checkRan) return '先执行告警检查'
  return ''
})
/** 复现校验只对增强产出的版本有意义：判据取版本 op，不由前端猜 */
const isAugmentVersion = computed(() =>
  String(ctx.versions.find(v => v.id === ctx.version)?.op || '').startsWith('augment'))
const replayVerdict = computed(() => {
  const r = store.replay
  if (!r) return null
  return { ok: r.identical === true, text: r.identical ? '参数链逐条一致 ✓' : '参数链逐条不一致 ✗' }
})
const diffRows = computed(() => store.replay?.diff || [])

function toggle(idx: number) { store.toggleRow(idx) }
function bytesOf(n?: number): string {
  if (!n) return '—'
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(2)} MB` : `${Math.round(n / 1024)} KB`
}
async function doRun() {
  confirmRun.value = false
  const v = await store.run(ctx.version, ctx.role)
  if (!v) return
  await ctx.loadVersions()
  ctx.setVersion(v)
}
async function doReplay() {
  await store.runReplay(ctx.version)
}
</script>

<template>
  <aside
    class="run"
    aria-label="增强执行与谱系"
    :data-can-run="String(store.canRun)"
    :data-run-disabled-title="runWhy"
    :data-lineage-rows="String(store.lineage.length)"
    :data-lineage-of="store.lineageOf"
    :data-replay-identical="String(store.replay?.identical ?? '')"
    :data-replay-diff="String(diffRows.length)"
  >
    <div class="run__block">
      <MButton
        variant="primary"
        :loading="store.running"
        :disabled="!store.canRun"
        :title="runWhy || `用 ${chainText} 增强当前版本`"
        @click="confirmRun = true"
      >
        执行增强
      </MButton>
      <p v-if="!store.checkRan && store.hasRecipe" class="run__hint">
        执行前必须先过告警检查——改一次参数或选一个配方就会自动跑。
      </p>
      <p v-if="store.runError" class="run__err" role="alert">{{ store.runError }}</p>
      <template v-if="store.running">
        <MProgress :value="-1" label="增强执行中" />
        <MButton variant="ghost" size="sm" @click="store.cancelInflight()">取消在途请求</MButton>
      </template>
      <p class="run__note">
        原件与父版本一律不动：增强只产新版本，标注按几何链映射过去
        （本次继承 {{ store.lastRun?.n_inherited ?? 0 }} 条）。
      </p>
    </div>

    <div class="run__block">
      <h3 class="run__h">
        谱系
        <MTag v-if="store.lineageOf" kind="neutral" :text="store.lineageOf" />
      </h3>
      <MEmpty v-if="store.lineageError" :text="`谱系读不出来：${store.lineageError}`" />
      <MEmpty
        v-else-if="!store.lineage.length"
        :text="isAugmentVersion ? '这个增强版本没有在册谱系（数据库里没有产出记录）' : '当前版本不是增强产出的：执行增强后自动列出'"
      />
      <ul v-else class="lin" role="list">
        <li v-for="(row, idx) in store.lineage" :key="`${row.source}-${row.rep}`">
          <button
            type="button"
            class="lin__row"
            :class="{ 'is-open': store.openRow === idx }"
            :aria-expanded="store.openRow === idx"
            @click="toggle(idx)"
          >
            <span class="lin__src mono" :title="row.source">{{ row.source }}</span>
            <MTag kind="modality" :text="String(row.modality || '')" />
            <span class="lin__rep num">r{{ row.rep }}</span>
            <span class="lin__bytes num">{{ bytesOf(row.bytes) }}</span>
          </button>
          <div v-if="store.openRow === idx" class="lin__detail">
            <!-- B 级：真 before/after 双联，两帧都走 /api/frame -->
            <div class="pair">
              <figure class="pair__cell">
                <img class="pair__img" :src="frameUrl(row.source)" :alt="`原件：${row.source}`" loading="lazy">
                <figcaption class="pair__cap">
                  before<span class="mono">{{ row.source_shape ? ` ${row.source_shape.join('×')}` : '' }}</span>
                </figcaption>
              </figure>
              <figure class="pair__cell">
                <img class="pair__img" :src="frameUrl(row.written)" :alt="`增强副本：${row.written}`" loading="lazy">
                <figcaption class="pair__cap">
                  after<span class="mono">{{ bytesOf(row.bytes) }}</span>
                </figcaption>
              </figure>
            </div>
            <p class="pair__after mono" :title="row.written">{{ row.written }}</p>
            <h4 class="lin__h4">逐条抽样参数链（服务端按 seed 抽的实际值）</h4>
            <ol class="lchain">
              <li v-for="(c, i) in row.chain || []" :key="i" class="lchain__item mono">
                {{ i + 1 }}. {{ c.op }} {{ JSON.stringify(c.params || {}) }}
              </li>
            </ol>
            <p v-if="row.aux && Object.keys(row.aux).length" class="lin__aux mono">
              派生文件：{{ Object.keys(row.aux).join(', ') }}
            </p>
          </div>
        </li>
      </ul>
    </div>

    <div class="run__block">
      <h3 class="run__h">复现校验</h3>
      <MButton
        variant="secondary"
        :loading="store.replaying"
        :disabled="!isAugmentVersion || store.replaying"
        :title="isAugmentVersion ? `按版本里存的 seed 重放参数链并逐条比对` : '先切到一个增强产出的版本'"
        @click="doReplay"
      >
        重放参数链
      </MButton>
      <p v-if="store.replayError" class="run__err" role="alert">{{ store.replayError }}</p>
      <div v-if="replayVerdict" class="replay" :class="replayVerdict.ok ? 'is-ok' : 'is-bad'">
        <span class="replay__verdict">{{ replayVerdict.text }}</span>
        <span class="replay__meta num">
          重放 {{ store.replay?.n_jobs ?? 0 }} 条 / 在册 {{ store.replay?.n_stored ?? 0 }} 条
        </span>
        <p class="replay__note">
          像素级一致性由渲染确定性另测，不在本次重放校验范围。
        </p>
        <ul v-if="diffRows.length" class="replay__diff">
          <li v-for="(d, i) in diffRows" :key="i" class="replay__diffrom mono">
            {{ d.uri }} r{{ d.rep }} — {{ d.why }}
          </li>
        </ul>
      </div>
      <MEmpty v-else-if="!store.replaying && isAugmentVersion" text="还没跑过复现校验" />
      <p v-if="tasks.inflight.length" class="run__inflight num">
        在途长任务 {{ tasks.inflight.length }} 个（见右下方任务抽屉）
      </p>
    </div>

    <MDialog :open="confirmRun" title="确认执行增强？" @close="confirmRun = false">
      <p class="dlg__line">
        将产<b>新版本</b>并写出副本文件；<b>原件与父版本一条都不动</b>，标注按几何链映射过去。
      </p>
      <p class="dlg__line num">算子链：{{ chainText }}</p>
      <p class="dlg__line num">
        seed {{ store.seed }} · 每张原件产 {{ store.factor }} 个副本 ·
        参与条目 {{ store.limit ? Math.min(store.limit, store.scope.length) : store.scope.length }} 条
      </p>
      <p v-if="store.warnings.length" class="dlg__warn">
        告警检查有 {{ store.warnings.length }} 条提示（后端原文，执行前请读一遍）：
      </p>
      <ul v-if="store.warnings.length" class="dlg__list">
        <li v-for="(w, i) in store.warnings" :key="i">{{ w }}</li>
      </ul>
      <p v-else class="dlg__ok">告警检查无提示，可以放心跑。</p>
      <template #footer>
        <MButton variant="ghost" @click="confirmRun = false">取消</MButton>
        <MButton variant="primary" @click="doRun">执行</MButton>
      </template>
    </MDialog>
  </aside>
</template>

<style scoped>
.run {
  width: 300px; flex: 0 0 300px; display: flex; flex-direction: column; gap: var(--sp-4);
  padding: var(--sp-4); overflow-y: auto; background: var(--bg-panel);
  border-left: 1px solid var(--border);
}
.run__block { display: flex; flex-direction: column; gap: var(--sp-2); }
.run__h {
  margin: 0; font-size: var(--fs-md); color: var(--text-primary);
  display: flex; align-items: center; gap: var(--sp-2);
}
.run__hint, .run__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.55; }
.run__err { margin: 0; font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.lin { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.lin__row {
  width: 100%; display: flex; align-items: center; gap: var(--sp-2); text-align: left;
  padding: 6px var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-control);
  background: var(--bg-raised); color: var(--text-primary); font-size: var(--fs-xs); cursor: pointer;
}
.lin__row:hover { background: var(--bg-hover); }
.lin__row.is-open { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent-soft); }
.lin__src { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lin__rep, .lin__bytes { font-family: var(--font-mono); color: var(--text-muted); }
.lin__detail {
  margin: 4px 0 var(--sp-2); padding: var(--sp-2); border: 1px solid var(--border);
  border-radius: var(--radius-control); background: var(--bg-base);
  display: flex; flex-direction: column; gap: var(--sp-2);
}
.lin__h4 { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); font-weight: 600; }
.lin__aux { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); }
.pair__cell { margin: 0; display: flex; flex-direction: column; gap: 4px; }
.pair__img {
  width: 100%; height: 96px; object-fit: contain; background: #000;
  border: 1px solid var(--border-strong); border-radius: var(--radius-control);
}
.pair__cap { font-size: var(--fs-xs); color: var(--text-secondary); display: flex; justify-content: space-between; gap: 4px; }
.pair__cap .mono { color: var(--text-muted); }
.pair__after {
  margin: 0; font-size: var(--fs-xs); color: var(--text-muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.lchain { margin: 0; padding-left: var(--sp-4); display: flex; flex-direction: column; gap: 2px; }
.lchain__item { font-size: var(--fs-xs); color: var(--text-secondary); word-break: break-all; }
.replay {
  display: flex; flex-direction: column; gap: 4px; padding: var(--sp-2);
  border-radius: var(--radius-control); border: 1px solid var(--border);
}
.replay.is-ok { border-color: var(--success); }
.replay.is-bad { border-color: var(--danger); }
.replay__verdict { font-size: var(--fs-md); font-weight: 600; }
.replay.is-ok .replay__verdict { color: var(--success); }
.replay.is-bad .replay__verdict { color: var(--danger); }
.replay__meta { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-secondary); }
.replay__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; }
.replay__diff { margin: 0; padding-left: var(--sp-4); font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.replay__diffrom { list-style: none; }
.run__inflight { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.dlg__line { margin: 0 0 var(--sp-2); font-size: var(--fs-md); line-height: 1.6; }
.dlg__line b { color: var(--accent); }
.dlg__warn { margin: 0 0 var(--sp-1); font-size: var(--fs-md); color: var(--warning); }
.dlg__ok { margin: 0; font-size: var(--fs-md); color: var(--success); }
.dlg__list { margin: 0; padding-left: var(--sp-5); display: flex; flex-direction: column; gap: 4px; }
.dlg__list li { font-size: var(--fs-xs); line-height: 1.55; color: var(--text-primary); }
</style>
