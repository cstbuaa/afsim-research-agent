"""Check .env for Crow + Falcon without printing secrets.

    python scripts/check_env.py            # configuration only
    python scripts/check_env.py --online   # also tests DashScope (2 tiny calls) and reachability

Secrets are shown only as "set (length N, prefix xx…)". Paste the output freely.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
PLACEHOLDERS = {"", "your-api-key", "changeme", "xxx", "sk-xxx"}

OK, WARN, BAD = "✅", "⚠️ ", "❌"
problems = 0


def mask(value: str) -> str:
    return f"已设置（长度 {len(value)}，前缀 {value[:3]}…）"


def line(status: str, name: str, detail: str) -> None:
    global problems
    if status == BAD:
        problems += 1
    print(f"{status} {name:<30} {detail}")


def is_secret(name: str) -> bool:
    return any(m in name for m in SECRET_MARKERS)


def check_config(env: dict[str, str]) -> None:
    print("== 1. 必需项（Crow 与 Falcon）")
    key = env.get("DASHSCOPE_API_KEY", "")
    if key.strip() in PLACEHOLDERS:
        line(BAD, "DASHSCOPE_API_KEY", "未设置或仍是模板占位值")
    elif not key.startswith("sk-"):
        line(WARN, "DASHSCOPE_API_KEY", mask(key) + "；百炼 key 通常以 sk- 开头，请核对")
    else:
        line(OK, "DASHSCOPE_API_KEY", mask(key))
    if key != key.strip() or '"' in key or "'" in key:
        line(BAD, "DASHSCOPE_API_KEY", "值两端有空格或引号，请去掉")
    base = env.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    if base.rstrip("/").endswith("/compatible-mode/v1"):
        line(OK, "DASHSCOPE_BASE_URL", base)
    else:
        line(WARN, "DASHSCOPE_BASE_URL", f"{base}（OpenAI 兼容地址应以 /compatible-mode/v1 结尾）")
    line(
        OK,
        "DASHSCOPE_CHAT_MODEL",
        env.get("DASHSCOPE_CHAT_MODEL") or "（未设置，默认 qwen3.8-flash）",
    )
    line(
        OK,
        "DASHSCOPE_EMBEDDING_MODEL",
        env.get("DASHSCOPE_EMBEDDING_MODEL") or "（未设置，默认 text-embedding-v4）",
    )
    dim = env.get("PAPERQA_EMBEDDING_DIMENSION", "1024")
    line(OK if dim.isdigit() else BAD, "PAPERQA_EMBEDDING_DIMENSION", dim)
    tavily = env.get("TAVILY_API_KEY", "")
    if tavily.strip() in PLACEHOLDERS:
        line(BAD, "TAVILY_API_KEY", "未设置：Crow 的网页检索不可用")
    else:
        line(OK if tavily.startswith("tvly-") else WARN, "TAVILY_API_KEY", mask(tavily))

    print("\n== 2. 建议项（影响检索质量与稳定性）")
    email = env.get("CROW_CONTACT_EMAIL", "")
    line(
        OK if "@" in email else WARN,
        "CROW_CONTACT_EMAIL",
        "已设置" if "@" in email else "未设置：OpenAlex 与部分出版方可能限流",
    )
    for name, why in (
        ("OPENALEX_API_KEY", "未设置时 OpenAlex 共享额度很小，常返回 429"),
        ("SEMANTIC_SCHOLAR_API_KEY", "未设置时共享池常返回 429"),
        ("CORE_API_KEY", "未设置时跳过 CORE"),
    ):
        value = env.get(name, "")
        line(OK if value.strip() else WARN, name, mask(value) if value.strip() else why)
    proxy = env.get("CROW_HTTP_PROXY", "")
    line(
        OK if proxy else WARN,
        "CROW_HTTP_PROXY",
        proxy
        or "未设置：境外条令/学术站点在国内网络下常下载失败（终端里的 HTTP(S)_PROXY 会被忽略）",
    )
    cidrs = env.get("CROW_ALLOWED_PROXY_CIDRS", "")
    if cidrs:
        line(OK, "CROW_ALLOWED_PROXY_CIDRS", cidrs)
    depth = env.get("TAVILY_SEARCH_DEPTH", "advanced")
    line(OK if depth in ("basic", "advanced") else BAD, "TAVILY_SEARCH_DEPTH", depth)

    print("\n== 3. 路径与数值")
    data_dir = ROOT / env.get("AFSIM_RESEARCH_DATA_DIR", "./data")
    line(
        OK if data_dir.exists() else WARN,
        "AFSIM_RESEARCH_DATA_DIR",
        f"{env.get('AFSIM_RESEARCH_DATA_DIR', './data')}（{'存在' if data_dir.exists() else '不存在，会自动创建'}）",
    )
    for d in [p.strip() for p in env.get("CROW_LIBRARY_DIRS", "").split(",") if p.strip()]:
        path = (ROOT / d) if not Path(d).is_absolute() else Path(d)
        pdfs = len(list(path.glob("**/*.pdf"))) if path.exists() else 0
        line(
            OK if path.exists() else WARN,
            "CROW_LIBRARY_DIRS",
            f"{d}（{'存在，PDF ' + str(pdfs) + ' 个' if path.exists() else '目录不存在'}）",
        )
    for name in (
        "CROW_MAX_SOURCES",
        "CROW_GAP_ROUNDS",
        "CROW_GAP_ROUND_SOURCES",
        "CROW_FETCH_WORKERS",
        "CROW_HOST_FAILURE_LIMIT",
        "PAPERQA_EVIDENCE_K",
        "PAPERQA_MAX_SOURCES",
        "PAPERQA_CONCURRENCY",
    ):
        value = env.get(name)
        if value is not None and value.strip() and not value.strip().isdigit():
            line(BAD, name, f"'{value}' 不是整数")
    falcon = env.get("FALCON_CHAT_MODEL")
    line(OK, "FALCON_CHAT_MODEL", falcon or "（未设置，Falcon 使用 DASHSCOPE_CHAT_MODEL）")

    print("\n== 4. 安全")
    ignore = (
        (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
    )
    line(
        OK if ".env" in ignore.split() else BAD,
        ".gitignore",
        "已忽略 .env" if ".env" in ignore.split() else ".env 没有被忽略，可能被提交",
    )
    example = dotenv_values(ROOT / ".env.example") if (ROOT / ".env.example").exists() else {}
    leaked = [
        n for n, v in example.items() if is_secret(n) and (v or "").strip() not in PLACEHOLDERS
    ]
    if leaked:
        line(
            BAD, ".env.example", f"模板里有非占位的密钥值：{leaked}；请清空（模板可能被提交或分享）"
        )
    else:
        line(OK, ".env.example", "未发现真实密钥")
    unknown = sorted(
        n
        for n in env
        if n.startswith(("CROW_", "DASHSCOPE_", "PAPERQA_", "FALCON_"))
        and n not in example
        and n
        not in ("FALCON_CHAT_MODEL", "FALCON_TIMEOUT_S", "FALCON_EXTRACT_BATCH", "FALCON_MIN_SCORE")
    )
    if unknown:
        line(WARN, "未识别的变量", f"{unknown}（可能拼写错误，程序不会读取）")


def check_online(env: dict[str, str]) -> None:
    import httpx

    print("\n== 5. 在线检查")
    key = env.get("DASHSCOPE_API_KEY", "")
    base = env.get(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).rstrip("/")
    model = env.get("FALCON_CHAT_MODEL") or env.get("DASHSCOPE_CHAT_MODEL") or "qwen3.8-flash"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        started = time.perf_counter()
        r = httpx.post(
            f"{base}/chat/completions",
            headers=headers,
            timeout=60,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "只回答：好"}],
                "max_tokens": 5,
                "enable_thinking": False,
            },
        )
        ms = (time.perf_counter() - started) * 1000
        line(
            OK if r.status_code == 200 else BAD,
            f"百炼对话 {model}",
            f"HTTP {r.status_code}，{ms:.0f} ms"
            + ("" if r.status_code == 200 else f"：{r.text[:160]}"),
        )
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "items": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["answer", "items"],
            "additionalProperties": False,
        }
        r = httpx.post(
            f"{base}/chat/completions",
            headers=headers,
            timeout=60,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "输出 answer=好，items=[1,2]"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "probe", "schema": schema},
                },
                "enable_thinking": False,
            },
        )
        ok = False
        if r.status_code == 200:
            try:
                content = r.json()["choices"][0]["message"]["content"]
                ok = isinstance(json.loads(content).get("items"), list)
            except (KeyError, ValueError, TypeError):
                ok = False
        line(
            OK if ok else BAD,
            "结构化输出 json_schema",
            "支持（Falcon 需要）"
            if ok
            else f"不支持或解析失败（HTTP {r.status_code}）：Falcon 需换支持 json_schema 的模型",
        )
        r = httpx.post(
            f"{base}/embeddings",
            headers=headers,
            timeout=60,
            json={
                "model": env.get("DASHSCOPE_EMBEDDING_MODEL") or "text-embedding-v4",
                "input": "测试",
                "dimensions": int(env.get("PAPERQA_EMBEDDING_DIMENSION", "1024")),
            },
        )
        line(
            OK if r.status_code == 200 else BAD,
            "百炼向量模型",
            f"HTTP {r.status_code}" + ("" if r.status_code == 200 else f"：{r.text[:160]}"),
        )
    except httpx.HTTPError as exc:
        line(BAD, "百炼接口", f"无法连接：{type(exc).__name__}")
    proxy = env.get("CROW_HTTP_PROXY") or None
    for name, url in (
        ("Tavily", "https://api.tavily.com"),
        ("OpenAlex", "https://api.openalex.org/works?per-page=1"),
        ("arXiv", "https://export.arxiv.org/api/query?search_query=all:formation&max_results=1"),
        ("armypubs（经代理）", "https://armypubs.army.mil/"),
    ):
        try:
            client = httpx.Client(
                proxy=proxy if "代理" in name else None, timeout=20, follow_redirects=True
            )
            code = client.get(url).status_code
            line(OK if code < 500 else WARN, name, f"可达（HTTP {code}）")
        except httpx.HTTPError as exc:
            line(WARN, name, f"不可达：{type(exc).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    path = ROOT / ".env"
    if not path.exists():
        print(f"{BAD} 找不到 {path}")
        return 1
    file_env = {k: (v or "") for k, v in dotenv_values(path).items()}
    # What the programs actually see: load_dotenv() never overrides variables
    # already exported in the shell, so those win over .env.
    prefixes = ("DASHSCOPE_", "TAVILY_", "CROW_", "PAPERQA_", "OPENALEX_", "FALCON_")
    env = dict(file_env)
    shadowed = []
    for name, value in os.environ.items():
        if name.startswith(prefixes) or name in ("SEMANTIC_SCHOLAR_API_KEY", "CORE_API_KEY"):
            if name in file_env and file_env[name] != value:
                shadowed.append(name)
            env[name] = value
    if shadowed:
        line(
            BAD,
            "终端环境变量覆盖 .env",
            f"{sorted(shadowed)} 已在终端/shell 配置中导出且与 .env 不同；程序实际使用终端里的值。"
            "执行 unset 这些变量，并检查 ~/.zshrc、~/.bash_profile 或 conda 环境变量中的 export",
        )
    print("（以下按程序实际生效的值检查：终端变量优先于 .env）\n")
    check_config(env)
    if args.online:
        check_online(env)
    print(
        f"\n结论：{'有 ' + str(problems) + ' 项必须处理（❌）' if problems else '没有必须处理的问题'}；⚠️ 为建议项。"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
