import assert from 'node:assert/strict'
import fs from 'node:fs'
import {chromium} from '@playwright/test'

const base='http://127.0.0.1:8765',out='../out/acceptance/model-release-20261116/machine-products'
fs.mkdirSync(out,{recursive:true})
const cases=JSON.parse(fs.readFileSync('../out/acceptance/model-release-20261116/cases.json','utf8'))
const jobs=JSON.parse(fs.readFileSync('../out/acceptance/model-release-20261116/jobs.json','utf8'))
const browser=await chromium.launch({channel:'chrome',headless:true})
const page=await browser.newPage({acceptDownloads:true,viewport:{width:1600,height:1100}})
const errors=[];page.on('pageerror',e=>errors.push(e.message))
try{
 for(const dataset of cases.datasets){
  const job=jobs.find(j=>j.model_id.startsWith(dataset.category))
  await page.goto(base+'/annotate?'+new URLSearchParams({ws:cases.workspace,dataset:dataset.dataset,version:dataset.version}))
  const panel=page.getByLabel('太阳模型候选标注',{exact:true})
  await panel.locator('summary').first().click()
  await panel.getByText('最近候选任务（关闭页面后可在这里重新打开）',{exact:true}).click()
  await panel.locator('.history-row').filter({hasText:job.model_id}).first().click()
  const downloadPromise=page.waitForEvent('download',{timeout:60000})
  await panel.getByRole('button',{name:'下载整批自动标注',exact:false}).click()
  const download=await downloadPromise
  assert.equal(await download.failure(),null)
  await download.saveAs(`${out}/${dataset.category}.zip`)
  await page.screenshot({path:`${out}/${dataset.category}.png`})
 }
 assert.deepEqual(errors,[])
 fs.writeFileSync(out+'/browser.json',JSON.stringify({downloaded:['AR.zip','CH.zip'],pageErrors:errors,manualAcceptanceRequired:false},null,2))
}finally{await browser.close()}
