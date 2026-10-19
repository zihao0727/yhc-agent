const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const { mkdirSync } = require('node:fs')
const path = require('node:path')

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage()
  const errors = [], previews = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'test'))
  const file = { id: 1, filename: 'drawings.pdf', page_count: 159, byte_size: 1000, media_type: 'application/pdf' }
  const rows = ['Entrance sign', 'Floor directory'].map((product, index) => ({
    id: `line${index}`, product, source_item: `${index + 1}`, quantity: 2, width_mm: '300',
    height_mm: '120', material: 'Steel', thickness_mm: '2', language: 'all', process: 'Paint',
    evidence: [{ file_id: 1, page: 3, text: product, source: 'image' }], text_evidence: [],
    uncertainties: [], extras: [], components: [], letter_groups: [], confirmed: false,
    selected_price_id: null, selected_price_revision: null, manual_unit_price: null, notes: '',
  }))
  const sourceAgent = {
    mode: 'ai', phase: 'reading', total_pages: 159, read_pages: 8, reviewed_pages: 2,
    plan: '先核对订购清单与图纸，再回看各区域遗漏和重复视图。',
    notice: '原图和修改理由已保留。',
    page_reviews: [{ file_id: 1, page: 1, role: 'catalog', status: 'reviewed',
      reason: '封面目录用于定位图纸，图纸张数不作为订购件数。' }],
    decisions: [{ action: 'exclude', observation_ids: ['o000001'], reason: '已回看原图，目录不是订购项目。' }],
  }
  const run = { id: 5, status: 'waiting', message: '资料核对达到本次预算，已保存进度，可继续处理',
    usage: { phase: 'source_reading', extraction_progress: { extraction_route: 'source_agent',
      completed_pages: 8, reviewed_pages: 2, total_pages: 159, saved_lines: 2 } }, steps: [], questions: [] }
  const job = { id: 1, title: '多文件报价核对', customer: '', brief: '', status: 'needs_review', revision: 3,
    created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z', files: [file], messages: [],
    requirements: rows, extraction: { incomplete: true, source_agent: sourceAgent, questions: [] },
    runs: [], agent_runs: [run], quotes: [] }
  const audit = { revision: 3, requires_review: 0, files: [file], lines: [], coverage: [],
    source_links: [], source_agent: sourceAgent, metrics: null }
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname.startsWith('/api/customer-files/')) {
      previews.push(pathname)
      return route.fulfill({ contentType: 'image/png', body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6N8AAAAASUVORK5CYII=', 'base64') })
    }
    const body = pathname === '/api/model-config' ? { configured: true }
      : pathname === '/api/embedding-config' ? { configured: false }
      : pathname === '/api/options' ? { products: [], categories: [] }
      : pathname === '/api/stats' ? { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
      : pathname === '/api/jobs' ? { items: [job], total: 1 }
      : pathname === '/api/jobs/1' ? job
      : pathname.endsWith('/pipeline-audit') ? audit
      : pathname.endsWith('/progress') ? { id: 1, revision: 3, status: job.status, run, pricing_progress: null }
      : {}
    return route.fulfill({ json: body })
  })
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  try {
    for (const [name, width, height] of [['desktop', 1440, 960], ['mobile', 390, 844]]) {
      job.extraction.incomplete = true
      sourceAgent.phase = 'reading'
      sourceAgent.unread_pages = []
      sourceAgent.unreviewed_pages = []
      job.pricing_progress = null
      await page.setViewportSize({ width, height })
      await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
      if (width < 600) await page.getByRole('button', { name: '会话列表', exact: true }).click()
      await page.locator('.chat-history-item').click()
      await page.getByRole('tab', { name: '报价结果', exact: true }).click()
      const source = page.getByRole('status', { name: '资料核对状态', exact: true })
      await source.waitFor()
      assert.match(await source.innerText(), /资料核对已暂停/)
      assert.match(await source.innerText(), /8\s*\/\s*159/)
      assert.match(await source.innerText(), /已复核 2 页/)
      assert.match(await source.innerText(), /本次预算/)
      assert(await source.getByRole('button', { name: '继续核对资料', exact: true }).isEnabled())
      const results = page.locator('.results-workspace')
      assert.doesNotMatch(await results.innerText(), /待计价/)
      assert.match(await results.innerText(), /待核对/)
      assert.equal(await results.locator('.result-pending-action:visible').count(), 0)
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      await page.screenshot({ path: path.join(output, `source-agent-${name}.png`), animations: 'disabled' })
      await page.getByRole('button', { name: '资料与执行记录', exact: true }).click()
      const drawer = page.locator('.el-drawer:visible')
      await drawer.getByRole('tab', { name: '执行记录', exact: true }).click()
      await drawer.getByRole('button', { name: '只读核查', exact: true }).click()
      await drawer.getByRole('region', { name: 'AI资料核对记录', exact: true }).waitFor()
      await drawer.locator('summary').filter({ hasText: '页面判断' }).click()
      await drawer.locator('summary').filter({ hasText: '项目调整记录' }).click()
      assert.match(await drawer.innerText(), /目录不是订购项目/)
      assert.match(await drawer.innerText(), /张数不作为订购件数/)
      await drawer.getByRole('button', { name: '查看原始页面', exact: true }).click()
      const preview = page.locator('.el-dialog:visible')
      await page.waitForFunction(() => document.querySelector('.customer-preview img')?.naturalWidth > 0)
      assert.equal(previews.at(-1), '/api/customer-files/1/pages/1')
      await preview.locator('.el-dialog__headerbtn').click()
      assert(await drawer.evaluate(el => el.scrollWidth <= el.clientWidth + 1))
      await drawer.screenshot({ path: path.join(output, `source-agent-audit-${name}.png`), animations: 'disabled' })
      await drawer.locator('.el-drawer__close-btn').click()
      await drawer.waitFor({ state: 'hidden' })
      job.extraction.incomplete = false
      sourceAgent.phase = 'complete'
      sourceAgent.unread_pages = [{ file_id: 1, page: 159 }]
      sourceAgent.unreviewed_pages = [{ file_id: 1, page: 159 }]
      job.pricing_progress = { revision: 3, run_id: 5, priced_count: 1, line_count: 2,
        known_subtotal: '40.00', complete: false, lines: [{
          line_id: rows[0].id, product: rows[0].product, amount: '40.00', unit_price: '20.00',
          blockers: [], warnings: [], resolution: 'priced',
        }] }
      await page.reload()
      if (width < 600) await page.getByRole('button', { name: '会话列表', exact: true }).click()
      await page.locator('.chat-history-item').click()
      await page.getByRole('tab', { name: '报价结果', exact: true }).click()
      const remaining = page.getByRole('status', { name: '剩余资料核对', exact: true })
      await remaining.waitFor()
      assert.match(await remaining.innerText(), /尚未读完 1 页/)
      assert.match(await page.locator('.results-workspace').innerText(), /40\.00/)
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      await page.screenshot({ path: path.join(output, `source-agent-partial-${name}.png`), animations: 'disabled' })
    }
    assert.deepEqual(errors, [])
    console.log('PASS: desktop/mobile source pause, partial quote scope, review progress, AI decisions and original preview.')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
