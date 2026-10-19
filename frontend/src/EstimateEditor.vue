<script setup lang="ts">
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-vue-next'
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { moveEstimateCost, removeEstimateCost, targetOptions } from './estimate-contract'
import { parameterFields, processLabels } from './generated-quote-contract'
import { costKinds, includedCostKinds, measurementLabels, pricingStrategies, rateUnits, type EstimateAssumption, type EstimateCost, type EstimatePlan, type Line } from './requirements-types'
const props = defineProps<{ plan: EstimatePlan; currentPolicy?: boolean; line?: Line }>()
const fields: Record<string, string> = {
  quantity: '数量', width_mm: '宽(mm)', height_mm: '高(mm)', thickness_mm: '板厚(mm)',
  depth_mm: '壳深(mm)', billing_length_mm: '计价长度(mm)', material: '材质', process: '工艺',
  language: '文字类型', pricing_category: '报价类别',
  character_count: '每件字数', area_m2: '图纸面积(㎡)', power_count: '每件电源台数',
  pricing_family: '计价分类', count: '字组字符数', quantity_per_item: '每件部件用量',
  labor_hours: '工时', geometry: '部件形状', area_basis: '用料面积口径', text_content: '文字内容',
}
const targets = computed(() => targetOptions(props.line ? { ...props.line,
  components: props.line.components?.length ? props.line.components : props.plan.proposed_components,
  letter_groups: props.line.letter_groups?.length ? props.line.letter_groups : props.plan.proposed_letter_groups,
} : undefined))
function evidenceKey(e: NonNullable<EstimateAssumption['evidence']>[number]) {
  return JSON.stringify({
    file_id: e.file_id, page: e.page, text: e.text, sheet: e.sheet || '', cell: e.cell || '', source: e.source || 'text',
  })
}
const evidenceOptions = computed(() => {
  const all = [...(props.line?.evidence || []), ...(props.line?.dimension_evidence || []),
    ...(props.line?.letter_groups || []).flatMap(group => group.evidence),
    ...(props.line?.components || []).flatMap(component => component.evidence)]
  return [...new Map(all.map(e => [evidenceKey(e), e])).entries()].map(([value, e]) => ({
    value, label: `${e.sheet || `文件${e.file_id}`} ${e.cell || `第${e.page}页`} · ${e.text.slice(0, 90)}`,
  }))
})
function fieldsFor(item: EstimateAssumption): string[] {
  return [...parameterFields[item.target || 'item']]
}
function setTarget(item: EstimateAssumption | EstimateCost, value: string) {
  const selected = targets.value.find(target => target.value === value)
  if (!selected) return
  item.target = selected.target; item.target_index = selected.index
  item.target_id = selected.object_id
  if (props.plan.contract_version !== 3) props.plan.contract_version = 2
  if ('field' in item) {
    if (!fieldsFor(item).includes(item.field)) item.field = fieldsFor(item)[0]!
    if (item.mode === 'correct') item.previous_value = previousValue(item)
  }
}
function previousValue(item: EstimateAssumption) {
  const obj = item.target === 'letter_group' ? props.line?.letter_groups?.[item.target_index ?? -1]
    : item.target === 'component' ? props.line?.components?.[item.target_index ?? -1] : props.line
  const value = (obj as unknown as Record<string, unknown> | undefined)?.[item.field]
  return value == null ? 'None' : String(value)
}
function setMode(item: EstimateAssumption, mode: 'fill' | 'correct') {
  item.mode = mode
  if (mode === 'correct') { if (props.plan.contract_version !== 3) props.plan.contract_version = 2; item.previous_value = previousValue(item) }
  else item.previous_value = null
}
function addAssumption() {
  props.plan.assumptions.push({ field: 'width_mm', value: '', reason: '', target: 'item', target_index: null, mode: 'fill', evidence: [] })
}
function addCost() {
  props.plan.costs.push({ label: '', rate: '0', quantity_formula: '=1', basis: 'per_piece',
    rate_unit: 'piece', expected_amount: null, measurement_note: '', measurement: 'auto',
    source: 'agent_estimate', reference_id: null, reference_revision: null, reason: '', kind: 'other',
    outsourcing_type: 'other', minimum_area_scope: 'unconfirmed' })
}
function moveCost(index: number, direction: number) {
  if (!moveEstimateCost(props.plan, index, direction)) ElMessage.warning('差价项必须位于其基准费用之后')
}
function removeCost(index: number) {
  if (!removeEstimateCost(props.plan, index)) ElMessage.warning('该费用仍被差价项引用')
}
</script>

<template>
  <section class="estimate-editor">
    <div class="estimate-input-row">
      <el-form-item label="计价契约"><el-select :model-value="plan.contract_version || 1" aria-label="计价契约" @update:model-value="plan.contract_version = $event"><el-option label="原有方案" :value="1" /><el-option label="通用对象级方案" :value="2" /><el-option label="程序计量方案" :value="3" /></el-select></el-form-item>
      <el-form-item v-if="(plan.contract_version || 1) >= 2" label="计价策略"><el-select :model-value="plan.pricing_strategy || 'auto'" aria-label="计价策略" @update:model-value="plan.pricing_strategy = $event"><el-option v-for="(label, value) in pricingStrategies" :key="value" :disabled="plan.contract_version === 3 && value === 'auto'" :value="value" :label="label" /></el-select></el-form-item>
    </div>
    <h3>补全参数</h3>
    <div v-for="(item, index) in plan.assumptions" :key="index" class="estimate-input-row">
      <el-form-item v-if="(plan.contract_version || 1) >= 2" label="参数对象"><el-select :model-value="targets.find(t => item.target_id && t.object_id === item.target_id)?.value || `${item.target || 'item'}:${item.target_index ?? ''}`" :aria-label="`参数${index + 1}对象`" @update:model-value="setTarget(item, $event)"><el-option v-for="target in targets" :key="target.value" :value="target.value" :label="target.label" /></el-select></el-form-item>
      <el-form-item label="参数字段"><el-select v-model="item.field" :aria-label="`参数${index + 1}字段`" @change="item.mode === 'correct' && (item.previous_value = previousValue(item))"><el-option v-for="field in fieldsFor(item)" :key="field" :value="field" :label="fields[field]" /></el-select></el-form-item>
      <el-form-item v-if="(plan.contract_version || 1) >= 2" label="采用方式"><el-select :model-value="item.mode || 'fill'" :aria-label="`参数${index + 1}采用方式`" @update:model-value="setMode(item, $event)"><el-option label="补全缺项" value="fill" /><el-option label="纠正识别值（待审核）" value="correct" /></el-select></el-form-item>
      <el-form-item :label="fields[item.field] || item.field" required>
        <el-input v-model="item.value" :aria-label="`暂定${fields[item.field] || item.field}`" />
      </el-form-item>
      <el-form-item label="补全依据" required><el-input v-model="item.reason" /></el-form-item>
      <template v-if="item.mode === 'correct'">
        <el-form-item label="识别原值"><el-input :model-value="item.previous_value" readonly /></el-form-item>
        <el-form-item label="原文证据" required><el-select multiple :model-value="(item.evidence || []).map(evidenceKey)" :aria-label="`参数${index + 1}原文证据`" @update:model-value="item.evidence = $event.map((value: string) => JSON.parse(value))"><el-option v-for="e in evidenceOptions" :key="e.value" :value="e.value" :label="e.label" /></el-select></el-form-item>
      </template>
      <el-tooltip content="移除参数"><button type="button" class="icon-button" :aria-label="`移除参数${index + 1}`" @click="plan.assumptions.splice(index, 1)"><Trash2 :size="16" /></button></el-tooltip>
    </div>
    <el-button @click="addAssumption"><Plus :size="14" />添加补全参数</el-button>
    <h3>成品费用方案</h3>
    <section v-for="(cost, index) in plan.costs" :key="index" class="cost-editor">
      <div class="cost-heading"><strong>费用 {{ index + 1 }}</strong><div class="cost-actions"><el-tooltip content="上移费用计算顺序"><button type="button" class="icon-button" :disabled="index === 0" :aria-label="`上移费用${index + 1}`" @click="moveCost(index, -1)"><ArrowUp :size="16" /></button></el-tooltip><el-tooltip content="下移费用计算顺序"><button type="button" class="icon-button" :disabled="index === plan.costs.length - 1" :aria-label="`下移费用${index + 1}`" @click="moveCost(index, 1)"><ArrowDown :size="16" /></button></el-tooltip><el-tooltip content="移除此项费用"><button type="button" class="icon-button" :aria-label="`移除费用${index + 1}`" @click="removeCost(index)"><Trash2 :size="16" /></button></el-tooltip></div></div>
      <div class="estimate-input-row">
        <el-form-item v-if="(plan.contract_version || 1) >= 2" label="费用对象"><el-select :model-value="targets.find(t => cost.target_id && t.object_id === cost.target_id)?.value || `${cost.target || 'item'}:${cost.target_index ?? ''}`" :aria-label="`费用${index + 1}对象`" @update:model-value="setTarget(cost, $event)"><el-option v-for="target in targets" :key="target.value" :value="target.value" :label="target.label" /></el-select></el-form-item>
        <el-form-item label="费用名称" required><el-input v-model="cost.label" /></el-form-item>
        <el-form-item label="费用类别"><el-select v-model="cost.kind" :aria-label="`费用${index + 1}类别`"><el-option v-for="(label, value) in currentPolicy ? includedCostKinds : costKinds" :key="value" :value="value" :label="label" /></el-select></el-form-item>
        <el-form-item label="覆盖工序"><el-select v-model="cost.process_codes" multiple :aria-label="`费用${index + 1}覆盖工序`"><el-option v-for="(label, value) in processLabels" :key="value" :value="value" :label="label" /></el-select></el-form-item>
        <el-form-item label="费率 / 单价" required><el-input v-model="cost.rate" inputmode="decimal" :aria-label="`费用${index + 1}单价`" /></el-form-item>
        <el-form-item label="单价单位" :required="currentPolicy">
          <el-select :model-value="cost.rate_unit || 'unknown'" :aria-label="`费用${index + 1}单价单位`" @update:model-value="cost.rate_unit = $event">
            <el-option v-for="(label, value) in rateUnits" :key="value" :value="value" :label="label" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="plan.contract_version === 3" label="计量口径" required><el-select v-model="cost.measurement" :aria-label="`费用${index + 1}计量口径`" @change="cost.quantity_formula = '=1'; cost.calculation_script = ''; cost.source_measurement_index = null; cost.basis = cost.measurement === 'source_quantity' ? 'total' : cost.basis"><el-option v-for="(label, value) in measurementLabels" :key="value" :label="label" :value="value" /></el-select></el-form-item>
        <el-form-item v-if="cost.measurement === 'source_quantity'" label="原表工程量" required><el-select v-model="cost.source_measurement_index" :aria-label="`费用${index + 1}原表工程量`"><el-option v-for="(measurement, measurementIndex) in line?.source_measurements || []" :key="measurementIndex" :value="measurementIndex" :label="`${measurement.value} ${measurement.unit}`" /></el-select></el-form-item>
        <el-form-item v-else label="计量公式" required><el-input v-model="cost.quantity_formula" :aria-label="`费用${index + 1}公式`" /></el-form-item>
        <el-form-item label="本项费用校对金额" :required="plan.contract_version !== 3 && currentPolicy && cost.source === 'agent_estimate'">
          <el-input :model-value="cost.expected_amount ?? ''" inputmode="decimal" :aria-label="`费用${index + 1}校对金额`" @update:model-value="cost.expected_amount = $event || null" />
        </el-form-item>
        <el-form-item label="费用范围"><el-select v-model="cost.basis" :disabled="cost.measurement === 'source_quantity'"><el-option label="每件费用" value="per_piece" /><el-option label="整单费用" value="total" /></el-select></el-form-item>
        <template v-if="(plan.contract_version || 1) >= 2">
          <el-form-item label="已含费用类别"><el-select v-model="cost.included_kinds" multiple :aria-label="`费用${index + 1}已含范围`"><el-option v-for="kind in ['material', 'process', 'power', 'bracket', 'outsourcing']" :key="kind" :value="kind" :label="costKinds[kind]" /></el-select></el-form-item>
          <el-form-item v-if="plan.contract_version !== 3" label="差价基准费用"><el-select v-model="cost.adjusts_cost_index" clearable :aria-label="`费用${index + 1}差价基准`" @clear="cost.adjusts_cost_index = null"><el-option v-for="(previous, previousIndex) in plan.costs.slice(0, index)" :key="previousIndex" :value="previousIndex" :label="`${previousIndex + 1} · ${previous.label}`" /></el-select></el-form-item>
          <el-form-item label="单价依据数值"><el-input v-model="cost.rate_basis_amount" inputmode="decimal" clearable @clear="cost.rate_basis_amount = null; cost.rate_basis_unit = null" /></el-form-item>
          <el-form-item label="单价依据单位"><el-select v-model="cost.rate_basis_unit" clearable @clear="cost.rate_basis_unit = null"><el-option v-for="(label, value) in rateUnits" v-show="value !== 'unknown'" :key="value" :value="value" :label="label" /></el-select></el-form-item>
        </template>
        <el-form-item label="价格来源"><el-select v-model="cost.source" @change="cost.reference_id = null; cost.reference_revision = null; cost.reference_part = null"><el-option label="方案估价" value="agent_estimate" /><el-option label="报价库引用" value="catalog" /><el-option label="历史案例引用" value="historical" /></el-select></el-form-item>
        <el-form-item v-if="cost.source === 'catalog'" label="引用范围">
          <el-select v-model="cost.reference_part" :aria-label="`费用${index + 1}引用范围`" clearable placeholder="完整单价" @clear="cost.reference_part = null">
            <el-option label="原表材料成本" value="material_price" />
            <el-option label="原表工序一成本" value="process_1_price" />
            <el-option label="原表工序二成本" value="process_2_price" />
          </el-select>
        </el-form-item>
        <template v-if="currentPolicy && cost.kind === 'outsourcing'">
          <el-form-item label="外加工类型"><el-select v-model="cost.outsourcing_type"><el-option label="其他 / 待补价格" value="other" /><el-option label="背胶 · 50元/㎡" value="adhesive" /><el-option label="背胶裁形 · 100元/㎡" value="cut_adhesive" /><el-option label="3M胶 · 200元/㎡" value="3m" /></el-select></el-form-item>
          <el-form-item label="最低0.1㎡适用范围"><el-select v-model="cost.minimum_area_scope" aria-label="最低面积范围"><el-option label="未确认" value="unconfirmed" /><el-option label="按每件（暂定，需审核）" value="per_piece" /><el-option label="按整行（暂定，需审核）" value="order" /></el-select></el-form-item>
        </template>
        <template v-if="cost.source !== 'agent_estimate'">
          <el-form-item label="引用编号"><el-input-number v-model="cost.reference_id" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="引用版本"><el-input-number v-model="cost.reference_revision" :min="1" :precision="0" /></el-form-item>
        </template>
      </div>
      <el-form-item label="实际部件工程量 / 占比依据"><el-input v-model="cost.measurement_note" :aria-label="`费用${index + 1}工程量依据`" /></el-form-item>
      <el-form-item v-if="cost.measurement === 'script'" label="工程量计算" required><el-input v-model="cost.calculation_script" type="textarea" :rows="6" :aria-label="`费用${index + 1}计算脚本`" /></el-form-item>
      <el-form-item label="费用依据 / 调整理由" required><el-input v-model="cost.reason" type="textarea" :rows="2" /></el-form-item>
    </section>
    <el-button @click="addCost"><Plus :size="14" />添加估算费用</el-button>
    <el-form-item label="报价包含范围、未包含费用及商业条件" required><el-input v-model="plan.scope" type="textarea" :rows="3" /></el-form-item>
  </section>
</template>

<style scoped>
.estimate-editor { border-top: 1px solid #dce4df; margin-top: 16px; padding: 12px 0; }
h3 { font-size: 14px; margin: 8px 0 12px; }
.estimate-input-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.estimate-input-row :deep(.el-form-item), .estimate-input-row :deep(.el-select) { min-width: 0; width: 100%; }
.cost-editor { padding: 12px 0; border-top: 1px solid #e4e9e6; }
.cost-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; font-size: 13px; }
.cost-actions { display: flex; gap: 4px; }
.estimate-editor > .el-form-item { margin-top: 16px; }
@media (max-width: 600px) { .estimate-input-row { grid-template-columns: minmax(0, 1fr); } }
</style>
