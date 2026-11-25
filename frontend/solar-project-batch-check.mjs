// Run against an isolated local workspace with the two deployed packages.
import assert from 'node:assert/strict'
import path from 'node:path'
import { chromium } from '@playwright/test'

const [base, ar, ch, screenshot, unknown] = process.argv.slice(2)
if (!base || !ar || !ch)
  throw Error('Usage: node solar-project-batch-check.mjs URL AR.jpg CH.jpg [screenshot.png] [unknown.png]')
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 940 } })
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
try {
  await page.goto(base + '/')
  await page.waitForURL((url) => url.pathname === '/monitor')
  await page.getByRole('link', { name: '太阳科学工作台', exact: true }).click()
  await page.getByRole('button', { name: /新建项目/ }).first().click()
  const dialog = page.getByRole('dialog')
  await dialog.getByPlaceholder('如：十一月太阳观测').fill('目标筛选验收 ' + Date.now())
  await dialog.getByRole('button', { name: '创建项目' }).click()
  await dialog.waitFor({ state: 'hidden' })
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.import-actions button')].some(
      button => button.textContent.trim() === '选择多张照片' && !button.disabled),
  )
  const sibling = path.join(path.dirname(ar), 'hmi.m_720s.20210130_000000_TAI.3.magnetogram.jpg')
  const files = unknown ? [ar, ch, sibling, unknown] : [ar, ch, sibling]
  await page.locator('input[type=file]:not([webkitdirectory])').setInputFiles(files)
  const thumbs = page.locator('.thumb')
  await thumbs.nth(files.length - 1).waitFor()
  await thumbs.first().click()
  await thumbs.nth(1).click({ modifiers: ['Control'] })
  await page.getByRole('heading', { name: '已选 2 张' }).waitFor()
  await page.getByRole('button', { name: '全选当前结果' }).click()

  const launch = page.getByRole('button', { name: '批量自动识别', exact: true })
  const flare = dialog.getByRole('button', { name: /活动区识别与耀斑风险试算/ })
  const coronal = dialog.getByRole('button', { name: /冕洞识别/ })
  const queue = dialog.getByRole('button', { name: /确认并加入队列/ })
  const device = dialog.getByRole('combobox', { name: '处理设备', exact: true })
  async function expectCount(count) {
    await dialog.getByRole('button', { name: '确认并加入队列（' + count + ' 张）', exact: true }).waitFor()
    if (count) await page.waitForFunction(() =>
      [...document.querySelectorAll('[role="dialog"] button')].some(b => b.textContent.includes('确认并加入队列') && !b.disabled),
    )
  }
  async function submit(target, category, count) {
    await device.selectOption('cpu')
    const sent = page.waitForRequest(r => new URL(r.url()).pathname.endsWith('/solar/project/batch') && r.method() === 'POST')
    await queue.click()
    const payload = (await sent).postDataJSON()
    assert.equal(payload.goal, target)
    assert.equal(payload.device, 'cpu')
    assert.equal(payload.selection.length, count)
    assert.ok(payload.selection.every(row => row.category === category))
    await dialog.waitFor({state:'hidden'})
    await page.getByText(count + ' / ' + count + ' 张已处理', { exact: false }).waitFor({ timeout: 180000 })
    assert.equal(await page.locator('.row-list button').count(), count)
  }

  await launch.click()
  assert.equal(await queue.isEnabled(), false)
  await flare.click()
  await expectCount(2)
  assert.match(await dialog.locator('.type-stats').innerText(), /HMI 磁图 2/)
  assert.match(await dialog.locator('.type-stats').innerText(), /AIA 观测图 1/)
  await dialog.locator('summary').filter({hasText:'目标不匹配'}).click()
  await dialog.getByText(path.basename(ch), {exact:true}).waitFor()
  if (unknown) {
    await dialog.locator('summary').filter({hasText:'逐张确认'}).click()
    const channel = dialog.getByLabel(path.basename(unknown) + ' 通道')
    await channel.selectOption('AR')
    await dialog.locator('summary').filter({hasText:'输入不支持或不可用'}).click()
    await dialog.getByText(/耀斑风险试算要求原生 1024/).waitFor()
    await channel.selectOption('')
  }

  await coronal.click()
  await expectCount(1)
  if (unknown) {
    await dialog.locator('summary').filter({hasText:'逐张确认'}).click()
    await dialog.getByLabel(path.basename(unknown) + ' 通道').selectOption('CH')
    await expectCount(2)
    await dialog.getByLabel(path.basename(unknown) + ' 通道').selectOption('')
    await expectCount(1)
  }
  await flare.click()
  await expectCount(2)
  await dialog.getByRole('button', {name:'近 24 小时'}).click()
  await dialog.getByText(/没有符合当前目标的照片/).waitFor()
  assert.equal(await queue.isEnabled(), false)
  await dialog.locator('summary').filter({hasText:'不在观测时间范围内'}).waitFor()
  await dialog.getByRole('button', {name:'清空范围'}).click()
  await expectCount(2)
  await device.selectOption('cpu')
  await dialog.getByRole('list').getByText(/CPU · 2 张/).waitFor()
  if (screenshot) await page.screenshot({path:path.join(path.dirname(screenshot),'confirmation.png')})
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(await page.locator('.batch-link').count(), 0)

  await launch.click()
  assert.equal(await queue.isEnabled(), false)
  await flare.click()
  await expectCount(2)
  await submit('flare-risk', 'AR', 2)
  await page.locator('.timeline').nth(1).waitFor({ timeout: 120000 })
  await page.reload()
  await page.locator('.row-list button').first().click()
  await page.getByRole('heading', { name: '事件试算' }).waitFor()
  const region = page.locator('.regions button').first()
  await region.click()
  assert.equal(await region.getAttribute('aria-pressed'), 'true')
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载整批结果' }).click()
  const file = await downloaded
  assert.equal(await file.failure(), null)
  if (screenshot) await file.saveAs(path.join(path.dirname(screenshot), 'browser-batch.zip'))

  await page.setViewportSize({ width: 390, height: 844 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
  await page.getByRole('button', { name: '返回照片', exact: false }).click()
  assert.equal(await thumbs.count(), files.length)
  await page.getByRole('button', { name: '全选当前结果' }).click()
  await launch.click()
  await coronal.click()
  await expectCount(1)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
  if (screenshot) await page.screenshot({ path: screenshot })
  await submit('coronal-hole', 'CH', 1)
  assert.match(await page.locator('.row-list button').first().innerText(), new RegExp(path.basename(ch)))
  await page.goto(base + '/recognize')
  await page.getByRole('heading', { name: '图片自动识别', exact: true }).waitFor()
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({
    photos:files.length, submittedHmi:2, submittedAia:1, manualChannel:!!unknown,
    switchedTargets:true, utcRange:true, emptyDisabled:true, cancelled:true, pairedForecasts:2,
    ctrlSelect:true, explicitCpu:true, reload:true, zip:true, mobile:true, errors,
  }))
} finally {
  await browser.close()
}
