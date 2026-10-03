<script setup lang="ts">
import { computed } from 'vue'
import type { DeferredIssue, Line, Quote } from './requirements-types'
import { deferredCauseLabels } from './requirements-types'
import { confirmationRows, questionParts } from './quote-confirmations'

const props = withDefaults(defineProps<{
  items?: Quote['payload']['confirmation_items']
  questions?: string[]
  requirements?: Line[]
  issues?: DeferredIssue[]
  title?: string
  showHeading?: boolean
}>(), { title: '未计价事项', showHeading: true })
const rows = computed(() => confirmationRows(props.items, props.questions, props.requirements, props.issues))
</script>

<template>
  <section v-if="rows.length" class="quote-confirmations">
    <div v-if="showHeading" class="confirmation-heading"><h4>{{ title }}</h4><span>{{ rows.length }} 项</span></div>
    <table class="confirmation-table" :aria-label="title">
      <colgroup><col class="confirmation-number-col" /><col class="confirmation-product-col" /><col /><col class="confirmation-owner-col" /></colgroup>
      <thead><tr><th scope="col">序号</th><th scope="col">项目</th><th scope="col">问题与处理事项</th><th scope="col">处理方</th></tr></thead>
      <tbody>
        <tr v-for="(row, index) in rows" :key="index">
          <td class="confirmation-number">{{ String(index + 1).padStart(2, '0') }}</td>
          <td class="confirmation-product">
            <div v-for="(product, productIndex) in row.products" :key="product.line_id || productIndex">
              <span v-if="product.source_item" class="confirmation-source">#{{ product.source_item }}</span>
              <strong>{{ product.product }}</strong>
            </div>
            <strong v-if="!row.products.length">整单</strong>
            <small v-if="row.cause" class="confirmation-cause">{{ deferredCauseLabels[row.cause] }}</small>
            <small class="confirmation-mobile-owner">{{ row.owner }}</small>
          </td>
          <td class="confirmation-content">
            <div v-for="(part, partIndex) in questionParts(row.question)" :key="partIndex" class="confirmation-question">{{ part }}</div>
            <small v-if="row.reason" class="confirmation-reason">{{ row.reason }}</small>
          </td>
          <td class="confirmation-owner">{{ row.owner }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.quote-confirmations { min-width: 0; margin: 14px 0 18px; color: #34443d; }
.confirmation-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.confirmation-heading h4 { margin: 0; font-size: 14px; font-weight: 600; }
.confirmation-heading > span { color: #7a8780; font-size: 12px; }
.confirmation-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; line-height: 1.75; text-align: left; }
.confirmation-number-col { width: 48px; }
.confirmation-product-col { width: 23%; }
.confirmation-owner-col { width: 88px; }
.confirmation-table th { background: #f1f4f3; color: #66746c; font-size: 11px; font-weight: 500; padding: 10px 12px; border-top: 1px solid #dce4df; border-bottom: 1px solid #dce4df; }
.confirmation-table td { padding: 14px 12px; vertical-align: top; border-bottom: 1px solid #e2e8e4; overflow-wrap: anywhere; white-space: normal; }
.confirmation-table tbody tr:nth-child(even) { background: #f8faf9; }
.confirmation-table tbody tr:hover { background: #f2f7f4; }
.confirmation-table .confirmation-number { color: #8a958e; font-size: 11px; font-variant-numeric: tabular-nums; }
.confirmation-product > div + div { margin-top: 8px; }
.confirmation-product strong { display: block; font-size: 12px; font-weight: 600; }
.confirmation-source { display: block; color: #829088; font-size: 10px; margin-bottom: 2px; }
.confirmation-question { white-space: pre-wrap; }
.confirmation-question + .confirmation-question { margin-top: 7px; }
.confirmation-reason { display: block; margin-top: 9px; padding-top: 8px; border-top: 1px solid #e4eae6; color: #7f7160; font-size: 11px; line-height: 1.7; }
.confirmation-owner { color: #67796d; font-size: 11px; }
.confirmation-cause { display: block; color: #987142; font-size: 10px; margin-top: 5px; }
.confirmation-mobile-owner { display: none; }
@media (max-width: 600px) {
  .confirmation-number-col { width: 36px; }
  .confirmation-product-col { width: 27%; }
  .confirmation-owner-col, .confirmation-table th:last-child, .confirmation-owner { display: none; }
  .confirmation-table th { padding: 9px 6px; }
  .confirmation-table td { padding: 12px 6px; }
  .confirmation-mobile-owner { display: block; margin-top: 6px; color: #7a8780; font-size: 10px; font-weight: 400; }
}
</style>
