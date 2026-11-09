"""Requirement text for a generated model (design ``training-data-pilot-formation-perception``
§6): the model's behaviour as plain facts, a model call that writes four requirement styles
from them, and checks that every stated number survives and no internal name leaks."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from afsim_research_agent.training.pilot_fp import Spec

STYLES = {
    "list": "列表式：逐条列出要求（1. 2. 3.），每条一句",
    "narrative": "叙述式：一段连贯的话讲清楚整个过程",
    "jargon": "术语式：用分队战术用语（梯队、展开、监视、脱离接触、撤收等）简洁表达",
    "ending_first": "先说收尾：先讲什么情况下结束或撤离，再讲行动过程",
}


def _n(x: float | None) -> str:
    return f"{x:g}" if x is not None else ""


def _secs(x: float) -> str:
    return f"{_n(x)} 秒" if x < 120 or x % 60 else f"{_n(x / 60)} 分钟"


def facts(s: Spec) -> list[str]:
    """What the model does, one fact per line, numbers only where the requirement states
    them (the rest are the capability defaults and stay unsaid)."""
    st = set(s.stated)
    out: list[str] = []
    if s.formation:
        out.append("三辆装甲车编队行动，长车指挥两辆僚车")
    else:
        out.append("单辆装甲车执行观察任务")
    if s.wait_start:
        out.append("开始时原地待命，收到上级开始命令后才行动")
    if s.formation:
        if s.assemble:
            col = f"（车距 {_n(s.column_spacing_m)} 米）" if "column_spacing_m" in st else ""
            out.append(f"先在出发点集结成纵队{col}，纵队就位后出发")
        col = (
            f"，车距 {_n(s.column_spacing_m)} 米"
            if "column_spacing_m" in st and not s.assemble
            else ""
        )
        out.append(f"沿想定给定的路线成纵队搜索前进{col}，行进中用雷达搜索敌车")
        if s.search_limit_s:
            out.append(f"搜索 {_secs(s.search_limit_s)}仍未发现敌车，就停车、关闭雷达，结束任务")
        wing = f"，与长车侧向间隔 {_n(s.wing_offset_m)} 米" if "wing_offset_m" in st else ""
        out.append(f"发现敌车后长车停车，僚车向两侧展开成面向敌车的横队{wing}")
        rng = f"约 {_n(s.desired_range_m)} 米的距离" if "desired_range_m" in st else "安全距离"
        out.append(f"横队到位后，长车与敌车保持{rng}，持续监视跟踪；僚车用本车雷达各自跟踪")
        if s.withdraw == "keep":
            out.append(
                "跟踪中敌车向我逼近时（敌车推进比我车后撤慢），长车带领编队后撤，"
                "始终不让敌车进入这个距离以内"
            )
        elif s.withdraw == "stop":
            out.append(
                f"跟踪中敌车逼近到 {_n(s.withdraw_threshold_m)} 米以内，"
                "立即停车、关闭雷达，结束任务"
            )
        elif s.withdraw == "back":
            out.append(
                f"跟踪中敌车逼近到 {_n(s.withdraw_threshold_m)} 米以内，编队后撤，"
                f"把距离拉开到约 {_n(s.withdraw_back_m)} 米后继续监视"
            )
        lost = "目标丢失"
    else:
        if s.search_limit_s:
            out.append(f"搜索 {_secs(s.search_limit_s)}仍未发现敌车就结束任务")
        out.append("在出发位置停车建立观察哨，开雷达搜索敌车")
        out.append("发现敌车后持续跟踪")
        lost = "目标丢失"
    if s.lost_hold or not s.formation:
        t = f"{_secs(s.reacquire_timeout_s)}内" if "reacquire_timeout_s" in st else "一定时间内"
        after = (
            ("恢复纵队继续搜索前进" if s.formation else "回到搜索")
            if s.on_timeout == "resume" or not s.formation
            else "停车、关闭雷达，结束任务"
        )
        hold = "原地保持，" if s.formation else ""
        out.append(f"{lost}后{hold}尝试重新捕获；{t}重新发现就继续跟踪，否则{after}")
    else:
        out.append(f"{lost}后停车、关闭雷达，结束任务")
    out.append("任何时候收到上级撤收命令，都先停车、再关闭雷达，然后结束任务")
    return out


def numbers(s: Spec) -> dict[str, float]:
    """The stated numbers the text must carry (value in the unit the facts use)."""
    out: dict[str, float] = {}
    for key in s.stated:
        v = getattr(s, key)
        if v is None:
            continue
        if key.endswith("_s") and (v >= 120 and not v % 60):
            out[key] = v / 60  # written in minutes
        else:
            out[key] = v
    return out


class Requirements(BaseModel):
    list: str = Field(description=STYLES["list"])
    narrative: str = Field(description=STYLES["narrative"])
    jargon: str = Field(description=STYLES["jargon"])
    ending_first: str = Field(description=STYLES["ending_first"])


def prompt(s: Spec) -> str:
    fs = "\n".join(f"- {f}" for f in facts(s))
    styles = "\n".join(f"- {k}：{v}" for k, v in STYLES.items())
    return f"""你在为装甲分队战术行为建模系统写用户需求。下面是一个战术行为的全部事实，\
请把它写成用户会提出的自然语言需求，按四种风格各写一版。

事实：
{fs}

风格：
{styles}

要求：
- 每一版都完整覆盖全部事实，不增加事实里没有的行为、数值或条件；
- 事实里出现的数字必须原样保留，用阿拉伯数字，带单位（米、秒、分钟）；
- 没有给数字的地方不要自己编数字；
- 不要照抄事实的句子，换成自己的说法和语序；
- 不要加入事实里没有的动作，特别是射击、开火、攻击、火力准备、呼叫支援、掩护等；
- 用作战语言，不要出现任何英文、变量名、状态名、动作名或代码；
- 可以口语化、可以换说法，例如“上级撤收命令”可写成“收到撤收命令”“上级叫停”；
- 每版 60–200 字。

按 JSON 输出：{{"list": ..., "narrative": ..., "jargon": ..., "ending_first": ...}}"""


_IDENT = re.compile(r"[A-Za-z_]{3,}")

# words the pilot behaviour never does; a text with one of them added a fact
FORBIDDEN = ("射击", "开火", "攻击", "打击", "火力", "炮", "支援", "掩护", "梯次")

# accepted wording for the required elements
SYNONYMS = {
    "撤": ("撤", "后退", "脱离"),
    "待命": ("待命", "待机", "待令", "听令", "命令"),
    "集结": ("集结", "集合", "编成纵队", "成纵队就位"),
}


_CN = "零一二三四五六七八九十"


def _cn(n: int) -> str | None:
    """Chinese numeral for 1–99 (十, 十五, 二十, 四十五)."""
    if not 0 < n < 100:
        return None
    tens, ones = divmod(n, 10)
    head = ("" if tens == 1 else _CN[tens]) + "十" if tens else ""
    return head + (_CN[ones] if ones else "")


def _has_number(text: str, value: float) -> bool:
    v = f"{value:g}"
    variants = {v}
    if value == int(value) and _cn(int(value)):
        variants.add(f"{_cn(int(value))}秒")
        variants.add(f"{_cn(int(value))}分钟")
    if value >= 1000 and value % 100 == 0:
        variants.add(f"{value / 1000:g}千米")
        variants.add(f"{value / 1000:g} 千米")
        variants.add(f"{value / 1000:g}公里")
        variants.add(f"{value / 1000:g} 公里")
        variants.add(f"{value / 1000:g}km")
    return any(x in text for x in variants)


def check(s: Spec, text: str) -> list[str]:
    """Problems of one requirement text: missing stated numbers, leaked identifiers."""
    probs = []
    for key, v in numbers(s).items():
        if not _has_number(text, v):
            probs.append(f"缺少数值 {key}={v:g}")
    leak = [w for w in _IDENT.findall(text) if w.lower() not in ("km",)]
    if leak:
        probs.append(f"含英文或标识符 {leak[:5]}")
    for word, needed in (
        ("撤", s.withdraw in ("keep", "back")),
        ("待命", s.wait_start),
        ("集结", s.assemble),
    ):
        if needed and not any(w in text for w in SYNONYMS[word]):
            probs.append(f"缺少要素“{word}”")
    added = [w for w in FORBIDDEN if w in text]
    if added:
        probs.append(f"加入了事实中没有的行为 {added}")
    return probs


class Fidelity(BaseModel):
    missing: list[int] = Field(description="需求文字完全没有表达的事实编号；表达了就不列")
    extra: list[str] = Field(description="事实中没有的新动作、新条件或新数值，每条 30 字以内")
    wrong: list[str] = Field(
        description="与事实相反的说法（数值不同、顺序颠倒、条件相反），每条 30 字以内"
    )


def judge_prompt(s: Spec, text: str) -> str:
    fs = "\n".join(f"{i}. {f}" for i, f in enumerate(facts(s), 1))
    return f"""核对需求文字是否表达了下列事实。只输出结论，不要写推理过程。

事实：
{fs}

需求文字：
{text}

判定规则：
- 换了说法、合并成一句、调换了叙述顺序都算表达了；
  例如“目标丢失或收到撤收命令都先停车再关雷达”同时表达了这两条事实；
- 同义：待机=待命，闭雷达=关闭雷达，锁定/监视=跟踪，撤收令=撤收命令，脱离接触、撤离、收尾=结束任务；
- missing 只列整条事实的主要行为或条件完全找不到的编号；只缺修饰词（如“面向敌车”“持续”）不算；
- extra 只列事实中没有的新动作、新条件或新数值；说法更具体不算；
- wrong 只列与事实相反的地方；
- 没有就给空列表。

按 JSON 输出：{{"missing": [编号...], "extra": [...], "wrong": [...]}}"""


async def judge(s: Spec, text: str, model: Any) -> list[str]:
    """A second model call: facts the text leaves out, adds or contradicts."""
    res = await model.generate("training:fidelity", judge_prompt(s, text), Fidelity)
    data = res.model_dump() if hasattr(res, "model_dump") else dict(res)
    fs = facts(s)
    out = [
        f"遗漏：{fs[i - 1]}"
        for i in sorted({int(i) for i in data.get("missing") or []})
        if 0 < i <= len(fs)
    ]
    for key, label in (("extra", "多出"), ("wrong", "矛盾")):
        out += [f"{label}：{str(x)[:80]}" for x in data.get(key) or [] if str(x).strip()]
    return out


async def write(s: Spec, model: Any, check_fidelity: bool = True) -> dict[str, Any]:
    """Ask the model for the four styles and check each; returns texts and problems."""
    res = await model.generate("training:requirements", prompt(s), Requirements)
    data = res.model_dump() if hasattr(res, "model_dump") else dict(res)
    out = {}
    for style in STYLES:
        text = str(data.get(style) or "").strip()
        out[style] = {"text": text, "problems": check(s, text) if text else ["空"]}
        if check_fidelity and text and not out[style]["problems"]:
            out[style]["fidelity"] = await judge(s, text, model)
    return out
