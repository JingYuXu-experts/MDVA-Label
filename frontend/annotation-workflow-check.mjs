import assert from 'node:assert/strict'
import fs from 'node:fs'
import {chromium} from '@playwright/test'
const root='../out/acceptance/annotation-ux-20261112',ds=JSON.parse(fs.readFileSync(root+'/context.json'))
const browser=await chromium.launch({executablePath:'C:/Users/Administrator/AppData/Local/ms-playwright/chromium-1217/chrome-win64/chrome.exe',headless:true})
const context=await browser.newContext({viewport:{width:1600,height:1000}}),page=await context.newPage(),errors=[]
page.on('pageerror',e=>errors.push(String(e)))
const base='http://127.0.0.1:8766'
async function settle(){await page.locator('.stage__frame').waitFor();await page.waitForFunction(()=>{const e=document.querySelector('.stage__frame');return e&&e.complete&&e.naturalWidth>0&&!document.querySelector('.stage-spinner')});await page.waitForTimeout(250)}
async function box(offset=0){await page.getByRole('radio',{name:'框',exact:true}).click();const r=await page.locator('.stage__svg').boundingBox();assert.ok(r);await page.mouse.move(r.x+r.width*(.32+offset),r.y+r.height*.34);await page.mouse.down();await page.mouse.move(r.x+r.width*(.54+offset),r.y+r.height*.58,{steps:7});await page.mouse.up()}
try{
 await page.goto(base+`/annotate?ws=.&dataset=${ds.dataset_id}&version=${ds.version_id}`);await settle()
 await page.locator('.queue-item .thumbnail img').first().waitFor({timeout:30000});assert.equal(await page.locator('.queue-item').count(),60)
 await page.getByText('列表显示设置',{exact:true}).click();await page.getByLabel('缩略图大小',{exact:true}).fill('112');assert.ok((await page.locator('.thumbnail').first().boundingBox()).height>=110)
 await page.locator('.queue-item').last().click();await settle();await page.getByRole('button',{name:'下一张',exact:true}).click();await settle();assert.match(await page.locator('.rail header').innerText(),/#61/)
 await page.getByRole('button',{name:'上一页',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.queue-item')?.textContent.includes('solar_001'));await page.locator('.queue-item').first().click();await settle()
 await box();assert.equal(await page.locator('g.ann').count(),1)
 let held=false,failSave=false
 await page.route(url=>url.pathname==='/api/annotations',async route=>{if(route.request().method()==='POST'&&failSave){failSave=false;await route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'回归测试：模拟保存失败'})});return}if(route.request().method()==='POST'&&!held){held=true;await new Promise(r=>setTimeout(r,1500))}await route.continue()})
 await page.locator('.toolbar').getByRole('button',{name:/^保存/}).click();await page.waitForTimeout(250);await box(.15)
 await page.waitForTimeout(1900);assert.equal(await page.locator('g.ann').count(),2);await page.getByLabel('有未保存修改',{exact:true}).waitFor()
 failSave=true;await page.locator('.toolbar').getByRole('button',{name:/^保存/}).click();await page.locator('.save-error').waitFor();assert.equal(await page.locator('g.ann').count(),2)
 await page.locator('.toolbar').getByRole('button',{name:/^保存/}).click();await page.waitForFunction(()=>!document.querySelector('[aria-label="有未保存修改"]'))
 await page.screenshot({path:root+'/annotation-list.png'})
 await page.getByRole('button',{name:'保存并下一张',exact:true}).click();await settle();assert.match(await page.locator('.annotate__uri').innerText(),/solar_002/)
 await page.getByRole('button',{name:'上一张',exact:true}).click();await settle();assert.equal(await page.locator('g.ann').count(),2)
 await page.locator('.toolbar').getByRole('button',{name:'提交审核',exact:true}).click();await page.locator('.annrow__act[title=通过]').first().waitFor();await page.locator('.annrow__act[title=通过]').first().click();await page.locator('.annrow__act[title=通过]').first().click();await page.waitForFunction(()=>document.querySelectorAll('.annrow__act[title=通过]').length===0);assert.equal(await page.getByLabel('有未保存修改',{exact:true}).count(),0);await page.getByTitle('退回修订',{exact:true}).first().click();await settle()
 await page.getByRole('radio',{name:'多边形',exact:true}).click();const poly=await page.locator('.stage__svg').boundingBox();for(const [x,y] of [[.4,.3],[.58,.32],[.56,.52]])await page.mouse.click(poly.x+poly.width*x,poly.y+poly.height*y);await page.keyboard.press('Enter');await page.locator('g.ann polygon').waitFor({timeout:3000});assert.equal(await page.locator('g.ann polygon').count(),1)
 await page.getByRole('navigation',{name:'数据处理流程'}).getByRole('link',{name:'质量清洗',exact:false}).click();await page.getByRole('dialog').waitFor()
 await page.getByRole('button',{name:'继续编辑',exact:true}).click();assert.ok(page.url().includes('/annotate'))
 await page.getByRole('navigation',{name:'数据处理流程'}).getByRole('link',{name:'质量清洗',exact:false}).click();await page.getByRole('button',{name:'保存并继续',exact:true}).click();await page.waitForURL('**/clean**')
 await page.getByRole('button',{name:/重复图片检查/}).click();await page.getByRole('button',{name:'检查这批图片',exact:true}).click();await page.getByRole('button',{name:'生成清洗版本',exact:true}).waitFor();await page.waitForFunction(()=>!document.querySelector('button.check')?.disabled)
 await page.getByRole('button',{name:'处理范围',exact:true}).click();await page.getByRole('option',{name:'整个版本',exact:true}).click();await page.waitForTimeout(200)
 assert.equal(await page.getByRole('button',{name:'生成清洗版本',exact:true}).isDisabled(),true)
 await page.getByRole('button',{name:'检查这批图片',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.summary')!==null);assert.match(await page.locator('.summary').innerText(),/72/)
 await page.waitForFunction(()=>{const img=document.querySelector('.candidates img');return !img||img.complete&&img.naturalWidth>0});await page.screenshot({path:root+'/clean-candidates.png'})
 await page.getByRole('button',{name:'生成清洗版本',exact:true}).click();await page.getByRole('button',{name:'确认生成',exact:true}).click();await page.locator('.success').waitFor({timeout:30000})
 await page.getByRole('link',{name:'下一步：图像增强 →',exact:true}).click();await page.waitForURL('**/augment**')
 await page.locator('.preset').first().waitFor();assert.equal(await page.locator('.preset').count(),2);const recipeText=await page.locator('.recipe').innerText();assert.ok(!/医学|计算机视觉|纯时序/.test(recipeText))
 await page.getByRole('button',{name:'预览这一张的效果',exact:true}).click();await page.getByAltText('实际增强效果',{exact:true}).waitFor({timeout:30000});await page.screenshot({path:root+'/augment-comparison.png'})
 await page.waitForFunction(()=>document.querySelector('[data-can-run="true"]')!==null);await page.getByRole('button',{name:'生成增强版本',exact:true}).click();await page.getByRole('dialog').getByRole('button',{name:'执行',exact:true}).click();await page.waitForFunction(()=>Number(document.querySelector('[data-lineage-rows]')?.getAttribute('data-lineage-rows'))>0,null,{timeout:30000})
 await page.getByRole('navigation',{name:'数据处理流程'}).getByRole('link',{name:'结果导出',exact:false}).click();await page.waitForURL('**/export**')
 await page.locator('input[name="export-format"][value="coco"]').check();await page.getByRole('button',{name:'导出 coco',exact:true}).click();await page.locator('.result__dir').waitFor({timeout:30000});await page.getByRole('button',{name:'回读校验',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[data-validated="true"]')!==null,{timeout:30000})
 await page.screenshot({path:root+'/export-result.png'});assert.deepEqual(errors,[])
 fs.writeFileSync(root+'/browser-report.json',JSON.stringify({thumbnails:true,resizable:true,cross_page_navigation:true,edits_during_save_preserved:true,failed_save_retry:true,save_and_leave:true,save_and_next:true,review_round_trip:true,polygon_saved:true,stale_clean_blocked:true,cleaned_version:true,aerospace_only:true,actual_augmentation_preview:true,augmentation_run:true,export_reimport:true,errors},null,2))
 process.stdout.write('Annotation workflow checks passed\n')
}catch(e){await page.screenshot({path:root+'/failure.png',fullPage:true});process.stdout.write((await page.locator('body').innerText()).slice(-2500)+'\n');throw e}finally{await context.close();await browser.close()}
