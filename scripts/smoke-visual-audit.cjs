const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
  const errors = [], regions = [], approvals = []
  page.on('pageerror', e => errors.push(e.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'test'))
  const file = { id: 1, filename: 'drawing.pdf', page_count: 1, byte_size: 1000, media_type: 'application/pdf' }
  const line = { id: 'l1', product: 'Door DD-01', source_item: '1', quantity: 2, width_mm: '300',
    height_mm: '200', material: 'Steel', language: 'all', thickness_mm: '2', process: 'Paint',
    evidence: [{ file_id: 1, page: 1, text: 'DD-01' }], uncertainties: [], extras: [], confirmed: true,
    selected_price_id: null, selected_price_revision: null, price_review_note: '', manual_unit_price: '100',
    manual_price_note: 'Reviewed', text_content: '', billing_length_mm: null }
  const quote = { id: 1, version: 1, status: 'draft', outdated: false, terms: 'Production only', payload: {
    lines: [{ line_id: 'l1', product: line.product, requirement: line, amount: '200.00', unit_price: '100.00',
      blockers: [], warnings: [], review_signals: [], formula: '100 * 2', resolution: 'priced' }],
    known_subtotal: '200.00', total: '200.00', complete: true, currency: 'CNY',
    source_review: { required: true, unresolved_regions: 1, unresolved_links: 1 }, confirmation_items: [],
    review_questions: ['DD-01 side view needs review'] } }
  const job = { id: 1, title: 'Visual audit', customer: '', brief: '', status: 'quoted', revision: 1,
    created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z', files: [file], messages: [],
    requirements: [line], extraction: { questions: [] }, runs: [], agent_runs: [], quotes: [quote] }
  const audit = { revision: 1, requires_review: 1, files: [file], metrics: null,
    coverage: [
      { file_id: 1, page: 1, row: 0, sheet: '', region: [0, 0, 1, .5], kind: 'region',
        expected_items: 2, extracted_items: 1, status: 'unresolved', reason: 'Review side view' },
      { file_id: 1, page: 1, row: 0, sheet: '', region: [0, 0, 1, .5], kind: 'view',
        label: 'DD-01 SIDE', status: 'unresolved', reason: 'Review side view' }],
    source_links: [{ file_ids: [1, 2], product: line.product, source_code: 'DD-01',
      status: 'unresolved', reason: 'Schedule and drawing conflict' }],
    lines: [{ line_id: 'l1', product: line.product, status: 'unchanged', old_amount: '200.00',
      shadow_amount: '200.00', diagnostics: [], review_signals: [] }] }
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname
    if (pathname.endsWith('/region')) {
      regions.push(url.searchParams.get('bottom'))
      return route.fulfill({ contentType: 'image/png', body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6N8AAAAASUVORK5CYII=', 'base64') })
    }
    if (pathname.endsWith('/approve')) {
      const data = request.postDataJSON()
      approvals.push(data)
      return route.fulfill({ json: { ...quote, status: 'approved' } })
    }
    const body = pathname === '/api/model-config' ? { configured: true }
      : pathname === '/api/embedding-config' ? { configured: false }
      : pathname === '/api/options' ? { products: [], categories: [] }
      : pathname === '/api/stats' ? { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
      : pathname === '/api/jobs' ? { items: [job], total: 1 }
      : pathname === '/api/jobs/1' ? job
      : pathname.endsWith('/pipeline-audit') ? audit
      : pathname.endsWith('/progress') ? { id: 1, revision: 1, status: job.status, run: null, pricing_progress: null }
      : {}
    await route.fulfill({ json: body })
  })
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  try {
    for (const [name, width, height] of [['desktop', 1440, 960], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height })
      await page.goto(process.env.UI_URL || 'http://127.0.0.1:5175')
      if (width < 600) await page.getByRole('button', { name: '会话列表', exact: true }).click()
      await page.locator('.chat-history-item').click()
      await page.getByRole('button', { name: '资料与执行记录', exact: true }).click()
      const drawer = page.locator('.el-drawer:visible')
      await drawer.getByRole('tab', { name: '执行记录', exact: true }).click()
      await drawer.getByRole('button', { name: '只读核查', exact: true }).click()
      await drawer.locator('.audit-table').waitFor()
      await drawer.locator('summary').filter({ hasText: '来源覆盖' }).click()
      await drawer.locator('summary').filter({ hasText: '多文件关联' }).click()
      assert.match(await drawer.innerText(), /DD-01 SIDE/)
      assert.match(await drawer.innerText(), /Schedule and drawing conflict/)
      await drawer.getByRole('button', { name: '查看原图区域', exact: true }).click()
      const preview = page.locator('.el-dialog:visible')
      await preview.locator('.customer-preview img').waitFor()
      await page.waitForFunction(() => document.querySelector('.customer-preview img')?.naturalWidth > 0)
      assert.equal(regions.at(-1), '0.5')
      await preview.locator('.el-dialog__headerbtn').click()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      assert(await drawer.evaluate(el => el.scrollWidth <= el.clientWidth + 1))
      await drawer.screenshot({ path: path.join(output, `visual-audit-${name}.png`), animations: 'disabled' })
      await drawer.locator('.el-drawer__close-btn').click()
      await drawer.waitFor({ state: 'hidden' })
      await page.getByRole('tab', { name: '报价结果', exact: true }).click()
      await page.getByRole('button', { name: '审核报价', exact: true }).click()
      const approval = page.locator('.el-dialog:visible')
      await approval.locator('label.el-checkbox').filter({ hasText: '已核对未读清区域' }).click()
      await approval.locator('label.el-checkbox').filter({ hasText: '已确认价格适用性' }).click()
      assert(await approval.getByRole('checkbox', { name: /已核对未读清区域/ }).isChecked())
      await approval.getByRole('textbox', { name: '审核说明', exact: true }).fill('原图及订购范围已核实')
      await approval.getByRole('button', { name: '批准', exact: true }).click()
      await approval.waitFor({ state: 'hidden' })
      assert.equal(approvals.at(-1).confirmed_source_review, true)
    }
    assert.deepEqual(errors, [])
    console.log('PASS: desktop/mobile region coverage, attachment links, original crop preview and source approval.')
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
