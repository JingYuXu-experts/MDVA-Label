<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent, CanvasRenderer])
const props=defineProps<{ points: {t:number;v:number}[]; unit:string; logarithmic?:boolean; bars?:boolean; cursor?:number; utc?:boolean }>()
const emit=defineEmits<{ select:[time:number] }>()
const element=ref<HTMLElement|null>(null)
let chart:echarts.ECharts|undefined
let observer:ResizeObserver|undefined
function draw(){
  const data:([number,number|null])[]=[]
  for(let i=0;i<props.points.length;i++){
    const point=props.points[i]!, prior=props.points[i-1]
    if(prior&&point.t-prior.t>600&&!props.bars)data.push([(prior.t+1)*1000,null])
    data.push([point.t*1000,props.logarithmic&&point.v<=0?null:point.v])
  }
  chart?.setOption({animation:false,grid:{left:51,right:12,top:22,bottom:34},tooltip:{trigger:'axis'},
    xAxis:{type:'time',axisLabel:{color:'#8fa5b8',fontSize:10,formatter:(v:number)=>new Date(v).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',timeZone:props.utc?'UTC':'Asia/Shanghai'})},splitLine:{show:false}},
    yAxis:{type:props.logarithmic?'log':'value',name:props.unit,nameTextStyle:{color:'#8fa5b8',fontSize:10},axisLabel:{color:'#8fa5b8',fontSize:10},splitLine:{lineStyle:{color:'#26333e'}}},
    series:[{type:props.bars?'bar':'line',data,showSymbol:false,connectNulls:false,lineStyle:{width:1.3,color:'#41d7c2'},itemStyle:{color:'#41d7c2'},markLine:{silent:true,symbol:'none',data:props.cursor?[{xAxis:props.cursor*1000}]:[],lineStyle:{color:'#ecb85b'},label:{show:false}}}]},true)
}
onMounted(()=>{if(element.value){chart=echarts.init(element.value);chart.on('click',p=>{const value=p.value as [number,number];if(Array.isArray(value))emit('select',value[0]/1000)});observer=new ResizeObserver(()=>chart?.resize());observer.observe(element.value);draw()}})
watch(()=>[props.points,props.cursor,props.utc],draw)
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div ref="element" class="monitor-chart" role="img" :aria-label="unit+'时间序列'"/><p v-if="!points.length" class="chart-empty">暂无有效数据 · 等待信源更新</p></template>
<style scoped>.monitor-chart{height:170px;width:100%}.chart-empty{font-size:11px;color:#8fa5b8;margin:-95px 0 76px;text-align:center;pointer-events:none}</style>
