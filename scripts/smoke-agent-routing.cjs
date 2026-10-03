const { chromium } = require('playwright')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  const now = Date.now() / 1000
  const at = new Date().toISOString()
  const line = {
    id: 'pending', product: 'A座造型发光标识', text_content: 'A', material: '亚克力＋紫铜',
    width_mm: null, height_mm: '2500', thickness_mm: null, billing_length_mm: null,
    quantity: 1, language: 'en', process: '立体发光字', notes: '', evidence: [],
    uncertainties: [], confirmed: false, selected_price_id: null, selected_price_revision: null,
    price_review_note: '', extras: [],
  }
  const issue = {
    line_id: line.id, product: line.product, cause: 'customer_input',
    reason: '单字宽度未标注，无法确定计价面积，缺少合理暂估依据。',
    question: '请提供字母A的宽度（毫米）或带宽度标注的图纸。',
    missing_fields: ['width_mm'],
  }
  const knownLine = { ...line, id: 'priced', product: '入口玻璃门腰线', text_content: '',
    process: '贴膜', material: '贴膜材料', width_mm: '800', height_mm: '120',
    manual_unit_price: '180', manual_price_note: '测试夹具：已保存的制作价' }
  const run = {
    id: 1, status: 'processing', created_at: at, phase: 'running', active_product: '电梯厅楼层标识',
    message: '正在处理其他项目，暂挂项目的问题已保存。',
    usage: { durable: true, phase: 'running', last_progress_at: now - 180,
      last_event_at: now, deferred_requirements: [issue] },
    steps: [],
  }
  const job = {
    id: 900001, title: '三路径报价验证', customer: '', brief: '测试样例，不写入业务数据库。',
    status: 'processing', revision: 1, created_at: at, updated_at: at,
    line_count: 2, pending_count: 1, file_count: 0,
    requirements: [knownLine, line], extraction: {}, files: [], messages: [], runs: [], quotes: [],
    agent_runs: [run],
    pricing_progress: { priced_count: 1, line_count: 2, known_subtotal: '180.00',
      complete: false, saved_at: now },
  }
  let finished = false
  const quote = {
    id: 1, version: 1, job_revision: 2, status: 'blocked', outdated: false, terms: '',
    created_at: at, approval_note: '',
    payload: {
      lines: [{ line_id: knownLine.id, product: knownLine.product, requirement: knownLine,
        amount: '180.00', unit_price: '180.00', formula: '180 x 1 = 180', price: null,
        blockers: [], warnings: [], manual_price: true },
      { line_id: line.id, product: line.product, requirement: line,
        amount: null, unit_price: null, formula: '', price: null,
        blockers: [issue.reason], warnings: [], deferred_issue: issue }],
      complete: false, total: null, known_subtotal: '180.00', currency: 'CNY',
      review_questions: [`${issue.product}：${issue.question}`],
      confirmation_items: [{ owner: '需求方', question: issue.question,
        lines: [{ line_id: line.id, product: line.product }] }],
    },
  }
  const finalJob = () => ({ ...job, status: 'needs_review', revision: 2, quotes: [quote],
    agent_runs: [{ ...run, status: 'waiting', phase: 'waiting', message: '其他项目已处理；本项待补充，已计价小计不是整单总价。',
      usage: { ...run.usage, phase: 'waiting' } }] })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const errors = []
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'mock-test-only'))
    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname
      let body
      if (pathname === '/api/session') body = { authenticated: true }
      else if (pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0,
        inactive: 0, products: 0, rules: 0, documents: 0 }
      else if (pathname === '/api/options') body = { categories: [], products: [] }
      else if (pathname.endsWith('model-config') || pathname.endsWith('embedding-config')) {
        body = { configured: true, model: 'mock-only', environment_managed: false }
      } else if (pathname === '/api/jobs') body = { items: [finished ? finalJob() : job], total: 1 }
      else if (pathname.endsWith('/progress')) body = {
        id: job.id, revision: finished ? 2 : 1, status: finished ? 'needs_review' : 'processing',
        pricing_progress: job.pricing_progress, server_time: now,
        run: { ...run, offset: 0, step_count: 0 },
      }
      else if (pathname === `/api/jobs/${job.id}`) body = finished ? finalJob() : job
      else throw new Error(`Unexpected API request: ${route.request().method()} ${pathname}`)
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto(process.env.ROUTING_UI_URL || 'http://127.0.0.1:5175')
    await page.locator('.chat-history-item').first().click()
    await page.getByRole('heading', { name: job.title, exact: true }).waitFor()
    await page.getByRole('region', { name: '逐项待补充问题' }).waitFor()
    await page.getByText('最近新增计价：3 分钟前', { exact: true }).waitFor()
    await page.getByText('最近工具调用：刚刚', { exact: true }).waitFor()
    assert(await page.getByText(issue.question, { exact: false }).isVisible())
    await page.screenshot({ path: path.join(output, 'agent-routing-desktop.png'), animations: 'disabled' })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1)
    await page.screenshot({ path: path.join(output, 'agent-routing-mobile.png'), fullPage: true, animations: 'disabled' })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      'Pending questions overflow the mobile viewport')
    finished = true
    await page.locator('.chat-quote-total').waitFor()
    assert(await page.getByText('未计价事项', { exact: true }).isVisible())
    assert.equal(await page.getByRole('button', { name: '批准报价', exact: true }).count(), 0)
    assert(await page.getByRole('textbox', { name: '消息', exact: true }).isEnabled())
    await page.screenshot({ path: path.join(output, 'agent-routing-partial-mobile.png'), fullPage: true, animations: 'disabled' })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      'Partial quote overflows the mobile viewport')
    assert.deepEqual(errors, [])
    console.log('Routing UI passed: desktop/mobile questions, real progress labels, partial-quote safety')
  } finally {
    await browser.close()
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
