import assert from 'node:assert/strict'
import test from 'node:test'
import { confirmationRows, questionParts } from '../src/quote-confirmations.ts'

const requirements = [
  { id: 'door', product: 'Door number', source_item: '12' },
  { id: 'arrow', product: 'Direction arrow', source_item: '13' },
]

test('structured confirmations keep all affected projects and original item numbers', () => {
  const rows = confirmationRows([{
    owner: 'Customer', question: 'Confirm width',
    lines: requirements.map(line => ({ line_id: line.id, product: line.product })),
  }], [], requirements)
  assert.equal(rows.length, 1)
  assert.deepEqual(rows[0].products.map(line => line.source_item), ['12', '13'])
  assert.equal(rows[0].owner, 'Customer')
})

test('legacy questions resolve exact project prefixes without truncating colon-containing content', () => {
  const rows = confirmationRows([], ['Door number:Confirm dimensions: 80 x 20 mm', 'Confirm delivery'], requirements)
  assert.equal(rows[0].products[0].line_id, 'door')
  assert.equal(rows[0].question, 'Confirm dimensions: 80 x 20 mm')
  assert.deepEqual(rows[1].products, [])
  assert.equal(rows[1].question, 'Confirm delivery')
})

test('matching legacy questions are deduplicated but additional facts and projects are preserved', () => {
  const rows = confirmationRows([{
    owner: 'Customer', question: 'Confirm width', lines: [{ line_id: 'door', product: 'Door number' }],
  }], ['Door number:Confirm width', 'Direction arrow:Confirm width', 'Door number:Confirm character count'], requirements)
  assert.equal(rows.length, 3)
  assert.equal(rows[1].products[0].line_id, 'arrow')
  assert.equal(rows[2].question, 'Confirm character count')
})

test('the longest project prefix is matched and unrelated colons remain in the question', () => {
  const rows = confirmationRows([], ['Door number:Confirm width', 'Dimensions: 80 x 20 mm'],
    [...requirements, { id: 'short', product: 'Door', source_item: '1' }])
  assert.equal(rows[0].products[0].line_id, 'door')
  assert.equal(rows[1].question, 'Dimensions: 80 x 20 mm')
  assert.deepEqual(rows[1].products, [])
})

test('deferred issues preserve reason and route execution/catalog questions to the administrator', () => {
  const issues = ['customer_input', 'catalog_gap', 'execution_error'].map(cause => ({
    line_id: 'door', product: 'Door number', cause, question: 'Confirm width',
    reason: 'Width is unreadable', missing_fields: ['width_mm'],
  }))
  const rows = confirmationRows([], [], requirements, issues)
  assert.equal(rows.length, 3)
  assert.notEqual(rows[0].owner, rows[1].owner)
  assert.equal(rows[1].owner, rows[2].owner)
  assert.equal(rows[0].reason, 'Width is unreadable')
  assert.deepEqual(rows.map(row => row.cause), ['customer_input', 'catalog_gap', 'execution_error'])
})

test('structured unresolved items keep diagnostic ownership and cause', () => {
  const rows = confirmationRows([{
    owner: 'Administrator', cause: 'execution_error', question: 'Repair measurement formula',
    lines: [{ line_id: 'door', product: 'Door number' }],
  }], ['Door number:Repair measurement formula'], requirements)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].cause, 'execution_error')
  assert.equal(rows[0].owner, 'Administrator')
})

test('numbered questions become readable lines without altering dimensions or quantities', () => {
  const original = 'Confirm: 1) width (80 x 2 mm); 2) height; 3) count (1 set of 6).'
  const parts = questionParts(original)
  assert.deepEqual(parts, ['Confirm:', '1) width (80 x 2 mm);', '2) height;', '3) count (1 set of 6).'])
  assert.equal(parts.join(' '), original)
  assert.deepEqual(questionParts('80 x 20 mm, 606-618; 1 set with 6 digits'), ['80 x 20 mm, 606-618; 1 set with 6 digits'])
})
