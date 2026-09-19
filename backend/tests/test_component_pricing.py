from app.component_pricing import analyze_components, surface_processes
from app.models import Category, Product, PriceItem
from app.requirements_schemas import RequirementLine
from app.quote_engine import build_quote
import pytest


def test_no_surface_process_is_distinct_from_unknown():
    assert surface_processes(["无"]) == ["无"]
    assert surface_processes([]) == []
    assert surface_processes(["切割", "表面UV印刷"]) == ["UV打印"]


def test_references_preserve_conditions_and_never_price_assembly(test_db):
    with test_db.begin() as db:
        category = Category(name="材料")
        db.add(category)
        db.flush()
        product = Product(name="材料+表面处理建议价", category_id=category.id)
        db.add(product)
        db.flush()
        for amount, process, status, unit in [
            ("150", "烤漆", "active", "m2"), ("200", "烤漆", "active", "m2"),
            ("80", "UV打印", "active", "m2"), ("10", "烤漆", "inactive", "m2"),
            ("1", "烤漆", "active", "unknown"),
        ]:
            db.add(PriceItem(product_id=product.id, material="304#", thickness_mm=1.2,
                             process=process, amount=amount, status=status, unit=unit,
                             notes="条件待核实", attributes={"process_1": process, "process_2": "无"}))
        db.flush()
        line = RequirementLine(id="line1", product="发光标识", width_mm=1000, height_mm=500,
                               components=[{"name": "外壳", "material": "304不锈钢", "thickness_mm": "1.2",
                                            "processes": ["烤漆"], "geometry": "shell",
                                            "evidence": [{"file_id": 1, "page": 1, "text": "1.2mm304不锈钢"}]}])
        result = analyze_components(db, line)
        component = result["components"][0]
        assert component["candidate_count"] == 2
        assert component["references"][0]["notes"] == "条件待核实"
        assert all(p["revision"] == 1 for p in component["references"])
        assert "异形/壳体的实际计价面积或展开尺寸" in component["missing"]
        assert "成品数量" not in component["missing"]
        assert "amount" not in component and "total" not in result
        line.components[0].material = "1.2mmTHK.304#不锈钢"
        line.components[0].processes = ["折弯焊接成型", "表面喷涂烤漆珍珠白"]
        component = analyze_components(db, line)["components"][0]
        assert component["candidate_count"] == 2
        assert "原文加工工序、色号及未匹配的表面处理尚未计价" in component["missing"]
        line.components[0].material = "不锈钢"
        assert analyze_components(db, line)["components"][0]["candidate_count"] == 0


@pytest.mark.parametrize("overrides,expected", [
    ({}, "15.00"),
    ({"geometry": "shell"}, None),
    ({"width_mm": None}, None),
    ({"quantity_per_item": None}, None),
    ({"evidence": []}, None),
    ({"processes": []}, None),
    ({"processes": ["烤漆", "焊接"]}, None),
    ({"uncertainties": ["部件尺寸有冲突"]}, None),
])
def test_material_cost_is_separate_from_finished_price(test_db, overrides, expected):
    with test_db.begin() as db:
        category = Category(name="材料")
        db.add(category)
        db.flush()
        product = Product(name="材料+表面处理建议价", category_id=category.id)
        db.add(product)
        db.flush()
        db.add(PriceItem(product_id=product.id, material="304#", thickness_mm="1.2",
                         process="烤漆", amount="150", status="active", unit="m2",
                         attributes={"process_1": "烤漆", "process_2": "无"}))
        db.flush()
        component = {"name": "面板", "material": "304#", "thickness_mm": "1.2",
                     "processes": ["烤漆"], "geometry": "flat_rectangle", "width_mm": "500",
                     "height_mm": "100", "quantity_per_item": 2,
                     "evidence": [{"file_id": 1, "page": 1, "text": "面板500x100，2片"}],
                     **overrides}
        line = RequirementLine(id="line", product="组合标识", quantity=None, components=[component])
        quote = build_quote(db, [line.model_dump(mode="json")], automatic=True)
        result = quote["lines"][0]
        assert result["component_analysis"]["components"][0]["cost_per_item"] == expected
        assert result["unit_price"] is None and result["amount"] is None
        assert quote["known_subtotal"] == "0" and quote["total"] is None
