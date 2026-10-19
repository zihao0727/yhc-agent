const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'test'))
  const line = { id: 'l1', product: 'Composite sign', source_item: '1', quantity: 2,
    width_mm: '1200', height_mm: '450', material: 'Steel', thickness_mm: '2', process: 'Paint',
    language: 'all', text_content: '', evidence: [], uncertainties: [], extras: [], confirmed: false,
    selected_price_id: null, selected_price_revision: null, price_review_note: '', billing_length_mm: null }
  const file = { id: 1, filename: 'Drawing schedule.xlsx', page_count: 2, byte_size: 15000,
    media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    parser_version: 2, resource_summary: { discovered: 4, resolved: 3, unresolved: 1 } }
  const quoteLine = { line_id: 'l1', product: line.product, requirement: line, amount: null,
    unit_price: null, blockers: ['Component quantity is missing'], warnings: [], formula: '',
    resolution: 'recognition_review', deferred_issue: { line_id: 'l1', product: line.product,
      cause: 'recognition_review', reason: 'Component quantity is missing', missing_fields: ['components[0].quantity_per_item'],
      owner: 'Reviewer', diagnostic_id: 'test-diagnostic', repair_attempts: 2 },
    diagnostics: [{ code: 'missing_target_measurement', cause: 'recognition_review',
      paths: ['components[0].quantity_per_item'], message: 'Component quantity is missing', action: 'inspect_source' }] }
  const job = { id: 1, title: 'Pipeline verification', customer: '', brief: '', status: 'needs_review',
    revision: 1, created_at: '2026-10-08T00:00:00Z', updated_at: '2026-10-08T00:00:00Z',
    files: [file], messages: [], requirements: [line], extraction: { questions: [] }, runs: [], agent_runs: [],
    quotes: [{ id: 1, version: 1, status: 'blocked', outdated: false, terms: '', payload: {
      lines: [quoteLine], known_subtotal: '0', total: null, complete: false,
      resolution_counts: { recognition_review: 1 }, confirmation_items: [] } }] }
  const audit = { revision: 1, requires_review: 1, files: [file],
    metrics: { tool_calls: 8, repeated_calls: 2, tokens: 3500, saved_items: 0, deferred_items: 1 },
    coverage: [{ file_id: 1, page: 1, sheet: 'Items', row: 3, status: 'unresolved', reason: 'Image unreadable' }],
    lines: [{ line_id: 'l1', product: line.product, status: 'blocked', old_amount: null,
      shadow_amount: null, diagnostics: quoteLine.diagnostics, review_signals: [] }] }
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname
    const body = pathname === '/api/model-config' ? { configured: true }
      : pathname === '/api/embedding-config' ? { configured: false }
      : pathname === '/api/options' ? { products: [], categories: [] }
      : pathname === '/api/stats' ? { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
      : pathname === '/api/jobs' ? { items: [job], total: 1 }
      : pathname === '/api/jobs/1' ? job
      : pathname.endsWith('/pipeline-audit') ? audit
      : pathname.endsWith('/file-workspace') ? {
        runtime: { available: true },
        recent: [{ artifact_id: 'a'.repeat(32), purpose: 'Workbook column recovery', status: 'needs_repair',
          line_count: 1, execution_error: null, validation: { error: 'Quantity source requires correction' } }],
        adapters: [{ adapter_id: 'b'.repeat(64), name: 'Three-column workbook', status: 'candidate' }],
      }
      : pathname.endsWith('/reparse-preview') ? { resource_summary: { discovered: 4, resolved: 4, unresolved: 0 } }
      : pathname.endsWith('/progress') ? { id: 1, revision: 1, status: job.status, run: null, pricing_progress: null } : {}
    await route.fulfill({ json: body })
  })
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
    await page.locator('.chat-history-item').click()
    await page.locator('.chat-quote-total').waitFor()
    await page.locator('.chat-pricing-details > summary').click()
    assert.match(await page.locator('.failure-details').innerText(), /test-diagnostic/)
    for (const [name, width, height] of [['desktop', 1440, 960], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height })
      await page.getByRole('button', { name: '\u660e\u7ec6\u4e0e\u62a5\u4ef7', exact: true }).click()
      const drawer = page.locator('.el-drawer:visible')
      await drawer.getByRole('button', { name: '\u53ea\u8bfb\u6838\u67e5', exact: true }).click()
      await drawer.locator('.audit-table').waitFor()
      assert.match(await drawer.locator('.pipeline-audit').innerText(), /Composite sign/)
      await drawer.getByRole('button', { name: '刷新文件处理记录' }).click()
      await drawer.getByText('Workbook column recovery', { exact: true }).waitFor()
      assert.match(await drawer.locator('.file-workspace').innerText(), /Quantity source requires correction/)
      await drawer.getByRole('button', { name: '\u6838\u67e5\u539f\u6587\u4ef6\u56fe\u7247' }).click()
      await page.waitForFunction(() => [...document.querySelectorAll('.audit-file')].some(el => el.textContent.includes('4 / 4')))
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      assert(await drawer.evaluate(el => el.scrollWidth <= el.clientWidth + 1))
      await page.mouse.move(0, 0)
      await page.waitForTimeout(500)
      await drawer.locator('.file-workspace').screenshot({ path: path.join(output, `file-workspace-${name}.png`), animations: 'disabled' })
      await page.screenshot({ path: path.join(output, `pipeline-v3-${name}.png`), animations: 'disabled' })
      await drawer.locator('.el-drawer__close-btn').click()
      await drawer.waitFor({ state: 'hidden' })
    }
    assert.deepEqual(errors, [])
    console.log('PASS: diagnostic reasons, read-only audit and reparse checks on desktop/mobile; no page overflow or browser errors.')
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
