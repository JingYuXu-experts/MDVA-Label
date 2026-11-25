import assert from 'node:assert/strict'
import fs from 'node:fs'
import {chromium} from '@playwright/test'

const base='http://127.0.0.1:8765',out='../out/acceptance/model-release-20261116'
const fixture=JSON.parse(fs.readFileSync(out+'/cases.json','utf8'))
const jobs=JSON.parse(fs.readFileSync(out+'/jobs.json','utf8'))
const browser=await chromium.launch({channel:'chrome',headless:true})
const page=await browser.newPage({viewport:{width:1600,height:1100},recordVideo:{dir:out+'/demo-video',size:{width:1600,height:1100}}}),errors=[],report={}
page.on('pageerror',e=>errors.push(e.message))
const ws=fixture.workspace
async function get(path,params={}){const r=await page.request.get(base+'/api'+path,{params:{ws,...params}});assert.equal(r.status(),200,await r.text());return r.json()}
async function post(path,body){const r=await page.request.post(base+'/api'+path,{data:{ws,...body}});assert.equal(r.status(),200,await r.text());return r.json()}
async function settle(){await page.waitForFunction(()=>{const image=document.querySelector('.stage__frame');return image?.complete&&image.naturalWidth>0&&!document.querySelector('.stage-spinner')},{},{timeout:30000})}
try{
 await page.goto(base+'/training');await page.getByText(/本轮训练39\/39完成/).first().waitFor()
 assert.equal((await get('/training/summary')).approved_total,39)
 await page.screenshot({path:out+'/training-final.png'})
 for(const entry of fixture.datasets){
  const job=await get('/solar/inference/jobs',{id:jobs.find(j=>j.model_id.startsWith(entry.category)).id})
  assert.equal(job.state,'completed');assert.equal(job.completed,30)
  const row=job.results.find(r=>r.state==='completed'&&r.annotations>0)
  assert.ok(row)
  const query=new URLSearchParams({ws,dataset:entry.dataset,version:entry.version,uri:row.uri})
  await page.goto(base+'/annotate?'+query);await settle()
  const panel=page.getByLabel('太阳模型候选标注',{exact:true})
  await panel.locator('summary').first().click()
  await panel.getByText('最近候选任务（关闭页面后可在这里重新打开）',{exact:true}).click()
  await panel.locator('.history-row').filter({hasText:job.model_id}).first().click()
  await page.getByAltText('模型栅格候选，待人工核对',{exact:true}).waitFor()
  const data=await get('/solar/inference/result',{id:job.id,index:row.index})
  const geometry=await page.getByAltText('模型栅格候选，待人工核对',{exact:true}).evaluate(e=>({width:e.naturalWidth,height:e.naturalHeight,left:parseFloat(e.style.left),top:parseFloat(e.style.top),cssWidth:parseFloat(e.style.width),cssHeight:parseFloat(e.style.height)}))
  assert.equal(geometry.width,data.width);assert.equal(geometry.height,data.height)
  assert.ok(Math.abs(geometry.cssWidth/geometry.cssHeight-data.width/data.height)<.001)
  assert.equal((await get('/reference',{uri:row.uri})).available,false,'No reference mask attached to this acceptance input')
  await panel.getByRole('button',{name:/接纳可编辑轮廓到草稿/}).click()
  assert.equal(await page.locator('g.ann').count(),data.annotations.length)
  await panel.getByRole('button',{name:/接纳可编辑轮廓到草稿/}).click()
  assert.equal(await page.locator('g.ann').count(),data.annotations.length,'Accepting twice must not duplicate candidates')
  await page.locator('.toolbar').getByRole('button',{name:/^保存/}).click()
  await page.waitForFunction(()=>!document.querySelector('[aria-label="有未保存修改"]'))
  const currentVersion=new URL(page.url()).searchParams.get('version')
  assert.notEqual(currentVersion,entry.version,'Original import version must remain unchanged')
  const saved=(await get('/annotations',{version:currentVersion,uri:row.uri})).annotations
  assert.equal(saved.length,data.annotations.length)
  assert.ok(saved.every(a=>a.ann.backend===job.model_id&&a.ann.source==='preannotate'))
  assert.deepEqual(saved.map(a=>JSON.stringify(a.ann.coords)).sort(),data.annotations.map(a=>JSON.stringify(a.coords)).sort(),'Native coordinates must survive save independent of database row order')
  await page.reload();await settle();assert.equal(await page.locator('g.ann').count(),saved.length)
  await post('/annotations/submit',{version:currentVersion,uri:row.uri,role:'annotator'})
  const approved=await post('/annotations/review',{version:currentVersion,role:'reviewer',action:'approve'})
  assert.ok(approved.n>0)
  const exported=await post('/export',{version:currentVersion,format:'coco',params:{annotation_source:'current'},scripts:false})
  const directory=exported.out_dir||exported.dir
  assert.ok(directory,JSON.stringify(exported))
  const validation=await post('/export/validate',{version:currentVersion,dir:directory})
  await page.screenshot({path:out+'/'+entry.category+'-saved-candidates.png'})
  report[entry.category]={job:job.id,uri:row.uri,version:currentVersion,annotations:saved.length,geometry,
     reviewed:approved.n,export:exported,validation,reference_mask_input:false}
 }
 const entry=fixture.datasets[0]
 const query=new URLSearchParams({ws,dataset:entry.dataset,version:entry.version,uri:entry.uris[0]})
 await page.goto(base+'/annotate?'+query);await settle()
 const panel=page.getByLabel('太阳模型候选标注',{exact:true});await panel.locator('summary').first().click()
 await panel.getByLabel('确认模型输入条件',{exact:true}).check()
 await panel.getByRole('button',{name:'识别当前图像',exact:true}).click()
 await panel.getByText(/候选生成完成 · 1 \/ 1/).waitFor({timeout:60000})
 await page.getByAltText('模型栅格候选，待人工核对',{exact:true}).waitFor()
 report.single_image_button=true
 const historyToggle=panel.getByText('最近候选任务（关闭页面后可在这里重新打开）',{exact:true})
 if(!await panel.locator('.history-row').first().isVisible())await historyToggle.click()
 let held=false
 await page.route('**/api/solar/inference/result?**',async route=>{
   const response=await route.fetch()
   if(!held){held=true;await new Promise(resolve=>setTimeout(resolve,1200))}
   await route.fulfill({response})
 })
 await panel.locator('.history-row').filter({hasText:'30/30'}).first().click()
 for(let i=0;i<40&&!held;i++)await page.waitForTimeout(50)
 assert.ok(held)
 await page.getByRole('button',{name:'下一张',exact:true}).click();await settle()
 await page.waitForTimeout(1600)
 const currentUri=(await page.locator('.annotate__uri').innerText()).trim()
 await page.getByAltText('模型栅格候选，待人工核对',{exact:true}).waitFor()
 assert.equal(await page.getByAltText('模型栅格候选，待人工核对',{exact:true}).getAttribute('data-candidate-uri'),currentUri)
 await page.unroute('**/api/solar/inference/result?**')
 report.stale_result_does_not_overwrite_current_image=true
 let failOnce=true
 await page.route('**/api/solar/inference/result?**',async route=>{
   if(failOnce){failOnce=false;await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'候选读取失败重试验收'})})}
   else await route.continue()
 })
 await panel.locator('.history-row').filter({hasText:'30/30'}).first().click()
 await panel.getByRole('alert').filter({hasText:'候选读取失败重试验收'}).waitFor()
 await panel.locator('.history-row').filter({hasText:'30/30'}).first().click()
 await page.getByAltText('模型栅格候选，待人工核对',{exact:true}).waitFor()
 assert.equal(await panel.getByRole('alert').count(),0)
 await page.unroute('**/api/solar/inference/result?**');report.failed_result_retry=true
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:out+'/mobile-candidates.png'})
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+2),'Mobile page must not overflow')
 assert.deepEqual(errors,[])
 fs.writeFileSync(out+'/browser-report.json',JSON.stringify({...report,errors,human_time_study:'not_performed'},null,2))
}catch(e){await page.screenshot({path:out+'/browser-failure.png'});throw e}
finally{await browser.close()}
