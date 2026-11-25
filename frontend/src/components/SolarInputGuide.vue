<script setup lang="ts">
import {onMounted,onBeforeUnmount,ref,computed} from 'vue'
import {get} from '@/api/client'
defineProps<{ expanded?: boolean }>()
const available=ref<string[]>([]),loaded=ref(false),failed=ref(false)
const controller=new AbortController()
const modelStatus=computed(()=>failed.value?'模型状态读取失败':!loaded.value?'正在读取模型状态':available.value.length?`${available.value.join(' / ')}研究候选可用 · FL未启用`:'AR/CH研究候选未就绪 · FL未启用')
onMounted(async()=>{try{const r=await get<{models:{category:string;available:boolean}[]}>('/solar/models',undefined,{signal:controller.signal});available.value=r.models.filter(m=>m.available).map(m=>m.category);loaded.value=true}catch{if(!controller.signal.aborted)failed.value=true}})
onBeforeUnmount(()=>controller.abort())

const tasks = [
  { id: 'AR', name: '太阳活动区', input: 'SDO/HMI 全日面视向磁图', output: '活动区的区域与边界', extension: 'SOHO/MDI 等磁图需另行验证；NOAA 编号需要观测时间与目录匹配。' },
  { id: 'CH', name: '冕洞', input: 'SDO/AIA 193 Å 全日面极紫外图像', output: '冕洞的区域与边界', extension: 'SOHO/EIT 195 Å 等来源需另行适配；AIA 211 Å 可作为后续联合输入。' },
  { id: 'FL', name: '日面暗条', input: 'Hα 线心全日面图像，如 BBSO、KSO、HSOS', output: '日面暗条的区域与边界', extension: '不同台站需验证预处理与效果；AIA 304 Å 可作联合输入，暂不作为单图暗条识别入口。' },
]
</script>

<template>
  <details class="input-guide" :open="expanded">
    <summary>
      <strong>图像输入说明</strong>
      <span>磁图 → AR · AIA 193 Å → CH · Hα → FL</span>
      <span class="model-status">{{modelStatus}}</span>
    </summary>
    <div class="input-guide__body">
      <p>单张或批量未标注图像无需提供掩膜、坐标或训练数据集。AR/CH研究候选入口位于标注页，模型必须匹配观测条件；生成后需人工核对、修订并保存。FL尚未启用。</p>
      <div class="input-guide__table">
        <table>
          <caption>输入与识别对象对应表（模型可用状态及输入限制分别核验）</caption>
          <thead><tr><th scope="col">识别对象</th><th scope="col">应提供的观测图像</th><th scope="col">预期输出</th><th scope="col">扩展与关联</th></tr></thead>
          <tbody><tr v-for="task in tasks" :key="task.id"><th scope="row">{{ task.id }} · {{ task.name }}</th><td>{{ task.input }}</td><td>{{ task.output }}</td><td>{{ task.extension }}</td></tr></tbody>
        </table>
      </div>
      <p>观测类型优先由仪器、波段等可靠元数据确认；信息不足时由用户选择，不凭伪彩颜色猜测。首轮以全日面图像为输入，局部裁剪和其他来源需另行验证。文件为 JPG 或 PNG 并不能说明观测类型，模型还需匹配图像的预处理方式。</p>
      <p>FL 指日面暗条，不是耀斑；白光黑子识别与活动区识别是不同任务。当前的原始标注叠加和轮廓复制均不代表模型推理结果。</p>
      <p class="input-guide__sources">依据：<a href="https://arxiv.org/html/2508.06892v1#S3.SS1" target="_blank" rel="noreferrer">原团队 SPNet §3.1</a> · <a href="https://arxiv.org/abs/2402.15407" target="_blank" rel="noreferrer">Hα 暗条识别方法</a>。文献能力与本平台已验证能力分别标注。</p>
    </div>
  </details>
</template>

<style scoped>
.input-guide { flex:none; min-width:0; border-bottom:1px solid var(--border); background:var(--bg-panel); font-size:12px; }
summary { padding:10px 16px; cursor:pointer; line-height:1.7; }
summary strong { margin-right:12px; }
summary span { margin-right:16px; color:var(--text-secondary); }
summary .model-status { color:var(--warning); }
.input-guide__body { max-height:35vh; overflow:auto; padding:0 16px 12px; line-height:1.7; }
p { margin:8px 0; }
.input-guide__table { overflow-x:auto; }
table { border-collapse:collapse; width:100%; min-width:640px; text-align:left; }
caption { text-align:left; color:var(--text-secondary); padding:6px 0; }
th,td { padding:8px; border:1px solid var(--border); vertical-align:top; }
tbody th { min-width:110px; }
.input-guide__sources { color:var(--text-secondary); }
a { color:var(--accent); }
</style>
