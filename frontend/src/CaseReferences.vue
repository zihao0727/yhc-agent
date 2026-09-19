<script setup lang="ts">
import type { PricingCase } from './requirements-types'
defineProps<{ cases: PricingCase[] }>()
</script>

<template>
  <details v-if="cases.length" class="case-references">
    <summary>人工报价案例 · {{ cases.length }} 条</summary>
    <section v-for="item in cases" :key="item.id">
      <strong>{{ item.product }} · 原序号 {{ item.source_item }}</strong>
      <p>原公式：<code>{{ item.formula }}</code></p>
      <p>原不含税单价：{{ item.historical_unit_price === null ? '待核实' : `${item.historical_unit_price} 元` }} · 原工程量 {{ item.historical_quantity }}</p>
      <p v-if="item.notes">原备注：{{ item.notes }}</p>
      <p>{{ item.issues.join('；') }}</p>
      <small v-if="item.source">{{ item.source.filename }} · {{ item.source.sheet }} · {{ item.source.cell }}</small>
      <p>{{ item.notice }}</p>
    </section>
  </details>
</template>

<style scoped>
.case-references { margin-top: 8px; font-size: 12px; min-width: 0; overflow-wrap: anywhere; }
summary { cursor: pointer; color: #216358; }
section { padding: 8px 0; border-bottom: 1px solid #ddd; }
p { margin: 6px 0; white-space: normal; }
code { white-space: normal; overflow-wrap: anywhere; }
</style>
