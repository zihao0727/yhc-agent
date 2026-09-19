<script setup lang="ts">
import type { QuoteLine } from './requirements-types'
import ComponentReferences from './ComponentReferences.vue'
defineProps<{ line: QuoteLine }>()
const money = (value: string) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
</script>

<template>
  <div class="pricing-details">
    <p v-if="line.estimated && line.effective_requirement"><strong>计价方案：</strong>{{ line.effective_requirement.width_mm || '?' }} × {{ line.effective_requirement.height_mm || '?' }} mm · {{ line.effective_requirement.quantity ?? '?' }} 件（补全项待审核）</p>
    <p v-if="line.unit_price != null" class="finished-unit"><strong>成品单价：¥ {{ money(line.unit_price) }} / {{ line.price?.unit === 'set' ? '套' : '件' }}</strong></p>
    <p v-if="line.unit_price != null && line.amount === null">成品金额未计入小计</p>
    <p v-if="line.fixed_charges && Number(line.fixed_charges)">整单附加费：¥ {{ money(line.fixed_charges) }}（单价外另计）</p>
    <details v-if="line.estimate_conditions?.length || line.warnings?.length">
      <summary>估价条件与范围</summary>
      <p v-for="condition in line.estimate_conditions || line.warnings" :key="condition">{{ condition }}</p>
      <p v-if="line.unit_formula">单价依据：{{ line.unit_formula }}</p>
    </details>
    <ComponentReferences v-if="line.component_analysis" :analysis="line.component_analysis" />
  </div>
</template>

<style scoped>
.pricing-details { min-width: 0; font-size: 12px; overflow-wrap: anywhere; white-space: normal; }
p { margin: 6px 0; }
.finished-unit { color: #216358; }
summary { cursor: pointer; }
</style>
