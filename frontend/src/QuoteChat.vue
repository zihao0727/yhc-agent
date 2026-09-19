<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ArrowRight, ArrowUp, Check, CircleAlert, FileImage, FileText, History, KeyRound, ListChecks, LoaderCircle, MessageSquare, Paperclip, Plus, RefreshCw, Search, Square, X } from 'lucide-vue-next'
import { jobLabels, quoteDisplayTotal, type Job, type CustomerFile } from './requirements-types'
import QuotePricingDetails from './QuotePricingDetails.vue'
import QuoteConfirmations from './QuoteConfirmations.vue'
import EstimateReviewList from './EstimateReviewList.vue'
import CaseReferences from './CaseReferences.vue'

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
}>()
const text = ref('')
const attachments = ref<File[]>([])
const consent = ref(false)
const attachmentError = ref('')
const historyOpen = ref(false)
const search = ref('')
const input = ref<HTMLInputElement>()
const scroll = ref<HTMLElement>()
const lastRun = computed(() => props.job?.agent_runs[0])
const running = computed(() => props.busy || props.job?.status === 'processing')
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
  if (running.value || !consent.value || !props.configured || (!text.value.trim() && !attachments.value.length)) return
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
watch(() => props.job?.id, async (id, previous) => {
  if (!props.busy || !id || previous) consent.value = false
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
        <button v-for="item in jobs" :key="item.id" :class="['chat-history-item', { selected: item.id === job?.id }]" :disabled="busy" @click="open(item.id)">
          <MessageSquare :size="15" /><div><strong>{{ item.title }}</strong><small>{{ jobLabels[item.status] || item.status }}<template v-if="item.customer"> · {{ item.customer }}</template></small></div>
        </button>
        <p v-if="!jobs.length" class="history-empty">{{ search ? '没有匹配的会话' : '暂无历史会话' }}</p>
      </div>
      <el-pagination v-if="total > 20" small :current-page="page" :total="total" :page-size="20" layout="prev, next" @current-change="emit('page', $event)" />
      <button class="chat-manual" :disabled="busy" @click="emit('manual')"><ListChecks :size="15" />新建项目表单</button>
    </aside>
    <section class="chat-main">
      <header class="chat-heading">
        <button class="icon-button history-toggle" aria-label="会话列表" @click="historyOpen = !historyOpen"><History :size="18" /></button>
        <div><h1>{{ job?.title || '报价助手' }}</h1><span><i :class="{ connected: configured }"></i>{{ running ? 'Agent 正在处理' : configured ? 'DeepSeek · 已配置' : 'DeepSeek · 未配置' }}</span></div>
        <div class="chat-heading-actions">
          <button v-if="job" class="icon-button bordered" title="明细与报价" aria-label="明细与报价" @click="emit('details', 'requirements')"><ListChecks :size="17" /></button>
          <button class="icon-button bordered" title="DeepSeek 配置" aria-label="DeepSeek 配置" @click="emit('settings')"><KeyRound :size="17" /></button>
          <button class="icon-button bordered" title="刷新对话" aria-label="刷新对话" @click="emit('refresh')"><RefreshCw :size="16" /></button>
          <button class="icon-button bordered" title="新对话" aria-label="新对话" :disabled="busy" @click="fresh"><Plus :size="18" /></button>
        </div>
      </header>
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
              <div class="message-author"><LoaderCircle v-if="event.run.status === 'processing'" class="spin" :size="15" /><Check v-else-if="event.run.status === 'succeeded'" :size="15" /><CircleAlert v-else :size="15" />{{ statusLabels[event.run.status] || event.run.status }}<time>{{ event.run.steps.length }} 步</time></div>
              <details :open="event.run.status === 'processing'">
                <summary>查价与计价记录</summary>
                <div v-for="(step, index) in event.run.steps" :key="index" class="chat-tool-step"><span class="step-number">{{ index + 1 }}</span><div><strong>{{ toolLabels[step.tool] || step.tool }}</strong><p v-if="step.tool === 'search_prices'">{{ step.arguments.query || '全部产品' }} · {{ step.result.total ?? 0 }} 条结果</p><p v-if="step.tool === 'response_recovery'">{{ step.result.message }}</p><p v-if="step.result.blockers">{{ Array.isArray(step.result.blockers) ? step.result.blockers.join('；') : step.result.blockers }}</p><p v-if="step.result.error">{{ step.result.error }}</p><details><summary>详情</summary><pre>{{ JSON.stringify({ input: step.arguments, output: step.result }, null, 2) }}</pre></details></div></div>
              </details>
            </article>
          </template>
          <article v-if="running && job.pricing_progress" class="chat-message assistant-message">
            <div class="message-author">整单计费进行中</div>
            <p>已计费 {{ job.pricing_progress.priced_count }} / {{ job.pricing_progress.line_count }} 项 · 已存入 Redis</p>
            <p>已计费小计（含待审核估价）：{{ money(job.pricing_progress.known_subtotal) }}</p>
            <p v-if="lastRun?.message">{{ lastRun.message }}</p>
          </article>
          <article v-if="job.quotes[0]" class="chat-message assistant-message chat-quote">
            <div class="message-author">{{ job.quotes[0].status === 'blocked' ? '待核价清单' : job.quotes[0].payload.has_estimates ? '总体报价' : '报价草稿' }} · V{{ job.quotes[0].version }}<span>{{ job.quotes[0].outdated ? '已过期' : job.quotes[0].status === 'approved' ? '已批准' : job.quotes[0].status === 'blocked' ? '需核实' : '待审核' }}</span></div>
            <div v-for="line in job.quotes[0].payload.lines" :key="line.line_id" class="chat-quote-line"><div>{{ line.requirement.source_item ? `#${line.requirement.source_item} · ` : '' }}{{ line.product }}<small v-if="line.requirement.pricing_category">报价类别：{{ line.requirement.pricing_category }}</small><small>{{ line.requirement.width_mm || '?' }} × {{ line.requirement.height_mm || '?' }} mm · {{ line.requirement.quantity ?? '?' }} 件</small><small v-if="line.blockers.length && !job.quotes[0].payload.confirmation_items">{{ line.blockers.join('；') }}</small><QuotePricingDetails :line="line" /><CaseReferences v-if="line.case_references?.length" :cases="line.case_references" /></div><strong>{{ line.amount === null ? '金额待补充' : money(line.amount) }}</strong></div>
            <div class="chat-quote-total"><span>{{ job.quotes[0].payload.complete ? job.quotes[0].payload.has_estimates && job.quotes[0].status !== 'approved' ? '总体报价（含暂定项，待审核）' : '合计' : quoteDisplayTotal(job.quotes[0].payload) === null ? '成品总价待补充' : '已计价成品小计' }}</span><strong>{{ money(quoteDisplayTotal(job.quotes[0].payload)) }}</strong></div>
            <EstimateReviewList :lines="job.quotes[0].payload.lines" :disabled="running || job.quotes[0].outdated" @edit="emit('editEstimate', $event)" />
            <QuoteConfirmations :items="job.quotes[0].payload.confirmation_items" />
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
        <div v-if="job?.status === 'processing'" class="chat-running-bar"><LoaderCircle :size="14" class="spin" /><span>正在整理需求并查询报价库</span><button v-if="lastRun?.status === 'processing'" @click="emit('stop')">停止</button><button @click="emit('recover')">恢复中断任务</button></div>
        <form class="chat-composer" @submit.prevent="send" @dragover.prevent @drop.prevent="!running && addFiles(Array.from($event.dataTransfer?.files || []))">
          <div v-if="attachments.length" class="composer-files"><div v-for="(file, index) in attachments" :key="index"><FileText :size="17" /><span>{{ file.name }}</span><button type="button" class="icon-button" aria-label="移除附件" :disabled="running" @click="attachments.splice(index, 1)"><X :size="14" /></button></div></div>
          <textarea v-model="text" aria-label="消息" :placeholder="job ? '补充需求，或回答报价助手的问题…' : '输入产品、尺寸、数量，或附上客户图片 / PDF…'" :disabled="running" maxlength="8000" rows="3" @keydown="keydown" @paste="paste"></textarea>
          <div class="composer-toolbar"><input ref="input" type="file" multiple hidden accept=".jpg,.jpeg,.png,.webp,.pdf" @change="choose" /><button type="button" class="icon-button" title="添加图片或 PDF" aria-label="添加附件" :disabled="running" @click="input?.click()"><Paperclip :size="19" /></button><span>图片 / PDF</span><button v-if="job && !running && !text.trim() && !attachments.length" type="button" class="chat-continue" :disabled="!consent || !configured" @click="emit('send', { text: '', files: [] })"><RefreshCw :size="14" />继续查价</button><button v-if="running && lastRun?.status === 'processing'" type="button" class="chat-send" aria-label="停止生成" title="停止生成" @click="emit('stop')"><Square :size="17" /></button><button v-else type="submit" class="chat-send" aria-label="发送消息" title="发送消息" :disabled="running || !configured || !consent || (!text.trim() && !attachments.length)"><ArrowUp :size="20" /></button></div>
        </form>
        <el-checkbox v-model="consent" :disabled="running" class="chat-consent">授权本次对话资料及候选报价发送至 DeepSeek；补充需求将重新识别并更新报价</el-checkbox>
      </div>
    </section>
  </div>
</template>
