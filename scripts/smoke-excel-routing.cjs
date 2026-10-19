const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const errors = []
  const at = '2026-10-12T01:00:00Z'
  const run = {
    id: 1, status: 'processing', phase: 'extracting', created_at: at, steps: [],
    message: '正在整理资料：已读取 5/15 行，已保存 0 项明细。正在读取单元格。',
    usage: { durable: true, prepared: false, extraction_only: true, phase: 'extracting',
      extraction_progress: { completed_units: 5, total_units: 15, unit: '行',
        completed_pages: 0, total_pages: 2, saved_lines: 0, page: 1,
        detail: '客户制作需求及数量清单.xlsx · 客户明细 · 第 2—6 行，读取单元格' } },
  }
  const job = {
    id: 101, title: 'Excel资料整理', customer: '', brief: '提取表格需求',
    status: 'processing', revision: 1, requirements: [], line_count: 0, pending_count: 0,
    created_at: at, updated_at: at, business_rules_version: '2026-09-23.v1',
    messages: [], extraction: {}, quotes: [], runs: [], agent_runs: [run],
    files: [{ id: 1, filename: '客户制作需求及数量清单.xlsx',
      media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      byte_size: 15000, page_count: 2 }],
  }
  try {
    for (const [name, viewport] of [['desktop', { width: 1440, height: 1000 }],
      ['mobile', { width: 390, height: 844 }]]) {
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
        else if (url.pathname === '/api/jobs') body = { items: [job], total: 1 }
        else if (url.pathname === '/api/jobs/101/progress') body = { id: 101, revision: 1,
          status: job.status, run: { ...run, offset: 0 }, server_time: Date.now() / 1000 }
        else if (url.pathname === '/api/jobs/101') body = job
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      })
      await page.goto(process.env.UI_BASE_URL || 'http://127.0.0.1:5173')
      await page.getByRole('heading', { name: '报价助手', exact: true }).waitFor()
      if (name === 'mobile') await page.getByRole('button', { name: '会话列表', exact: true }).click()
      await page.locator('.chat-history-item').filter({ hasText: job.title }).click()
      await page.getByText('已整理 5 / 15 行', { exact: true }).waitFor()
      const progress = page.getByRole('progressbar', { name: '资料识别进度' })
      assert.equal(await progress.getAttribute('value'), '5')
      assert.equal(await progress.getAttribute('max'), '15')
      assert(!(await page.locator('.chat-progress-strip').innerText()).includes('当前第'))
      await page.getByText('2 个预览分段', { exact: true }).waitFor()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
      await page.screenshot({ path: path.join(output, `excel-routing-${name}.png`) })
      await page.close()
    }
    assert.deepEqual(errors, [])
    console.log('Excel row progress passed on desktop and mobile')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
