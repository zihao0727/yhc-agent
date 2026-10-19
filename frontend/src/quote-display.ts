import type { Job, Line, QuoteLine } from './requirements-types.ts'
import { workspaceVersion } from './generated-quote-contract.ts'

export function currentWorkspace(job: Job) {
  const workspace = job.quote_workspace
  return workspace?.version === workspaceVersion && workspace.revision === job.revision
    && (workspace.run_id == null || workspace.run_id === job.agent_runs[0]?.id) ? workspace : null
}

export function sourcePreparationPending(job: Job) {
  return Boolean(job.extraction?.incomplete) && !job.agent_runs[0]?.usage.prepared
}

export function sourceStage(job: Job) {
  const run = job.agent_runs[0]
  const audit = job.extraction?.source_agent
  if (!sourcePreparationPending(job)) return null
  const progress = run?.usage.extraction_progress
  return {
    title: job.status === 'processing'
      ? (run?.usage.phase === 'source_review' ? 'AI正在复核资料' : 'AI正在阅读并比对资料') : '资料核对已暂停',
    message: run?.message || '已保存临时识别结果，尚未进入计价。',
    read: progress?.completed_pages ?? audit?.read_pages ?? 0,
    reviewed: progress?.reviewed_pages ?? audit?.reviewed_pages ?? 0,
    total: progress?.total_pages ?? audit?.total_pages ?? job.files?.reduce((n, f) => n + f.page_count, 0) ?? 0,
  }
}

export function sourceRemainder(job: Job) {
  const audit = job.extraction?.source_agent
  if (job.extraction?.incomplete || audit?.mode !== 'ai') return null
  const unread = audit.unread_pages?.length || 0
  const unreviewed = audit.unreviewed_pages?.length || 0
  return unread || unreviewed ? { unread, unreviewed } : null
}

export function liveQuoteLines(job: Job): QuoteLine[] {
  const workspace = currentWorkspace(job)
  if (workspace) {
    const requirements = new Map(job.requirements.map(raw => [raw.id, raw]))
    return workspace.lines.flatMap(row => {
      const raw = requirements.get(row.line_id)
      return raw ? [{ ...row, requirement: raw,
        effective_requirement: row.effective_requirement ? { ...raw, ...row.effective_requirement } : undefined }] : []
    })
  }
  const progress = job.pricing_progress
  const recognizing = sourcePreparationPending(job)
  const compatible = !recognizing && progress && (progress.revision == null || progress.revision === job.revision)
    && (progress.run_id == null || progress.run_id === job.agent_runs[0]?.id)
  const rows = new Map((compatible ? progress.lines : [])?.map(row => [row.line_id, row]) || [])
  const issues = new Map((job.agent_runs[0]?.usage.deferred_requirements || []).map(issue => [issue.line_id, issue]))
  return job.requirements.map(requirement => {
    const saved = rows.get(requirement.id)
    const issue = issues.get(requirement.id)
    const result: QuoteLine = {
      line_id: requirement.id, product: requirement.product, amount: null, unit_price: null,
      blockers: [], warnings: [], price: null, formula: '', resolution: 'unresolved', ...saved, requirement,
      effective_requirement: saved?.effective_requirement ? { ...requirement, ...saved.effective_requirement } : undefined,
      ...(saved?.amount == null && issue && !recognizing ? { deferred_issue: issue, resolution: {
        execution_error: 'execution_error', plan_repair: 'estimate_repair', service_unavailable: 'service_unavailable',
        recognition_review: 'recognition_review', customer_input: 'customer_confirmation', catalog_gap: 'administrator_pricing',
      }[issue.cause] } : {}),
    }
    return recognizing ? { ...result, resolution: 'recognition_review',
      blockers: ['资料仍在核对，尚未进入计价'] } : result
  })
}

export function sourceDescription(line: Line) {
  const text = line.evidence?.filter(evidence => evidence.source !== 'image').map(evidence => evidence.text)
    .find(text => /规格|尺寸|材质|厚度/.test(text))
  return text || line.notes || ''
}

export function lineSpecification(line: Line) {
  const size = line.width_mm && line.height_mm ? `${line.width_mm} × ${line.height_mm} mm`
    : line.area_m2 ? `面积 ${line.area_m2} m²`
      : line.billing_length_mm ? `计费长度 ${line.billing_length_mm} mm` : ''
  const material = [line.material, line.thickness_mm ? `厚 ${line.thickness_mm} mm` : ''].filter(Boolean).join(' · ')
  if (size) return [size, material].filter(Boolean).join(' · ')
  const parts = [...(line.components || []).map(part => ({ ...part, label: part.name })),
    ...(line.letter_groups || [])].filter(part => part.width_mm && part.height_mm)
  if (parts.length) {
    return parts.slice(0, 3).map(part => `${part.label}：${part.width_mm} × ${part.height_mm} mm`).join('；')
      + (parts.length > 3 ? `；另有 ${parts.length - 3} 个部件` : '')
  }
  const original = sourceDescription(line)
  const sizes = [...new Set([...original.matchAll(/规格尺寸\s*[:：]\s*([^\n,，;；]+)/g)].map(match => match[1]!.trim()))]
  if (sizes.length) return `原文规格：${sizes.join('；')}`
  const dimensions = [...new Set([...original.matchAll(/\d+(?:\.\d+)?\s*[*×xX]\s*\d+(?:\.\d+)?(?:\s*[*×xX]\s*\d+(?:\.\d+)?)?\s*(?:mm|毫米|cm|厘米|m|米)/g)].map(match => match[0]))]
  if (dimensions.length) return `原文规格：${dimensions.slice(0, 3).join('；')}`
  return material || '规格待确认'
}

export function quantityUnit(line: Line) {
  if (line.quantity_unit) return line.quantity_unit
  if (line.quantity == null && line.source_measurements?.length === 1) {
    return engineeringUnits[line.source_measurements[0]!.unit]!
  }
  return line.evidence?.find(evidence => /^(套|件|个|块|组|台|处|点|座|项|批|米|平方米|立方米|千克|小时)$/.test(evidence.text.trim()))?.text.trim() || '件'
}

const engineeringUnits: Record<string, string> = { m: '米', m2: '平方米', m3: '立方米', kg: '千克', hour: '小时' }

export function quoteQuantity(row: QuoteLine) {
  if (row.billing_quantity != null) return row.billing_quantity
  const line = row.effective_requirement || row.requirement
  return line.quantity ?? (line.source_measurements?.length === 1 ? line.source_measurements[0]!.value : null)
}

export function quoteQuantityUnit(row: QuoteLine) {
  return row.billing_unit ? engineeringUnits[row.billing_unit] || row.billing_unit
    : quantityUnit(row.effective_requirement || row.requirement)
}
