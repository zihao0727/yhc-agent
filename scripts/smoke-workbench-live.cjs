const { chromium } = require('playwright')
const { readFileSync, mkdirSync } = require('node:fs')
const { parseEnv } = require('node:util')
const path = require('node:path')
const assert = require('node:assert/strict')

async function main() {
  const root = path.resolve(__dirname, '..')
  const config = parseEnv(readFileSync(path.join(root, 'backend/.env'), 'utf8'))
  const output = path.join(root, '.runtime/screenshots')
  mkdirSync(output, { recursive: true })
  const browser = await chromium.launch({ channel: 'chrome', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(token => sessionStorage.setItem('firefly-token', token), config.ADMIN_TOKEN)
  // This acceptance reads existing data and never uploads, recalculates or approves an order.
  await page.route('**/api/**', route => {
    assert.equal(route.request().method(), 'GET', 'Live acceptance must remain read-only')
    return route.continue()
  })
  try {
    const url = process.env.UI_URL || 'http://127.0.0.1:5173'
    await page.goto(url)
    await page.locator('.chat-history-item').first().waitFor()
    const targetId = Number(process.env.JOB_ID || 20)
    assert(Number.isSafeInteger(targetId) && targetId > 0, 'JOB_ID must be a positive integer')
    const desired = await page.evaluate(async id => {
      const response = await fetch(`/api/jobs/${id}`, { headers: { 'X-Admin-Token': sessionStorage.getItem('firefly-token') || '' } })
      return response.ok ? response.json() : null
    }, targetId)
    if (desired) {
      await page.getByRole('textbox', { name: '搜索会话', exact: true }).fill(desired.title)
      await page.getByRole('button', { name: '执行会话搜索', exact: true }).click()
      await page.locator(`.chat-history-item[data-job-id="${desired.id}"]`).click()
    } else await page.locator('.chat-history-item').first().click()
    await page.getByRole('tab', { name: /^待补充/ }).waitFor()
    await page.getByRole('tab', { name: /^待补充/ }).click()
    const count = await page.locator('.supplement-row').count()
    await page.screenshot({ path: path.join(output, 'workbench-live-pending-desktop.png'), animations: 'disabled' })
    if (count) {
      const scopedRow = page.locator('.supplement-row').filter({
        has: page.locator('.supplement-heading').filter({ hasText: '建筑入口标识（演员）' }),
      }).first()
      const row = await scopedRow.count() ? scopedRow : page.locator('.supplement-row').first()
      await row.locator('.supplement-heading').click()
      for (const input of await row.locator('.supplement-scoped-fields input').all()) {
        assert(await input.isEnabled(), 'Scoped local draft fields are editable')
      }
      assert.doesNotMatch(await row.locator('.supplement-question').allTextContents().then(text => text.join('\n')),
        /billable_area_m2|catalog_id|\/parameters\//)
    }
    await page.screenshot({ path: path.join(output, 'workbench-live-form-desktop.png'), animations: 'disabled' })
    await page.getByRole('tab', { name: '报价结果', exact: true }).click()
    await page.locator('.result-actions, .result-pending-action, .workbench-empty').first().waitFor()
    const pricedFilter = page.getByRole('button', { name: /^已计价 \d+$/, exact: true })
    if (await pricedFilter.count()) {
      const priced = Number((await pricedFilter.innerText()).match(/\d+/)[0])
      if (priced > 0) {
        await pricedFilter.click()
        const resultRows = page.locator('.result-table tbody tr')
        assert.equal(await resultRows.count(), Math.min(priced, 50), 'Priced count agrees with visible rows')
        for (const text of await resultRows.allInnerTexts()) {
          assert(text.includes('¥'), 'Priced rows show their amounts')
          assert(!text.includes('? × ?'), 'Unknown dimensions are not displayed as bogus specifications')
        }
      }
    }
    await page.screenshot({ path: path.join(output, 'workbench-live-results-desktop.png'), animations: 'disabled' })
    await page.setViewportSize({ width: 390, height: 844 })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Live mobile overflow')
    await page.screenshot({ path: path.join(output, 'workbench-live-results-mobile.png'), animations: 'disabled' })
    await page.getByRole('tab', { name: /^待补充/ }).click()
    await page.screenshot({ path: path.join(output, 'workbench-live-pending-mobile.png'), animations: 'disabled' })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    assert.deepEqual(errors, [])
    console.log(`PASS: existing-order read-only UI, ${count} pending question groups, results, desktop/mobile, no runtime errors.`)
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
