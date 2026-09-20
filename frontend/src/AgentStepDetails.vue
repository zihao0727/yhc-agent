<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { api } from './api'
const props = defineProps<{ jobId: number; runId: number; index: number }>()
const data = ref<unknown>()
const error = ref('')
const loading = ref(false)
const controller = new AbortController()
onBeforeUnmount(() => controller.abort())
async function load(event: Event) {
  if (!(event.target as HTMLDetailsElement).open || data.value || loading.value) return
  loading.value = true; error.value = ''
  try {
    data.value = await api(`/jobs/${props.jobId}/agent-runs/${props.runId}/steps/${props.index}`,
      { signal: controller.signal })
  } catch (e) {
    if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : '加载失败'
  } finally { loading.value = false }
}
</script>

<template>
  <details @toggle="load">
    <summary>诊断数据</summary>
    <p v-if="loading">加载中…</p>
    <p v-else-if="error" role="alert">{{ error }}</p>
    <pre v-else-if="data">{{ JSON.stringify(data, null, 2) }}</pre>
  </details>
</template>
