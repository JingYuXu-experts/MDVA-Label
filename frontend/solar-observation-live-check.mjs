import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdir,writeFile } from 'node:fs/promises'
const output='../out/acceptance/solar-observation-20261112'
await mkdir(output,{recursive:true})
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader','--enable-webgl']})
try{
 const page=await browser.newPage({viewport:{width:1920,height:1080}}),errors=[],results=[]
 page.on('pageerror',e=>errors.push(e.message))
 await page.goto('http://127.0.0.1:8765/monitor')
 await page.getByRole('button',{name:'进入三维全屏',exact:false}).click()
 await page.getByRole('button',{name:'太阳活动观测',exact:true}).click()
 const viewer=page.locator('.solar-observation')
 for(const channel of ['304','171','193','hmi','c2','c3']){
  const start=Date.now()
  await page.getByLabel('太阳观测通道',{exact:true}).selectOption(channel)
  await page.locator(`.solar-observation[data-displayed-channel="${channel}"]`).waitFor({timeout:65000})
  await page.waitForFunction(()=>{const image=document.querySelector('.observation-view img');return image?.complete&&image.naturalWidth>0})
  results.push({channel,milliseconds:Date.now()-start,caption:await viewer.locator('.observation-stamp').innerText(),image:await viewer.locator('img').getAttribute('src'),natural:await viewer.locator('img').evaluate(img=>[img.naturalWidth,img.naturalHeight])})
  if(channel==='193'||channel==='hmi')await page.screenshot({path:`${output}/actual-${channel}.png`})
 }
 assert.equal(new Set(results.map(r=>r.image)).size,6,'All channels point to the same immutable asset')
 assert.ok(results[3].caption.includes('上游延迟'))
 await page.getByRole('button',{name:'太阳风怎样到地球',exact:true}).click()
 await page.getByRole('button',{name:'太阳活动观测',exact:true}).click()
 assert.equal(await page.getByLabel('太阳观测通道',{exact:true}).inputValue(),'c3')
 await page.setViewportSize({width:390,height:844})
 await page.screenshot({path:output+'/actual-mobile.png'})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 assert.deepEqual(errors,[])
 const training=await (await page.request.get('http://127.0.0.1:8765/api/training/summary')).json()
 assert.equal(training.progress.active.state,'running')
 await writeFile(output+'/actual-browser.json',JSON.stringify({actual_public_data:true,software_webgl:true,channel_results:results,training_active:training.progress.active.id,training_epoch:training.progress.active.active_epoch,errors},null,2))
 console.log('Actual six-channel images, labels, stale notice and training verified')
}finally{await browser.close()}
