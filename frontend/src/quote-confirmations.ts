import type { DeferredIssue, Line, Quote } from './requirements-types'

type Item = NonNullable<Quote['payload']['confirmation_items']>[number]
type Product = { line_id: string; product: string; source_item: string }
export type ConfirmationRow = {
  products: Product[]
  question: string
  owner: string
  reason?: string
  cause?: DeferredIssue['cause']
}

export function confirmationRows(
  items: Item[] = [], questions: string[] = [], requirements: Line[] = [], issues: DeferredIssue[] = [],
): ConfirmationRow[] {
  const product = (id: string, name: string): Product => ({
    line_id: id, product: name,
    source_item: requirements.find(line => line.id === id)?.source_item || '',
  })
  const rows: ConfirmationRow[] = [
    ...items.map(item => ({
      products: item.lines.map(line => product(line.line_id, line.product)),
      question: item.question, owner: item.owner, cause: item.cause,
    })),
    ...issues.map(issue => ({
      products: [product(issue.line_id, issue.product)],
      question: issue.question || issue.reason,
      owner: issue.cause === 'customer_input' ? '需求方' : '报价管理员',
      reason: issue.question && issue.question !== issue.reason ? issue.reason : undefined,
      cause: issue.cause,
    })),
  ]
  const names = [...new Set([
    ...requirements.map(line => line.product),
    ...rows.flatMap(row => row.products.map(line => line.product)),
  ])].filter(Boolean).sort((a, b) => b.length - a.length)
  for (const raw of questions) {
    const name = names.find(name => raw.startsWith(`${name}：`) || raw.startsWith(`${name}:`))
    const question = (name ? raw.slice(name.length + 1) : raw).trim()
    if (!question || rows.some(row => row.question.trim() === question
        && (!name || row.products.some(line => line.product === name)))) continue
    const associated = name ? requirements.filter(line => line.product === name)
      .map(line => product(line.id, line.product)) : []
    rows.push({
      products: associated.length ? associated : name ? [product('', name)] : [],
      question, owner: '待确认',
    })
  }
  return rows
}

export function questionParts(question: string): string[] {
  // Only break list markers after separators; dimensions and parenthetical quantities stay intact.
  return question.replace(/([：:；;。，,\n]\s*)(?=[1-9]\d?[)）]\s*)/g, '$1\n')
    .split(/\n+/).map(part => part.trim()).filter(Boolean)
}
