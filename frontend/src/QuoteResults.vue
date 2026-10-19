<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, ChevronRight, Download, FileText, Pencil, RefreshCw, Search } from 'lucide-vue-next'
import { costKinds, quoteDisplayTotal, rateUnits, type Job, type Quote, type QuoteLine } from './requirements-types'
import { lineNeedsReview, linePresentation, quoteSummary, resultStatus } from './quote-workspace'
import { currentWorkspace, lineSpecification, liveQuoteLines, quoteQuantity, quoteQuantityUnit, sourceDescription, sourceStage, sourceRemainder } from './quote-display'
import QuotePricingDetails from './QuotePricingDetails.vue'
import QuoteVersionDiff from './QuoteVersionDiff.vue'
import QuantitySources from './QuantitySources.vue'
import CaseReferences from './CaseReferences.vue'

const props = defineProps<{ job: Job; busy: boolean; quoteId: number | null; configured: boolean }>()
const emit = defineEmits<{
  'update:quoteId': [value: number | null]
  approve: []; download: [audience: 'customer' | 'review']; edit: [id: string]
  supplement: []; continue: []; evidence: [fileId: number, page: number]
  maintain: [action: 'revalidate' | 'refresh_candidates' | 'retry_failed' | 'disable_auto_resume']
}>()
const selected = computed(() => props.job.quotes.find(quote => quote.id === props.quoteId) || props.job.quotes[0])
const reading = computed(() => sourceStage(props.job))
const remainingSources = computed(() => sourceRemainder(props.job))
const pendingSources = computed(() => props.job.extraction.pricing_inventory?.pending_sources || [])
const live = computed(() => props.quoteId === null && (Boolean(reading.value) || props.job.status === 'processing'
  || !selected.value || Boolean(currentWorkspace(props.job))
    && !(selected.value.status === 'approved' && !selected.value.outdated)))
const displayLines = computed(() => live.value ? liveQuoteLines(props.job) : selected.value?.payload.lines || [])
const approved = computed(() => !live.value && selected.value?.status === 'approved')
const previous = computed(() => props.job.quotes.find(quote => quote.version < (selected.value?.version || 0)))
const search = ref('')
const filter = ref('all')
const page = ref(1)
const detailOpen = ref(false)
const detailId = ref('')
const detailTab = ref('costs')
const selectedLine = computed(() => displayLines.value.find(line => line.line_id === detailId.value))
const detailPresentation = computed(() => {
  const line = selectedLine.value
  if (!line) return null
  const current = live.value || selected.value?.id === props.job.quotes[0]?.id && !selected.value?.outdated
  const issue = current ? props.job.agent_runs[0]?.usage.deferred_requirements?.find(issue => issue.line_id === line.line_id) : undefined
  return linePresentation(line, issue)
})
const filters = [
  { value: 'all', label: '全部' }, { value: 'priced', label: '已计价' },
  { value: 'pending', label: '待处理' }, { value: 'review', label: '待审核' },
]
function matches(line: QuoteLine, value: string) {
  if (value === 'priced') return line.amount !== null
  if (value === 'pending') return line.amount === null
  if (value === 'review') return !approved.value && lineNeedsReview(line)
  return true
}
const visible = computed(() => displayLines.value.filter(line =>
  matches(line, filter.value) && (!search.value
    || `${line.product} ${line.requirement.source_item || ''} ${specs(line)}`.includes(search.value))))
const paged = computed(() => visible.value.slice((page.value - 1) * 50, page.value * 50))
const changedIds = computed(() => {
  if (live.value) return new Set<string>()
  const before = new Map(previous.value?.payload.lines.map(line => [line.line_id, line]) || [])
  return new Set(selected.value?.payload.lines.filter(line => {
    const old = before.get(line.line_id)
    return previous.value && (!old || old.amount !== line.amount || JSON.stringify(old.requirement) !== JSON.stringify(line.requirement))
  }).map(line => line.line_id))
})
const locked = computed(() => props.busy || props.job.status === 'processing')
const money = (value: string | null | undefined) => value == null ? reading.value && live.value ? '待核对' : '待计价'
  : `¥ ${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
function specs(line: QuoteLine) {
  return lineSpecification(line.effective_requirement || line.requirement)
}
const unit = quoteQuantityUnit
function showLine(line: QuoteLine) { detailId.value = line.line_id; detailTab.value = 'costs'; detailOpen.value = true }
function version(value: number) { emit('update:quoteId', value === 0 ? null : value) }
function totalLabel(quote: Quote) {
  if (quote.outdated) return '历史金额（已失效）'
  if (!quote.payload.complete) return '已计价小计（非完整报价）'
  return quote.status === 'approved' ? '报价合计' : quote.payload.has_estimates ? '报价合计（含估价，待审核）' : '报价合计（待审核）'
}
watch(() => props.job.id, () => { search.value = ''; filter.value = 'all'; page.value = 1; detailOpen.value = false })
watch([search, filter, () => selected.value?.id], () => { page.value = 1 })
watch(selectedLine, line => { if (!line) detailOpen.value = false })
</script>

<template>
  <section class="results-workspace" aria-label="报价结果">
    <section v-if="reading" class="source-reading-notice" role="status" aria-label="资料核对状态">
      <strong>{{ reading.title }}</strong>
      <p>已读 {{ reading.read }} / {{ reading.total }} 页 · 已复核 {{ reading.reviewed }} 页 · 临时明细 {{ job.requirements.length }} 项</p>
      <p>{{ reading.message }}</p>
      <el-button v-if="job.status !== 'processing'" type="primary" :disabled="busy || !configured" @click="emit('continue')"><RefreshCw :size="14" />继续核对资料</el-button>
    </section>
    <section v-else-if="remainingSources" class="source-reading-notice" role="status" aria-label="剩余资料核对">
      <strong>资料尚未核对完整</strong>
      <p>尚未读完 {{ remainingSources.unread }} 页 · 尚未复核 {{ remainingSources.unreviewed }} 页</p>
      <p>已计价小计仅包含当前项目，剩余资料可能增加或调整报价范围。</p>
      <el-button v-if="job.status !== 'processing'" type="primary" :disabled="busy || !configured" @click="emit('continue')"><RefreshCw :size="14" />继续核对资料</el-button>
    </section>
    <details v-if="!reading && pendingSources.length" class="source-reading-notice" aria-label="待归属图纸记录">
      <summary>另有 {{ pendingSources.length }} 条图纸观察待归属</summary>
      <p>当前按已明确的订购清单计价，以下图纸尚未确认是否另行订购。</p>
      <ul style="max-height: 180px; overflow: auto">
        <li v-for="entry in pendingSources" :key="entry.line_id">
          {{ entry.product }}
          <button v-for="source in entry.sources" :key="`${source.file_id}:${source.page}`"
            class="text-button" @click="emit('evidence', source.file_id, source.page)">第{{ source.page }}页</button>
        </li>
      </ul>
    </details>
    <template v-if="selected || job.requirements.length">
      <div class="result-actions">
        <el-select v-if="job.quotes.length" :model-value="live ? 0 : selected?.id" aria-label="报价版本" @update:model-value="version">
          <el-option v-if="currentWorkspace(job) || job.status === 'processing'" :value="0" label="当前计价结果" />
          <el-option v-for="quote in job.quotes" :key="quote.id" :value="quote.id" :label="`V${quote.version}${quote.outdated ? ' · 历史版本' : ''}`" />
        </el-select>
        <span v-if="live" class="workbench-status information">{{ reading ? '临时识别结果，待核对' : job.status === 'processing' ? '逐项计价中' : '当前计价结果' }}</span>
        <span v-else-if="selected" :class="['workbench-status', selected.outdated ? 'system' : approved ? 'priced' : 'information']">{{ selected.outdated ? '已失效' : approved ? '已批准' : '待审核' }}</span>
        <el-dropdown v-if="currentWorkspace(job)" :disabled="locked" @command="emit('maintain', $event)">
          <el-button :disabled="locked"><RefreshCw :size="15" />处理</el-button>
          <template #dropdown><el-dropdown-menu>
            <el-dropdown-item command="revalidate">重新校验当前结果</el-dropdown-item>
            <el-dropdown-item command="refresh_candidates">重新检索价格依据</el-dropdown-item>
            <el-dropdown-item command="retry_failed" :disabled="!configured">恢复系统受阻项目</el-dropdown-item>
            <el-dropdown-item v-if="job.quote_workspace?.actions.auto_resume" command="disable_auto_resume">关闭自动恢复</el-dropdown-item>
          </el-dropdown-menu></template>
        </el-dropdown>
        <div v-if="selected && !live" class="result-action-buttons">
          <el-button v-if="selected.status === 'draft'" type="primary" :disabled="locked || selected.outdated" @click="emit('approve')"><Check :size="15" />审核报价</el-button>
          <el-dropdown @command="emit('download', $event)" :disabled="locked || selected.outdated">
            <el-button :disabled="locked || selected.outdated"><Download :size="15" />导出</el-button>
            <template #dropdown><el-dropdown-menu><el-dropdown-item command="review">内部审核 CSV</el-dropdown-item><el-dropdown-item command="customer" :disabled="selected.status !== 'approved'">客户报价 CSV</el-dropdown-item></el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </div>
      <p v-if="selected?.outdated && !live" class="workbench-notice">需求或计价依据已变化，请重新计价后使用新版本。</p>
      <div class="workbench-toolbar">
        <div class="workbench-filters" role="group" aria-label="报价状态筛选">
          <button v-for="item in filters" :key="item.value" :class="{ selected: filter === item.value }" :aria-pressed="filter === item.value" @click="filter = item.value">
            {{ item.label }} <span>{{ displayLines.filter(line => matches(line, item.value)).length }}</span>
          </button>
        </div>
        <div class="workbench-search"><Search :size="16" /><input v-model="search" aria-label="搜索报价项目" placeholder="搜索名称、序号或材质" /></div>
      </div>
      <el-table class="result-table" :data="paged" empty-text="没有匹配的报价项目" @row-click="showLine">
        <el-table-column label="序号" width="68"><template #default="{ row, $index }">{{ row.requirement.source_item || (page - 1) * 50 + $index + 1 }}</template></el-table-column>
        <el-table-column label="项目" min-width="170"><template #default="{ row }"><button class="product-link" @click.stop="showLine(row)">{{ row.product }}</button><small v-if="changedIds.has(row.line_id)" class="changed-mark">本版更新</small></template></el-table-column>
        <el-table-column label="规格" min-width="170"><template #default="{ row }"><span class="cell-secondary">{{ specs(row) }}</span></template></el-table-column>
        <el-table-column label="数量" width="110"><template #default="{ row }">{{ quoteQuantity(row) ?? '待确认' }}<small v-if="quoteQuantity(row) != null"> {{ unit(row) }}</small></template></el-table-column>
        <el-table-column label="成品单价" align="right" width="123"><template #default="{ row }">{{ money(row.unit_price) }}</template></el-table-column>
        <el-table-column label="金额" align="right" width="130"><template #default="{ row }"><strong class="result-amount">{{ money(row.amount) }}</strong></template></el-table-column>
        <el-table-column label="状态" width="117"><template #default="{ row }"><span :class="['workbench-status', row.amount === null ? 'information' : 'priced']">{{ resultStatus(row, approved) }}</span></template></el-table-column>
      </el-table>
      <div class="result-mobile-list">
        <button v-for="(line, index) in paged" :key="line.line_id" class="result-mobile-row" @click="showLine(line)">
          <div class="result-mobile-top"><strong>{{ line.requirement.source_item ? `#${line.requirement.source_item} ` : `#${(page - 1) * 50 + index + 1} ` }}{{ line.product }}</strong><ChevronRight :size="16" /></div>
          <p>{{ specs(line) }}</p>
          <p>{{ quoteQuantity(line) ?? '数量待确认' }}<template v-if="quoteQuantity(line) != null"> {{ unit(line) }}</template> · 单价 {{ money(line.unit_price) }}</p>
          <div class="result-mobile-bottom"><span class="workbench-status">{{ resultStatus(line, approved) }}</span><small v-if="changedIds.has(line.line_id)" class="changed-mark">本版更新</small><strong>{{ money(line.amount) }}</strong></div>
        </button>
        <p v-if="!paged.length" class="workbench-empty">没有匹配的报价项目</p>
      </div>
      <el-pagination v-if="visible.length > 50" v-model:current-page="page" :total="visible.length" :page-size="50" layout="total, prev, pager, next" />
      <div v-if="live" class="result-total"><span>已计价小计（非完整报价）</span><strong>{{ money(quoteSummary(job).amount) }}</strong></div>
      <div v-else-if="selected" :class="['result-total', { outdated: selected.outdated }]"><span>{{ totalLabel(selected) }}</span><strong>{{ money(quoteDisplayTotal(selected.payload)) }}</strong></div>
      <div v-if="!reading && (live || !selected?.payload.complete || selected?.outdated)" class="result-pending-action">
        <span>{{ selected?.outdated && !live ? '需要重新计价' : `${displayLines.filter(line => line.amount === null).length} 项尚未计入金额` }}</span>
        <el-button :disabled="busy" @click="emit('supplement')">查看待补充</el-button>
        <el-button v-if="job.status !== 'processing'" type="primary" :disabled="locked || !configured" @click="emit('continue')"><RefreshCw :size="14" />{{ selected?.outdated ? '重新计价' : '继续报价' }}</el-button>
      </div>
      <details v-if="selected && previous && !live" class="result-secondary-section"><summary>版本变化 · V{{ previous.version }} → V{{ selected.version }}</summary><QuoteVersionDiff :current="selected" :previous="previous" /></details>
      <details v-if="selected && !live" class="result-secondary-section"><summary>报价范围与条款</summary><p>{{ selected.terms || '条款待确认' }}</p><p v-if="selected.payload.business_rules">不含{{ selected.payload.business_rules.excluded_fees.join('、') }}</p><p v-if="selected.payload.rounding && selected.payload.rounding !== 'none'">取整前 {{ money(selected.payload.unrounded_total) }} · 取整调整 {{ selected.payload.rounding_adjustment }} 元</p><p v-if="selected.approval_note">批准说明：{{ selected.approval_note }}</p></details>
    </template>
    <div v-else class="workbench-empty"><FileText :size="28" /><p>{{ job.status === 'processing' ? '正在整理和计算报价' : '尚未生成报价' }}</p><el-button v-if="job.status !== 'processing'" :disabled="busy || !configured" type="primary" @click="emit('continue')"><RefreshCw :size="15" />继续报价</el-button></div>
  </section>

  <el-drawer v-model="detailOpen" :title="selectedLine?.product || '项目明细'" size="min(640px, 100vw)">
    <section v-if="selectedLine" class="result-line-detail">
      <div :class="['result-detail-summary', { pending: selectedLine.amount === null }]"><span>{{ selectedLine.amount === null ? resultStatus(selectedLine, approved) : '项目金额' }}</span><strong>{{ selectedLine.amount === null ? '暂未计价' : money(selectedLine.amount) }}</strong></div>
      <p class="cell-secondary">{{ specs(selectedLine) }} · {{ quoteQuantity(selectedLine) ?? '?' }} {{ unit(selectedLine) }}</p>
      <p v-if="selected?.outdated && !live" class="workbench-notice">此版本金额已失效。</p>
      <div class="result-detail-actions"><el-button :disabled="locked" @click="detailOpen = false; emit('edit', selectedLine.line_id)"><Pencil :size="15" />修改项目</el-button><el-button v-if="selectedLine.amount === null" type="primary" :disabled="busy" @click="detailOpen = false; emit('supplement')">补充信息</el-button></div>
      <el-tabs v-model="detailTab">
        <el-tab-pane label="费用组成" name="costs">
          <section v-if="selectedLine.amount === null && detailPresentation?.items.length" class="result-needed">
            <h3>{{ detailPresentation.title }}</h3>
            <ul>
              <li v-for="(item, index) in detailPresentation.items" :key="index"><strong v-if="item.label">{{ item.label }}</strong><span>{{ item.text }}</span></li>
            </ul>
          </section>
          <dl v-if="selectedLine.unit_price != null" class="result-facts"><dt>成品单价</dt><dd>{{ money(selectedLine.unit_price) }} / {{ unit(selectedLine) }}</dd><dt v-if="Number(selectedLine.fixed_charges)">另计固定费用</dt><dd v-if="Number(selectedLine.fixed_charges)">{{ money(selectedLine.fixed_charges) }}</dd></dl>
          <p v-if="!selectedLine.estimate_costs?.length && selectedLine.amount === null" class="result-cost-empty">费用明细将在计价完成后显示</p>
          <div v-for="(cost, index) in selectedLine.estimate_costs || []" :key="index" class="result-cost-row">
            <div><strong>{{ cost.label }}</strong><span>{{ money(cost.amount) }} / {{ cost.basis === 'per_piece' ? '件' : '整项' }}</span></div>
            <p>{{ costKinds[cost.kind || 'other'] }} · {{ cost.rate }} {{ rateUnits[cost.rate_unit || 'unknown'] }} · 工程量 {{ cost.measure }}</p>
            <p v-if="cost.measurement_note || cost.reason">{{ cost.measurement_note || cost.reason }}</p>
          </div>
          <section v-if="selectedLine.review_signals?.length" class="result-review-needed"><h3>审核提醒</h3><ul><li v-for="signal in selectedLine.review_signals" :key="`${signal.code}:${signal.cost_index}`">{{ signal.message }}</li></ul></section>
          <section v-if="selectedLine.production_coverage?.requirements.length" class="result-review-needed">
            <h3>工艺费用覆盖</h3><ul><li v-for="entry in selectedLine.production_coverage.requirements"
              :key="`${entry.target_id}:${entry.process_code}`">{{ entry.label }} · {{ entry.covered ? '已覆盖' : '尚未覆盖' }}</li></ul>
          </section>
          <details v-if="detailPresentation?.notes.length || selectedLine.estimate_scope" class="result-secondary-section result-pricing-notes">
            <summary>计价说明<span v-if="detailPresentation?.notes.length"> · {{ detailPresentation.notes.length }} 条</span></summary>
            <ul><li v-for="note in detailPresentation?.notes || []" :key="note">{{ note }}</li></ul>
            <p v-if="selectedLine.estimate_scope">{{ selectedLine.estimate_scope }}</p>
          </details>
          <details v-if="selectedLine.formula" class="result-secondary-section"><summary>计算方式</summary><p class="result-formula">{{ selectedLine.formula }}</p></details>
        </el-tab-pane>
        <el-tab-pane label="采用参数" name="parameters">
          <dl class="result-facts"><template v-for="field in ['width_mm', 'height_mm', 'quantity', 'material', 'process'] as const" :key="field"><dt>{{ { width_mm: '宽度 (mm)', height_mm: '高度 (mm)', quantity: '数量', material: '材质', process: '工艺' }[field] }}</dt><dd>{{ (selectedLine.effective_requirement || selectedLine.requirement)[field] || '未提供' }}</dd></template></dl>
          <div v-for="(item, index) in selectedLine.estimate_review || []" :key="index" class="result-cost-row"><strong>{{ item.label }} · {{ item.applied ? '采用参数，待审核' : '原始参数' }}</strong><p>原始：{{ item.original ?? '未提供' }} → 采用：{{ item.applied ? item.value : item.original }}</p><p>{{ item.reason }}</p></div>
          <QuantitySources :line="selectedLine.requirement" />
        </el-tab-pane>
        <el-tab-pane label="来源与说明" name="sources">
          <p v-if="sourceDescription(selectedLine.requirement)" class="result-source-description">{{ sourceDescription(selectedLine.requirement) }}</p>
          <div v-for="(cost, index) in selectedLine.estimate_costs || []" :key="index" class="result-cost-row"><strong>{{ cost.label }}</strong><p>{{ cost.source === 'agent_estimate' ? '方案估价，待审核' : cost.source === 'catalog' ? '成本资料库' : '历史案例' }} · {{ cost.reason }}</p><p v-if="cost.reference?.source_cell">原表位置：{{ cost.reference.source_cell }}</p></div>
          <button v-for="(evidence, index) in selectedLine.requirement.evidence" :key="index" class="source-link evidence-link" @click="emit('evidence', evidence.file_id, evidence.page)">{{ job.files.find(file => file.id === evidence.file_id)?.filename || '来源资料' }} · {{ evidence.sheet ? `${evidence.sheet}!${evidence.cell || evidence.source_range || ''}` : `第 ${evidence.page} 页` }}</button>
          <p v-for="text in selectedLine.requirement.text_evidence || []" :key="text" class="cell-secondary">{{ text }}</p>
          <CaseReferences v-if="selectedLine.case_references?.length" :cases="selectedLine.case_references" />
          <p v-if="!selectedLine.requirement.evidence.length && !selectedLine.requirement.text_evidence?.length && !selectedLine.estimate_costs?.length" class="cell-secondary">暂无来源记录</p>
        </el-tab-pane>
      </el-tabs>
      <details class="result-secondary-section"><summary>执行与校验记录</summary><QuotePricingDetails :line="selectedLine" /></details>
    </section>
  </el-drawer>
</template>
