"""Bounded arithmetic interpreter. Never compile/exec model-authored Python."""
import ast
import hashlib
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR, localcontext

INPUTS = {
    "width_mm", "height_mm", "depth_mm", "thickness_mm", "billing_length_mm",
    "quantity", "area_m2", "billable_area_m2", "letter_cm", "character_count",
    "component_count", "labor_hours", "power_units",
    "unit_subtotal", "fixed_subtotal", "order_subtotal",
}
FUNCTIONS = {"min", "max", "abs", "ceil", "floor"}
LIMIT = Decimal("1e18")


def parse_script(source):
    if not source.strip() or len(source) > 6000:
        raise ValueError("计算脚本不能为空或超过6000字符")
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("计算脚本语法错误") from exc
    if len(list(ast.walk(tree))) > 500 or len(tree.body) > 60:
        raise ValueError("计算脚本超出复杂度上限")
    defined, required = set(), set()
    allowed = (ast.Module, ast.Assign, ast.Assert, ast.Expr, ast.Name, ast.Load, ast.Store,
               ast.Constant, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp, ast.Call,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub,
               ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("脚本只支持数值赋值、四则运算、条件表达式和assert；不支持导入、属性、循环或文件操作")
        if isinstance(node, ast.Call) and (
                not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS
                or node.keywords or not 1 <= len(node.args) <= 20):
            raise ValueError("脚本函数仅支持min/max/abs/ceil/floor")
        if isinstance(node, ast.Constant) and type(node.value) not in (int, float, str):
            raise ValueError("脚本只支持数值和断言说明")
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise ValueError("脚本每次只能赋值一个变量")
            name = statement.targets[0].id
            if name in INPUTS | FUNCTIONS or name.startswith("_"):
                raise ValueError("不能覆盖输入参数或内置函数")
            expression = statement.value
        elif isinstance(statement, ast.Assert):
            if statement.msg is not None and (
                    not isinstance(statement.msg, ast.Constant) or not isinstance(statement.msg.value, str)):
                raise ValueError("assert说明必须是文字常量")
            expression = statement.test
        else:
            raise ValueError("脚本仅允许赋值和assert语句")
        for node in ast.walk(expression):
            if isinstance(node, ast.Name) and node.id not in FUNCTIONS | defined:
                if node.id not in INPUTS:
                    raise ValueError(f"未知计算参数：{node.id}")
                required.add(node.id)
        if isinstance(statement, ast.Assign):
            defined.add(name)
    if "result" not in defined:
        raise ValueError("脚本须将最终工程量赋值给result")
    return tree, required


def run_script(source, inputs):
    tree, required = parse_script(source)
    values = {}
    for key in required:
        raw = inputs.get(key)
        if raw is None or isinstance(raw, bool):
            raise ValueError(f"脚本缺少参数：{key}")
        try:
            values[key] = Decimal(str(raw))
        except DecimalException as exc:
            raise ValueError(f"脚本参数不是数值：{key}") from exc
        if not values[key].is_finite() or abs(values[key]) > LIMIT:
            raise ValueError(f"脚本参数超出范围：{key}")
    initial = {key: str(value) for key, value in sorted(values.items())}
    trace, checks = [], []

    def number(value):
        if not isinstance(value, Decimal) or not value.is_finite() or abs(value) > LIMIT:
            raise ValueError("脚本数值超出范围或将布尔结果用于计算")
        return value

    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return number(Decimal(ast.get_source_segment(source, node)))
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            value = number(evaluate(node.operand))
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = number(evaluate(node.left)), number(evaluate(node.right))
            if isinstance(node.op, ast.Add):
                return number(left + right)
            if isinstance(node.op, ast.Sub):
                return number(left - right)
            if isinstance(node.op, ast.Mult):
                return number(left * right)
            return number(left / right)
        if isinstance(node, ast.Compare):
            left = number(evaluate(node.left))
            for op, other in zip(node.ops, node.comparators):
                right = number(evaluate(other))
                passed = {ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right,
                          ast.GtE: left >= right, ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
                if not passed:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            condition = evaluate(node.test)
            if type(condition) is not bool:
                raise ValueError("条件表达式必须使用比较结果")
            return evaluate(node.body if condition else node.orelse)
        if isinstance(node, ast.Call):
            args = [number(evaluate(arg)) for arg in node.args]
            fn = node.func.id
            if fn in ("min", "max"):
                return (min if fn == "min" else max)(args)
            if len(args) != 1:
                raise ValueError("abs/ceil/floor只接受一个参数")
            if fn == "abs":
                return abs(args[0])
            return args[0].to_integral_value(rounding=ROUND_CEILING if fn == "ceil" else ROUND_FLOOR)
        raise ValueError("计算表达式无效")

    try:
        with localcontext() as context:
            context.prec = 28
            for statement in tree.body:
                if isinstance(statement, ast.Assign):
                    name = statement.targets[0].id
                    values[name] = number(evaluate(statement.value))
                    trace.append({"variable": name, "value": str(values[name])})
                else:
                    passed = evaluate(statement.test)
                    message = statement.msg.value if statement.msg else ast.get_source_segment(source, statement.test)
                    if passed is not True:
                        raise ValueError("脚本校验未通过：" + message[:300])
                    checks.append({"message": message[:300], "passed": True})
    except (DecimalException, OverflowError, RecursionError, KeyError) as exc:
        raise ValueError("脚本计算失败：检查除零、参数及数值范围") from exc
    if values["result"] < 0:
        raise ValueError("脚本工程量不能为负数")
    return {"engine": "arithmetic.v1", "sha256": hashlib.sha256(source.encode()).hexdigest(),
            "source": source, "inputs": initial, "dependencies": sorted(required),
            "result": str(values["result"]), "trace": trace, "checks": checks}
