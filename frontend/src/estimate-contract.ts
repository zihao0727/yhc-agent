import type { EstimatePlan, Line, PricingTarget } from './requirements-types'

export function targetOptions(line?: Line) {
  return [
    { value: 'item:', label: '整件', target: 'item' as PricingTarget, index: null, object_id: null },
    ...(line?.letter_groups || []).map((group, index) => ({
      value: `letter_group:${index}`, label: `字组 ${index + 1} · ${group.label || '未命名'}`,
      target: 'letter_group' as PricingTarget, index, object_id: group.object_id || null,
    })),
    ...(line?.components || []).map((component, index) => ({
      value: `component:${index}`, label: `部件 ${index + 1} · ${component.name || '未命名'}`,
      target: 'component' as PricingTarget, index, object_id: component.object_id || null,
    })),
  ]
}

export function moveEstimateCost(plan: EstimatePlan, index: number, direction: number): boolean {
  const target = index + direction
  if (target < 0 || target >= plan.costs.length) return false
  const old = [...plan.costs]
  const next = [...old]
  const [cost] = next.splice(index, 1)
  if (!cost) return false
  next.splice(target, 0, cost)
  const references = next.map(item => item.adjusts_cost_index == null
    ? null : next.indexOf(old[item.adjusts_cost_index]!))
  if (references.some((reference, i) => reference !== null && (reference < 0 || reference >= i))) return false
  next.forEach((item, i) => { if (item.adjusts_cost_index != null) item.adjusts_cost_index = references[i]! })
  plan.costs.splice(0, plan.costs.length, ...next)
  return true
}

export function removeEstimateCost(plan: EstimatePlan, index: number): boolean {
  if (plan.costs.some(cost => cost.adjusts_cost_index === index)) return false
  plan.costs.splice(index, 1)
  plan.costs.forEach(cost => {
    if (cost.adjusts_cost_index != null && cost.adjusts_cost_index > index) cost.adjusts_cost_index--
  })
  return true
}

export function removePricingTarget(line: Line, target: Exclude<PricingTarget, 'item'>, index: number): boolean {
  const references = [...(line.estimate?.assumptions || []), ...(line.estimate?.costs || [])]
  const collection = target === 'letter_group' ? line.letter_groups : line.components
  if (references.some(item => item.target === target && (item.target_index === index
    || item.target_id && item.target_id === collection?.[index]?.object_id))) return false
  if (target === 'component' && line.estimate?.costs.some(cost =>
    cost.covers_component_ids?.includes(collection?.[index]?.object_id || ''))) return false
  collection?.splice(index, 1)
  references.forEach(item => {
    if (item.target === target && item.target_index != null && item.target_index > index) item.target_index--
  })
  line.dimension_evidence = line.dimension_evidence?.filter(e => e.target !== target || e.target_index !== index)
  line.dimension_evidence?.forEach(e => {
    if (e.target === target && e.target_index != null && e.target_index > index) e.target_index--
  })
  return true
}
