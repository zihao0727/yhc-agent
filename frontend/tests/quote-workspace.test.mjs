import assert from 'node:assert/strict'
import test from 'node:test'
import { applySupplementEdits, pendingPresentation, pendingRows, quoteSummary, resultStatus } from '../src/quote-workspace.ts'

const line = (id = 'panel') => ({
  id, product: 'Panel', source_item: '12', quantity: null, width_mm: '1000', height_mm: '500',
  material: 'Aluminium', process: 'Cutting', area_m2: '0.5', confirmed: false,
  selected_price_id: 2, selected_price_revision: 1, text_evidence: [],
  uncertainties: [], components: [], letter_groups: [],
  estimate: { contract_version: 3, assumptions: [{ field: 'quantity', value: '1', target: 'item' },
    { field: 'width_mm', value: '1000', target: 'item' }], costs: [{ expected_amount: '50' }],
    proposed_components: [{ object_id: 'back', name: 'Back panel' }] },
})
const job = () => ({
  requirements: [line()], quotes: [], agent_runs: [], line_count: 1, status: 'needs_review',
})

test('pending rows deduplicate quote, run and legacy questions while keeping original scope and missing fields', () => {
  const current = job()
  const issue = { line_id: 'panel', product: 'Panel', cause: 'customer_input', question: 'Confirm quantity',
    reason: 'Quantity missing', missing_fields: ['quantity', 'components[0].width_mm'] }
  current.agent_runs = [{ usage: { deferred_requirements: [issue] } }]
  current.quotes = [{ payload: { confirmation_items: [{ question: issue.question, owner: 'Customer',
    cause: issue.cause, lines: [{ line_id: 'panel', product: 'Panel' }] }],
    review_questions: ['Panel:Confirm quantity'],
    lines: [{ requirement: line(), line_id: 'panel', amount: null, deferred_issue: issue }] } }]
  const rows = pendingRows(current)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].products[0].source_item, '12')
  assert.deepEqual(rows[0].fields, ['quantity'])
  assert.equal(rows[0].kind, 'information')
  assert.equal(rows[0].reason, issue.reason)
})

test('system failures are not routed to customer forms and price gaps remain distinct', () => {
  const current = job()
  current.agent_runs = [{ usage: { deferred_requirements: [
    { line_id: 'panel', product: 'Panel', cause: 'execution_error', question: 'Retry calculation', missing_fields: [] },
    { line_id: 'panel', product: 'Panel', cause: 'catalog_gap', question: 'Provide rate basis', missing_fields: [] },
  ] } }]
  assert.deepEqual(pendingRows(current).map(row => row.kind), ['system', 'pricing'])
  current.quotes = [{ payload: { lines: [], confirmation_items: [{
    owner: '报价管理员', cause: 'recognition_review', question: 'Confirm drawing dimensions', lines: [],
  }] } }]
  assert.equal(pendingRows(current).find(row => row.question === 'Confirm drawing dimensions').kind, 'system')
})

test('recognition uncertainties alone do not become customer supplement requests', () => {
  const current = job()
  current.requirements[0].uncertainties = ['Read original drawing', 'Four components per set']
  assert.deepEqual(pendingRows(current), [])
  current.agent_runs = [{ usage: { deferred_requirements: [{
    line_id: 'panel', product: 'Panel', cause: 'customer_input',
    question: 'Confirm quantity', missing_fields: ['quantity'],
  }] } }]
  assert.equal(pendingRows(current)[0].kind, 'information')
  assert.deepEqual(pendingRows(current)[0].fields, ['quantity'])
})

test('each question keeps its own inputs even when the same project has multiple issues', () => {
  const current = job()
  current.agent_runs = [{ usage: { deferred_requirements: [
    { line_id: 'panel', product: 'Panel', cause: 'customer_input', question: 'Confirm width',
      missing_fields: ['width_mm'], inputs: [{ key: 'item:item:width_mm', line_id: 'panel',
        target: 'item', target_id: null, target_name: 'Panel', field: 'width_mm', missing: true }] },
    { line_id: 'panel', product: 'Panel', cause: 'catalog_gap', question: 'Provide LED rate',
      missing_fields: [], inputs: [] },
    { line_id: 'panel', product: 'Panel', cause: 'execution_error', question: 'Retry calculation',
      missing_fields: [], inputs: [] },
  ] } }]
  const rows = pendingRows(current)
  assert.match(pendingPresentation(rows[0]).question, /宽度/)
  assert.equal(pendingPresentation(rows[1]).question, 'Provide LED 单价')
  assert.equal(pendingPresentation(rows[2]).question, 'Retry calculation')
  assert.deepEqual(rows.slice(1).map(row => row.inputs), [[], []])
})

test('identical question wording with different causes is not merged', () => {
  const current = job()
  current.agent_runs = [{ usage: { deferred_requirements: ['catalog_gap', 'customer_input'].map(cause => ({
    line_id: 'panel', product: 'Panel', cause, question: 'Please confirm', missing_fields: [],
  })) } }]
  const rows = pendingRows(current)
  assert.equal(rows.length, 2)
  assert.notEqual(rows[0].key, rows[1].key)
})

test('legacy validation dumps have a readable summary while retaining technical evidence and row keys', () => {
  const current = job()
  const raw = "相同计价校验多次未通过；1 validation error for EstimateCost\nrate\nDecimal input should have no more than 6 decimal places [type=decimal_max_places, input_value=Decimal('99.99999962')]"
  current.agent_runs = [{ usage: { deferred_requirements: [
    { line_id: 'panel', product: 'Panel', cause: 'execution_error', reason: raw, missing_fields: [] },
  ] } }]
  const row = pendingRows(current)[0]
  const shown = pendingPresentation(row)
  assert.match(shown.question, /小数精度/)
  assert.doesNotMatch(shown.question, /validation error|decimal_max_places/)
  assert.equal(shown.technical, raw)
  assert.equal(row.question, raw)
  assert.match(row.key, /decimal_max_places/)
  assert.equal(row.kind, 'system')
  assert.equal(pendingPresentation({ ...row, question: '方案校验失败', reason: raw }).technical, `方案校验失败\n${raw}`)
})

test('ordinary missing facts and price questions keep their complete original wording', () => {
  const row = { question: '请确认数量及尺寸', reason: '原始资料缺少数量', owner: '需求方', products: [] }
  assert.deepEqual(pendingPresentation(row), { question: row.question, reason: row.reason, technical: '' })
})

test('one grouped question is not duplicated by deferred questions for each included project', () => {
  const current = job()
  current.requirements.push(line('second'))
  current.quotes = [{ payload: { lines: [], confirmation_items: [{
    question: 'Confirm quantity', owner: 'Customer', cause: 'customer_input',
    lines: current.requirements.map(line => ({ line_id: line.id, product: line.product })),
  }] } }]
  current.agent_runs = [{ usage: { deferred_requirements: current.requirements.map(line => ({
    line_id: line.id, product: line.product, cause: 'customer_input', question: 'Confirm quantity', missing_fields: ['quantity'],
  })) } }]
  assert.equal(pendingRows(current).length, 1)
  assert.equal(pendingRows(current)[0].products.length, 2)
})

test('freshly priced projects and approved quotes do not retain obsolete run questions', () => {
  const current = job()
  current.agent_runs = [{ usage: { deferred_requirements: [{
    line_id: 'panel', product: 'Panel', cause: 'catalog_gap', question: 'Find price', missing_fields: [],
  }] } }]
  current.quotes = [{ status: 'draft', outdated: false, payload: {
    lines: [{ line_id: 'panel', amount: '100' }], confirmation_items: [], review_questions: [],
  } }]
  assert.equal(pendingRows(current).length, 0)
  current.quotes[0].status = 'approved'
  current.quotes[0].payload.review_questions = ['Confirm estimated rate']
  assert.equal(pendingRows(current).length, 0)
})

test('partial amounts and genuine zero amounts remain distinguishable from missing prices', () => {
  const current = job()
  current.quotes = [{ outdated: false, payload: { lines: [{ amount: '0.00' }], complete: false,
    total: null, known_subtotal: '0.00' } }]
  assert.equal(quoteSummary(current).amount, '0.00')
  assert.equal(quoteSummary(current).priced, 1)
  current.quotes[0].payload.lines[0].amount = null
  assert.equal(quoteSummary(current).amount, null)
  assert.equal(quoteSummary(current).priced, 0)
  assert.equal(resultStatus({ amount: null, resolution: 'execution_error' }), '系统待处理')
})

test('supplement quantity updates only selected projects, removes stale whole-item assumptions and preserves costs', () => {
  const requirements = [line(), line('second')]
  const result = applySupplementEdits(requirements, [{ lineId: 'panel', field: 'quantity', value: '20' }])
  assert.equal(result[0].quantity, 20)
  assert.equal(result[0].estimate.assumptions.some(item => item.field === 'quantity'), false)
  assert.equal(result[0].estimate.costs[0].expected_amount, '50')
  assert.deepEqual(result[1], requirements[1])
  assert.equal(requirements[0].quantity, null)
  assert.match(result[0].text_evidence[0], /20/)
})

test('dimension supplements invalidate area and computed amounts; material changes invalidate old costs', () => {
  const result = applySupplementEdits([line()], [{ lineId: 'panel', field: 'width_mm', value: '2000' }])[0]
  assert.equal(result.area_m2, null)
  assert.equal(result.estimate.costs[0].expected_amount, null)
  const changed = applySupplementEdits([line()], [{ lineId: 'panel', field: 'material', value: 'Steel' }])[0]
  assert.equal(changed.estimate, null)
  assert.equal(changed.selected_price_id, null)
  assert.equal(changed.components[0].object_id, 'back')
})

test('conflicting grouped responses, invalid quantities, missing targets and unsupported fields cannot save', () => {
  assert.throws(() => applySupplementEdits([line()], [
    { lineId: 'panel', field: 'quantity', value: '20' }, { lineId: 'panel', field: 'quantity', value: '10' },
  ]))
  for (const value of ['-1', '0', '1.5', 'Infinity', 'twenty', '1000001']) {
    assert.throws(() => applySupplementEdits([line()], [{ lineId: 'panel', field: 'quantity', value }]))
  }
  assert.throws(() => applySupplementEdits([line()], [{ lineId: 'missing', field: 'quantity', value: '1' }]))
  assert.throws(() => applySupplementEdits([line()], [{ lineId: 'panel', field: 'confirmed', value: 'true' }]))
})

test('manual finished prices require a basis and preserve explicit zero without implying approval', () => {
  assert.throws(() => applySupplementEdits([line()], [{ lineId: 'panel', field: 'manual_unit_price', value: '100' }]))
  const result = applySupplementEdits([line()], [
    { lineId: 'panel', field: 'manual_unit_price', value: '0' },
    { lineId: 'panel', field: 'manual_price_note', value: 'Free sample, all fabrication included' },
  ])[0]
  assert.equal(result.manual_unit_price, '0')
  assert.equal(result.confirmed, false)
})

test('new finished prices and area are preserved regardless of the order fields were selected', () => {
  const result = applySupplementEdits([line()], [
    { lineId: 'panel', field: 'manual_unit_price', value: '200' },
    { lineId: 'panel', field: 'manual_price_note', value: 'Complete fabrication price' },
    { lineId: 'panel', field: 'area_m2', value: '0.8' },
    { lineId: 'panel', field: 'material', value: 'Steel' },
    { lineId: 'panel', field: 'width_mm', value: '1600' },
  ])[0]
  assert.equal(result.manual_unit_price, '200')
  assert.equal(result.manual_price_note, 'Complete fabrication price')
  assert.equal(result.area_m2, '0.8')
})
