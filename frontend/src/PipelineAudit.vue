<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Eye, RefreshCw, ScanSearch } from 'lucide-vue-next'
import { api } from './api'
import type { CustomerFile, SourceAgentAudit } from './requirements-types'
const props = defineProps<{ jobId: number; revision: number; disabled?: boolean }>()
const emit = defineEmits<{ preview: [fileId: number, page: number, region?: number[]] }>()
type Audit = {
  revision: number; requires_review: number; files: CustomerFile[]
  lines: { line_id: string; product: string; status: string; old_amount: string | null; shadow_amount: string | null
    diagnostics: { code: string; message: string }[]; review_signals?: { code: string; message: string }[] }[]
  coverage: { file_id: number; page: number; sheet: string; row: number; status: string; reason: string
    kind?: string; region?: number[] | null; label?: string; expected_items?: number; extracted_items?: number }[]
  source_links?: { file_ids: number[]; product: string; source_code: string; status: string; reason: string }[]
  source_agent?: SourceAgentAudit | null
  metrics: { tool_calls: number; repeated_calls: number; tokens: number; saved_items: number; deferred_items: number } | null
}
const report = ref<Audit | null>(null)
const loading = ref(false), error = ref('')
type Workspace = {
  runtime: { available: boolean; reason?: string }
  recent: { artifact_id: string; status: string; purpose: string; line_count: number; execution_error?: string
    validation: { error?: string; status?: string; corrected_fields?: unknown[]; quantity_missing?: number[]
      unresolved_sources?: unknown[]; diagnostics?: { reason?: string } } }[]
  adapters: { adapter_id: string; name: string; status: string }[]
}
const workspace = ref<Workspace | null>(null)
const workspaceLoading = ref(false)
const scriptStatuses: Record<string, string> = {
  running: '执行中', validated: '校验通过', needs_review: '待复核', needs_repair: '待修正',
  observations_only: '已生成观察结果', execution_failed: '执行失败', interrupted: '已中断',
}
const reparsing = ref<number | null>(null)
const parsed = ref<Record<number, { discovered: number; resolved: number; unresolved: number }>>({})
const statuses: Record<string, string> = { unchanged: '金额一致', changed: '金额变化，待复核', blocked: '新契约未通过', needs_review: '需选择计价策略' }
const decisionLabels: Record<string, string> = { add: '新增观察', revise: '纠正观察', exclude: '移出报价', merge: '合并来源', restore: '恢复项目' }
const roleLabels: Record<string, string> = { requirements: '订购清单', drawing: '图纸', catalog: '目录', cover: '封面', price_reference: '价格参考', note: '说明', unknown: '用途待核对' }
const unresolvedRows = computed(() => report.value?.coverage.filter(row => row.status === 'unresolved') || [])
const visualRegions = computed(() => report.value?.coverage.filter(row => row.kind === 'region') || [])
function filename(id: number) { return report.value?.files.find(file => file.id === id)?.filename || `文件${id}` }
watch(() => [props.jobId, props.revision], () => { report.value = null; workspace.value = null; parsed.value = {}; error.value = '' })
async function inspectWorkspace() {
  workspaceLoading.value = true; error.value = ''
  const id = props.jobId, revision = props.revision
  try {
    const next = await api<Workspace>(`/jobs/${id}/file-workspace`)
    if (id === props.jobId && revision === props.revision) workspace.value = next
  } catch (e) { error.value = e instanceof Error ? e.message : '工作区读取失败' }
  finally { workspaceLoading.value = false }
}
async function inspect() {
  loading.value = true; error.value = ''
  const id = props.jobId, revision = props.revision
  try {
    const next = await api<Audit>(`/jobs/${id}/pipeline-audit`)
    if (id === props.jobId && revision === props.revision) report.value = next
  } catch (e) { error.value = e instanceof Error ? e.message : '核查失败' }
  finally { loading.value = false }
}
async function reparse(file: CustomerFile) {
  reparsing.value = file.id; error.value = ''
  const id = props.jobId, revision = props.revision
  try {
    const data = await api<{ resource_summary: { discovered: number; resolved: number; unresolved: number } }>(
      `/customer-files/${file.id}/reparse-preview`, { method: 'POST' })
    if (id === props.jobId && revision === props.revision) parsed.value[file.id] = data.resource_summary
  } catch (e) { error.value = e instanceof Error ? e.message : '原文件核查失败' }
  finally { reparsing.value = null }
}
</script>

<template>
  <section class="pipeline-audit">
    <header><h3>资料与计价核查</h3><el-button :loading="loading" :disabled="disabled || reparsing !== null" @click="inspect"><ScanSearch :size="15" />只读核查</el-button></header>
    <p v-if="error" role="alert" class="audit-error">{{ error }}</p>
    <div class="file-workspace">
      <header><h3>文件处理工作区</h3><el-tooltip content="刷新文件处理记录"><el-button :loading="workspaceLoading" aria-label="刷新文件处理记录" @click="inspectWorkspace"><RefreshCw :size="15" /></el-button></el-tooltip></header>
      <template v-if="workspace">
        <p>{{ workspace.runtime.available ? '隔离执行环境可用' : workspace.runtime.reason }}</p>
        <p v-if="!workspace.recent.length">暂无脚本处理记录</p>
        <div v-for="entry in workspace.recent" :key="entry.artifact_id" class="audit-file">
          <span>{{ entry.purpose }}</span><span>{{ scriptStatuses[entry.status] || entry.status }} · {{ entry.line_count }} 项</span>
          <p v-if="entry.execution_error || entry.validation.error" class="audit-error">{{ entry.execution_error || entry.validation.error }}</p>
          <p v-if="entry.validation.diagnostics?.reason" class="audit-error">{{ entry.validation.diagnostics.reason }}</p>
          <p v-if="entry.validation.corrected_fields?.length">发现 {{ entry.validation.corrected_fields.length }} 处数值差异，须复核</p>
          <p v-if="entry.validation.quantity_missing?.length">{{ entry.validation.quantity_missing.length }} 项成品数量待确认</p>
          <p v-if="entry.validation.unresolved_sources?.length">{{ entry.validation.unresolved_sources.length }} 处原文尚未确认用途</p>
        </div>
        <details v-if="workspace.adapters.length"><summary>文件适配器 · {{ workspace.adapters.length }}</summary>
          <p v-for="adapter in workspace.adapters" :key="adapter.adapter_id">{{ adapter.name }} · {{ adapter.status === 'verified' ? '多文件校验通过' : '候选' }}</p>
        </details>
      </template>
    </div>
    <template v-if="report">
      <p>需求版本 {{ report.revision }} · {{ report.requires_review }} 项待复核 · 原报价未修改</p>
      <section v-if="report.source_agent" aria-label="AI资料核对记录">
        <p>AI已读 {{ report.source_agent.read_pages ?? 0 }} / {{ report.source_agent.total_pages ?? 0 }} 页 · 已复核 {{ report.source_agent.reviewed_pages ?? 0 }} 页</p>
        <p v-if="report.source_agent.plan">{{ report.source_agent.plan }}</p>
        <p v-if="report.source_agent.notice">{{ report.source_agent.notice }}</p>
        <details v-if="report.source_agent.page_reviews?.length"><summary>页面判断 · {{ report.source_agent.page_reviews.length }} 页</summary>
          <div v-for="review in report.source_agent.page_reviews" :key="`${review.file_id}:${review.page}`" class="audit-file">
            <span>{{ filename(review.file_id) }} · 第 {{ review.page }} 页 · {{ roleLabels[review.role] || review.role }} · {{ review.status === 'reviewed' ? 'AI已复核' : '仍有疑问' }}</span>
            <p>{{ review.reason }}</p>
            <el-tooltip content="查看原始页面"><el-button aria-label="查看原始页面" @click="emit('preview', review.file_id, review.page)"><Eye :size="15" /></el-button></el-tooltip>
          </div>
        </details>
        <details v-if="report.source_agent.decisions?.length"><summary>项目调整记录 · {{ report.source_agent.decisions.length }} 条</summary>
          <p v-for="(decision, index) in report.source_agent.decisions" :key="index">{{ decisionLabels[decision.action] || decision.action }} · {{ decision.observation_ids.join('、') }} · {{ decision.reason }}</p>
        </details>
      </section>
      <p v-if="report.metrics">工具调用 {{ report.metrics.tool_calls }} 次 · 重复拦截 {{ report.metrics.repeated_calls }} 次 · {{ report.metrics.tokens.toLocaleString() }} tokens</p>
      <div v-for="file in report.files" :key="file.id" class="audit-file">
        <strong>{{ file.filename }}</strong>
        <span v-if="parsed[file.id] || file.resource_summary">图纸读取 {{ (parsed[file.id] || file.resource_summary)!.resolved }} / {{ (parsed[file.id] || file.resource_summary)!.discovered }}，未读取 {{ (parsed[file.id] || file.resource_summary)!.unresolved }}</span>
        <span v-else-if="file.media_type.includes('spreadsheetml')">旧解析版本，图片完整性待核查</span>
        <el-tooltip v-if="file.media_type.includes('spreadsheetml')" content="重新解析原文件，仅核查，不覆盖原资料">
          <el-button :loading="reparsing === file.id" :disabled="loading || reparsing !== null || disabled" aria-label="核查原文件图片" @click="reparse(file)"><RefreshCw :size="15" /></el-button>
        </el-tooltip>
      </div>
      <p v-if="!report.coverage.length">原识别记录缺少来源覆盖清单，完整性待核查</p>
      <details v-else><summary>来源覆盖 · {{ report.coverage.length }} 处 · {{ unresolvedRows.length }} 处未解决</summary>
        <div v-for="(row, index) in visualRegions" :key="`region:${index}`" class="audit-file">
          <span>{{ filename(row.file_id) }} · 第 {{ row.page }} 页 · 区域 {{ row.region?.map(v => Math.round(v * 100)).join(', ') }}%</span>
          <span>{{ row.extracted_items ?? '?' }} / {{ row.expected_items ?? '?' }} 项 · {{ row.status === 'unresolved' ? '待核图' : '已核对' }}</span>
          <el-tooltip content="查看原图区域"><el-button aria-label="查看原图区域" @click="emit('preview', row.file_id, row.page, row.region || undefined)"><Eye :size="15" /></el-button></el-tooltip>
        </div>
        <p v-for="(row, index) in unresolvedRows.filter(row => row.kind !== 'region')" :key="`unresolved:${index}`">{{ row.sheet || filename(row.file_id) }} · 第 {{ row.page }} 页<span v-if="row.row"> · 第 {{ row.row }} 行</span> · {{ row.label }} {{ row.reason }}</p>
      </details>
      <details v-if="report.source_links?.length"><summary>多文件关联 · {{ report.source_links.length }} 处</summary>
        <p v-for="(link, index) in report.source_links" :key="`link:${index}`">{{ link.product }} {{ link.source_code }} · {{ link.file_ids.map(filename).join('、') }} · {{ link.status === 'merged' ? '已合并证据' : link.status === 'reference' ? '单价参考' : '归属待确认' }} · {{ link.reason }}</p>
      </details>
      <div class="audit-table" role="region" aria-label="新旧计价对比">
        <table><thead><tr><th>项目</th><th>原金额</th><th>新契约试算</th><th>核查结果</th></tr></thead>
          <tbody><tr v-for="line in report.lines" :key="line.line_id"><td>{{ line.product }}</td><td>{{ line.old_amount ?? '未计价' }}</td><td>{{ line.shadow_amount ?? '未计价' }}</td><td>{{ statuses[line.status] }}<p v-for="d in [...line.diagnostics, ...(line.review_signals || [])]" :key="d.code">{{ d.message }}</p></td></tr></tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.pipeline-audit { border-top: 1px solid #dce4df; padding: 14px 0; min-width: 0; }
header, .audit-file { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
h3 { font-size: 15px; margin: 0; }
p, .audit-file { font-size: 12px; overflow-wrap: anywhere; }
.audit-file { padding: 8px 0; border-bottom: 1px solid #e2e6e3; }
.audit-error { color: #aa4939; }
.file-workspace { padding: 12px 0; border-bottom: 1px solid #dce4df; }
.file-workspace .audit-file > p { flex-basis: 100%; margin: 0; }
.audit-table { overflow-x: auto; margin-top: 12px; }
table { border-collapse: collapse; width: 100%; min-width: 480px; font-size: 12px; table-layout: fixed; }
th, td { padding: 8px; border-bottom: 1px solid #dce4df; vertical-align: top; text-align: left; overflow-wrap: anywhere; }
th:nth-child(2), th:nth-child(3) { width: 80px; }
summary { cursor: pointer; }
</style>
