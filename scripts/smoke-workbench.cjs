const { chromium } = require('playwright')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

async function main() {
  const output = path.resolve(__dirname, '../.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  const at = '2026-10-13T02:00:00Z'
  const base = {
    text_content: '', material: '铝板', thickness_mm: '3', width_mm: '1000', height_mm: '500',
    billing_length_mm: null, quantity: null, language: 'unknown', process: '切割',
    notes: '', evidence: [], uncertainties: [], confirmed: false, selected_price_id: null,
    selected_price_revision: null, price_review_note: '', extras: [], text_evidence: [],
  }
  const requirements = [
    { ...base, id: 'door', product: '门牌标识', source_item: '12' },
    { ...base, id: 'arrow', product: '方向指引', source_item: '13' },
    { ...base, id: 'panel', product: '楼层总索引', source_item: '14', quantity: 2 },
    { ...base, id: 'error', product: '加工待恢复项目', source_item: '15', quantity: 1 },
  ]
  function quoteLine(line, amount = null) {
    return { line_id: line.id, product: line.product, requirement: structuredClone(line), amount,
      unit_price: amount === null ? null : '100.00', blockers: amount === null ? ['缺少数量'] : [], warnings: [],
      estimated: amount !== null, price: null, formula: amount === null ? '' : `100 × ${line.quantity}`,
      resolution: amount === null ? 'customer_confirmation' : 'priced_review',
      estimate_costs: amount === null ? [] : [{ label: '板材及加工', amount: '100.00', rate: '200',
        rate_unit: 'm2', measure: '0.5', kind: 'finished', basis: 'per_piece', source: 'agent_estimate',
        reason: '制作范围待审核', measurement_note: '单件板材尺寸换算面积', quantity_formula: '=width_mm * height_mm / 1000000',
        script_run: { source: 'result = width_mm * height_mm / 1000000', result: '0.5',
          inputs: { width_mm: '1000', height_mm: '500' }, checks: [{ message: '面积为正数', passed: true }] } }],
    }
  }
  const lines = requirements.map(line => quoteLine(line, line.id === 'panel' ? '200.00' : null))
  lines[3].resolution = 'execution_error'
  lines[3].deferred_issue = { line_id: 'error', product: requirements[3].product, cause: 'execution_error',
    question: "相同计价校验多次未通过；1 validation error for EstimateCost\nrate\nDecimal input should have no more than 6 decimal places [type=decimal_max_places, input_value=Decimal('99.99999962'), input_type=Decimal]\nFor further information visit https://errors.pydantic.dev/2.13/v/decimal_max_places",
    reason: '程序读取的单价精度超出字段限制', missing_fields: [], owner: '报价管理员' }
  const question = '请确认门牌及方向指引的制作数量。'
  const quote = { id: 1, version: 1, status: 'blocked', outdated: false, terms: '不含运输、安装及税费',
    job_revision: 1, created_at: at, approval_note: '', payload: {
      lines, complete: false, total: null, known_subtotal: '200.00', has_estimates: true, currency: 'CNY',
      confirmation_items: [
        { owner: '需求方', question, cause: 'customer_input', lines: requirements.slice(0, 2).map(line => ({ line_id: line.id, product: line.product })) },
        { owner: '报价管理员', question: lines[3].deferred_issue.question, cause: 'execution_error', lines: [{ line_id: 'error', product: requirements[3].product }] },
      ], review_questions: [`门牌标识：${question}`],
    } }
  const issues = requirements.slice(0, 2).map(line => ({ line_id: line.id, product: line.product,
    cause: 'customer_input', question, reason: question, missing_fields: ['quantity'] }))
  const job = { id: 900013, title: '杭州标识制作报价', customer: '样例客户', brief: '门牌及楼层标识制作',
    status: 'needs_review', revision: 1, created_at: at, updated_at: at,
    line_count: 4, pending_count: 3, file_count: 0, requirements, extraction: {},
    files: [], messages: [], runs: [], quotes: [quote],
    agent_runs: [{ id: 1, status: 'waiting', message: '已计算1项，请补充数量后继续',
      created_at: at, usage: { deferred_requirements: issues }, steps: [] }] }
  const other = { ...structuredClone(job), id: 900014, title: '另一份报价', quotes: [], requirements: [], agent_runs: [], line_count: 0 }
  const jobs = [job, other]
  let failSave = false
  let saves = 0
  let resumes = 0
  let conversations = 0
  let stopRequests = 0
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const errors = []
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => sessionStorage.setItem('firefly-token', 'mock-workbench-only'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    let body
    if (pathname === '/api/session') body = { authenticated: true }
    else if (pathname === '/api/stats') body = { prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 }
    else if (pathname === '/api/options') body = { categories: [], products: [] }
    else if (pathname === '/api/upload-config') body = {}
    else if (pathname.endsWith('model-config') || pathname.endsWith('embedding-config')) body = { configured: true, model: 'mock-only' }
    else if (pathname === '/api/jobs') body = { items: jobs, total: jobs.length }
    else if (pathname === `/api/jobs/${job.id}`) body = job
    else if (pathname === `/api/jobs/${other.id}`) body = other
    else if (pathname === `/api/jobs/${job.id}/requirements`) {
      const data = request.postDataJSON()
      assert.equal(data.revision, job.revision)
      if (failSave) {
        failSave = false
        await route.fulfill({ status: 503, json: { detail: '模拟保存失败' } })
        return
      }
      saves++
      job.requirements = data.lines
      job.revision++
      job.quotes.forEach(q => { q.outdated = true })
      body = job
    } else if (pathname === `/api/jobs/${job.id}/agent`) {
      const data = request.postDataJSON()
      assert.equal(data.intent, 'resume')
      assert.equal(data.replace_existing, false)
      assert.equal(data.revision, job.revision)
      resumes++
      const newLines = job.requirements.map(line => quoteLine(line, `${(line.quantity || 1) * 100}.00`))
      const version = job.quotes.length + 1
      job.quotes.unshift({ ...quote, id: version, version, job_revision: job.revision, status: 'draft',
        outdated: false, payload: { ...quote.payload, lines: newLines, confirmation_items: [], review_questions: [],
          complete: true, known_subtotal: '4300.00', total: '4300.00' } })
      job.agent_runs = [{ id: version, status: 'succeeded', message: '已生成新版报价', created_at: at,
        usage: { deferred_requirements: [] }, steps: [] }]
      job.status = 'quoted'
      body = job
    } else if (pathname === `/api/jobs/${job.id}/conversation`) {
      conversations++
      const data = request.postDataJSON()
      assert.match(data.supplementary_text, /整单补充/)
      job.messages.push({ role: 'user', text: data.supplementary_text, at })
      job.messages.push({ role: 'assistant', text: '建议将门牌数量改为30件，请确认', at })
      body = { job, changes: [{ line_id: 'door', field: 'quantity', value: '30', reason: '用户要求修改' }], operations: [] }
    } else if (pathname === `/api/jobs/${job.id}/changes`) {
      const data = request.postDataJSON()
      assert.equal(data.changes[0].value, '30')
      job.requirements.find(line => line.id === 'door').quantity = 30
      job.revision++
      job.quotes.forEach(q => { q.outdated = true })
      body = job
    } else if (pathname === `/api/jobs/${job.id}/quotes`) {
      const version = job.quotes.length + 1
      const next = { ...job.quotes[0], id: version, version, outdated: false,
        payload: { ...job.quotes[0].payload,
          lines: job.requirements.map(line => quoteLine(line, `${(line.quantity || 1) * 100}.00`)) } }
      job.quotes.unshift(next)
      body = next
    } else if (pathname.endsWith('/agent/stop')) { stopRequests++; body = job }
    else throw new Error(`Unexpected mock API: ${request.method()} ${pathname}`)
    await route.fulfill({ json: body })
  })
  const tab = name => page.getByRole('tab', { name, exact: true })
  async function openJob(title) {
    if (!(await page.locator('.chat-history').isVisible())) await page.getByRole('button', { name: '会话列表', exact: true }).click()
    await page.locator('.chat-history-item').filter({ hasText: title }).click()
    await page.getByRole('heading', { name: title, exact: true }).waitFor()
  }
  try {
    await page.goto(process.env.UI_URL || 'http://127.0.0.1:5173')
    await openJob(job.title)
    const pendingTab = () => page.getByRole('tab', { name: /^待补充/ })
    assert.equal(await pendingTab().getAttribute('aria-selected'), 'true')
    const system = page.locator('.supplement-row').filter({ hasText: '#15 加工待恢复项目' }).first()
    assert.match(await system.locator('.supplement-heading').innerText(), /小数精度/)
    assert.doesNotMatch(await system.locator('.supplement-heading').innerText(), /validation error|pydantic.dev/)
    await system.locator('.supplement-heading').click()
    assert.equal(await system.locator('.supplement-technical').isVisible(), false)
    await system.getByText('技术详情', { exact: true }).click()
    assert.match(await system.locator('.supplement-technical').innerText(), /99.99999962/)
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      await page.setViewportSize({ width, height })
      await system.scrollIntoViewIfNeeded()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: validation detail overflow`)
      await page.screenshot({ path: path.join(output, `workbench-validation-${name}.png`), animations: 'disabled' })
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await system.locator('.supplement-heading').click()
    const group = page.locator('.supplement-row').filter({ hasText: '#12 门牌标识、#13 方向指引' }).first()
    await group.locator('.supplement-heading').click()
    const quantity = group.getByRole('textbox', { name: '门牌标识、方向指引：数量', exact: true })
    await quantity.fill('20')
    await tab('报价结果').click()
    await page.getByText('已计价小计（非完整报价）', { exact: true }).waitFor()
    assert.equal(await page.locator('.result-table tbody tr').count(), 4)
    assert.equal(await page.getByText('¥ 0.00', { exact: true }).count(), 0)
    await pendingTab().click()
    assert.equal(await quantity.inputValue(), '20')
    await openJob(other.title)
    await openJob(job.title)
    await group.locator('.supplement-heading').click()
    assert.equal(await quantity.inputValue(), '20')
    await page.reload()
    await openJob(job.title)
    await group.locator('.supplement-heading').click()
    assert.equal(await quantity.inputValue(), '20')
    for (const [name, width, height] of [['desktop', 1440, 1000], ['tablet', 768, 1024], ['mobile', 390, 844], ['narrow', 320, 740]]) {
      await page.setViewportSize({ width, height })
      await quantity.scrollIntoViewIfNeeded()
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: page overflow`)
      const buttonFits = await page.locator('.supplement-footer .el-button').evaluateAll(buttons =>
        buttons.every(button => button.scrollWidth <= button.clientWidth + 2))
      assert(buttonFits, `${name}: action text overflow`)
      await page.screenshot({ path: path.join(output, `workbench-supplement-${name}.png`), animations: 'disabled' })
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    failSave = true
    await page.getByRole('button', { name: '保存补充', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: '模拟保存失败' }).waitFor()
    assert.equal(await quantity.inputValue(), '20')
    assert.equal(saves, 0)
    await group.locator('.supplement-targets .el-checkbox').nth(1).click()
    assert.equal(await group.getByRole('checkbox').nth(1).isChecked(), false)
    await page.getByRole('button', { name: '保存补充', exact: true }).click()
    await page.getByText('项目补充已保存', { exact: true }).waitFor()
    await page.getByRole('button', { name: '继续报价', exact: true }).waitFor()
    assert.equal(saves, 1)
    assert.equal(resumes, 0, 'Saving fields alone must not invoke the agent')
    assert.equal(job.requirements[0].quantity, 20)
    assert.equal(job.requirements[1].quantity, null, 'Deselected projects are outside the update scope')
    if (!(await group.getByRole('checkbox').nth(1).isChecked())) await group.locator('.supplement-targets .el-checkbox').nth(1).click()
    assert.equal(await group.getByRole('checkbox').nth(1).isChecked(), true)
    await quantity.fill('20')
    await page.getByRole('button', { name: '保存补充并继续报价', exact: true }).click()
    await tab('报价结果').waitFor()
    await page.getByText('¥ 4,300.00', { exact: true }).first().waitFor()
    assert.equal(saves, 2)
    assert.equal(resumes, 1, 'A submitted supplement resumes pricing once')
    assert.deepEqual(job.requirements.slice(0, 2).map(line => line.quantity), [20, 20])
    assert.equal(job.requirements[2].quantity, 2, 'Unselected projects remain intact')
    const resultRow = page.locator('.result-table tbody tr').filter({ hasText: '楼层总索引' })
    await resultRow.getByRole('button', { name: '楼层总索引', exact: true }).click()
    await page.getByRole('tab', { name: '采用参数', exact: true }).click()
    await page.getByRole('tab', { name: '来源与说明', exact: true }).click()
    await page.getByRole('tab', { name: '费用组成', exact: true }).click()
    assert.equal(await page.locator('.pricing-details pre').isVisible(), false, 'Scripts remain folded by default')
    await page.getByText('执行与校验记录', { exact: true }).click()
    await page.getByText('板材及加工 · 计算与校验', { exact: true }).click()
    assert(await page.locator('.pricing-details pre').isVisible())
    await page.locator('.el-drawer__close-btn:visible').click()
    await page.getByRole('dialog', { name: '楼层总索引', exact: true }).waitFor({ state: 'hidden' })
    await page.locator('.el-message').first().waitFor({ state: 'hidden' })
    await page.mouse.move(400, 400)
    for (const [name, width, height] of [['desktop', 1440, 1000], ['tablet', 768, 1024], ['mobile', 390, 844], ['narrow', 320, 740]]) {
      await page.setViewportSize({ width, height })
      await page.mouse.move(1, 1)
      await page.locator('.el-popper.is-dark.el-tooltip:visible').first().waitFor({ state: 'hidden' })
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: result overflow`)
      await page.screenshot({ path: path.join(output, `workbench-results-${name}.png`), animations: 'disabled' })
    }
    await page.getByRole('button', { name: '切换夜间模式' }).click()
    await page.screenshot({ path: path.join(output, 'workbench-results-dark.png'), animations: 'disabled' })
    await page.getByRole('button', { name: '切换日间模式' }).click()
    await page.setViewportSize({ width: 1440, height: 1000 })
    await pendingTab().click()
    await page.getByRole('textbox', { name: '整单补充', exact: true }).fill('门牌数量改为30件')
    await page.getByRole('button', { name: '保存补充', exact: true }).click()
    await page.getByRole('dialog', { name: '确认局部需求修改' }).waitFor()
    assert.equal(conversations, 1)
    assert.equal(resumes, 1, 'Save-only does not start another agent')
    await page.getByRole('button', { name: '确认修改并重新计价', exact: true }).click()
    await page.getByText('¥ 3,000.00', { exact: true }).first().waitFor()
    assert.equal(job.requirements[0].quantity, 30)
    await tab('沟通记录').click()
    assert.equal(await page.locator('.chat-quote, .chat-confirmation-details').count(), 0)
    assert(await page.getByRole('textbox', { name: '消息', exact: true }).isEnabled())
    assert.equal(stopRequests, 0)
    assert.deepEqual(errors, [], 'No browser runtime errors')
    console.log('PASS: grouped scoped supplements, persisted drafts, save failure/retry, one resume, updated quotes, details, script folding, free-text proposals, desktop/tablet/mobile and dark mode.')
    console.log('Screenshots: .runtime/screenshots/workbench-*.png')
  } catch (error) {
    console.error(await page.evaluate(() => ({
      width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      overflowing: [...document.querySelectorAll('body *')].filter(element => {
        const rect = element.getBoundingClientRect()
        return rect.width && rect.right > innerWidth + 1
      }).slice(0, 12).map(element => ({ tag: element.tagName, class: element.className,
        width: element.getBoundingClientRect().width, text: element.textContent?.slice(0, 100) })),
    })))
    await page.screenshot({ path: path.join(output, 'workbench-failure.png'), animations: 'disabled' })
    throw error
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
