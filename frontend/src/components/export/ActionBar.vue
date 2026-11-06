<script setup lang="ts">
/**
 * 动作（§5.5 Step④）：导出 + 回读校验 + 结果与差异明细。
 *
 * 导出是同步长请求（§3.2-5）：按钮 loading + indeterminate Progress + 可取消；
 * 校验结论只转述后端 ok/issues，前端不做"这算不算问题"的判断。
 */
import { computed } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MProgress from '@/components/ui/MProgress.vue'
import MTag from '@/components/ui/MTag.vue'
import StatCard from '@/components/ui/StatCard.vue'
import JsonView from '@/components/ui/JsonView.vue'
import { useExportStore } from '@/stores/export'
import { useCtxStore } from '@/stores/ctx'

const store = useExportStore()
const ctx = useCtxStore()

const res = computed(() => store.result)
const counts = computed(() => {
  const r = res.value
  if (!r) return []
  const rows: { label: string; value: number | string }[] = []
  if (typeof r.n_images === 'number') rows.push({ label: '图片/样本', value: r.n_images })
  if (typeof r.n_annotations === 'number') rows.push({ label: '标注条数', value: r.n_annotations })
  if (typeof r.n_categories === 'number') rows.push({ label: '类别数', value: r.n_categories })
  if (typeof r.n_skipped === 'number') rows.push({ label: '明确跳过', value: r.n_skipped })
  return rows
})
const scripts = computed(() => store.scriptPaths)
const check = computed(() => store.check)
const issues = computed(() => store.checkIssues)
/** 后端累计的问题条数：明细被 MAX_ISSUES 截断时，计数仍然按真实值说 */
const issueCount = computed(() => store.checkIssueCount)
const truncated = computed(() => issueCount.value > issues.value.length)
const checkedRows = computed(() => Object.entries(check.value?.checked || {}))
/** 落盘路径出站时被后端 relativize 成工作区相对路径，界面如实标注基准，不假装是绝对路径 */
const pathBase = computed(() => {
  const p = String(res.value?.out_dir || '')
  return /^([A-Za-z]:[\\/]|[\\/])/.test(p) ? '绝对路径' : '相对工作区根目录'
})
/** 送出去的参数串原样回显（后端报告里的 params），便于核对"界面填的就是实际用的" */
const sentParams = computed(() => res.value?.params || null)
</script>

<template>
  <div
    class="act"
    data-el="action-bar"
    :data-can-export="String(store.canExport)"
    :data-export-disabled-title="store.exportWhy"
    :data-out-dir="String(res?.out_dir || '')"
    :data-out-dir-base="pathBase"
    :data-validate-ok="String(store.validated?.ok ?? '')"
    :data-validate-issues="String(issueCount)"
    :data-validate-shown="String(issues.length)"
    :data-validate-check-version="String(check?.version || '')"
    :data-validate-check-format="String(check?.format || '')"
    :data-scripts-paths="String(scripts.length)"
    :data-scripts-notes="store.scriptNotes.map(([k, v]) => k + '=' + v).join(' ')"
  >
    <div class="act__row">
      <MButton
        variant="primary"
        :loading="store.running"
        :disabled="!store.canExport"
        :title="store.exportWhy || `把当前版本导成 ${store.chosen}`"
        @click="store.run(ctx.version)"
      >
        导出 {{ store.chosen || '（未选格式）' }}
      </MButton>
      <MButton
        variant="secondary"
        :loading="store.validating"
        :disabled="!store.canValidate"
        :title="store.canValidate ? `回读校验 ${store.validateDir}` : '先导出一次，校验目标目录才存在'"
        @click="store.validate(ctx.version)"
      >
        回读校验
      </MButton>
      <span v-if="res && !store.validated && !store.validating" class="act__suggest">
        建议立即校验：产物与版本对不对，只有回读才算数。
      </span>
    </div>

    <template v-if="store.running">
      <MProgress :value="-1" label="导出进行中" />
      <MButton variant="ghost" size="sm" @click="store.cancelInflight()">取消在途请求</MButton>
      <p class="act__note">取消只是不再等待结果，后端这次导出会继续执行完并落库（契约 §4 同步长任务语义）。</p>
    </template>
    <p v-if="store.runError" class="act__err" role="alert">{{ store.runError }}</p>

    <section v-if="res" class="result">
      <h3 class="result__h">导出结果（{{ res.format }}）</h3>
      <div class="result__path">
        <span class="result__label">落盘路径</span>
        <code class="result__dir mono" :title="res.out_dir">{{ res.out_dir }}</code>
        <MTag kind="neutral" :text="pathBase" />
        <MButton variant="ghost" size="sm" title="复制落盘路径" @click="store.copyPath(String(res.out_dir))">
          复制
        </MButton>
      </div>
      <div class="result__stats">
        <StatCard
          v-for="c in counts"
          :key="c.label"
          :label="c.label"
          :value="c.value"
          hint="取自导出报告"
        />
        <StatCard v-if="res.files" label="产物项" :value="res.files.length" :hint="res.files.join(' · ')" />
      </div>
      <p class="result__meta num">
        版本 {{ res.version }}（op={{ res.op }}）· 数据集 {{ res.dataset }} · 任务留痕 {{ res.task_id }}
      </p>
      <div v-if="scripts.length" class="scripts">
        <h4 class="scripts__h">生成的训练脚本与数据卡（离线可跑的证据入口）</h4>
        <ul class="scripts__list">
          <li v-for="[name, path] in scripts" :key="name" class="scripts__item">
            <MTag kind="success" :text="name" />
            <code class="scripts__path mono" :title="String(path)">{{ path }}</code>
            <MButton variant="ghost" size="sm" :aria-label="`复制 ${name} 路径`" @click="store.copyPath(String(path))">
              复制
            </MButton>
          </li>
        </ul>
        <p v-if="store.scriptNotes.length" class="scripts__notes">
          同一字段里的非路径描述：
          <code v-for="[k, v] in store.scriptNotes" :key="k" class="mono">{{ k }}={{ v }}</code>
          ——这几个不是文件位置，界面不把它们列成可复制的路径。
        </p>
      </div>
      <details v-if="sentParams" class="result__more">
        <summary class="result__summary">送出去的实际参数（后端报告回显）</summary>
        <JsonView :data="sentParams" />
      </details>
    </section>

    <section v-if="store.validated || store.validateError" class="validate">
      <h3 class="validate__h">回读校验</h3>
      <p v-if="store.validateError" class="act__err" role="alert">{{ store.validateError }}</p>
      <template v-else-if="store.validated">
        <div class="validate__head">
          <StatCard
            label="一致性"
            :value="store.validated.ok ? 'ok = true' : 'ok = false'"
            :tone="store.validated.ok ? 'success' : 'danger'"
            hint="结论来自后端 POST /api/export/validate"
          />
          <StatCard
            label="差异条数"
            :value="issueCount"
            :tone="issueCount ? 'danger' : 'success'"
            :hint="issueCount ? '后端累计计数（明细可能被 50 条上限截断）' : '后端 issues 为空'"
          />
        </div>
        <p v-if="store.validated.summary" class="validate__summary mono">
          {{ store.validated.summary }}
        </p>
        <p class="validate__path mono" :title="check?.path">
          校验对象 {{ check?.path }}（格式 {{ check?.format }} · 版本 {{ check?.version }}）
        </p>
        <p v-if="truncated" class="validate__trunc" role="note">
          后端只回传前 {{ issues.length }} 条明细，实际累计 {{ issueCount }} 条——条数以下方数字为准，别把明细当全量。
        </p>
        <ul v-if="issues.length" class="validate__issues" role="list">
          <li v-for="(it, i) in issues" :key="i" class="validate__issue">{{ it }}</li>
        </ul>
        <p v-else class="validate__ok">后端未报任何差异：写出的产物与该版本的库内数据对得上。</p>
        <details v-if="checkedRows.length" class="validate__more">
          <summary class="result__summary">对账明细（后端 checked / counts 原文）</summary>
          <JsonView :data="{ checked: check?.checked, counts: check?.counts }" />
        </details>
      </template>
    </section>
  </div>
</template>

<style scoped>
.act { display: flex; flex-direction: column; gap: var(--sp-3); }
.act__row { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.act__suggest { font-size: var(--fs-xs); color: var(--warning); }
.act__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.55; }
.act__err { margin: 0; font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.result, .validate {
  display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--radius-panel); background: var(--bg-raised);
}
.result__h, .validate__h { margin: 0; font-size: var(--fs-md); color: var(--text-primary); }
.result__path { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.result__label { font-size: var(--fs-xs); color: var(--text-secondary); }
.result__dir {
  padding: 3px var(--sp-2); border: 1px solid var(--border-strong); border-radius: var(--radius-control);
  background: var(--bg-base); color: var(--accent); font-size: var(--fs-xs);
  max-width: 620px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.result__stats { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.result__meta { font-size: var(--fs-xs); color: var(--text-muted); }
.result__more, .validate__more { display: flex; flex-direction: column; gap: var(--sp-2); }
.result__summary { font-size: var(--fs-xs); color: var(--text-secondary); cursor: pointer; }
.scripts { display: flex; flex-direction: column; gap: var(--sp-2); }
.scripts__h { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); }
.scripts__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.scripts__notes { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.6; }
.scripts__notes code { color: var(--text-secondary); margin-right: var(--sp-2); }
.scripts__item { display: flex; align-items: center; gap: var(--sp-2); }
.scripts__path {
  font-size: var(--fs-xs); color: var(--text-primary); max-width: 560px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.validate__head { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.validate__summary {
  margin: 0; font-size: var(--fs-xs); color: var(--text-secondary);
  padding: var(--sp-2); border-left: 2px solid var(--accent); background: var(--bg-base);
  white-space: pre-wrap; word-break: break-all;
}
.validate__trunc { margin: 0; font-size: var(--fs-xs); color: var(--warning); }
.validate__path { font-size: var(--fs-xs); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.validate__issues { margin: 0; padding-left: var(--sp-5); display: flex; flex-direction: column; gap: 4px; }
.validate__issue { font-size: var(--fs-xs); line-height: 1.55; color: var(--danger); word-break: break-all; }
.validate__ok { margin: 0; font-size: var(--fs-xs); color: var(--success); }
</style>
