const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  let configured = true
  let sent = 0
  let deleted = 0
  const job = { id: 1, title: '测试报价', brief: '测试报价', status: 'draft', revision: 1,
    created_at: '2026-09-20T00:00:00Z', messages: [], files: [], agent_runs: [], quotes: [], requirements: [] }
  let jobs = [job, { ...job, id: 2, title: '运行中的会话', status: 'processing' }]
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'ui-test'))
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    let body = {}
    if (url.pathname === '/api/model-config') body = { configured, model: 'test' }
    else if (url.pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
    else if (url.pathname === '/api/options') body = { products: [], categories: [] }
    else if (url.pathname === '/api/jobs' && route.request().method() === 'POST') body = job
    else if (url.pathname === '/api/jobs/1/agent') {
      assert.equal(route.request().postDataJSON().allow_external_processing, true)
      sent++
      body = job
    } else if (url.pathname === '/api/jobs/1' && route.request().method() === 'DELETE') {
      assert.equal(route.request().postDataJSON().revision, job.revision)
      jobs = jobs.filter(item => item.id !== 1)
      deleted++
      body = { deleted: 1 }
    } else if (url.pathname === '/api/jobs/1') body = job
    else if (url.pathname === '/api/jobs') body = { items: jobs, total: jobs.length }
    await route.fulfill({ json: body })
  })
  try {
    await page.goto('http://127.0.0.1:5173')
    const input = page.getByRole('textbox', { name: '消息', exact: true })
    const send = page.getByRole('button', { name: '发送消息', exact: true })
    await input.waitFor()
    assert.equal(await page.locator('.chat-composer-area input[type=checkbox]').count(), 0)
    assert(await send.isDisabled())
    await input.fill('测试报价')
    assert(await send.isEnabled())
    await input.press('Shift+Enter')
    assert.equal(sent, 0)
    await input.press('Enter')
    await page.getByRole('button', { name: '继续查价', exact: true }).waitFor()
    assert.equal(sent, 1)
    await page.getByRole('button', { name: '继续查价', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('textarea').disabled)
    assert.equal(sent, 2)
    await page.getByRole('button', { name: '新对话', exact: true }).last().click()
    await page.locator('.chat-composer input[type=file]').setInputFiles({
      name: 'sample.png', mimeType: 'image/png', buffer: Buffer.from('test'),
    })
    assert(await send.isEnabled())
    await page.getByRole('button', { name: '移除附件', exact: true }).click()
    assert(await send.isDisabled())
    assert(await page.getByRole('button', { name: '删除会话：运行中的会话', exact: true }).isDisabled())
    await page.locator('.chat-history-item').filter({ hasText: '测试报价' }).click()
    await page.getByRole('button', { name: '继续查价', exact: true }).waitFor()
    await page.getByRole('button', { name: '删除会话：测试报价', exact: true }).click()
    await page.getByRole('button', { name: '取消', exact: true }).click()
    assert.equal(deleted, 0)
    await page.getByRole('button', { name: '删除会话：测试报价', exact: true }).click()
    await page.getByRole('button', { name: '删除', exact: true }).click()
    await page.getByRole('heading', { name: '报价助手', exact: true }).waitFor()
    await page.getByRole('button', { name: '删除会话：测试报价', exact: true }).waitFor({ state: 'hidden' })
    assert.equal(deleted, 1)
    const output = path.resolve(__dirname, '../.runtime/screenshots')
    mkdirSync(output, { recursive: true })
    await page.waitForFunction(() => !document.querySelector('.el-message'))
    await page.screenshot({ path: path.join(output, 'simple-chat-desktop.png'), animations: 'disabled' })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.screenshot({ path: path.join(output, 'simple-chat-mobile.png'), animations: 'disabled' })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    configured = false
    await page.reload()
    await page.locator('.chat-config-warning').waitFor()
    await input.fill('测试问题')
    assert(await send.isDisabled())
    assert.deepEqual(errors, [])
    console.log('PASS: direct send, keyboard, attachments, delete/cancel/processing guard, missing key, desktop/mobile.')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
