import type { Price } from './api'

export type Evidence = { file_id: number; page: number; text: string; sheet?: string; cell?: string; source_range?: string; source?: 'image' | 'text'; region?: number[] | null }
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
  unknown: '待确认', cm: '元/厘米', m: '元/米', m2: '元/平方米', m3: '元/立方米', kg: '元/千克',
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
  estimate_repair: '计价方案待修正', recognition_review: '资料识别待核实', service_unavailable: '服务暂不可用',
}
export const measurementLabels: Record<string, string> = {
  auto: '待选择', item_area: '整件面积', letter_area: '逐字面积', letter_length: '逐字长度',
  item_length: '整件 / 部件长度', component_area: '部件面积', component_count: '部件数量',
  item_count: '整件 / 套', labor_hours: '工时', power_units: '电源台数', fixed: '整项固定费用',
  subtotal_ratio: '小计利润比例', script: '自定义工程量', source_quantity: '原表工程量',
}
export const repairActionLabels: Record<string, string> = {
  repair_estimate: '修正计价方案', inspect_source: '核对原文及图纸', complete_target: '补全指定对象参数',
  read_target: '重新核对对象', choose_measurement: '选择计量口径', choose_strategy: '选择计价策略',
  refresh_reference: '重新核验价格依据', complete_power: '核实电源台数及费用',
  verify_rate_and_measurement: '核对费率、工程量和程序系数', submit_typed_value: '修正字段格式',
  repair_unit_and_formula: '核对单位与计量口径', use_generated_formula: '采用程序计量模板',
  find_basis_or_estimate: '补充价格依据或待审核估价', review_rate: '审核费率依据',
  contact_operations: '按诊断编号检查程序日志',
}
export type LetterGroup = {
  object_id?: string
  label: string; count: number; width_mm: string | null; height_mm: string | null
  billing_length_mm: string | null; evidence: Evidence[]
  text_content?: string; count_basis?: 'characters' | 'repeated' | 'unknown'
}
export type MaterialComponent = {
  object_id?: string
  name: string; material: string; thickness_mm: string | null; width_mm: string | null; height_mm: string | null
  billing_length_mm?: string | null; depth_mm?: string | null; area_m2?: string | null; labor_hours?: string | null
  quantity_per_item: number | null; geometry: string; processes: string[]; evidence: Evidence[]; uncertainties: string[]
  area_basis?: 'unknown' | 'rectangle' | 'net' | 'developed'
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
  target_id?: string | null
  previous_value?: string | null; evidence?: Evidence[]; path?: string
  basis?: 'source' | 'derived' | 'assumption'
}
export type EstimateCost = {
  process_codes?: string[]
  calculation_script?: string
  label: string; rate: string; quantity_formula: string; basis: 'per_piece' | 'total'
  rate_unit?: string; expected_amount?: string | null; measurement_note?: string
  source: 'agent_estimate' | 'catalog' | 'historical'
  reference_id?: number | null; reference_revision?: number | null; reason: string
  reference_part?: 'material_price' | 'process_1_price' | 'process_2_price' | null
  kind?: string; outsourcing_type?: string; minimum_area_scope?: string
  target?: PricingTarget; target_index?: number | null; included_kinds?: string[]; covers_component_ids?: string[]
  target_id?: string | null; measurement?: string
  source_measurement_index?: number | null
  adjusts_cost_index?: number | null; rate_basis_amount?: string | null; rate_basis_unit?: string | null
}
export type EstimatePlan = {
  assumptions: EstimateAssumption[]; costs: EstimateCost[]; scope: string
  contract_version?: 1 | 2 | 3; pricing_strategy?: string
  proposed_components?: MaterialComponent[]; proposed_letter_groups?: LetterGroup[]
}
export type DeferredIssue = {
  line_id: string; product: string; cause: 'customer_input' | 'catalog_gap' | 'execution_error' | 'plan_repair' | 'recognition_review' | 'service_unavailable'
  reason: string; question: string; missing_fields: string[]
  owner?: string; diagnostic_id?: string; repair_action?: string; repair_attempts?: number
  inputs?: PendingInput[]
}
export type PendingInput = {
  key: string; line_id: string; target: PricingTarget; target_id: string | null; target_name: string
  field: string; value: string | null; basis: 'original' | 'source' | 'derived' | 'assumption'; missing: boolean
}
export const deferredCauseLabels: Record<DeferredIssue['cause'], string> = {
  customer_input: '待客户确认', catalog_gap: '待管理员核价', execution_error: '系统执行异常',
  plan_repair: '计价方案待修正', recognition_review: '资料识别待核实', service_unavailable: '服务暂不可用',
}
export type Line = {
  production_requirements?: { target: 'item' | 'component'; target_id?: string | null
    process_code: string; evidence: Evidence[] }[]
  measurement_version?: 2 | 3
  business_rules_version?: string; pricing_family?: string; type_note?: string; difficulty?: string; difficulty_note?: string
  character_count?: number | null; letter_groups?: LetterGroup[]; area_m2?: string | null
  faces?: string; lighting?: string; installation_scene?: string; length_rounding?: string
  power_mode?: string; power_count?: number | null; power_adjustment?: number; power_note?: string
  source_item?: string; depth_mm?: string | null
  labor_hours?: string | null; components?: MaterialComponent[]
  dimension_evidence?: (Evidence & { field: string; value: string | null; unit: string
    target?: PricingTarget; target_index?: number | null; target_id?: string | null; role?: string })[]
  pricing_category?: string; category_basis?: string
  id: string; product: string; text_content: string; material: string
  thickness_mm: string | null; width_mm: string | null; height_mm: string | null
  quantity: number | null; language: string; process: string; notes: string
  quantity_unit?: string
  quantity_evidence?: (Evidence & { quantity: number | null; raw_quantity?: string; validation_status?: 'unchecked' | 'verified' | 'rejected'; validation_note?: string })[]
  source_measurements?: { value: string; unit: 'm' | 'm2' | 'm3' | 'kg' | 'hour'; evidence: Evidence[] }[]
  evidence: Evidence[]; text_evidence?: string[]; uncertainties: string[]; confirmed: boolean; billing_length_mm: string | null
  rate_evidence_required?: boolean
  selected_price_id: number | null; selected_price_revision: number | null
  price_review_note: string; extras: Extra[]
  manual_unit_price?: string | null; manual_price_note?: string
  estimate?: EstimatePlan | null
}
export type CustomerFile = {
  id: number; filename: string; media_type: string; page_count: number; byte_size: number
  parser_version?: number
  resource_summary?: { discovered: number; resolved: number; unresolved: number } | null
}
export type QuoteLine = {
  workflow?: { state: string; label: string; owner: string; valid: boolean; engine_version: string
    input_fingerprint: string; blocker?: (Partial<DeferredIssue> & { inputs?: PendingInput[] }) | null }
  production_coverage?: { version: string; complete: boolean; requirements: {
    target_id: string; process_code: string; label: string; covered: boolean; cost_indices: number[] }[] }
  line_id: string; product: string; blockers: string[]; amount: string | null
  warnings?: string[]
  unit_price?: string | null; unit_formula?: string; fixed_charges?: string
  billing_quantity?: string | null; billing_unit?: string
  estimate_conditions?: string[]
  estimated?: boolean; effective_requirement?: Line; estimate_scope?: string
  estimate_review?: (EstimateAssumption & { label: string; original: string | null; applied: boolean })[]
  estimate_costs?: (EstimateCost & { amount: string; measure: string; difficulty_multiplier?: string
    script_run?: { source: string; sha256: string; inputs: Record<string, string>; result: string;
      dependencies: string[]; trace: { variable: string; value: string }[]; checks: { message: string; passed: boolean }[] }
    resolved_formula?: string; parameters?: Record<string, string | null>; reference?: Record<string, unknown> | null })[]
  formula: string; price: Price | null; requirement: Line
  component_analysis?: ComponentAnalysis
  case_references?: PricingCase[]
  deferred_issue?: DeferredIssue
  resolution?: string; pricing_strategy?: { key: string; label: string; version: string }
  diagnostics?: { code: string; cause: string; paths: string[]; message: string; action: string; owner?: string }[]
  review_signals?: { code: string; cost_index: number; message: string }[]
}
export type SourceAgentAudit = {
  mode: 'ai'; phase: string; plan?: string; summary?: string
  total_pages?: number; read_pages?: number; reviewed_pages?: number
  unread_pages?: { file_id: number; page: number }[]
  unreviewed_pages?: { file_id: number; page: number }[]
  page_reviews?: { file_id: number; page: number; role: string; status: string; reason: string; questions: string[] }[]
  decisions?: { action: string; observation_ids: string[]; reason: string }[]
  notice?: string
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
    source_review?: { required: boolean; unresolved_regions: number; unresolved_links: number }
    resolution_counts?: Record<string, number>; measurement_contract?: string
    review_items?: Record<string, unknown>[]
    confirmation_items?: { owner: string; question: string; cause?: DeferredIssue['cause']; lines: { line_id: string; product: string }[] }[] }
}
export function quoteDisplayTotal(payload: Quote['payload']): string | null {
  if (payload.complete) return payload.total
  return payload.lines.some(line => line.amount !== null) ? payload.known_subtotal : null
}
export type Job = {
  quote_workspace?: { version: string; engine_version: string; revision: number; run_id: number | null
    preparing: boolean; state_counts: Record<string, number>
    lines: (Omit<QuoteLine, 'requirement' | 'effective_requirement'> & { effective_requirement?: Partial<Line> })[]
    pending: { key: string; kind: 'information' | 'pricing' | 'system'; cause: DeferredIssue['cause']
      owner: string; question: string; reason: string; fields: string[]; inputs: PendingInput[]
      products: { line_id: string; product: string; source_item: string }[] }[]
    summary: { count: number; priced: number; pending: number; amount: string | null; complete: boolean; outdated: boolean }
    actions: { can_revalidate: boolean; can_resume: boolean; auto_resume: boolean; can_disable_auto_resume: boolean } }
  business_rules_version?: string
  pricing_progress?: { priced_count: number; line_count: number; known_subtotal: string; complete: boolean; saved_at: number
    revision?: number; run_id?: number
    lines?: (Omit<QuoteLine, 'requirement' | 'effective_requirement'> & { effective_requirement?: Partial<Line> })[] } | null
  id: number; title: string; customer: string; brief: string; status: string; revision: number
  created_at: string; updated_at: string; file_count: number; line_count: number; pending_count: number
  requirements: Line[]; extraction: { summary?: string; questions?: string[]; incomplete?: boolean; source_agent?: SourceAgentAudit | null
    pricing_inventory?: { schedule_line_count: number; pending_observation_count: number
      pending_sources: { line_id: string; product: string; source_code: string
        sources: { file_id: number; page: number }[] }[] } }
  files: CustomerFile[]; messages: { role: string; text: string; at: string; file_ids?: number[]; pending_reply?: boolean }[]
  runs: { id: number; status: string; model: string; attempts: number; error: string; created_at: string; usage: Record<string, number> }[]
  quotes: Quote[]
  agent_runs: {
    id: number; status: string; message: string; created_at: string
    phase?: string; step_count?: number; active_product?: string
    usage: { [key: string]: unknown; total_tokens?: number; retry_at?: number;
      durable?: boolean; phase?: string; last_progress_at?: number; last_event_at?: number;
      prepared?: boolean; extraction_only?: boolean;
      extraction_progress?: { stage?: string; page?: number; completed_pages?: number; total_pages?: number;
        reviewed_pages?: number; extraction_route?: string;
        completed_units?: number; total_units?: number; unit?: string; detail?: string;
        completed_rows?: number; total_rows?: number; sheet?: string; row_start?: number; row_end?: number;
        saved_lines?: number; region_index?: number; total_regions?: number };
      deferred_requirements?: DeferredIssue[] }
    steps: { tool: string; arguments: Record<string, unknown>; result: Record<string, unknown>; at: string }[]
  }[]
}
export type JobProgress = Pick<Job, 'id' | 'revision' | 'status' | 'pricing_progress' | 'quote_workspace'> & {
  messages?: Job['messages']
  server_time: number
  run: (Job['agent_runs'][number] & { offset: number }) | null
}
export const jobLabels: Record<string, string> = {
  uploaded: '待整理', processing: '执行中', needs_review: '待确认', ready: '需求已确认', failed: '已中断', quoted: '报价待审核',
}
