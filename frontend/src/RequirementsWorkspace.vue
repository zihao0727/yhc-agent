<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowRight, Check, ChevronLeft, ChevronRight, FileImage, FileText, KeyRound, Link, Pencil, Plus, RefreshCw, Save, Search, Sparkles, Table2, Trash2, Upload, X, ZoomIn, ZoomOut } from 'lucide-vue-next'
import { api, apiBlob, languageLabels, statusLabels, unitLabels, type Price, type Rule } from './api'
import { businessRuleVersion, businessTerms, costKinds, includedCostKinds, jobLabels, type CustomerFile, type Job, type JobProgress, type Line, type Quote } from './requirements-types'
import BusinessFields from './BusinessFields.vue'
import QuantitySources from './QuantitySources.vue'
import QuoteChat from './QuoteChat.vue'
import QuoteResults from './QuoteResults.vue'
import QuoteSupplement from './QuoteSupplement.vue'
import EstimateEditor from './EstimateEditor.vue'
import PipelineAudit from './PipelineAudit.vue'
import { shouldResumeConversation } from './agent-intent'
import { attachmentAccept, uploadLimits, validateAttachments } from './upload-limits'
import { applySupplementEdits, pendingRows, type SupplementEdit } from './quote-workspace'

const chat = ref<InstanceType<typeof QuoteChat>>()
const supplementPanel = ref<InstanceType<typeof QuoteSupplement>>()
const workspaceView = ref<'results' | 'supplement' | 'conversation'>('conversation')
const detailsOpen = ref(false)
type ProposedChange = { line_id: string; field: keyof Line; value: string | null; reason: string;
  target?: string; target_id?: string | null }
type QuoteOperation = { kind: 'add' | 'remove' | 'estimate' | 'extras'; line_id: string; reason: string;
  new_line?: Partial<Line>; estimate?: Line['estimate']; extras?: Line['extras'] }
const operationLabels = { add: '新增项目', remove: '删除项目', estimate: '修改计价方案', extras: '修改附加费' }
const proposal = ref<{ jobId: number; revision: number; changes: ProposedChange[]; operations: QuoteOperation[]; resumeAfter?: boolean } | null>(null)
const proposalOpen = ref(false)
const fieldLabels: Record<string, string> = {
  quantity: '数量', width_mm: '宽度 (mm)', height_mm: '高度 (mm)', depth_mm: '深度 (mm)',
  thickness_mm: '厚度 (mm)', billing_length_mm: '计价长度 (mm)', material: '材质',
  process: '工艺', language: '语言', product: '项目名称', pricing_category: '报价类别', text_content: '文字内容',
  type_note: '类型备注', difficulty: '工艺难度', difficulty_note: '工艺难度备注原文', character_count: '每件字数', area_m2: '图纸面积',
  faces: '单/双面', lighting: '装灯', installation_scene: '安装场景', power_count: '电源台数', power_note: '电源核算依据',
}
const jobs = ref<Job[]>([])
const job = ref<Job | null>(null)
const total = ref(0)
const page = ref(1)
const query = ref('')
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const config = ref({ configured: false, model: '', provider: '', environment_managed: false })
const embeddingConfig = ref({ configured: false, model: 'Qwen/Qwen3-Embedding-4B', provider: '硅基流动', environment_managed: false })
const configTab = ref<'quote' | 'embedding'>('quote')
const currentKeyConfig = computed(() => configTab.value === 'embedding' ? embeddingConfig.value : config.value)
const externalProviders = computed(() => embeddingConfig.value.configured ? 'DeepSeek及硅基流动' : 'DeepSeek')
const createOpen = ref(false)
const createForm = reactive({ title: '', customer: '', brief: '' })
const files = ref<File[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const keyOpen = ref(false)
const key = ref('')
const dialogError = ref('')
const extractOpen = ref(false)
const supplementary = ref('')
const consent = ref(false)
const replace = ref(false)
const agentMode = ref(false)
const requestIntent = ref<'auto' | 'resume' | 'extract'>('auto')
const autoRun = ref(false)
const lineOpen = ref(false)
const editingLine = ref<Line | null>(null)
const pricingProducts = ref<{ id: number; name: string }[]>([])
const reason = ref('')
const candidateOpen = ref(false)
const matchingLine = ref<Line | null>(null)
const candidates = ref<(Price & { mismatches: string[] })[]>([])
const candidateRules = ref<Rule[]>([])
const candidateSearch = ref('')
const candidateLoading = ref(false)
const quoteOpen = ref(false)
const terms = ref('')
const quoteRounding = ref('none')
const currentPolicy = computed(() => job.value?.business_rules_version === businessRuleVersion)
const selectedQuoteId = ref<number | null>(null)
const selectedQuote = computed(() => job.value?.quotes.find(q => q.id === selectedQuoteId.value) || job.value?.quotes[0])
const approvalOpen = ref(false)
const approvalNote = ref('')
const approvalTerms = ref('')
const commercialConfirmed = ref(false)
const estimatesConfirmed = ref(false)
const sourcesConfirmed = ref(false)
const previewOpen = ref(false)
const previewFile = ref<CustomerFile | null>(null)
const previewPage = ref(1)
const previewUrl = ref('')
const previewLoading = ref(false)
const previewScale = ref(100)
type ExcelSource = { sheet: string; row_start: number; row_end: number; notice: string; warnings: string[]
  rows: { row: number; cells: { cell: string; value: string; formula?: string; formula_status?: string }[] }[]
  images: { row: number; cell: string; url: string }[] }
const previewSource = ref<ExcelSource | null>(null)
const isExcel = (file: CustomerFile) => file.media_type.includes('spreadsheetml')
const detailTab = ref('requirements')
const requirementPage = ref(1)
const visibleRequirements = computed(() => job.value?.requirements.slice((requirementPage.value - 1) * 50, requirementPage.value * 50) || [])
let previewSequence = 0
let pollTimer: ReturnType<typeof setInterval> | undefined
let disposed = false
let polling = false
let lastIdlePoll = 0
let navigationSequence = 0
let listSequence = 0
let pollController: AbortController | undefined
function invalidateRequests() {
  navigationSequence++
  pollController?.abort()
}
function discardProposal() { proposal.value = null; proposalOpen.value = false }
function proposalTarget(change: ProposedChange) {
  const line = job.value?.requirements.find(line => line.id === change.line_id)
  if (!line) return { name: '项目', original: null }
  const objects = change.target === 'component'
    ? [...(line.components || []), ...(line.estimate?.proposed_components || [])]
    : [...(line.letter_groups || []), ...(line.estimate?.proposed_letter_groups || [])]
  const target = change.target && change.target !== 'item'
    ? objects.find(object => object.object_id === change.target_id) : line
  return { name: `${line.source_item ? `#${line.source_item} ` : ''}${line.product}${target && target !== line ? ` · ${'name' in target ? target.name : 'label' in target ? target.label : ''}` : ''}`,
    original: target ? (target as unknown as Record<string, unknown>)[change.field] : null }
}
async function pollProgress() {
  if (polling || busy.value || !job.value || disposed) return
  if (job.value.status !== 'processing') {
    if (!job.value.quote_workspace?.actions.auto_resume || Date.now() - lastIdlePoll < 15000) return
    lastIdlePoll = Date.now()
  }
  polling = true
  const id = job.value.id
  const sequence = navigationSequence
  const run = job.value.agent_runs[0]
  const controller = new AbortController()
  pollController = controller
  try {
    const progress = await api<JobProgress>(`/jobs/${id}/progress?run_id=${run?.id || 0}&after=${run?.steps.length || 0}`,
      { signal: controller.signal })
    if (disposed || sequence !== navigationSequence || job.value?.id !== id || busy.value) return
    if (progress.revision < job.value.revision) return
    if (progress.messages) job.value.messages = progress.messages
    if (progress.revision > job.value.revision && (progress.run?.usage.prepared
        || progress.run?.usage.extraction_progress?.extraction_route === 'source_agent')) {
      const current = await api<Job>(`/jobs/${id}`, { signal: controller.signal })
      if (!disposed && sequence === navigationSequence && job.value?.id === id && !busy.value) job.value = current
      return
    }
    if (progress.status !== 'processing' && progress.status === job.value.status
      && progress.revision === job.value.revision && progress.run?.id === job.value.agent_runs[0]?.id
      && progress.run?.step_count === job.value.agent_runs[0]?.steps.length) {
      job.value.quote_workspace = progress.quote_workspace
      job.value.pricing_progress = progress.pricing_progress
      return
    }
    if (progress.status !== 'processing' || job.value.status !== 'processing') {
      const current = await api<Job>(`/jobs/${id}`, { signal: controller.signal })
      if (!disposed && sequence === navigationSequence && job.value?.id === id && !busy.value) {
        job.value = current
        await refreshList()
      }
      return
    }
    // Keep the complete document revision until a full reload; only progress is incremental.
    job.value.pricing_progress = progress.pricing_progress
    job.value.quote_workspace = progress.quote_workspace
    if (progress.run) {
      const incoming = progress.run
      const previous = job.value.agent_runs.find(item => item.id === incoming.id)
      const merged = { ...incoming, steps: [...(previous?.steps.slice(0, incoming.offset) || []), ...incoming.steps] }
      job.value.agent_runs = [merged, ...job.value.agent_runs.filter(item => item.id !== incoming.id)]
      const savedLines = incoming.usage.extraction_progress?.saved_lines || 0
      if (savedLines > job.value.requirements.length) {
        const rows = await api<{ lines: Line[]; total: number }>(`/jobs/${id}/requirements-page?offset=${job.value.requirements.length}&limit=100`,
          { signal: controller.signal })
        if (disposed || sequence !== navigationSequence || job.value?.id !== id || busy.value) return
        job.value.requirements.push(...rows.lines)
        job.value.line_count = rows.total
        job.value.extraction.incomplete = true
      }
    }
    error.value = ''
  } catch (e) {
    if (!controller.signal.aborted && sequence === navigationSequence) error.value = `进度连接中断，正在重试：${err(e)}`
  } finally { polling = false }
}
const money = (value: string | null) => value === null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const err = (e: unknown) => e instanceof Error ? e.message : '操作失败'
const newLine = (): Line => ({
  id: crypto.randomUUID().replaceAll('-', '').slice(0, 16), product: '', text_content: '', material: '',
  thickness_mm: null, width_mm: null, height_mm: null, quantity: null, language: 'unknown', process: '',
  notes: '', evidence: [], uncertainties: [], confirmed: false, billing_length_mm: null,
  selected_price_id: null, selected_price_revision: null, price_review_note: '', extras: [],
  business_rules_version: job.value?.business_rules_version || businessRuleVersion,
  pricing_family: 'unknown', type_note: '', difficulty: 'unknown', difficulty_note: '', character_count: null, letter_groups: [],
  area_m2: null, faces: 'unknown', lighting: 'unknown', installation_scene: 'unknown',
  length_rounding: 'exact', power_mode: 'manual', power_count: null, power_adjustment: 0, power_note: '',
})
async function refresh() {
  loading.value = true; error.value = ''
  const id = job.value?.id
  const sequence = navigationSequence
  try {
    config.value = await api('/model-config')
    Object.assign(uploadLimits, await api('/upload-config'))
    embeddingConfig.value = { ...embeddingConfig.value, ...await api<typeof embeddingConfig.value>('/embedding-config') }
    pricingProducts.value = (await api<{ products: typeof pricingProducts.value }>('/options')).products
    if (id) {
      const current = await api<Job>(`/jobs/${id}`)
      if (sequence === navigationSequence && job.value?.id === id && current.revision >= job.value.revision) job.value = current
    }
    await refreshList()
  } catch (e) { error.value = err(e) }
  finally { loading.value = false }
}
async function refreshList() {
  const sequence = ++listSequence
  const result = await api<{ items: Job[]; total: number }>(`/jobs?page=${page.value}&q=${encodeURIComponent(query.value)}`)
  if (disposed || sequence !== listSequence) return
  jobs.value = result.items; total.value = result.total
}
function openDetails(tab: string) {
  if (tab === 'review') { workspaceView.value = 'supplement'; detailsOpen.value = false; return }
  if (tab === 'quotes') { workspaceView.value = 'results'; detailsOpen.value = false; return }
  detailTab.value = tab; detailsOpen.value = true
}
function newConversation() { invalidateRequests(); discardProposal(); job.value = null; error.value = ''; detailsOpen.value = false; workspaceView.value = 'conversation'; selectedQuoteId.value = null }
async function sendChat(payload: { text: string; files: File[] }, options: { autoContinue?: boolean; onFilesAccepted?: () => void } = {}) {
  if (busy.value) return false
  if (job.value?.status === 'processing' && (!payload.text.trim() || payload.files.length)) return false
  invalidateRequests()
  discardProposal()
  busy.value = true; error.value = ''
  try {
    const initial = !job.value
    const resume = !initial && job.value?.status !== 'processing' && shouldResumeConversation(
      payload.text, payload.files.length, Boolean(job.value?.requirements.length))
    if (!initial && !resume && !payload.files.length && job.value?.requirements.length) {
      const current = job.value
      const response = await api<{ job: Job; changes: ProposedChange[]; operations?: QuoteOperation[];
        applied?: boolean; queued?: boolean; continue_pricing?: boolean }>(`/jobs/${current.id}/conversation`, {
        method: 'POST', body: JSON.stringify({ revision: current.revision,
          supplementary_text: payload.text, allow_external_processing: true }),
      })
      job.value = response.job
      chat.value?.clearComposer()
      discardProposal()
      if (response.changes.length || response.operations?.length) {
        proposal.value = { jobId: current.id, revision: response.job.revision, changes: response.changes,
          operations: response.operations || [], resumeAfter: options.autoContinue === true }
        proposalOpen.value = true
      }
      if (response.queued) ElMessage.info('补充已收到，将在当前步骤完成后处理')
      if (response.continue_pricing && !proposal.value && options.autoContinue !== false && response.job.status !== 'processing') {
        agentMode.value = true; consent.value = true; replace.value = false
        requestIntent.value = 'resume'; supplementary.value = ''
        await extract()
      }
      await refreshList()
      return true
    }
    if (initial) {
      const form = new FormData()
      form.append('title', (payload.text || payload.files[0]?.name || '新报价').slice(0, 80))
      form.append('brief', payload.text)
      payload.files.forEach(file => form.append('files', file))
      job.value = await api<Job>('/jobs', { method: 'POST', body: form })
    } else if (payload.files.length && job.value) {
      if (job.value.requirements.length) {
        try {
          await ElMessageBox.confirm('新增附件将重新整理整单需求，原有价格关联会被清除，旧报价将失效。是否继续？', '重新识别资料',
            { confirmButtonText: '确认重新识别', cancelButtonText: '取消', type: 'warning' })
        } catch { return false }
      }
      const form = new FormData()
      form.append('revision', String(job.value.revision))
      payload.files.forEach(file => form.append('files', file))
      job.value = await api<Job>(`/jobs/${job.value.id}/files`, { method: 'POST', body: form })
      options.onFilesAccepted?.()
    }
    // Clear accepted attachments before running so retries cannot upload duplicates.
    chat.value?.clearComposer()
    if (options.autoContinue === false) { await refreshList(); return true }
    agentMode.value = true; consent.value = true; replace.value = !resume
    requestIntent.value = resume ? 'resume' : 'extract'
    supplementary.value = initial ? '' : payload.text
    const accepted = await extract()
    if (accepted) workspaceView.value = 'results'
    await refreshList()
    return accepted
  } catch (e) { error.value = err(e); return false }
  finally { busy.value = false }
}
async function openJob(id: number) {
  requirementPage.value = 1
  invalidateRequests()
  discardProposal()
  const sequence = navigationSequence
  loading.value = true; error.value = ''
  try {
    const current = await api<Job>(`/jobs/${id}`)
    if (sequence !== navigationSequence || disposed) return
    job.value = current; detailTab.value = 'requirements'; selectedQuoteId.value = null
    workspaceView.value = pendingRows(current).length ? 'supplement' : current.quotes.length || current.requirements.length ? 'results' : 'conversation'
  } catch (e) { if (sequence === navigationSequence) error.value = err(e) }
  finally { if (sequence === navigationSequence) loading.value = false }
}
async function adoptPolicy() {
  if (!job.value || busy.value) return
  try {
    await ElMessageBox.confirm('采用9月23日现场口径后，旧报价将失效，所有需求需重新审核。原价格、方案和历史版本保留。', '更新报价口径', {
      confirmButtonText: '采用现场口径', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  busy.value = true
  try {
    job.value = await api<Job>(`/jobs/${job.value.id}/pricing-policy`, { method: 'PUT',
      body: JSON.stringify({ revision: job.value.revision, reason: '管理员明确采用2026-09-23现场口径' }) })
    await refreshList()
    ElMessage.success('报价口径已更新，请审核需求并重新计价')
  } catch (e) { ElMessage.error(err(e)) }
  finally { busy.value = false }
}
async function applyProposal() {
  if (!proposal.value || !job.value || busy.value) return
  const pending = proposal.value
  if (pending.jobId !== job.value.id || pending.revision !== job.value.revision) {
    dialogError.value = '需求版本已变化，请重新提出修改'; return
  }
  busy.value = true; dialogError.value = ''
  let saved = false
  try {
    job.value = await api<Job>(`/jobs/${pending.jobId}/changes`, { method: 'POST',
      body: JSON.stringify({ revision: pending.revision, changes: pending.changes, operations: pending.operations }) })
    saved = true
    discardProposal()
    const quote = await api<Quote>(`/jobs/${pending.jobId}/quotes`, { method: 'POST',
      body: JSON.stringify({ revision: job.value.revision,
        terms: job.value.quotes[0]?.terms || '根据用户确认的局部修改重新计价，费用及范围待审核' }) })
    await openJob(pending.jobId)
    selectedQuoteId.value = quote.id
    workspaceView.value = 'results'; detailsOpen.value = false
    ElMessage.success(`修改已保存并重新计价，生成 V${quote.version}`)
    if (pending.resumeAfter) {
      agentMode.value = true; consent.value = true; replace.value = false
      requestIntent.value = 'resume'; supplementary.value = ''
      await extract()
    }
  } catch (e) {
    if (saved) error.value = `修改已保存，重新计价失败：${err(e)}。请在报价结果中重新计价。`
    else dialogError.value = err(e)
  } finally { busy.value = false }
}
async function deleteConversation(item: Job) {
  if (busy.value || item.status === 'processing') return
  try {
    await ElMessageBox.confirm(`删除“${item.title}”？该会话的消息、附件和报价记录将永久删除，操作审计保留。`, '删除会话', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  if (busy.value) return
  busy.value = true
  try {
    const current = job.value?.id === item.id ? job.value : item
    await api(`/jobs/${item.id}`, { method: 'DELETE',
      body: JSON.stringify({ revision: current.revision, reason: '用户删除报价会话' }) })
    if (job.value?.id === item.id) {
      newConversation()
      chat.value?.clearComposer()
    }
    if (jobs.value.length === 1 && page.value > 1) page.value--
    await refreshList()
    ElMessage.success('会话已删除')
  } catch (e) { ElMessage.error(err(e)) }
  finally { busy.value = false }
}
function chooseFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const selected = Array.from(input.files || [])
  const validation = validateAttachments([...files.value, ...selected])
  if (validation) ElMessage.error(validation)
  else files.value.push(...selected)
  input.value = ''
}
function startCreate() {
  Object.assign(createForm, { title: '', customer: '', brief: '' }); files.value = []; autoRun.value = false; dialogError.value = ''; createOpen.value = true
}
async function createJob() {
  dialogError.value = ''
  if (!createForm.title.trim() || (!files.value.length && !createForm.brief.trim())) { dialogError.value = '请填写任务名称，并补充说明或上传报价资料'; return }
  busy.value = true
  try {
    const form = new FormData()
    for (const [k, v] of Object.entries(createForm)) form.append(k, v)
    files.value.forEach(file => form.append('files', file))
    job.value = await api<Job>('/jobs', { method: 'POST', body: form })
    createOpen.value = false; detailTab.value = 'requirements'; selectedQuoteId.value = null
    ElMessage.success('资料已保存到本地')
    if (autoRun.value) {
      agentMode.value = true; consent.value = true; supplementary.value = ''; replace.value = false
      requestIntent.value = 'extract'
      await extract()
    }
  } catch (e) { dialogError.value = err(e) }
  finally { busy.value = false }
}
async function saveKey() {
  busy.value = true; dialogError.value = ''
  try {
    const target = configTab.value === 'embedding' ? embeddingConfig : config
    target.value = await api(configTab.value === 'embedding' ? '/embedding-config' : '/model-config',
      { method: 'PUT', body: JSON.stringify({ api_key: key.value }) })
    key.value = ''; keyOpen.value = false; ElMessage.success('API Key 已保存')
  } catch (e) { dialogError.value = err(e) }
  finally { busy.value = false }
}
async function clearKey() {
  try {
    await ElMessageBox.confirm(`删除本地 ${currentKeyConfig.value.provider || 'DeepSeek'} API Key？`, '删除密钥', { confirmButtonText: '删除', cancelButtonText: '取消' })
    const target = configTab.value === 'embedding' ? embeddingConfig : config
    target.value = await api(configTab.value === 'embedding' ? '/embedding-config' : '/model-config', { method: 'DELETE' }); keyOpen.value = false
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(err(e)) }
}
function startExtraction(agent = false) {
  agentMode.value = agent
  requestIntent.value = 'auto'
  supplementary.value = ''; consent.value = false; replace.value = false; dialogError.value = ''; extractOpen.value = true
}
async function extract() {
  if (!job.value) return false
  const intent = !agentMode.value ? 'extract' : requestIntent.value !== 'auto' ? requestIntent.value
    : replace.value ? 'extract'
      : shouldResumeConversation(supplementary.value, 0, Boolean(job.value.requirements.length)) ? 'resume' : 'auto'
  const changingRequirements = !agentMode.value || intent === 'extract' || (intent !== 'resume' && Boolean(supplementary.value.trim()))
  if (!consent.value || (job.value.requirements.length && changingRequirements && !replace.value)) {
    dialogError.value = '请确认外部处理授权及重新识别影响'; return false
  }
  const id = job.value.id
  const payload = { revision: job.value.revision, supplementary_text: supplementary.value,
    allow_external_processing: consent.value, replace_existing: replace.value, intent }
  busy.value = true; extractOpen.value = false; error.value = ''
  const isAgent = agentMode.value
  job.value.status = 'processing'
  try {
    const result = await api<Job>(`/jobs/${id}/${isAgent ? 'agent' : 'extract'}`, { method: 'POST', body: JSON.stringify(payload) })
    if (job.value?.id === id) {
      job.value = result
      if (isAgent && result.agent_runs[0]?.status === 'succeeded') workspaceView.value = 'results'
      if (isAgent && result.agent_runs[0]?.status === 'waiting') workspaceView.value = 'supplement'
    }
    if (isAgent) ElMessage.info(result.agent_runs[0]?.message || 'Agent 执行结束')
    else if (result.status === 'processing') ElMessage.info('后台资料整理已启动')
    else ElMessage.success('需求整理完成，等待人工确认')
    return true
  } catch (e) {
    error.value = err(e)
    try { if (job.value?.id === id) job.value = await api<Job>(`/jobs/${id}`) } catch { /* Preserve actionable error. */ }
    return false
  } finally { busy.value = false }
}
async function continuePricing() {
  if (!job.value || busy.value || job.value.status === 'processing' || !config.value.configured) return false
  supplementary.value = ''; replace.value = false; agentMode.value = true
  requestIntent.value = job.value.requirements.length ? 'resume' : 'extract'; consent.value = true
  const accepted = await extract()
  if (accepted) workspaceView.value = 'results'
  return accepted
}
async function saveSupplement(payload: { edits: SupplementEdit[]; text: string; files: File[]; resume: boolean }) {
  if (!job.value || busy.value || job.value.status === 'processing') return
  invalidateRequests()
  error.value = ''
  let savedFields = false
  if (payload.files.length && payload.edits.length) {
    error.value = '附件会重新整理需求，请先提交附件，再填写项目字段；当前草稿已保留。'; return
  }
  try {
    if (payload.edits.length) {
      busy.value = true
      const lines = applySupplementEdits(job.value.requirements, payload.edits)
      await storeLines(lines, `用户补充${new Set(payload.edits.map(edit => edit.lineId)).size}个项目的信息`)
      savedFields = true
      supplementPanel.value?.acceptFields()
      ElMessage.success('项目补充已保存')
      busy.value = false
    }
    if (payload.text || payload.files.length) {
      const accepted = await sendChat({ text: payload.text, files: payload.files }, {
        autoContinue: payload.resume, onFilesAccepted: () => supplementPanel.value?.acceptFiles(),
      })
      if (!accepted) {
        if (savedFields && error.value) error.value = `字段已保存；文字或附件未完成处理：${error.value}`
        return
      }
      supplementPanel.value?.acceptAll()
      if (proposal.value) return
    } else supplementPanel.value?.acceptAll()
    if (payload.resume && job.value?.status !== 'processing') {
      if (!(await continuePricing()) && savedFields) error.value = `补充已保存，继续报价失败：${error.value || '请重试'}`
    } else await refreshList()
  } catch (e) { error.value = `${savedFields ? '部分补充已保存；' : ''}${err(e)}，未提交内容仍保留在草稿中` }
  finally { busy.value = false }
}
function editReviewLine(id: string, match = false) {
  const line = job.value?.requirements.find(item => item.id === id)
  if (!line) return
  if (match) matchLine(line)
  else editLine(line)
}
async function stopAgent() {
  if (!job.value) return
  invalidateRequests()
  const id = job.value.id
  const sequence = navigationSequence
  try {
    const current = await api<Job>(`/jobs/${id}/agent/stop`, { method: 'POST',
      body: JSON.stringify({ revision: job.value.revision, reason: '管理员停止 Agent' }) })
    if (sequence === navigationSequence && job.value?.id === id) job.value = current
  } catch (e) { ElMessage.error(err(e)) }
}
async function recover() {
  if (!job.value) return
  invalidateRequests()
  const id = job.value.id
  const sequence = navigationSequence
  try {
    const current = await api<Job>(`/jobs/${id}/recover`, { method: 'POST',
      body: JSON.stringify({ revision: job.value.revision, reason: '管理员恢复中断识别' }) })
    if (sequence === navigationSequence && job.value?.id === id) job.value = current
  } catch (e) { ElMessage.error(err(e)) }
}
function editLine(line?: Line) {
  editingLine.value = line ? JSON.parse(JSON.stringify(line)) : newLine()
  if (!editingLine.value!.estimate) editingLine.value!.price_review_note = ''
  reason.value = ''; dialogError.value = ''; lineOpen.value = true
}
async function storeLines(lines: Line[], why: string) {
  if (!job.value) return
  job.value = await api<Job>(`/jobs/${job.value.id}/requirements`, { method: 'PUT',
    body: JSON.stringify({ revision: job.value.revision, lines, reason: why }) })
}
async function maintain(action: 'revalidate' | 'refresh_candidates' | 'retry_failed' | 'disable_auto_resume') {
  if (!job.value || busy.value) return
  const id = job.value.id
  const sequence = navigationSequence
  busy.value = true
  try {
    const result = await api<{ job: Job; maintenance: { notice?: string; reopened: string[]
      searches?: { candidates: unknown[] }[] } }>(`/jobs/${id}/maintenance`, {
      method: 'POST', body: JSON.stringify({ revision: job.value.revision, action,
        reason: { revalidate: '管理员重新校验当前报价', refresh_candidates: '管理员重新检索价格依据',
          retry_failed: '管理员恢复系统受阻项目', disable_auto_resume: '管理员关闭自动恢复' }[action] }),
    })
    if (sequence !== navigationSequence || job.value?.id !== id) return
    job.value = result.job
    selectedQuoteId.value = null
    ElMessage.success(result.maintenance.notice || (action === 'refresh_candidates'
      ? `已检索，找到 ${result.maintenance.searches?.reduce((sum, entry) => sum + entry.candidates.length, 0) || 0} 条候选依据`
      : action === 'retry_failed' ? `已恢复 ${result.maintenance.reopened.length} 项`
        : action === 'disable_auto_resume' ? '已关闭自动恢复' : '当前结果已重新校验'))
  } catch (e) { ElMessage.error(err(e)) }
  finally { busy.value = false }
}
async function saveLine(recalculate = false) {
  if (!editingLine.value || !job.value) return
  dialogError.value = ''
  if (reason.value.trim().length < 2) { dialogError.value = '请填写修改原因'; return }
  busy.value = true
  try {
    const value = JSON.parse(JSON.stringify(editingLine.value)) as Line
    for (const k of ['width_mm', 'height_mm', 'thickness_mm', 'depth_mm', 'billing_length_mm', 'manual_unit_price', 'area_m2', 'labor_hours'] as const) if (value[k] === '') value[k] = null
    for (const group of value.letter_groups || []) {
      for (const key of ['width_mm', 'height_mm', 'billing_length_mm'] as const) if (group[key] === '') group[key] = null
    }
    for (const component of value.components || []) {
      for (const key of ['width_mm', 'height_mm', 'thickness_mm', 'billing_length_mm', 'area_m2', 'labor_hours'] as const) if (component[key] === '') component[key] = null
    }
    for (const cost of value.estimate?.costs || []) {
      if (cost.rate_basis_amount === '') { cost.rate_basis_amount = null; cost.rate_basis_unit = null }
    }
    const existing = job.value.requirements.findIndex(line => line.id === value.id)
    const lines = [...job.value.requirements]
    if (existing >= 0) lines[existing] = value
    else lines.push(value)
    await storeLines(lines, reason.value)
    if (recalculate && job.value) {
      const id = job.value.id
      const quote = await api<Quote>(`/jobs/${id}/quotes`, { method: 'POST',
        body: JSON.stringify({ revision: job.value.revision,
          terms: job.value.quotes[0]?.terms || '补全方案计价，范围及商业条款按审核清单，待批准',
          rounding: job.value.quotes[0]?.payload.rounding || 'none' }) })
      await openJob(id)
      selectedQuoteId.value = quote.id
      workspaceView.value = 'results'; detailsOpen.value = false
      ElMessage.success(`已重新计价并生成 V${quote.version}`)
    } else ElMessage.success('需求已保存')
    lineOpen.value = false
  } catch (e) { dialogError.value = err(e) }
  finally { busy.value = false }
}
async function removeLine(line: Line) {
  try {
    const answer = await ElMessageBox.prompt('删除该需求会使现有报价失效。', '删除需求', {
      confirmButtonText: '删除', cancelButtonText: '取消', inputPlaceholder: '删除原因',
      inputValidator: v => (v || '').trim().length >= 2 || '请填写原因',
    })
    await storeLines(job.value!.requirements.filter(l => l.id !== line.id), answer.value)
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(err(e)) }
}
async function matchLine(line: Line) {
  matchingLine.value = line; candidateSearch.value = line.pricing_category || line.product; candidateOpen.value = true
  await searchCandidates()
}
async function searchCandidates() {
  if (!job.value || !matchingLine.value) return
  candidateLoading.value = true
  try {
    const result = await api<{ prices: typeof candidates.value; rules: Rule[] }>(
      `/jobs/${job.value.id}/lines/${matchingLine.value.id}/candidates?q=${encodeURIComponent(candidateSearch.value)}`)
    candidates.value = result.prices; candidateRules.value = result.rules
  } catch (e) { ElMessage.error(err(e)) }
  finally { candidateLoading.value = false }
}
async function choosePrice(price: Price) {
  if (!job.value || !matchingLine.value) return
  try {
    const result = await ElMessageBox.prompt(currentPolicy.value ? '确认规格、材质、工艺及费用范围，核对已含工序与排除费用。' : '确认规格、材质、工艺、税费及附加规则适用。附加费用需单独填写。', '价格适用性确认', {
      inputPlaceholder: '填写核对依据', confirmButtonText: '选择此价格', cancelButtonText: '取消',
      inputValidator: v => (v || '').trim().length >= 2 || '请填写核对依据',
    })
    const lines = job.value.requirements.map(line => line.id === matchingLine.value!.id ? {
      ...line, selected_price_id: price.id, selected_price_revision: price.revision, price_review_note: result.value,
    } : line)
    await storeLines(lines, `选择价格 #${price.id}：${result.value}`)
    candidateOpen.value = false; ElMessage.success('已关联价格')
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(err(e)) }
}
function startQuote() {
  terms.value = job.value?.quotes[0]?.terms || (currentPolicy.value ? businessTerms : '')
  quoteRounding.value = job.value?.quotes[0]?.payload.rounding || 'none'
  dialogError.value = ''; quoteOpen.value = true
}
async function calculate() {
  if (!job.value) return
  busy.value = true; dialogError.value = ''
  try {
    const quote = await api<Quote>(`/jobs/${job.value.id}/quotes`, { method: 'POST',
      body: JSON.stringify({ revision: job.value.revision, terms: terms.value, rounding: quoteRounding.value }) })
    await openJob(job.value.id)
    selectedQuoteId.value = quote.id; workspaceView.value = 'results'; detailsOpen.value = false; quoteOpen.value = false
  } catch (e) { dialogError.value = err(e) }
  finally { busy.value = false }
}
async function approve() {
  if (!selectedQuote.value || !job.value) return
  busy.value = true; dialogError.value = ''
  try {
    const quote = await api<Quote>(`/quotes/${selectedQuote.value.id}/approve`, { method: 'POST',
      body: JSON.stringify({ note: approvalNote.value, confirmed_commercial_terms: commercialConfirmed.value,
        confirmed_estimates: estimatesConfirmed.value,
        confirmed_source_review: sourcesConfirmed.value,
        terms: selectedQuote.value.payload.generated_by === 'agent' ? approvalTerms.value : undefined }) })
    job.value.quotes = job.value.quotes.map(q => q.id === quote.id ? quote : q)
    approvalOpen.value = false; ElMessage.success('报价已批准')
  } catch (e) { dialogError.value = err(e) }
  finally { busy.value = false }
}
async function download(audience: 'customer' | 'review' = 'customer') {
  if (!selectedQuote.value) return
  try {
    const blob = await apiBlob(`/quotes/${selectedQuote.value.id}/export?audience=${audience}`)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = `报价-${selectedQuote.value.id}-v${selectedQuote.value.version}-${audience === 'review' ? '内部审核' : '客户'}.csv`; a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) { ElMessage.error(err(e)) }
}
async function preview(file: CustomerFile, pageNumber = 1, region?: number[]) {
  previewFile.value = file; previewPage.value = pageNumber; previewOpen.value = true; previewLoading.value = true
  previewScale.value = 100; previewSource.value = null
  if (previewUrl.value) { URL.revokeObjectURL(previewUrl.value); previewUrl.value = '' }
  const sequence = ++previewSequence
  try {
    const area = region?.length === 4
      ? `/region?left=${region[0]}&top=${region[1]}&right=${region[2]}&bottom=${region[3]}` : ''
    const blob = await apiBlob(`/customer-files/${file.id}/pages/${pageNumber}${area}`)
    if (sequence !== previewSequence || disposed) return
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = URL.createObjectURL(blob)
    if (isExcel(file)) {
      const source = await api<ExcelSource>(`/customer-files/${file.id}/source/${pageNumber}`)
      if (sequence === previewSequence && !disposed) previewSource.value = source
    }
  } catch (e) { ElMessage.error(err(e)) }
  finally { if (sequence === previewSequence) previewLoading.value = false }
}
async function previewDrawing(url: string) {
  const sequence = ++previewSequence
  previewLoading.value = true
  try {
    const blob = await apiBlob(url.replace(/^\/api/, ''))
    if (sequence !== previewSequence || disposed) return
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = URL.createObjectURL(blob)
    previewScale.value = 100
  } catch (e) { ElMessage.error(err(e)) }
  finally { if (sequence === previewSequence) previewLoading.value = false }
}
function showEvidence(fileId: number, pageNumber: number, region?: number[]) {
  const file = job.value?.files.find(f => f.id === fileId)
  if (file) preview(file, pageNumber, region)
}
watch(() => job.value?.status, (status, before) => {
  if (before === 'processing' && status && status !== 'processing' && workspaceView.value === 'results'
    && job.value && pendingRows(job.value).length) workspaceView.value = 'supplement'
})
onMounted(() => {
  refresh()
  pollTimer = setInterval(pollProgress, 3000)
})
onBeforeUnmount(() => {
  disposed = true
  invalidateRequests()
  clearInterval(pollTimer)
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})
</script>

<template>
  <QuoteChat ref="chat" :job="job" :jobs="jobs" :total="total" :page="page" :busy="busy" :loading="loading"
    :configured="config.configured" :error="error" v-model:view="workspaceView" @send="sendChat" @open="openJob" @fresh="newConversation" @remove="deleteConversation"
    @settings="key = ''; dialogError = ''; keyOpen = true" @refresh="refresh" @stop="stopAgent" @recover="recover"
    @details="openDetails" @preview="preview" @search="query = $event; page = 1; refresh()"
    @page="page = $event; refresh()" @manual="startCreate" @dismiss-error="error = ''">
    <template #results>
      <QuoteResults v-if="job" :job="job" :busy="busy" :configured="config.configured" v-model:quote-id="selectedQuoteId" @maintain="maintain"
        @approve="approvalNote = ''; commercialConfirmed = false; estimatesConfirmed = false; sourcesConfirmed = false; approvalTerms = selectedQuote?.terms || ''; dialogError = ''; approvalOpen = true"
        @download="download" @edit="editReviewLine" @supplement="workspaceView = 'supplement'"
        @continue="continuePricing" @evidence="showEvidence" />
    </template>
    <template #supplement>
      <QuoteSupplement v-if="job" ref="supplementPanel" :job="job" :busy="busy" :configured="config.configured"
        @submit="saveSupplement" @continue="continuePricing" @edit="editReviewLine" @match="editReviewLine($event, true)" @preview="preview" />
    </template>
  </QuoteChat>
  <el-dialog v-model="proposalOpen" title="确认局部需求修改" width="min(720px, 96vw)" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @open="dialogError = ''" @closed="discardProposal">
    <div v-for="operation in proposal?.operations || []" :key="`${operation.line_id}:${operation.kind}`" class="proposal-change">
      <strong>{{ operationLabels[operation.kind] }} · {{ job?.requirements.find(line => line.id === operation.line_id)?.product || operation.new_line?.product }}</strong>
      <p>{{ operation.reason }}</p>
      <p v-if="operation.kind === 'add'">数量：{{ operation.new_line?.quantity ?? '待确认' }} · {{ operation.new_line?.material || '材质待确认' }}</p>
      <p v-if="operation.kind === 'estimate'">费用组成：{{ operation.estimate?.costs.map(cost => cost.label).join('、') || '待补全' }}</p>
      <p v-if="operation.kind === 'extras'">附加费用：{{ operation.extras?.map(extra => `${extra.label} ${extra.amount} 元`).join('、') }}</p>
      <details><summary>执行数据</summary><pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(operation, null, 2) }}</pre></details>
    </div>
    <div v-for="change in proposal?.changes || []" :key="`${change.line_id}:${change.target_id || ''}:${change.field}`" class="proposal-change">
      <strong>{{ proposalTarget(change).name }} · {{ fieldLabels[change.field] || change.field }}</strong>
      <div><span>原始：{{ proposalTarget(change).original ?? '未提供' }}</span><ArrowRight :size="16" /><span>修改为：{{ change.value ?? '未提供' }}</span></div>
      <p>{{ change.reason }}</p>
    </div>
    <div v-if="dialogError" class="form-error">{{ dialogError }}</div>
    <template #footer><el-button :disabled="busy" @click="discardProposal">取消修改</el-button><el-button type="primary" :loading="busy" @click="applyProposal">确认修改并重新计价</el-button></template>
  </el-dialog>
  <el-drawer v-model="detailsOpen" title="资料与执行记录" size="min(1080px, 100vw)">
  <div class="requirements-workspace">
    <div v-if="!config.configured" class="notice-strip"><KeyRound :size="16" /><span>DeepSeek API Key 尚未配置</span><el-button link type="primary" @click="key = ''; dialogError = ''; keyOpen = true">配置密钥</el-button></div>
    <div v-if="error" class="error-banner" role="alert">{{ error }}<button class="icon-button" aria-label="关闭提示" @click="error = ''"><X :size="15" /></button></div>

    <template v-if="!job">
      <div class="jobs-toolbar"><form class="search-box" @submit.prevent="page = 1; refresh()"><Search :size="16" /><input v-model="query" aria-label="搜索客户需求" placeholder="搜索项目或客户" /><button class="search-submit" aria-label="搜索需求" type="submit"><ArrowRight :size="16" /></button></form><span>{{ total }} 个任务</span></div>
      <el-table v-loading="loading" :data="jobs" empty-text="暂无客户需求" @row-dblclick="(row: Job) => openJob(row.id)">
        <el-table-column label="项目 / 客户" min-width="230"><template #default="{ row }"><button class="product-link" @click="openJob(row.id)">{{ row.title }}</button><div class="cell-secondary">{{ row.customer || '未指定客户' }} · #{{ row.id }}</div></template></el-table-column>
        <el-table-column label="状态" width="120"><template #default="{ row }"><span :class="['status-badge', row.status === 'ready' ? 'active' : 'draft']">{{ jobLabels[row.status] }}</span></template></el-table-column>
        <el-table-column label="资料 / 明细" width="140"><template #default="{ row }">{{ row.file_count }} 份文件 · {{ row.line_count }} 项</template></el-table-column>
        <el-table-column label="更新时间" min-width="180"><template #default="{ row }">{{ date(row.updated_at) }}</template></el-table-column>
        <el-table-column width="60" fixed="right"><template #default="{ row }"><button class="icon-button" aria-label="打开任务" @click="openJob(row.id)"><ChevronRight :size="17" /></button></template></el-table-column>
      </el-table>
      <div class="pagination-bar"><span>共 {{ total }} 个任务</span><el-pagination v-model:current-page="page" :total="total" :page-size="20" layout="prev, pager, next" @current-change="refresh" /></div>
    </template>
    <template v-else>
      <div class="job-meta"><span>{{ job.customer || '未指定客户' }}</span><span>需求版本 {{ job.revision }}</span><span :class="['status-badge', job.status === 'ready' ? 'active' : 'draft']">{{ jobLabels[job.status] }}</span></div>
      <div class="job-meta"><span>报价口径：{{ currentPolicy ? '2026-09-23 现场规则' : '历史口径' }}</span><el-button v-if="!currentPolicy" link :disabled="busy || job.status === 'processing'" @click="adoptPolicy">采用现场口径</el-button></div>
      <section class="customer-files"><div class="section-title"><h2>客户资料</h2><span>{{ job.files.reduce((sum, f) => sum + f.page_count, 0) }} 页</span></div><div class="file-list"><button v-for="file in job.files" :key="file.id" class="customer-file" @click="preview(file)"><component :is="isExcel(file) ? Table2 : file.media_type === 'application/pdf' ? FileText : FileImage" :size="22" /><div><strong>{{ file.filename }}</strong><small>{{ file.page_count }} 页 · {{ (file.byte_size / 1024).toFixed(0) }} KB</small><small v-if="file.resource_summary">图纸 {{ file.resource_summary.resolved }}/{{ file.resource_summary.discovered }}<span v-if="file.resource_summary.unresolved"> · {{ file.resource_summary.unresolved }} 项未读取</span></small></div><ChevronRight :size="15" /></button></div><p v-if="job.brief" class="brief-text">{{ job.brief }}</p></section>
      <div class="job-actions"><span>{{ job.extraction.summary || '客户资料' }}</span><el-button :disabled="busy || !config.configured || job.status === 'processing'" @click="startExtraction(false)">重新整理资料</el-button></div>
      <div v-if="job.runs[0]?.error" class="error-banner">{{ job.runs[0].error }}</div>
      <div v-if="job.extraction.questions?.length" class="questions-band"><h3>模型提出的待核实项</h3><ul><li v-for="question in job.extraction.questions" :key="question">{{ question }}</li></ul></div>
      <el-tabs v-model="detailTab">
        <el-tab-pane :label="`需求明细 (${job.requirements.length})`" name="requirements">
          <div class="detail-toolbar"><span>{{ job.pending_count }} 项待确认</span><el-button :disabled="busy || job.status === 'processing'" @click="editLine()"><Plus :size="15" />添加明细</el-button><el-button type="primary" :disabled="busy || !job.requirements.length || job.status === 'processing'" @click="startQuote">计算报价</el-button></div>
          <el-table class="requirements-desktop-table" :data="visibleRequirements" empty-text="暂无需求明细">
            <el-table-column label="产品 / 文字" min-width="185"><template #default="{ row }"><button class="product-link" @click="editLine(row)">{{ row.product || '产品待确认' }}</button><div class="cell-secondary">{{ row.text_content || '—' }}</div><span :class="['status-badge', row.confirmed ? 'active' : 'draft']">{{ row.confirmed ? '已核实' : '待核实' }}</span></template></el-table-column>
            <el-table-column label="规格 / 材质" min-width="170"><template #default="{ row }">{{ row.width_mm || '?' }} × {{ row.height_mm || '?' }} mm<div class="cell-secondary">{{ row.material || '材质未指定' }} {{ row.thickness_mm ? `${row.thickness_mm}mm` : '' }} · {{ row.process || '工艺未指定' }}</div></template></el-table-column>
            <el-table-column label="数量" width="80"><template #default="{ row }">{{ row.quantity ?? '待确认' }}</template></el-table-column>
            <el-table-column label="类型 / 工艺难度" min-width="140"><template #default="{ row }">{{ row.type_note || '未标注' }}<div class="cell-secondary">{{ row.difficulty === 'unknown' ? '难度待核实' : row.difficulty }}</div></template></el-table-column>
            <el-table-column label="价格关联" min-width="150"><template #default="{ row }"><el-button link type="primary" :disabled="busy || job.status === 'processing'" @click="matchLine(row)"><Link :size="14" />{{ row.selected_price_id ? `#${row.selected_price_id} · v${row.selected_price_revision}` : '匹配价格' }}</el-button></template></el-table-column>
            <el-table-column label="来源" min-width="120"><template #default="{ row }"><button v-for="(e, index) in row.evidence" :key="index" class="source-link evidence-link" @click="showEvidence(e.file_id, e.page)">{{ e.sheet ? `${e.sheet}!${e.cell}` : `文件 ${e.file_id} · ${e.page} 页` }}</button><el-tooltip v-if="row.text_evidence?.length" :content="row.text_evidence.join('；')"><span class="cell-secondary">客户对话文字</span></el-tooltip><span v-else-if="!row.evidence.length" class="cell-secondary">人工录入</span></template></el-table-column>
            <el-table-column width="80" fixed="right"><template #default="{ row }"><button class="icon-button" aria-label="编辑需求" :disabled="busy || job.status === 'processing'" @click="editLine(row)"><Pencil :size="15" /></button><button class="icon-button danger" aria-label="删除需求" :disabled="busy || job.status === 'processing'" @click="removeLine(row)"><Trash2 :size="15" /></button></template></el-table-column>
          </el-table>
          <div class="requirements-mobile-list"><article v-for="line in visibleRequirements" :key="line.id"><div class="mobile-line-heading"><strong>{{ line.product || '产品待确认' }}</strong><span :class="['status-badge', line.confirmed ? 'active' : 'draft']">{{ line.confirmed ? '已核实' : '待核实' }}</span><button class="icon-button" aria-label="编辑需求" :disabled="busy || job.status === 'processing'" @click="editLine(line)"><Pencil :size="15" /></button><button class="icon-button danger" aria-label="删除需求" :disabled="busy || job.status === 'processing'" @click="removeLine(line)"><Trash2 :size="15" /></button></div><p>{{ line.width_mm || '?' }} × {{ line.height_mm || '?' }} mm · {{ line.quantity ?? '?' }} 件</p><p>{{ line.material || '材质未指定' }} {{ line.thickness_mm ? `${line.thickness_mm}mm` : '' }} · {{ line.process || '工艺未指定' }}</p><p v-if="line.text_content">{{ line.text_content }}</p><el-button link type="primary" :disabled="busy || job.status === 'processing'" @click="matchLine(line)"><Link :size="14" />{{ line.selected_price_id ? `价格 #${line.selected_price_id} · v${line.selected_price_revision}` : '匹配价格' }}</el-button><button v-for="e in line.evidence" :key="`${e.file_id}-${e.page}`" class="source-link evidence-link" @click="showEvidence(e.file_id, e.page)">来源：文件 {{ e.file_id }} · 第 {{ e.page }} 页</button></article><el-empty v-if="!job.requirements.length" description="暂无需求明细" /></div>
          <el-pagination v-if="job.requirements.length > 50" v-model:current-page="requirementPage" :total="job.requirements.length" :page-size="50" layout="total, prev, pager, next" />
        </el-tab-pane>
        <el-tab-pane label="执行记录" name="execution">
          <PipelineAudit v-if="detailsOpen && detailTab === 'execution'" :job-id="job.id" :revision="job.revision" :disabled="job.status === 'processing'" @preview="showEvidence" />
          <details v-for="run in job.agent_runs" :key="run.id" class="result-secondary-section"><summary>{{ date(run.created_at) }} · {{ run.status }} · {{ run.steps.length }} 个步骤</summary><p>{{ run.message }}</p><details v-for="(step, index) in run.steps" :key="index" class="agent-step"><summary>{{ index + 1 }} · {{ step.tool }}</summary><pre>{{ JSON.stringify({ arguments: step.arguments, result: step.result }, null, 2) }}</pre></details></details>
        </el-tab-pane>
        <el-tab-pane label="补充与识别记录" name="history"><div v-for="m in job.messages" :key="m.at" class="message-row"><strong>{{ m.role === 'user' ? '补充需求' : '模型整理' }}</strong><span>{{ date(m.at) }}</span><p>{{ m.text }}</p></div><div v-for="run in job.runs" :key="run.id" class="message-row"><strong>识别 #{{ run.id }} · {{ run.status }}</strong><span>{{ date(run.created_at) }}</span><p>{{ run.model }} · {{ run.attempts }} 次调用 · {{ run.usage.total_tokens || 0 }} tokens</p><p v-if="run.error" class="warning-text">{{ run.error }}</p></div><el-empty v-if="!job.messages.length && !job.runs.length" description="暂无识别记录" /></el-tab-pane>
      </el-tabs>
    </template>
  </div>
  </el-drawer>

  <el-dialog v-model="createOpen" title="新建客户需求" width="670px" :close-on-click-modal="false">
    <el-form label-position="top">
      <div class="form-grid"><el-form-item label="任务名称" required><el-input v-model="createForm.title" maxlength="150" /></el-form-item><el-form-item label="客户名称"><el-input v-model="createForm.customer" maxlength="150" /></el-form-item></div>
      <el-form-item label="客户补充说明"><el-input v-model="createForm.brief" type="textarea" :rows="3" maxlength="8000" /></el-form-item>
      <el-form-item label="报价资料"><input ref="fileInput" type="file" hidden multiple :accept="attachmentAccept" @change="chooseFiles" /><el-button @click="fileInput?.click()"><Upload :size="16" />选择文件</el-button><span class="upload-limits">最多 {{ uploadLimits.max_files }} 个文件 · PDF {{ uploadLimits.max_pdf_bytes / 1024 / 1024 }}MB/份 · 其他 {{ uploadLimits.max_file_bytes / 1024 / 1024 }}MB/份 · 合计 {{ uploadLimits.max_job_bytes / 1024 / 1024 }}MB / {{ uploadLimits.max_pages }} 页</span></el-form-item>
      <div v-for="(file, index) in files" :key="index" class="upload-row"><FileText :size="16" /><span>{{ file.name }}</span><small>{{ (file.size / 1024).toFixed(0) }} KB</small><button class="icon-button" aria-label="移除文件" @click="files.splice(index, 1)"><X :size="15" /></button></div>
      <div class="consent-fields"><el-checkbox v-model="autoRun" :disabled="!config.configured">上传后自动报价，并授权将客户资料及候选报价数据发送至 {{ externalProviders }}</el-checkbox></div>
      <div v-if="dialogError" class="form-error" role="alert">{{ dialogError }}</div>
    </el-form>
    <template #footer><el-button @click="createOpen = false">取消</el-button><el-button type="primary" :loading="busy" @click="createJob">{{ autoRun ? '上传并自动报价' : '保存资料' }}</el-button></template>
  </el-dialog>

  <el-dialog v-model="keyOpen" title="模型配置" width="min(540px, 96vw)" @closed="key = ''">
    <el-tabs v-model="configTab" @tab-change="key = ''; dialogError = ''">
      <el-tab-pane label="DeepSeek · 报价" name="quote" />
      <el-tab-pane label="硅基流动 · 检索" name="embedding" />
    </el-tabs>
    <div class="model-info"><strong>{{ currentKeyConfig.model }}</strong><span :class="['status-badge', currentKeyConfig.configured ? 'active' : 'draft']">{{ currentKeyConfig.configured ? '已配置' : '未配置' }}</span></div>
    <el-form label-position="top"><el-form-item label="API Key"><el-input v-model="key" type="password" show-password autocomplete="off" :disabled="currentKeyConfig.environment_managed" :placeholder="currentKeyConfig.configured ? '输入新密钥以替换' : 'sk-...'" /></el-form-item><div v-if="dialogError" class="form-error">{{ dialogError }}</div></el-form>
    <template #footer><el-button v-if="currentKeyConfig.configured && !currentKeyConfig.environment_managed" type="danger" plain @click="clearKey">删除密钥</el-button><el-button :disabled="currentKeyConfig.environment_managed" type="primary" :loading="busy" @click="saveKey">保存密钥</el-button></template>
  </el-dialog>

  <el-dialog v-model="extractOpen" :title="agentMode ? 'Agent 自动报价' : 'DeepSeek 需求整理'" width="660px" :close-on-click-modal="false"><el-form label-position="top"><el-form-item label="本次补充 / 客户回答"><el-input v-model="supplementary" type="textarea" :rows="5" maxlength="8000" /></el-form-item><div class="consent-fields"><el-checkbox v-model="consent">同意将本任务图片、需求、补充说明及候选报价数据发送至 {{ agentMode ? externalProviders : 'DeepSeek' }}</el-checkbox><el-checkbox v-if="job?.requirements.length" v-model="replace">重新识别将替换当前需求，清除价格关联，并使原报价失效</el-checkbox></div><div v-if="dialogError" class="form-error">{{ dialogError }}</div></el-form><template #footer><el-button @click="extractOpen = false">取消</el-button><el-button type="primary" :loading="busy" @click="extract"><Sparkles :size="16" />{{ agentMode ? '开始自动报价' : '开始识别' }}</el-button></template></el-dialog>

  <el-dialog v-model="lineOpen" title="需求明细" width="820px" :close-on-click-modal="false"><el-form v-if="editingLine" label-position="top">
    <div class="form-grid"><el-form-item label="报价类别"><el-select v-model="editingLine.pricing_category" filterable clearable><el-option v-for="product in pricingProducts" :key="product.id" :value="product.name" :label="product.name" /></el-select></el-form-item><el-form-item label="类别认定依据"><el-input v-model="editingLine.category_basis" maxlength="1500" /></el-form-item></div>
    <div class="form-grid"><el-form-item label="人工不含税单价（元/件）"><el-input v-model="editingLine.manual_unit_price" inputmode="decimal" clearable @clear="editingLine.manual_unit_price = null" /></el-form-item><el-form-item label="人工补价依据及包含范围"><el-input v-model="editingLine.manual_price_note" maxlength="2000" /></el-form-item></div>
    <div class="form-grid"><el-form-item label="产品名称"><el-input v-model="editingLine.product" maxlength="150" /></el-form-item><el-form-item label="文字内容"><el-input v-model="editingLine.text_content" maxlength="1000" /></el-form-item><el-form-item label="材质"><el-input v-model="editingLine.material" maxlength="100" /></el-form-item><el-form-item label="厚度 (mm)"><el-input v-model="editingLine.thickness_mm" inputmode="decimal" /></el-form-item><el-form-item label="单件宽 (mm)"><el-input v-model="editingLine.width_mm" inputmode="decimal" /></el-form-item><el-form-item label="单件高 (mm)"><el-input v-model="editingLine.height_mm" inputmode="decimal" /></el-form-item><el-form-item label="单件数量"><el-input-number v-model="editingLine.quantity" :min="1" :max="1000000" :precision="0" /></el-form-item><el-form-item label="文字类型"><el-select v-model="editingLine.language"><el-option v-for="(label, value) in languageLabels" :key="value" :value="value" :label="label" /></el-select></el-form-item><el-form-item label="工艺"><el-input v-model="editingLine.process" maxlength="255" /></el-form-item><el-form-item label="每件计价长度 (mm)"><el-input v-model="editingLine.billing_length_mm" inputmode="decimal" placeholder="仅按 cm / m 计价时填写" /></el-form-item></div>
    <BusinessFields v-if="currentPolicy" :line="editingLine" />
    <QuantitySources :line="editingLine" />
    <el-button v-if="!editingLine.estimate && currentPolicy" @click="editingLine.estimate = { assumptions: [], costs: [], scope: businessTerms, contract_version: 3, pricing_strategy: 'auto' }"><Plus :size="14" />建立费用方案</el-button>
    <EstimateEditor v-if="editingLine.estimate" :plan="editingLine.estimate" :line="editingLine" :current-policy="currentPolicy" />
    <el-form-item label="备注 / 不确定项"><el-input v-model="editingLine.notes" type="textarea" :rows="2" /><ul v-if="editingLine.uncertainties.length" class="blocker-list"><li v-for="u in editingLine.uncertainties" :key="u">{{ u }}</li></ul></el-form-item>
    <div class="section-title"><h3>人工确认附加费用</h3><el-button link type="primary" @click="editingLine.extras.push({ label: '', amount: '0', basis: 'total', reason: '' })"><Plus :size="14" />添加费用</el-button></div>
    <div v-for="(charge, index) in editingLine.extras" :key="index" class="extra-row"><el-input v-model="charge.label" placeholder="费用名称" aria-label="费用名称" /><el-input v-model="charge.amount" placeholder="金额" aria-label="费用金额" inputmode="decimal" /><el-select v-model="charge.basis"><el-option label="整行一次" value="total" /><el-option label="每件" value="per_piece" /></el-select><el-select v-model="charge.kind" aria-label="附加费用类别"><el-option v-for="(label, value) in currentPolicy ? includedCostKinds : costKinds" :key="value" :value="value" :label="label" /></el-select><el-input v-model="charge.reason" placeholder="计价依据" aria-label="费用依据" /><button class="icon-button danger" aria-label="删除附加费" @click="editingLine.extras.splice(index, 1)"><Trash2 :size="15" /></button></div>
    <el-form-item label="修改原因" required><el-input v-model="reason" maxlength="2000" /></el-form-item><el-checkbox v-model="editingLine.confirmed">已核实产品、数量、尺寸和工艺，不确定项已澄清</el-checkbox><div v-if="dialogError" class="form-error">{{ dialogError }}</div>
  </el-form><template #footer><el-button @click="lineOpen = false">取消</el-button><el-button :type="editingLine?.estimate ? 'default' : 'primary'" :loading="busy" @click="saveLine()"><Save :size="15" />保存明细</el-button><el-button v-if="editingLine?.estimate" type="primary" :loading="busy" @click="saveLine(true)"><RefreshCw :size="15" />保存并重新计价</el-button></template></el-dialog>

  <el-drawer v-model="candidateOpen" title="匹配报价库" size="min(950px, 100vw)"><form class="search-box" @submit.prevent="searchCandidates"><Search :size="16" /><input v-model="candidateSearch" placeholder="产品名称或材质" aria-label="搜索候选价格" /><button type="submit" class="search-submit" aria-label="执行价格匹配"><ArrowRight :size="16" /></button></form><el-tabs><el-tab-pane :label="`候选价格 (${candidates.length})`"><el-table v-loading="candidateLoading" :data="candidates" empty-text="没有匹配价格，请调整搜索词或在报价库中新增"><el-table-column label="产品 / 规格" min-width="205"><template #default="{ row }"><strong>{{ row.product }}</strong><div class="cell-secondary">{{ row.spec }} · {{ row.material }} {{ row.thickness_mm }} · {{ languageLabels[row.language] }}</div><div class="cell-secondary">{{ row.process }} {{ row.quality }}</div><span v-if="row.mismatches.length" class="warning-text">{{ row.mismatches.join('；') }}</span><p class="cell-secondary">{{ row.notes }}</p></template></el-table-column><el-table-column label="单价" width="130" align="right"><template #default="{ row }">{{ row.amount }}<div class="cell-secondary">{{ unitLabels[row.unit] }}</div><span :class="['status-badge', row.status]">{{ statusLabels[row.status] }}</span></template></el-table-column><el-table-column label="来源" min-width="130"><template #default="{ row }"><span class="cell-secondary">{{ row.source ? `${row.source.sheet}!${row.source.cell}` : '手动录入' }}</span><div class="cell-secondary">#{{ row.id }} · v{{ row.revision }}</div></template></el-table-column><el-table-column width="80" fixed="right"><template #default="{ row }"><el-button link type="primary" :disabled="row.status !== 'active' || row.mismatches.length > 0" @click="choosePrice(row)">选择</el-button></template></el-table-column></el-table></el-tab-pane><el-tab-pane :label="`相关规则原文 (${candidateRules.length})`"><article v-for="rule in candidateRules" :key="rule.id" class="rule-reference"><h3>{{ rule.name }}</h3><p>{{ rule.content }}</p><span class="cell-secondary">{{ rule.source_cell }} · {{ statusLabels[rule.status] }}</span></article></el-tab-pane></el-tabs></el-drawer>

  <el-dialog v-model="quoteOpen" title="计算报价草稿" width="650px"><el-form label-position="top"><el-form-item :label="currentPolicy ? '报价条款：排除费用范围、有效期' : '报价条款：税费、运费、安装费、有效期'" required><el-input v-model="terms" type="textarea" :rows="6" maxlength="4000" /></el-form-item><el-form-item label="整单商业取整"><el-select v-model="quoteRounding" aria-label="整单商业取整"><el-option label="不取整，保留分" value="none" /><el-option label="四舍五入到元（需审核）" value="yuan" /><el-option label="四舍五入到十元（需审核）" value="ten" /><el-option label="四舍五入到百元（需审核）" value="hundred" /></el-select></el-form-item><div v-if="dialogError" class="form-error">{{ dialogError }}</div></el-form><template #footer><el-button @click="quoteOpen = false">取消</el-button><el-button type="primary" :loading="busy" @click="calculate">计算草稿</el-button></template></el-dialog>
  <el-dialog v-model="approvalOpen" title="批准报价" width="580px" @open="approvalTerms = selectedQuote?.payload.business_rules ? businessTerms : ''; estimatesConfirmed = false; sourcesConfirmed = false"><el-form label-position="top"><el-form-item label="审核说明" required><el-input v-model="approvalNote" aria-label="审核说明" type="textarea" :rows="3" /></el-form-item><el-form-item v-if="selectedQuote?.payload.generated_by === 'agent'" :label="selectedQuote.payload.business_rules ? '正式条款：排除费用范围及有效期' : '正式条款：税费、运费、安装费及有效期'" required><el-input v-model="approvalTerms" aria-label="正式报价条款" type="textarea" :rows="4" maxlength="4000" /></el-form-item><el-checkbox v-if="selectedQuote?.payload.has_estimates" v-model="estimatesConfirmed">已逐项审核并确认补全参数、估算费用和报价范围</el-checkbox><el-checkbox v-if="selectedQuote?.payload.source_review?.required" v-model="sourcesConfirmed">已核对未读清区域、跨文件归属和订购范围，确认无漏项或重复计费</el-checkbox><el-checkbox v-model="commercialConfirmed">{{ selectedQuote?.payload.business_rules ? '已确认价格适用性、排除费用范围、取整策略及条款' : '已确认价格适用性、税费、运费、安装费及条款' }}</el-checkbox><div v-if="dialogError" class="form-error">{{ dialogError }}</div></el-form><template #footer><el-button @click="approvalOpen = false">取消</el-button><el-button type="primary" :loading="busy" @click="approve"><Check :size="15" />批准</el-button></template></el-dialog>
  <el-dialog v-model="previewOpen" :title="previewFile?.filename" width="1000px"><div class="preview-toolbar"><button class="icon-button bordered" aria-label="上一页" :disabled="previewPage <= 1" @click="previewFile && preview(previewFile, previewPage - 1)"><ChevronLeft :size="17" /></button><span>{{ previewPage }} / {{ previewFile?.page_count }}</span><button class="icon-button bordered" aria-label="下一页" :disabled="previewPage >= (previewFile?.page_count || 1)" @click="previewFile && preview(previewFile, previewPage + 1)"><ChevronRight :size="17" /></button><el-tooltip content="缩小图纸"><button class="icon-button bordered" aria-label="缩小图纸" :disabled="previewScale <= 50" @click="previewScale = Math.max(50, previewScale - 25)"><ZoomOut :size="17" /></button></el-tooltip><span>{{ previewScale }}%</span><el-tooltip content="放大图纸"><button class="icon-button bordered" aria-label="放大图纸" :disabled="previewScale >= 400" @click="previewScale = Math.min(400, previewScale + 25)"><ZoomIn :size="17" /></button></el-tooltip></div><div v-loading="previewLoading" class="customer-preview zoom-preview"><img v-if="previewUrl" :src="previewUrl" :style="{ width: `${previewScale}%`, maxWidth: 'none' }" :alt="`${previewFile?.filename} 第 ${previewPage} 页`" /></div><section v-if="previewSource" class="excel-source"><h3>{{ previewSource.sheet }} · 原始行 {{ previewSource.row_start }}–{{ previewSource.row_end }}</h3><p v-if="previewSource.images.length">图纸 {{ previewSource.images.length }} 张</p><div class="source-image-actions"><el-button v-for="(image, index) in previewSource.images" :key="image.url" @click="previewDrawing(image.url)"><FileImage :size="14" />图纸 {{ image.cell }}{{ previewSource.images.length > 1 ? ` · ${index + 1}` : '' }}</el-button><el-button @click="previewFile && preview(previewFile, previewPage)">整页资料</el-button></div><p v-for="warning in previewSource.warnings" :key="warning">{{ warning }}</p><details><summary>单元格原文与公式缓存</summary><p>{{ previewSource.notice }}</p><div v-for="row in previewSource.rows" :key="row.row" class="excel-source-row"><span v-for="cell in row.cells" :key="cell.cell"><strong>{{ cell.cell }}</strong> {{ cell.value }}<small v-if="cell.formula">{{ cell.formula }} · {{ cell.formula_status === 'missing_cache' ? '无缓存值' : '缓存值未验证' }}</small></span></div></details></section></el-dialog>
</template>
