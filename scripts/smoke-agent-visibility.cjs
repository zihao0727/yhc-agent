const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  const errors = []
  const detailRequests = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'test'))
  const requirement = { id: 'line1', product: 'Entrance sign', source_item: '1',
    quantity: 2, width_mm: '300', height_mm: '120', thickness_mm: '3',
    material: 'Aluminium', process: 'Print', text_content: 'Entrance',
    evidence: [], uncertainties: [], extras: [], confirmed: false }
  const run = { id: 33, status: 'succeeded', message: 'Quotation is ready for review.',
    created_at: '2026-10-08T00:01:00Z', usage: { total_tokens: 689537, durable: true },
    steps: Array.from({ length: 202 }, (_, index) => ({
      tool: index % 2 ? 'search_pricing_cases' : 'read_requirement',
      arguments: { query: `INTERNAL_QUERY_${index}` }, result: { total: 2803 },
    })) }
  const job = { id: 1, title: 'Sign quotation', brief: 'Two entrance signs', customer: '',
    status: 'quoted', revision: 1, created_at: '2026-10-08T00:00:00Z',
    updated_at: run.created_at, files: [], messages: [], requirements: [requirement],
    extraction: { summary: 'Customer requirements', questions: ['Confirm installation scope.'] },
    runs: [], agent_runs: [run], pending_count: 1, pricing_progress: null,
    quotes: [{ id: 1, version: 1, status: 'draft', outdated: false, terms: 'Installation excluded.',
      payload: { complete: true, total: '120.00', known_subtotal: '120.00',
        confirmation_items: [], lines: [{ line_id: requirement.id, product: requirement.product,
          requirement, amount: '120.00', unit_price: '60.00', blockers: [], warnings: [],
          formula: '60 x 2 = 120' }] } }] }
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname
    if (/\/steps\//.test(pathname)) detailRequests.push(pathname)
    let body = {}
    if (pathname === '/api/model-config') body = { configured: true }
    else if (pathname === '/api/embedding-config') body = { configured: false }
    else if (pathname === '/api/options') body = { products: [], categories: [] }
    else if (pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0,
      inactive: 0, products: 0, rules: 0, documents: 0 }
    else if (pathname === '/api/jobs') body = { items: [job], total: 1 }
    else if (pathname === '/api/jobs/1') body = job
    else if (pathname === '/api/jobs/1/progress') body = { id: job.id, revision: job.revision,
      status: job.status, pricing_progress: job.pricing_progress, run: null }
    await route.fulfill({ json: body })
  })
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  async function assertNoExecutionDetails(scope) {
    assert.equal(await scope.locator('.agent-progress, .agent-step, .chat-tool-step, .agent-step-details').count(), 0)
    assert.equal(await scope.locator('.chat-run > details').count(), 0)
    assert.doesNotMatch(await scope.innerText(), /Agent #33|689537|202\s*\u6b65|INTERNAL_QUERY_|read_requirement|search_pricing_cases/)
  }
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
    await page.locator('.chat-history-item').click()
    await page.locator('.chat-quote-total').waitFor()
    for (const [name, width, height] of [['desktop', 1440, 960], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height })
      await assertNoExecutionDetails(page.locator('.chat-main'))
      assert.match(await page.locator('.chat-run').innerText(), /Quotation is ready for review/)
      assert.match(await page.locator('.chat-quote-total').innerText(), /120\.00/)
      await page.screenshot({ path: path.join(output, `agent-hidden-chat-${name}.png`), animations: 'disabled' })
      await page.getByRole('button', { name: '\u660e\u7ec6\u4e0e\u62a5\u4ef7', exact: true }).click()
      const drawer = page.locator('.el-drawer:visible')
      await drawer.waitFor()
      await drawer.locator('.el-tabs__item').first().click()
      await assertNoExecutionDetails(drawer)
      assert.match(await drawer.innerText(), /Confirm installation scope/)
      assert.match(await drawer.innerText(), /Entrance sign/)
      assert.equal(await drawer.locator('.el-tab-pane:visible .el-table__body tr').count(), 1)
      await page.screenshot({ path: path.join(output, `agent-hidden-requirements-${name}.png`), animations: 'disabled' })
      await drawer.locator('.el-tabs__item').nth(1).click()
      await drawer.locator('.quote-total').waitFor()
      assert.match(await drawer.locator('.quote-total').innerText(), /120\.00/)
      await page.screenshot({ path: path.join(output, `agent-hidden-drawer-${name}.png`), animations: 'disabled' })
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      await drawer.locator('.el-drawer__close-btn').click()
      await drawer.waitFor({ state: 'hidden' })
    }
    job.status = 'processing'
    run.status = 'processing'
    run.message = 'Working on the quotation.'
    run.phase = 'running'
    await page.setViewportSize({ width: 1440, height: 960 })
    await page.reload()
    await page.locator('.chat-history-item').click()
    await page.locator('.chat-progress-strip').waitFor()
    await assertNoExecutionDetails(page.locator('.chat-main'))
    assert(await page.getByRole('button', { name: '\u505c\u6b62\u751f\u6210', exact: true }).isEnabled())
    assert(await page.getByRole('textbox', { name: '\u6d88\u606f', exact: true }).isDisabled())
    assert.deepEqual(detailRequests, [])
    assert.deepEqual(errors, [])
    console.log('PASS: 202 internal steps hidden in chat/drawer on desktop/mobile; quote, review, progress and stop controls retained; no step-detail requests.')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
