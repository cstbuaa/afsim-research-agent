"""Reader for AFSIM event-pipe recordings (``.aer``, written by ``event_pipe``; Mystic's input).

The file is self-describing: after a two-byte prefix and ``WSF_PIPE`` it carries its schema as
text (``Settings``, ``BasicType``, ``Struct``, ``Enum``, ``List`` blocks, NUL-terminated),
followed by messages::

    uint32 size (bytes after this field) | uint32 message id | body

A struct body is: a little-endian bit field (one bit per ``option`` / ``bit`` index, rounded
up to whole bytes; absent when the struct has neither), then the fields of its ``base``
struct, then its own fields in order. An optional field is present only when its bit is set;
a ``bit`` field is the bit itself. Strings and lists are a uint32 length followed by the
bytes / items; enums are one byte; ``index`` is a uint32; ``time`` is a double when
``Settings.double_time`` is 1. The message ids and fields come from the file, so recordings of
other AFSIM versions decode as long as these encoding rules hold.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_BASIC = {
    "double": ("<d", 8),
    "float": ("<f", 4),
    "int8": ("<b", 1),
    "uint8": ("<B", 1),
    "int16": ("<h", 2),
    "uint16": ("<H", 2),
    "int": ("<i", 4),
    "uint": ("<I", 4),
    "int64": ("<q", 8),
    "index": ("<I", 4),
}
_TOKEN = re.compile(r"\{|\}|[^\s{}]+:\s+[^\n]*|[A-Za-z_]+")


class AerError(ValueError):
    pass


def parse_schema(text: str) -> list[dict[str, Any]]:
    """Schema text → nodes ``{kind, props, kids}``."""
    toks = _TOKEN.findall(text)
    pos = 0

    def node(kind: str) -> dict[str, Any]:
        nonlocal pos
        props: dict[str, str] = {}
        kids: list[dict[str, Any]] = []
        if toks[pos] != "{":
            raise AerError(f"schema: '{{' expected after {kind}")
        pos += 1
        while toks[pos] != "}":
            tok = toks[pos]
            if ":" in tok and not tok.endswith(":"):
                key, value = tok.split(":", 1)
                props.setdefault(key.strip(), value.strip())
                pos += 1
            else:
                pos += 1
                kids.append(node(tok))
        pos += 1
        return {"kind": kind, "props": props, "kids": kids}

    out = []
    while pos < len(toks):
        kind = toks[pos]
        pos += 1
        out.append(node(kind))
    return out


class Recording:
    """An ``.aer`` file: its schema and an iterator over ``(message name, fields)``."""

    def __init__(self, data: bytes) -> None:
        if data[2:10] != b"WSF_PIPE":
            raise AerError("not an AFSIM event-pipe recording (no WSF_PIPE header)")
        end = data.index(b"\x00", 10)
        self.nodes = parse_schema(data[10:end].decode("utf-8", "replace"))
        self.data = data
        self.start = end + 1
        self.types: dict[str, dict[str, Any]] = {}
        self.messages: dict[int, dict[str, Any]] = {}

        def register(n: dict[str, Any], prefix: str = "") -> None:
            name = prefix + n["props"].get("id", "")
            self.types[name] = n
            for k in n["kids"]:
                if k["kind"] in ("Struct", "Enum", "List"):
                    register(k, name + ".")

        for n in self.nodes:
            if n["kind"] in ("Struct", "Enum", "List", "BasicType"):
                register(n)
            mid = n["props"].get("message")
            if n["kind"] == "Struct" and mid and mid.isdigit():
                self.messages[int(mid)] = n
        settings = next((n for n in self.nodes if n["kind"] == "Settings"), None)
        double = settings is not None and settings["props"].get("double_time") == "1"
        self.time_fmt = ("<d", 8) if double else ("<f", 4)

    @classmethod
    def load(cls, path: Path | str) -> Recording:
        return cls(Path(path).read_bytes())

    # ------------------------------------------------------------------ decoding
    def _fields(self, n: dict[str, Any]) -> list[dict[str, Any]]:
        base = n["props"].get("base")
        out = self._fields(self.types[base]) if base else []
        return out + [k for k in n["kids"] if k["kind"] == "Field"]

    def _type(self, name: str, scope: str) -> dict[str, Any]:
        if name in self.types:
            return self.types[name]
        while scope:
            if f"{scope}.{name}" in self.types:
                return self.types[f"{scope}.{name}"]
            scope = scope.rpartition(".")[0]
        raise AerError(f"unknown type {name}")

    def _read(self, tname: str, p: int, scope: str) -> tuple[Any, int]:
        d = self.data
        if tname == "time":
            return struct.unpack_from(self.time_fmt[0], d, p)[0], p + self.time_fmt[1]
        if tname in _BASIC:
            fmt, size = _BASIC[tname]
            return struct.unpack_from(fmt, d, p)[0], p + size
        if tname == "string":
            (n,) = struct.unpack_from("<I", d, p)
            return d[p + 4 : p + 4 + n].decode("utf-8", "replace"), p + 4 + n
        if tname == "bool":
            return d[p] != 0, p + 1
        t = self._type(tname, scope)
        name = t["props"]["id"]
        if t["kind"] == "Enum":
            values = {int(k["props"]["value"]): k["props"]["id"] for k in t["kids"]}
            return values.get(d[p], d[p]), p + 1
        if t["kind"] == "List":
            (n,) = struct.unpack_from("<I", d, p)
            p += 4
            items = []
            for _ in range(n):
                value, p = self._read(t["props"]["type"], p, name)
                items.append(value)
            return items, p
        if t["kind"] == "Struct":
            return self._struct(t, p, tname)
        raise AerError(f"cannot decode {tname}")

    def _struct(self, t: dict[str, Any], p: int, scope: str) -> tuple[dict[str, Any], int]:
        fields = self._fields(t)
        nbits = max(
            (int(f["props"][k]) + 1 for f in fields for k in ("option", "bit") if k in f["props"]),
            default=0,
        )
        bits = 0
        if nbits:
            nbytes = (nbits + 7) // 8
            bits = int.from_bytes(self.data[p : p + nbytes], "little")
            p += nbytes
        out: dict[str, Any] = {}
        for f in fields:
            pr = f["props"]
            if "bit" in pr:
                out[pr["id"]] = bool(bits >> int(pr["bit"]) & 1)
            elif "option" in pr and not bits >> int(pr["option"]) & 1:
                continue
            else:
                out[pr["id"]], p = self._read(pr["type"], p, scope)
        return out, p

    def __iter__(self) -> Iterator[tuple[str, dict[str, Any]]]:
        d, p = self.data, self.start
        while p + 8 <= len(d):
            size, mid = struct.unpack_from("<II", d, p)
            if size < 4:
                break
            t = self.messages.get(mid)
            if t is not None:
                try:
                    msg, _ = self._struct(t, p + 8, t["props"]["id"])
                except (struct.error, AerError, IndexError, KeyError) as exc:
                    msg = {"_error": f"{type(exc).__name__}: {exc}"}
                yield t["props"]["id"], msg
            p += 4 + size
