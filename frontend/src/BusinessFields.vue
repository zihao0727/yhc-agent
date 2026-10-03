<script setup lang="ts">
import { Plus, Trash2 } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import { removePricingTarget } from './estimate-contract'
import type { Line } from './requirements-types'
const props = defineProps<{ line: Line }>()
function removeTarget(target: 'letter_group' | 'component', index: number) {
  if (!removePricingTarget(props.line, target, index)) ElMessage.warning('该对象仍被费用或补全参数引用，请先调整引用')
}
function addComponent() {
  (props.line.components ||= []).push({
    name: '', material: '', thickness_mm: null, width_mm: null, height_mm: null,
    billing_length_mm: null, area_m2: null, labor_hours: null, quantity_per_item: null,
    geometry: 'unknown', processes: [], evidence: [], uncertainties: [],
  })
}
</script>

<template>
  <section class="business-fields">
    <h3>现场计价口径</h3>
    <div class="business-grid">
      <el-form-item label="报价路径"><el-select v-model="line.pricing_family" aria-label="报价路径"><el-option label="待确认" value="unknown" /><el-option label="字类" value="letter" /><el-option label="立牌 / 造型 / 材料" value="material" /></el-select></el-form-item>
      <el-form-item label="类型备注"><el-input v-model="line.type_note" maxlength="255" aria-label="类型备注" /></el-form-item>
      <el-form-item label="工艺难度等级"><el-select v-model="line.difficulty" aria-label="工艺难度等级"><el-option label="未标注，待核实" value="unknown" /><el-option label="A · 加工工序 3 倍" value="A" /><el-option label="B · 加工工序 2 倍" value="B" /><el-option label="C · 常规加工" value="C" /></el-select></el-form-item>
      <el-form-item label="工艺难度备注原文"><el-input v-model="line.difficulty_note" maxlength="500" aria-label="工艺难度备注原文" /></el-form-item>
      <el-form-item label="图纸标注面积 (㎡)"><el-input v-model="line.area_m2" inputmode="decimal" clearable @clear="line.area_m2 = null" /></el-form-item>
      <el-form-item label="每件工时"><el-input v-model="line.labor_hours" inputmode="decimal" clearable @clear="line.labor_hours = null" /></el-form-item>
      <el-form-item label="单面 / 双面"><el-select v-model="line.faces"><el-option label="待确认" value="unknown" /><el-option label="单面" value="single" /><el-option label="双面" value="double" /></el-select></el-form-item>
      <el-form-item label="是否装灯"><el-select v-model="line.lighting" aria-label="是否装灯"><el-option label="待确认" value="unknown" /><el-option label="装灯" value="yes" /><el-option label="不装灯" value="no" /></el-select></el-form-item>
      <el-form-item label="安装场景"><el-select v-model="line.installation_scene" aria-label="安装场景"><el-option label="待确认" value="unknown" /><el-option label="楼顶" value="rooftop" /><el-option label="其他场景" value="other" /></el-select></el-form-item>
    </div>
    <template v-if="line.pricing_family === 'letter'">
      <div class="business-grid">
        <el-form-item label="每件字数 / 字母数"><el-input-number v-model="line.character_count" :min="1" :max="10000" :precision="0" aria-label="每件字数" /></el-form-item>
        <el-form-item label="单字厘米取整"><el-select v-model="line.length_rounding" aria-label="单字厘米取整"><el-option label="原值，不取整" value="exact" /><el-option label="向上取整至厘米（需审核）" value="ceil_cm" /><el-option label="四舍五入至厘米（需审核）" value="nearest_cm" /></el-select></el-form-item>
      </div>
      <div class="group-heading"><h3>不同尺寸的字组</h3><el-button link @click="(line.letter_groups ||= []).push({ label: '', count: 1, width_mm: null, height_mm: null, billing_length_mm: null, evidence: [] })"><Plus :size="14" />添加字组</el-button></div>
      <div v-for="(group, index) in line.letter_groups || []" :key="index" class="letter-group">
        <div class="group-heading"><strong>字组 {{ index + 1 }}</strong><el-tooltip content="删除字组"><button type="button" class="icon-button danger" :aria-label="`删除字组${index + 1}`" @click="removeTarget('letter_group', index)"><Trash2 :size="15" /></button></el-tooltip></div>
        <div class="business-grid">
          <el-form-item label="字组名称"><el-input v-model="group.label" /></el-form-item>
          <el-form-item label="字组文字"><el-input v-model="group.text_content" :aria-label="`字组${index + 1}文字`" /></el-form-item>
          <el-form-item label="字数依据"><el-select :model-value="group.count_basis || 'unknown'" :aria-label="`字组${index + 1}字数依据`" @update:model-value="group.count_basis = $event"><el-option label="待确认" value="unknown" /><el-option label="按可读字符" value="characters" /><el-option label="明确重复数量" value="repeated" /></el-select></el-form-item>
          <el-form-item label="每件成品中的字数"><el-input-number v-model="group.count" :min="1" :max="10000" :precision="0" /></el-form-item>
          <el-form-item label="单字宽 (mm)"><el-input v-model="group.width_mm" inputmode="decimal" /></el-form-item>
          <el-form-item label="单字高 (mm)"><el-input v-model="group.height_mm" inputmode="decimal" /></el-form-item>
          <el-form-item label="单字计价长度 (mm)"><el-input v-model="group.billing_length_mm" inputmode="decimal" /></el-form-item>
        </div>
      </div>
    </template>
    <div class="group-heading"><h3>材料与加工部件</h3><el-button link @click="addComponent"><Plus :size="14" />添加部件</el-button></div>
    <div v-for="(component, index) in line.components || []" :key="index" class="letter-group">
      <div class="group-heading"><strong>部件 {{ index + 1 }}</strong><el-tooltip content="删除部件"><button type="button" class="icon-button danger" :aria-label="`删除部件${index + 1}`" @click="removeTarget('component', index)"><Trash2 :size="15" /></button></el-tooltip></div>
      <div class="business-grid">
        <el-form-item label="部件名称"><el-input v-model="component.name" :aria-label="`部件${index + 1}名称`" /></el-form-item>
        <el-form-item label="部件材质"><el-input v-model="component.material" /></el-form-item>
        <el-form-item label="板厚 (mm)"><el-input v-model="component.thickness_mm" inputmode="decimal" /></el-form-item>
        <el-form-item label="每件部件用量"><el-input-number v-model="component.quantity_per_item" :min="1" :max="10000" :precision="0" /></el-form-item>
        <el-form-item label="部件宽 (mm)"><el-input v-model="component.width_mm" inputmode="decimal" /></el-form-item>
        <el-form-item label="部件高 (mm)"><el-input v-model="component.height_mm" inputmode="decimal" /></el-form-item>
        <el-form-item label="部件计价长度 (mm)"><el-input v-model="component.billing_length_mm" inputmode="decimal" /></el-form-item>
        <el-form-item label="部件明确面积 (㎡)"><el-input v-model="component.area_m2" inputmode="decimal" /></el-form-item>
        <el-form-item label="单个部件工时"><el-input v-model="component.labor_hours" inputmode="decimal" /></el-form-item>
        <el-form-item label="几何形状"><el-select v-model="component.geometry"><el-option label="矩形平板" value="flat_rectangle" /><el-option label="异形" value="shaped" /><el-option label="壳体" value="shell" /><el-option label="待确认" value="unknown" /></el-select></el-form-item>
        <el-form-item label="部件工序"><el-select v-model="component.processes" multiple filterable allow-create default-first-option><el-option v-for="process in component.processes" :key="process" :label="process" :value="process" /></el-select></el-form-item>
      </div>
    </div>
    <details v-if="line.dimension_evidence?.length">
      <summary>尺寸来源与归属</summary>
      <div v-for="(evidence, index) in line.dimension_evidence" :key="index" class="dimension-evidence">
        <strong>{{ evidence.sheet || `文件${evidence.file_id}` }} {{ evidence.cell || `第${evidence.page}页` }}</strong>
        <span>{{ evidence.target === 'letter_group' ? `字组 ${(evidence.target_index || 0) + 1}` : evidence.target === 'component' ? `部件 ${(evidence.target_index || 0) + 1}` : '整件' }} · {{ evidence.role || 'overall' }} · {{ evidence.field }} = {{ evidence.value ?? '未读清' }} {{ evidence.unit }}</span>
        <span>{{ evidence.text }}</span>
      </div>
    </details>
    <template v-if="line.lighting === 'yes'">
      <h3>电源台数</h3>
      <div class="business-grid">
        <el-form-item label="台数计算策略"><el-select v-model="line.power_mode" aria-label="台数计算策略"><el-option label="逐字核算后填写台数" value="manual" /><el-option label="逐字每3㎡向上取整（需审核）" value="per_letter_ceiling" /></el-select></el-form-item>
        <el-form-item v-if="line.power_mode !== 'per_letter_ceiling'" label="每件成品电源台数"><el-input-number v-model="line.power_count" :min="0" :max="1000000" :precision="0" aria-label="每件电源台数" /></el-form-item>
        <el-form-item v-else label="笔画疏密调整台数"><el-input-number v-model="line.power_adjustment" :min="-10000" :max="10000" :precision="0" /></el-form-item>
        <el-form-item label="电源核算 / 调整依据"><el-input v-model="line.power_note" maxlength="1500" /></el-form-item>
      </div>
    </template>
  </section>
</template>

<style scoped>
.business-fields { border-top: 1px solid #dce4df; padding: 12px 0; margin-top: 12px; }
h3 { font-size: 14px; margin: 8px 0 12px; }
.business-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.group-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; }
.letter-group { border-top: 1px solid #e4e9e6; padding: 12px 0; }
.business-grid :deep(.el-form-item), .business-grid :deep(.el-select) { min-width: 0; width: 100%; }
.dimension-evidence { display: grid; gap: 5px; padding: 10px 0; border-bottom: 1px solid #e4e9e6; font-size: 12px; overflow-wrap: anywhere; }
summary { cursor: pointer; font-size: 12px; margin: 12px 0; }
@media (max-width: 600px) { .business-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
