import assert from 'node:assert/strict'
import test from 'node:test'
import { lineSpecification, liveQuoteLines, quantityUnit, quoteQuantity, quoteQuantityUnit, sourceStage, sourceRemainder } from '../src/quote-display.ts'
import { applySupplementEdits, compactObjectName, linePresentation, pendingKind, pendingPresentation, pendingRows, pendingSummary, quoteSummary, resultStatus } from '../src/quote-workspace.ts'

const line = id => ({
  id, product: 'Entrance sign', source_item: '2', quantity: 2, width_mm: null, height_mm: null,
  material: '', thickness_mm: null, notes: '1.规格尺寸:2531*315mm\n2.规格尺寸:1950*315mm、1982*210mm',
  evidence: [{ file_id: 1, page: 1, cell: 'D6', text: '规格尺寸:2531*315mm', source: 'text' },
    { file_id: 1, page: 1, cell: 'E6', text: '套', source: 'text' }],
  text_evidence: [], uncertainties: [], components: [], letter_groups: [], confirmed: false,
})
const job = () => ({
  id: 1, revision: 3, status: 'processing', requirements: [line('one'), line('two')],
  quotes: [], agent_runs: [{ id: 5, usage: {} }], pricing_progress: {
    revision: 3, run_id: 5, priced_count: 1, line_count: 2, known_subtotal: '100.00', complete: false,
    lines: [{ line_id: 'one', product: 'Entrance sign', amount: '100.00', unit_price: '50.00', blockers: [],
      effective_requirement: { width_mm: '2531', height_mm: '315', material: 'Steel' } }],
  },
})

test('engineering amounts display their original quantity and dimensional unit', () => {
  const raw = { ...line('engineering'), quantity: null,
    source_measurements: [{ value: '819.17', unit: 'm2', evidence: [] }] }
  const row = { requirement: raw, amount: '16393.40', billing_quantity: '819.17', billing_unit: 'm2' }
  assert.equal(quoteQuantity(row), '819.17')
  assert.equal(quoteQuantityUnit(row), '平方米')
  assert.equal(raw.quantity, null)
  assert.equal(quoteQuantity({ requirement: raw, amount: null }), '819.17')
  assert.equal(quantityUnit(raw), '平方米')
  raw.source_measurements.push({ value: '20', unit: 'm', evidence: [] })
  assert.equal(quoteQuantity({ requirement: raw, amount: null }), null)
})

test('location count keeps its actual unit across quote displays', () => {
  const raw = { ...line('painting'), quantity: 3, quantity_unit: '处' }
  assert.equal(quoteQuantity({ requirement: raw }), 3)
  assert.equal(quoteQuantityUnit({ requirement: raw }), '处')
})

test('paused source reading exposes the cause and never presents provisional rows as pricing work', () => {
  const current = job()
  current.status = 'needs_review'
  current.extraction = { incomplete: true }
  current.agent_runs[0].status = 'waiting'
  current.agent_runs[0].message = 'PDF page 3 needs another look'
  current.agent_runs[0].usage.extraction_progress = { completed_pages: 8, total_pages: 159, reviewed_pages: 2 }
  const stage = sourceStage(current)
  assert.equal(stage.title, '资料核对已暂停')
  assert.match(stage.message, /page 3/)
  assert.equal(stage.read, 8)
  assert.equal(stage.reviewed, 2)
  assert.equal(stage.total, 159)
  assert.equal(liveQuoteLines(current)[0].amount, null)
  assert.equal(resultStatus(liveQuoteLines(current)[0]), '待核对')
  assert.equal(quoteSummary(current).priced, 0)
  assert.equal(quoteSummary(current).amount, null)
  assert.equal(pendingRows(current).length, 0)
})

test('completed source review returns to the ordinary quotation display', () => {
  const current = job()
  current.extraction = { source_agent: { mode: 'ai', phase: 'complete' } }
  assert.equal(sourceStage(current), null)
  assert.equal(liveQuoteLines(current)[0].amount, '100.00')
  assert.equal(sourceRemainder(current), null)
})

test('prepared pricing remains visible when source review was explicitly skipped', () => {
  const current = job()
  current.extraction = { incomplete: true, source_agent: { phase: 'review' } }
  current.agent_runs[0].usage = { prepared: true, phase: 'running',
    source_review_skipped: { instruction: 'Skip review and start pricing' } }
  assert.equal(sourceStage(current), null)
  assert.equal(liveQuoteLines(current)[0].amount, '100.00')
  assert.equal(quoteSummary(current).priced, 1)
  assert.equal(quoteSummary(current).amount, '100.00')
  assert.notEqual(resultStatus(liveQuoteLines(current)[1]), '待核对')
})

test('partial source delivery keeps prices visible and reports pages still awaiting review', () => {
  const current = job()
  current.extraction = { source_agent: { mode: 'ai', phase: 'complete',
    unread_pages: [{ file_id: 1, page: 2 }], unreviewed_pages: [{ file_id: 1, page: 2 }] } }
  assert.equal(sourceStage(current), null)
  assert.deepEqual(sourceRemainder(current), { unread: 1, unreviewed: 1 })
  assert.equal(liveQuoteLines(current)[0].amount, '100.00')
  assert.equal(quoteSummary(current).amount, '100.00')
})

test('saved live prices and adopted specifications display before a quote version exists', () => {
  const current = job()
  const rows = liveQuoteLines(current)
  assert.equal(rows[0].amount, '100.00')
  assert.equal(rows[0].unit_price, '50.00')
  assert.equal(rows[1].amount, null)
  assert.match(lineSpecification(rows[0].effective_requirement), /2531 × 315 mm.*Steel/)
  assert.equal(current.requirements[0].width_mm, null)
  assert.equal(rows[0].requirement, current.requirements[0])
  assert.equal(quoteSummary(current).priced, 1)
})

test('stale snapshots and snapshots from an older run cannot supply prices', () => {
  const current = job()
  current.pricing_progress.revision = 2
  assert.equal(liveQuoteLines(current)[0].amount, null)
  current.pricing_progress.revision = 3
  current.pricing_progress.run_id = 4
  assert.equal(liveQuoteLines(current)[0].amount, null)
})

test('zero prices are shown and old questions for freshly priced rows disappear', () => {
  const current = job()
  current.pricing_progress.lines[0].amount = '0.00'
  current.agent_runs[0].usage.deferred_requirements = [{
    line_id: 'one', product: 'Entrance sign', cause: 'catalog_gap', question: 'Find a rate', missing_fields: [],
  }]
  assert.equal(liveQuoteLines(current)[0].amount, '0.00')
  assert.equal(pendingRows(current).length, 0)
})

test('specifications use actual components, literal source text and source quantity units without inventing dimensions', () => {
  const raw = line('one')
  assert.equal(quantityUnit(raw), '套')
  assert.equal(lineSpecification(raw), '原文规格：2531*315mm')
  raw.evidence = []
  assert.match(lineSpecification(raw), /1950\*315mm、1982\*210mm/)
  raw.components = [{ name: 'Frame', width_mm: '1950', height_mm: '315' }]
  assert.equal(lineSpecification(raw), 'Frame：1950 × 315 mm')
  assert.equal(raw.width_mm, null)
})

test('missing inputs are named by business meaning and exact component scope', () => {
  const input = { key: 'component:face:quantity_per_item', line_id: 'one', target: 'component',
    target_id: 'face', target_name: '发光面板', field: 'quantity_per_item', missing: true, value: null, basis: 'original' }
  const current = job()
  current.agent_runs[0].usage.deferred_requirements = [{
    line_id: 'two', product: 'Entrance sign', cause: 'recognition_review',
    reason: '连续操作未推进计价；计量对象缺少参数：billable_area_m2', missing_fields: ['/parameters/-'],
    inputs: [{ ...input, line_id: 'two' }],
  }]
  const row = pendingRows(current)[0]
  assert.equal(row.inputs[0].target_id, 'face')
  const shown = pendingPresentation(row)
  assert.match(shown.question, /发光面板.*每套该部件数量/)
  assert.doesNotMatch(shown.question, /billable_area_m2|parameters|未推进/)
  assert.match(shown.technical, /billable_area_m2/)
})

test('an older execution-labeled cost gap is presented as a concrete price question', () => {
  const row = { question: '连续操作未推进计价；费用LED电源缺少单价依据：选择catalog_id或提供待审核费率、单位和具体推导理由',
    cause: 'execution_error', owner: '系统运维', products: [] }
  assert.equal(pendingKind(row), 'pricing')
  assert.equal(pendingPresentation(row).question, '请补充“LED电源”的单价、计费单位，以及价格来源或估价依据。')
  assert.equal(pendingKind({ ...row, question: '计算服务连接失败' }), 'system')
  assert.equal(resultStatus({ amount: null, resolution: 'execution_error',
    deferred_issue: { cause: 'execution_error', reason: row.question, question: '' } }), '待核价')
})

test('grouped missing fields identify each project and name each component once', () => {
  const inputs = ['one', 'two'].flatMap(line_id => ['width_mm', 'height_mm'].map(field => ({
    line_id, field, target: 'component', target_id: 'face', target_name: '发光面板', missing: true,
  })))
  const shown = pendingPresentation({
    question: 'billable_area_m2',
    products: [{ line_id: 'one', source_item: '1', product: '入口标识' },
      { line_id: 'two', source_item: '4', product: '演员标识' }],
    inputs,
  })
  assert.equal(shown.question.split('\n').length, 3)
  assert.equal(shown.question.match(/发光面板/g).length, 2)
  assert.match(shown.question, /#1 入口标识.*部件宽度.*部件高度/)
  assert.match(shown.question, /#4 演员标识.*部件宽度.*部件高度/)
})

test('compact display removes repeated object dimensions and keeps the full stored name', () => {
  const name = '正面发光面板2531×315mm（316#1.0mm围边+8mm磨砂亚克力，3块/套）'
  assert.equal(compactObjectName(name), '正面发光面板')
  const row = { products: [], question: 'billable_area_m2', fields: [], inputs: [
    { target: 'component', target_name: name, field: 'width_mm', missing: true },
    { target: 'component', target_name: name, field: 'height_mm', missing: true },
  ] }
  assert.equal(pendingSummary(row), '正面发光面板 · 待补充：部件宽度 (mm)、部件高度 (mm)')
  assert.equal(row.inputs[0].target_name, name)
})

test('unpriced detail separates actionable facts from general terms and deduplicates explanations', () => {
  const current = {
    line_id: 'one', product: '入口标识', amount: null,
    blockers: ['尚未选择价格', '计量对象缺少参数：billable_area_m2'],
    warnings: ['工艺及尺寸原文已保留，计价时按对象解析；有歧义再核对来源'],
    estimate_conditions: ['工艺及尺寸原文已保留，计价时按对象解析；有歧义再核对来源'],
  }
  const issue = { cause: 'recognition_review', reason: '计量对象缺少参数：billable_area_m2', inputs: [
    { line_id: 'one', target: 'component', target_id: 'panel', target_name: '正面面板（含焊接）', field: 'width_mm', missing: true },
    { line_id: 'one', target: 'component', target_id: 'panel', target_name: '正面面板（含焊接）', field: 'height_mm', missing: true },
  ] }
  const shown = linePresentation(current, issue)
  assert.equal(shown.title, '需要系统处理')
  assert.deepEqual(shown.items, [{ label: '正面面板', text: '请补充部件宽度 (mm)、部件高度 (mm)' }])
  assert.deepEqual(shown.notes, ['尺寸和工艺以原始资料为准'])
  assert.equal(current.warnings[0], current.estimate_conditions[0])
})

test('unpriced detail prioritizes the concrete cost gap and retains other actual blockers', () => {
  const reason = '连续操作未推进计价；费用LED电源缺少单价依据：选择catalog_id或提供待审核费率、单位和具体推导理由'
  const shown = linePresentation({ line_id: 'one', amount: null,
    blockers: ['尚未选择价格', '缺少每件计价长度（mm）', reason] },
  { cause: 'execution_error', reason })
  assert.equal(shown.title, '还需确认价格')
  assert.equal(shown.items.length, 2)
  assert.match(shown.items[0].text, /LED电源.*单价.*计费单位/)
  assert.equal(shown.items[1].text, '请补充每件用于计费的长度（毫米）')
  assert.doesNotMatch(shown.items.map(item => item.text).join(), /catalog_id|未推进|尚未确定单价/)
})

test('scoped supplements update only the named component and retain whole-item dimensions', () => {
  const raw = { ...line('one'), width_mm: '5000', height_mm: '1000',
    components: [{ object_id: 'face', name: 'Face', width_mm: '100', height_mm: '200', area_m2: '0.02',
      quantity_per_item: null }], estimate: { assumptions: [], costs: [{ expected_amount: '10' }] } }
  const result = applySupplementEdits([raw, line('two')], [{
    lineId: 'one', target: 'component', targetId: 'face', targetName: 'Face', field: 'quantity_per_item', value: '3',
  }])
  assert.equal(result[0].components[0].quantity_per_item, 3)
  assert.equal(result[0].width_mm, '5000')
  assert.equal(result[0].estimate.costs[0].expected_amount, '10')
  assert.deepEqual(result[1], line('two'))
  assert.equal(raw.components[0].quantity_per_item, null)
})

test('an explicitly supplemented new component records only user-supplied facts', () => {
  const raw = line('one')
  const result = applySupplementEdits([raw], [{
    lineId: 'one', target: 'component', targetId: 'face', targetName: 'Face', field: 'quantity_per_item', value: '3',
  }])[0]
  assert.equal(result.components[0].quantity_per_item, 3)
  assert.equal(result.components[0].width_mm, null)
  assert.equal(result.width_mm, null)
  assert.match(result.text_evidence[0], /Face.*对象 face.*3/)
  assert.throws(() => applySupplementEdits([raw], [{ lineId: 'one', field: 'quantity_per_item', value: '3' }]))
})
