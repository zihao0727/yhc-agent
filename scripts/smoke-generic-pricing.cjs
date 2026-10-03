const { chromium } = require('playwright')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  const at = new Date().toISOString()
  const version = '2026-09-23.v1'
  const evidence = { file_id: 1, page: 1, sheet: '制作清单', cell: 'C2', text: '面板500×400mm', source: 'text' }
  const base = {
    text_content: '', material: '', thickness_mm: null, depth_mm: null, width_mm: '2000', height_mm: '1000',
    billing_length_mm: null, quantity: 2, language: 'all', process: '', notes: '',
    evidence: [evidence], uncertainties: [], confirmed: true, selected_price_id: null,
    selected_price_revision: null, price_review_note: '', extras: [], components: [],
    business_rules_version: version, pricing_family: 'letter', difficulty: 'C', difficulty_note: '',
    type_note: '组合制作', letter_groups: [], faces: 'single', lighting: 'no', installation_scene: 'other',
    power_count: null, power_mode: 'manual', power_adjustment: 0, length_rounding: 'exact',
  }
  const cost = {
    label: '面板制作', rate: '100', rate_unit: 'm2', expected_amount: '20',
    quantity_formula: '=billable_area_m2', basis: 'per_piece', kind: 'material',
    source: 'agent_estimate', reference_id: null, reference_revision: null,
    reason: '按面板实际面积暂估，单价待管理员核实', measurement_note: '',
    target: 'component', target_index: 0, included_kinds: ['process'], adjusts_cost_index: null,
    rate_basis_amount: null, rate_basis_unit: null, outsourcing_type: 'other', minimum_area_scope: 'unconfirmed',
  }
  const assembled = {
    ...base, id: 'assembled', product: '组合标牌', source_item: '1',
    source_measurements: [{ value: '1.5', unit: 'm', evidence: [
      { ...evidence, cell: 'F2', text: '1.5' }, { ...evidence, cell: 'G2', text: '米' },
    ] }],
    quantity_evidence: [{ ...evidence, cell: 'F2', quantity: null, raw_quantity: '1.5米',
      validation_status: 'rejected', validation_note: '数量栏为长度工程量，不能认作成品件数' }],
    letter_groups: [
      { label: '数字', count: 2, width_mm: '80', height_mm: '80', billing_length_mm: '80', evidence: [] },
      { label: '连接符', count: 1, width_mm: '15', height_mm: '15', billing_length_mm: '15', evidence: [] },
    ],
    dimension_evidence: [{ ...evidence, field: 'height_mm', value: '400', unit: 'mm',
      target: 'component', target_index: 0, role: 'overall' }],
    components: [
      { name: '面板', material: '铝', thickness_mm: '3', width_mm: '500', height_mm: '400',
        area_m2: null, quantity_per_item: 1, geometry: 'flat_rectangle', processes: [],
        labor_hours: null, billing_length_mm: null, evidence: [evidence], uncertainties: [] },
      { name: '边框', material: '不锈钢', thickness_mm: null, width_mm: null, height_mm: null,
        area_m2: null, quantity_per_item: 1, geometry: 'unknown', processes: [],
        labor_hours: null, billing_length_mm: '1800', evidence: [], uncertainties: [] },
    ],
    estimate: { contract_version: 2, pricing_strategy: 'composite', scope: '制作费待审核，不含安装、运输、税费', assumptions: [], costs: [cost] },
  }
  const causes = ['customer_input', 'catalog_gap', 'execution_error']
  const statuses = ['customer_confirmation', 'administrator_pricing', 'execution_error']
  const names = ['缺少客户尺寸', '缺少特殊工艺单价', '公式方案受阻']
  const pending = causes.map((cause, index) => ({
    ...base, id: cause, product: names[index], source_item: String(index + 2), confirmed: false,
    estimate: null, dimension_evidence: [],
  }))
  const requirements = [assembled, ...pending]
  const quoted = {
    line_id: assembled.id, product: assembled.product, requirement: assembled, effective_requirement: assembled,
    estimated: true, amount: '40.00', unit_price: '20.00', formula: '20 × 2 = 40',
    fixed_charges: '0', price: null, blockers: [], warnings: ['暂估价格须人工审核'],
    resolution: 'priced_review', estimate_review: [], estimate_scope: assembled.estimate.scope,
    pricing_strategy: { key: 'composite', label: '部件组合', version: 'measurement.v2' },
    estimate_costs: [{ ...cost, amount: '20.00', measure: '.2', resolved_formula: '100 × 0.2 = 20', reference: null }],
    review_signals: [{ code: 'unverified_rate', cost_index: 0, message: '单价尚未确认，须管理员核价' }],
  }
  const quote = {
    id: 1, version: 1, job_revision: 1, status: 'blocked', outdated: false, terms: assembled.estimate.scope,
    created_at: at, approval_note: '',
    payload: {
      lines: [quoted, ...pending.map((line, index) => ({
        line_id: line.id, product: line.product, requirement: line, amount: null,
        unit_price: null, formula: '', price: null, blockers: [names[index]], warnings: [],
        resolution: statuses[index], deferred_issue: { cause: causes[index], reason: names[index] },
      }))],
      complete: false, total: null, known_subtotal: '40.00', currency: 'CNY', has_estimates: true,
      priced_count: 1, pending_count: 3,
      resolution_counts: { priced_review: 1, customer_confirmation: 1, administrator_pricing: 1, execution_error: 1 },
      confirmation_items: pending.map((line, index) => ({
        owner: index === 0 ? '需求方' : '报价管理员', cause: causes[index],
        question: names[index], lines: [{ line_id: line.id, product: line.product }],
      })),
    },
  }
  let job = {
    id: 900003, title: '跨类型通用报价验收', customer: '', brief: '',
    status: 'needs_review', business_rules_version: version, revision: 1, created_at: at, updated_at: at,
    line_count: 4, pending_count: 3, file_count: 0, requirements, extraction: {},
    files: [], messages: [], runs: [], quotes: [quote], agent_runs: [],
  }
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const errors = []
  const writes = []
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'mock-test-only'))
    await page.route('**/api/**', async route => {
      const request = route.request()
      const pathname = new URL(request.url()).pathname
      let body
      if (request.method() === 'PUT' && pathname === `/api/jobs/${job.id}/requirements`) {
        const payload = request.postDataJSON()
        writes.push(payload)
        job = { ...job, requirements: payload.lines, revision: job.revision + 1 }
        body = job
      } else {
        assert.equal(request.method(), 'GET', 'All mutations must remain mocked')
        if (pathname === '/api/session') body = { authenticated: true }
        else if (pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
        else if (pathname === '/api/options') body = { categories: [], products: [] }
        else if (pathname.endsWith('model-config') || pathname.endsWith('embedding-config')) body = { configured: true, model: 'mock-only', environment_managed: false }
        else if (pathname === '/api/jobs') body = { items: [job], total: 1 }
        else if (pathname === `/api/jobs/${job.id}`) body = job
        else throw new Error(`Unexpected request: ${pathname}`)
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto(process.env.GENERIC_PRICING_UI_URL || 'http://127.0.0.1:5176')
    await page.locator('.chat-history-item').first().click()
    const counts = page.locator('.chat-resolution-counts')
    await counts.waitFor()
    assert.match(await counts.textContent(), /待客户确认 1 项/)
    assert.match(await counts.textContent(), /待管理员核价 1 项/)
    assert.match(await counts.textContent(), /系统执行异常 1 项/)
    await page.screenshot({ path: path.join(output, 'generic-pricing-status-desktop.png'), animations: 'disabled' })
    await page.getByRole('button', { name: '明细与报价', exact: true }).click()
    await page.getByRole('tab', { name: '需求明细 (4)' }).click()
    await page.locator('.product-link').filter({ hasText: '组合标牌' }).click()
    const dialog = page.getByRole('dialog', { name: '需求明细', exact: true })
    await dialog.waitFor()
    const sources = dialog.locator('.quantity-sources')
    assert.match(await sources.textContent(), /原始工程量：1.5 米/)
    await sources.getByText('数量依据待核实', { exact: true }).click()
    assert(await sources.getByText('数量栏为长度工程量，不能认作成品件数', { exact: true }).isVisible())
    assert.match(await sources.textContent(), /制作清单 F2/)
    assert(await dialog.getByRole('checkbox').isChecked(), 'Opening editor must preserve prior human confirmation')
    assert(await dialog.getByRole('textbox', { name: '部件1名称', exact: true }).isVisible())
    await dialog.getByRole('button', { name: '添加补全参数', exact: true }).click()
    async function select(label, option) {
      const select = dialog.getByRole('combobox', { name: label, exact: true })
      await select.locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " el-select ")][1]').click()
      const listId = await select.getAttribute('aria-controls')
      await page.locator(`[id="${listId}"]`).getByRole('option', { name: option, exact: true }).click()
    }
    await select('参数1对象', '部件 1 · 面板')
    await select('参数1字段', '高(mm)')
    await select('参数1采用方式', '纠正识别值（待审核）')
    const previous = dialog.locator('.el-form-item').filter({ hasText: '识别原值' }).getByRole('textbox')
    assert.equal(await previous.inputValue(), '400')
    await select('参数1原文证据', '制作清单 C2 · 面板500×400mm')
    await page.keyboard.press('Escape')
    await select('参数1对象', '部件 2 · 边框')
    assert.equal(await previous.inputValue(), 'None', 'Changing correction target refreshes its original value')
    await select('参数1对象', '部件 1 · 面板')
    await dialog.getByRole('textbox', { name: '暂定高(mm)', exact: true }).fill('400')
    await dialog.locator('.el-form-item').filter({ hasText: '补全依据' }).getByRole('textbox').fill('按该部件单元格原文纠正识别高度，待审核')
    await select('费用1对象', '部件 2 · 边框')
    await select('费用1对象', '部件 1 · 面板')
    await dialog.getByRole('button', { name: '删除部件1', exact: true }).click()
    await page.getByText('该对象仍被费用或补全参数引用，请先调整引用', { exact: true }).waitFor()
    assert.equal(await dialog.getByRole('textbox', { name: '部件1名称', exact: true }).inputValue(), '面板')
    const editor = dialog.locator('.estimate-editor')
    for (const [name, viewport] of [
      ['desktop', { width: 1440, height: 1000 }],
      ['tablet', { width: 768, height: 1024 }],
      ['mobile', { width: 390, height: 844 }],
      ['narrow-mobile', { width: 320, height: 740 }],
    ]) {
      await page.setViewportSize(viewport)
      await editor.locator('h3').first().scrollIntoViewIfNeeded()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: page overflow`)
      assert(await editor.evaluate(e => e.scrollWidth <= e.clientWidth + 1), `${name}: editor overflow`)
      await sources.scrollIntoViewIfNeeded()
      assert(await sources.evaluate(e => e.scrollWidth <= e.clientWidth + 1), `${name}: quantity source overflow`)
      await page.screenshot({ path: path.join(output, `quantity-sources-${name}.png`), animations: 'disabled' })
      await editor.locator('h3').first().scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(output, `generic-pricing-editor-${name}.png`), animations: 'disabled' })
      await editor.locator('.cost-heading').first().scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(output, `generic-pricing-cost-${name}.png`), animations: 'disabled' })
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await dialog.locator('.el-checkbox').click()
    assert.equal(await dialog.getByRole('checkbox').isChecked(), false)
    await dialog.locator('.el-form-item').filter({ hasText: '修改原因' }).getByRole('textbox').fill('通用对象级方案测试，不写业务数据库')
    await dialog.getByRole('button', { name: '保存明细', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert.equal(writes.length, 1)
    const plan = writes[0].lines[0].estimate
    assert.equal(plan.contract_version, 2)
    assert.equal(plan.assumptions[0].target, 'component')
    assert.equal(plan.assumptions[0].previous_value, '400')
    assert.equal(plan.assumptions[0].evidence[0].cell, 'C2')
    assert.deepEqual(plan.costs[0].included_kinds, ['process'])
    assert.equal(plan.costs[0].target_index, 0)
    assert.equal(writes[0].lines[0].source_measurements[0].value, '1.5')
    assert.equal(writes[0].lines[0].quantity_evidence[0].raw_quantity, '1.5米')
    assert.deepEqual(errors, [])
    console.log('Generic pricing UI passed: distinct statuses, scoped targets, corrections, source selection, safe deletion, mocked save, desktop/tablet/mobile')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
