"""Python condition expressions of a v3 tactic sheet → IR 2.0 expressions.

Only a small subset is accepted; nothing is executed (``ast.parse`` only):

==========================================  ==========================================
``sense.target.valid``                       ``{bb: sense.target.valid}``
``a and b`` / ``a or b`` / ``not a``         ``all`` / ``any`` / ``not``
``x <= line_tolerance``                      ``compare`` (``==  !=  <  <=  >  >=``)
``x < 500 * m`` or ``x < "500 m"``           quantity ``{value: 500, unit: m}``
``line_tolerance`` (a parameter)             ``{parameter_ref: line_tolerance}``
``target_lost`` (another condition)          ``{condition_ref: target_lost}``
``time_in_state() >= 10 * s``                ``time_in_state_gte``
``elapsed_since(mission.t) >= 5 * s``        ``elapsed_since_gte``
==========================================  ==========================================

A dotted name is a blackboard field: one of the capability set's fields, or a
``mission.<name>`` field the sheet declares.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.ir.units import infer_dimension

OPS = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "lte",
    ast.Gt: "gt",
    ast.GtE: "gte",
}
_QUANTITY = re.compile(r"^\s*(?P<num>-?\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z/°_]+)\s*$")
FUNCTIONS = {"time_in_state": "time_in_state_gte", "elapsed_since": "elapsed_since_gte"}


class ExprError(ValueError):
    def __init__(self, message: str, line_offset: int = 0, candidates: list[str] | None = None):
        super().__init__(message)
        self.line_offset = line_offset
        self.candidates = candidates or []


@dataclass
class Symbols:
    fields: set[str]  # blackboard fields (capability set + declared mission.*)
    parameters: set[str]
    conditions: set[str] = field(default_factory=set)


def quantity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    m = _QUANTITY.match(value)
    if not m or infer_dimension(m.group("unit")) is None:
        return None
    number = float(m.group("num"))
    return {"value": int(number) if number.is_integer() else number, "unit": m.group("unit")}


def dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class ExprCompiler:
    def __init__(self, symbols: Symbols) -> None:
        self.s = symbols

    def compile(self, text: str) -> dict[str, Any]:
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as exc:
            raise ExprError(f"表达式语法错误：{exc.msg}") from exc
        return self.boolean(tree.body)

    def compile_node(self, node: ast.AST) -> dict[str, Any]:
        return self.boolean(node)

    # ------------------------------------------------------------------ boolean
    def boolean(self, node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.BoolOp):
            key = "all" if isinstance(node.op, ast.And) else "any"
            return {key: [self.boolean(v) for v in node.values]}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return {"not": self.boolean(node.operand)}
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return {"literal": node.value}
        if isinstance(node, ast.Name):
            if node.id in self.s.conditions:
                return {"condition_ref": node.id}
            if node.id in self.s.parameters:
                return {"parameter_ref": node.id}
            raise self._unknown(node.id, node, "条件或参数")
        path = dotted(node)
        if path is not None:
            return self.field(path, node)
        raise ExprError(f"不支持的条件写法：{ast.unparse(node)[:60]}", self._line(node))

    def compare(self, node: ast.Compare) -> dict[str, Any]:
        parts = []
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            if type(op) not in OPS:
                raise ExprError(f"不支持的比较运算：{ast.unparse(node)[:60]}", self._line(node))
            call = self._function(left)
            if call is not None:
                if not isinstance(op, ast.GtE):
                    raise ExprError(f"{call[0]}() 只能写成 {call[0]}(…) >= 时长", self._line(node))
                parts.append(self._call(call, right))
            else:
                parts.append(
                    {
                        "compare": {
                            "left": self.operand(left),
                            "operator": OPS[type(op)],
                            "right": self.operand(right),
                        }
                    }
                )
            left = right
        return parts[0] if len(parts) == 1 else {"all": parts}

    def _function(self, node: ast.AST) -> tuple[str, list[ast.expr]] | None:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FUNCTIONS:
                return node.func.id, list(node.args)
        return None

    def _call(self, call: tuple[str, list[ast.expr]], right: ast.AST) -> dict[str, Any]:
        name, args = call
        duration = self.operand(right)
        if name == "time_in_state":
            if args:
                raise ExprError("time_in_state() 不带参数")
            arguments: dict[str, Any] = {"duration": duration}
        else:
            if len(args) != 1 or dotted(args[0]) is None:
                raise ExprError("elapsed_since(字段) 需要一个记录时刻的黑板字段")
            path = dotted(args[0]) or ""
            self.field(path, args[0])
            arguments = {"field": path, "duration": duration}
        return {"function_call": {"name": FUNCTIONS[name], "arguments": arguments}}

    # ------------------------------------------------------------------ operands
    def operand(self, node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                q = quantity(node.value)
                return q if q is not None else {"literal": node.value}
            if isinstance(node.value, bool | int | float):
                return {"literal": node.value}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = self.operand(node.operand)
            if "literal" in inner and isinstance(inner["literal"], int | float):
                return {"literal": -inner["literal"]}
            if "unit" in inner:
                return {"value": -inner["value"], "unit": inner["unit"]}
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            # 500 * m
            if isinstance(node.left, ast.Constant) and isinstance(node.right, ast.Name):
                if isinstance(node.left.value, int | float) and infer_dimension(node.right.id):
                    return {"value": node.left.value, "unit": node.right.id}
        if isinstance(node, ast.Name):
            if node.id in self.s.parameters:
                return {"parameter_ref": node.id}
            raise self._unknown(node.id, node, "参数")
        path = dotted(node)
        if path is not None:
            return self.field(path, node)
        raise ExprError(f"不支持的取值写法：{ast.unparse(node)[:60]}", self._line(node))

    def field(self, path: str, node: ast.AST) -> dict[str, Any]:
        if path in self.s.fields:
            return {"bb": path}
        raise ExprError(
            f"'{path}' 不是能力集或本模型声明的黑板字段",
            self._line(node),
            difflib.get_close_matches(path, sorted(self.s.fields), n=5, cutoff=0.5),
        )

    def _unknown(self, name: str, node: ast.AST, what: str) -> ExprError:
        pool = sorted(self.s.parameters | self.s.conditions)
        return ExprError(
            f"'{name}' 不是已定义的{what}",
            self._line(node),
            difflib.get_close_matches(name, pool, n=5, cutoff=0.5),
        )

    @staticmethod
    def _line(node: ast.AST) -> int:
        return max(0, getattr(node, "lineno", 1) - 1)
