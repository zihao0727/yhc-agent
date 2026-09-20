import type { Price } from './api'

export type Evidence = { file_id: number; page: number; text: string }
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
export type Extra = { label: string; amount: string; basis: 'per_piece' | 'total'; reason: string }
export type EstimateAssumption = {
  field: string; value: string; reason: string
}
export type EstimateCost = {
  label: string; rate: string; quantity_formula: string; basis: 'per_piece' | 'total'
  source: 'agent_estimate' | 'catalog' | 'historical'
  reference_id?: number | null; reference_revision?: number | null; reason: string
}
export type EstimatePlan = { assumptions: EstimateAssumption[]; costs: EstimateCost[]; scope: string }
export type Line = {
  source_item?: string; depth_mm?: string | null
  pricing_category?: string; category_basis?: string
  id: string; product: string; text_content: string; material: string
  thickness_mm: string | null; width_mm: string | null; height_mm: string | null
  quantity: number | null; language: string; process: string; notes: string
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
  estimate_costs?: (EstimateCost & { amount: string; measure: string; reference?: Record<string, unknown> | null })[]
  formula: string; price: Price | null; requirement: Line
  component_analysis?: ComponentAnalysis
  case_references?: PricingCase[]
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
    has_estimates?: boolean
    review_items?: Record<string, unknown>[]
    confirmation_items?: { owner: string; question: string; lines: { line_id: string; product: string }[] }[] }
}
export function quoteDisplayTotal(payload: Quote['payload']): string | null {
  if (payload.complete) return payload.total
  return payload.lines.some(line => line.amount !== null) ? payload.known_subtotal : null
}
export type Job = {
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
      durable?: boolean; phase?: string; last_progress_at?: number; last_event_at?: number }
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
