<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink,useRoute } from 'vue-router'
import { useCtxStore } from '@/stores/ctx'
const ctx=useCtxStore(),route=useRoute()
const stages=[['/console?browse=items','素材管理'],['/annotate','人工标注'],['/clean','质量清洗'],['/augment','图像增强'],['/export','结果导出']]
const name=computed(()=>ctx.currentDataset?.name||'尚未选择数据集')
</script>
<template><nav class="workflow" aria-label="数据处理流程"><div class="context"><b>{{name}}</b><span>当前版本 {{ctx.version.slice(0,9)||'未选择'}}</span></div><div class="steps"><RouterLink v-for="([path,label],i) in stages" :key="path" :to="path" :class="{current:route.path===path.split('?')[0]}" :aria-current="route.path===path.split('?')[0]?'step':undefined"><i>{{i+1}}</i>{{label}}</RouterLink></div></nav></template>
<style scoped>
.workflow{flex:none;display:flex;align-items:center;gap:20px;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--bg-panel);font-size:11px}.context{display:flex;gap:8px;min-width:0;flex-direction:column}.context>b{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:320px}.context>span{color:var(--text-muted)}.steps{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.steps a{display:flex;gap:6px;align-items:center;color:var(--text-muted);text-decoration:none;padding:6px;border-radius:4px}.steps a.current{color:var(--accent);background:var(--bg-raised)}.steps i{font-style:normal;border:1px solid var(--border);border-radius:50%;width:18px;height:18px;display:grid;place-items:center;font-size:10px}@media(max-width:1050px){.workflow{flex-direction:column;align-items:flex-start;gap:6px}.context{flex-direction:row}.steps{gap:2px}.steps a{padding:4px;font-size:10px}.context>b{max-width:250px}}
</style>
