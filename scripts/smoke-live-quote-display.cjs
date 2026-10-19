const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  const base = { text_content: '', material: '铝板', thickness_mm: null, width_mm: null, height_mm: null,
    billing_length_mm: null, quantity: 2, language: 'unknown', process: '', notes: '', evidence: [],
    uncertainties: [], confirmed: false, selected_price_id: null, selected_price_revision: null,
    price_review_note: '', extras: [], text_evidence: [], components: [], letter_groups: [], estimate: null }
  const requirements = [
    { ...base, id: 'priced', product: '已计价标识', source_item: '1',
      evidence: [{ file_id: 1, page: 1, cell: 'D2', source: 'text', text: '规格尺寸:125*212mm' },
        { file_id: 1, page: 1, cell: 'E2', source: 'text', text: '套' }] },
    { ...base, id: 'panel', product: '制作面板', source_item: '2', width_mm: '800', height_mm: '600' },
    { ...base, id: 'zero', product: '免费样品', source_item: '3', quantity: 1 },
  ]
  const input = (field, value, basis, missing) => ({
    key: `component:face:${field}`, line_id: 'panel', target: 'component', target_id: 'face',
    target_name: '发光面板', field, value, basis, missing,
  })
  const issue = { line_id: 'panel', product: '制作面板', cause: 'recognition_review', question: '',
    reason: '连续操作未推进计价；计量对象缺少参数：billable_area_m2', missing_fields: ['/parameters/-'],
    inputs: [input('quantity_per_item', null, 'original', true),
      input('width_mm', '100', 'source', false), input('height_mm', '300', 'assumption', false)] }
  const job = { id: 900021, revision: 3, title: '实时计价与补充', customer: '', brief: '', status: 'processing',
    created_at: '2026-10-13T02:00:00Z', updated_at: '2026-10-13T02:00:00Z', line_count: 3, file_count: 0,
    pending_count: 1, requirements, quotes: [], files: [], messages: [], runs: [], extraction: {},
    agent_runs: [{ id: 4, status: 'processing', message: '报价中', created_at: '2026-10-13T02:00:00Z',
      steps: [], usage: { prepared: true, phase: 'running', deferred_requirements: [issue] } }],
    pricing_progress: { revision: 3, run_id: 4, priced_count: 2, line_count: 3, known_subtotal: '100.00',
      complete: false, saved_at: 1, lines: [
        { line_id: 'priced', product: '已计价标识', amount: '100.00', unit_price: '50.00', blockers: [], formula: '', price: null,
          effective_requirement: { components: [{ object_id: 'sign', name: '标识牌', width_mm: '125', height_mm: '212' }] } },
        { line_id: 'panel', product: '制作面板', amount: null, unit_price: null, blockers: [], formula: '', price: null },
        { line_id: 'zero', product: '免费样品', amount: '0.00', unit_price: '0.00', blockers: [], formula: '', price: null },
      ] } }
  let writes = 0
  const errors = []
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'mock-only'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    let body
    if (request.method() !== 'GET') writes++
    if (pathname === '/api/session') body = { authenticated: true }
    else if (pathname === '/api/stats') body = { prices: 0, products: 0, rules: 0, documents: 0 }
    else if (pathname === '/api/options') body = { categories: [], products: [] }
    else if (pathname === '/api/upload-config') body = {}
    else if (pathname.endsWith('model-config') || pathname.endsWith('embedding-config')) body = { configured: true, model: 'mock-only' }
    else if (pathname === '/api/jobs') body = { items: [job], total: 1 }
    else if (pathname === `/api/jobs/${job.id}`) body = job
    else if (pathname === `/api/jobs/${job.id}/progress`) body = {
      id: job.id, revision: job.revision, status: job.status, server_time: 1,
      pricing_progress: job.pricing_progress, run: { ...job.agent_runs[0], offset: 0 },
    }
    else if (pathname === `/api/jobs/${job.id}/requirements`) {
      const data = request.postDataJSON()
      assert.equal(job.status, 'needs_review')
      assert.equal(data.revision, job.revision)
      assert.equal(data.lines[1].components[0].object_id, 'face')
      assert.equal(data.lines[1].components[0].quantity_per_item, 3)
      assert.equal(data.lines[1].components[0].width_mm, null, 'Untouched prefills are not promoted to new user facts')
      assert.equal(data.lines[1].width_mm, '800', 'A part quantity must not alter whole-item dimensions')
      assert.deepEqual(data.lines[0], requirements[0])
      job.requirements = data.lines; job.revision++
      job.pricing_progress.revision = job.revision
      body = job
    } else throw new Error(`Unexpected request: ${request.method()} ${pathname}`)
    await route.fulfill({ json: body })
  })
  async function openJob() {
    if (!(await page.locator('.chat-history').isVisible())) await page.getByRole('button', { name: '会话列表', exact: true }).click()
    await page.locator(`.chat-history-item[data-job-id="${job.id}"]`).click()
  }
  try {
    await page.goto('http://127.0.0.1:5173')
    await openJob()
    await page.getByRole('tab', { name: '报价结果', exact: true }).click()
    const row = page.locator('.result-table tbody tr').filter({ hasText: '已计价标识' })
    assert.match(await row.innerText(), /50\.00.*100\.00/s)
    assert.match(await row.innerText(), /标识牌：125 × 212 mm/)
    assert.match(await row.innerText(), /2\s*套/)
    assert.match(await page.locator('.result-table tbody tr').filter({ hasText: '免费样品' }).innerText(), /0\.00/)
    await page.getByRole('button', { name: /^已计价 2$/, exact: true }).click()
    assert.equal(await page.locator('.result-table tbody tr').count(), 2)
    await page.screenshot({ path: path.join(output, 'live-prices-desktop.png'), animations: 'disabled' })
    await row.getByRole('button', { name: '已计价标识', exact: true }).click()
    await page.getByRole('tab', { name: '来源与说明', exact: true }).click()
    assert.match(await page.locator('.result-source-description').innerText(), /125\*212mm/)
    await page.locator('.el-drawer__close-btn:visible').click()
    await page.getByRole('dialog', { name: '已计价标识', exact: true }).waitFor({ state: 'hidden' })
    await page.getByRole('tab', { name: /^待补充/ }).click()
    const pending = page.locator('.supplement-row').filter({ hasText: '制作面板' }).first()
    assert.doesNotMatch(await pending.locator('.supplement-heading').innerText(), /billable_area_m2|parameters|未推进/)
    assert.match(await pending.locator('.supplement-heading').innerText(), /发光面板.*每套该部件数量/)
    await pending.locator('.supplement-heading').click()
    const count = pending.getByRole('textbox', { name: '发光面板：每套该部件数量', exact: true })
    assert(await count.isEnabled(), 'Local drafts remain editable during pricing')
    assert.equal(await pending.getByRole('textbox', { name: '发光面板：部件宽度 (mm)', exact: true }).inputValue(), '100')
    const assumedHeight = pending.getByRole('textbox', { name: '发光面板：部件高度 (mm)', exact: true })
    assert.equal(await assumedHeight.inputValue(), '', 'An assumption is not silently confirmed')
    assert.match(await assumedHeight.getAttribute('placeholder'), /300.*待确认/)
    assert.equal(await pending.locator('.draft-mark').count(), 0, 'Known prefills are not user edits')
    await pending.locator('.supplement-other-fields > summary').click()
    await pending.locator('.el-select__wrapper').click()
    await page.getByRole('option', { name: '单件宽度 (mm)', exact: true }).click()
    await page.keyboard.press('Escape')
    assert.equal(await pending.getByRole('textbox', { name: '制作面板：单件宽度 (mm)', exact: true }).inputValue(), '800')
    await count.fill('3')
    assert(await page.getByRole('button', { name: '保存补充', exact: true }).isDisabled())
    assert.equal(writes, 0)
    await page.reload()
    await openJob()
    await pending.locator('.supplement-heading').click()
    assert.equal(await count.inputValue(), '3', 'The draft survives refresh while the worker runs')
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height })
      await page.mouse.move(1, 1)
      await page.locator('.el-popper.is-dark.el-tooltip:visible').first().waitFor({ state: 'hidden' })
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      await page.screenshot({ path: path.join(output, `live-supplement-${name}.png`), animations: 'disabled' })
    }
    job.status = 'needs_review'; job.agent_runs[0].status = 'waiting'
    await page.getByRole('button', { name: '保存补充', exact: true }).waitFor({ state: 'visible' })
    await page.waitForFunction(() => ![...document.querySelectorAll('button')].find(button => button.textContent.trim() === '保存补充')?.disabled)
    await page.getByRole('button', { name: '保存补充', exact: true }).click()
    await page.getByText('项目补充已保存', { exact: true }).waitFor()
    assert.equal(writes, 1)
    assert.deepEqual(errors, [])
    console.log('PASS: live prices, zero prices, adopted part specifications, original units, readable scoped missing inputs, source prefills, explicit assumptions, drafts during pricing/reload, precise scoped submission, desktop/mobile.')
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
