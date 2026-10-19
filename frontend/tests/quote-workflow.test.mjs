import assert from 'node:assert/strict'
import test from 'node:test'
import { liveQuoteLines } from '../src/quote-display.ts'
import { pendingRows, quoteSummary, resultStatus } from '../src/quote-workspace.ts'
import { workspaceVersion } from '../src/generated-quote-contract.ts'

function job() {
  return {
    revision: 3, status: 'needs_review', requirements: [{ id: 'sign', product: 'Sign' }],
    agent_runs: [{ id: 7, usage: { deferred_requirements: [{ line_id: 'sign',
      cause: 'customer_input', question: 'Old incorrect question' }] } }],
    quotes: [{ payload: { lines: [{ line_id: 'sign', amount: '1299.60' }] } }],
    quote_workspace: {
      version: workspaceVersion, revision: 3, run_id: 7, preparing: false,
      lines: [{ line_id: 'sign', product: 'Sign', amount: null, blockers: ['Fix plan'],
        formula: '', price: null, workflow: { state: 'system_error', label: '系统待处理', valid: false } }],
      pending: [{ key: 'one', kind: 'system', cause: 'plan_repair', owner: '系统核价',
        question: 'Fix component quantities', reason: 'Outer dimensions cannot price every layer',
        products: [{ line_id: 'sign', product: 'Sign', source_item: '1' }], fields: [], inputs: [] }],
      summary: { count: 1, priced: 0, pending: 1, amount: null, complete: false, outdated: false },
    },
  }
}

test('canonical results and blockers replace conflicting historical and run representations', () => {
  const current = job()
  assert.equal(liveQuoteLines(current)[0].amount, null)
  assert.equal(resultStatus(liveQuoteLines(current)[0]), '系统待处理')
  assert.equal(quoteSummary(current).amount, null)
  assert.equal(pendingRows(current).length, 1)
  assert.equal(pendingRows(current)[0].question, 'Fix component quantities')
  assert.match(pendingRows(current)[0].reason, /Outer dimensions/)
})

test('zero is a valid calculated amount and is distinct from no quotation', () => {
  const current = job()
  current.quote_workspace.lines[0].amount = '0.00'
  current.quote_workspace.lines[0].workflow = { state: 'priced', label: '已计价', valid: true }
  current.quote_workspace.pending = []
  current.quote_workspace.summary = { count: 1, priced: 1, pending: 0, amount: '0.00',
    complete: true, outdated: false }
  assert.equal(quoteSummary(current).amount, '0.00')
  assert.equal(resultStatus(liveQuoteLines(current)[0]), '已计价')
  assert.deepEqual(pendingRows(current), [])
})

test('a response for a previous input revision or run cannot override current progress', () => {
  const current = job()
  current.quotes = []
  current.agent_runs[0].usage = {}
  current.quote_workspace.revision = 2
  assert.deepEqual(pendingRows(current), [])
  current.quote_workspace.revision = 3
  current.quote_workspace.run_id = 6
  assert.deepEqual(pendingRows(current), [])
})
