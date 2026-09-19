<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import RequirementsWorkspace from './RequirementsWorkspace.vue'
import {
  ArrowLeft, ArrowRight, BookOpen, Check, ChevronRight, CircleHelp, Database, FileSpreadsheet,
  History, Lightbulb, ListFilter, LockKeyhole, LogOut, Menu, Pencil, Plus, RefreshCw, Search, ShieldCheck,
  SlidersHorizontal, TableProperties, Trash2, X,
} from 'lucide-vue-next'
import {
  api, kindLabels, languageLabels, ruleLabels, statusLabels, unitLabels,
  type Audit, type Document, type Options, type Price, type Rule, type Source, type Stats,
} from './api'

type View = 'jobs' | 'prices' | 'rules' | 'documents' | 'audit'
const nav = [
  { id: 'jobs' as View, label: '客户需求', icon: BookOpen },
  { id: 'prices' as View, label: '价格明细', icon: TableProperties },
  { id: 'rules' as View, label: '规则与案例', icon: SlidersHorizontal },
  { id: 'documents' as View, label: '来源文件', icon: FileSpreadsheet },
  { id: 'audit' as View, label: '修改记录', icon: History },
]
const loggedIn = ref(false)
const compact = ref(window.innerWidth < 800)
function resize() { compact.value = window.innerWidth < 800 }
const loginToken = ref('')
const loginError = ref('')
const loginBusy = ref(false)
const view = ref<View>('jobs')
const mobileNav = ref(false)
const loading = ref(false)
const pageError = ref('')
const saving = ref(false)
const page = ref(1)
const pageSize = ref(25)
const total = ref(0)
const query = ref('')
const status = ref('')
const category = ref<number | ''>('')
const productId = ref<number | ''>('')
const prices = ref<Price[]>([])
const rules = ref<Rule[]>([])
const documents = ref<Document[]>([])
const audits = ref<Audit[]>([])
const options = ref<Options>({ categories: [], products: [] })
const stats = ref<Stats>({ prices: 0, draft: 0, active: 0, inactive: 0, products: 0, rules: 0, documents: 0 })
const filteredProducts = computed(() => options.value.products.filter(p => !category.value || p.category_id === category.value))
const title = computed(() => nav.find(n => n.id === view.value)?.label)
const count = computed(() => view.value === 'documents' ? documents.value.length : total.value)
const drawer = ref(false)
const selected = ref<Price | null>(null)
const revisions = ref<{ revision: number; reason: string; created_at: string; snapshot: Record<string, unknown> }[]>([])
const detailTab = ref('detail')
const rawSource = ref<{ filename: string; sheet: string; row_number: number; cells: Record<string, unknown> } | null>(null)
const sourceOpen = ref(false)
const sourceBusy = ref(false)
const auditOpen = ref(false)
const auditSelected = ref<Audit | null>(null)
const priceDialog = ref(false)
const ruleDialog = ref(false)
const editingId = ref<number | null>(null)
const ruleEditingId = ref<number | null>(null)
const formError = ref('')
const ruleError = ref('')
const defaults = () => ({
  category: '发光字与金属字', product: '', material: '', thickness_mm: '' as string | null, spec: '',
  language: 'all', process: '', quality: '', unit: 'cm', amount: '' as string | null,
  price_kind: 'standard', status: 'active', notes: '', review_reason: '', revision: undefined as number | undefined,
})
const form = reactive(defaults())
const ruleForm = reactive({
  product_id: null as number | null, name: '', content: '', rule_type: 'reference', status: 'draft', reason: '',
  revision: undefined as number | undefined,
})
const money = (value: string | null) => value === null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 6 })
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败'
let loadSequence = 0

async function login() {
  loginBusy.value = true; loginError.value = ''
  sessionStorage.setItem('firefly-token', loginToken.value.trim())
  try {
    await api('/session')
    loggedIn.value = true
    await reload()
    loginToken.value = ''
  } catch (e) { loginError.value = message(e); sessionStorage.removeItem('firefly-token') }
  finally { loginBusy.value = false }
}
function logout() {
  loggedIn.value = false; sessionStorage.removeItem('firefly-token')
  drawer.value = false; priceDialog.value = false; ruleDialog.value = false; sourceOpen.value = false
}
async function loadMeta() {
  const [s, o] = await Promise.all([api<Stats>('/stats'), api<Options>('/options')])
  stats.value = s; options.value = o
}
async function load() {
  if (view.value === 'jobs') return
  const sequence = ++loadSequence
  loading.value = true; pageError.value = ''
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: String(pageSize.value) })
    if (query.value) params.set('q', query.value)
    if (status.value) params.set('status', status.value)
    if (category.value) params.set('category_id', String(category.value))
    if (productId.value) params.set('product_id', String(productId.value))
    if (view.value === 'prices') {
      const result = await api<{ items: Price[]; total: number }>(`/prices?${params}`)
      if (sequence !== loadSequence) return
      prices.value = result.items; total.value = result.total
    } else if (view.value === 'rules') {
      const result = await api<{ items: Rule[]; total: number }>(`/rules?${params}`)
      if (sequence !== loadSequence) return
      rules.value = result.items; total.value = result.total
    } else if (view.value === 'documents') {
      const result = await api<Document[]>('/documents')
      if (sequence !== loadSequence) return
      documents.value = result
    } else {
      const result = await api<{ items: Audit[]; total: number }>(`/audit?${params}`)
      if (sequence !== loadSequence) return
      audits.value = result.items; total.value = result.total
    }
  } catch (e) { if (sequence === loadSequence) pageError.value = message(e) }
  finally { if (sequence === loadSequence) loading.value = false }
}
async function reload() {
  try { await loadMeta(); await load() } catch (e) { pageError.value = message(e) }
}
function changeView(id: View) {
  view.value = id; page.value = 1; query.value = ''; status.value = ''; category.value = ''; productId.value = ''
  mobileNav.value = false; load()
}
function filter() { page.value = 1; load() }
function clearFilters() { query.value = ''; status.value = ''; category.value = ''; productId.value = ''; filter() }
async function openPrice(row: Price) {
  try {
    const [detail, history] = await Promise.all([
      api<Price>(`/prices/${row.id}`),
      api<typeof revisions.value>(`/prices/${row.id}/history`),
    ])
    selected.value = detail; revisions.value = history; detailTab.value = 'detail'; drawer.value = true
  } catch (e) { ElMessage.error(message(e)) }
}
function editPrice(item?: Price) {
  editingId.value = item?.id ?? null
  Object.assign(form, defaults())
  if (item) {
    for (const key of Object.keys(defaults()) as (keyof ReturnType<typeof defaults>)[]) {
      if (key in item) (form as Record<string, unknown>)[key] = item[key as keyof Price]
    }
    form.review_reason = ''
  }
  formError.value = ''; priceDialog.value = true
}
async function savePrice() {
  formError.value = ''
  if (!form.product.trim() || !form.category.trim()) { formError.value = '请填写产品名称和分类'; return }
  if (form.review_reason.trim().length < 2) { formError.value = '请填写修改或确认原因（至少 2 个字）'; return }
  saving.value = true
  try {
    const payload = { ...form, amount: form.amount === '' ? null : form.amount,
      thickness_mm: form.thickness_mm === '' ? null : form.thickness_mm }
    const result = await api<Price>(editingId.value ? `/prices/${editingId.value}` : '/prices', {
      method: editingId.value ? 'PUT' : 'POST', body: JSON.stringify(payload),
    })
    priceDialog.value = false
    ElMessage.success(editingId.value ? '修改已保存' : '价格已新增')
    if (drawer.value && selected.value?.id === result.id) await openPrice(result)
    await reload()
  } catch (e) { formError.value = message(e) }
  finally { saving.value = false }
}
async function deactivate(item: Price | Rule, entity: 'prices' | 'rules') {
  try {
    const result = await ElMessageBox.prompt(`停用后保留来源和修改记录。`, '停用记录', {
      confirmButtonText: '确认停用', cancelButtonText: '取消',
      inputPlaceholder: '填写停用原因', inputValidator: v => (v || '').trim().length >= 2 || '至少填写 2 个字',
    })
    await api(`/${entity}/${item.id}`, { method: 'DELETE',
      body: JSON.stringify({ revision: item.revision, reason: result.value.trim() }) })
    ElMessage.success('已停用')
    drawer.value = false
    await reload()
  } catch (e) { if (e !== 'cancel' && e !== 'close') ElMessage.error(message(e)) }
}
function editRule(item?: Rule) {
  ruleEditingId.value = item?.id ?? null
  Object.assign(ruleForm, { product_id: item?.product_id ?? null, name: item?.name ?? '',
    content: item?.content ?? '', rule_type: item?.rule_type ?? 'reference', status: item?.status ?? 'draft',
    reason: '', revision: item?.revision })
  ruleError.value = ''; ruleDialog.value = true
}
async function saveRule() {
  if (!ruleForm.name.trim() || !ruleForm.content.trim() || ruleForm.reason.trim().length < 2) {
    ruleError.value = '请填写名称、规则原文及修改原因'; return
  }
  saving.value = true; ruleError.value = ''
  try {
    await api(ruleEditingId.value ? `/rules/${ruleEditingId.value}` : '/rules', {
      method: ruleEditingId.value ? 'PUT' : 'POST', body: JSON.stringify(ruleForm),
    })
    ruleDialog.value = false; ElMessage.success('规则已保存'); await reload()
  } catch (e) { ruleError.value = message(e) }
  finally { saving.value = false }
}
async function showSource(source: Source) {
  sourceOpen.value = true; sourceBusy.value = true; rawSource.value = null
  try { rawSource.value = await api(`/sources/${source.record_id}`) }
  catch (e) { ElMessage.error(message(e)); sourceOpen.value = false }
  finally { sourceBusy.value = false }
}
function auditDetail(item: Audit) { auditSelected.value = item; auditOpen.value = true }
const auditDiff = computed(() => {
  const a = auditSelected.value
  if (!a) return []
  return Object.keys(a.after || {}).filter(k => JSON.stringify(a.before?.[k]) !== JSON.stringify(a.after?.[k]))
    .map(k => ({ field: k, before: a.before?.[k] ?? '—', after: a.after?.[k] ?? '—' }))
})
onMounted(async () => {
  window.addEventListener('firefly-unauthorized', logout)
  window.addEventListener('resize', resize)
  if (sessionStorage.getItem('firefly-token')) {
    try { await api('/session'); loggedIn.value = true; await reload() } catch { logout() }
  }
})
onUnmounted(() => {
  window.removeEventListener('firefly-unauthorized', logout)
  window.removeEventListener('resize', resize)
})
</script>

<template>
  <div v-if="!loggedIn" class="login-page">
    <div class="login-brand"><span class="brand-symbol"><Lightbulb :size="24" /></span><strong>萤火虫</strong><span>报价管理</span></div>
    <form class="login-form" @submit.prevent="login">
      <span class="section-eyebrow">WORKSPACE ACCESS</span>
      <h1>登录报价工作区</h1>
      <label for="admin-token">管理口令</label>
      <el-input id="admin-token" v-model="loginToken" type="password" show-password autocomplete="current-password" size="large" placeholder="管理口令" />
      <div v-if="loginError" class="form-error" role="alert">{{ loginError }}</div>
      <el-button type="primary" native-type="submit" size="large" :loading="loginBusy"><LockKeyhole :size="16" />进入工作区<ArrowRight :size="16" /></el-button>
      <div class="login-meta"><ShieldCheck :size="15" /> 本地管理端 <span>PostgreSQL</span></div>
    </form>
    <footer>萤火虫广告标识 · 价格与工艺资料库</footer>
  </div>
  <div v-else class="app-shell">
    <div v-if="mobileNav" class="nav-overlay" @click="mobileNav = false"></div>
    <aside class="sidebar" :class="{ 'mobile-open': mobileNav }">
      <div class="brand"><span class="brand-symbol"><Lightbulb :size="22" /></span><div><strong>萤火虫</strong><small>报价管理工作区</small></div><button class="icon-button mobile-close" aria-label="关闭导航" @click="mobileNav = false"><X :size="18" /></button></div>
      <div class="workspace"><span class="workspace-initial">萤</span><div>萤火虫广告标识<small>内部价格资料库</small></div><ChevronRight :size="15" /></div>
      <div class="nav-label">工作区</div>
      <nav aria-label="主导航">
        <button v-for="item in nav" :key="item.id" :class="['nav-item', { active: view === item.id }]" @click="changeView(item.id)">
          <component :is="item.icon" :size="18" /><span>{{ item.label }}</span>
          <span v-if="item.id === 'prices'" class="nav-count">{{ stats.prices.toLocaleString() }}</span>
        </button>
      </nav>
      <div class="sidebar-footer"><div class="db-indicator"><span></span> PostgreSQL <small>本地</small></div><button class="account" @click="logout"><span class="avatar">管</span><div>工作区管理员<small>local-admin</small></div><LogOut :size="16" /></button></div>
    </aside>
    <div class="main-area">
      <header class="topbar">
        <button class="icon-button mobile-menu" aria-label="打开导航" @click="mobileNav = true"><Menu :size="20" /></button>
        <div class="breadcrumb">报价资料库<ChevronRight :size="14" /><strong>{{ title }}</strong></div>
        <div class="topbar-right"><span class="local-label"><span></span>本地工作区</span><span class="avatar small">管</span></div>
      </header>
      <main>
        <RequirementsWorkspace v-if="view === 'jobs'" />
        <template v-else>
        <div class="page-heading"><div><div class="section-eyebrow">PRICING WORKSPACE</div><h1>{{ title }}</h1></div>
          <div class="heading-actions"><el-tooltip content="刷新数据"><button class="icon-button bordered" aria-label="刷新数据" @click="reload"><RefreshCw :size="17" :class="{ spinning: loading }" /></button></el-tooltip>
            <el-button v-if="view === 'prices'" type="primary" @click="editPrice()"><Plus :size="16" />新增价格</el-button>
            <el-button v-if="view === 'rules'" type="primary" @click="editRule()"><Plus :size="16" />新增规则</el-button>
          </div>
        </div>
        <section class="stats-band" aria-label="资料库概况">
          <div><span>价格条目</span><strong>{{ stats.prices.toLocaleString() }}<small>条</small></strong></div>
          <div><span><i class="dot amber"></i>待确认</span><strong>{{ stats.draft.toLocaleString() }}<small>条</small></strong></div>
          <div><span><i class="dot green"></i>已启用</span><strong>{{ stats.active.toLocaleString() }}<small>条</small></strong></div>
          <div><span>产品类型</span><strong>{{ stats.products }}<small>类</small></strong></div>
          <div><span>来源文件</span><strong>{{ stats.documents }}<small>份</small></strong></div>
        </section>
        <div v-if="pageError" class="error-banner" role="alert"><CircleHelp :size="18" /><span>{{ pageError }}</span><el-button @click="reload">重试</el-button></div>
        <template v-else>
          <div v-if="view === 'prices' || view === 'rules'" class="filter-bar">
            <form class="search-box" @submit.prevent="filter"><Search :size="17" /><input v-model="query" :placeholder="view === 'prices' ? '搜索产品、材质、规格或工艺' : '搜索规则名称或内容'" aria-label="搜索" /><button type="submit" class="search-submit" aria-label="执行搜索"><ArrowRight :size="17" /></button></form>
            <el-select v-if="view === 'prices'" v-model="category" placeholder="全部分类" clearable aria-label="分类筛选" @change="productId = ''; filter()"><el-option v-for="item in options.categories" :key="item.id" :label="item.name" :value="item.id" /></el-select>
            <el-select v-if="view === 'prices'" v-model="productId" placeholder="全部产品" clearable filterable aria-label="产品筛选" @change="filter"><el-option v-for="item in filteredProducts" :key="item.id" :label="item.name" :value="item.id" /></el-select>
            <el-select v-model="status" placeholder="全部状态" clearable aria-label="状态筛选" @change="filter"><el-option v-for="(label, key) in statusLabels" :key="key" :label="label" :value="key" /></el-select>
            <el-tooltip content="重置筛选"><button class="icon-button bordered" aria-label="重置筛选" @click="clearFilters"><ListFilter :size="17" /></button></el-tooltip>
          </div>
          <div class="table-caption"><strong>{{ view === 'prices' ? '全部价格' : title }}</strong><span>{{ count.toLocaleString() }} {{ view === 'documents' ? '份文件' : '条记录' }}</span><span v-if="view === 'rules'" class="caption-note">规则原文 · 尚未启用自动计价</span><span v-if="view === 'prices'" class="caption-note">CNY · 人民币</span></div>
          <section v-if="view === 'prices'" class="table-region" aria-label="价格列表">
            <el-table v-loading="loading" :data="prices" row-key="id" @row-dblclick="openPrice" :empty-text="loading ? '加载中' : '没有符合条件的价格记录'">
              <el-table-column label="产品 / 规格" :min-width="compact ? 150 : 235"><template #default="{ row }"><button class="product-link" @click="openPrice(row)">{{ row.product }}</button><div class="cell-secondary">{{ row.spec || '未指定规格' }}<template v-if="compact"> · {{ languageLabels[row.language] }}</template></div><span v-if="compact" :class="['status-badge', row.status]"><i></i>{{ statusLabels[row.status] }}</span></template></el-table-column>
              <el-table-column v-if="!compact" label="材质 / 厚度" min-width="125"><template #default="{ row }"><span>{{ row.material || '—' }}</span><div class="cell-secondary">{{ row.thickness_mm ? `${Number(row.thickness_mm)} mm` : languageLabels[row.language] }}</div></template></el-table-column>
              <el-table-column v-if="!compact" label="工艺 / 品质" min-width="155"><template #default="{ row }"><span>{{ row.process || '常规' }}</span><div class="cell-secondary">{{ row.quality || languageLabels[row.language] }}</div></template></el-table-column>
              <el-table-column label="单价" :min-width="compact ? 110 : 145" align="right"><template #default="{ row }"><strong class="price-value">{{ money(row.amount) }}</strong><div class="cell-secondary" :class="{ 'warning-text': row.unit === 'unknown' }">{{ unitLabels[row.unit] }} · {{ kindLabels[row.price_kind] }}</div></template></el-table-column>
              <el-table-column v-if="!compact" label="状态" width="110"><template #default="{ row }"><span :class="['status-badge', row.status]"><i></i>{{ statusLabels[row.status] }}</span></template></el-table-column>
              <el-table-column v-if="!compact" label="来源" width="118"><template #default="{ row }"><button v-if="row.source" class="source-link" @click="showSource(row.source)"><FileSpreadsheet :size="14" />{{ row.source.cell }}</button><span v-else class="cell-secondary">手动新增</span><div class="cell-secondary">版本 {{ row.revision }}</div></template></el-table-column>
              <el-table-column label="" :width="compact ? 70 : 86" fixed="right"><template #default="{ row }"><div class="row-actions"><el-tooltip content="编辑价格"><button class="icon-button" :aria-label="`编辑价格 ${row.id}`" @click="editPrice(row)"><Pencil :size="15" /></button></el-tooltip><el-tooltip content="停用价格"><button class="icon-button danger" :disabled="row.status === 'inactive'" :aria-label="`停用价格 ${row.id}`" @click="deactivate(row, 'prices')"><Trash2 :size="15" /></button></el-tooltip></div></template></el-table-column>
            </el-table>
          </section>
          <section v-else-if="view === 'rules'" class="table-region" aria-label="规则列表">
            <el-table v-loading="loading" :data="rules" row-key="id" empty-text="没有符合条件的规则">
              <el-table-column label="规则名称 / 原文" min-width="430"><template #default="{ row }"><button class="product-link" @click="editRule(row)">{{ row.name }}</button><div class="rule-excerpt">{{ row.content }}</div></template></el-table-column>
              <el-table-column label="关联产品" min-width="175" prop="product" />
              <el-table-column label="类型" width="112"><template #default="{ row }">{{ ruleLabels[row.rule_type] }}</template></el-table-column>
              <el-table-column label="状态" width="100"><template #default="{ row }"><span :class="['status-badge', row.status]"><i></i>{{ statusLabels[row.status] }}</span></template></el-table-column>
              <el-table-column label="来源" width="105"><template #default="{ row }"><button v-if="row.source" class="source-link" @click="showSource(row.source)">{{ row.source.cell }}</button><span v-else>手动新增</span></template></el-table-column>
              <el-table-column label="" width="86" fixed="right"><template #default="{ row }"><div class="row-actions"><button class="icon-button" aria-label="编辑规则" @click="editRule(row)"><Pencil :size="15" /></button><button class="icon-button danger" aria-label="停用规则" :disabled="row.status === 'inactive'" @click="deactivate(row, 'rules')"><Trash2 :size="15" /></button></div></template></el-table-column>
            </el-table>
          </section>
          <section v-else-if="view === 'documents'" v-loading="loading" class="documents">
            <article v-for="doc in documents" :key="doc.id" class="document-item">
              <div class="document-top"><span class="file-icon"><FileSpreadsheet :size="27" /></span><div><h2>{{ doc.filename }}</h2><span class="cell-secondary">导入于 {{ date(doc.imported_at) }}</span></div><span class="status-badge active"><Check :size="13" />已导入</span></div>
              <div class="document-metrics"><div><strong>{{ doc.report.prices.toLocaleString() }}</strong><span>价格条目</span></div><div><strong>{{ doc.report.rules }}</strong><span>规则与案例</span></div><div><strong>{{ doc.report.source_rows.toLocaleString() }}</strong><span>原始资料行</span></div></div>
              <div class="sheet-list"><span v-for="sheet in doc.report.sheets" :key="sheet"><TableProperties :size="13" />{{ sheet }}</span></div>
              <div class="document-warnings"><p v-for="warning in doc.report.warnings" :key="warning">{{ warning }}</p></div>
              <div class="file-hash"><span>SHA-256</span><code>{{ doc.sha256 }}</code></div>
            </article>
            <el-empty v-if="!loading && !documents.length" description="尚未导入报价文件" />
          </section>
          <section v-else class="table-region">
            <el-table v-loading="loading" :data="audits" empty-text="暂无修改记录" @row-dblclick="auditDetail">
              <el-table-column label="时间" min-width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
              <el-table-column label="记录" min-width="160"><template #default="{ row }"><button class="product-link" @click="auditDetail(row)">{{ ({ price_items: '价格', pricing_rules: '规则', requirement_jobs: '客户需求', quote_drafts: '报价草稿' } as Record<string, string>)[row.entity] || row.entity }} #{{ row.entity_id }}</button></template></el-table-column>
              <el-table-column label="操作" width="100"><template #default="{ row }">{{ ({ import: '导入', create: '新增', update: '修改', deactivate: '停用' } as Record<string, string>)[row.action] || row.action }}</template></el-table-column>
              <el-table-column label="原因" min-width="250" prop="reason" />
              <el-table-column label="操作人" min-width="140" prop="actor" />
              <el-table-column width="60"><template #default="{ row }"><button class="icon-button" aria-label="查看变更" @click="auditDetail(row)"><ChevronRight :size="17" /></button></template></el-table-column>
            </el-table>
          </section>
          <div v-if="view !== 'documents'" class="pagination-bar"><span>共 {{ total.toLocaleString() }} 条</span><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[25, 50, 100]" :total="total" layout="sizes, prev, pager, next" :pager-count="5" @current-change="load" @size-change="filter" /></div>
        </template>
        <footer class="page-footer"><span>萤火虫 · 价格资料库</span><span>数据来源可追溯 <span class="footer-dot">·</span> 历史版本保留</span></footer>
        </template>
      </main>
    </div>
  </div>

  <el-drawer v-model="drawer" :title="selected?.product" size="min(640px, 100vw)">
    <template v-if="selected">
      <div class="detail-top"><div><strong>{{ money(selected.amount) }}</strong><span>{{ unitLabels[selected.unit] }}</span></div><span :class="['status-badge', selected.status]"><i></i>{{ statusLabels[selected.status] }}</span></div>
      <el-tabs v-model="detailTab"><el-tab-pane label="价格详情" name="detail"><dl class="detail-list">
        <dt>分类</dt><dd>{{ selected.category }}</dd><dt>规格</dt><dd>{{ selected.spec || '—' }}</dd><dt>材质 / 厚度</dt><dd>{{ selected.material || '—' }} {{ selected.thickness_mm ? `${Number(selected.thickness_mm)} mm` : '' }}</dd>
        <dt>文字类型</dt><dd>{{ languageLabels[selected.language] }}</dd><dt>工艺</dt><dd>{{ selected.process || '—' }}</dd><dt>品质</dt><dd>{{ selected.quality || '—' }}</dd><dt>价格性质</dt><dd>{{ kindLabels[selected.price_kind] }}</dd><dt>备注</dt><dd class="multiline">{{ selected.notes || '—' }}</dd><dt>确认原因</dt><dd>{{ selected.review_reason }}</dd>
      </dl><div v-if="selected.source" class="detail-source"><h3><BookOpen :size="16" />原始来源</h3><p>{{ selected.source.filename }}</p><button class="source-link" @click="showSource(selected.source)">{{ selected.source.sheet }} / {{ selected.source.cell }}<ChevronRight :size="14" /></button></div>
      <div v-if="selected.attributes && Object.keys(selected.attributes).length" class="attributes"><h3>原始计价参数</h3><pre>{{ JSON.stringify(selected.attributes, null, 2) }}</pre></div>
      </el-tab-pane><el-tab-pane :label="`版本历史 (${revisions.length})`" name="history"><div v-for="revision in revisions" :key="revision.revision" class="revision-item"><div><strong>版本 {{ revision.revision }}</strong><span>{{ date(revision.created_at) }}</span></div><p>{{ revision.reason }}</p><span class="cell-secondary">{{ money(revision.snapshot.amount as string | null) }} · {{ statusLabels[String(revision.snapshot.status)] }}</span></div></el-tab-pane></el-tabs>
    </template>
    <template #footer><el-button @click="drawer = false">关闭</el-button><el-button type="primary" @click="selected && editPrice(selected)"><Pencil :size="15" />编辑价格</el-button></template>
  </el-drawer>

  <el-dialog v-model="priceDialog" :title="editingId ? '编辑价格' : '新增价格'" width="760px" :close-on-click-modal="false" destroy-on-close>
    <el-form label-position="top" @submit.prevent="savePrice">
      <div class="form-grid">
        <el-form-item label="产品分类" required><el-select v-model="form.category" filterable allow-create default-first-option><el-option v-for="c in options.categories" :key="c.id" :label="c.name" :value="c.name" /></el-select></el-form-item>
        <el-form-item label="产品名称" required><el-input v-model="form.product" maxlength="150" /></el-form-item>
        <el-form-item label="材质"><el-input v-model="form.material" maxlength="100" /></el-form-item>
        <el-form-item label="厚度 (mm)"><el-input v-model="form.thickness_mm" inputmode="decimal" placeholder="未指定" /></el-form-item>
        <el-form-item label="尺寸 / 规格"><el-input v-model="form.spec" maxlength="255" /></el-form-item>
        <el-form-item label="文字类型"><el-select v-model="form.language"><el-option v-for="(label, key) in languageLabels" :key="key" :label="label" :value="key" /></el-select></el-form-item>
        <el-form-item label="工艺"><el-input v-model="form.process" maxlength="255" /></el-form-item>
        <el-form-item label="品质"><el-input v-model="form.quality" maxlength="100" /></el-form-item>
        <el-form-item label="单价 (CNY)"><el-input v-model="form.amount" inputmode="decimal" placeholder="待确认" /></el-form-item>
        <el-form-item label="计价单位"><el-select v-model="form.unit"><el-option v-for="(label, key) in unitLabels" :key="key" :label="label" :value="key" /></el-select></el-form-item>
        <el-form-item label="价格性质"><el-select v-model="form.price_kind"><el-option v-for="(label, key) in kindLabels" :key="key" :label="label" :value="key" /></el-select></el-form-item>
        <el-form-item label="状态"><el-select v-model="form.status"><el-option v-for="(label, key) in statusLabels" :key="key" :label="label" :value="key" /></el-select></el-form-item>
      </div>
      <el-form-item label="备注"><el-input v-model="form.notes" type="textarea" :rows="3" maxlength="10000" /></el-form-item>
      <el-form-item label="修改 / 确认原因" required><el-input v-model="form.review_reason" maxlength="2000" placeholder="本次调整的业务依据" /></el-form-item>
      <div v-if="formError" class="form-error" role="alert">{{ formError }}</div>
    </el-form>
    <template #footer><el-button @click="priceDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="savePrice"><Check :size="16" />保存</el-button></template>
  </el-dialog>

  <el-dialog v-model="ruleDialog" :title="ruleEditingId ? '编辑规则' : '新增规则'" width="700px" :close-on-click-modal="false">
    <el-form label-position="top">
      <el-form-item label="规则名称" required><el-input v-model="ruleForm.name" maxlength="255" /></el-form-item>
      <div class="form-grid"><el-form-item label="关联产品"><el-select v-model="ruleForm.product_id" filterable clearable placeholder="公共 / 待归属"><el-option v-for="p in options.products" :key="p.id" :label="p.name" :value="p.id" /></el-select></el-form-item><el-form-item label="规则类型"><el-select v-model="ruleForm.rule_type"><el-option v-for="(label, key) in ruleLabels" :key="key" :label="label" :value="key" /></el-select></el-form-item></div>
      <el-form-item label="规则原文" required><el-input v-model="ruleForm.content" type="textarea" :rows="7" maxlength="20000" /></el-form-item>
      <el-form-item label="状态"><el-radio-group v-model="ruleForm.status"><el-radio value="draft">待确认</el-radio><el-radio value="inactive">已停用</el-radio></el-radio-group></el-form-item>
      <el-form-item label="修改原因" required><el-input v-model="ruleForm.reason" maxlength="2000" /></el-form-item>
      <div v-if="ruleError" class="form-error" role="alert">{{ ruleError }}</div>
    </el-form>
    <template #footer><el-button @click="ruleDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveRule"><Check :size="16" />保存</el-button></template>
  </el-dialog>

  <el-dialog v-model="sourceOpen" title="原表记录" width="800px">
    <div v-loading="sourceBusy" class="source-content"><template v-if="rawSource"><h3>{{ rawSource.filename }}</h3><p class="cell-secondary">{{ rawSource.sheet }} · 第 {{ rawSource.row_number }} 行</p><el-table :data="Object.entries(rawSource.cells).map(([cell, value]) => ({ cell, value }))"><el-table-column prop="cell" label="单元格" width="100" /><el-table-column label="原始内容"><template #default="{ row }"><span class="multiline">{{ row.value }}</span></template></el-table-column></el-table></template></div>
  </el-dialog>
  <el-dialog v-model="auditOpen" title="变更详情" width="850px"><template v-if="auditSelected"><p>{{ auditSelected.reason }}</p><p class="cell-secondary">{{ date(auditSelected.created_at) }} · {{ auditSelected.actor }}</p><el-table :data="auditDiff"><el-table-column prop="field" label="字段" width="145" /><el-table-column label="修改前" min-width="200"><template #default="{ row }"><pre class="diff-value">{{ typeof row.before === 'object' ? JSON.stringify(row.before, null, 2) : row.before }}</pre></template></el-table-column><el-table-column label="修改后" min-width="200"><template #default="{ row }"><pre class="diff-value">{{ typeof row.after === 'object' ? JSON.stringify(row.after, null, 2) : row.after }}</pre></template></el-table-column></el-table></template></el-dialog>
</template>
