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
  const line = { id: 'panel', product: '门头双面铝板', quantity: 2, width_mm: '1000', height_mm: '500',
    thickness_mm: '3', material: '铝', process: '制作', evidence: [], uncertainties: [], extras: [],
    confirmed: false, text_content: '', notes: '', language: 'unknown',
    selected_price_id: null, selected_price_revision: null, billing_length_mm: null }
  const run = { id: 10, status: 'processing', phase: 'running', message: '正在计算其他项目',
    created_at: '2026-10-12T01:00:00Z', usage: { prepared: true, durable: true,
      deferred_requirements: [{ line_id: 'panel', product: line.product, cause: 'recognition_review',
        reason: '图纸与表格宽度不一致', question: '宽度采用1000毫米还是2000毫米？', missing_fields: [] }] },
    steps: [], step_count: 0 }
  const job = { id: 1, title: '门头制作报价', brief: '双板制作及加工', customer: '',
    status: 'processing', revision: 1, created_at: run.created_at, updated_at: run.created_at,
    files: [], messages: [], requirements: [line], extraction: { summary: '', questions: [] },
    runs: [], agent_runs: [run], pending_count: 1, quotes: [], pricing_progress: null }
  let queued = false
  let submissions = 0
  let stops = 0
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname
    let body = {}
    if (pathname === '/api/model-config') body = { configured: true }
    else if (pathname === '/api/embedding-config') body = { configured: false }
    else if (pathname === '/api/options') body = { products: [], categories: [] }
    else if (pathname === '/api/stats') body = { prices: 0, products: 0, rules: 0, documents: 0 }
    else if (pathname === '/api/jobs') body = { items: [job], total: 1 }
    else if (pathname === '/api/jobs/1') body = job
    else if (pathname === '/api/jobs/1/agent/stop') { stops++; body = job }
    else if (pathname === '/api/jobs/1/conversation') {
      const data = route.request().postDataJSON()
      assert.match(data.supplementary_text, /2000/)
      submissions++
      queued = true
      job.messages.push({ role: 'user', text: data.supplementary_text, pending_reply: true,
        at: '2026-10-12T01:01:00Z' })
      body = { job, queued: true, changes: [], operations: [] }
    } else if (pathname === '/api/jobs/1/progress') {
      if (queued) {
        queued = false
        job.revision++
        line.width_mm = '2000'
        job.messages[0].pending_reply = false
        job.messages.push({ role: 'assistant', text: '宽度已采用2000毫米，重新计价为480.00元，待审核。',
          at: '2026-10-12T01:02:00Z' })
        run.usage.deferred_requirements = []
        job.quotes = [{ id: 1, version: 1, status: 'draft', outdated: false,
          terms: '制作报价待审核', payload: { complete: true, total: '480.00', known_subtotal: '480.00',
            has_estimates: true, lines: [{ line_id: line.id, product: line.product, requirement: line,
              amount: '480.00', unit_price: '240.00', formula: '240 x 2', estimated: true,
              blockers: [], warnings: ['自定义工程量待审核'], estimate_costs: [{
                label: '双板材料', measurement_note: '两块矩形板，毫米换算平方米',
                resolved_formula: '100 x 2 = 200', amount: '200', measure: '2',
                script_run: { source: 'front = width_mm * height_mm / 1000000\nresult = front * 2',
                  inputs: { width_mm: '2000', height_mm: '500' }, result: '2',
                  checks: [{ message: '宽度大于零', passed: true }] },
              }] }] } }]
      }
      body = { id: job.id, revision: job.revision, status: job.status, messages: job.messages,
        pricing_progress: null, run: { ...run, offset: 0 } }
    }
    await route.fulfill({ json: body })
  })
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
    await page.locator('.chat-history-item').click()
    await page.locator('.chat-pending-issues').waitFor()
    const input = page.getByRole('textbox', { name: '消息', exact: true })
    assert(await input.isEnabled())
    await input.fill('采用2000毫米，请重新计算')
    await page.getByRole('button', { name: '发送消息', exact: true }).click()
    await page.getByText('宽度已采用2000毫米，重新计价为480.00元，待审核。', { exact: true }).waitFor()
    assert.equal(submissions, 1)
    assert.equal(stops, 0)
    await page.locator('.chat-pricing-details > summary').click()
    await page.getByText('双板材料 · 计算与校验', { exact: true }).click()
    for (const [name, width, height] of [['desktop', 1440, 960], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height })
      await page.locator('.pricing-details pre').scrollIntoViewIfNeeded()
      assert(await page.locator('.pricing-details pre').isVisible())
      assert(await input.isEnabled())
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      await page.screenshot({ path: path.join(output, `collaborative-${name}.png`), animations: 'disabled' })
    }
    assert.deepEqual(errors, [])
    console.log('PASS: live question reply, no stop, revised quote and script audit on desktop/mobile')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
