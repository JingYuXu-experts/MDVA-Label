import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {chromium} from '@playwright/test'

const out='../out/acceptance/direct-recognition-20261116'
fs.mkdirSync(out,{recursive:true})
const fixture=JSON.parse(fs.readFileSync('../out/acceptance/model-release-20261116/cases.json','utf8'))
const workspace=process.env.MDVA_DIRECT_WS||fixture.workspace
const base='http://127.0.0.1:8765',reports=[],errors=[]
const browser=await chromium.launch({channel:'chrome',headless:true})
const page=await browser.newPage({viewport:{width:1500,height:1100},acceptDownloads:true})
page.on('pageerror',e=>errors.push(e.message))
try{
 for(const entry of fixture.datasets){
  await page.goto(base+'/recognize?'+new URLSearchParams({ws:workspace}))
  await page.getByRole('heading',{name:'图片自动识别',exact:true}).waitFor()
  await page.getByLabel('识别任务',{exact:true}).selectOption(entry.category==='AR'?'AR-A0-s42':'CH-C2-s2026')
  const filename=path.resolve('../datas of importance/Situation Awareness Dataset',entry.uris[0].slice('@ssafd/'.length))
  await page.locator('input[type=file]').setInputFiles(filename)
  await page.getByLabel(/我确认图像符合上述/).check()
  const responsePromise=page.waitForResponse(r=>r.url().includes('/api/solar/analyze')&&r.request().method()==='POST')
  await page.getByRole('button',{name:'开始自动识别',exact:true}).click()
  const response=await responsePromise;assert.equal(response.status(),200,await response.text())
  const job=await response.json()
  assert.equal(job.dataset,'');assert.equal(job.version,'');assert.equal(job.input_kind,'uploaded_image')
  await page.getByAltText('自动输出的活动范围',{exact:true}).waitFor({timeout:180000})
  const original=await page.getByAltText('自动识别输入原图',{exact:true}).evaluate(e=>({w:e.naturalWidth,h:e.naturalHeight}))
  const overlay=await page.getByAltText('自动输出的活动范围',{exact:true}).evaluate(e=>({w:e.naturalWidth,h:e.naturalHeight}))
  assert.deepEqual(original,overlay)
  await page.screenshot({path:`${out}/${entry.category}.png`})
  await page.reload()
  await page.getByAltText('自动输出的活动范围',{exact:true}).waitFor({timeout:30000})
  assert.equal(await page.getByLabel('识别任务',{exact:true}).inputValue(),job.model_id)
  const downloadPromise=page.waitForEvent('download')
  await page.getByRole('button',{name:'下载自动标注（掩膜 / COCO / 区域数据）',exact:true}).click()
  const download=await downloadPromise;assert.equal(await download.failure(),null)
  await download.saveAs(`${out}/${entry.category}.zip`)
  reports.push({category:entry.category,job_id:job.id,dataset_required:false,reference_input:false,
    native_geometry:original,reload_restored:true,downloaded:true,url:page.url()})
 }
 let retry=true
 await page.route('**/api/solar/inference/result?**',async route=>{
  if(retry){retry=false;await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'acceptance retry probe'})})}
  else await route.continue()
 })
 await page.reload();await page.getByRole('button',{name:'重新读取结果',exact:true}).waitFor()
 await page.getByRole('button',{name:'重新读取结果',exact:true}).click()
 await page.getByAltText('自动输出的活动范围',{exact:true}).waitFor()
 await page.unroute('**/api/solar/inference/result?**')
 const datasets=await page.request.get(base+'/api/datasets',{params:{ws:workspace}})
 assert.equal(datasets.status(),200)
 if(process.env.MDVA_DIRECT_WS)assert.deepEqual((await datasets.json()).datasets,[])
 await page.setViewportSize({width:390,height:844});await page.reload()
 await page.getByRole('heading',{name:'图片自动识别',exact:true}).waitFor()
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1))
 await page.screenshot({path:out+'/mobile.png'})
 assert.deepEqual(errors,[])
 fs.writeFileSync(out+'/browser.json',JSON.stringify({workspace,reports,reload_model_context_correct:true,retry_after_503:true,empty_workspace_no_datasets:!!process.env.MDVA_DIRECT_WS,mobile_no_horizontal_overflow:true,errors},null,2))
}catch(e){await page.screenshot({path:out+'/failure.png'});throw e}
finally{await browser.close()}
