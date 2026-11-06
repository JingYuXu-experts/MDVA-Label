<script setup lang="ts">
/**
 * 选项（§5.5 Step③）：scripts 开关 + out_dir 只读显示。
 * out_dir 不给输入框：界面显示的就是 GET /api/export/formats 回的默认落盘根，
 * 导出后以报告里的 out_dir 为准（真正写出去的路径），两处都不允许前端臆造。
 */
import MSwitch from '@/components/ui/MSwitch.vue'
import MButton from '@/components/ui/MButton.vue'
import { useExportStore } from '@/stores/export'

const store = useExportStore()
</script>

<template>
  <div class="opts" data-el="options-row" :data-scripts="String(store.scripts)">
    <MSwitch
      :model-value="store.scripts"
      label="顺带生成 train.py 与 DATASET.md"
      @update:model-value="store.scripts = Boolean($event)"
    />
    <p class="opts__note">
      开关打开时后端会在导出目录里一并写 train.py / train.bat / DATASET.md，
      路径在下面"导出结果"里逐条列出——这是离线可跑的交付证据入口。
    </p>

    <div class="opts__dir">
      <span class="opts__label">落盘根（后端返回值，只读）</span>
      <code class="opts__path mono" :title="store.defaultOutDir">{{ store.defaultOutDir || '（未读到）' }}</code>
      <MButton
        variant="ghost"
        size="sm"
        :disabled="!store.defaultOutDir"
        title="复制落盘根路径"
        @click="store.copyPath(store.defaultOutDir)"
      >
        复制
      </MButton>
    </div>
    <p class="opts__note">
      每种格式的实际目录是<code class="mono">&lt;落盘根&gt;/&lt;数据集&gt;-&lt;版本&gt;-&lt;格式&gt;</code>，
      由后端生成并在导出报告里回显，界面上不重复拼一遍以免对不上。
    </p>
  </div>
</template>

<style scoped>
.opts { display: flex; flex-direction: column; gap: var(--sp-3); }
.opts__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.55; max-width: 640px; }
.opts__note code { color: var(--text-secondary); }
.opts__dir { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.opts__label { font-size: var(--fs-xs); color: var(--text-secondary); }
.opts__path {
  padding: 3px var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-control);
  background: var(--bg-base); color: var(--text-primary); font-size: var(--fs-xs);
  max-width: 560px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
