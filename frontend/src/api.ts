export type Status = 'draft' | 'active' | 'inactive'
export type Source = { filename: string; sheet: string; row: number; cell: string; record_id: number }
export type Price = {
  id: number; product_id: number; product: string; category: string; category_id: number
  material: string; thickness_mm: string | null; spec: string; language: string
  process: string; quality: string; unit: string; amount: string | null; price_kind: string
  status: Status; notes: string; review_reason: string; revision: number; source: Source | null
  attributes?: Record<string, unknown>; updated_at: string
}
export type Rule = {
  id: number; product_id: number | null; product: string; name: string; content: string
  rule_type: string; status: Status; revision: number; source: Source | null; source_cell: string
}
export type Audit = {
  id: number; entity: string; entity_id: number; action: string; actor: string; reason: string
  before: Record<string, unknown> | null; after: Record<string, unknown> | null; created_at: string
}
export type Document = {
  id: number; filename: string; sha256: string; imported_at: string
  report: { prices: number; rules: number; source_rows: number; duplicate_candidates: number; sheets: string[]; warnings: string[] }
}
export type Options = {
  categories: { id: number; name: string }[]
  products: { id: number; name: string; category_id: number }[]
}
export type Stats = { prices: number; draft: number; active: number; inactive: number; products: number; rules: number; documents: number }
export const statusLabels: Record<string, string> = { draft: '待确认', active: '已启用', inactive: '已停用' }
export const unitLabels: Record<string, string> = { cm: '元 / cm', m: '元 / m', m2: '元 / ㎡', piece: '元 / 件', set: '元 / 套', unknown: '单位待确认' }
export const kindLabels: Record<string, string> = { standard: '标准价', suggested: '建议价', starting: '起价', reference: '参考价' }
export const languageLabels: Record<string, string> = { zh: '中文', en: '英文 / 数字', all: '通用', unknown: '待确认' }
export const ruleLabels: Record<string, string> = { reference: '参考说明', surcharge: '附加费用', constraint: '适用限制', formula: '计价公式', case: '项目案例' }

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), 'X-Admin-Token': sessionStorage.getItem('firefly-token') || '', ...init.headers },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('firefly-unauthorized'))
    const detail = Array.isArray(body.detail) ? body.detail.map((v: { msg: string }) => v.msg).join('；') : body.detail
    throw new ApiError(detail || '请求失败，请稍后重试', response.status)
  }
  return body
}

export async function apiBlob(path: string): Promise<Blob> {
  const response = await fetch(`/api${path}`, {
    headers: { 'X-Admin-Token': sessionStorage.getItem('firefly-token') || '' },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(body.detail || '文件读取失败', response.status)
  }
  return response.blob()
}
