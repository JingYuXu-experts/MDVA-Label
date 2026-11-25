import {chromium} from '@playwright/test'
import assert from 'node:assert/strict'
import {mkdir,writeFile} from 'node:fs/promises'
const origin='http://127.0.0.1:8765',out='../out/acceptance/training-visibility-20261115'
await mkdir(out,{recursive:true})
const browser=await chromium.launch({channel:'chrome',headless:true})
try {
 const page=await browser.newPage({viewport:{width:1500,height:1000}})
 const errors=[];page.on('pageerror',e=>errors.push(e.message))
 const summary=await (await page.request.get(origin+'/api/training/summary')).json()
 const active=summary.progress.active
 assert.ok(active,'The real training must remain active during this UI check')
 await page.goto(origin+'/training')
 const progress=page.getByRole('region',{name:'训练进度与接续日志'})
 await progress.getByText('当前训练：'+active.id,{exact:true}).waitFor()
 assert.ok((await progress.boundingBox()).y<240,'Progress must be in the initial viewport')
 const details=page.getByRole('region',{name:'训练模型详情'})
 await details.getByRole('heading',{name:active.id,exact:true}).waitFor()
 assert.equal(await page.locator('.diagnostic-fold').evaluate(e=>e.open),false)
 await page.screenshot({path:out+'/live-top.png'})
 await page.getByRole('button',{name:'FL-F0-s17',exact:true}).click()
 await details.getByRole('heading',{name:'FL-F0-s17',exact:true}).waitFor()
 assert.equal(await page.getByLabel('自动跟随当前或最近训练').isChecked(),false)
 await page.getByRole('button',{name:'刷新进度',exact:true}).click()
 await details.getByRole('heading',{name:'FL-F0-s17',exact:true}).waitFor()
 await page.getByLabel('自动跟随当前或最近训练').check()
 await details.getByRole('heading',{name:active.id,exact:true}).waitFor()
 const paused=await browser.newPage({viewport:{width:1500,height:1000}})
 const fixture=structuredClone(summary),last={...active,state:'paused'}
 fixture.runs=fixture.runs.map(r=>r.id===active.id?last:r)
 Object.assign(fixture.progress,{active:null,last_run:last,state:'paused',automatic_continuation:false,paused_requested:true,headline:'已暂停，进度已保存'})
 await paused.route('**/api/training/summary',r=>r.fulfill({json:fixture}))
 await paused.route('**/api/training/run?*',async r=>{
  const response=await r.fetch();const body=await response.json()
  if(body.status.id===active.id)body.status.state='paused'
  await r.fulfill({response,json:body})
 })
 await paused.goto(origin+'/training')
 const saved=paused.getByRole('region',{name:'训练进度与接续日志'})
 await saved.getByText('训练已暂停',{exact:true}).waitFor()
 await saved.getByText('最近训练：'+active.id+' · 已暂停',{exact:true}).waitFor()
 await saved.getByText('已保存至第 '+summary.progress.checkpoint.completed_epoch+' 轮',{exact:true}).waitFor()
 await paused.getByRole('region',{name:'训练模型详情'}).getByRole('heading',{name:active.id,exact:true}).waitFor()
 assert.ok(await paused.getByRole('img',{name:'验证Dice随训练轮数变化'}).count())
 await paused.screenshot({path:out+'/paused-top.png'})
 await paused.setViewportSize({width:390,height:844})
 assert.equal(await paused.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false)
 assert.deepEqual(errors,[])
 const actual=await (await page.request.get(origin+'/api/training/summary')).json()
 assert.equal(actual.progress.paused_requested,false,'The real training must never be paused by a browser fixture')
 await writeFile(out+'/verification.json',JSON.stringify({real_training_id:active.id,live_progress_initial_viewport:true,default_curve_follows_active:true,manual_choice_retained:true,paused_case_is_browser_fixture:true,paused_checkpoint_and_curve_retained:true,real_training_uninterrupted:true,mobile_no_overflow:true,errors},null,2))
 console.log('Visible real progress and model following passed; isolated paused snapshot retains checkpoint and curve')
} finally {await browser.close()}
