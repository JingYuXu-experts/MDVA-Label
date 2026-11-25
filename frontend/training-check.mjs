import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'

const origin = process.env.MDVA_TRAINING_TEST_ORIGIN || 'http://127.0.0.1:8766'
const output = '../out/training-20261112-v2/browser'
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-gpu'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
await page.goto(`${origin}/training`)
await page.getByRole('heading', { name: '本地模型实验', exact: true }).waitFor()
await page.locator('.runs tbody tr').first().waitFor()
assert.equal(await page.locator('.runs tbody tr').count(), 42)
assert.equal(await page.locator('.phases article').count(), 7)
await page.getByLabel('筛选类别').selectOption('AR')
assert.equal(await page.locator('.runs tbody tr').count(), 6)
await page.locator('.runs tbody tr').first().click()
await page.locator('.details h2').filter({ hasText: 'AR-A0-s17' }).waitFor()
await page.getByLabel('筛选类别').selectOption('FL')
await page.getByRole('button', { name: 'FL-F2-s17', exact: true }).click()
await page.locator('.details svg').waitFor()
await page.route('**/api/training/run**', async route => {
  if (new URL(route.request().url()).searchParams.get('id') === 'AR-A1-s17') {
    await new Promise(resolve => setTimeout(resolve, 300))
  }
  await route.continue()
})
await page.getByLabel('筛选类别').selectOption('AR')
await page.getByRole('button', { name: 'AR-A1-s17', exact: true }).click()
await page.getByLabel('筛选类别').selectOption('FL')
await page.getByRole('button', { name: 'FL-F2-s17', exact: true }).click()
await page.waitForTimeout(500)
assert.equal(await page.locator('.details h2').innerText(), 'FL-F2-s17')
const summary = await (await page.request.get(`${origin}/api/training/summary`)).json()
assert.equal(summary.forecast_revision, 'forecast-v2')
assert.equal(summary.forecast.length, 31)
assert.equal(await page.locator('.error').count(), 0)
await page.screenshot({ path: `${output}/desktop.png`, fullPage: true })
await page.setViewportSize({ width: 390, height: 844 })
await page.screenshot({ path: `${output}/mobile.png`, fullPage: true })
const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)
assert.equal(overflow, false, 'mobile page overflow')
assert.deepEqual(errors, [])
await writeFile(`${output}/verification.json`, JSON.stringify({
  passed: true, desktop: '1440x1000', mobile: '390x844', runs: 42, phases: 7,
  checks: ['real API', 'corrected forecast v2: 31 models', 'category filter', 'run switch', 'stale response rejection', 'learning curve', 'mobile overflow', 'no JS error'],
}, null, 2))
await browser.close()
console.log('训练实验页 Chromium 桌面/手机验证通过')
