import { chromium } from '@playwright/test'
import assert from 'node:assert/strict'
import { mkdir,writeFile } from 'node:fs/promises'
const origin=process.env.MDVA_SOLAR_TEST_ORIGIN||'http://127.0.0.1:5173'
const output='../out/acceptance/solar-observation-20261112'
await mkdir(output,{recursive:true})
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-angle=swiftshader','--enable-unsafe-swiftshader','--enable-webgl']})
try{
 const page=await browser.newPage({viewport:{width:1600,height:1050}}),errors=[],requests=[],pins=[]
 const flags={slow171:false,fail193:false,delayAll:false,duplicate:false,latestRevision:0}
 page.on('pageerror',e=>errors.push(e.message))
 await page.clock.install()
 await page.route('**/api/sw/summary*',r=>r.fulfill({json:{sources:[],observations:[],events:[],simulations:[],alerts:[],forecast:null,enabled:false}}))
 await page.route('**/api/sw/series*',r=>r.fulfill({json:{points:[],unit:''}}))
 await page.route('**/api/sw/magnetosphere/model*',r=>r.fulfill({status:503,json:{error:'Isolated UI check: model computation disabled'}}))
 await page.route('**/api/sw/pin*',r=>{pins.push(r.request().postDataJSON());return r.fulfill({json:{pinned_frames:1}})})
 await page.route('**/api/sw/assets?id=fixture-*',r=>r.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZleoAAAAASUVORK5CYII=','base64')}))
 await page.route('**/api/sw/solar/image*',async r=>{
  const p=new URL(r.request().url()).searchParams,channel=p.get('channel'),latest=p.get('latest')==='true',time=latest?Date.now()-120000:Date.parse(p.get('time'))
  requests.push({channel,time,latest})
  if(flags.delayAll||channel==='171'&&flags.slow171)await new Promise(resolve=>setTimeout(resolve,500))
  try{
   if(channel==='193'&&flags.fail193){await r.fulfill({status:504,json:{error:'公开信源测试超时'}});return}
   const image_id=channel+'-'+(flags.duplicate?'same':Math.floor(time/60000))+(latest?'-'+flags.latestRevision:'')
   await r.fulfill({json:{id:'fixture-'+image_id,image_id,channel,observed_at:new Date(time).toISOString(),requested_at:new Date(time).toISOString(),offset_seconds:0,source_url:'https://api.helioviewer.org/docs/v2/',note:'Isolated fixture only',selection:latest?'latest':'at_time',metadata:{width:4096,height:4096,rsun:1600,refPixelX:2048.5,refPixelY:2048.5,rotation:0}}})
  }catch{/* An old request may have been aborted by a new selection. */}
 })
 await page.goto(origin+'/monitor')
 await page.getByRole('button',{name:'太阳活动观测',exact:true}).click()
 const viewer=page.locator('.solar-observation'),channel=page.getByLabel('太阳观测通道',{exact:true})
 await page.locator('.solar-observation[data-displayed-channel="304"]').waitFor()
 assert.match(await viewer.innerText(),/不是连续直播|按实际可用影像/)
 assert.equal(await page.getByRole('button',{name:'播放观测',exact:true}).isDisabled(),true)
 const oldSrc=await viewer.locator('.observation-view img').getAttribute('src')
 await channel.selectOption('193')
 await page.locator('.solar-observation[data-displayed-channel="193"]').waitFor()
 assert.notEqual(await viewer.locator('.observation-view img').getAttribute('src'),oldSrc)
 assert.match(await viewer.locator('.observation-stamp').innerText(),/AIA 193/)
 assert.ok(requests.some(r=>r.channel==='193'))
 flags.slow171=true
 await channel.selectOption('171')
 assert.equal(await viewer.getAttribute('data-displayed-channel'),'')
 await channel.selectOption('hmi')
 await page.locator('.solar-observation[data-displayed-channel="hmi"]').waitFor()
 await page.waitForTimeout(650)
 assert.equal(await viewer.getAttribute('data-displayed-channel'),'hmi')
 assert.match(await viewer.locator('.observation-stamp').innerText(),/HMI/)
 flags.slow171=false
 await page.getByRole('button',{name:'太阳风怎样到地球',exact:true}).click()
 await page.getByRole('button',{name:'太阳活动观测',exact:true}).click()
 assert.equal(await channel.inputValue(),'hmi')
 assert.equal(await viewer.getAttribute('data-displayed-channel'),'hmi')
 const beforeRefresh=requests.length
 flags.latestRevision++
 await page.clock.fastForward(60001)
 await page.waitForFunction(before=>performance.getEntriesByType('resource').length>=before,0)
 await page.waitForTimeout(100)
 assert.ok(requests.length>beforeRefresh,'Recent-mode refresh did not request a new observation')
 assert.match(await viewer.innerText(),/1 \/ 1 帧/,'Recent images silently accumulate into an unrequested sequence')
 await page.getByRole('button',{name:'载入时序（最多13帧）',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.solar-observation')?.getAttribute('aria-busy')==='false')
 assert.match(await viewer.innerText(),/1 \/ 13 帧/)
 await page.getByRole('button',{name:'下一帧',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.solar-observation')?.textContent.includes('2 / 13 帧'))
 const first=await viewer.locator('.observation-stamp time').innerText()
 await page.getByRole('button',{name:'播放观测',exact:true}).click()
 await page.clock.fastForward(850)
 await page.waitForFunction(previous=>document.querySelector('.observation-stamp time')?.textContent!==previous,first)
 await page.getByRole('button',{name:'暂停回放',exact:true}).click()
 await page.getByRole('button',{name:'固定离线时序',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.solar-observation')?.textContent.includes('已固定13张观测'))
 assert.equal(pins.length,13)
 const beforeHistory=requests.length
 await page.clock.fastForward(60001)
 await page.waitForTimeout(100)
 assert.equal(requests.length,beforeHistory,'History mode must not refresh to current Sun')
 const date=page.getByLabel('太阳观测起始时间 UTC',{exact:true})
 await date.fill('2012-08-31T20:15');await date.dispatchEvent('change')
 await page.waitForFunction(()=>document.querySelector('.solar-observation')?.getAttribute('aria-busy')==='false')
 assert.match(await viewer.locator('.observation-stamp').innerText(),/2012-08-31/)
 await page.getByRole('button',{name:'2012日珥喷发案例',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.solar-observation')?.getAttribute('aria-busy')==='false')
 assert.equal(await channel.inputValue(),'304')
 assert.match(await viewer.locator('.observation-stamp').innerText(),/2012教学案例/)
 flags.fail193=true
 await channel.selectOption('193')
 await viewer.getByRole('alert').waitFor()
 assert.equal(await viewer.locator('.observation-view img').count(),0,'Failed channel switch retains a mislabeled old image')
 flags.fail193=false
 await page.getByRole('button',{name:'重试此次读取',exact:true}).click()
 await page.locator('.solar-observation[data-displayed-channel="193"]').waitFor()
 flags.duplicate=true
 await page.getByRole('button',{name:'载入时序（最多13帧）',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('.solar-observation')?.getAttribute('aria-busy')==='false')
 assert.match(await viewer.innerText(),/只有一张不同观测/)
 assert.equal(await page.getByRole('button',{name:'播放观测',exact:true}).isDisabled(),true)
 flags.duplicate=false;flags.delayAll=true
 await page.getByRole('button',{name:'查看近期观测',exact:true}).click()
 await page.getByRole('button',{name:'停止等待',exact:true}).click()
 await page.waitForTimeout(650)
 assert.match(await viewer.innerText(),/已停止等待/)
 flags.delayAll=false
 await channel.selectOption('c2')
 await page.locator('.solar-observation[data-displayed-channel="c2"]').waitFor()
 await page.getByRole('button',{name:'进入三维全屏',exact:false}).click()
 await page.setViewportSize({width:390,height:844})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 const controls=await viewer.locator('aside').boundingBox()
 assert.ok(controls.height>230,'Mobile controls have no usable reading area')
 assert.deepEqual(errors,[])
 await writeFile(output+'/ui-boundaries.json',JSON.stringify({fixture_only:true,automatic_channel_switch:true,caption_matches_image:true,rapid_switch_stale_response_rejected:true,tab_state_preserved:true,recent_auto_refresh:true,historical_no_refresh:true,play_step_and_pin:true,example_labeled_historical:true,failure_retry:true,duplicate_frames_not_fake_video:true,stop_waiting:true,mobile_no_overflow:true,errors},null,2))
 console.log('Solar observation interaction boundary checks passed (isolated fixtures)')
}finally{await browser.close()}
