"""``python -m afsim_research_agent.orchestrator`` — run and check the deep agent.

Subcommands:

- ``tools``  list the tools, their flags and which extensions are connected (no model).
- ``check``  offline wiring check: builds the real deep agent with a scripted model,
             runs the deterministic tools, an approval pause and an ``ask_human`` pause.
             Needs deepagents installed, no API key, no network.
- ``probe``  live function-calling probe of the configured qwen model on the real tools
             (read-only tools only). Needs DASHSCOPE_API_KEY.
- ``run``    run the agent on a requirement in the terminal; answers approval and
             ``ask_human`` pauses interactively.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from afsim_research_agent.orchestrator.extensions import get_registry
from afsim_research_agent.orchestrator.tools import TOOL_SPECS
from afsim_research_agent.orchestrator.workspace import Workspace


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m afsim_research_agent.orchestrator")
    parser.add_argument(
        "--data-dir", type=Path, help="工作区根目录（默认 AFSIM_RESEARCH_DATA_DIR 或 ./data）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tools", help="列出工具与扩展接入情况")
    chk = sub.add_parser("check", help="离线检查 agent 组装、工具接口和人工暂停（不调用模型）")
    chk.add_argument(
        "--package", help="用一个已有模型包（工作区相对路径）跑 falcon_check / simulate"
    )
    prb = sub.add_parser("probe", help="实测 qwen 的函数调用（只用只读工具）")
    prb.add_argument("--package", required=True, help="一个已有模型包（工作区相对路径）")
    prb.add_argument("--trials", type=int, default=5)
    prb.add_argument("--model", help="覆盖 DASHSCOPE_CHAT_MODEL")
    prb.add_argument("--out", type=Path, help="报告 JSON 路径（默认 runs/agent-probe/<时间>.json）")
    ui = sub.add_parser("ui", help="启动本地网页界面（默认 http://127.0.0.1:18765）")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=18765)
    ui.add_argument(
        "--demo", action="store_true", help="演示模式：不调用模型，用脚本化过程展示界面"
    )
    ui.add_argument(
        "--checkpoint-db",
        type=Path,
        help="SQLite 检查点文件（默认 runs/agent-ui/checkpoints.sqlite；需要 langgraph-checkpoint-sqlite）",
    )
    run = sub.add_parser("run", help="在终端运行 agent")
    run.add_argument("requirement", nargs="?", help="自然语言需求；缺省从终端读入")
    run.add_argument("--thread", help="会话 id（配合 --resume 续跑）")
    run.add_argument(
        "--checkpoint-db", type=Path, help="SQLite 检查点文件（需要 langgraph-checkpoint-sqlite）"
    )
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    ws = Workspace(args.data_dir.resolve()) if args.data_dir else Workspace.from_settings()
    if args.command == "tools":
        return _tools()
    if args.command == "check":
        return _check(ws, args.package)
    if args.command == "ui":
        return _ui(ws, args)
    if args.command == "probe":
        return _probe(ws, args)
    return _run(ws, args)


# ============================================================================ tools
def _tools() -> int:
    reg = get_registry()
    print(f"{'工具':<26}{'分组':<11}{'调用模型':<8}{'需人工批准'}")
    for s in TOOL_SPECS:
        print(
            f"{s.name:<26}{s.group:<11}{'是' if s.uses_model else '':<8}{'是' if s.needs_approval else ''}"
        )
    print(f"{'ask_human':<26}{'human':<11}{'':<8}在工具内暂停")
    print("\n扩展接入：")
    for slot, state in reg.status().items():
        print(f"  {slot:<20}{state}")
    for p in reg.check():
        print(f"  ⚠ {p}")
    return 0


# ============================================================================ check
def _check(ws: Workspace, package: str | None) -> int:
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from afsim_research_agent.orchestrator.graph import build_agent
    from afsim_research_agent.orchestrator.testing import ScriptedChatModel, call

    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    first = [call("overview")]
    if package:
        first += [
            call("falcon_check", {"package": package}),
            call("simulate", {"package": package}),
        ]
    first += [
        call("list_actions"),
        call("platform_export", {"package": package or "/runs/none", "out_dir": "/runs/_never"}),
    ]
    turns = [
        first,
        "第一轮完成。",
        [
            call(
                "ask_human",
                {"question": "受损时继续交替跃进还是撤出？", "options": ["继续", "撤出"]},
            )
        ],
        "已记录用户回答。",
    ]
    model = ScriptedChatModel(turns=turns)
    try:
        agent = build_agent(model, workspace=ws, checkpointer=InMemorySaver(), toolset="all")
        record("create_deep_agent 组装", True)
    except Exception as exc:  # noqa: BLE001
        record("create_deep_agent 组装", False, f"{type(exc).__name__}: {exc}")
        return 1
    config = {"configurable": {"thread_id": f"check-{uuid.uuid4().hex[:6]}"}}
    out = agent.invoke({"messages": [HumanMessage("自检")]}, config)
    expected = {s.name for s in TOOL_SPECS} | {
        "ask_human",
        "read_file",
        "ls",
        "task",
        "write_todos",
    }
    missing = expected - set(model.seen_tools)
    record(
        "工具对模型可见",
        not missing,
        f"缺少 {sorted(missing)}" if missing else f"{len(model.seen_tools)} 个",
    )
    pauses = out.get("__interrupt__") or []
    hitl = [p for p in pauses if isinstance(p.value, dict) and "action_requests" in p.value]
    names = [r["name"] for p in hitl for r in p.value["action_requests"]]
    record("需批准的工具在执行前暂停", names == ["platform_export"], f"暂停于 {names}")
    out = agent.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "自检：不导出"}]}), config
    )
    msgs = out["messages"]
    tool_msgs = {m.name: m.content for m in msgs if getattr(m, "type", "") == "tool"}
    for name in ["overview", "list_actions"] + (["falcon_check", "simulate"] if package else []):
        content = tool_msgs.get(name, "")
        try:
            status = json.loads(content)["status"]
            record(f"工具 {name} 返回结构化结果", True, f"status={status}")
        except Exception:  # noqa: BLE001
            record(f"工具 {name} 返回结构化结果", False, content[:120])
    record(
        "拒绝后工具未执行",
        "platform_export" in tool_msgs and "status" not in tool_msgs["platform_export"][:40],
        tool_msgs.get("platform_export", "")[:80],
    )

    out = agent.invoke({"messages": [HumanMessage("问用户")]}, config)
    asks = [
        p
        for p in out.get("__interrupt__") or []
        if isinstance(p.value, dict) and p.value.get("type") == "ask_human"
    ]
    record("ask_human 暂停并给出问题", bool(asks), asks[0].value["question"] if asks else "")
    if asks:
        out = agent.invoke(Command(resume="撤出"), config)
        last_tool = [m for m in out["messages"] if getattr(m, "type", "") == "tool"][-1]
        record("ask_human 恢复后拿到回答", "撤出" in last_tool.content, last_tool.content[:60])

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 项通过")
    return 1 if failed else 0


# ============================================================================ probe
def _probe(ws: Workspace, args) -> int:
    """Does the model call the right tool with valid arguments? (read-only tools)"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from afsim_research_agent.config import Settings
    from afsim_research_agent.orchestrator.graph import default_model, orchestrator_model_name
    from afsim_research_agent.orchestrator.tools import build_tools

    settings = Settings.from_environment()
    model_name = args.model or orchestrator_model_name(settings)
    model = default_model(settings, model_name)
    ws.existing(args.package)
    tools = build_tools(
        ws,
        include={
            "overview",
            "falcon_check",
            "simulate",
            "confirmation_status",
            "capability_gaps",
            "list_actions",
        },
    )
    by_name = {t.name: t for t in tools}
    bound = model.bind_tools(tools)
    cases = [
        ("overview", "先了解一下流程和扩展接入情况。", {}),
        (
            "falcon_check",
            f"检查模型包 {args.package} 有没有问题，不要改动它。",
            {"package": args.package},
        ),
        ("simulate", f"在二维仿真里跑一下 {args.package} 的全部想定。", {"package": args.package}),
        (
            "confirmation_status",
            f"{args.package} 还有多少项待人工确认？",
            {"package": args.package},
        ),
        (
            "falcon_check",
            f"对 {args.package} 做检查，并且把二维想定也一起跑。",
            {"package": args.package, "with_l3": True},
        ),
    ]
    system = SystemMessage(
        "你是 AFSIM 战术建模 agent。根据用户请求调用一个最合适的工具，参数用工作区路径。"
    )
    rows = []
    for name, prompt, expect in cases:
        for i in range(args.trials):
            t0 = time.time()
            row: dict[str, Any] = {"case": name, "trial": i, "prompt": prompt}
            try:
                msg = bound.invoke([system, HumanMessage(prompt)])
                calls = msg.tool_calls or []
                row["calls"] = [{"name": c["name"], "args": c["args"]} for c in calls]
                row["invalid"] = len(getattr(msg, "invalid_tool_calls", []) or [])
                ok_name = bool(calls) and calls[0]["name"] == name
                ok_args = ok_name and all(calls[0]["args"].get(k) == v for k, v in expect.items())
                if ok_name:
                    by_name[name].args_schema.model_validate(calls[0]["args"])
                row.update(ok_name=ok_name, ok_args=ok_args)
            except Exception as exc:  # noqa: BLE001
                row.update(
                    ok_name=False, ok_args=False, error=f"{type(exc).__name__}: {str(exc)[:200]}"
                )
            row["seconds"] = round(time.time() - t0, 2)
            rows.append(row)
            print(
                f"{name:<20}#{i}  name={'✓' if row['ok_name'] else '✗'} args={'✓' if row['ok_args'] else '✗'}  {row['seconds']}s"
            )
    n = len(rows)
    summary = {
        "model": model_name,
        "trials": n,
        "right_tool": sum(r["ok_name"] for r in rows) / n,
        "right_args": sum(r["ok_args"] for r in rows) / n,
        "errors": sum("error" in r for r in rows),
        "invalid_tool_calls": sum(r.get("invalid", 0) for r in rows),
    }
    out = args.out or ws.resolve(f"runs/agent-probe/{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    print(f"报告：{out}")
    return 0 if summary["right_args"] >= 0.9 else 1


# ============================================================================ run
def _checkpointer(path: Path | None):
    if path is None:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        print(
            "未安装 langgraph-checkpoint-sqlite，改用内存检查点（退出后不能续跑）", file=sys.stderr
        )
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def _run(ws: Workspace, args) -> int:
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from afsim_research_agent.orchestrator.graph import build_agent

    agent = build_agent(workspace=ws, checkpointer=_checkpointer(args.checkpoint_db))
    thread = args.thread or f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    config = {"configurable": {"thread_id": thread}}
    print(f"会话 {thread}（Ctrl-C 退出；带 --thread {thread} --checkpoint-db 可续跑）")
    requirement = args.requirement
    payload: Any = None
    if requirement or not args.thread:
        requirement = requirement or input("请输入战术需求：\n> ").strip()
        payload = {"messages": [HumanMessage(requirement)]}
    else:
        payload = Command(resume=None)
    seen = 0
    while True:
        out = agent.invoke(payload, config)
        msgs = out.get("messages", [])
        for m in msgs[seen:]:
            _print_message(m)
        seen = len(msgs)
        pauses = out.get("__interrupt__") or []
        if not pauses:
            text = input("\n你（回车结束）：").strip()
            if not text:
                return 0
            payload = {"messages": [HumanMessage(text)]}
            continue
        payload = Command(resume=_answer(pauses[0].value))


def _print_message(m) -> None:
    kind = getattr(m, "type", "")
    if kind == "ai":
        for c in getattr(m, "tool_calls", []) or []:
            print(f"\n→ 调用 {c['name']} {json.dumps(c['args'], ensure_ascii=False)[:200]}")
        if m.content:
            print(f"\nagent：{m.content}")
    elif kind == "tool":
        text = str(m.content)
        try:
            d = json.loads(text)
            text = f"[{d.get('status')}] {d.get('summary')}"
        except Exception:  # noqa: BLE001
            text = text[:300]
        print(f"← {m.name}: {text}")


def _answer(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") == "ask_human":
        print(f"\n❓ {value['question']}")
        for i, o in enumerate(value.get("options") or [], 1):
            print(f"   {i}. {o}")
        text = input("你的回答（可填序号）：").strip()
        options = value.get("options") or []
        if text.isdigit() and 1 <= int(text) <= len(options):
            text = options[int(text) - 1]
        return text
    if isinstance(value, dict) and "action_requests" in value:
        decisions = []
        for req in value["action_requests"]:
            print(f"\n⏸ 需要你批准：{req['name']} {json.dumps(req['args'], ensure_ascii=False)}")
            choice = input("批准 a / 修改参数 e / 拒绝 r：").strip().lower()
            if choice.startswith("e"):
                raw = input("新的参数（JSON）：")
                decisions.append(
                    {
                        "type": "edit",
                        "edited_action": {"name": req["name"], "args": json.loads(raw)},
                    }
                )
            elif choice.startswith("r"):
                decisions.append(
                    {"type": "reject", "message": input("拒绝理由：").strip() or "用户拒绝"}
                )
            else:
                decisions.append({"type": "approve"})
        return {"decisions": decisions}
    return input(f"\n{value}\n> ")


# ============================================================================ ui
def _ui(ws: Workspace, args) -> int:
    from afsim_research_agent.orchestrator.web.server import App, index_path, serve
    from afsim_research_agent.orchestrator.web.session import AgentRunner, DemoRunner, ThreadStore

    (ws.root / "runs").mkdir(parents=True, exist_ok=True)
    if args.demo:
        runner, model_name, index = DemoRunner(), "演示（不调用模型）", None
    else:
        from afsim_research_agent.orchestrator.graph import build_agent, orchestrator_model_name

        db = args.checkpoint_db or ws.resolve("runs/agent-ui/checkpoints.sqlite")
        agent = build_agent(workspace=ws, checkpointer=_checkpointer(db))
        model_name = orchestrator_model_name()
        runner, index = AgentRunner(agent, model_name=model_name), index_path(ws)
    app = App(ThreadStore(runner, index), ws, model_name=model_name)
    try:
        server = serve(app, host=args.host, port=args.port)
    except OSError as exc:
        print(
            f"端口 {args.port} 无法使用（{exc.strerror}）。可能已有程序占用："
            f"用 `lsof -i :{args.port}` 查看，或换一个端口：--port {args.port + 1}",
            file=sys.stderr,
        )
        return 2
    print(f"界面：http://{args.host}:{args.port}  （Ctrl-C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
