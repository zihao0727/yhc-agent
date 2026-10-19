<script setup lang="ts">
import { quoteQuantity, quoteQuantityUnit } from './quote-display'
import type { QuoteLine } from './requirements-types'
import { resolutionLabels, repairActionLabels } from './requirements-types'
import ComponentReferences from './ComponentReferences.vue'
import QuantitySources from './QuantitySources.vue'
defineProps<{ line: QuoteLine }>()
const money = (value: string) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
</script>

<template>
  <div class="pricing-details">
    <QuantitySources :line="line.requirement" />
    <p v-if="line.resolution"><strong>{{ resolutionLabels[line.resolution] }}</strong><span v-if="line.pricing_strategy"> · {{ line.pricing_strategy.label }}</span></p>
    <div v-if="line.amount === null" class="failure-details">
      <p v-for="issue in (line.deferred_issue ? [line.deferred_issue.reason] : line.blockers).slice(0, 3)" :key="issue">{{ issue }}</p>
      <p v-if="line.deferred_issue?.owner">处理责任：{{ line.deferred_issue.owner }}</p>
      <p v-if="line.deferred_issue?.repair_attempts">已尝试修正：{{ line.deferred_issue.repair_attempts }} 次</p>
      <p v-if="line.diagnostics?.[0]?.action">下一步：{{ repairActionLabels[line.diagnostics[0].action] || '核对详细诊断' }}</p>
      <p v-if="line.deferred_issue?.diagnostic_id">诊断编号：{{ line.deferred_issue.diagnostic_id }}</p>
      <details v-if="line.diagnostics?.length"><summary>问题定位</summary><p v-for="d in line.diagnostics" :key="d.code + d.paths.join()">{{ d.message }}<span v-if="d.paths.length"> · {{ d.paths.join('、') }}</span></p></details>
    </div>
    <p v-if="line.estimated && line.effective_requirement"><strong>计价方案：</strong>{{ line.effective_requirement.width_mm || '?' }} × {{ line.effective_requirement.height_mm || '?' }} mm · {{ quoteQuantity(line) ?? '?' }} {{ quoteQuantityUnit(line) }}（补全项待审核）</p>
    <p v-if="line.requirement.type_note || line.requirement.difficulty !== 'unknown' && line.requirement.difficulty"><strong>类型 / 工艺难度：</strong>{{ line.requirement.type_note || '未标注' }} · {{ line.requirement.difficulty || 'unknown' }}</p>
    <p v-if="(line.effective_requirement || line.requirement).pricing_family === 'letter'">每件字数：{{ (line.effective_requirement || line.requirement).letter_groups?.reduce((n, g) => n + g.count, 0) || (line.effective_requirement || line.requirement).character_count || '待确认' }}</p>
    <p v-if="line.requirement.difficulty_note">工艺难度备注原文：{{ line.requirement.difficulty_note }}</p>
    <p v-if="line.unit_price != null" class="finished-unit"><strong>单价：¥ {{ money(line.unit_price) }} / {{ quoteQuantityUnit(line) }}</strong></p>
    <p v-if="line.unit_price != null && line.amount === null">成品金额未计入小计</p>
    <p v-if="line.fixed_charges && Number(line.fixed_charges)">整单附加费：¥ {{ money(line.fixed_charges) }}（单价外另计）</p>
    <details v-if="line.estimate_conditions?.length || line.warnings?.length">
      <summary>估价条件与范围</summary>
      <p v-for="condition in line.estimate_conditions || line.warnings" :key="condition">{{ condition }}</p>
      <p v-if="line.unit_formula">单价依据：{{ line.unit_formula }}</p>
    </details>
    <details v-if="line.review_signals?.length"><summary>计价风险 · {{ line.review_signals.length }} 项</summary><p v-for="signal in line.review_signals" :key="`${signal.code}:${signal.cost_index}`">{{ signal.message }}</p></details>
    <ComponentReferences v-if="line.component_analysis" :analysis="line.component_analysis" />
    <template v-for="(cost, index) in line.estimate_costs || []" :key="index">
      <p v-if="cost.reference?.part">
        {{ cost.label }} · 原表{{ cost.reference.part === 'material_price' ? '材料费' : cost.reference.part === 'process_1_price' ? '工艺一' : '工艺二' }}
        <span v-if="cost.reference.source_cell"> · {{ cost.reference.source_cell }}</span>
        · {{ cost.rate }} / {{ cost.rate_unit }}
      </p>
      <details v-if="cost.script_run">
        <summary>{{ cost.label }} · 计算与校验</summary>
        <p>{{ cost.measurement_note }}</p>
        <p>工程量：{{ cost.script_run.result }} · {{ cost.resolved_formula }}</p>
        <p v-for="(value, key) in cost.script_run.inputs" :key="key">{{ key }} = {{ value }}</p>
        <p v-for="check in cost.script_run.checks" :key="check.message">{{ check.passed ? '通过' : '未通过' }}：{{ check.message }}</p>
        <pre>{{ cost.script_run.source }}</pre>
      </details>
    </template>
  </div>
</template>

<style scoped>
.pricing-details { min-width: 0; font-size: 12px; overflow-wrap: anywhere; white-space: normal; }
p { margin: 6px 0; }
.finished-unit { color: #216358; }
summary { cursor: pointer; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-width: 100%; }
.failure-details { border-left: 2px solid #bb6c42; padding-left: 8px; color: #854531; }
</style>
