<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useItemsStore, bucketOf, type StateBucket } from '@/stores/items'
import { useCanvasStore } from '@/stores/canvas'
const emit=defineEmits<{pick:[uri:string]}>()
const items=useItemsStore(),canvas=useCanvasStore()
function preference(key:string,fallback:number,min:number,max:number){try{const value=Number(localStorage.getItem(key));return value>=min&&value<=max?value:fallback}catch{return fallback}}
const width=ref(preference('mdva.annotation.railWidth',240,190,360)),size=ref(preference('mdva.annotation.thumbSize',76,48,128))
const query=ref(items.search),jump=ref(1),list=ref<HTMLElement|null>(null)
const states:Record<string,string>={unlabeled:'未标注',draft:'草稿 / 待修订',submitted:'待审核',approved:'已通过'}
const pages=computed(()=>Math.max(1,Math.ceil(items.total/items.pageSize)))
const position=computed(()=>{const i=items.items.findIndex(x=>x.uri===canvas.currentUri);return i<0?null:(items.page-1)*items.pageSize+i+1})
let observer:IntersectionObserver|undefined,timer:ReturnType<typeof setTimeout>|undefined
const visible=new Set<string>()
async function observe(){
  await nextTick();observer?.disconnect();visible.clear()
  if(!list.value)return
  observer=new IntersectionObserver(entries=>{
    for(const entry of entries){const uri=(entry.target as HTMLElement).dataset.uri!;if(entry.isIntersecting)visible.add(uri);else visible.delete(uri)}
    clearTimeout(timer);timer=setTimeout(()=>void items.fetchThumbs([...visible]),60)
  },{root:list.value,rootMargin:'160px'})
  list.value.querySelectorAll('[data-uri]').forEach(el=>observer!.observe(el))
}
watch(()=>items.items,()=>void observe(),{immediate:true})
watch([width,size],()=>{try{localStorage.setItem('mdva.annotation.railWidth',String(width.value));localStorage.setItem('mdva.annotation.thumbSize',String(size.value))}catch{/* Preferences are optional. */}})
watch(()=>items.page,p=>jump.value=p,{immediate:true})
async function page(value:number){if(items.loading)return;value=Number.isFinite(Number(value))?Math.round(Number(value)):items.page;items.page=Math.max(1,Math.min(pages.value,value));await items.fetchItems();list.value?.scrollTo({top:0})}
async function filter(state:StateBucket){items.stateFilter=state;items.page=1;await items.fetchItems()}
async function search(){items.search=query.value.trim();items.page=1;await items.fetchItems()}
async function adjacent(dir:number):Promise<string>{
  if(items.loading||!items.items.length)return ''
  const index=items.items.findIndex(x=>x.uri===canvas.currentUri),next=index+dir
  if(index<0)return (dir>0?items.items[0]:items.items[items.items.length-1])?.uri||''
  if(next>=0&&next<items.items.length)return items.items[next]!.uri
  const target=items.page+dir
  if(target<1||target>pages.value)return ''
  await page(target)
  if(items.error)return ''
  return (dir>0?items.items[0]:items.items[items.items.length-1])?.uri||''
}
function retryThumbs(){for(const [uri,row] of items.thumbs)if(row.error)items.thumbs.delete(uri);void items.fetchThumbs([...visible])}
defineExpose({adjacent,position})
onBeforeUnmount(()=>{observer?.disconnect();clearTimeout(timer)})
</script>
<template>
  <aside class="rail" :style="{'--queue-width':width+'px','--thumb-size':size+'px'}" aria-label="标注素材浏览器">
    <header><div class="rail-title"><b>素材列表</b><span>{{items.total.toLocaleString()}} 张</span></div>
      <form class="search" @submit.prevent="search"><input v-model="query" aria-label="搜索标注素材" placeholder="搜索文件名或路径"/><button :disabled="items.loading">搜索</button></form>
      <select :value="items.stateFilter" aria-label="素材标注状态" @change="filter(($event.target as HTMLSelectElement).value as StateBucket)"><option value="">全部标注状态</option><option v-for="(label,value) in states" :key="value" :value="value">{{label}}</option></select>
      <details class="display"><summary>列表显示设置</summary><label>缩略图大小<input v-model.number="size" type="range" min="48" max="128" aria-label="缩略图大小"/></label><label>列表宽度<input v-model.number="width" type="range" min="190" max="360" aria-label="素材列表宽度"/></label></details>
      <small>{{position?'当前筛选序号 #'+position:'按下方缩略图打开图片'}} · ← / → 切换</small>
    </header>
    <p v-if="items.error" class="failure" role="alert">{{items.error}} <button @click="items.fetchItems()">重试列表</button></p>
    <div ref="list" class="rail-list" role="listbox" aria-label="素材缩略图" :aria-busy="items.loading">
      <button v-for="(item,i) in items.items" :key="item.uri" :disabled="items.loading" class="queue-item" role="option" :data-uri="item.uri" :aria-selected="item.uri===canvas.currentUri" :class="{active:item.uri===canvas.currentUri}" :title="item.uri" @click="emit('pick',item.uri)">
        <div class="thumbnail"><img v-if="items.thumbs.get(item.uri)?.png" :src="'data:image/png;base64,'+items.thumbs.get(item.uri)!.png" :alt="item.name+' 缩略图'" loading="lazy"/><span v-else>{{items.thumbs.get(item.uri)?.error?'读取失败':'载入缩略图…'}}</span><em>#{{String((items.page-1)*items.pageSize+i+1).padStart(3,'0')}}</em></div>
        <div class="item-meta"><b>{{item.name}}</b><span class="state" :data-state="bucketOf(item)">{{states[bucketOf(item)]}}</span><small>{{item.n_annotations}} 个标注</small></div>
      </button>
      <p v-if="!items.items.length&&!items.loading" class="empty">没有符合条件的素材。可以清空搜索或切换标注状态。</p><p v-if="items.loading" class="empty">正在读取素材…</p>
    </div>
    <footer><div><button :disabled="items.page<=1||items.loading" @click="page(items.page-1)">上一页</button><span>{{items.page}} / {{pages}}</span><button :disabled="items.page>=pages||items.loading" @click="page(items.page+1)">下一页</button></div><form @submit.prevent="page(jump)"><label>跳到<input v-model.number="jump" type="number" min="1" :max="pages" aria-label="素材页码"/></label><button :disabled="items.loading">确定</button><button type="button" @click="retryThumbs">重试缩略图</button></form></footer>
  </aside>
</template>
<style scoped>
.rail{width:var(--queue-width);flex:none;min-height:0;display:flex;flex-direction:column;background:var(--bg-panel);border-right:1px solid var(--border);font-size:12px}.rail header{padding:12px;border-bottom:1px solid var(--border);display:grid;gap:10px}.rail-title{display:flex;align-items:center;justify-content:space-between}.rail-title b{font-size:14px}.rail-title span,.rail small{color:var(--text-muted)}.rail button,.rail select,.rail input{font:inherit;background:var(--bg-raised);color:var(--text-primary);border:1px solid var(--border);border-radius:4px;padding:6px;min-width:0}.rail button{cursor:pointer}.rail button:hover{border-color:var(--accent)}.rail button:disabled{opacity:.4;cursor:default}.search{display:flex;gap:5px}.search input{flex:1;width:50%}.display summary{cursor:pointer;color:var(--text-secondary);font-size:11px}.display label{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px;margin-top:9px}.display input{width:110px;padding:0;accent-color:var(--accent)}.rail-list{flex:1;overflow:auto;min-height:0;padding:8px}.rail button.queue-item{display:flex;text-align:left;width:100%;gap:10px;padding:7px;margin-bottom:6px;background:transparent}.rail .queue-item.active{background:var(--bg-hover);border-color:var(--accent);box-shadow:inset 3px 0 var(--accent)}.thumbnail{position:relative;flex:none;width:var(--thumb-size);height:var(--thumb-size);background:#050a10;display:flex;align-items:center;justify-content:center;border-radius:3px;overflow:hidden}.thumbnail img{width:100%;height:100%;object-fit:contain}.thumbnail>span{font-size:10px;color:#8497a9;text-align:center}.thumbnail em{position:absolute;left:0;bottom:0;background:#07111ed9;color:#e2eef8;font:10px Consolas,monospace;padding:3px 4px;border-radius:0 4px 0 0}.item-meta{min-width:0;display:flex;flex-direction:column;justify-content:center;gap:7px}.item-meta>b{font-size:11px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.item-meta small{font-size:10px}.state{font-size:10px;color:var(--text-muted)}.state[data-state=draft]{color:var(--warning)}.state[data-state=submitted]{color:var(--accent)}.state[data-state=approved]{color:var(--success)}.rail footer{border-top:1px solid var(--border);padding:9px;display:grid;gap:8px;font-size:11px}.rail footer>div,.rail footer form{display:flex;align-items:center;gap:7px;justify-content:space-between}.rail footer input{width:42px;margin-left:5px}.failure,.empty{padding:10px;font-size:11px;line-height:1.7;color:var(--text-muted)}.failure{color:var(--danger)}
</style>
