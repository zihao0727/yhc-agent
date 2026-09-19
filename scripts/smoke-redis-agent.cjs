const { chromium } = require('playwright')
const { readFileSync, mkdirSync } = require('node:fs')
const { parseEnv } = require('node:util')
const path = require('node:path')
const assert = require('node:assert/strict')

async function main() {
  const root = path.resolve(__dirname, '..')
  const config = parseEnv(readFileSync(path.join(root, 'backend/.env'), 'utf8'))
  const output = path.join(root, '.runtime', 'screenshots')
  mkdirSync(output, { recursive: true })
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.goto('http://127.0.0.1:5173')
    await page.getByLabel('管理口令', { exact: true }).fill(config.ADMIN_TOKEN)
    await page.getByRole('button', { name: '进入工作区' }).click()
    await page.locator('.chat-history-item').first().click()
    const progress = page.getByText('整单计费进行中', { exact: true })
    await progress.waitFor()
    await progress.scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'redis-progress-desktop.png') })
    assert.match(await page.locator('.chat-transcript').innerText(), /已存入 Redis/)
    assert.match(await page.locator('.chat-transcript').innerText(), /含待审核估价/)
    await page.setViewportSize({ width: 390, height: 844 })
    await progress.scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'redis-progress-mobile.png') })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    assert.deepEqual(errors, [])
    console.log('PASS: live Redis progress, provisional subtotal, desktop/mobile layout.')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
