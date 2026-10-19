import { confirmationRows, type ConfirmationRow } from './quote-confirmations.ts'
import { quoteDisplayTotal, type DeferredIssue, type Job, type Line, type PendingInput, type PricingTarget, type QuoteLine } from './requirements-types.ts'
import { currentWorkspace, liveQuoteLines, sourcePreparationPending } from './quote-display.ts'

export type PendingKind = 'information' | 'pricing' | 'system'
export type PendingRow = ConfirmationRow & { key: string; kind: PendingKind; fields: SupplementField[]; inputs: PendingInput[] }
export const supplementLabels = {
  quantity: '数量', width_mm: '单件宽度 (mm)', height_mm: '单件高度 (mm)',
  depth_mm: '单件深度 (mm)', thickness_mm: '厚度 (mm)', billing_length_mm: '单件计价长度 (mm)',
  material: '材质', process: '工艺', text_content: '文字内容',
  character_count: '每件字数', area_m2: '单件面积 (m²)', power_count: '电源台数',
  labor_hours: '工时', manual_unit_price: '成品单价 (元/件)', manual_price_note: '单价依据及包含范围',
  quantity_per_item: '每套该部件数量', count: '每件字符数量',
} as const
export type SupplementField = keyof typeof supplementLabels
export type SupplementEdit = { lineId: string; field: SupplementField; value: string
  target?: PricingTarget; targetId?: string; targetName?: string }
const numericFields = new Set<SupplementField>([
  'quantity', 'width_mm', 'height_mm', 'depth_mm', 'thickness_mm', 'billing_length_mm',
  'character_count', 'area_m2', 'power_count', 'labor_hours', 'manual_unit_price', 'quantity_per_item', 'count',
])
const integerFields = new Set<SupplementField>(['quantity', 'character_count', 'power_count', 'quantity_per_item', 'count'])
export const itemSupplementFields = Object.entries(supplementLabels).filter(([field]) => !['quantity_per_item', 'count'].includes(field))
export function isNumericField(field: SupplementField) { return numericFields.has(field) }
export function pendingKind(row: ConfirmationRow): PendingKind {
  if (/费用.+缺少单价依据/.test(`${row.question} ${row.reason || ''}`)) return 'pricing'
  if (['execution_error', 'plan_repair', 'service_unavailable', 'recognition_review'].includes(row.cause || '')) return 'system'
  if (row.cause === 'customer_input') return 'information'
  if (row.cause === 'catalog_gap' || /管理员|核价/.test(row.owner)) return 'pricing'
  return 'information'
}
export function humanizePricingMessage(text: string) {
  const labels: Record<string, string> = {
    billable_area_m2: '用于计费的面积（需要对应部件的宽、高和数量）', area_m2: '面积（平方米）',
    quantity_per_item: '每套该部件数量', component_count: '每套该部件数量',
    width_mm: '宽度（毫米）', height_mm: '高度（毫米）', depth_mm: '深度（毫米）',
    thickness_mm: '厚度（毫米）', billing_length_mm: '用于计费的长度（毫米）',
    letter_cm: '每个字用于计费的长度', power_units: '电源台数', power_count: '电源台数',
    character_count: '每件字符数量', labor_hours: '加工工时', catalog_id: '库内价格',
    rate_unit: '单价单位', expected_amount: '费用核对金额', quantity_formula: '用量计算方式',
    measurement_note: '用量依据', rate_basis_amount: '价格依据金额', rate_basis_unit: '价格依据单位', rate: '单价',
  }
  let result = text.replace(/^(?:连续操作未推进计价|相同计价校验多次未通过|单项处理时间达到上限)[；;]\s*/, '')
  const fee = result.match(/费用(.+?)缺少单价依据/)
  if (fee) return `请补充“${fee[1]}”的单价、计费单位，以及价格来源或估价依据。`
  for (const [key, value] of Object.entries(labels)) result = result.replace(new RegExp(`\\b${key}\\b`, 'g'), value)
  return result
}
export function inputLabel(input: PendingInput) {
  if (input.field === 'width_mm') return input.target === 'letter_group' ? '每个字的宽度 (mm)' : input.target === 'component' ? '部件宽度 (mm)' : supplementLabels.width_mm
  if (input.field === 'height_mm') return input.target === 'letter_group' ? '每个字的高度 (mm)' : input.target === 'component' ? '部件高度 (mm)' : supplementLabels.height_mm
  return supplementLabels[input.field as SupplementField] || humanizePricingMessage(input.field)
}
export function compactObjectName(name: string) {
  return name.replace(/[（(][^（）()]*[）)]/g, '').replace(/\d+(?:\.\d+)?\s*[×*xX]\s*\d+(?:\.\d+)?(?:\s*mm)?/g, '')
    .replace(/^[、，\s]+|[、，\s]+$/g, '').trim() || name
}
export function concisePricingMessage(text: string) {
  const readable = humanizePricingMessage(text)
  if (/validation errors?\b|decimal_max_places|Traceback/i.test(text)) {
    return pendingPresentation({ question: text, products: [], owner: '' }).question
  }
  if (/原表分项工艺为.+没有对应工艺依据/.test(readable)) {
    const process = readable.match(/原表分项工艺为[“"](.+?)[”"]/)
    return `需核对${process ? `“${process[1]}”的` : ''}工艺做法和单价依据`
  }
  if (/报价类别尚未匹配/.test(readable)) return '需核对报价类别与对应的成本资料'
  if (/文字类型不匹配或未确认/.test(readable)) return '需确认文字类型和对应做法'
  if (/^尚未选择价格$/.test(readable)) return '尚未确定单价'
  if (/^缺少每件计价长度/.test(readable)) return '请补充每件用于计费的长度（毫米）'
  if (/^工艺及尺寸原文已保留/.test(readable)) return '尺寸和工艺以原始资料为准'
  if (/^本报价不含物流/.test(readable)) return '不含物流、运输、现场安装、税费及打包；支架制作费不含现场安装'
  if (/^工艺难度未标注/.test(readable)) return '工艺难度未确认，暂按基础加工费计算'
  if (/^字类\/材料造型分类尚未明确/.test(readable)) return '文字或造型类别待确认，需核对适用报价表'
  return readable
}
export function pendingSummary(row: PendingRow) {
  const missing = row.inputs.filter(input => input.missing)
  if (missing.length) {
    const fields = [...new Set(missing.map(input => inputLabel(input)))]
    const objects = [...new Set(missing.filter(input => input.target !== 'item').map(input => compactObjectName(input.target_name)))]
    return `${objects.length === 1 ? `${objects[0]} · ` : ''}待补充：${fields.join('、')}`
  }
  return concisePricingMessage(pendingPresentation(row).question)
}
export function linePresentation(line: QuoteLine, issue?: DeferredIssue) {
  const current = issue || line.deferred_issue
  const inputs = current?.inputs?.filter(input => input.line_id === line.line_id) || []
  const missing = inputs.filter(input => input.missing)
  const primary = current?.question || current?.reason
  const messages = [...new Set([...(primary ? [primary] : []), ...line.blockers])]
    .filter(message => message !== '尚未选择价格' || !primary && line.blockers.length === 1)
  const actionable = missing.length
    ? messages.filter(message => !/计量对象缺少参数|billable_area_m2|\/parameters\//.test(message))
    : messages
  const grouped = new Map<string, { name: string; fields: string[] }>()
  for (const input of missing) {
    const key = `${input.target}:${input.target_id || 'item'}`
    const group = grouped.get(key) || { name: input.target === 'item' ? '项目资料' : compactObjectName(input.target_name), fields: [] }
    if (!group.fields.includes(inputLabel(input))) group.fields.push(inputLabel(input))
    grouped.set(key, group)
  }
  const items = [...grouped.values()].map(group => ({ label: group.name, text: `请补充${group.fields.join('、')}` }))
  items.push(...[...new Set(actionable.map(concisePricingMessage))].filter(Boolean).map(text => ({ label: '', text })))
  const kind = current ? pendingKind({ question: primary || '', cause: current.cause, owner: current.owner || '', products: [] })
    : ['execution_error', 'estimate_repair', 'service_unavailable'].includes(line.resolution || '') ? 'system' : 'information'
  const notes = [...new Set([...(line.warnings || []), ...(line.estimate_conditions || [])].map(concisePricingMessage))]
  return { items, title: kind === 'system' ? '需要系统处理' : kind === 'pricing' ? '还需确认价格' : '还需补充', notes }
}
export function pendingPresentation(row: ConfirmationRow & { inputs?: PendingInput[] }) {
  const technical = [...new Set([row.question, row.reason].filter((value): value is string => Boolean(value)))].join('\n')
  const missing = row.inputs?.filter(input => input.missing) || []
  if (missing.length) {
    const groups = new Map<string, { input: PendingInput; fields: string[] }>()
    for (const input of missing) {
      const key = `${input.line_id}:${input.target}:${input.target_id || 'item'}`
      const group = groups.get(key) || { input, fields: [] }
      if (!group.fields.includes(inputLabel(input))) group.fields.push(inputLabel(input))
      groups.set(key, group)
    }
    const names = [...groups.values()].map(({ input, fields }) => {
      const product = row.products.find(product => product.line_id === input.line_id)
      const project = row.products.length > 1 && product
        ? `${product.source_item ? `#${product.source_item} ` : ''}${product.product} · ` : ''
      const target = input.target === 'item' ? '' : `“${input.target_name}”的`
      return `${project}${target}${fields.join('、')}`
    })
    return { question: names.length === 1 ? `请确认${names[0]}。` : `请补充以下资料：\n${names.join('\n')}`,
      reason: '这些资料尚未明确，相关项目暂未计入报价。', technical }
  }
  if (!/validation errors?\b|decimal_max_places|Traceback \(most recent call last\)/i.test(technical)) {
    const question = humanizePricingMessage(row.question)
    const reason = row.reason ? humanizePricingMessage(row.reason) : undefined
    return { question, reason, technical: question !== row.question || reason !== row.reason ? technical : '' }
  }
  return {
    question: /decimal_max_places|decimal places/i.test(technical)
      ? '费用单价或计量值的小数精度超出限制，需要重新校验'
      : '计价方案未通过数据校验，需要修正后重试',
    reason: '处理方需核对计算方案和数据格式。处理后继续报价；已完成的项目会保留。',
    technical,
  }
}
export function pendingRows(job: Job): PendingRow[] {
  const workspace = currentWorkspace(job)
  if (workspace) return workspace.pending.map(row => ({ ...row,
    fields: row.fields.filter((field): field is SupplementField => Object.hasOwn(supplementLabels, field)) }))
  if (sourcePreparationPending(job)) return []
  const quote = job.quotes[0]
  if (quote?.status === 'approved' && !quote.outdated) return []
  const resolved = new Set([
    ...(!quote?.outdated ? quote?.payload.lines.filter(line => line.amount !== null).map(line => line.line_id) || [] : []),
    ...liveQuoteLines(job).filter(line => line.amount !== null).map(line => line.line_id),
  ])
  const issues = [
    ...(job.agent_runs[0]?.usage.deferred_requirements || []).filter(issue => !resolved.has(issue.line_id)),
    ...(quote?.payload.lines.flatMap(line => line.deferred_issue && !resolved.has(line.line_id) ? [line.deferred_issue] : []) || []),
  ]
  const rows = confirmationRows(quote?.payload.confirmation_items, quote?.payload.review_questions,
    job.requirements, issues)
  if (job.status === 'processing') {
    for (let index = rows.length - 1; index >= 0; index--) {
      const row = rows[index]!
      if (!row.products.length) continue
      row.products = row.products.filter(product => !resolved.has(product.line_id))
      if (!row.products.length) rows.splice(index, 1)
    }
  }
  for (const line of quote?.payload.lines || []) {
    if (line.amount !== null || resolved.has(line.line_id) || rows.some(row => row.products.some(p => p.line_id === line.line_id))) continue
    rows.push({
      products: [{ line_id: line.line_id, product: line.product, source_item: line.requirement.source_item || '' }],
      question: line.blockers.join('；') || '请补充计价依据', owner: '待确认',
      cause: line.deferred_issue?.cause,
    })
  }
  const unique = new Map<string, PendingRow>()
  for (const row of rows.sort((a, b) => b.products.length - a.products.length)) {
    const ids = row.products.map(p => p.line_id).sort()
    const associated = issues.filter(issue => ids.includes(issue.line_id)
      && (!row.cause || issue.cause === row.cause)
      && [issue.question, issue.reason].some(text => text?.trim() === row.question.trim()))
    const cause = row.cause || associated[0]?.cause
    const kind = pendingKind({ ...row, cause })
    const key = JSON.stringify([ids, row.question.trim(), cause || kind])
    const fields = [...new Set(associated
      .flatMap(issue => issue.missing_fields))].filter((field): field is SupplementField =>
        Object.hasOwn(supplementLabels, field))
    const inputs = [...new Map(associated
      .flatMap(issue => issue.inputs || []).map(input => [`${input.line_id}:${input.key}`, input])).values()]
    fields.push(...inputs.filter(input => input.target === 'item' && Object.hasOwn(supplementLabels, input.field))
      .map(input => input.field as SupplementField).filter(field => !fields.includes(field)))
    const group = [...unique.values()].find(existing => existing.question.trim() === row.question.trim()
      && existing.kind === kind && existing.cause === cause && ids.length > 0
      && ids.every(id => existing.products.some(product => product.line_id === id)))
    if (group) {
      if (!group.reason && row.reason) group.reason = row.reason
      group.fields = [...new Set([...group.fields, ...fields])]
      group.inputs = [...new Map([...group.inputs, ...inputs].map(input => [`${input.line_id}:${input.key}`, input])).values()]
      if (!group.cause && row.cause) group.cause = row.cause
      continue
    }
    const existing = unique.get(key)
    unique.set(key, { ...existing, ...row, cause,
      key, kind, fields, inputs })
  }
  return [...unique.values()]
}
export function lineNeedsReview(line: QuoteLine) {
  return line.amount !== null && Boolean(line.estimated || line.warnings?.length
    || line.review_signals?.length || line.estimate_conditions?.length || line.resolution === 'priced_review')
}
export function resultStatus(line: QuoteLine, approved = false) {
  if (line.workflow && !approved) return line.workflow.label
  if (line.amount === null) {
    if (line.deferred_issue && pendingKind({
      question: line.deferred_issue.question || line.deferred_issue.reason,
      cause: line.deferred_issue.cause, owner: line.deferred_issue.owner || '', products: [],
    }) === 'pricing') return '待核价'
    if (line.resolution === 'unresolved') return '待计价'
    if (line.resolution === 'recognition_review') return '待核对'
    if (['execution_error', 'estimate_repair', 'service_unavailable'].includes(line.resolution || '')) return '系统待处理'
    if (line.resolution === 'administrator_pricing') return '待核价'
    return '待补充'
  }
  return approved ? '已确认' : lineNeedsReview(line) ? '估价待审核' : '已计价'
}
export function quoteSummary(job: Job) {
  const workspace = currentWorkspace(job)
  if (workspace) return workspace.summary
  if (sourcePreparationPending(job)) {
    const count = job.line_count ?? job.requirements.length
    return { count, priced: 0, pending: count, outdated: Boolean(job.quotes[0]?.outdated),
      amount: null, complete: false }
  }
  const quote = job.quotes[0]
  const progress = job.status === 'processing' || !quote ? job.pricing_progress : null
  const count = progress?.line_count ?? Math.max(job.line_count || 0, job.requirements.length,
    quote?.outdated ? 0 : quote?.payload.lines.length || 0)
  const priced = progress?.priced_count ?? quote?.payload.lines.filter(line => line.amount !== null).length ?? 0
  return { count, priced, pending: Math.max(0, count - priced), outdated: Boolean(quote?.outdated),
    amount: progress ? progress.priced_count ? progress.known_subtotal : null
      : quote ? quoteDisplayTotal(quote.payload) : null,
    complete: !progress && Boolean(quote?.payload.complete) }
}

export function applySupplementEdits(requirements: Line[], edits: SupplementEdit[]): Line[] {
  const lines = JSON.parse(JSON.stringify(requirements)) as Line[]
  const byId = new Map(lines.map(line => [line.id, line]))
  const seen = new Map<string, string>()
  const touched = new Set<string>()
  // Apply supplied area and finished prices after changes that invalidate their old values.
  const priority = (field: SupplementField) => field === 'area_m2' ? 1 : field === 'manual_unit_price' ? 2 : field === 'manual_price_note' ? 3 : 0
  for (const edit of [...edits].sort((a, b) => priority(a.field) - priority(b.field))) {
    const line = byId.get(edit.lineId)
    if (!line || !Object.hasOwn(supplementLabels, edit.field)) throw new Error('补充项目或字段已变化，请重新核对')
    const value = edit.value.trim()
    if (!value) continue
    const targetKind = edit.target || 'item'
    const key = `${edit.lineId}:${targetKind}:${edit.targetId || ''}:${edit.field}`
    if (seen.has(key) && seen.get(key) !== value) throw new Error(`${line.product}的${supplementLabels[edit.field]}填写不一致`)
    seen.set(key, value)
    if (numericFields.has(edit.field)) {
      if (!/^\d+(?:\.\d+)?$/.test(value) || !Number.isFinite(Number(value))
        || Number(value) < 0 || (Number(value) === 0 && !['power_count', 'manual_unit_price'].includes(edit.field))
        || (integerFields.has(edit.field) && (!Number.isInteger(Number(value)) || Number(value) > 1000000))) {
        throw new Error(`${line.product}的${supplementLabels[edit.field]}格式无效`)
      }
      if (['count', 'quantity_per_item', 'character_count'].includes(edit.field) && Number(value) > 10000) {
        throw new Error(`${line.product}的${supplementLabels[edit.field]}不能超过 10000`)
      }
    }
    let record = line as unknown as Record<string, unknown>
    if (targetKind !== 'item') {
      const allowed = targetKind === 'component'
        ? ['width_mm', 'height_mm', 'thickness_mm', 'billing_length_mm', 'material', 'quantity_per_item', 'area_m2', 'labor_hours']
        : ['width_mm', 'height_mm', 'billing_length_mm', 'count', 'text_content']
      if (!edit.targetId || !/^[a-zA-Z0-9_-]{1,50}$/.test(edit.targetId) || !allowed.includes(edit.field)) {
        throw new Error('补充对象或字段无效，请核对部件名称')
      }
      const kind = targetKind === 'component' ? 'components' : 'letter_groups'
      const objects = [...(line[kind] || []), ...(line.estimate?.[`proposed_${kind}`] || [])]
      let object = objects.find(obj => obj.object_id === edit.targetId)
      if (!object) {
        if (!edit.targetName?.trim()) throw new Error('补充对象已变化，请重新核对')
        const common = { object_id: edit.targetId, width_mm: null, height_mm: null, billing_length_mm: null, evidence: [] }
        if (kind === 'components') {
          object = { ...common, name: edit.targetName, material: '', thickness_mm: null, quantity_per_item: null,
            geometry: 'unknown', processes: [], uncertainties: [] }
          ;(line.components ||= []).push(object)
        } else {
          const count = edits.find(other => other.lineId === edit.lineId && other.targetId === edit.targetId && other.field === 'count')
          if (!count?.value || !/^[1-9]\d*$/.test(count.value)) throw new Error(`请同时确认“${edit.targetName}”的每件字符数量`)
          object = { ...common, label: edit.targetName, count: Number(count.value) }
          ;(line.letter_groups ||= []).push(object)
        }
      }
      record = object as unknown as Record<string, unknown>
    } else if (edit.targetId || ['count', 'quantity_per_item'].includes(edit.field)) {
      throw new Error('部件数量不能写入整件项目，请选择对应部件')
    }
    record[edit.field] = integerFields.has(edit.field) ? Number(value) : value
    line.confirmed = false
    touched.add(line.id)
    if (targetKind !== 'letter_group' && (edit.field === 'width_mm' || edit.field === 'height_mm')) record.area_m2 = null
    if (line.estimate) {
      line.estimate.assumptions = line.estimate.assumptions.filter(item =>
        !(item.field === edit.field && (item.target || 'item') === targetKind
          && (targetKind === 'item' || item.target_id === edit.targetId)))
      if (edit.field !== 'quantity' && (line.estimate.contract_version || 1) >= 3) {
        line.estimate.costs.forEach(cost => { cost.expected_amount = null })
      }
    }
    if (['material', 'process', 'thickness_mm'].includes(edit.field)) {
      line.selected_price_id = null; line.selected_price_revision = null
      if (line.estimate) {
        line.components = [...(line.components || []), ...(line.estimate.proposed_components || [])]
        line.letter_groups = [...(line.letter_groups || []), ...(line.estimate.proposed_letter_groups || [])]
      }
      line.estimate = null; line.manual_unit_price = null; line.manual_price_note = ''
    }
    const prefix = `用户补充${targetKind === 'item' ? '' : `${edit.targetName || edit.targetId}（对象 ${edit.targetId}）的`}${supplementLabels[edit.field]}：`
    const note = `${prefix}${value}`.slice(0, 1500)
    const evidence = (line.text_evidence || []).filter(text => !text.startsWith(prefix))
    if (evidence.length >= 20) throw new Error(`${line.product}的补充来源记录已满，请在完整编辑中核对来源`)
    line.text_evidence = [...evidence, note]
  }
  for (const id of touched) {
    const line = byId.get(id)!
    if (line.manual_unit_price != null && !line.manual_price_note?.trim()) {
      throw new Error(`${line.product}需要填写单价依据及包含范围`)
    }
  }
  return lines
}
