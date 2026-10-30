"""Behavior trees of a v3 tactic sheet (``python <role>`` blocks) → IR 2.0 nodes.

Each ``def`` is a tree. A function named like a state of the role (``def SEARCH_ADVANCE():``)
is that state's tree; any other function (lower case) is a subtree of the model that trees
call by name. Nothing is executed; the text is only parsed with :func:`ast.parse`.

==========================================  ==============================================
``nav.goto(lat_deg=…)``                     action leaf (action-set id, contract params)
``tactic("sense.track")``                   tactic-set subtree / state tree
``gap("smoke_screen", duration_s=30 * s)``  placeholder of a gap listed in ``yaml gaps``
``observe()``                               a subtree written in this sheet
``assert target_acquired``                  condition leaf (a condition name or expression)
several statements                          ``sequence_with_memory`` (resumes at the
                                            running child, the usual behavior-tree sequence)
``with sequence():`` / ``with selector():``  ``sequence_with_memory`` / ``selector_with_memory``
``with reactive_sequence():``               ``sequence`` (re-checks from the first child
``with reactive_selector():``               every update) / ``selector``
``with parallel("all"):``                   ``parallel`` (``"one"``, ``"all"`` or a count)
``with repeat(until_done=True):``           ``repeat`` (``count=n`` / ``duration=10 * s``)
``with inverter():`` / ``with succeeder():``  decorators
``if c: A`` / ``else: B``                   ``sequence(c, A)`` / ``selector(sequence(c, A), B)``
``try: A`` ``except: B``                    ``selector_with_memory(A, B)``
==========================================  ==============================================

Parameter values: numbers, strings, quantities (``500 * m`` or ``"500 m"``) and parameter
names (``search_route`` → ``$search_route``).
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.falcon.v3.pyexpr import ExprCompiler, ExprError, dotted, quantity
from afsim_research_agent.ir.units import infer_dimension

CONTROLS = {
    "sequence": "sequence_with_memory",
    "selector": "selector_with_memory",
    "fallback": "selector_with_memory",
    "reactive_sequence": "sequence",
    "reactive_selector": "selector",
    "parallel": "parallel",
    "repeat": "repeat",
    "inverter": "inverter",
    "succeeder": "succeeder",
    "negator": "negator",
}
DECORATORS = ("repeat", "inverter", "succeeder", "negator")


@dataclass
class TreeProblem:
    line: int  # 0-based line inside the block
    message: str
    candidates: list[str] = field(default_factory=list)


@dataclass
class TreeSymbols:
    actions: set[str]
    tactics: set[str]  # tactic-set subtrees and state trees
    gaps: dict[str, str]  # gap id → kind
    parameters: set[str]
    expr: ExprCompiler


@dataclass
class ParsedFunction:
    name: str
    line: int
    root: dict[str, Any] | None
    calls: list[str] = field(default_factory=list)  # local subtrees called


@dataclass
class ParsedBlock:
    functions: list[ParsedFunction] = field(default_factory=list)
    conditions: dict[str, tuple[dict[str, Any], int]] = field(default_factory=dict)  # auto
    problems: list[TreeProblem] = field(default_factory=list)


class _Translator:
    def __init__(self, symbols: TreeSymbols, local: set[str], prefix: str) -> None:
        self.s = symbols
        self.local = local
        self.prefix = prefix  # for generated condition ids
        self.out = ParsedBlock()
        self.calls: list[str] = []

    def fail(self, node: ast.AST, message: str, candidates: list[str] | None = None) -> None:
        self.out.problems.append(
            TreeProblem(max(0, getattr(node, "lineno", 1) - 1), message, candidates or [])
        )

    def function(self, fn: ast.FunctionDef) -> ParsedFunction:
        self.calls = []
        if fn.args.args or fn.args.vararg or fn.args.kwarg:
            self.fail(fn, f"树函数 {fn.name}() 不带参数")
        body = [s for s in fn.body if not _docstring(s)]
        root = self.body(body, fn)
        return ParsedFunction(fn.name, fn.lineno - 1, root, list(self.calls))

    def node(self, data: dict[str, Any], where: ast.AST) -> dict[str, Any]:
        data["_line"] = max(0, getattr(where, "lineno", 1) - 1)
        return data

    def body(self, stmts: list[ast.stmt], where: ast.AST) -> dict[str, Any] | None:
        nodes = [n for n in (self.statement(s) for s in stmts) if n is not None]
        if not nodes:
            if not any(isinstance(s, ast.Pass) for s in stmts):
                self.fail(where, "树是空的")
            return None
        if len(nodes) == 1:
            return nodes[0]
        return self.node({"type": "sequence_with_memory", "children": nodes}, stmts[0])

    def statement(self, stmt: ast.stmt) -> dict[str, Any] | None:
        if isinstance(stmt, ast.Pass):
            return None
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            return self.call(stmt.value)
        if isinstance(stmt, ast.Assert):
            return self.condition(stmt.test, stmt)
        if isinstance(stmt, ast.With):
            return self.with_(stmt)
        if isinstance(stmt, ast.If):
            return self.if_(stmt)
        if isinstance(stmt, ast.Try):
            return self.try_(stmt)
        if isinstance(stmt, ast.Assign | ast.AugAssign | ast.AnnAssign):
            self.fail(
                stmt,
                "树中不能给黑板赋值：IR 2.0 的叶节点只有动作、条件、战术集引用和子树；"
                "需要记录状态时使用会写黑板的动作，或登记缺口",
            )
            return None
        self.fail(stmt, f"不支持的语句：{ast.unparse(stmt)[:60]}")
        return None

    # ------------------------------------------------------------------ leaves
    def condition(self, test: ast.expr, where: ast.AST) -> dict[str, Any] | None:
        if isinstance(test, ast.Name) and test.id in self.s.expr.s.conditions:
            return self.node({"type": "condition", "condition": test.id}, where)
        try:
            expr = self.s.expr.compile_node(test)
        except ExprError as exc:
            self.fail(where, str(exc), exc.candidates)
            return None
        cid = f"{self.prefix}_c{len(self.out.conditions) + 1}"
        self.out.conditions[cid] = (expr, max(0, getattr(where, "lineno", 1) - 1))
        return self.node({"type": "condition", "condition": cid}, where)

    def call(self, call: ast.Call) -> dict[str, Any] | None:
        name = dotted(call.func)
        if name is None:
            self.fail(call, f"无法识别的调用：{ast.unparse(call)[:60]}")
            return None
        if name in ("tactic", "gap"):
            if not call.args or not isinstance(call.args[0], ast.Constant):
                self.fail(call, f'{name}() 的第一个参数写成字符串，如 {name}("…")')
                return None
            ref = str(call.args[0].value)
            params = self.params(call)
            if name == "tactic":
                if ref not in self.s.tactics:
                    self.fail(
                        call,
                        f"'{ref}' 不是战术集的子树或状态树",
                        difflib.get_close_matches(ref, sorted(self.s.tactics), n=5, cutoff=0.4),
                    )
                    return None
                return self.node({"type": "tactic", "tactic": ref, "params": params}, call)
            kind = self.s.gaps.get(ref)
            if kind is None:
                self.fail(call, f"缺口 '{ref}' 没有在 yaml gaps 中登记", sorted(self.s.gaps))
                return None
            if kind == "subtree":
                return self.node(
                    {"type": "subtree", "subtree": f"gap.{ref}", "params": params}, call
                )
            return self.node({"type": "action", "action": f"gap.{ref}", "params": params}, call)
        if name in CONTROLS:
            self.fail(call, f"{name}(…) 是控制节点，写成 with {name}(…): 并在下面缩进子节点")
            return None
        if "." not in name:
            if name in self.local:
                if call.args or call.keywords:
                    self.fail(call, f"子树 {name}() 不带参数")
                self.calls.append(name)
                return self.node({"type": "subtree", "subtree": name}, call)
            self.fail(
                call,
                f"'{name}' 不是本稿定义的子树函数",
                difflib.get_close_matches(name, sorted(self.local), n=5, cutoff=0.4),
            )
            return None
        if name not in self.s.actions:
            self.fail(
                call,
                f"'{name}' 不是动作集中的动作；能力集里没有时，在 yaml gaps 中登记并写 gap(\"…\")",
                difflib.get_close_matches(name, sorted(self.s.actions), n=5, cutoff=0.4),
            )
            return None
        if call.args:
            self.fail(call, f"动作 {name} 的参数写成关键字参数，如 {name}(参数=值)")
        return self.node({"type": "action", "action": name, "params": self.params(call)}, call)

    def params(self, call: ast.Call) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for kw in call.keywords:
            if kw.arg is None:
                self.fail(call, "不支持 **参数")
                continue
            value = self.value(kw.value)
            if value is not None:
                out[kw.arg] = value
        return out

    def value(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                q = quantity(node.value)
                return q if q is not None else node.value
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = self.value(node.operand)
            if isinstance(inner, int | float):
                return -inner
            if isinstance(inner, dict) and "unit" in inner:
                return {"value": -inner["value"], "unit": inner["unit"]}
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, int | float)
            and isinstance(node.right, ast.Name)
            and infer_dimension(node.right.id)
        ):
            return {"value": node.left.value, "unit": node.right.id}
        if isinstance(node, ast.Name):
            if node.id in self.s.parameters:
                return f"${node.id}"
            if node.id in ("True", "False"):
                return node.id == "True"
            self.fail(
                node,
                f"'{node.id}' 不是已定义的参数（在 yaml model 的 parameters 中定义）",
                difflib.get_close_matches(node.id, sorted(self.s.parameters), n=5, cutoff=0.4),
            )
            return None
        self.fail(node, f"不支持的参数值：{ast.unparse(node)[:60]}")
        return None

    # ------------------------------------------------------------------ controls
    def with_(self, stmt: ast.With) -> dict[str, Any] | None:
        if len(stmt.items) != 1 or not isinstance(stmt.items[0].context_expr, ast.Call):
            self.fail(stmt, 'with 后写一个控制节点，如 with parallel("all"):')
            return None
        call = stmt.items[0].context_expr
        name = dotted(call.func) or ""
        kind = CONTROLS.get(name)
        if kind is None:
            self.fail(stmt, f"'{name}' 不是控制节点", sorted(CONTROLS))
            return None
        kids = [n for n in (self.statement(s) for s in stmt.body) if n is not None]
        if not kids:
            self.fail(stmt, f"{name} 下面没有子节点")
            return None
        out: dict[str, Any] = {"type": kind}
        if name in DECORATORS:
            out["children"] = [
                kids[0]
                if len(kids) == 1
                else self.node({"type": "sequence_with_memory", "children": kids}, stmt)
            ]
        else:
            out["children"] = kids
        if kind == "parallel":
            out["policy"] = self.policy(call)
        elif kind == "repeat":
            rep = self.repeat(call)
            if rep is None:
                return None
            out["repeat"] = rep
        elif call.args or call.keywords:
            self.fail(stmt, f"{name}() 不带参数")
        return self.node(out, stmt)

    def policy(self, call: ast.Call) -> dict[str, Any]:
        arg: ast.expr | None = call.args[0] if call.args else None
        for kw in call.keywords:
            if kw.arg in ("policy", "success"):
                arg = kw.value
        if arg is None:
            return {"type": "all"}
        if isinstance(arg, ast.Constant):
            if arg.value in ("all", "one"):
                return {"type": arg.value}
            if isinstance(arg.value, int) and not isinstance(arg.value, bool) and arg.value > 0:
                return {"type": "threshold", "count": arg.value}
        self.fail(call, 'parallel 的成功策略写 "all"、"one" 或一个正整数')
        return {"type": "all"}

    def repeat(self, call: ast.Call) -> dict[str, Any] | None:
        given = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        if call.args or len(given) != 1:
            self.fail(call, "repeat 写一个关键字：until_done=True、count=次数 或 duration=时长")
            return None
        key, node = next(iter(given.items()))
        if key == "until_done":
            return {"until_done": True}
        if key == "count" and isinstance(node, ast.Constant) and isinstance(node.value, int):
            return {"count": node.value}
        if key in ("duration", "duration_s"):
            value = self.value(node)
            if isinstance(value, int | float) and not isinstance(value, bool):
                return {"duration_s": float(value)}
            if isinstance(value, dict) and infer_dimension(value["unit"]) == "time":
                from afsim_research_agent.ir.units import to_canonical

                return {"duration_s": to_canonical(float(value["value"]), value["unit"], "time")}
        self.fail(call, "repeat 写一个关键字：until_done=True、count=次数 或 duration=时长")
        return None

    def if_(self, stmt: ast.If) -> dict[str, Any] | None:
        cond = self.condition(stmt.test, stmt)
        then = self.body(stmt.body, stmt)
        if cond is None or then is None:
            return None
        guarded = self.node({"type": "sequence", "children": [cond, then]}, stmt)
        if not stmt.orelse:
            return guarded
        other = self.body(stmt.orelse, stmt.orelse[0])
        if other is None:
            return guarded
        return self.node({"type": "selector", "children": [guarded, other]}, stmt)

    def try_(self, stmt: ast.Try) -> dict[str, Any] | None:
        first = self.body(stmt.body, stmt)
        fallbacks = [self.body(h.body, h) for h in stmt.handlers]
        kids = [k for k in [first, *fallbacks] if k is not None]
        if len(kids) < 2:
            self.fail(stmt, "try/except 需要两部分都有节点")
            return kids[0] if kids else None
        return self.node({"type": "selector_with_memory", "children": kids}, stmt)


def _docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def parse_block(text: str, symbols: TreeSymbols, prefix: str) -> ParsedBlock:
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        out = ParsedBlock()
        out.problems.append(
            TreeProblem(max(0, (exc.lineno or 1) - 1), f"Python 语法错误：{exc.msg}")
        )
        return out
    functions = [s for s in module.body if isinstance(s, ast.FunctionDef)]
    local = {f.name for f in functions}
    tr = _Translator(symbols, local, prefix)
    for stmt in module.body:
        if isinstance(stmt, ast.FunctionDef) or _docstring(stmt):
            continue
        tr.fail(stmt, "块里只写 def 树函数")
    seen: set[str] = set()
    for fn in functions:
        if fn.name in seen:
            tr.fail(fn, f"函数 {fn.name} 定义了两次")
            continue
        seen.add(fn.name)
        tr.out.functions.append(tr.function(fn))
    return tr.out
