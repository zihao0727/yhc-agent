<script setup lang="ts">
import { attachmentAccept, validateAttachments } from './upload-limits'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowRight, ArrowUp, FileImage, FileText, FileSpreadsheet, History, KeyRound, ListChecks, MessageSquare, MoreHorizontal, Paperclip, Plus, RefreshCw, Square, Trash2, X } from 'lucide-vue-next'
import { jobLabels, type Job, type CustomerFile } from './requirements-types'
import { pendingRows, quoteSummary } from './quote-workspace'
import { sourceStage } from './quote-display'

type WorkspaceView = 'results' | 'supplement' | 'conversation'
const props = defineProps<{
  job: Job | null; jobs: Job[]; total: number; page: number; busy: boolean; loading: boolean
  configured: boolean; error: string; view: WorkspaceView
}>()
const emit = defineEmits<{
  send: [payload: { text: string; files: File[] }]
  open: [id: number]; fresh: []; settings: []; refresh: []; stop: []; recover: []
  details: [tab: string]; preview: [file: CustomerFile]; search: [query: string]; page: [page: number]
  'update:view': [view: WorkspaceView]; dismissError: []
  manual: []; remove: [job: Job]
}>()
const text = ref('')
const attachments = ref<File[]>([])
const attachmentError = ref('')
const historyOpen = ref(false)
const search = ref('')
const input = ref<HTMLInputElement>()
const scroll = ref<HTMLElement>()
const lastRun = computed(() => props.job?.agent_runs[0])
const reading = computed(() => props.job ? sourceStage(props.job) : null)
const running = computed(() => props.busy || props.job?.status === 'processing')
const composerBlocked = computed(() => props.busy || (props.job?.status === 'processing' && !lastRun.value?.usage.prepared))
const phaseLabels: Record<string, string> = {
  queued: '排队中', rendering: '正在生成预览', running: '正在计价', extracting: '正在整理需求', retrying: '等待重试',
  recovering: '正在核对资料', waiting: '有待补充事项', failed: '执行失败', succeeded: '报价待审核', cancelled: '已停止',
  source_reading: 'AI正在阅读并比对', source_review: 'AI正在复核资料',
  repairing: '正在修复执行问题',
}
const phase = computed(() => lastRun.value?.status !== 'processing' ? lastRun.value?.status
  : lastRun.value?.phase || lastRun.value?.usage.phase || 'queued')
const phaseLabel = computed(() => props.busy && props.job?.status !== 'processing' ? '正在保存'
  : reading.value ? reading.value.title
  : lastRun.value?.usage.extraction_only && phase.value === 'succeeded' ? '需求待确认'
    : phaseLabels[phase.value || ''] || jobLabels[props.job?.status || ''] || '待上传资料')
const extractionProgress = computed(() => lastRun.value?.usage.extraction_progress)
const recognizing = computed(() => Boolean(extractionProgress.value) && !lastRun.value?.usage.prepared)
const summary = computed(() => props.job ? quoteSummary(props.job) : null)
const pending = computed(() => props.job ? pendingRows(props.job).length : 0)
const clock = ref(Date.now())
const clockTimer = setInterval(() => { clock.value = Date.now() }, 1000)
onBeforeUnmount(() => clearInterval(clockTimer))
const retrySeconds = computed(() => Math.max(0, Math.ceil(((lastRun.value?.usage.retry_at || 0) * 1000 - clock.value) / 1000)))
const money = (value: string | null) => value === null ? reading.value ? '待核对' : '待计价' : `¥ ${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
const initialFiles = computed(() => {
  const added = new Set(props.job?.messages.flatMap(message => message.file_ids || []))
  return props.job?.files.filter(file => !added.has(file.id)) || []
})
function addFiles(selected: File[]) {
  attachmentError.value = validateAttachments([...attachments.value, ...selected], props.job?.files.length || 0,
    props.job?.files.reduce((sum, file) => sum + file.byte_size, 0) || 0)
  if (!attachmentError.value) attachments.value.push(...selected)
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
  if (composerBlocked.value || !props.configured || (!text.value.trim() && !attachments.value.length)) return
  emit('send', { text: text.value.trim(), files: [...attachments.value] })
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send() }
}
function clearComposer() { text.value = ''; attachments.value = []; attachmentError.value = '' }
defineExpose({ clearComposer })
function fresh() { clearComposer(); historyOpen.value = false; emit('fresh') }
function open(id: number) { clearComposer(); historyOpen.value = false; emit('open', id) }
function messageFiles(ids: number[]) { return props.job?.files.filter(file => ids.includes(file.id)) || [] }
function messageText(value: string) { return value.replace(/（项目编号 [a-zA-Z0-9_-]{1,50}）/g, '') }
function menu(command: string) {
  if (command === 'settings') emit('settings')
  if (command === 'refresh') emit('refresh')
  if (command === 'recover') emit('recover')
}
watch(() => [props.job?.id, props.job?.messages.length, props.view], async () => {
  const nearBottom = !scroll.value || scroll.value.scrollHeight - scroll.value.scrollTop - scroll.value.clientHeight < 180
  await nextTick()
  if (nearBottom && scroll.value) scroll.value.scrollTop = scroll.value.scrollHeight
})
</script>

<template>
  <div class="quote-chat-layout">
    <button v-if="historyOpen" class="history-backdrop" aria-label="关闭会话列表遮罩" @click="historyOpen = false"></button>
    <aside :class="['chat-history', { 'is-open': historyOpen }]">
      <div class="chat-history-heading"><strong>报价会话</strong><button class="icon-button" aria-label="新对话" title="新对话" :disabled="busy" @click="fresh"><Plus :size="18" /></button><button class="icon-button history-close" aria-label="关闭会话列表" @click="historyOpen = false"><X :size="18" /></button></div>
      <form class="chat-search" @submit.prevent="emit('search', search)"><MessageSquare :size="15" /><input v-model="search" placeholder="搜索会话" aria-label="搜索会话" /><button class="icon-button" type="submit" aria-label="执行会话搜索"><ArrowRight :size="14" /></button></form>
      <div class="chat-history-items" v-loading="loading && !job">
        <div v-for="item in jobs" :key="item.id" :class="['chat-history-row', { selected: item.id === job?.id }]">
          <button class="chat-history-item" :data-job-id="item.id" :disabled="busy" @click="open(item.id)"><MessageSquare :size="15" /><div><strong>{{ item.title }}</strong><small>{{ jobLabels[item.status] || item.status }}<template v-if="item.customer"> · {{ item.customer }}</template></small></div></button>
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
        <div><h1>{{ job?.title || '报价助手' }}</h1><span v-if="job"><i :class="{ connected: running && phase !== 'retrying' }"></i>{{ phaseLabel }}</span></div>
        <div class="chat-heading-actions">
          <button v-if="lastRun?.status === 'processing'" class="icon-button bordered" title="停止报价" aria-label="停止报价" @click="emit('stop')"><Square :size="16" /></button>
          <button v-if="job" class="icon-button bordered" title="资料与执行记录" aria-label="资料与执行记录" @click="emit('details', 'requirements')"><FileText :size="17" /></button>
          <el-dropdown @command="menu"><button class="icon-button bordered" aria-label="报价设置" title="报价设置"><MoreHorizontal :size="18" /></button><template #dropdown><el-dropdown-menu><el-dropdown-item command="settings">模型配置</el-dropdown-item><el-dropdown-item command="refresh">刷新数据</el-dropdown-item><el-dropdown-item v-if="job?.status === 'processing' && !lastRun?.usage.durable" command="recover">恢复中断任务</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
        </div>
      </header>
      <div v-if="summary" class="workbench-summary" aria-label="订单汇总">
        <span>共 <strong>{{ summary.count }}</strong> 项</span><span>已计价 <strong>{{ summary.priced }}</strong></span><span>待处理 <strong>{{ summary.pending }}</strong></span>
        <span class="workbench-summary-amount">{{ summary.outdated && !job?.pricing_progress ? '历史金额（已失效）' : summary.complete ? '合计' : '已计价小计' }} <strong>{{ money(summary.amount) }}</strong></span>
      </div>
      <section v-if="job?.status === 'processing'" class="chat-progress-strip" aria-live="polite">
        <div><span>{{ lastRun?.active_product || (recognizing ? '正在整理资料' : '正在计算报价') }}</span><span v-if="phase === 'retrying'">{{ retrySeconds > 0 ? `${retrySeconds} 秒后重试` : '等待后台重试' }}</span></div>
        <template v-if="recognizing && extractionProgress"><progress :value="extractionProgress.completed_units ?? extractionProgress.completed_pages ?? 0" :max="Math.max(1, extractionProgress.total_units ?? extractionProgress.total_pages ?? 0)" aria-label="资料识别进度"></progress><small>已整理 {{ extractionProgress.completed_units ?? extractionProgress.completed_pages ?? 0 }} / {{ extractionProgress.total_units ?? extractionProgress.total_pages ?? 0 }} {{ extractionProgress.unit || '页' }}</small></template>
        <progress v-else-if="job.pricing_progress" :value="job.pricing_progress.priced_count" :max="Math.max(1, job.pricing_progress.line_count)" aria-label="已计价项目进度"></progress>
      </section>
      <div v-if="error" class="workbench-feedback form-error" role="alert"><span>{{ error }}</span><button class="icon-button" aria-label="关闭报价提示" @click="emit('dismissError')"><X :size="15" /></button></div>
      <div v-if="job && !configured" class="workbench-feedback chat-config-warning"><KeyRound :size="15" /><span>报价模型尚未配置</span><button @click="emit('settings')">配置</button></div>
      <nav v-if="job" class="workbench-tabs" role="tablist" aria-label="报价工作区">
        <button id="workbench-results-tab" role="tab" :aria-selected="view === 'results'" aria-controls="workbench-results" @click="emit('update:view', 'results')">报价结果</button>
        <button id="workbench-supplement-tab" role="tab" :aria-selected="view === 'supplement'" aria-controls="workbench-supplement" @click="emit('update:view', 'supplement')">待补充 <span v-if="pending">{{ pending }} 类</span></button>
        <button id="workbench-conversation-tab" role="tab" :aria-selected="view === 'conversation'" aria-controls="workbench-conversation" @click="emit('update:view', 'conversation')">沟通记录</button>
      </nav>
      <div v-if="job" v-show="view === 'results'" id="workbench-results" class="workbench-panel" role="tabpanel" aria-labelledby="workbench-results-tab"><slot name="results" /></div>
      <div v-if="job" v-show="view === 'supplement'" id="workbench-supplement" class="workbench-panel" role="tabpanel" aria-labelledby="workbench-supplement-tab"><slot name="supplement" /></div>
      <div v-show="!job || view === 'conversation'" id="workbench-conversation" class="workbench-conversation" role="tabpanel" aria-labelledby="workbench-conversation-tab">
        <div ref="scroll" class="chat-scroll" role="log" aria-label="报价对话">
          <div v-if="!job" class="chat-empty"><div class="chat-avatar"><MessageSquare :size="23" /></div><h2>这次需要报价什么？</h2></div>
          <div v-else class="chat-transcript">
            <article class="chat-message user-message"><div class="message-author">你 <time>{{ time(job.created_at) }}</time></div><p>{{ job.brief || job.title }}</p><div class="chat-files"><button v-for="file in initialFiles" :key="file.id" @click="emit('preview', file)"><component :is="file.media_type.includes('spreadsheetml') ? FileSpreadsheet : file.media_type === 'application/pdf' ? FileText : FileImage" :size="20" /><span>{{ file.filename }}</span></button></div></article>
            <article v-for="(message, index) in job.messages" :key="`${message.at}:${index}`" :class="['chat-message', message.role === 'user' ? 'user-message' : 'assistant-message']">
              <div class="message-author">{{ message.role === 'user' ? '你' : '报价助手' }}<time>{{ time(message.at) }}</time><small v-if="message.pending_reply">等待处理</small></div><p>{{ messageText(message.text) }}</p>
              <div v-if="message.file_ids" class="chat-files"><button v-for="file in messageFiles(message.file_ids)" :key="file.id" @click="emit('preview', file)"><FileText :size="18" /><span>{{ file.filename }}</span></button></div>
            </article>
            <article v-if="lastRun && lastRun.status !== 'processing'" class="chat-message chat-run"><div class="message-author">最近处理结果</div><p>{{ lastRun.message }}</p><button class="source-link" @click="emit('update:view', pending ? 'supplement' : 'results')">{{ pending ? '查看待补充' : '查看报价结果' }}<ArrowRight :size="14" /></button></article>
          </div>
        </div>
        <div class="chat-composer-area">
          <div v-if="attachmentError" class="form-error" role="alert">{{ attachmentError }}</div>
          <div v-if="!configured" class="chat-config-warning"><KeyRound :size="15" /><span>报价模型尚未配置</span><button @click="emit('settings')">配置</button></div>
          <form class="chat-composer" @submit.prevent="send" @dragover.prevent @drop.prevent="!running && addFiles(Array.from($event.dataTransfer?.files || []))">
            <div v-if="attachments.length" class="composer-files"><div v-for="(file, index) in attachments" :key="index"><FileText :size="17" /><span>{{ file.name }}</span><button type="button" class="icon-button" aria-label="移除附件" :disabled="running" @click="attachments.splice(index, 1)"><X :size="14" /></button></div></div>
            <textarea v-model="text" aria-label="消息" :placeholder="job ? '补充需求或提出修改…' : '这次需要报价什么？'" :disabled="composerBlocked" maxlength="8000" rows="2" @keydown="keydown" @paste="paste"></textarea>
            <div class="composer-toolbar"><input ref="input" type="file" multiple hidden :accept="attachmentAccept" @change="choose" /><button type="button" class="icon-button composer-attach" title="添加报价资料" aria-label="添加附件" :disabled="running" @click="input?.click()"><Paperclip :size="19" /></button><button v-if="job && !running && !text.trim() && !attachments.length" type="button" class="chat-continue" :disabled="!configured" @click="emit('send', { text: '', files: [] })"><RefreshCw :size="14" />继续报价</button><button type="submit" class="chat-send" aria-label="发送消息" title="发送消息" :disabled="composerBlocked || !configured || (!text.trim() && !attachments.length)"><ArrowUp :size="20" /></button></div>
          </form>
        </div>
      </div>
    </section>
  </div>
</template>
