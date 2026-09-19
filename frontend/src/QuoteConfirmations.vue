<script setup lang="ts">
import type { Quote } from './requirements-types'
defineProps<{ items: Quote['payload']['confirmation_items'] }>()
</script>

<template>
  <section v-if="items?.length" class="quote-confirmations">
    <h4>影响金额的待补充项</h4>
    <p v-for="(item, index) in items" :key="index">
      <strong>{{ item.owner }} · {{ item.question }}</strong><br />
      {{ item.lines.map(line => line.product).join('、') }}
    </p>
  </section>
</template>

<style scoped>
.quote-confirmations { border-top: 1px solid #ddd; padding: 12px 0; overflow-wrap: anywhere; }
h4 { margin: 0 0 8px; font-size: 14px; }
p { margin: 8px 0; font-size: 12px; }
</style>
