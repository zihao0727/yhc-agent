<script setup lang="ts">
import type { ComponentAnalysis } from './requirements-types'
defineProps<{ analysis: ComponentAnalysis }>()
</script>

<template>
  <details v-if="analysis.components.length" class="component-references">
    <summary>材料成本参考 · {{ analysis.components.length }} 个部件<span v-if="analysis.components.some(c => c.cost_per_item != null)"> · 已有可计算部件</span></summary>
    <p>{{ analysis.notice }}</p>
    <section v-for="(component, index) in analysis.components" :key="index">
      <strong>{{ component.name }} · {{ component.material }} {{ component.thickness_mm }} mm</strong>
      <p v-if="component.cost_per_item != null"><strong>每件成品所用此部件成本参考：¥ {{ Number(component.cost_per_item).toFixed(2) }}</strong><small>{{ component.cost_formula }}</small></p>
      <p v-else>尚不能计算此部件成本</p>
      <p v-for="price in component.references" :key="price.id">
        #{{ price.id }} v{{ price.revision }} · {{ price.process }} ·
        {{ Number(price.amount).toLocaleString('zh-CN', { maximumFractionDigits: 6 }) }} 元/m²
        <small v-if="price.source">{{ price.source.filename }} · {{ price.source.sheet }} · {{ price.source.cell }}</small>
        <small v-if="price.notes">{{ price.notes }}</small>
      </p>
      <p v-if="component.candidate_count > component.references.length">共 {{ component.candidate_count }} 条候选，仅展示前 {{ component.references.length }} 条，未自动择价。</p>
      <p v-if="component.missing.length">待核实：{{ component.missing.join('；') }}</p>
      <p v-for="condition in component.estimate_conditions || []" :key="condition">{{ condition }}</p>
    </section>
  </details>
</template>

<style scoped>
.component-references { margin-top: 8px; font-size: 12px; min-width: 0; overflow-wrap: anywhere; }
summary { cursor: pointer; color: #216358; }
p { margin: 6px 0; white-space: normal; }
section { padding: 8px 0; border-top: 1px solid #ddd; }
small { display: block; }
</style>
