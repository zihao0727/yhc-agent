<script setup lang="ts">
import { Pencil } from 'lucide-vue-next'
import type { QuoteLine } from './requirements-types'
defineProps<{ lines: QuoteLine[]; disabled?: boolean }>()
defineEmits<{ edit: [lineId: string] }>()
const sources = { catalog: '报价库引用', historical: '历史案例引用', agent_estimate: '方案估价（非确认价格）' }
const money = (v: string) => Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
</script>

<template>
  <section v-if="lines.some(line => line.estimated)" class="estimate-review">
    <h3>自动补全与费用审核清单</h3>
    <article v-for="line in lines.filter(item => item.estimated)" :key="line.line_id">
      <header><strong>{{ line.product }}</strong><el-button :disabled="disabled" @click="$emit('edit', line.line_id)"><Pencil :size="14" />修改并重新计价</el-button></header>
      <dl v-for="(item, index) in line.estimate_review || []" :key="index">
        <dt>{{ item.label }} · {{ item.applied ? '暂定' : '沿用原始需求' }}</dt>
        <dd>{{ item.applied ? item.value : item.original }}<span v-if="!item.applied">（未采用建议值 {{ item.value }}）</span></dd>
        <dd class="muted">{{ item.reason }}</dd>
      </dl>
      <div v-for="(cost, index) in line.estimate_costs || []" :key="index" class="estimate-cost">
        <div><strong>{{ cost.label }}</strong><span>¥ {{ money(cost.amount) }} / {{ cost.basis === 'per_piece' ? '件' : '整单' }}</span></div>
        <p>{{ sources[cost.source] }}<span v-if="cost.reference_id"> · #{{ cost.reference_id }} v{{ cost.reference_revision }}</span></p>
        <p>{{ cost.rate }} × ({{ cost.quantity_formula.slice(1) }}) · {{ cost.reason }}</p>
        <details v-if="cost.reference"><summary>引用价格原始条件</summary><pre>{{ JSON.stringify(cost.reference, null, 2) }}</pre></details>
      </div>
      <p class="scope"><strong>报价范围：</strong>{{ line.estimate_scope }}</p>
      <p v-if="line.formula">计价：{{ line.formula }}</p>
    </article>
  </section>
</template>

<style scoped>
.estimate-review { border-top: 1px solid #dce4df; padding: 16px 0; min-width: 0; overflow-wrap: anywhere; }
h3 { font-size: 15px; margin: 0 0 12px; }
article { border-bottom: 1px solid #dce4df; padding: 12px 0; }
header, .estimate-cost > div { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
header strong { font-size: 14px; }
dl { margin: 12px 0; font-size: 12px; }
dt { font-weight: 600; }
dd { margin: 4px 0; }
p { font-size: 12px; margin: 6px 0; white-space: normal; }
.muted { color: #647168; }
.estimate-cost { border-top: 1px solid #edf0ee; padding: 10px 0; font-size: 12px; }
.scope { padding-top: 8px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
summary { cursor: pointer; font-size: 12px; }
</style>
