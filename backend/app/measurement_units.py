"""Canonical dimensions and source-unit conversions, separate from piece counts."""
from decimal import Decimal
from typing import Literal

EngineeringUnit = Literal["m", "m2", "m3", "kg", "hour"]
COUNT_ALIASES = {
    "个": "piece", "件": "piece", "只": "piece", "piece": "piece", "pieces": "piece", "pc": "piece", "pcs": "piece",
    "套": "set", "set": "set", "sets": "set", "组": "组", "块": "块", "台": "台", "张": "张",
    "樘": "樘", "面": "面", "处": "处", "点": "点", "座": "座", "项": "项", "批": "批",
}
ENGINEERING_FACTORS = {
    "mm": ("m", Decimal(".001")), "毫米": ("m", Decimal(".001")),
    "cm": ("m", Decimal(".01")), "厘米": ("m", Decimal(".01")), "m": ("m", Decimal(1)), "米": ("m", Decimal(1)),
    "m2": ("m2", Decimal(1)), "m^2": ("m2", Decimal(1)), "平方米": ("m2", Decimal(1)),
    "cm2": ("m2", Decimal(".0001")), "平方厘米": ("m2", Decimal(".0001")),
    "m3": ("m3", Decimal(1)), "m^3": ("m3", Decimal(1)), "立方米": ("m3", Decimal(1)),
    "kg": ("kg", Decimal(1)), "千克": ("kg", Decimal(1)), "公斤": ("kg", Decimal(1)),
    "t": ("kg", Decimal(1000)), "吨": ("kg", Decimal(1000)),
    "h": ("hour", Decimal(1)), "hour": ("hour", Decimal(1)), "小时": ("hour", Decimal(1)),
}
UNIT_LABELS = {"m": "米", "m2": "平方米", "m3": "立方米", "kg": "千克", "hour": "小时"}
