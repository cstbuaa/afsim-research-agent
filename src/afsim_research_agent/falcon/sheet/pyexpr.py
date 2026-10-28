"""Python conditions → IR expression DSL (``ast.parse`` + whitelist; never executed).

    near_urban = terrain.distance_to_class("urban") <= urban_transition_distance
    in_contact = track.nearest_hostile_range <= contact_range or bb.contact_reported
    contact_cleared = not in_contact and time_in_state() >= contact_hold_time
    mover_ok = mover.available

Names resolve to parameters (``parameter_ref``), conditions (``condition_ref``) or
coordination-generated conditions (``coord.report_received``). Operands are the
catalog operands without the ``_value`` suffix (``terrain``, ``track``, ``mover``,
``self``, ``formation``, ``team``, ``simulation``; ``sensor("id").on`` for
components); ``bb.x`` / ``<role>.bb.x`` read blackboards. Anything else is a hole.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.falcon.sheet.blocks import SheetProblem
from afsim_research_agent.falcon.sheet.values import near, parse_quantity
from afsim_research_agent.ir.catalog import Catalog

COMPARE = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "lte",
    ast.Gt: "gt",
    ast.GtE: "gte",
}
# f() >= x  →  f_gte(x)  (the catalog functions that take one threshold)
SUGAR = {
    ("time_in_state", "gte"): ("time_in_state_gte", "duration"),
    ("time_in_state", "gt"): ("time_in_state_gte", "duration"),
    ("range_to_current_target", "lte"): ("range_to_current_target_lte", "range"),
    ("range_to_current_target", "lt"): ("range_to_current_target_lte", "range"),
}
PROPERTY_ALIASES = {
    ("terrain_value", "distance_to"): "distance_to_class",
    ("terrain_value", "class_"): "class",
    ("terrain_value", "kind"): "class",
}
_KEYWORD_ATTR = re.compile(r"\.class\b")


def prepare(text: str) -> str:
    """``terrain.class`` is natural but ``class`` is a Python keyword."""
    return _KEYWORD_ATTR.sub(".class_", text)


MAX_DEPTH = 24


@dataclass
class ExprSymbols:
    catalog: Catalog
    parameters: set[str] = field(default_factory=set)
    conditions: set[str] = field(default_factory=set)
    coordination: dict[str, str] = field(default_factory=dict)  # id -> pattern
    roles: set[str] = field(default_factory=set)
    components: dict[str, set[str]] = field(default_factory=dict)  # kind -> ids

    def generated(self, coord: str, kind: str) -> list[str]:
        pattern = self.catalog.patterns.get(self.coordination.get(coord, ""))
        return list(pattern.generated.get(kind, [])) if pattern else []

    def namespaces(self) -> dict[str, str]:
        out = {}
        for name in self.catalog.operands:
            short = name[: -len("_value")] if name.endswith("_value") else name
            out[short] = name
        for alias, target in self.catalog.operand_aliases.items():
            out[alias[: -len("_value")] if alias.endswith("_value") else alias] = target
        return out


class ExprCompiler:
    def __init__(self, symbols: ExprSymbols, block: str, line_offset: int) -> None:
        self.s = symbols
        self.block = block
        self.offset = line_offset
        self.problems: list[SheetProblem] = []
        self.ns = symbols.namespaces()
        self.blackboard_reads: set[str] = set()  # "role.var" or ".var" (own role)

    # ------------------------------------------------------------------ helpers
    def _line(self, node: ast.AST) -> int:
        return self.offset + getattr(node, "lineno", 1) - 1

    def fail(self, node: ast.AST, message: str, candidates: list[str] | None = None) -> None:
        self.problems.append(SheetProblem(self.block, self._line(node), message, candidates or []))

    def parse(self, text: str, node_for_line: ast.AST | None = None) -> dict[str, Any] | None:
        """One expression written somewhere else (Mermaid label, XML attribute)."""
        try:
            tree = ast.parse(prepare(text.strip()), mode="eval")
        except SyntaxError as exc:
            self.problems.append(
                SheetProblem(self.block, self.offset, f"表达式无法解析：{text!r}（{exc.msg}）")
            )
            return None
        return self.expr(tree.body)

    # ------------------------------------------------------------------ expressions
    def expr(self, node: ast.AST, depth: int = 0) -> dict[str, Any] | None:
        if depth > MAX_DEPTH:
            self.fail(node, "表达式嵌套过深")
            return None
        if isinstance(node, ast.BoolOp):
            items = [self.expr(v, depth + 1) for v in node.values]
            if any(i is None for i in items):
                return None
            return {"all" if isinstance(node.op, ast.And) else "any": items}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = self.expr(node.operand, depth + 1)
            return None if inner is None else {"not": inner}
        if isinstance(node, ast.Compare):
            return self.compare(node, depth)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                return self.method_property(node)
            return self.call(node, depth)
        return self.operand(node, depth, boolean=True)

    def compare(self, node: ast.Compare, depth: int) -> dict[str, Any] | None:
        parts: list[dict[str, Any]] = []
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            name = COMPARE.get(type(op))
            if name is None:
                self.fail(node, f"不支持的比较运算 {type(op).__name__}（只用 == != < <= > >=）")
                return None
            # time_in_state() >= x
            if isinstance(left, ast.Call) and isinstance(left.func, ast.Name) and not left.args:
                sugar = SUGAR.get((left.func.id, name))
                if sugar is not None:
                    arg = self.operand(right, depth + 1)
                    if arg is None:
                        return None
                    parts.append(
                        {"function_call": {"name": sugar[0], "arguments": {sugar[1]: arg}}}
                    )
                    left = right
                    continue
            a = self.operand(left, depth + 1)
            b = self.operand(right, depth + 1)
            if a is None or b is None:
                return None
            parts.append({"compare": {"left": a, "operator": name, "right": b}})
            left = right
        return parts[0] if len(parts) == 1 else {"all": parts}

    def call(self, node: ast.Call, depth: int) -> dict[str, Any] | None:
        if not isinstance(node.func, ast.Name):
            self.fail(node, "只能调用目录函数（如 message_received(...)、formation_is(...)）")
            return None
        name = node.func.id
        fn = self.s.catalog.functions.get(name)
        if fn is None:
            candidates = near(name, set(self.s.catalog.functions) | {"time_in_state"})
            self.fail(node, f"未知函数 '{name}'", candidates)
            return None
        params = list(fn.arguments.items())
        args: dict[str, Any] = {}
        if len(node.args) > len(params):
            self.fail(node, f"函数 {name} 只有 {len(params)} 个参数")
            return None
        values = list(zip([p[0] for p in params], node.args, strict=False))
        values += [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
        for arg_name, value in values:
            kind = fn.arguments.get(arg_name)
            if kind is None:
                self.fail(node, f"函数 {name} 没有参数 '{arg_name}'", [p[0] for p in params])
                return None
            converted = self.argument(value, kind, depth)
            if converted is None:
                return None
            args[arg_name] = converted
        missing = [p for p, _ in params if p not in args]
        if missing:
            self.fail(node, f"函数 {name} 缺少参数 {missing}")
            return None
        return {"function_call": {"name": name, "arguments": args}}

    def argument(self, node: ast.AST, kind: str, depth: int) -> Any:
        if kind in ("message", "coordination", "formation", "string"):
            text = self.id_text(node)
            if text is None:
                self.fail(node, f"参数应是名字或字符串（{kind}）")
                return None
            if kind == "message":
                return text.replace(".", "__")
            if kind == "coordination" and text not in self.s.coordination:
                self.fail(node, f"未定义的协同 '{text}'", near(text, self.s.coordination))
                return None
            return text
        return self.operand(node, depth + 1)

    @staticmethod
    def id_text(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        return None

    # ------------------------------------------------------------------ operands
    def operand(self, node: ast.AST, depth: int, *, boolean: bool = False) -> dict[str, Any] | None:
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, str):
                q = parse_quantity(value)
                if q is not None:
                    return {"value": q[0], "unit": q[1]}
            if isinstance(value, bool | int | float | str):
                return {"literal": value}
            self.fail(node, f"不支持的常量 {value!r}")
            return None
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int | float)
        ):
            return {"literal": -node.operand.value}
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                return self.method_property(node)
            if isinstance(node.func, ast.Name) and node.func.id == "q" and node.args:
                text = self.id_text(node.args[0])
                q = parse_quantity(text)
                if q is None:
                    self.fail(node, f'q(...) 需要 "数 单位"，得到 {text!r}')
                    return None
                return {"value": q[0], "unit": q[1]}
            return self.call(node, depth)
        if isinstance(node, ast.Name):
            return self.name(node)
        if isinstance(node, ast.Attribute):
            return self.attribute(node)
        if isinstance(node, ast.BoolOp | ast.Compare | ast.UnaryOp) and boolean is False:
            return self.expr(node, depth + 1)
        self.fail(
            node, f"不支持的写法 {type(node).__name__}（只允许比较、and/or/not、属性和目录函数）"
        )
        return None

    def name(self, node: ast.Name) -> dict[str, Any] | None:
        n = node.id
        if n in ("True", "False"):
            return {"literal": n == "True"}
        if n in self.s.parameters:
            return {"parameter_ref": n}
        if n in self.s.conditions:
            return {"condition_ref": n}
        if n in self.ns:
            self.fail(
                node, f"'{n}' 后面要接属性（如 {n}.{self._props(self.ns[n])[:1] or ['…']}[0]）"
            )
            return None
        candidates = near(n, self.s.parameters | self.s.conditions)
        self.fail(node, f"未定义的名字 '{n}'（参数或条件）", candidates)
        return None

    def _props(self, operand: str) -> list[str]:
        op = self.s.catalog.operands.get(operand)
        return list(op.properties) if op else []

    def attribute(self, node: ast.Attribute) -> dict[str, Any] | None:
        prop = node.attr
        base = node.value
        # bb.x  /  role.bb.x
        if isinstance(base, ast.Name) and base.id in ("bb", "blackboard"):
            self.blackboard_reads.add(f".{prop}")
            return {"blackboard_value": f"mission.{prop}"}
        if (
            isinstance(base, ast.Attribute)
            and base.attr in ("bb", "blackboard")
            and isinstance(base.value, ast.Name)
        ):
            role = base.value.id
            if role not in self.s.roles:
                self.fail(node, f"未定义的角色 '{role}'", near(role, self.s.roles))
                return None
            self.blackboard_reads.add(f"{role}.{prop}")
            return {"blackboard_value": f"{role}.mission.{prop}"}
        # coord.generated_condition
        if isinstance(base, ast.Name) and base.id in self.s.coordination:
            generated = self.s.generated(base.id, "conditions")
            if prop not in generated:
                if prop in self.s.generated(base.id, "behaviors"):
                    self.fail(
                        node,
                        f"{base.id}.{prop} 是协同生成的行为（在行为树里调用 {base.id}.{prop}()），"
                        "不能当作条件。动作做完后要离开状态，写 time_in_state() >= <参数或时长>；"
                        "或者不拆状态，在同一状态的树里写 if 条件: 行为()。"
                        f"该协同的条件（{', '.join(generated) or '无'}）表示收到了新消息，"
                        "不表示本状态的动作已经完成，不要拿来代替",
                        generated,
                    )
                    return None
                self.fail(node, f"协同 {base.id} 没有生成条件 '{prop}'", generated)
                return None
            return {"condition_ref": f"{base.id}__{prop}"}
        # terrain.class / sensor("id").on
        component = None
        if isinstance(base, ast.Call) and isinstance(base.func, ast.Name):
            ns_name = base.func.id
            component = self.id_text(base.args[0]) if base.args else None
        elif isinstance(base, ast.Name):
            ns_name = base.id
        else:
            self.fail(node, "无法理解的属性访问")
            return None
        operand = self.ns.get(ns_name)
        if operand is None:
            self.fail(node, f"未知的操作数 '{ns_name}'", near(ns_name, self.ns))
            return None
        prop = PROPERTY_ALIASES.get((operand, prop), prop)
        catalog_op = self.s.catalog.operands[operand]
        if prop not in catalog_op.properties:
            self.fail(node, f"{ns_name} 没有属性 '{prop}'", near(prop, catalog_op.properties))
            return None
        out: dict[str, Any] = {"property": prop}
        if component:
            kind = catalog_op.component
            out[kind] = component
        return {operand: out}

    def method_property(self, node: ast.Call) -> dict[str, Any] | None:
        """terrain.distance_to_class("urban") — a property that takes an argument."""
        func = node.func
        assert isinstance(func, ast.Attribute)
        inner = self.attribute(func)
        if inner is None:
            return None
        operand, spec = next(iter(inner.items()))
        catalog_op = self.s.catalog.operands[operand]
        argument = catalog_op.properties[spec["property"]].argument
        if not argument:
            self.fail(node, f"{spec['property']} 不接受参数")
            return None
        if len(node.args) != 1:
            self.fail(node, f"{spec['property']} 需要一个参数（{argument}）")
            return None
        value = self.id_text(node.args[0])
        if value is None:
            self.fail(node, f"{spec['property']} 的参数应是字符串")
            return None
        spec[argument] = value
        return inner


# ---------------------------------------------------------------------- the block
@dataclass
class ConditionDef:
    name: str
    expression: dict[str, Any] | None
    line: int
    description: str = ""
    reads: set[str] = field(default_factory=set)


def condition_names(text: str) -> list[str]:
    """Names assigned in a conditions block (for linking before compiling)."""
    try:
        tree = ast.parse(prepare(text))
    except SyntaxError:
        return []
    return [
        stmt.targets[0].id
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ]


def _comments(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string.lstrip("#").strip()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def compile_conditions(
    text: str, symbols: ExprSymbols, block: str, line: int
) -> tuple[list[ConditionDef], list[SheetProblem]]:
    problems: list[SheetProblem] = []
    text = prepare(text)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], [
            SheetProblem(block, line + (exc.lineno or 1) - 1, f"Python 语法错误：{exc.msg}")
        ]
    comments = _comments(text)
    out: list[ConditionDef] = []
    seen: set[str] = set()
    for stmt in tree.body:
        here = line + stmt.lineno - 1
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # a docstring
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            problems.append(SheetProblem(block, here, "只允许 `名字 = 表达式` 形式的条件定义"))
            continue
        name = stmt.targets[0].id
        if name in seen:
            problems.append(SheetProblem(block, here, f"条件 '{name}' 重复定义"))
            continue
        seen.add(name)
        compiler = ExprCompiler(symbols, block, line)
        expression = compiler.expr(stmt.value)
        problems += compiler.problems
        out.append(
            ConditionDef(
                name,
                expression,
                here,
                comments.get(stmt.lineno, ""),
                compiler.blackboard_reads,
            )
        )
    return out, problems
