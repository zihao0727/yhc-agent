const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const errors = []
  const rows = Array.from({ length: 120 }, (_, index) => ({
    id: `line${index}`, source_item: String(index + 1), product: `Sign ${index + 1}`,
    text_content: '', material: 'Aluminum', thickness_mm: '3', width_mm: '300', height_mm: '200',
    quantity: 2, process: '', confirmed: false, uncertainties: [], text_evidence: [],
    evidence: [{ file_id: 1, page: index + 1, text: 'Original drawing', region: [0, 0, 1, 0.5] }],
    language: 'unknown', notes: '', selected_price_id: null, difficulty: 'unknown', type_note: '',
    extras: [], letter_groups: [], components: [],
  }))
  const run = {
    id: 1, status: 'processing', phase: 'extracting', created_at: '2026-10-12T01:00:00Z', steps: [],
    message: '正在整理资料：已保存 120/240 页、120 项明细。',
    usage: { durable: true, prepared: false, extraction_only: true, phase: 'extracting',
      extraction_progress: { completed_pages: 120, total_pages: 240, saved_lines: 120,
        page: 121, region_index: 2, total_regions: 4 } },
  }
  const job = {
    id: 101, title: 'Large PDF 240 pages', customer: 'UI verification', brief: '',
    status: 'processing', revision: 1, requirements: rows, line_count: 120, pending_count: 120, file_count: 1,
    created_at: '2026-10-12T01:00:00Z', updated_at: '2026-10-12T01:10:00Z',
    business_rules_version: '2026-09-23.v1', messages: [], extraction: { incomplete: true },
    files: [{ id: 1, filename: 'drawing-240-pages.pdf', media_type: 'application/pdf',
      byte_size: 125829120, page_count: 240 }], quotes: [], runs: [], agent_runs: [run],
    pricing_progress: { priced_count: 0, line_count: 120, known_subtotal: '0', complete: false },
  }
  try {
    for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
      const page = await browser.newPage({ viewport })
      page.on('pageerror', error => errors.push(error.message))
      await page.addInitScript(() => {
        sessionStorage.setItem('firefly-token', 'ui-test-mocked')
        localStorage.setItem('firefly-theme', 'light')
      })
      await page.route('**/api/**', async route => {
        const url = new URL(route.request().url())
        let body = {}
        if (url.pathname === '/api/options') body = { categories: [], products: [] }
        else if (url.pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
        else if (url.pathname === '/api/model-config') body = { configured: true, model: 'test' }
        else if (url.pathname === '/api/upload-config') body = { max_files: 6, max_file_bytes: 20971520,
          max_pdf_bytes: 209715200, max_job_bytes: 524288000, max_pages: 500, max_lines: 10000 }
        else if (url.pathname === '/api/jobs') body = { items: [job], total: 1 }
        else if (url.pathname === '/api/jobs/101/progress') body = { id: 101, revision: 1,
          status: 'processing', pricing_progress: job.pricing_progress, run: { ...run, offset: 0 }, server_time: Date.now() / 1000 }
        else if (url.pathname === '/api/jobs/101') body = job
        else if (url.pathname.endsWith('/pipeline-audit')) body = { lines: [], coverage: [], files: [], metrics: null, requires_review: 0 }
        else if (url.pathname.endsWith('/requirements-page')) body = { lines: [], total: 120 }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      })
      await page.goto(process.env.UI_BASE_URL || 'http://127.0.0.1:5173')
      await page.getByRole('heading', { name: '报价助手', exact: true }).waitFor()
      if (name === 'mobile') await page.getByRole('button', { name: '会话列表', exact: true }).click()
      await page.locator('.chat-history-item').filter({ hasText: job.title }).click()
      await page.getByRole('heading', { name: job.title, exact: true }).waitFor()
      await page.getByText('已整理 120 / 240 页', { exact: true }).waitFor()
      assert.equal(await page.getByRole('progressbar', { name: '资料识别进度' }).getAttribute('value'), '120')
      await page.screenshot({ path: path.join(output, `large-pdf-progress-${name}.png`) })
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
      await page.getByRole('button', { name: '明细与报价', exact: true }).click()
      const drawer = page.getByRole('dialog', { name: '明细与报价', exact: true })
      await drawer.waitFor()
      assert.equal(await drawer.locator('.requirements-desktop-table .el-table__body tbody tr').count(), 50)
      assert.equal(await drawer.locator('.requirements-mobile-list article').count(), 50)
      await drawer.locator('.el-pagination .btn-next').click()
      const list = drawer.locator(name === 'mobile' ? '.requirements-mobile-list' : '.requirements-desktop-table')
      await list.getByText('Sign 51', { exact: true }).first().waitFor()
      await page.screenshot({ path: path.join(output, `large-pdf-details-${name}.png`) })
      await page.close()
    }
    assert.deepEqual(errors, [])
    console.log('Large PDF progress and pagination passed on desktop and mobile')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
