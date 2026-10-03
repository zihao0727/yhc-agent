const { chromium } = require('playwright')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  const at = new Date().toISOString()
  const base = {
    text_content: '', material: '', thickness_mm: null, width_mm: null, height_mm: null,
    billing_length_mm: null, quantity: 1, language: 'unknown', process: '', notes: '',
    evidence: [], uncertainties: [], confirmed: false, selected_price_id: null,
    selected_price_revision: null, price_review_note: '', extras: [],
  }
  const requirements = [
    { ...base, id: 'door', product: '门牌编号', source_item: '12' },
    { ...base, id: 'arrow', product: '过道口方向指引', source_item: '13' },
  ]
  const questions = [
    '请提供门牌编号各字组的单字计价长度（或单字宽度）：1）数字字的单字宽×高（如80×2mm）与每套字符数；2）点点/连接符的单字尺寸；3）每套成品包含的编号数量及字符数（如606—618是否为1套内共6位数字）。据此才能按字类规则逐字计价。',
    '请提供过道口方向指引的逐字计价尺寸：1）箭头图形的宽度（或单字计价长度，mm）；2）数字的单字宽度（或单字计价长度，mm）；3）本套是否确实只含箭头1个+数字1个，有无其他字符。据此按字类规则逐字计价，并与门牌编号一并确认单字宽度口径。',
  ]
  const quote = {
    id: 1, version: 1, job_revision: 1, status: 'blocked', outdated: false, terms: '',
    created_at: at, approval_note: '',
    payload: {
      lines: requirements.map((line, index) => ({
        line_id: line.id, product: line.product, requirement: line, amount: null,
        unit_price: null, formula: '', price: null, blockers: [questions[index]], warnings: [],
      })),
      complete: false, total: null, known_subtotal: '0.00', currency: 'CNY',
      confirmation_items: requirements.map((line, index) => ({
        owner: '需求方', question: questions[index], lines: [{ line_id: line.id, product: line.product }],
      })),
      review_questions: requirements.map((line, index) => `${line.product}：${questions[index]}`),
    },
  }
  const job = {
    id: 900002, title: '浙江世界贸易中心标识制作清单', customer: '', brief: '',
    status: 'needs_review', revision: 1, created_at: at, updated_at: at,
    line_count: 2, pending_count: 2, file_count: 0, requirements, extraction: {},
    files: [], messages: [], runs: [], quotes: [quote], agent_runs: [],
  }
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const errors = []
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'mock-test-only'))
    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname
      assert.equal(route.request().method(), 'GET', 'UI checks must not modify business data')
      let body
      if (pathname === '/api/session') body = { authenticated: true }
      else if (pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
      else if (pathname === '/api/options') body = { categories: [], products: [] }
      else if (pathname.endsWith('model-config') || pathname.endsWith('embedding-config')) {
        body = { configured: true, model: 'mock-only', environment_managed: false }
      } else if (pathname === '/api/jobs') body = { items: [job], total: 1 }
      else if (pathname === `/api/jobs/${job.id}`) body = job
      else throw new Error(`Unexpected API request: ${pathname}`)
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto(process.env.CONFIRMATIONS_UI_URL || 'http://127.0.0.1:5175')
    await page.locator('.chat-history-item').first().click()
    const table = page.locator('.chat-confirmation-details .confirmation-table')
    await table.waitFor()
    assert.equal(await table.locator('tbody tr').count(), 2, 'Duplicate legacy questions must not create duplicate rows')
    assert.deepEqual(await table.locator('thead th').allTextContents(), ['序号', '项目', '问题与处理事项', '处理方'])
    assert(await table.getByText('#12', { exact: true }).isVisible())
    assert(await table.getByText('#13', { exact: true }).isVisible())
    assert.equal(await table.locator('.confirmation-question').count(), 8)
    for (const [name, viewport] of [
      ['desktop', { width: 1440, height: 1000 }],
      ['tablet', { width: 768, height: 1024 }],
      ['mobile', { width: 390, height: 844 }],
      ['narrow-mobile', { width: 320, height: 740 }],
    ]) {
      await page.setViewportSize(viewport)
      await table.scrollIntoViewIfNeeded()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: page overflow`)
      assert(await table.evaluate(element => element.scrollWidth <= element.clientWidth + 1), `${name}: table overflow`)
      if (viewport.width < 600) {
        assert(await table.locator('.confirmation-mobile-owner').first().isVisible())
        assert.equal(await table.locator('.confirmation-owner').first().isVisible(), false)
      }
      await table.screenshot({ path: path.join(output, `confirmations-${name}.png`), animations: 'disabled' })
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.getByRole('button', { name: '补充后继续', exact: true }).click()
    const dialogTable = page.locator('.el-dialog .confirmation-table')
    await dialogTable.waitFor()
    assert.equal(await dialogTable.locator('tbody tr').count(), 2)
    await page.locator('.el-dialog').screenshot({ path: path.join(output, 'confirmations-review-dialog.png'), animations: 'disabled' })
    await page.getByRole('button', { name: '稍后补充', exact: true }).click()
    await page.getByRole('button', { name: '查看与审核报价', exact: true }).click()
    await page.locator('.el-dialog, .el-drawer').filter({ has: page.locator('.confirmation-table') }).first().waitFor()
    assert.deepEqual(errors, [])
    console.log('Confirmation table UI passed: deduplication, original item numbers, numbered points, desktop/tablet/mobile, review dialog and quote view')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
