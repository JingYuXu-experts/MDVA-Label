import {chromium} from '@playwright/test'
import assert from 'node:assert/strict'
import {writeFile} from 'node:fs/promises'
const origin='http://127.0.0.1:8765'
const browser=await chromium.launch({channel:'chrome',headless:true})
const errors=[]
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}})
 page.on('pageerror',e=>errors.push(e.message))
 let casesFailed=false, imageFailed=false
 await page.route('**/api/training/diagnostics/cases*',async route=>{
  if(!casesFailed){casesFailed=true;return route.fulfill({status:503,json:{error:'isolated retry check'}})}
  const u=new URL(route.request().url())
  if(u.searchParams.get('run_id')==='FL-F2-s17'&&!u.searchParams.get('sample_id'))await new Promise(r=>setTimeout(r,900))
  return route.continue()
 })
 await page.route('**/api/training/diagnostics/assets*',route=>{
  const id=new URL(route.request().url()).searchParams.get('id')
  if(!imageFailed&&id?.endsWith('/original.png')){imageFailed=true;return route.fulfill({status:503,body:'isolated asset retry check'})}
  return route.continue()
 })
 await page.goto(origin+'/training')
 const panel=page.getByRole('region',{name:'识别效果诊断'})
 await panel.getByRole('button',{name:'重试案例',exact:true}).waitFor()
 await panel.getByRole('button',{name:'重试案例',exact:true}).click()
 await panel.getByRole('button',{name:'重试图片',exact:true}).waitFor()
 await panel.getByRole('button',{name:'重试图片',exact:true}).click()
 await page.waitForFunction(()=>[...document.querySelectorAll('.case-detail img')].every(i=>i.complete&&i.naturalWidth>0))
 const delayed=page.waitForRequest(r=>r.url().includes('/diagnostics/cases')&&new URL(r.url()).searchParams.get('run_id')==='FL-F2-s17'&&!new URL(r.url()).searchParams.get('sample_id'))
 await page.getByLabel('诊断案例模型',{exact:true}).selectOption('FL-F2-s17')
 await delayed
 await page.getByLabel('诊断案例模型',{exact:true}).selectOption('FL-F0-s17')
 await page.waitForTimeout(1400)
 assert.match(await panel.locator('.image-comparison figure').nth(2).locator('figcaption').innerText(),/^FL-F0-s17/)
 const payload=await (await page.request.get(origin+'/api/training/diagnostics/cases?subset=test')).json()
 assert.match(payload.error,/训练|验证/)
 const training=await (await page.request.get(origin+'/api/training/summary')).json()
 assert.equal(training.progress.paused_requested,true)
 assert.equal(training.progress.automatic_continuation,false)
 assert.deepEqual(errors,[])
 await writeFile('../out/acceptance/diagnostics-20261115/browser-boundaries.json',JSON.stringify({isolated_http_retry:casesFailed,isolated_image_retry:imageFailed,late_response_did_not_overwrite:true,test_input_rejected:true,training_still_paused:true,errors},null,2))
 console.log('Isolated API/image retries, stale response protection and split restrictions passed')
}finally{await browser.close()}
