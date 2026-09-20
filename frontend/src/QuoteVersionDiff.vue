<script setup lang="ts">
import { computed } from 'vue'
import type { Quote } from './requirements-types'
const props = defineProps<{ current: Quote; previous?: Quote }>()
const money = (value: string | null | undefined) => value == null ? '待计价'
  : `¥ ${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const fieldLabels: Record<string, string> = {
  quantity: '数量', width_mm: '宽度', height_mm: '高度', depth_mm: '深度',
  thickness_mm: '厚度', material: '材质', process: '工艺', billing_length_mm: '计价长度',
  product: '项目名称', pricing_category: '报价类别', text_content: '文字内容', language: '语言',
}
const changes = computed(() => {
  if (!props.previous) return []
  const before = new Map(props.previous.payload.lines.map(line => [line.line_id, line]))
  const rows: { id: string; product: string; before: string | null; after: string | null; reason: string }[] = []
  for (const line of props.current.payload.lines) {
    const old = before.get(line.line_id)
    before.delete(line.line_id)
    const fields = old ? Object.entries(fieldLabels).filter(([key]) =>
      JSON.stringify(old.requirement[key as keyof typeof old.requirement]) !==
      JSON.stringify(line.requirement[key as keyof typeof line.requirement])).map(([, label]) => label) : []
    if (old && JSON.stringify(old.requirement.estimate) !== JSON.stringify(line.requirement.estimate)) fields.push('估算方案')
    if (old && (old.requirement.manual_unit_price !== line.requirement.manual_unit_price
      || JSON.stringify(old.requirement.extras) !== JSON.stringify(line.requirement.extras))) fields.push('人工费用')
    if (!old || old.amount !== line.amount || fields.length) rows.push({
      id: line.line_id, product: line.product, before: old?.amount ?? null, after: line.amount,
      reason: !old ? '新增项目' : fields.length ? `已变更：${fields.join('、')}` : '价格或计价依据变化',
    })
  }
  for (const line of before.values()) rows.push({
    id: line.line_id, product: line.product, before: line.amount, after: null, reason: '移除项目',
  })
  return rows
})
</script>

<template>
  <section v-if="previous" class="quote-version-diff">
    <h3>相比 V{{ previous.version }}</h3>
    <div class="version-totals"><span>上版{{ previous.payload.complete ? '总额' : '小计' }}：{{ money(previous.payload.complete ? previous.payload.total : previous.payload.known_subtotal) }}</span><strong>本版{{ current.payload.complete ? '总额' : '小计' }}：{{ money(current.payload.complete ? current.payload.total : current.payload.known_subtotal) }}</strong></div>
    <details v-if="changes.length"><summary>{{ changes.length }} 项变化</summary>
      <div v-for="line in changes" :key="line.id" class="version-change"><div><strong>{{ line.product }}</strong><p>{{ line.reason }}</p></div><span>{{ money(line.before) }} → {{ money(line.after) }}</span></div>
    </details>
    <p v-else>项目金额与主要参数未变化</p>
  </section>
</template>

<style scoped>
.quote-version-diff { padding: 16px 0; border-block: 1px solid #dce4df; margin-bottom: 16px; font-size: 13px; overflow-wrap: anywhere; }
h3 { font-size: 14px; margin: 0 0 12px; }
.version-totals, .version-change { display: flex; gap: 12px; justify-content: space-between; flex-wrap: wrap; }
.version-totals { margin-bottom: 12px; }
summary { cursor: pointer; color: #426a59; }
.version-change { padding: 12px 0; border-bottom: 1px solid #edf0ee; align-items: center; }
p { margin: 5px 0 0; color: #68776f; font-size: 12px; }
.version-change > span { font-variant-numeric: tabular-nums; }
</style>
