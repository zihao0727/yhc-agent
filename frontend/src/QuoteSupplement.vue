<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown, ChevronRight, FileText, Link, Paperclip, Pencil, RefreshCw, Save, Search, X } from 'lucide-vue-next'
import type { CustomerFile, Job, PendingInput } from './requirements-types'
import { compactObjectName, inputLabel, isNumericField, itemSupplementFields, pendingPresentation, pendingRows, pendingSummary, supplementLabels, type PendingRow, type SupplementEdit, type SupplementField } from './quote-workspace'
import { lineSpecification, quantityUnit } from './quote-display'
import { attachmentAccept, validateAttachments } from './upload-limits'
import { questionParts } from './quote-confirmations'

const props = defineProps<{ job: Job; busy: boolean; configured: boolean }>()
const emit = defineEmits<{
  submit: [payload: { edits: SupplementEdit[]; text: string; files: File[]; resume: boolean }]
  continue: []
  edit: [id: string]; match: [id: string]; preview: [file: CustomerFile]
}>()
type AnswerDraft = { answer: string; fields: SupplementField[]; values: Partial<Record<SupplementField, string>>; ids: string[]
  defaults: Partial<Record<SupplementField, string>>; inputs: Record<string, string>; inputDefaults: Record<string, string> }
type Draft = { answers: Record<string, AnswerDraft>; note: string }
const draft = ref<Draft>({ answers: {}, note: '' })
const files = ref<File[]>([])
const fileDrafts = new Map<number, File[]>()
const search = ref('')
const filter = ref('all')
const openKey = ref('')
const localError = ref('')
const fileInput = ref<HTMLInputElement>()
const rows = computed(() => pendingRows(props.job))
const kinds = [
  { value: 'all', label: '全部' }, { value: 'information', label: '补充资料' },
  { value: 'pricing', label: '补充价格' }, { value: 'system', label: '系统待处理' },
]
const visible = computed(() => rows.value.filter(row => (filter.value === 'all' || row.kind === filter.value)
  && (!search.value || `${pendingSummary(row)} ${row.question} ${row.reason || ''} ${row.products.map(p => `${p.source_item} ${p.product}`).join(' ')}`.includes(search.value)))
  .sort((a, b) => ['information', 'pricing', 'system'].indexOf(a.kind) - ['information', 'pricing', 'system'].indexOf(b.kind)))
const locked = computed(() => props.busy)
const submitLocked = computed(() => locked.value || props.job.status === 'processing' || Boolean(props.job.extraction.incomplete))
const filled = computed(() => Object.values(draft.value.answers).filter(edited).length)
const hasDraft = computed(() => filled.value > 0 || Boolean(draft.value.note.trim()) || files.value.length > 0)
const orphanDrafts = computed(() => Object.entries(draft.value.answers).filter(([key, answer]) =>
  !rows.value.some(row => row.key === key) && edited(answer)))
function changed(value: string | undefined, previous: string | undefined) { return (value || '').trim() !== (previous || '').trim() }
function edited(answer: AnswerDraft) {
  return Boolean(answer.answer.trim()) || answer.fields.some(field => changed(answer.values[field], answer.defaults[field]))
    || Object.entries(answer.inputs).some(([key, value]) => changed(value, answer.inputDefaults[key]))
}
let draftJob = 0
function persist() {
  if (!draftJob) return
  try { sessionStorage.setItem(`firefly-supplement-${draftJob}`, JSON.stringify(draft.value)) }
  catch { /* Keep the in-memory draft if browser storage is full. */ }
}
watch(() => props.job.id, id => {
  persist()
  if (draftJob) fileDrafts.set(draftJob, files.value)
  draftJob = id
  let saved: Draft | undefined
  try {
    const raw = JSON.parse(sessionStorage.getItem(`firefly-supplement-${id}`) || 'null')
    if (raw && typeof raw.note === 'string' && raw.answers && typeof raw.answers === 'object') {
      saved = { note: raw.note, answers: {} }
      for (const [key, value] of Object.entries(raw.answers)) {
        const answer = value as AnswerDraft
        if (!answer || typeof answer.answer !== 'string' || !Array.isArray(answer.fields)
          || !Array.isArray(answer.ids) || !answer.values || typeof answer.values !== 'object') continue
        saved.answers[key] = {
          answer: answer.answer, ids: answer.ids.filter(id => typeof id === 'string'),
          fields: answer.fields.filter(field => Object.hasOwn(supplementLabels, field)),
          values: Object.fromEntries(Object.entries(answer.values).filter(([field, value]) =>
            Object.hasOwn(supplementLabels, field) && typeof value === 'string')),
          defaults: Object.fromEntries(Object.entries(answer.defaults || {}).filter(([field, value]) =>
            Object.hasOwn(supplementLabels, field) && typeof value === 'string')),
          inputs: Object.fromEntries(Object.entries(answer.inputs || {}).filter(([, value]) => typeof value === 'string')),
          inputDefaults: Object.fromEntries(Object.entries(answer.inputDefaults || {}).filter(([, value]) => typeof value === 'string')),
        }
      }
    }
  } catch { /* Ignore an invalid saved draft. */ }
  draft.value = saved || { answers: {}, note: '' }
  files.value = fileDrafts.get(id) || []
  search.value = ''; filter.value = 'all'; localError.value = ''; openKey.value = ''
}, { immediate: true })
watch(draft, persist, { deep: true, flush: 'sync' })
function answerFor(row: PendingRow): AnswerDraft {
  const answer = draft.value.answers[row.key] ||= {
    answer: '', fields: row.kind === 'pricing' ? [] : [...row.fields],
    values: {}, defaults: {}, inputs: {}, inputDefaults: {}, ids: row.products.map(p => p.line_id || p.product),
  }
  for (const field of answer.fields) {
    if (changed(answer.values[field], answer.defaults[field])) continue
    const values = answer.ids.map(id => props.job.requirements.find(line => line.id === id)?.[field as keyof typeof props.job.requirements[number]])
    const value = values.length && values.every(value => value === values[0]) && values[0] != null ? String(values[0]) : ''
    answer.values[field] = value; answer.defaults[field] = value
  }
  for (const input of scopedInputs(row)) {
    const key = inputKey(input)
    if (changed(answer.inputs[key], answer.inputDefaults[key])) continue
    const value = input.value != null && ['original', 'source'].includes(input.basis) ? input.value : ''
    answer.inputs[key] = value; answer.inputDefaults[key] = value
  }
  return answer
}
const inputKey = (input: PendingInput) => `${input.line_id}:${input.key}`
const scopedInputs = (row: PendingRow) => row.inputs.filter(input =>
  input.target !== 'item' && Object.hasOwn(supplementLabels, input.field))
function inputGroups(row: PendingRow, lineId: string) {
  const groups = new Map<string, { key: string; name: string; inputs: PendingInput[] }>()
  for (const input of scopedInputs(row).filter(input => input.line_id === lineId)) {
    const key = `${input.line_id}:${input.target}:${input.target_id}`
    const group = groups.get(key) || { key, name: input.target_name, inputs: [] }
    group.inputs.push(input)
    groups.set(key, group)
  }
  return [...groups.values()].map(group => ({ ...group, inputs: group.inputs.sort((a, b) => {
    const order = ['width_mm', 'height_mm', 'quantity_per_item', 'count']
    const position = (field: string) => order.includes(field) ? order.indexOf(field) : order.length
    return position(a.field) - position(b.field)
  }) }))
}
function rowTitle(row: PendingRow) {
  const titles = row.products.map(product => `${product.source_item ? `#${product.source_item} ` : ''}${product.product}`)
  return titles.slice(0, 2).join('、') || '整单事项'
}
function toggle(row: PendingRow) {
  answerFor(row)
  openKey.value = openKey.value === row.key ? '' : row.key
}
function known(id: string) {
  const line = props.job.requirements.find(line => line.id === id)
  if (!line) return ''
  return [
    lineSpecification(line), line.quantity != null ? `${line.quantity} ${quantityUnit(line)}` : '', line.process,
  ].filter(Boolean).join(' · ')
}
function placeholder(row: PendingRow, field: SupplementField) {
  const values = answerFor(row).ids.map(id =>
    (props.job.requirements.find(line => line.id === id) as unknown as Record<string, unknown> | undefined)?.[field])
  return values.length && values.every(value => value === values[0]) && values[0] != null && values[0] !== ''
    ? `当前：${values[0]}` : '未填写'
}
function chooseFiles(event: Event) {
  const target = event.target as HTMLInputElement
  const selected = Array.from(target.files || [])
  localError.value = validateAttachments([...files.value, ...selected], props.job.files.length,
    props.job.files.reduce((sum, file) => sum + file.byte_size, 0))
  if (!localError.value) files.value.push(...selected)
  target.value = ''
}
function submit(resume: boolean) {
  localError.value = ''
  if (resume && props.job.extraction.incomplete && !props.busy && props.job.status !== 'processing' && props.configured) {
    emit('continue'); return
  }
  if (submitLocked.value || (resume && !props.configured)) return
  if (orphanDrafts.value.length) { localError.value = '部分问题已更新，请先处理保留的草稿再提交'; return }
  const edits: SupplementEdit[] = []
  const replies: string[] = []
  for (const row of rows.value) {
    const answer = draft.value.answers[row.key]
    if (!answer || row.kind === 'system') continue
    const fields = answer.fields.filter(field => answer.values[field]?.trim() && changed(answer.values[field], answer.defaults[field]))
    const inputs = scopedInputs(row).filter(input => answer.inputs[inputKey(input)]?.trim()
      && changed(answer.inputs[inputKey(input)], answer.inputDefaults[inputKey(input)]))
    if (!answer.answer.trim() && !fields.length && !inputs.length) continue
    const targets = row.products.filter(product => answer.ids.includes(product.line_id || product.product))
    if (row.products.length && !targets.length) { localError.value = '请选择补充信息适用的项目'; return }
    for (const product of targets) {
      for (const field of fields) if (product.line_id) edits.push({ lineId: product.line_id, field, value: answer.values[field]! })
    }
    for (const input of inputs.filter(input => answer.ids.includes(input.line_id))) {
      edits.push({ lineId: input.line_id, field: input.field as SupplementField, value: answer.inputs[inputKey(input)]!,
        target: input.target, targetId: input.target_id || undefined, targetName: input.target_name })
      if (input.target === 'letter_group' && input.field !== 'count') {
        const count = scopedInputs(row).find(other => other.line_id === input.line_id
          && other.target_id === input.target_id && other.field === 'count')
        if (count && answer.inputs[inputKey(count)]?.trim()
          && !inputs.some(other => other.line_id === input.line_id && other.target_id === input.target_id && other.field === 'count')) {
          edits.push({ lineId: input.line_id, field: 'count', value: answer.inputs[inputKey(count)]!,
            target: input.target, targetId: input.target_id || undefined, targetName: input.target_name })
        }
      }
    }
    if (answer.answer.trim()) {
      const scope = targets.length ? targets.map(product =>
        `${product.source_item ? `#${product.source_item} ` : ''}${product.product}${product.line_id ? `（项目编号 ${product.line_id}）` : ''}`).join('、') : '整单'
      replies.push(`适用项目：${scope}\n待确认问题：${row.question}\n用户补充：${answer.answer.trim()}`)
    }
  }
  if (draft.value.note.trim()) replies.push(`整单补充：${draft.value.note.trim()}`)
  const text = replies.join('\n\n')
  if (text.length > 8000) { localError.value = '本次文字补充超过 8000 字，请分批提交'; return }
  if (!resume && !edits.length && !text && !files.value.length) { localError.value = '请先填写补充内容'; return }
  if ((text || files.value.length) && !props.configured) { localError.value = '文字和附件补充需要先配置报价模型'; return }
  emit('submit', { edits, text, files: [...files.value], resume })
}
function acceptFields() {
  for (const answer of Object.values(draft.value.answers)) {
    answer.values = {}; answer.defaults = {}; answer.inputs = {}; answer.inputDefaults = {}
  }
  persist()
}
function acceptAll() {
  draft.value = { answers: {}, note: '' }; files.value = []
  fileDrafts.delete(props.job.id); persist()
}
function acceptFiles() { files.value = []; fileDrafts.delete(props.job.id) }
function orphanQuestion(key: string) {
  try { return JSON.parse(key)[1] as string } catch { return '先前的补充内容' }
}
function recoverDraft(key: string, answer: AnswerDraft) {
  const projects = answer.ids.map(id => props.job.requirements.find(line => line.id === id))
    .filter(Boolean).map(line => `${line!.source_item ? `#${line!.source_item} ` : ''}${line!.product}`).join('、')
  const values = Object.entries(answer.values).filter(([field, value]) =>
    value?.trim() && changed(value, answer.defaults[field as SupplementField]))
    .map(([field, value]) => `${supplementLabels[field as SupplementField]}：${value}`)
  values.push(...Object.entries(answer.inputs).filter(([key, value]) => value?.trim() && changed(value, answer.inputDefaults[key]))
    .map(([key, value]) => `${key.split(':').slice(1, -1).join(' · ')} ${supplementLabels[key.split(':').at(-1) as SupplementField] || ''}：${value}`))
  draft.value.note = [draft.value.note, `先前补充${projects ? `（${projects}）` : ''}：${orphanQuestion(key)}`,
    ...values, answer.answer].filter(Boolean).join('\n')
  delete draft.value.answers[key]
}
defineExpose({ acceptFields, acceptAll, acceptFiles })
</script>

<template>
  <section class="supplement-workspace" aria-label="待补充">
    <div class="workbench-toolbar">
      <div class="workbench-filters" role="group" aria-label="待补充分类">
        <button v-for="kind in kinds" :key="kind.value" :class="{ selected: filter === kind.value }" :aria-pressed="filter === kind.value" @click="filter = kind.value">
          {{ kind.label }} <span>{{ kind.value === 'all' ? rows.length : rows.filter(row => row.kind === kind.value).length }}</span>
        </button>
      </div>
      <div class="workbench-search"><Search :size="16" /><input v-model="search" placeholder="搜索项目或问题" aria-label="搜索待补充项目" /></div>
    </div>
    <p v-if="job.extraction.incomplete" class="workbench-notice">资料尚未整理完整，请先继续整理。已填写的补充草稿会保留。</p>
    <details v-if="orphanDrafts.length" open class="result-secondary-section">
      <summary>{{ orphanDrafts.length }} 份草稿对应的问题已更新</summary>
      <div v-for="[key, answer] in orphanDrafts" :key="key" class="result-cost-row">
        <strong>{{ orphanQuestion(key) }}</strong><p>{{ answer.answer }}</p>
        <p v-for="(value, field) in answer.values" :key="field" v-show="value">{{ supplementLabels[field] }}：{{ value }}</p>
        <div><el-button :disabled="locked" @click="recoverDraft(key, answer)">移入整单补充</el-button><button class="icon-button" :disabled="locked" aria-label="删除过期问题草稿" title="删除此草稿" @click="delete draft.answers[key]"><X :size="15" /></button></div>
      </div>
    </details>
    <div v-if="!visible.length" class="workbench-empty">{{ rows.length ? '没有匹配的待补充事项' : '暂无待补充事项' }}</div>
    <article v-for="row in visible" :key="row.key" class="supplement-row">
      <button class="supplement-heading" :aria-expanded="openKey === row.key" @click="toggle(row)">
        <component :is="openKey === row.key ? ChevronDown : ChevronRight" :size="17" />
        <div>
          <strong>{{ rowTitle(row) }}<small v-if="row.products.length > 2" class="supplement-project-count">等 {{ row.products.length }} 项</small></strong>
          <span v-if="row.owner" class="cell-secondary">处理方：{{ row.owner }}</span>
          <span>{{ pendingSummary(row) }}</span>
        </div>
        <small :class="['workbench-status', row.kind]">{{ row.kind === 'system' ? '系统待处理' : row.kind === 'pricing' ? '补充价格' : '补充资料' }}</small>
        <small v-if="draft.answers[row.key] && edited(draft.answers[row.key]!)" class="draft-mark">已填写</small>
      </button>
      <div v-if="openKey === row.key" class="supplement-body">
        <p v-if="pendingPresentation(row).reason" class="supplement-question">{{ pendingPresentation(row).reason }}</p>
        <template v-if="!scopedInputs(row).length">
          <p v-for="(part, index) in questionParts(pendingSummary(row))" :key="index" class="supplement-question">{{ part }}</p>
        </template>
        <template v-if="row.kind !== 'system'">
          <div v-if="row.products.length && !scopedInputs(row).length" class="supplement-targets">
            <el-checkbox v-for="product in row.products" :key="product.line_id || product.product" v-model="answerFor(row).ids" :value="product.line_id || product.product" :disabled="locked" @change="answerFor(row)">
              {{ product.source_item ? `#${product.source_item} ` : '' }}{{ product.product }}
              <span class="cell-secondary">{{ known(product.line_id) }}</span>
            </el-checkbox>
          </div>
          <el-form label-position="top" :disabled="locked">
            <div v-if="scopedInputs(row).length" class="supplement-scoped-fields">
              <section v-for="product in row.products" :key="product.line_id" class="supplement-project">
                <header class="supplement-project-header">
                  <el-checkbox v-model="answerFor(row).ids" :value="product.line_id || product.product" :disabled="locked" @change="answerFor(row)">
                    {{ product.source_item ? `#${product.source_item} ` : '' }}{{ product.product }}
                  </el-checkbox>
                  <span class="cell-secondary">{{ known(product.line_id) }}</span>
                </header>
                <section v-for="group in inputGroups(row, product.line_id)" :key="group.key" class="supplement-object-group">
                  <strong :title="group.name">{{ compactObjectName(group.name) }}</strong>
                  <div class="supplement-object-field-grid">
                    <div v-for="input in group.inputs" :key="inputKey(input)" class="supplement-object-field">
                      <el-form-item>
                        <template #label><span>{{ inputLabel(input) }}</span><small :class="['supplement-input-state', input.missing || ['derived', 'assumption'].includes(input.basis) ? 'missing' : 'known']">{{ input.missing ? '待补充' : ['derived', 'assumption'].includes(input.basis) ? '待确认' : input.value != null ? '已知' : '' }}</small></template>
                        <el-input v-model="answerFor(row).inputs[inputKey(input)]"
                          :aria-label="`${input.target_name}：${inputLabel(input)}`"
                          :disabled="locked || !answerFor(row).ids.includes(input.line_id)"
                          :inputmode="isNumericField(input.field as SupplementField) ? 'decimal' : 'text'"
                          :placeholder="input.value != null ? `方案采用：${input.value}${['derived', 'assumption'].includes(input.basis) ? '（待确认）' : ''}` : '未提供'"
                          maxlength="1000" />
                      </el-form-item>
                    </div>
                  </div>
                  <details v-if="compactObjectName(group.name) !== group.name" class="supplement-object-description">
                    <summary>部件说明</summary><p>{{ group.name }}</p>
                  </details>
                </section>
              </section>
            </div>
            <details v-if="row.products.some(p => p.line_id)" class="supplement-other-fields" :open="!scopedInputs(row).length || answerFor(row).fields.length > 0">
              <summary>{{ scopedInputs(row).length ? '补充其他信息' : '补充项目资料' }}</summary>
              <el-form-item label="本次补充字段">
                <el-select v-model="answerFor(row).fields" multiple clearable :aria-label="`补充字段：${row.products.map(p => p.product).join('、')}`" @change="answerFor(row)">
                  <el-option v-for="[field, label] in itemSupplementFields" :key="field" :value="field" :label="label" />
                </el-select>
              </el-form-item>
              <div class="supplement-field-grid">
                <el-form-item v-for="field in answerFor(row).fields" :key="field" :label="supplementLabels[field]">
                  <el-input v-model="answerFor(row).values[field]" :aria-label="`${row.products.map(p => p.product).join('、')}：${supplementLabels[field]}`"
                    :inputmode="isNumericField(field) ? 'decimal' : 'text'" :placeholder="placeholder(row, field)" :maxlength="isNumericField(field) ? 30 : field === 'manual_price_note' ? 2000 : 1000" />
                </el-form-item>
              </div>
            </details>
            <el-form-item label="补充说明">
              <el-input v-model="answerFor(row).answer" type="textarea" :rows="3" :maxlength="4000" :aria-label="`补充说明：${row.products.map(p => p.product).join('、') || '整单'}`" />
            </el-form-item>
          </el-form>
          <div class="supplement-secondary">
            <template v-for="product in row.products" :key="product.line_id">
              <el-button v-if="product.line_id" link :disabled="submitLocked" @click="emit('edit', product.line_id)"><Pencil :size="14" />{{ row.products.length > 1 ? `${product.product} · ` : '' }}完整编辑</el-button>
              <el-button v-if="product.line_id && row.kind === 'pricing'" link :disabled="submitLocked" @click="emit('match', product.line_id)"><Link :size="14" />匹配库内价格</el-button>
            </template>
          </div>
        </template>
        <template v-else>
          <div v-if="row.products.length > 2" class="supplement-affected-projects"><span v-for="product in row.products" :key="product.line_id">{{ product.source_item ? `#${product.source_item} ` : '' }}{{ product.product }}</span></div>
          <p class="cell-secondary">由{{ row.owner || '报价管理员' }}核对后继续报价</p>
        </template>
        <details v-if="pendingPresentation(row).technical || scopedInputs(row).length" class="result-secondary-section">
          <summary>技术详情</summary>
          <pre class="supplement-technical">{{ pendingPresentation(row).technical || row.question }}</pre>
        </details>
      </div>
    </article>
    <section class="supplement-general">
      <label for="supplement-order-note">整单补充</label>
      <el-input id="supplement-order-note" v-model="draft.note" :disabled="locked" type="textarea" :rows="2" maxlength="4000" aria-label="整单补充" />
      <div class="supplement-attachments">
        <input ref="fileInput" type="file" multiple hidden :accept="attachmentAccept" @change="chooseFiles" />
        <button class="icon-button bordered" title="添加补充附件" aria-label="添加补充附件" :disabled="locked" @click="fileInput?.click()"><Paperclip :size="17" /></button>
        <div v-for="(file, index) in files" :key="index" class="supplement-file"><FileText :size="15" /><span>{{ file.name }}</span><button class="icon-button" aria-label="移除补充附件" :disabled="locked" @click="files.splice(index, 1)"><X :size="14" /></button></div>
      </div>
      <p v-if="files.length && job.requirements.length" class="workbench-notice">新增附件需要重新整理需求，提交前会确认影响范围。</p>
    </section>
    <div v-if="localError" class="form-error" role="alert">{{ localError }}</div>
    <footer class="supplement-footer">
      <span>{{ job.status === 'processing' ? `${hasDraft ? `${filled} 类已填写 · ` : ''}报价进行中 · 补充草稿已保留` : hasDraft ? `${filled} 类问题已填写 · 草稿已保留` : `${rows.length} 类待处理问题` }}</span>
      <el-button :disabled="submitLocked || !hasDraft || files.length > 0" :loading="busy" @click="submit(false)"><Save :size="15" />保存补充</el-button>
      <el-button type="primary" :disabled="busy || job.status === 'processing' || !configured" :loading="busy" @click="submit(true)"><RefreshCw :size="15" />{{ job.extraction.incomplete ? '继续整理资料' : hasDraft ? '保存补充并继续报价' : '继续报价' }}</el-button>
    </footer>
  </section>
</template>
