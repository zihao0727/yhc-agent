const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  const samples = [
    ['film', '玻璃贴膜', '500', 'm', '米', '10010.00'],
    ['paint', '墙面涂装', '819.17', 'm2', '平方米', '16393.40'],
    ['base', '混凝土基础', '2.5', 'm3', '立方米', '60.00'],
    ['metal', '结构钢材', '250', 'kg', '千克', '5010.00'],
    ['labor', '现场加工', '1.5', 'hour', '小时', '40.00'],
    ['location', '功能区标识', '3', null, '处', '60.00'],
  ]
  const requirements = samples.map(([id, product, quantity, unit], index) => ({
    id, product, source_item: String(index + 1), text_content: '', material: '', thickness_mm: null,
    width_mm: null, height_mm: null, billing_length_mm: null,
    quantity: unit ? null : 3, quantity_unit: unit ? '' : '处',
    source_measurements: unit ? [{ value: quantity, unit, evidence: [] }] : [],
    language: 'unknown', process: '', notes: '', evidence: [], uncertainties: [], confirmed: false,
    selected_price_id: null, selected_price_revision: null, price_review_note: '', extras: [],
    text_evidence: [], components: [], letter_groups: [], estimate: null,
  }))
  const lines = samples.map(([id, product, quantity, unit, , amount]) => ({
    line_id: id, product, amount, unit_price: '20.00', fixed_charges: unit ? '10.00' : '0.00',
    ...(unit ? { billing_quantity: quantity, billing_unit: unit } : {}),
    blockers: [], warnings: [], formula: '', price: null, estimated: true,
    effective_requirement: requirements.find(row => row.id === id),
  }))
  const job = {
    id: 900015, revision: 1, title: '通用工程量报价验收', customer: '', brief: '', status: 'needs_review',
    requirements, quotes: [], files: [], messages: [], extraction: {}, line_count: lines.length,
    agent_runs: [{ id: 1, status: 'waiting', steps: [], usage: {} }],
    pricing_progress: { revision: 1, run_id: 1, complete: true, priced_count: lines.length,
      line_count: lines.length, known_subtotal: lines.reduce((sum, row) => sum + Number(row.amount), 0).toFixed(2), lines },
  }
  const errors = []
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'mock-only'))
  await page.route('**/api/**', async route => {
    assert.equal(route.request().method(), 'GET', 'Acceptance must not mutate real or mock orders')
    const pathname = new URL(route.request().url()).pathname
    let body
    if (pathname === '/api/session') body = { authenticated: true }
    else if (pathname === '/api/stats') body = { prices: 0, products: 0, rules: 0, documents: 0 }
    else if (pathname === '/api/options') body = { categories: [], products: [] }
    else if (pathname === '/api/upload-config') body = {}
    else if (pathname.endsWith('model-config') || pathname.endsWith('embedding-config')) body = { configured: true }
    else if (pathname === '/api/jobs') body = { items: [job], total: 1 }
    else if (pathname === `/api/jobs/${job.id}`) body = job
    else if (pathname === `/api/jobs/${job.id}/progress`) body = {
      id: job.id, revision: 1, status: job.status, server_time: 1,
      pricing_progress: job.pricing_progress, run: { ...job.agent_runs[0], offset: 0 },
    }
    else throw new Error(`Unexpected API request: ${pathname}`)
    await route.fulfill({ json: body })
  })
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
    await page.locator(`.chat-history-item[data-job-id="${job.id}"]`).click()
    await page.getByRole('tab', { name: '报价结果', exact: true }).click()
    for (const [, product, quantity, , label] of samples) {
      const row = page.locator('.result-table tbody tr').filter({ hasText: product })
      assert.match(await row.innerText(), new RegExp(`${quantity.replace('.', '\\.')}\\s*${label}`))
      assert.doesNotMatch(await row.innerText(), /数量待确认|待计价/)
    }
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: path.join(output, 'pricing-foundations-desktop.png'), animations: 'disabled' })
    const paint = page.locator('.result-table tbody tr').filter({ hasText: '墙面涂装' })
    await paint.getByRole('button', { name: '墙面涂装', exact: true }).click()
    const drawer = page.getByRole('dialog', { name: '墙面涂装', exact: true })
    assert.match(await drawer.innerText(), /819\.17\s*平方米/)
    assert.match(await drawer.innerText(), /20\.00\s*\/\s*平方米/)
    await drawer.locator('.el-drawer__close-btn').click()
    await drawer.waitFor({ state: 'hidden' })
    for (const [width, height] of [[390, 844], [320, 740]]) {
      await page.setViewportSize({ width, height })
      for (const [, product, quantity, , label] of samples) {
        const row = page.locator('.result-mobile-row').filter({ hasText: product })
        assert.match(await row.innerText(), new RegExp(`${quantity.replace('.', '\\.')}\\s*${label}`))
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      await page.screenshot({ path: path.join(output, `pricing-foundations-mobile-${width}.png`), animations: 'disabled' })
    }
    assert.deepEqual(errors, [])
    console.log('PASS: meters, area, volume, weight, hours and location counts across desktop, drawer and mobile; GET only.')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
