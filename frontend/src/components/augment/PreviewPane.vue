<script setup lang="ts">
/**
 * 中央预览（§5.4 rev6 A 级）：告警条 + 示例条目 before 帧 + 配方层参数链。
 *
 * 分级口径的实现要点：
 * - 执行前没有 after——增强结果只在 run 落盘后才存在，界面不放占位假图、不臆造效果；
 *   逐条抽样参数也只在执行后的谱系里（rev6 实测接口面）。
 * - 真 before/after 双联在右栏谱系行展开处（B 级），两帧都走 /api/frame。
 * - 告警原文一律照后端 warnings 逐条显示，不改写、不概括。
 */
import { computed, ref } from 'vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MButton from '@/components/ui/MButton.vue'
import MTag from '@/components/ui/MTag.vue'
import { frameUrl } from '@/api/endpoints'
import { useAugmentStore } from '@/stores/augment'

const store = useAugmentStore()

const frameBroken = ref(false)
const exampleIdx = computed(() => store.scope.indexOf(store.exampleUri))
const exampleSrc = computed(() => store.exampleUri ? frameUrl(store.exampleUri) : '')
/** 换示例条目只重置图片加载态，不改配方（配方是唯一真源，示例只是看一眼原件） */
function step(d: number) {
  if (!store.scope.length) return
  const cur = exampleIdx.value < 0 ? 0 : exampleIdx.value
  const next = Math.min(store.scope.length - 1, Math.max(0, cur + d))
  frameBroken.value = false
  store.setExample(store.scope[next])
}
const pad = (n: number) => String(n).padStart(2, '0')
const checkedAtText = computed(() => {
  if (!store.checkedAt) return ''
  const d = new Date(store.checkedAt)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})
</script>

<template>
  <section
    class="apreview"
    aria-label="增强预览"
    data-level="A"
    :data-example="store.exampleUri"
    :data-warnings="String(store.warnings.length)"
    :data-check-ran="String(store.checkRan)"
    :data-checking="String(store.checking)"
    :data-chain-ops="store.chain.map(c => c.name).join(',')"
    :data-checked-at="String(store.checkedAt)"
    data-mask-lossless="1"
  >
    <div v-if="store.warnings.length" class="warnbar" role="alert">
      <h3 class="warnbar__h">这类数据这么增强会出问题（{{ store.warnings.length }} 条，后端原文）</h3>
      <ul class="warnbar__list">
        <li v-for="(w, i) in store.warnings" :key="i">{{ w }}</li>
      </ul>
      <p class="warnbar__foot">告警不禁止执行，但确认框会把这些话原样复述给你。</p>
    </div>

    <div class="apreview__bar">
      <div class="apreview__title">
        <h3 class="apreview__h">示例条目 before 帧</h3>
        <span v-if="store.exampleUri" class="apreview__uri mono" :title="store.exampleUri">{{ store.exampleUri }}</span>
      </div>
      <div class="apreview__nav">
        <MButton variant="ghost" size="sm" :disabled="exampleIdx <= 0" title="上一条原件" @click="step(-1)">←</MButton>
        <span class="apreview__count num">{{ store.scope.length ? exampleIdx + 1 : 0 }} / {{ store.scope.length }}</span>
        <MButton variant="ghost" size="sm" :disabled="exampleIdx >= store.scope.length - 1" title="下一条原件" @click="step(1)">→</MButton>
      </div>
    </div>

    <div v-if="!store.hasRecipe" class="apreview__body">
      <MEmpty text="先选一个配方，或到「自定义算子」勾选算子——没有算子的配方后端不会产出任何副本" />
    </div>
    <div v-else class="apreview__body">
      <div class="before">
        <img
          v-if="exampleSrc && !frameBroken"
          class="before__img"
          :src="exampleSrc"
          :alt="`原件帧：${store.exampleUri}`"
          loading="lazy"
          @error="frameBroken = true"
        >
        <p v-else-if="frameBroken" class="before__fail" role="alert">
          这帧渲染失败（条目可能已不在当前版本，或后端渲染报错）：{{ store.exampleUri }}
        </p>
        <p v-else class="before__fail">当前版本没有可显示的条目</p>
        <p class="before__cap">
          只有 before——增强后的图要在执行落盘后才有，右栏谱系行点开就是真 before/after 双联。
        </p>
      </div>

      <h3 class="apreview__h2">配方参数链（执行前）</h3>
      <table class="chain">
        <caption class="sr-only">配方层的算子与参数</caption>
        <thead>
          <tr><th scope="col">顺序</th><th scope="col">算子</th><th scope="col">参数</th></tr>
        </thead>
        <tbody>
          <tr v-for="(c, i) in store.chain" :key="c.name">
            <td class="num">{{ i + 1 }}</td>
            <th scope="row" class="mono">{{ c.name }}</th>
            <td class="chain__params mono">{{ c.params || '（用默认值）' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="apreview__note">
        这是<b>配方值</b>（会原样发给后端）；每条副本实际抽到的参数（比如这次转了几格、裁掉多少）
        由服务端按 seed 抽样，执行后可在右栏谱系逐条查。
      </p>

      <div class="constraint">
        <MTag kind="accent" text="引擎约束" />
        <p class="constraint__text">
          几何算子只走整数索引：<b>掩膜不插值、标签无损</b>——掩膜与图像经过完全相同的索引链，
          类别编号不会被插值糊成第三类；光度扰动对掩膜是恒等变换。
        </p>
      </div>

      <p v-if="store.checking" class="apreview__status" role="status">告警检查进行中…</p>
      <p v-else-if="store.checkError" class="apreview__status is-err" role="alert">
        告警检查失败：{{ store.checkError }}
      </p>
      <p v-else-if="!store.checkRan" class="apreview__status">
        还没跑告警检查：改一次参数或选一个配方就会自动跑（执行前必须先过检查）。
      </p>
      <p v-else class="apreview__status">
        告警检查已完成{{ checkedAtText ? `（${checkedAtText}）` : '' }}，
        {{ store.warnings.length ? `有 ${store.warnings.length} 条提示` : '无提示' }}，可以执行。
      </p>
    </div>
  </section>
</template>

<style scoped>
.apreview { flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
.warnbar {
  margin: 0; padding: var(--sp-3) var(--sp-5); background: rgba(217, 160, 63, 0.12);
  border-bottom: 1px solid var(--warning);
}
.warnbar__h { margin: 0 0 var(--sp-2); font-size: var(--fs-md); color: var(--warning); }
.warnbar__list { margin: 0; padding-left: var(--sp-5); display: flex; flex-direction: column; gap: 4px; }
.warnbar__list li { font-size: var(--fs-xs); line-height: 1.55; color: var(--text-primary); }
.warnbar__foot { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); }
.apreview__bar {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-5); border-bottom: 1px solid var(--border); background: var(--bg-raised);
}
.apreview__title { display: flex; align-items: baseline; gap: var(--sp-3); min-width: 0; }
.apreview__h { margin: 0; font-size: var(--fs-md); color: var(--text-primary); white-space: nowrap; }
.apreview__uri { color: var(--text-secondary); font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.apreview__nav { display: flex; align-items: center; gap: var(--sp-2); }
.apreview__count { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); }
.apreview__body { flex: 1; min-height: 0; overflow-y: auto; padding: var(--sp-4) var(--sp-5); }
.apreview__h2 { margin: var(--sp-5) 0 var(--sp-2); font-size: var(--fs-md); color: var(--text-primary); }
.before { display: flex; flex-direction: column; gap: var(--sp-2); }
.before__img {
  max-width: 100%; max-height: 340px; border: 1px solid var(--border-strong);
  border-radius: var(--radius-control); background: #000; object-fit: contain;
}
.before__fail { margin: 0; font-size: var(--fs-xs); color: var(--warning); }
.before__cap { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; }
.chain { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.chain th, .chain td { text-align: left; padding: 6px var(--sp-2); border-bottom: 1px solid var(--border); }
.chain thead th { color: var(--text-muted); font-weight: 500; }
.chain tbody th { color: var(--text-primary); font-weight: 500; }
.chain__params { color: var(--text-secondary); word-break: break-all; }
.apreview__note { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.6; }
.apreview__note b { color: var(--text-secondary); }
.constraint {
  display: flex; align-items: flex-start; gap: var(--sp-3); margin-top: var(--sp-4);
  padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-raised);
}
.constraint__text { margin: 0; font-size: var(--fs-xs); line-height: 1.6; color: var(--text-secondary); }
.constraint__text b { color: var(--text-primary); }
.apreview__status { margin: var(--sp-4) 0 0; font-size: var(--fs-xs); color: var(--text-muted); }
.apreview__status.is-err { color: var(--danger); }
.sr-only {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
</style>
