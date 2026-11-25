import { chromium } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'

const origin = process.env.MDVA_TRAINING_TEST_ORIGIN || 'http://127.0.0.1:8766'
const output = '../out/training-20261112-v2/progress-ui-acceptance'
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-gpu'] })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1300 }, acceptDownloads: true })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${origin}/training`)
  await page.getByRole('heading', { name: '训练进度与接续日志', exact: true }).waitFor()
  await page.locator('.timeline li').first().waitFor()
  assert.match(await page.locator('.progress-panel .badge').innerText(), /自动接续已开启/)
  const overall = page.getByRole('progressbar', { name: '登记实验完成进度', exact: true })
  assert.equal(await overall.getAttribute('max'), '42')
  assert.ok(Number(await overall.getAttribute('value')) >= 1)
  await page.getByRole('button', { name: '查看实时进度与下一步', exact: true }).click()
  await page.getByRole('dialog', { name: '实时进度与下一步', exact: true }).waitFor()
  assert.match(await page.locator('.file-preview').innerText(), /python -m mdva.training/)
  const pendingDownload = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载完整文件', exact: true }).click()
  const download = await pendingDownload
  assert.equal(download.suggestedFilename(), '训练进度与接续.md')
  await download.saveAs(`${output}/downloaded-progress.md`)
  assert.match(await readFile(`${output}/downloaded-progress.md`, 'utf8'), /下一步命令/)
  await page.getByRole('button', { name: '关闭预览', exact: true }).click()
  const firstRecord = await page.locator('.timeline li').first().innerText()
  await page.getByRole('button', { name: '下一页', exact: true }).first().click()
  await page.waitForFunction(first => document.querySelector('.timeline li')?.innerText !== first, firstRecord)
  await page.getByLabel('日志分类').selectOption('development')
  await page.waitForFunction(() => document.querySelectorAll('.timeline li').length > 0 && [...document.querySelectorAll('.timeline li')].every(x => x.classList.contains('development')))
  let fail = true
  await page.route('**/api/training/journal*', async route => {
    if (fail) { fail = false; await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: '验收模拟日志读取失败' }) }) }
    else await route.continue()
  })
  await page.getByRole('button', { name: '刷新日志', exact: true }).click()
  await page.locator('.journal-error').filter({ hasText: '验收模拟日志读取失败' }).waitFor()
  assert.ok(await page.locator('.timeline li').count())
  await page.getByRole('button', { name: '刷新日志', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('.journal-error'))
  await page.getByLabel('日志分类').selectOption('all')
  await page.locator('.training').evaluate(element => { element.scrollTop = 0 })
  await page.screenshot({ path: `${output}/desktop.png`, fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.training').evaluate(element => { element.scrollTop = 0 })
  await page.screenshot({ path: `${output}/mobile.png`, fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+2), false)
  assert.deepEqual(errors, [])
  await writeFile(`${output}/verification.json`, JSON.stringify({ passed: true, checks: [
    'live continued queue', 'real progress bars', 'persistent paginated journal', 'journal filters',
    'inline file preview', 'complete Unicode-filename download', 'failure retry preserves records',
    'desktop/mobile', 'no JavaScript errors',
  ] }, null, 2))
  console.log('持续训练进度、日志与交接文件 Chromium 验证通过')
} finally { await browser.close() }
