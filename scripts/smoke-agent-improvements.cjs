const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  const errors = []
  const requests = {}
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'test'))
  const raw = { id: 'line1', product: '门店入口标识牌', source_item: '1', quantity: null,
    width_mm: '300', height_mm: '120', material: '铝', thickness_mm: '3', process: '',
    evidence: [], uncertainties: [], extras: [], confirmed: false, estimate: {
      assumptions: [{ field: 'quantity', value: '2', reason: '暂定两处，待审核' }],
      costs: [], scope: '含制作，不含安装，费用待审核' } }
  const line = { line_id: 'line1', product: raw.product, requirement: raw, amount: '141.60',
    unit_price: '60.80', estimated: true, blockers: [], warnings: [], formula: '60.80 × 2 + 20 = 141.60',
    estimate_scope: raw.estimate.scope, estimate_review: [{ ...raw.estimate.assumptions[0], label: '数量', original: null, applied: true }],
    estimate_costs: [{ label: '制作', source: 'agent_estimate', basis: 'per_piece', amount: '60.80',
      rate: '60.80', quantity_formula: '=1', reason: '方案估价，非确认价格' }] }
  const run = { id: 1, status: 'processing', message: '网络暂时不可用，进度已保存。',
    created_at: '2026-09-20T00:00:00Z', usage: { durable: true, phase: 'retrying',
      retry_at: Date.now() / 1000 + 60, last_progress_at: Date.now() / 1000 }, steps: [] }
  const job = { id: 1, title: '门店标识报价', brief: '门店入口标识牌报价', customer: '', status: 'processing',
    revision: 1, created_at: run.created_at, updated_at: run.created_at, files: [], messages: [],
    requirements: [raw], extraction: {}, runs: [], quotes: [], agent_runs: [run], pending_count: 1,
    pricing_progress: { line_count: 3, priced_count: 1, known_subtotal: '141.60', saved_at: Date.now() / 1000 } }
  let patches = 0
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    requests[url.pathname] = (requests[url.pathname] || 0) + 1
    let body = {}
    if (url.pathname === '/api/model-config') body = { configured: true }
    else if (url.pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
    else if (url.pathname === '/api/options') body = { products: [], categories: [] }
    else if (url.pathname === '/api/jobs') body = { items: [job], total: 1 }
    else if (url.pathname.endsWith('/progress')) body = { id: 1, revision: job.revision,
      status: job.status, pricing_progress: job.pricing_progress, server_time: Date.now() / 1000,
      run: { ...run, phase: run.usage.phase, active_product: raw.product, offset: 0, step_count: 0 } }
    else if (url.pathname.endsWith('/conversation')) body = { job, changes: [{
      line_id: 'line1', field: 'quantity', value: '5', reason: '客户要求改为5件' }] }
    else if (url.pathname.endsWith('/changes')) {
      patches++
      assert.equal(route.request().postDataJSON().changes[0].value, '5')
      raw.quantity = 5
      job.revision++
      body = job
    } else if (url.pathname.endsWith('/quotes') && route.request().method() === 'POST') {
      const quote = structuredClone(job.quotes[0])
      quote.id = 2; quote.version = 2; quote.payload.total = '324.00'
      quote.payload.lines[0].amount = '324.00'; quote.payload.lines[0].requirement.quantity = 5
      job.quotes.unshift(quote)
      body = quote
    } else if (url.pathname === '/api/jobs/1') body = job
    await route.fulfill({ json: body })
  })
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
    await page.locator('.chat-history-item').click()
    await page.locator('.chat-progress-strip').waitFor()
    assert.match(await page.locator('.chat-progress-strip').innerText(), /等待重试/)
    const initialConfigRequests = requests['/api/model-config']
    const initialOptionsRequests = requests['/api/options']
    await page.waitForFunction(() => document.querySelector('.chat-progress-strip')?.textContent.includes('门店入口标识牌'))
    assert.equal(requests['/api/model-config'], initialConfigRequests)
    assert.equal(requests['/api/options'], initialOptionsRequests)
    assert.equal(await page.getByRole('button', { name: '恢复中断任务' }).count(), 0)
    await page.screenshot({ path: path.join(output, 'agent-progress-desktop.png') })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1)
    await page.screenshot({ path: path.join(output, 'agent-progress-mobile.png'), animations: 'disabled' })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    job.status = 'quoted'; run.status = 'succeeded'
    job.quotes = [{ id: 1, version: 1, status: 'draft', outdated: false, terms: '待审核条款',
      payload: { lines: [structuredClone(line)], total: '141.60', known_subtotal: '141.60', complete: true,
        has_estimates: true, confirmation_items: [] } }]
    await page.getByRole('button', { name: '查看与审核报价' }).waitFor({ timeout: 10000 })
    assert.equal(await page.locator('.chat-transcript .estimate-review').count(), 0)
    await page.getByRole('button', { name: '查看与审核报价' }).click()
    await page.locator('.estimate-review').waitFor()
    await page.locator('.review-item > summary').click()
    await page.locator('.review-values').waitFor()
    await page.locator('.estimate-review').scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'agent-review-mobile.png') })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.setViewportSize({ width: 1440, height: 960 })
    await page.waitForFunction(() => [...document.querySelectorAll('.el-drawer')].some(el =>
      el.getBoundingClientRect().width >= 1000 && el.getBoundingClientRect().height > 0))
    await page.screenshot({ path: path.join(output, 'agent-review-desktop.png'), animations: 'disabled' })
    await page.locator('.el-drawer__close-btn:visible').click()
    await page.getByRole('textbox', { name: '消息', exact: true }).fill('第一项数量改成5件')
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '确认局部需求修改' })
    await dialog.waitFor()
    assert.equal(patches, 0)
    await page.screenshot({ path: path.join(output, 'agent-patch-confirmation.png'), animations: 'disabled' })
    await page.getByRole('button', { name: '确认修改并重新计价' }).click()
    await page.locator('.quote-version-diff').waitFor()
    assert.equal(patches, 1)
    assert.match(await page.locator('.quote-version-diff').innerText(), /324.00/)
    assert.deepEqual(errors, [])
    console.log('PASS: incremental polling, retry status, desktop/mobile review, explicit patch confirmation, version comparison.')
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
