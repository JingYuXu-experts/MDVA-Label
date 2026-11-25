// Run against an isolated workspace with a 63-image "观测档案", a "日面备份" project and two upload files.
import assert from 'node:assert/strict'
import { chromium } from '@playwright/test'

const [base, folder, screenshot] = process.argv.slice(2)
if (!base || !folder) throw Error('Usage: node solar-project-library-check.mjs URL upload-folder [screenshot.png]')
const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1450, height: 950 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
try {
  await page.goto(base + '/solar')
  const nav = page.getByRole('navigation', { name: '图像项目' })
  const archive = nav.getByRole('button', { name: /^观测档案/ })
  const backup = nav.getByRole('button', { name: /^日面备份/ })
  await backup.click()
  await page.locator('.project-context').getByText(/日面备份/).waitFor()
  await archive.click()
  await page.locator('.project-context').getByText(/观测档案/).waitFor()

  await page.getByRole('button', { name: '从项目选取' }).click()
  const dialog = page.getByRole('dialog', { name: '从项目选取照片' })
  await dialog.getByText('63 张照片 · 已选 0 张').waitFor()
  await dialog.locator('.preview img').first().evaluate(img => img.decode())
  await dialog.getByRole('button', { name: '全选本页' }).click()
  await dialog.getByText('63 张照片 · 已选 60 张').waitFor()
  await dialog.getByRole('button', { name: '全选全部照片' }).click()
  await dialog.getByText('63 张照片 · 已选 63 张').waitFor()
  await dialog.getByRole('button', { name: '清除', exact: true }).click()

  await dialog.getByRole('button', { name: '选择 obs-00.png' }).click()
  await dialog.getByRole('button', { name: '下一页' }).click()
  await dialog.getByRole('button', { name: '选择 obs-62.png' }).click()
  await dialog.getByText('63 张照片 · 已选 2 张').waitFor()
  if (screenshot) await page.screenshot({ path: screenshot })
  await dialog.getByRole('button', { name: '导入所选 2 张' }).click()
  await page.getByText('新增 2 张 · 已有 0 张 · 失败 0 张').waitFor()
  assert.equal(await page.locator('.thumb').count(), 2)

  await page.locator('input[webkitdirectory]').setInputFiles(folder)
  await page.locator('.thumb').nth(3).waitFor()
  await page.getByRole('heading', { name: '已选 2 张', exact: true }).waitFor()
  await page.locator('.thumb img').last().evaluate(img => img.decode())

  await nav.getByRole('button', { name: '最近', exact: true }).click()
  await nav.getByRole('heading', { name: '最近打开' }).waitFor()
  await nav.getByRole('button', { name: '常用', exact: true }).click()
  await nav.getByRole('heading', { name: '最常用' }).waitFor()
  await nav.getByRole('button', { name: '置顶 观测档案', exact: true }).click()
  await nav.getByRole('region', { name: '收藏置顶' }).getByRole('button', { name: /^观测档案/ }).waitFor()
  await page.reload()
  await nav.getByRole('button', { name: '取消置顶 观测档案' }).waitFor()
  await page.locator('.thumb').nth(3).waitFor()
  await page.setViewportSize({width: 390, height: 844})
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ sourceImages: 63, crossPageImport: 2, folderImport: 2, thumbnails: true, persistedPin: true, mobile: true, errors }))
} finally {
  await browser.close()
}
