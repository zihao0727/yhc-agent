<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowRight, ArrowUp, Check, CircleAlert, FileImage, FileText, History, KeyRound, ListChecks, LoaderCircle, MessageSquare, Paperclip, Plus, RefreshCw, Search, Square, Trash2, X } from 'lucide-vue-next'
import { jobLabels, quoteDisplayTotal, type Job, type CustomerFile } from './requirements-types'
import QuotePricingDetails from './QuotePricingDetails.vue'
import CaseReferences from './CaseReferences.vue'
import AgentStepDetails from './AgentStepDetails.vue'

const props = defineProps<{
  job: Job | null; jobs: Job[]; total: number; page: number; busy: boolean; loading: boolean
  configured: boolean; error: string
}>()
const emit = defineEmits<{
  send: [payload: { text: string; files: File[] }]
  open: [id: number]; fresh: []; settings: []; refresh: []; stop: []; recover: []
  details: [tab: string]; preview: [file: CustomerFile]; search: [query: string]; page: [page: number]
  editEstimate: [lineId: string]
  manual: []
  remove: [job: Job]
}>()
const text = ref('')
const attachments = ref<File[]>([])
const attachmentError = ref('')
const historyOpen = ref(false)
const search = ref('')
const expandedRuns = ref(new Set<number>())
function toggleRun(id: number, event: Event) {
  if ((event.target as HTMLDetailsElement).open) expandedRuns.value.add(id)
  else expandedRuns.value.delete(id)
}
const input = ref<HTMLInputElement>()
const scroll = ref<HTMLElement>()
const lastRun = computed(() => props.job?.agent_runs[0])
const running = computed(() => props.busy || props.job?.status === 'processing')
const clock = ref(Date.now())
const clockTimer = setInterval(() => { clock.value = Date.now() }, 1000)
onBeforeUnmount(() => clearInterval(clockTimer))
const phaseLabels: Record<string, string> = {
  queued: '排队中', running: '正在计价', extracting: '正在整理需求', retrying: '等待重试',
  waiting: '需要人工介入', failed: '执行失败', succeeded: '报价待审核', cancelled: '已停止',
}
const phase = computed(() => lastRun.value?.status !== 'processing' ? lastRun.value?.status
  : lastRun.value?.phase || lastRun.value?.usage.phase || 'queued')
const phaseLabel = computed(() => props.busy && props.job?.status !== 'processing'
  ? '正在处理消息' : phaseLabels[phase.value || ''] || '正在处理')
const retrySeconds = computed(() => Math.max(0, Math.ceil(((lastRun.value?.usage.retry_at || 0) * 1000 - clock.value) / 1000)))
const progressAge = computed(() => {
  const at = lastRun.value?.usage.last_progress_at || lastRun.value?.usage.last_event_at
  return at ? Math.max(0, Math.floor((clock.value / 1000 - at) / 60)) : null
})
const reviewCount = computed(() => props.job?.quotes[0]?.payload.lines.filter(line =>
  line.estimated || line.blockers.length || line.warnings?.length).length || 0)
function stepSummary(step: Job['agent_runs'][number]['steps'][number]) {
  if (step.result.error) return String(step.result.error)
  if (step.result.saved) return `${step.result.product || step.arguments.line_id || '项目'} · 已保存 · ${money(step.result.amount as string | null)}`
  if (step.tool === 'search_prices') return `${step.arguments.query || '全部产品'} · ${step.result.total ?? 0} 条结果`
  if (step.tool === 'response_recovery') return String(step.result.message || '本轮未执行，已保留进度')
  return ''
}
const statusLabels: Record<string, string> = { processing: '正在处理', succeeded: '报价已生成', waiting: '等待补充', failed: '执行失败', cancelled: '已停止', limited: '达到执行上限' }
const toolLabels: Record<string, string> = { analyze_components: '核对部件材料单价', finish_pending_quote: '生成待核价清单', context_compact: '压缩记录并自动继续', extract_requirements: '整理客户需求', search_prices: '查询报价库', evaluate_price: '核对价格条件', select_price: '关联适用单价', calculate_quote: '校验计算结果', finish_quote: '生成报价草稿', ask_user: '等待补充信息' }
const money = (value: string | null) => value === null ? '待确认' : `¥ ${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
Object.assign(toolLabels, { search_pricing_cases: '查询人工报价案例', evaluate_pricing_case: '复算人工报价公式' })
Object.assign(toolLabels, { complete_estimate: '自动补全并试算审核方案' })
Object.assign(toolLabels, { response_recovery: '响应异常，自动恢复' })
Object.assign(toolLabels, { extract_requirements_failed: '需求识别失败，已保留原数据' })
Object.assign(toolLabels, { read_requirement: '读取项目资料及已保存方案' })
const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
const initialFiles = computed(() => {
  const added = new Set(props.job?.messages.flatMap(m => m.file_ids || []))
  return props.job?.files.filter(f => !added.has(f.id)) || []
})
const events = computed(() => {
  if (!props.job) return []
  return [
    ...props.job.messages.map((message, i) => ({ key: `m${i}`, at: message.at, message, run: null as Job['agent_runs'][number] | null })),
    ...props.job.agent_runs.map(run => ({ key: `r${run.id}`, at: run.created_at, run, message: null as Job['messages'][number] | null })),
  ].sort((a, b) => a.at.localeCompare(b.at))
})
function addFiles(selected: File[]) {
  attachmentError.value = ''
  if (selected.length + attachments.value.length + (props.job?.files.length || 0) > 6) {
    attachmentError.value = '每个会话累计最多 6 个文件'; return
  }
  if (selected.some(f => !/\.(png|jpe?g|webp|pdf)$/i.test(f.name) || f.size > 20 * 1024 * 1024)) {
    attachmentError.value = '仅支持 20MB 以内的 JPG、PNG、WebP、PDF'; return
  }
  attachments.value.push(...selected)
}
function choose(event: Event) {
  const target = event.target as HTMLInputElement
  addFiles(Array.from(target.files || [])); target.value = ''
}
function paste(event: ClipboardEvent) {
  if (running.value) return
  const files = Array.from(event.clipboardData?.files || [])
  if (files.length) { event.preventDefault(); addFiles(files) }
}
function send() {
  if (running.value || !props.configured || (!text.value.trim() && !attachments.value.length)) return
  emit('send', { text: text.value.trim(), files: [...attachments.value] })
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send() }
}
function clearComposer() { text.value = ''; attachments.value = []; attachmentError.value = '' }
defineExpose({ clearComposer })
function fresh() { clearComposer(); historyOpen.value = false; emit('fresh') }
function open(id: number) { clearComposer(); historyOpen.value = false; emit('open', id) }
function messageFiles(ids: number[]) { return props.job?.files.filter(f => ids.includes(f.id)) || [] }
watch(() => props.job?.id, async () => {
  await nextTick()
  if (scroll.value) scroll.value.scrollTop = scroll.value.scrollHeight
})
watch(() => [props.job?.messages.length, props.job?.agent_runs[0]?.steps.length, props.job?.quotes.length], async () => {
  const nearBottom = !scroll.value || scroll.value.scrollHeight - scroll.value.scrollTop - scroll.value.clientHeight < 180
  await nextTick()
  if (nearBottom && scroll.value) scroll.value.scrollTop = scroll.value.scrollHeight
})
</script>

<template>
  <div class="quote-chat-layout">
    <aside :class="['chat-history', { 'is-open': historyOpen }]">
      <div class="chat-history-heading"><strong>报价会话</strong><button class="icon-button" aria-label="新对话" title="新对话" :disabled="busy" @click="fresh"><Plus :size="18" /></button><button class="icon-button history-close" aria-label="关闭会话列表" @click="historyOpen = false"><X :size="18" /></button></div>
      <form class="chat-search" @submit.prevent="emit('search', search)"><Search :size="15" /><input v-model="search" placeholder="搜索会话" aria-label="搜索会话" /><button class="icon-button" type="submit" aria-label="执行会话搜索"><ArrowRight :size="14" /></button></form>
      <div class="chat-history-items" v-loading="loading && !job">
        <div v-for="item in jobs" :key="item.id" :class="['chat-history-row', { selected: item.id === job?.id }]">
        <button class="chat-history-item" :disabled="busy" @click="open(item.id)">
          <MessageSquare :size="15" /><div><strong>{{ item.title }}</strong><small>{{ jobLabels[item.status] || item.status }}<template v-if="item.customer"> · {{ item.customer }}</template></small></div>
        </button>
        <button class="icon-button chat-history-delete" :aria-label="`删除会话：${item.title}`" :title="item.status === 'processing' ? '处理中，暂不可删除' : '删除会话'" :disabled="busy || item.status === 'processing'" @click="emit('remove', item)"><Trash2 :size="15" /></button>
        </div>
        <p v-if="!jobs.length" class="history-empty">{{ search ? '没有匹配的会话' : '暂无历史会话' }}</p>
      </div>
      <el-pagination v-if="total > 20" small :current-page="page" :total="total" :page-size="20" layout="prev, next" @current-change="emit('page', $event)" />
      <button class="chat-manual" :disabled="busy" @click="emit('manual')"><ListChecks :size="15" />新建项目表单</button>
    </aside>
    <section class="chat-main">
      <header class="chat-heading">
        <button class="icon-button history-toggle" aria-label="会话列表" @click="historyOpen = !historyOpen"><History :size="18" /></button>
        <div><h1>{{ job?.title || '报价助手' }}</h1><span v-if="lastRun || running"><i :class="{ connected: running && phase !== 'retrying' }"></i>{{ phaseLabel }}</span></div>
        <div class="chat-heading-actions">
          <button v-if="job" class="icon-button bordered" title="明细与报价" aria-label="明细与报价" @click="emit('details', 'requirements')"><ListChecks :size="17" /></button>
          <button class="icon-button bordered" title="DeepSeek 配置" aria-label="DeepSeek 配置" @click="emit('settings')"><KeyRound :size="17" /></button>
          <button class="icon-button bordered" title="新对话" aria-label="新对话" :disabled="busy" @click="fresh"><Plus :size="18" /></button>
        </div>
      </header>
      <section v-if="running && job" class="chat-progress-strip" aria-live="polite">
        <div><strong>{{ phaseLabel }}</strong><span v-if="lastRun?.active_product">{{ lastRun.active_product }}</span><span v-if="phase === 'retrying'">{{ retrySeconds > 0 ? `${retrySeconds} 秒后重试` : '等待后台重试' }}</span></div>
        <template v-if="job.pricing_progress">
          <progress :value="job.pricing_progress.priced_count" :max="Math.max(1, job.pricing_progress.line_count)" aria-label="已计价项目进度"></progress>
          <div><span>已计价 {{ job.pricing_progress.priced_count }} / {{ job.pricing_progress.line_count }} 项</span><strong>{{ money(job.pricing_progress.known_subtotal) }} <small>小计，含待审核估价</small></strong></div>
        </template>
        <small v-if="progressAge !== null">最近进展：{{ progressAge === 0 ? '刚刚' : `${progressAge} 分钟前` }}</small>
      </section>
      <div ref="scroll" class="chat-scroll" role="log" aria-label="报价对话">
        <div v-if="!job" class="chat-empty">
          <div class="chat-avatar"><MessageSquare :size="23" /></div><h2>这次需要报价什么？</h2>
        </div>
        <div v-else class="chat-transcript">
          <article class="chat-message user-message">
            <div class="message-author">你 <time>{{ time(job.created_at) }}</time></div>
            <p v-if="job.brief">{{ job.brief }}</p><p v-else>{{ job.title }}</p>
            <div class="chat-files"><button v-for="file in initialFiles" :key="file.id" @click="emit('preview', file)"><component :is="file.media_type === 'application/pdf' ? FileText : FileImage" :size="20" /><span>{{ file.filename }}<small>{{ file.page_count }} 页</small></span></button></div>
          </article>
          <template v-for="event in events" :key="event.key">
            <article v-if="event.message" :class="['chat-message', event.message.role === 'user' ? 'user-message' : 'assistant-message']">
              <div class="message-author">{{ event.message.role === 'user' ? '你' : '报价助手' }}<time>{{ time(event.at) }}</time></div>
              <p>{{ event.message.text }}</p>
              <div v-if="event.message.file_ids" class="chat-files"><button v-for="file in messageFiles(event.message.file_ids)" :key="file.id" @click="emit('preview', file)"><FileText :size="18" /><span>{{ file.filename }}</span></button></div>
            </article>
            <article v-else-if="event.run" class="chat-message assistant-message chat-run">
              <div class="message-author"><LoaderCircle v-if="event.run.status === 'processing'" class="spin" :size="15" /><Check v-else-if="event.run.status === 'succeeded'" :size="15" /><CircleAlert v-else :size="15" />{{ event.run.id === lastRun?.id ? phaseLabel : statusLabels[event.run.status] || event.run.status }}<time>{{ event.run.step_count ?? event.run.steps.length }} 步</time></div>
              <p v-if="event.run.message">{{ event.run.message }}</p>
              <details @toggle="toggleRun(event.run.id, $event)">
                <summary>查价与计价记录</summary>
                <template v-if="expandedRuns.has(event.run.id)"><div v-for="(step, index) in event.run.steps" :key="index" class="chat-tool-step"><span class="step-number">{{ index + 1 }}</span><div><strong>{{ toolLabels[step.tool] || step.tool }}</strong><p v-if="stepSummary(step)">{{ stepSummary(step) }}</p><p v-if="step.result.blockers">{{ Array.isArray(step.result.blockers) ? step.result.blockers.join('；') : step.result.blockers }}</p><AgentStepDetails :job-id="job.id" :run-id="event.run.id" :index="index" /></div></div></template>
              </details>
            </article>
          </template>
          <article v-if="job.quotes[0]" class="chat-message assistant-message chat-quote">
            <div class="message-author">{{ job.quotes[0].status === 'blocked' ? '待核价清单' : job.quotes[0].payload.has_estimates ? '总体报价' : '报价草稿' }} · V{{ job.quotes[0].version }}<span>{{ job.quotes[0].outdated ? '已过期' : job.quotes[0].status === 'approved' ? '已批准' : job.quotes[0].status === 'blocked' ? '需核实' : '待审核' }}</span></div>
            <details class="chat-pricing-details">
              <summary>报价明细 · {{ job.quotes[0].payload.lines.length }} 项</summary>
            <div v-for="line in job.quotes[0].payload.lines" :key="line.line_id" class="chat-quote-line"><div>{{ line.requirement.source_item ? `#${line.requirement.source_item} · ` : '' }}{{ line.product }}<small v-if="line.requirement.pricing_category">报价类别：{{ line.requirement.pricing_category }}</small><small>{{ line.requirement.width_mm || '?' }} × {{ line.requirement.height_mm || '?' }} mm · {{ line.requirement.quantity ?? '?' }} 件</small><small v-if="line.blockers.length && !job.quotes[0].payload.confirmation_items">{{ line.blockers.join('；') }}</small><QuotePricingDetails :line="line" /><CaseReferences v-if="line.case_references?.length" :cases="line.case_references" /></div><strong>{{ line.amount === null ? '金额待补充' : money(line.amount) }}</strong></div>
            </details>
            <div class="chat-quote-total"><span>{{ job.quotes[0].payload.complete ? job.quotes[0].payload.has_estimates && job.quotes[0].status !== 'approved' ? '总体报价（含暂定项，待审核）' : '合计' : quoteDisplayTotal(job.quotes[0].payload) === null ? '成品总价待补充' : '已计价成品小计' }}</span><strong>{{ money(quoteDisplayTotal(job.quotes[0].payload)) }}</strong></div>
            <p v-if="reviewCount" class="chat-review-summary">{{ reviewCount }} 项需要审核 · {{ job.quotes[0].payload.lines.length }} 项报价明细</p>
            <details v-if="job.quotes[0].payload.review_questions?.length"><summary>其他复核记录</summary><p>{{ job.quotes[0].payload.review_questions.join('；') }}</p></details>
            <el-button @click="emit('details', 'quotes')">查看与审核报价</el-button>
            <el-button v-if="!job.quotes[0].payload.complete" type="primary" @click="emit('details', 'review')">补充后继续</el-button>
          </article>
          <div v-if="running && !lastRun" class="chat-working"><LoaderCircle :size="16" class="spin" />正在整理需求…</div>
        </div>
      </div>
      <div class="chat-composer-area">
        <div v-if="error || attachmentError" class="form-error" role="alert">{{ attachmentError || error }}</div>
        <div v-if="!configured" class="chat-config-warning"><KeyRound :size="15" /><span>尚未配置 DeepSeek API Key</span><button @click="emit('settings')">配置</button></div>
        <div v-if="job?.status === 'processing'" class="chat-running-bar"><LoaderCircle :size="14" class="spin" /><span>{{ phaseLabel }}</span><button v-if="lastRun?.status === 'processing'" @click="emit('stop')">停止</button></div>
        <form class="chat-composer" @submit.prevent="send" @dragover.prevent @drop.prevent="!running && addFiles(Array.from($event.dataTransfer?.files || []))">
          <div v-if="attachments.length" class="composer-files"><div v-for="(file, index) in attachments" :key="index"><FileText :size="17" /><span>{{ file.name }}</span><button type="button" class="icon-button" aria-label="移除附件" :disabled="running" @click="attachments.splice(index, 1)"><X :size="14" /></button></div></div>
          <textarea v-model="text" aria-label="消息" :placeholder="job ? '输入问题或补充需求…' : '这次需要报价什么？'" :disabled="running" maxlength="8000" rows="2" @keydown="keydown" @paste="paste"></textarea>
          <div class="composer-toolbar"><input ref="input" type="file" multiple hidden accept=".jpg,.jpeg,.png,.webp,.pdf" @change="choose" /><button type="button" class="icon-button composer-attach" title="添加图片或 PDF" aria-label="添加附件" :disabled="running" @click="input?.click()"><Paperclip :size="19" /></button><button v-if="job && !running && !text.trim() && !attachments.length" type="button" class="chat-continue" :disabled="!configured" @click="emit('send', { text: '', files: [] })"><RefreshCw :size="14" />继续查价</button><button v-if="running && lastRun?.status === 'processing'" type="button" class="chat-send" aria-label="停止生成" title="停止生成" @click="emit('stop')"><Square :size="17" /></button><button v-else type="submit" class="chat-send" aria-label="发送消息" title="发送消息" :disabled="running || !configured || (!text.trim() && !attachments.length)"><ArrowUp :size="20" /></button></div>
        </form>
      </div>
    </section>
  </div>
</template>
