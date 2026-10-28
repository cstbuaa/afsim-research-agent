"""BehaviorTree.CPP v4 XML, parsed with expat (no DTDs, no entities) keeping line numbers.

One XML block per role:

    <root BTCPP_format="4" role="platoon_leader">
      <BehaviorTree ID="travel_open">          <!-- the tree of state travel_open -->
        <Sequence>
          <SetBlackboard key="current_formation" value="wedge"/>
          <Coordination use="platoon_follow.announce_formation"/>
          <FollowRoute route="main_route" speed="travel_speed_open"/>
        </Sequence>
      </BehaviorTree>
    </root>

Only the tree shape is read here; ``compiler.py`` turns tags into IR nodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.parsers import expat

from afsim_research_agent.falcon.sheet.blocks import SheetProblem

MAX_NODES = 2000
MAX_DEPTH = 40


@dataclass
class XmlNode:
    tag: str
    attrs: dict[str, str]
    line: int
    children: list[XmlNode] = field(default_factory=list)


class _Refused(Exception):
    pass


def parse_xml(text: str, block: str, line: int) -> tuple[XmlNode | None, list[SheetProblem]]:
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip())
    if re.search(r"<!DOCTYPE|<!ENTITY", body, re.I):
        return None, [SheetProblem(block, line, "XML 中不允许 DOCTYPE/ENTITY")]
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[XmlNode] = []
    root: list[XmlNode] = []
    count = [0]
    # the stripped text starts where the first non-blank line of the block was
    lead = len(text) - len(text.lstrip())
    first = line + text[:lead].count("\n")

    def start(tag: str, attrs: dict[str, str]) -> None:
        count[0] += 1
        if count[0] > MAX_NODES or len(stack) > MAX_DEPTH:
            raise _Refused("XML 节点过多或嵌套过深")
        node = XmlNode(tag, dict(attrs), first + parser.CurrentLineNumber - 1)
        if stack:
            stack[-1].children.append(node)
        else:
            root.append(node)
        stack.append(node)

    def end(_tag: str) -> None:
        stack.pop()

    def refuse(*_args: object) -> None:
        raise _Refused("XML 中不允许 DOCTYPE/ENTITY")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse  # type: ignore[assignment]
    try:
        parser.Parse(body, True)
    except _Refused as exc:
        return None, [SheetProblem(block, line, str(exc))]
    except expat.ExpatError as exc:
        where = first + (exc.lineno or 1) - 1
        return None, [
            SheetProblem(block, where, f"XML 格式错误：{expat.errors.messages.get(exc.code, exc)}")
        ]
    if not root:
        return None, [SheetProblem(block, line, "XML 块是空的")]
    return root[0], []
