const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'test'))
  let configured = true
  let saved = false
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    let body = {}
    if (url.pathname === '/api/model-config') {
      body = { configured: true, model: 'deepseek-flash', provider: 'DeepSeek', environment_managed: false }
    } else if (url.pathname === '/api/embedding-config') {
      if (route.request().method() === 'PUT') {
        assert.equal(route.request().postDataJSON().api_key, 'fake-ui-key-not-a-real-secret')
        saved = true
        configured = true
      } else if (route.request().method() === 'DELETE') {
        configured = false
      }
      body = { configured, model: 'Qwen/Qwen3-Embedding-4B', provider: '硅基流动', environment_managed: false }
    } else if (url.pathname === '/api/stats') {
      body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
    } else if (url.pathname === '/api/options') {
      body = { products: [], categories: [] }
    } else if (url.pathname === '/api/jobs') {
      body = { items: [], total: 0 }
    }
    await route.fulfill({ json: body })
  })
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5175')
    await page.getByRole('button', { name: '模型配置', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '模型配置' })
    await dialog.getByRole('tab', { name: '硅基流动 · 检索' }).click()
    await dialog.getByText('Qwen/Qwen3-Embedding-4B', { exact: true }).waitFor()
    assert.equal(await dialog.locator('input[type=password]').inputValue(), '')
    await page.screenshot({ path: path.join(output, 'semantic-config-desktop.png'), animations: 'disabled' })
    await page.setViewportSize({ width: 390, height: 844 })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    const bounds = await dialog.locator('.model-info strong').boundingBox()
    const dialogBounds = await dialog.boundingBox()
    assert(bounds.x >= dialogBounds.x && bounds.x + bounds.width <= dialogBounds.x + dialogBounds.width)
    await page.screenshot({ path: path.join(output, 'semantic-config-mobile.png'), animations: 'disabled' })
    await dialog.locator('input[type=password]').fill('fake-ui-key-not-a-real-secret')
    await dialog.getByRole('tab', { name: 'DeepSeek · 报价' }).click()
    assert.equal(await dialog.locator('input[type=password]').inputValue(), '')
    await dialog.getByRole('tab', { name: '硅基流动 · 检索' }).click()
    await dialog.locator('input[type=password]').fill('fake-ui-key-not-a-real-secret')
    await dialog.getByRole('button', { name: '保存密钥' }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert(saved)
    await page.getByRole('button', { name: '模型配置', exact: true }).click()
    assert.equal(await dialog.locator('input[type=password]').inputValue(), '')
    await dialog.getByRole('button', { name: '删除密钥' }).click()
    await page.getByRole('button', { name: '删除', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert(!configured)
    assert.deepEqual(errors, [])
    console.log('PASS: 4B config, separate provider keys, tab reset, save/delete, desktop/mobile layout.')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
