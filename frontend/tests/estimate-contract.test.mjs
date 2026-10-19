import assert from 'node:assert/strict'
import test from 'node:test'
import { moveEstimateCost, removeEstimateCost, removePricingTarget, targetOptions } from '../src/estimate-contract.ts'

test('object selectors distinguish groups and components', () => {
  assert.deepEqual(targetOptions({ letter_groups: [{ label: 'A' }], components: [{ name: 'Panel' }] })
    .map(option => option.value), ['item:', 'letter_group:0', 'component:0'])
})

test('reordering preserves adjustment reference and prevents forward dependencies', () => {
  const plan = { costs: [{ label: 'base' }, { label: 'other' }, { label: 'adjustment', adjusts_cost_index: 0 }] }
  assert(moveEstimateCost(plan, 0, 1))
  assert.equal(plan.costs[2].adjusts_cost_index, 1)
  assert.equal(moveEstimateCost(plan, 2, -1), false)
  assert.deepEqual(plan.costs.map(cost => cost.label), ['other', 'base', 'adjustment'])
})

test('deleting costs rejects referenced bases and updates remaining references', () => {
  const plan = { costs: [{ label: 'other' }, { label: 'base' }, { label: 'adjustment', adjusts_cost_index: 1 }] }
  assert.equal(removeEstimateCost(plan, 1), false)
  assert(removeEstimateCost(plan, 0))
  assert.equal(plan.costs[1].adjusts_cost_index, 0)
})

test('deleting objects cannot silently redirect existing fees to another component', () => {
  const line = { components: [{ name: 'A' }, { name: 'B' }], estimate: {
    assumptions: [], costs: [{ target: 'component', target_index: 1 }],
  }, dimension_evidence: [{ target: 'component', target_index: 1 }] }
  assert.equal(removePricingTarget(line, 'component', 1), false)
  assert(removePricingTarget(line, 'component', 0))
  assert.equal(line.components[0].name, 'B')
  assert.equal(line.estimate.costs[0].target_index, 0)
  assert.equal(line.dimension_evidence[0].target_index, 0)
})

test('stable object references prevent deleting a referenced component without an index', () => {
  const line = { components: [{ name: 'A', object_id: 'a' }, { name: 'B', object_id: 'b' }],
    estimate: { assumptions: [], costs: [{ target: 'component', target_id: 'b' }] } }
  assert.equal(removePricingTarget(line, 'component', 1), false)
  assert(removePricingTarget(line, 'component', 0))
  assert.equal(line.estimate.costs[0].target_id, 'b')
  assert.equal(targetOptions(line)[1].object_id, 'b')
})

test('finished assembly coverage prevents deleting a covered component', () => {
  const line = { components: [{ name: 'Front', object_id: 'front' }, { name: 'Back', object_id: 'back' }],
    estimate: { assumptions: [], costs: [{ target: 'item', covers_component_ids: ['front'] }] } }
  assert.equal(removePricingTarget(line, 'component', 0), false)
  assert(removePricingTarget(line, 'component', 1))
  assert.equal(line.components.length, 1)
})
