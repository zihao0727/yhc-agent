// Read-only acceptance of actual incident sources and the current rendered projection.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { parseEnv } = require('node:util')
const { chromium } = require('playwright')
const fixture = require('./fixtures/latest-image-cases.json')

async function main() {
  const root = path.resolve(__dirname, '..')
  const config = parseEnv(fs.readFileSync(path.join(root, 'backend/.env'), 'utf8'))
  const base = process.env.QUOTE_WEB_URL || 'http://127.0.0.1:3001'
  const headers = { 'X-Admin-Token': config.ADMIN_TOKEN }
  const output = path.join(root, '.runtime/screenshots')
  fs.mkdirSync(output, { recursive: true })
  async function get(endpoint) {
    const response = await fetch(`${base}/api${endpoint}`, { headers })
    assert(response.ok, `${endpoint}: HTTP ${response.status}`)
    return response.json()
  }
  const contract = await get('/quotation-contract')
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  const report = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(token => sessionStorage.setItem('firefly-token', token), config.ADMIN_TOKEN)
  await page.route('**/api/**', route => route.request().method() === 'GET' ? route.continue() : route.abort())
  try {
    await page.goto(base)
    await page.locator('.chat-history').waitFor()
    for (const sample of fixture.cases) {
      const job = await get(`/jobs/${sample.job_id}`)
      assert(job.files.some(file => file.sha256 === sample.source_sha256), 'Incident source changed')
      assert.equal(job.quote_workspace.version, contract.workspace_version)
      const progress = await get(`/jobs/${job.id}/progress`)
      assert.deepEqual(progress.quote_workspace.summary, job.quote_workspace.summary)
      assert.deepEqual(progress.quote_workspace.pending, job.quote_workspace.pending)
      const line = job.requirements[0]
      assert(line, 'Recognized requirement is missing')
      const candidates = await get(`/jobs/${job.id}/lines/${line.id}/candidates?q=${encodeURIComponent(line.product)}`)
      for (const expected of fixture.materials) {
        assert(candidates.prices.some(price =>
          (expected.material === '不锈钢' ? /201|304|316|不锈钢/.test(price.material)
            : price.material.includes(expected.material)) && Number(price.thickness_mm) === expected.thickness_mm),
        `Missing ${expected.material} ${expected.thickness_mm}mm`)
      }
      const source = await fetch(`${base}/api/customer-files/${job.files[0].id}/pages/1`, { headers })
      assert(source.ok && source.headers.get('content-type').startsWith('image/'), 'Source preview failed')
      assert((await source.arrayBuffer()).byteLength > 1000, 'Source image is blank')
      const loaded = page.waitForResponse(response =>
        response.url().endsWith(`/api/jobs/${job.id}`) && response.ok())
      await page.locator(`.chat-history-item[data-job-id="${job.id}"]`).click()
      await loaded
      await page.getByRole('tab', { name: '报价结果', exact: true }).click()
      await page.locator('.result-table tbody tr').first().waitFor()
      const current = await page.locator('.result-table').innerText()
      const rows = job.quote_workspace.lines
      assert(!current.includes('1,299.60'), 'Invalid historical amount was displayed as current')
      for (const row of rows) {
        if (row.amount !== null) assert(row.workflow.valid, 'Unvalidated amount is exposed')
      }
      await page.screenshot({ path: path.join(output, `workflow-${job.id}-desktop.png`) })
      await page.getByRole('tab').filter({ hasText: '待补充' }).click()
      await page.locator('.supplement-row').first().waitFor()
      const pending = await page.locator('.supplement-workspace').innerText()
      assert.equal(await page.locator('.supplement-row').count(), job.quote_workspace.pending.length)
      for (const issue of job.quote_workspace.pending) {
        assert(issue.reason && issue.owner, 'Pending issue needs a reason and responsible owner')
        assert(pending.includes(issue.owner), 'Issue owner is missing in the UI')
      }
      if (job.quote_workspace.summary.priced === 0) {
        assert(pending.includes('实际采购件数') && pending.includes('LED模组/灯带'),
          'Real customer/pricing questions were lost')
      }
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
          `Horizontal overflow at ${width}px`)
        await page.screenshot({ path: path.join(output, `workflow-${job.id}-pending-${width}.png`) })
      }
      await page.setViewportSize({ width: 1440, height: 1000 })
      report.push({ job_id: job.id, revision: job.revision, source_sha256: sample.source_sha256,
        candidates: candidates.prices.length, summary: job.quote_workspace.summary,
        pending_owners: job.quote_workspace.pending.map(issue => issue.owner) })
    }
    assert.deepEqual(errors, [], 'Browser errors')
    fs.writeFileSync(path.join(root, '.runtime/quote-workflow-acceptance.json'),
      JSON.stringify({ checked_at: new Date().toISOString(), read_only: true, cases: report }, null, 2))
    console.log(JSON.stringify({ passed: report.length, read_only: true, cases: report }))
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1 })
