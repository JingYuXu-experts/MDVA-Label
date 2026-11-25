<script setup lang="ts">
import { computed } from 'vue'
import land from '@/assets/land-110m.json'
const props=defineProps<{time:number}>()
const rad=Math.PI/180
// Solar declination/equation of time approximation for a labelled daylight overview.
const solar=computed(()=>{
 const d=new Date(props.time*1000),year=d.getUTCFullYear()
 const day=(d.getTime()-Date.UTC(year,0,1))/86400000
 const g=2*Math.PI/365*(day+(d.getUTCHours()-12)/24)
 const eq=229.18*(.000075+.001868*Math.cos(g)-.032077*Math.sin(g)-.014615*Math.cos(2*g)-.040849*Math.sin(2*g))
 const dec=.006918-.399912*Math.cos(g)+.070257*Math.sin(g)-.006758*Math.cos(2*g)+.000907*Math.sin(2*g)-.002697*Math.cos(3*g)+.00148*Math.sin(3*g)
 const longitude=(720-(d.getUTCHours()*60+d.getUTCMinutes())-eq)/4
 return {dec,longitude}
})
type Polygon=number[][][]
const polygons:Polygon[]=[]
for(const feature of land.features){
 const coords:unknown=feature.geometry.coordinates
 if(feature.geometry.type==='Polygon')polygons.push(coords as Polygon)
 else if(feature.geometry.type==='MultiPolygon')polygons.push(...coords as Polygon[])
}
const paths=polygons.map(poly=>poly.map(ring=>ring.map((p,i)=>(i?'L':'M')+(p[0]!+180).toFixed(2)+','+(90-p[1]!).toFixed(2)).join(' ')+'Z').join(' '))
const night=computed(()=>{
 const cells=[]
 for(let lat=-88;lat<=88;lat+=4)for(let lon=-178;lon<=178;lon+=4){
  const cosine=Math.sin(lat*rad)*Math.sin(solar.value.dec)+Math.cos(lat*rad)*Math.cos(solar.value.dec)*Math.cos((lon-solar.value.longitude)*rad)
  if(cosine<0)cells.push({x:lon+178,y:88-lat})
 }
 return cells
})
</script>
<template><div class="daylight"><svg viewBox="0 0 360 180" role="img" aria-label="地球日照范围示意"><rect width="360" height="180" fill="#142f44"/><path v-for="(p,i) in paths" :key="i" :d="p" fill="#8a967a" stroke="#b6c0a6" stroke-width=".2"/><path v-for="x in [60,120,180,240,300]" :key="x" :d="`M${x} 0V180`" stroke="#7292a3" stroke-width=".2"/><path v-for="y in [30,60,90,120,150]" :key="y" :d="`M0 ${y}H360`" stroke="#7292a3" stroke-width=".2"/><rect v-for="(cell,i) in night" :key="i" :x="cell.x" :y="cell.y" width="4.1" height="4.1" fill="#000" opacity=".58"/></svg><p>日照范围近似示意 · 非通信故障分布 · Natural Earth 底图</p></div></template>
<style scoped>.daylight svg{width:100%;display:block;max-height:180px}.daylight p{font-size:10px;color:#8fa5b8;padding:4px 8px;margin:0}</style>
