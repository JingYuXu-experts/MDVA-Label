import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'

const origin=process.env.MDVA_MONITOR_TEST_ORIGIN||'http://127.0.0.1:8765'
const output='../out/acceptance/space-impact-20261112'
await mkdir(output,{recursive:true})
// Software rendering keeps this short browser acceptance off the training GPU.
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader','--enable-webgl']})
const errors=[]
try{
 const page=await browser.newPage({viewport:{width:1920,height:1080}})
 page.on('pageerror',e=>errors.push(e.message))
 await page.goto(origin+'/monitor')
 const scene=page.locator('.solar-studio')
 await page.locator('.solar-studio[data-render-state=ready]').waitFor({timeout:30000})
 await page.getByRole('button',{name:'专业浏览',exact:true}).click()
 await page.waitForFunction(()=>Number(document.querySelector('.solar-studio')?.dataset.windTracers)>0)
 assert.match(await page.locator('.render-chip').innerText(),/训练优先/)
 assert.equal(await page.locator('[data-impact-state=unselected]').count(),1)
 await page.locator('.wind-evidence summary').click()
 assert.match(await page.locator('.impact-brief').innerText(),/不是整个迎风面的密度地图/)
 await page.locator('.wind-evidence summary').click()
 await scene.screenshot({path:output+'/actual-monitor.png'})
 await page.getByRole('button',{name:'进入三维全屏',exact:false}).click()
 const rail=await page.locator('.impact-brief').boundingBox(),stage=await page.locator('.scene-stage').boundingBox()
 assert.ok(rail.y+rail.height<=stage.y+2,'Fullscreen summary covers the rendering area')
 assert.ok(rail.y+rail.height<350,'Default impact summary is too tall')
 await page.screenshot({path:output+'/actual-fullscreen.png'})
 await page.locator('[data-lesson=wind]').click()
 assert.match(await page.getByRole('dialog').innerText(),/各方向向外|线条数量不是粒子密度/)
 await page.getByRole('button',{name:'关闭讲解',exact:true}).click()
 await page.getByRole('button',{name:'太阳近景',exact:true}).click()
 assert.ok(Number(await scene.getAttribute('data-wind-tracers'))>0,'Sun close-up drops the solar wind')
 await page.getByRole('button',{name:'退出全屏',exact:false}).click()
 await page.getByRole('button',{name:'官方二维模型',exact:true}).click()
 assert.equal(await page.locator('.space-viewport canvas').count(),0)
 await page.getByRole('button',{name:'三维日地场景',exact:true}).click()
 await page.locator('.solar-studio[data-render-state=ready]').waitFor()
 await page.setViewportSize({width:390,height:844})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 await scene.screenshot({path:output+'/actual-mobile.png'})
 const training=await page.request.get(origin+'/api/training/summary')
 const active=(await training.json()).progress.active
 assert.equal(active.state,'running')
 if(active.id.startsWith('CH-'))assert.equal(active.numerical_revision,'ch-fp32-v1')
 await page.goto(origin+'/training')
 await page.getByRole('heading',{name:'训练进度与接续日志',exact:true}).waitFor()
 assert.ok((await page.locator('.progress-panel').innerText()).includes(active.id))
 await page.close()

 // Synthetic cases exercise missing/removed/non-Earth estimates. They are never
 // stored in the production database or presented as a live event screenshot.
 const fixture=await browser.newPage({viewport:{width:1280,height:1000}})
 fixture.on('pageerror',e=>errors.push(e.message))
 const now=Date.now(),iso=delta=>new Date(now+delta*3600000).toISOString()
 const e={activityID:'fixture-CME',startTime:iso(-12),cmeAnalyses:[{speed:600,longitude:10,latitude:0,halfAngle:30,isMostAccurate:true}]}
 const model={simulationID:'fixture-run',modelCompletionTime:iso(-2),cmeInputs:[{CMEID:e.activityID}],estimatedShockArrivalTime:iso(20),link:'https://ccmc.gsfc.nasa.gov/tools/DONKI/'}
 await fixture.route('**/api/sw/summary*',route=>route.fulfill({json:{now:iso(0),enabled:false,sources:[],observations:[],forecast:null,alerts:[],events:[e],simulations:[model]}}))
 await fixture.route('**/api/sw/series*',route=>{const metric=new URL(route.request().url()).searchParams.get('metric');return route.fulfill({json:{points:[{t:now/1000-60,v:metric==='speed'?400:metric==='density'?5:0,seen:now/1000,kind:'observation'}],unit:''}})})
 await fixture.goto(origin+'/monitor')
 await fixture.getByRole('button',{name:'专业浏览',exact:true}).click()
 await fixture.getByLabel('三维CME事件',{exact:true}).selectOption(e.activityID)
 await fixture.locator('[data-impact-state=earth_arrival]').waitFor()
 await fixture.locator('.wind-evidence summary').click()
 await fixture.locator('.event-evidence summary').click()
 assert.match(await fixture.locator('.impact-brief').innerText(),/2\.00e\+12 质子/)
 assert.match(await fixture.locator('.impact-brief').innerText(),/来源未给出量化概率/)
 model.estimatedShockArrivalTime=iso(-1)
 await fixture.reload()
 await fixture.getByRole('button',{name:'专业浏览',exact:true}).click()
 await fixture.getByLabel('三维CME事件',{exact:true}).selectOption(e.activityID)
 await fixture.locator('[data-impact-state=awaiting_confirmation]').waitFor()
 model.estimatedShockArrivalTime=null
 model.impactList=[{location:'Mars',arrivalTime:iso(10)}]
 await fixture.reload()
 await fixture.getByRole('button',{name:'专业浏览',exact:true}).click()
 await fixture.getByLabel('三维CME事件',{exact:true}).selectOption(e.activityID)
 await fixture.locator('[data-impact-state=no_earth_estimate]').waitFor()
 await fixture.locator('.event-evidence summary').click()
 assert.match(await fixture.locator('.impact-brief').innerText(),/不能把其他行星/)
 assert.deepEqual(errors,[])
 await writeFile(output+'/acceptance.json',JSON.stringify({software_webgl:true,actual_training_active:active.id,active_epoch:active.active_epoch,fullscreen_top_summary:true,wind_closeup_preserved:true,official_2d_preserved:true,mobile_overflow:false,fixture_only:['future_earth_estimate','passed_estimate_not_confirmation','non_earth_estimate','density_units'],errors},null,2))
 console.log('Actual monitor/training and isolated impact boundary checks passed')
}finally{await browser.close()}
