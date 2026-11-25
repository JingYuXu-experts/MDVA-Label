import { chromium } from '@playwright/test'
import fs from 'node:fs'
const base=process.env.MDVA_TEST_URL||'http://127.0.0.1:8766'
const output='../out/acceptance'
fs.mkdirSync(output,{recursive:true})
const options={headless:true}
const installed='C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1217/chrome-win64/chrome.exe'
if(fs.existsSync(installed))options.executablePath=installed
const browser=await chromium.launch(options)
const context=await browser.newContext({viewport:{width:1920,height:1080},recordVideo:{dir:output+'/monitor-video',size:{width:1920,height:1080}}})
const page=await context.newPage()
const errors=[]
page.on('pageerror',error=>errors.push(error.message))
const start=Date.now()
await page.goto(base)
await page.getByText('MDVA 空间天气监测台',{exact:false}).first().waitFor()
await page.getByRole('button',{name:'官方二维模型',exact:true}).click()
await page.locator('.model-image img').waitFor({timeout:30000})
await page.waitForFunction(()=>document.querySelector('.model-image img')?.naturalWidth>0)
await page.waitForTimeout(500)
const firstScreen=Date.now()-start
await page.screenshot({path:output+'/monitor-desktop-20261111.png',fullPage:true})
const slider=page.getByRole('slider',{name:'传播帧时间轴'})
const before=await slider.inputValue()
await page.getByRole('button',{name:'下一帧',exact:true}).click()
await page.waitForFunction(before=>Number(document.querySelector('input[type=range]').value)===Number(before)+1,before)
await page.getByRole('button',{name:'播放',exact:true}).click()
await page.waitForTimeout(6000)
await page.getByRole('button',{name:'暂停',exact:true}).click()
if(Number(await slider.inputValue())<=Number(before)+1)throw Error('Playback did not advance')
await page.locator('.event-library>summary').click()
await page.locator('.event-list button').first().click()
await page.locator('.event-verification>summary').click()
await page.screenshot({path:output+'/monitor-event-20261111.png',fullPage:true})
await page.setViewportSize({width:390,height:844})
await page.screenshot({path:output+'/monitor-mobile-20261111.png',fullPage:true})
if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile overflow')
await page.setViewportSize({width:1920,height:1080})
await page.route('**/api/sw/summary*',route=>route.abort())
await page.getByRole('button',{name:'重试读取',exact:true}).count().then(async n=>{if(n)await page.getByRole('button',{name:'重试读取',exact:true}).click()})
await context.setOffline(true)
await page.waitForTimeout(1000)
if(!await page.locator('.model-image img').isVisible())throw Error('Offline lost rendered model')
await context.setOffline(false)
await context.close()
await browser.close()
const report={url:base,first_screen_ms:firstScreen,js_errors:errors,step:true,playback:true,mobile_overflow:false,retains_rendered_view_when_disconnected:true}
fs.writeFileSync(output+'/monitor-browser-20261111.json',JSON.stringify(report,null,2))
console.log(JSON.stringify(report))
if(errors.length)process.exitCode=1
