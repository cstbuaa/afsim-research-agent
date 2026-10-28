"""Behavior trees written as Python functions (``python <role>`` blocks).

Code-BT (IJCAI 2025) found that models write correct trees more often as constrained
Python mapped to a tree by fixed AST rules than as tree XML. This module is that
mapping: a restricted Python subset → the same :class:`XmlNode` shape ``btxml.py``
produces, so the compiler checks both forms with one code path. Nothing is executed;
the text is only parsed with :func:`ast.parse`.

    def travel_open():                          # tree of state travel_open
        bb.current_formation = "wedge"          # <SetBlackboard key=… value=…/>
        platoon_follow.announce_formation()     # <Coordination use="…"/>
        FollowRoute(route="main_route", speed=travel_speed_open)

    def report_once():                          # a subtree: report_once()
        if not bb.contact_reported:             # if → ReactiveFallback
            coord_report.report()
            bb.contact_reported = True

Mapping (each line keeps its sheet line number for error messages):

==============================  =================================================
``def name():``                 ``<BehaviorTree ID="name">``; several statements
                                → ``Sequence``
``Action(port=value, …)``       action node (CamelCase or snake_case name)
``coord.behavior()``            ``<Coordination use="coord.behavior"/>``
``coord.tree()`` / ``name()``   ``<SubTree ID=…/>`` (generated tree / function here)
``bb.var = value``              ``<SetBlackboard key="var" value="value"/>``
``assert cond``                 ``<Condition ID="cond"/>``
``if c: A`` / ``else: B``       ``ReactiveFallback(Condition(not c), A)`` /
                                ``ReactiveFallback(ReactiveSequence(c, A),
                                ReactiveSequence(not c, B))``
``try: A`` ``except: B``        ``Fallback(A, B)``
``with ctl(…):``                control or decorator node over the block
==============================  =================================================
"""

from __future__ import annotations

import ast
import re

from afsim_research_agent.falcon.sheet.blocks import SheetProblem
from afsim_research_agent.falcon.sheet.btxml import MAX_DEPTH, MAX_NODES, XmlNode

# with-block helpers → (tag, positional attribute name)
WITH_CONTROLS: dict[str, tuple[str, str | None]] = {
    "sequence": ("Sequence", None),
    "reactive": ("ReactiveSequence", None),
    "reactive_sequence": ("ReactiveSequence", None),
    "fallback": ("Fallback", None),
    "reactive_fallback": ("ReactiveFallback", None),
    "parallel": ("Parallel", "success_count"),
    "repeat": ("Repeat", "num_cycles"),
    "retry": ("RetryUntilSuccessful", "num_attempts"),
    "force_success": ("ForceSuccess", None),
    "invert": ("Inverter", None),
    "once": ("Once", None),  # expanded by the compiler: once per entry into the state
}
DECORATOR_TAGS = {"Repeat", "RetryUntilSuccessful", "ForceSuccess", "Inverter"}
# keyword spellings of the reserved XML attributes
PORT_ALIASES = {
    "success_if": "_successIf",
    "failure_if": "_failureIf",
    "while_": "_while",
    "only_while": "_while",
}
SUPPORTED = (
    "可用写法：动作调用 Action(端口=值)、协同.行为()、子树函数()、bb.变量 = 值、assert 条件、"
    "if/elif/else、try/except、with sequence()/reactive()/fallback()/reactive_fallback()/"
    "parallel(n)/repeat(n)/retry(n)/force_success()/invert()/once()"
)


class _Stop(Exception):
    pass


def snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


class _Translator:
    def __init__(self, block: str, first_line: int, coord_trees: set[str]) -> None:
        self.block = block
        self.offset = first_line - 1
        self.coord_trees = coord_trees
        self.functions: set[str] = set()
        self.problems: list[SheetProblem] = []
        self.failed: set[str] = set()  # functions with a failing statement
        self.count = 0

    # ------------------------------------------------------------------ helpers
    def line(self, node: ast.AST) -> int:
        return self.offset + getattr(node, "lineno", 1)

    def fail(self, node: ast.AST, message: str) -> None:
        self.problems.append(SheetProblem(self.block, self.line(node), message))

    def make(self, tag: str, attrs: dict[str, str], node: ast.AST, depth: int) -> XmlNode:
        self.count += 1
        if self.count > MAX_NODES or depth > MAX_DEPTH:
            raise _Stop("行为树节点过多或嵌套过深")
        return XmlNode(tag, attrs, self.line(node))

    def text(self, node: ast.expr) -> str:
        return ast.unparse(node)

    # ------------------------------------------------------------------ module
    def module(self, tree: ast.Module, line: int) -> XmlNode | None:
        root = XmlNode("root", {"BTCPP_format": "4"}, line)
        defs = [s for s in tree.body if isinstance(s, ast.FunctionDef)]
        self.functions = {d.name for d in defs}
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef):
                bt = self.function(stmt)
                if bt is not None:
                    root.children.append(bt)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue  # a module docstring
            else:
                self.fail(stmt, "顶层只能写 def 树名():（每个函数是一棵树：状态名或子树名）")
        return root

    def function(self, fn: ast.FunctionDef) -> XmlNode | None:
        args = fn.args
        if args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg:
            self.fail(fn, f"树函数 {fn.name}() 不能有参数")
            return None
        if fn.decorator_list or fn.returns is not None:
            self.fail(fn, f"树函数 {fn.name}() 不能有装饰器或返回类型")
            return None
        bt = self.make("BehaviorTree", {"ID": fn.name}, fn, 0)
        body = self.body(fn.body, 1)
        if body is None:
            self.failed.add(fn.name)  # the failing statements are reported already
            return bt
        if not body:
            self.fail(fn, f"树函数 {fn.name}() 是空的")
            self.failed.add(fn.name)
            return bt
        bt.children.append(self.sequence(body, fn, 1))
        return bt

    def sequence(self, nodes: list[XmlNode], where: ast.AST, depth: int) -> XmlNode:
        if len(nodes) == 1:
            return nodes[0]
        seq = self.make("Sequence", {}, where, depth)
        seq.children = nodes
        return seq

    def body(self, stmts: list[ast.stmt], depth: int) -> list[XmlNode] | None:
        out: list[XmlNode] = []
        ok = True
        for i, stmt in enumerate(stmts):
            if (
                i == 0
                and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue  # docstring
            if isinstance(stmt, ast.Pass):
                continue
            node = self.statement(stmt, depth)
            if node is None:
                ok = False
            else:
                out.append(node)
        return out if ok else None

    # ------------------------------------------------------------------ statements
    def statement(self, stmt: ast.stmt, depth: int) -> XmlNode | None:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            return self.call(stmt.value, depth)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            self.fail(stmt, "语句中间的字符串没有作用；说明请写成 # 注释")
            return None
        if isinstance(stmt, ast.Assert):
            return self.make("Condition", {"ID": self.text(stmt.test)}, stmt, depth)
        if isinstance(stmt, ast.Assign):
            return self.assign(stmt, depth)
        if isinstance(stmt, ast.If):
            return self.if_(stmt, depth)
        if isinstance(stmt, ast.With):
            return self.with_(stmt, depth)
        if isinstance(stmt, ast.Try):
            return self.try_(stmt, depth)
        if isinstance(stmt, (ast.While, ast.For)):
            self.fail(
                stmt,
                "不支持 while/for：每周期重新检查用 with reactive(): assert 条件 …；"
                "重复用 with repeat(n): / with retry(n):",
            )
            return None
        if isinstance(stmt, ast.Return):
            self.fail(
                stmt, "树函数不能 return；失败用 assert 条件，跳过失败用 with force_success():"
            )
            return None
        self.fail(stmt, f"不支持的语句 {type(stmt).__name__}。{SUPPORTED}")
        return None

    def assign(self, stmt: ast.Assign, depth: int) -> XmlNode | None:
        target = stmt.targets[0] if len(stmt.targets) == 1 else None
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "bb"
        ):
            self.fail(stmt, "赋值只能写本角色黑板：bb.变量 = 值")
            return None
        value = self.value(stmt.value)
        if value is None:
            self.fail(stmt, f"黑板值 {self.text(stmt.value)} 应是字符串、数、true/false 或参数名")
            return None
        return self.make("SetBlackboard", {"key": target.attr, "value": value}, stmt, depth)

    def if_(self, stmt: ast.If, depth: int) -> XmlNode | None:
        then = self.body(stmt.body, depth + 2)
        other = self.body(stmt.orelse, depth + 2) if stmt.orelse else []
        if then is None or other is None:
            return None
        cond = self.text(stmt.test)
        negated = _negate(cond)
        node = self.make("ReactiveFallback", {}, stmt, depth)
        if not stmt.orelse:
            if not then:
                self.fail(stmt, "if 分支是空的")
                return None
            node.children = [
                self.make("Condition", {"ID": negated}, stmt, depth + 1),
                self.sequence(then, stmt, depth + 1),
            ]
            return node
        branches = []
        for test, nodes, where in ((cond, then, stmt), (negated, other, stmt.orelse[0])):
            branch = self.make("ReactiveSequence", {}, where, depth + 1)
            branch.children = [self.make("Condition", {"ID": test}, where, depth + 2)]
            if nodes:
                branch.children.append(self.sequence(nodes, where, depth + 2))
            branches.append(branch)
        node.children = branches
        return node

    def try_(self, stmt: ast.Try, depth: int) -> XmlNode | None:
        if len(stmt.handlers) != 1 or stmt.orelse or stmt.finalbody:
            self.fail(
                stmt, "try 只能带一个 except（不写 else/finally）：try 失败时执行 except 分支"
            )
            return None
        first = self.body(stmt.body, depth + 1)
        second = self.body(stmt.handlers[0].body, depth + 1)
        if first is None or second is None:
            return None
        if not first or not second:
            self.fail(stmt, "try 与 except 分支都不能为空")
            return None
        node = self.make("Fallback", {}, stmt, depth)
        node.children = [
            self.sequence(first, stmt, depth + 1),
            self.sequence(second, stmt.handlers[0], depth + 1),
        ]
        return node

    def with_(self, stmt: ast.With, depth: int) -> XmlNode | None:
        item = stmt.items[0] if len(stmt.items) == 1 else None
        expr = item.context_expr if item is not None else None
        call = expr if isinstance(expr, ast.Call) else None
        name = call.func.id if call is not None and isinstance(call.func, ast.Name) else None
        if item is None or item.optional_vars is not None or name not in WITH_CONTROLS:
            self.fail(stmt, f"with 只能写 {'/'.join(f'{k}()' for k in WITH_CONTROLS)} 之一")
            return None
        assert call is not None and name is not None
        tag, positional = WITH_CONTROLS[name]
        attrs: dict[str, str] = {}
        if call.args:
            if positional is None or len(call.args) > 1:
                self.fail(stmt, f"{name}() 不接受位置参数")
                return None
            attrs[positional] = self.text(call.args[0])
        for kw in call.keywords:
            if kw.arg is None:
                self.fail(stmt, f"{name}() 不接受 **参数")
                return None
            attrs[kw.arg] = self.text(kw.value)
        for key, raw in attrs.items():
            if key != "name" and not re.fullmatch(r"\d+", raw):
                self.fail(stmt, f"{name}() 的 {key} 应是整数，得到 {raw}")
                return None
        children = self.body(stmt.body, depth + 1)
        if children is None:
            return None
        if not children:
            self.fail(stmt, f"with {name}(): 下面没有语句")
            return None
        node = self.make(tag, attrs, stmt, depth)
        if tag in DECORATOR_TAGS:
            node.children = [self.sequence(children, stmt, depth + 1)]
        else:
            node.children = children
        return node

    # ------------------------------------------------------------------ calls
    def call(self, call: ast.Call, depth: int) -> XmlNode | None:
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            attrs = self.name_only(call, f"{self.text(func)}()")
            if attrs is None:
                return None
            ref = f"{func.value.id}.{func.attr}"
            if func.attr == "tree" or ref in self.coord_trees:
                return self.make("SubTree", {"ID": ref, **attrs}, call, depth)
            return self.make("Coordination", {"use": ref, **attrs}, call, depth)
        if not isinstance(func, ast.Name):
            self.fail(call, f"无法识别的调用 {self.text(func)}。{SUPPORTED}")
            return None
        name = func.id
        if name in self.functions:
            attrs = self.name_only(call, f"子树 {name}()")
            if attrs is None:
                return None
            return self.make("SubTree", {"ID": name, **attrs}, call, depth)
        if name in WITH_CONTROLS:
            self.fail(call, f"{name}() 是控制结构，写成 with {name}(): 再缩进子语句")
            return None
        if call.args:
            self.fail(call, f"{name}(…) 的参数要写成 端口=值")
            return None
        tag = name if name[:1].isupper() else snake_to_camel(name)
        attrs: dict[str, str] = {}
        for kw in call.keywords:
            if kw.arg is None:
                self.fail(call, f"{name}() 不接受 **参数")
                return None
            port = PORT_ALIASES.get(kw.arg, kw.arg)
            if port.startswith("_"):  # condition-valued attributes take any expression
                value = kw.value.value if isinstance(kw.value, ast.Constant) else None
                attrs[port] = value if isinstance(value, str) else self.text(kw.value)
                continue
            value = self.value(kw.value)
            if value is None:
                self.fail(
                    call,
                    f"{name}() 的端口 {kw.arg} 应是字符串、数、参数名、bb.变量 或 协同ID.消息，"
                    f"得到 {self.text(kw.value)}",
                )
                return None
            attrs[port] = value
        return self.make(tag, attrs, call, depth)

    def name_only(self, call: ast.Call, what: str) -> dict[str, str] | None:
        """Coordination and subtree calls take no arguments except ``name="节点名"``."""
        attrs: dict[str, str] = {}
        extra = list(call.args)
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                attrs["name"] = str(kw.value.value)
            else:
                extra.append(kw.value)
        if extra:
            self.fail(call, f'{what} 不带参数（只能写 name="节点名"）')
            return None
        return attrs

    def value(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return "true" if node.value else "false"
            if isinstance(node.value, (str, int, float)):
                return str(node.value)
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = self.value(node.operand)
            return f"-{inner}" if inner is not None else None
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts = _dotted(node)
            if parts is None:
                return None
            if parts[0] == "bb" and len(parts) == 2:
                return "{" + parts[1] + "}"
            if len(parts) == 3 and parts[1] == "bb":
                return "{" + f"{parts[0]}.{parts[2]}" + "}"
            return ".".join(parts)
        return None


def _dotted(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(parts)]


def _negate(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"not\s+([A-Za-z_][\w.]*)", stripped)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z_][\w.]*", stripped):
        return f"not {stripped}"
    return f"not ({stripped})"


def parse_pytree(
    text: str, block: str, line: int, coord_trees: set[str] | None = None
) -> tuple[XmlNode | None, list[SheetProblem], set[str]]:
    """``python <role>`` block → ``<root>`` with one ``<BehaviorTree>`` per function,
    the problems, and the functions left incomplete by a failing statement.

    ``coord_trees``: ``coord.tree`` names generated by coordination patterns (calls to
    them become ``SubTree``; other ``coord.x()`` calls become ``Coordination``)."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        where = line + (exc.lineno or 1) - 1
        return None, [SheetProblem(block, where, f"Python 语法错误：{exc.msg}")], set()
    translator = _Translator(block, line, coord_trees or set())
    try:
        root = translator.module(tree, line)
    except _Stop as exc:
        return None, [SheetProblem(block, line, str(exc))], set()
    if not root.children and not translator.problems:
        return None, [SheetProblem(block, line, "没有树函数（def 状态名(): …）")], set()
    return root, translator.problems, translator.failed
