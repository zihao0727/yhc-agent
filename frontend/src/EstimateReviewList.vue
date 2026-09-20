<script setup lang="ts">
import { Pencil } from 'lucide-vue-next'
import { computed, ref } from 'vue'
import type { QuoteLine } from './requirements-types'
const props = defineProps<{ lines: QuoteLine[]; disabled?: boolean }>()
defineEmits<{ edit: [lineId: string] }>()
const filter = ref('all')
const search = ref('')
const filters = [
  { value: 'all', label: '全部' }, { value: 'agent', label: '模型估价' },
  { value: 'quantity', label: '暂定数量' }, { value: 'issues', label: '存在疑问' },
  { value: 'reference', label: '引用价格' },
]
const visibleLines = computed(() => props.lines.filter(line => {
  if (!line.estimated && !line.blockers.length && !line.warnings?.length) return false
  if (search.value && !line.product.includes(search.value)) return false
  if (filter.value === 'agent') return line.estimate_costs?.some(cost => cost.source === 'agent_estimate')
  if (filter.value === 'quantity') return line.estimate_review?.some(item => item.field === 'quantity' && item.applied)
  if (filter.value === 'issues') return line.blockers.length || line.warnings?.length
  if (filter.value === 'reference') return line.estimate_costs?.some(cost => cost.source !== 'agent_estimate')
  return true
}))
const sources = { catalog: '报价库引用', historical: '历史案例引用', agent_estimate: '方案估价（非确认价格）' }
const money = (v: string) => Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
</script>

<template>
  <section v-if="lines.some(line => line.estimated || line.blockers.length || line.warnings?.length)" class="estimate-review">
    <h3>自动补全与费用审核清单</h3>
    <div class="review-filters">
      <el-select v-model="filter" aria-label="审核风险筛选"><el-option v-for="item in filters" :key="item.value" :value="item.value" :label="item.label" /></el-select>
      <el-input v-model="search" clearable placeholder="搜索项目" aria-label="搜索审核项目" />
      <span>{{ visibleLines.length }} 项</span>
    </div>
    <p v-if="!visibleLines.length">没有符合条件的项目</p>
    <details v-for="line in visibleLines" :key="line.line_id" class="review-item">
      <summary><span>{{ line.requirement.source_item ? `#${line.requirement.source_item} · ` : '' }}{{ line.product }}</span><small>{{ line.estimate_review?.filter(item => item.applied).length || 0 }} 项暂定参数</small><strong>{{ line.amount === null ? '待计价' : `¥ ${money(line.amount)}` }}</strong></summary>
      <header><strong>原始需求与计价依据</strong><el-button :disabled="disabled" @click="$emit('edit', line.line_id)"><Pencil :size="14" />修改并重新计价</el-button></header>
      <p v-for="issue in [...line.blockers, ...(line.warnings || [])]" :key="issue" class="review-issue">{{ issue }}</p>
      <dl v-for="(item, index) in line.estimate_review || []" :key="index">
        <dt>{{ item.label }} · {{ item.applied ? '暂定' : '沿用原始需求' }}</dt>
        <dd class="review-values"><span>原始：{{ item.original ?? '未提供' }}</span><span>计价采用：{{ item.applied ? item.value : item.original }}</span></dd>
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
    </details>
  </section>
</template>

<style scoped>
.estimate-review { border-top: 1px solid #dce4df; padding: 16px 0; min-width: 0; overflow-wrap: anywhere; }
h3 { font-size: 15px; margin: 0 0 12px; }
article { border-bottom: 1px solid #dce4df; padding: 12px 0; }
.review-filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.review-filters .el-select { width: 145px; }
.review-filters .el-input { width: 190px; max-width: 100%; }
.review-filters > span { font-size: 12px; color: #647168; }
.review-item { border-bottom: 1px solid #dce4df; padding: 12px 0; }
.review-item > summary { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; list-style: none; }
.review-item > summary::before { content: "+"; width: 12px; color: #63756b; }
.review-item[open] > summary::before { content: "-"; }
.review-item > summary > span { flex: 1; min-width: 120px; }
.review-item > summary small { color: #886124; }
.review-item > summary strong { font-variant-numeric: tabular-nums; }
.review-item header { margin-top: 16px; }
.review-values { display: flex; gap: 24px; flex-wrap: wrap; }
.review-issue { color: #9d493c; }
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
