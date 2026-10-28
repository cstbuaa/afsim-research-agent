"""Extension points for capabilities under development.

Five slots, each an interface (``Protocol``) plus a registry entry:

=====================  ===============================================  ===================
slot                   what it is                                       status
=====================  ===============================================  ===================
``action_library``     动作集：平台无关的原子动作（取代过细的能力目录）    in development
``tactic_library``     战术集：由动作编排成的战术模板，可按需求检索        in development
``plan_compiler``      高层描述 → 行为树（确定性程序；模型只写高层描述）    in development
``platform_backends``  仿真平台后端：导出、运行、读回报错（AFSIM 等）       in development
``plugin_builder``     能力插件开发：为能力缺口起草并验证平台插件          planned
=====================  ===============================================  ===================

A slot that has nothing registered still shows up as a tool; the tool answers
``not_configured`` so the agent (and the person reading the trace) sees what is missing.

Registering an implementation::

    # mypkg/afsim_ext.py
    def setup(registry):
        registry.action_library = MyActionLibrary(...)
        registry.platform_backends["afsim"] = AfsimTemplateBackend(...)

and either call ``setup(get_registry())`` yourself, or list it in the environment:
``AFSIM_AGENT_EXTENSIONS=mypkg.afsim_ext:setup`` (comma separated), or declare a
Python entry point in group ``afsim_research_agent.extensions``.

The data types below are deliberately small. Keep backend-native names and bindings in
``ActionSpec.native`` / ``_local`` — they are never shown to the model.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

ENV_VAR = "AFSIM_AGENT_EXTENSIONS"
ENTRY_POINT_GROUP = "afsim_research_agent.extensions"


# ============================================================================ data types
@dataclass
class ActionSpec:
    """One action of the action library (动作集条目)."""

    name: str
    description: str
    platform_classes: list[str] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)  # name -> type / unit
    preconditions: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    category: str = ""
    source: str = ""  # where it came from (lab model, doctrine, catalog …)
    native: dict[str, Any] = field(default_factory=dict, repr=False)  # never shown to the model

    def public(self) -> dict[str, Any]:
        out = {"name": self.name, "description": self.description}
        for key in ("category", "platform_classes", "parameters", "preconditions", "effects"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


@dataclass
class TacticSpec:
    """One tactic of the tactic library (战术集条目): actions arranged into phases."""

    name: str
    description: str
    phases: list[dict[str, Any]] = field(default_factory=list)  # [{phase, actions, until}]
    roles: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # action names it relies on
    parameters: dict[str, str] = field(default_factory=dict)
    applicability: str = ""
    source: str = ""

    def public(self) -> dict[str, Any]:
        out = {"name": self.name, "description": self.description}
        for key in ("applicability", "roles", "phases", "actions", "parameters"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


@dataclass
class CompileOutput:
    """Result of compiling a high-level plan into behavior trees / a model package."""

    files: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class PlatformRun:
    """One run of an exported model on a simulation platform (e.g. AFSIM)."""

    ok: bool
    problems: list[str] = field(default_factory=list)  # parsed errors / failed checks
    log: Path | None = None  # full console output, kept on disk
    metrics: dict[str, Any] = field(default_factory=dict)


# ============================================================================ interfaces
@runtime_checkable
class ActionLibrary(Protocol):
    name: str
    version: str

    def actions(self, platform_class: str | None = None) -> list[ActionSpec]: ...

    def get(self, name: str) -> ActionSpec | None: ...


@runtime_checkable
class TacticLibrary(Protocol):
    name: str
    version: str

    def tactics(self) -> list[TacticSpec]: ...

    def get(self, name: str) -> TacticSpec | None: ...

    def match(self, query: str, *, limit: int = 5) -> list[TacticSpec]:
        """Tactics relevant to a requirement or tactic card (retrieval / rules)."""
        ...


@runtime_checkable
class PlanCompiler(Protocol):
    """Deterministic generator: high-level description written by the model → behavior trees.

    ``plan_format`` names the description language (e.g. ``"python"`` for Code-BT style
    functions, or ``"yaml"``). ``format_guide`` is what the model is told when it writes one.
    """

    name: str
    plan_format: str

    def format_guide(self) -> str: ...

    def validate(self, plan: str, *, actions: ActionLibrary | None = None) -> list[str]: ...

    def compile(
        self, plan: str, *, out_dir: Path, actions: ActionLibrary | None = None
    ) -> CompileOutput: ...


@runtime_checkable
class PlatformBackend(Protocol):
    """A simulation platform the model package is mapped onto (AFSIM: template + run)."""

    name: str

    def export(self, package_dir: Path, out_dir: Path) -> CompileOutput: ...

    def run(self, export_dir: Path, *, timeout_s: float = 600) -> PlatformRun: ...


@runtime_checkable
class CapabilityPluginBuilder(Protocol):
    """Drafts a platform plugin for a capability gap and verifies the draft."""

    name: str
    platform: str

    def draft(self, gap: dict[str, Any], out_dir: Path) -> CompileOutput: ...

    def verify(self, draft_dir: Path) -> PlatformRun: ...


# ============================================================================ registry
@dataclass
class ExtensionRegistry:
    action_library: ActionLibrary | None = None
    tactic_library: TacticLibrary | None = None
    plan_compiler: PlanCompiler | None = None
    platform_backends: dict[str, PlatformBackend] = field(default_factory=dict)
    plugin_builder: CapabilityPluginBuilder | None = None
    loaded_from: list[str] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)

    SLOTS: ClassVar[tuple[str, ...]] = (
        "action_library",
        "tactic_library",
        "plan_compiler",
        "platform_backends",
        "plugin_builder",
    )
    INTERFACES: ClassVar[dict[str, type]] = {}  # filled below

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for slot in self.SLOTS:
            value = getattr(self, slot)
            if slot == "platform_backends":
                out[slot] = ", ".join(sorted(value)) or "未接入"
            else:
                out[slot] = _describe(value)
        return out

    def check(self) -> list[str]:
        """Interface conformance problems of whatever is registered."""
        problems: list[str] = []
        for slot, iface in self.INTERFACES.items():
            value = getattr(self, slot)
            items = value.items() if slot == "platform_backends" else [(slot, value)]
            for key, impl in items:
                if impl is None:
                    continue
                if not isinstance(impl, iface):
                    missing = [m for m in _members(iface) if not hasattr(impl, m)]
                    problems.append(f"{key}: {type(impl).__name__} 缺少 {', '.join(missing)}")
        return problems + list(self.load_errors)

    def load(self, specs: Sequence[str] | None = None, *, entry_points: bool = True) -> None:
        """Run ``setup(registry)`` hooks from the environment and entry points."""
        if specs is None:
            specs = [s.strip() for s in os.getenv(ENV_VAR, "").split(",") if s.strip()]
        hooks: list[tuple[str, Callable[[ExtensionRegistry], None]]] = []
        for spec in specs:
            try:
                hooks.append((spec, _import(spec)))
            except Exception as exc:  # noqa: BLE001 - report and keep going
                self.load_errors.append(f"{spec}: {type(exc).__name__}: {exc}")
        if entry_points:
            for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
                try:
                    hooks.append((f"{ep.module}:{ep.attr}", ep.load()))
                except Exception as exc:  # noqa: BLE001
                    self.load_errors.append(f"{ep.name}: {type(exc).__name__}: {exc}")
        for spec, hook in hooks:
            if spec in self.loaded_from:
                continue
            try:
                hook(self)
                self.loaded_from.append(spec)
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(f"{spec}: {type(exc).__name__}: {exc}")


ExtensionRegistry.INTERFACES = {
    "action_library": ActionLibrary,
    "tactic_library": TacticLibrary,
    "plan_compiler": PlanCompiler,
    "platform_backends": PlatformBackend,
    "plugin_builder": CapabilityPluginBuilder,
}

_REGISTRY: ExtensionRegistry | None = None


def get_registry(*, load: bool = True) -> ExtensionRegistry:
    """The process-wide registry (extensions from the environment loaded once)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ExtensionRegistry()
        if load:
            _REGISTRY.load()
    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


def _import(spec: str) -> Callable[[ExtensionRegistry], None]:
    module, _, attr = spec.partition(":")
    obj = importlib.import_module(module)
    return getattr(obj, attr or "setup")


def _members(iface: type) -> list[str]:
    methods = {n for n in vars(iface) if not n.startswith("_") and callable(vars(iface)[n])}
    return sorted(methods | set(getattr(iface, "__annotations__", {})))


def _describe(impl: Any) -> str:
    if impl is None:
        return "未接入"
    name = getattr(impl, "name", type(impl).__name__)
    version = getattr(impl, "version", "")
    return f"{name} {version}".strip()


# ============================================================================ built-ins
class CatalogActionLibrary:
    """Adapter: the current IR action catalog seen through the ``ActionLibrary`` interface.

    This is the fine-grained catalog the action library is meant to replace; it is here
    so the tools and tests have a working default and so the two can be compared.
    It is *not* registered automatically.
    """

    name = "ir-catalog"

    def __init__(self) -> None:
        from afsim_research_agent.ir.catalog import load_catalog

        self._catalog = load_catalog()
        self.version = self._catalog.versions.get("actions", "")

    def actions(self, platform_class: str | None = None) -> list[ActionSpec]:
        out = []
        for d in self._catalog.actions.values():
            if (
                platform_class
                and d.capability_classes
                and platform_class not in d.capability_classes
            ):
                continue
            out.append(self._spec(d))
        return out

    def get(self, name: str) -> ActionSpec | None:
        d = self._catalog.action(name)
        return self._spec(d) if d else None

    @staticmethod
    def _spec(d) -> ActionSpec:
        return ActionSpec(
            name=d.name,
            description=f"{d.category} action",
            category=d.category,
            platform_classes=list(d.capability_classes),
            parameters={
                a.name: a.type + ("" if not a.required else " (必填)") for a in d.arguments.values()
            },
            preconditions=[f"requires {r}" for r in d.requires],
            source="ir/catalog/actions.toml",
        )
