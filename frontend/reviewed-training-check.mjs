import {chromium} from '@playwright/test'
import assert from 'node:assert/strict'
import {writeFile} from 'node:fs/promises'
const origin='http://127.0.0.1:8765'
const browser=await chromium.launch({channel:'chrome',headless:true})
try {
 const page=await browser.newPage({viewport:{width:1550,height:1050}})
 const errors=[];page.on('pageerror',e=>errors.push(e.message))
 await page.goto(origin+'/training')
 const progress=page.getByRole('region',{name:'训练进度与接续日志'})
 await progress.getByText('自动接续已开启',{exact:true}).waitFor()
 const summary=await (await page.request.get(origin+'/api/training/summary')).json()
 assert.equal(summary.progress.training_plan_revision,'20261115-R1')
 assert.equal(summary.progress.paused_requested,false)
 assert.equal(summary.progress.automatic_continuation,true)
 assert.equal(summary.test_unlocked,false)
 assert.equal(summary.completed>=30,true)
 const diagnostics=page.getByRole('region',{name:'识别效果诊断'})
 await diagnostics.getByText('修订训练进行中 · 逐项验证',{exact:true}).waitFor()
 await diagnostics.locator('.case-grid button').first().waitFor()
 await progress.getByRole('button',{name:'查看暗条小样本学习诊断',exact:true}).click()
 await page.getByRole('dialog').waitFor()
 await page.getByRole('dialog').getByText(/300步内未达到0.90参考线/).waitFor()
 await page.getByRole('dialog').getByRole('button',{name:'关闭预览',exact:true}).click()
 await progress.getByRole('button',{name:'查看修订训练效果分析',exact:true}).click()
 await page.getByRole('dialog').getByText(/AR-A0-s17/).last().waitFor()
 await page.getByRole('dialog').getByRole('button',{name:'关闭预览',exact:true}).click()
 await progress.scrollIntoViewIfNeeded()
 await page.screenshot({path:'../out/acceptance/training-plan-revision-20261115/training-running.png'})
 assert.deepEqual(errors,[])
 await writeFile('../out/acceptance/training-plan-revision-20261115/browser-running.json',JSON.stringify({running_revision:true,diagnostic_and_training_distinguished:true,FL_negative_result_visible:true,AR_effect_analysis_visible:true,test_locked:true,errors},null,2))
 console.log('Real running training, validation distinction, negative FL result and effects reports visible')
} finally {await browser.close()}
