import type { Price } from './api'

export type Evidence = { file_id: number; page: number; text: string; sheet?: string; cell?: string; source?: 'image' | 'text' }
export const businessRuleVersion = '2026-09-23.v1'
export const businessTerms = '本报价不含物流、运输、现场安装、发票/税费及打包，上述费用由甲方另行列示。支架制作费不等于现场安装费。'
export const costKinds: Record<string, string> = {
  finished: '主体成品', material: '材料', process: '加工工序', power: '电源', bracket: '支架制作',
  outsourcing: '外加工', profit: '利润', other: '其他制作费用',
  logistics: '物流', transport: '运输', installation: '现场安装', invoice: '发票', packing: '打包', tax: '税费',
}
export const includedCostKinds = Object.fromEntries(Object.entries(costKinds).filter(([key]) =>
  !['logistics', 'transport', 'installation', 'invoice', 'packing', 'tax'].includes(key)))
export const rateUnits: Record<string, string> = {
  unknown: '待确认', cm: '元/厘米', m: '元/米', m2: '元/平方米',
  piece: '元/件', set: '元/套', fixed: '元/整项', ratio: '比例', hour: '元/小时',
}
export const pricingStrategies: Record<string, string> = {
  auto: '按方案选择', letters: '逐字计价', area: '面积计价', length: '长度计价',
  item: '件 / 套计价', composite: '部件组合', labor: '工序 / 工时',
}
export type PricingTarget = 'item' | 'letter_group' | 'component'
export const resolutionLabels: Record<string, string> = {
  priced: '已计价', priced_review: '已计价待审核', unresolved: '待计价',
  customer_confirmation: '待客户确认', administrator_pricing: '待管理员核价', execution_error: '系统执行异常',
}
export type LetterGroup = {
  label: string; count: number; width_mm: string | null; height_mm: string | null
  billing_length_mm: string | null; evidence: Evidence[]
  text_content?: string; count_basis?: 'characters' | 'repeated' | 'unknown'
}
export type MaterialComponent = {
  name: string; material: string; thickness_mm: string | null; width_mm: string | null; height_mm: string | null
  billing_length_mm?: string | null; area_m2?: string | null; labor_hours?: string | null
  quantity_per_item: number | null; geometry: string; processes: string[]; evidence: Evidence[]; uncertainties: string[]
}
export type ComponentAnalysis = {
  notice: string; missing: string[]
  components: {
    name: string; material: string; thickness_mm: string | null; candidate_count: number
    missing: string[]
    cost_per_item?: string | null; cost_formula?: string; estimate_conditions?: string[]
    references: { id: number; revision: number; amount: string; unit: string; process: string; notes: string
      source: { filename: string; sheet: string; cell: string } | null }[]
  }[]
}
export type Extra = { label: string; amount: string; basis: 'per_piece' | 'total'; reason: string; kind?: string }
export type EstimateAssumption = {
  field: string; value: string; reason: string
  target?: PricingTarget; target_index?: number | null; mode?: 'fill' | 'correct'
  previous_value?: string | null; evidence?: Evidence[]; path?: string
}
export type EstimateCost = {
  label: string; rate: string; quantity_formula: string; basis: 'per_piece' | 'total'
  rate_unit?: string; expected_amount?: string | null; measurement_note?: string
  source: 'agent_estimate' | 'catalog' | 'historical'
  reference_id?: number | null; reference_revision?: number | null; reason: string
  kind?: string; outsourcing_type?: string; minimum_area_scope?: string
  target?: PricingTarget; target_index?: number | null; included_kinds?: string[]
  adjusts_cost_index?: number | null; rate_basis_amount?: string | null; rate_basis_unit?: string | null
}
export type EstimatePlan = {
  assumptions: EstimateAssumption[]; costs: EstimateCost[]; scope: string
  contract_version?: 1 | 2; pricing_strategy?: string
}
export type DeferredIssue = {
  line_id: string; product: string; cause: 'customer_input' | 'catalog_gap' | 'execution_error'
  reason: string; question: string; missing_fields: string[]
}
export const deferredCauseLabels: Record<DeferredIssue['cause'], string> = {
  customer_input: '待客户确认', catalog_gap: '待管理员核价', execution_error: '系统执行异常',
}
export type Line = {
  business_rules_version?: string; pricing_family?: string; type_note?: string; difficulty?: string; difficulty_note?: string
  character_count?: number | null; letter_groups?: LetterGroup[]; area_m2?: string | null
  faces?: string; lighting?: string; installation_scene?: string; length_rounding?: string
  power_mode?: string; power_count?: number | null; power_adjustment?: number; power_note?: string
  source_item?: string; depth_mm?: string | null
  labor_hours?: string | null; components?: MaterialComponent[]
  dimension_evidence?: (Evidence & { field: string; value: string | null; unit: string
    target?: PricingTarget; target_index?: number | null; role?: string })[]
  pricing_category?: string; category_basis?: string
  id: string; product: string; text_content: string; material: string
  thickness_mm: string | null; width_mm: string | null; height_mm: string | null
  quantity: number | null; language: string; process: string; notes: string
  quantity_evidence?: (Evidence & { quantity: number | null; raw_quantity?: string; validation_status?: 'unchecked' | 'verified' | 'rejected'; validation_note?: string })[]
  source_measurements?: { value: string; unit: 'm' | 'm2'; evidence: Evidence[] }[]
  evidence: Evidence[]; text_evidence?: string[]; uncertainties: string[]; confirmed: boolean; billing_length_mm: string | null
  selected_price_id: number | null; selected_price_revision: number | null
  price_review_note: string; extras: Extra[]
  manual_unit_price?: string | null; manual_price_note?: string
  estimate?: EstimatePlan | null
}
export type CustomerFile = {
  id: number; filename: string; media_type: string; page_count: number; byte_size: number
}
export type QuoteLine = {
  line_id: string; product: string; blockers: string[]; amount: string | null
  warnings?: string[]
  unit_price?: string | null; unit_formula?: string; fixed_charges?: string
  estimate_conditions?: string[]
  estimated?: boolean; effective_requirement?: Line; estimate_scope?: string
  estimate_review?: (EstimateAssumption & { label: string; original: string | null; applied: boolean })[]
  estimate_costs?: (EstimateCost & { amount: string; measure: string; difficulty_multiplier?: string
    resolved_formula?: string; parameters?: Record<string, string | null>; reference?: Record<string, unknown> | null })[]
  formula: string; price: Price | null; requirement: Line
  component_analysis?: ComponentAnalysis
  case_references?: PricingCase[]
  deferred_issue?: DeferredIssue
  resolution?: string; pricing_strategy?: { key: string; label: string; version: string }
  diagnostics?: { code: string; cause: string; paths: string[]; message: string; action: string }[]
  review_signals?: { code: string; cost_index: number; message: string }[]
}
export type PricingCase = {
  id: number; revision: number; product: string; source_item: string; formula: string
  historical_quantity: number; historical_tax_rate: string; historical_unit_price: string | null
  historical_taxed_amount: string | null; notes: string; issues: string[]; notice: string
  source: { filename: string; sheet: string; cell: string } | null
}
export type Quote = {
  id: number; version: number; status: string; outdated: boolean; terms: string
  job_revision: number; approval_note: string; created_at: string
  payload: { lines: QuoteLine[]; known_subtotal: string; total: string | null; complete: boolean; currency: string; generated_by?: string; review_questions?: string[]
    business_rules?: { version: string; excluded_fees: string[]; unconfirmed: string[] }
    rounding?: string; unrounded_total?: string | null; rounding_adjustment?: string
    has_estimates?: boolean
    resolution_counts?: Record<string, number>; measurement_contract?: string
    review_items?: Record<string, unknown>[]
    confirmation_items?: { owner: string; question: string; cause?: DeferredIssue['cause']; lines: { line_id: string; product: string }[] }[] }
}
export function quoteDisplayTotal(payload: Quote['payload']): string | null {
  if (payload.complete) return payload.total
  return payload.lines.some(line => line.amount !== null) ? payload.known_subtotal : null
}
export type Job = {
  business_rules_version?: string
  pricing_progress?: { priced_count: number; line_count: number; known_subtotal: string; complete: boolean; saved_at: number } | null
  id: number; title: string; customer: string; brief: string; status: string; revision: number
  created_at: string; updated_at: string; file_count: number; line_count: number; pending_count: number
  requirements: Line[]; extraction: { summary?: string; questions?: string[] }
  files: CustomerFile[]; messages: { role: string; text: string; at: string; file_ids?: number[] }[]
  runs: { id: number; status: string; model: string; attempts: number; error: string; created_at: string; usage: Record<string, number> }[]
  quotes: Quote[]
  agent_runs: {
    id: number; status: string; message: string; created_at: string
    phase?: string; step_count?: number; active_product?: string
    usage: { [key: string]: unknown; total_tokens?: number; retry_at?: number;
      durable?: boolean; phase?: string; last_progress_at?: number; last_event_at?: number;
      deferred_requirements?: DeferredIssue[] }
    steps: { tool: string; arguments: Record<string, unknown>; result: Record<string, unknown>; at: string }[]
  }[]
}
export type JobProgress = Pick<Job, 'id' | 'revision' | 'status' | 'pricing_progress'> & {
  server_time: number
  run: (Job['agent_runs'][number] & { offset: number }) | null
}
export const jobLabels: Record<string, string> = {
  uploaded: '待整理', processing: '执行中', needs_review: '待确认', ready: '需求已确认', failed: '已中断', quoted: '报价待审核',
}
