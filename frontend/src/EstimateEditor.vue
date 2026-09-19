<script setup lang="ts">
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-vue-next'
import type { EstimatePlan } from './requirements-types'
const props = defineProps<{ plan: EstimatePlan }>()
const fields: Record<string, string> = {
  quantity: '数量', width_mm: '宽(mm)', height_mm: '高(mm)', thickness_mm: '板厚(mm)',
  depth_mm: '壳深(mm)', billing_length_mm: '计价长度(mm)', material: '材质', process: '工艺',
  language: '文字类型', pricing_category: '报价类别',
}
function addCost() {
  props.plan.costs.push({ label: '', rate: '0', quantity_formula: '=1', basis: 'per_piece',
    source: 'agent_estimate', reference_id: null, reference_revision: null, reason: '' })
}
function moveCost(index: number, direction: number) {
  const target = index + direction
  if (target < 0 || target >= props.plan.costs.length) return
  const [cost] = props.plan.costs.splice(index, 1)
  if (cost) props.plan.costs.splice(target, 0, cost)
}
</script>

<template>
  <section class="estimate-editor">
    <h3>补全参数</h3>
    <div v-for="(item, index) in plan.assumptions" :key="index" class="estimate-input-row">
      <el-form-item :label="fields[item.field] || item.field" required>
        <el-input v-model="item.value" :aria-label="`暂定${fields[item.field] || item.field}`" />
      </el-form-item>
      <el-form-item label="补全依据" required><el-input v-model="item.reason" /></el-form-item>
    </div>
    <h3>成品费用方案</h3>
    <section v-for="(cost, index) in plan.costs" :key="index" class="cost-editor">
      <div class="cost-heading"><strong>费用 {{ index + 1 }}</strong><div class="cost-actions"><el-tooltip content="上移费用计算顺序"><button type="button" class="icon-button" :disabled="index === 0" :aria-label="`上移费用${index + 1}`" @click="moveCost(index, -1)"><ArrowUp :size="16" /></button></el-tooltip><el-tooltip content="下移费用计算顺序"><button type="button" class="icon-button" :disabled="index === plan.costs.length - 1" :aria-label="`下移费用${index + 1}`" @click="moveCost(index, 1)"><ArrowDown :size="16" /></button></el-tooltip><el-tooltip content="移除此项费用"><button type="button" class="icon-button" :aria-label="`移除费用${index + 1}`" @click="plan.costs.splice(index, 1)"><Trash2 :size="16" /></button></el-tooltip></div></div>
      <div class="estimate-input-row">
        <el-form-item label="费用名称" required><el-input v-model="cost.label" /></el-form-item>
        <el-form-item label="费率 / 单价" required><el-input v-model="cost.rate" inputmode="decimal" :aria-label="`费用${index + 1}单价`" /></el-form-item>
        <el-form-item label="计量公式" required><el-input v-model="cost.quantity_formula" :aria-label="`费用${index + 1}公式`" /></el-form-item>
        <el-form-item label="费用范围"><el-select v-model="cost.basis"><el-option label="每件费用" value="per_piece" /><el-option label="整单费用" value="total" /></el-select></el-form-item>
        <el-form-item label="价格来源"><el-select v-model="cost.source" @change="cost.reference_id = null; cost.reference_revision = null"><el-option label="方案估价" value="agent_estimate" /><el-option label="报价库引用" value="catalog" /><el-option label="历史案例引用" value="historical" /></el-select></el-form-item>
        <template v-if="cost.source !== 'agent_estimate'">
          <el-form-item label="引用编号"><el-input-number v-model="cost.reference_id" :min="1" :precision="0" /></el-form-item>
          <el-form-item label="引用版本"><el-input-number v-model="cost.reference_revision" :min="1" :precision="0" /></el-form-item>
        </template>
      </div>
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
.cost-editor { padding: 12px 0; border-top: 1px solid #e4e9e6; }
.cost-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; font-size: 13px; }
.cost-actions { display: flex; gap: 4px; }
.estimate-editor > .el-form-item { margin-top: 16px; }
@media (max-width: 600px) { .estimate-input-row { grid-template-columns: minmax(0, 1fr); } }
</style>
