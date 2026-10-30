"""Backend binding and run configuration for compiling IR 2.0 to AFSIM 2.9.

Both files are kept next to the platform project, never in this repository: the binding
names the project's native scripts, leaves and auxiliary-data keys; a run configuration
describes one mission run (platforms, stimuli, observers, the handwritten reference).

Binding (TOML)::

    [meta]
    name = "<project>"
    model_root = "<project directory>"        # added to file_path of every run
    includes = ["<file>", ...]                 # include_once'd by the behavior file
    base_platform_type = "<type>"              # each role's platform type derives from it
    scripts = "<helper scripts>"               # written after the includes

    [blackboard.files]                         # slice → YAML file with a `blackboard` map
    <slice> = "<path relative to model_root>"
    [blackboard.fields]                        # field → auxiliary-data key (overrides)

    [instance]
    request = "{model}_{role}"                 # request id of the role instance
    owner = "{model}"
    on_initialize = "<script>"
    state_entry = "<script>"                   # {state}

    [action."<action id>"]                     # one per action the model uses
    kind = "script"                            # body returns "RUNNING", "SUCCESS" or
    body = "<script>"                          #   "FAILURE:<code>"; {platform} {request}
    cancel = "<script>"                        #   {owner} {action} {p:<parameter>}
    # or
    kind = "native_leaf"                       # the project's own leaf, fed by inputs
    leaf = "<advanced_behavior name>"
    inputs = "<script>"                        # {request} is a fresh id per activation
    done = "<expression>"                      # true once {request} has finished
    cancel = "<script>"
    exit = "<script>"                          # on_exit of states using it, always
    enter = "<script>"                         # on_entry of states using it
    # {p:<name>} writes a parameter; {p?:<name>} falls back to the platform's
    # auxiliary data ir_param_<name> when the leaf gives none (tactic-set leaves)

    [external]                                 # event id → extra raising condition
    <event id> = "<expression>"

    [resource."<resource>"]
    release = "<script>"                       # {platform} {request} {resource}

Run configuration (TOML)::

    id = "<run>"
    start_time = "18:00:00"
    end_time_s = 28
    random_seed = 1
    includes = ["<observer file>", ...]
    preamble = "<raw input before the platforms>"   # e.g. event_output
    extra = "<raw input after the platforms>"       # other platforms, execute blocks
    [[platforms]]
    name = "<platform>"; role = "<role>"; instance = 0
    body = "<platform body: side, position, edits, added processors>"
    [[events]]
    at_s = 1.0; event = "<event id or tactic text>"; platform = "<platform>"
    [parameters]                               # model parameters the scenario supplies
    <parameter> = "<value>"
    [expect]
    sequence = ["<state>", ...]                # optional
    markers = ['<regex>', ...]                 # console must contain each (optional)
    [reference]                                # the handwritten version's evidence
    console = "<console log, relative to model_root>"
    state_pattern = '<regex with one group: the state name; a time group named t>'
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(_Strict):
    name: str
    model_root: str
    includes: list[str] = Field(default_factory=list)
    base_platform_type: str
    update_interval_s: float = 1.0
    scripts: str = ""  # helper scripts written after the includes


class BlackboardBinding(_Strict):
    files: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, str] = Field(default_factory=dict)


class InstanceBinding(_Strict):
    request: str = "{model}_{role}"
    owner: str = "{model}"
    on_initialize: str = ""
    state_entry: str = ""


class ActionBinding(_Strict):
    kind: Literal["script", "native_leaf"]
    body: str = ""
    leaf: str = ""
    inputs: str = ""
    done: str = ""
    cancel: str = ""
    exit: str = ""  # run in on_exit of every state using the action, running or not
    enter: str = ""  # run in on_entry of every state using the action


class ResourceBinding(_Strict):
    release: str = ""


class Binding(_Strict):
    meta: Meta
    blackboard: BlackboardBinding = Field(default_factory=BlackboardBinding)
    instance: InstanceBinding = Field(default_factory=InstanceBinding)
    action: dict[str, ActionBinding] = Field(default_factory=dict)
    resource: dict[str, ResourceBinding] = Field(default_factory=dict)
    # external event id → extra script condition that also raises it (e.g. a project flag)
    external: dict[str, str] = Field(default_factory=dict)

    # filled by load(): slice → field → auxiliary-data key
    aux: dict[str, dict[str, str]] = Field(default_factory=dict, exclude=True)

    @classmethod
    def load(cls, path: Path) -> Binding:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        binding = cls.model_validate(data)
        root = Path(binding.meta.model_root)
        for slice_id, rel in binding.blackboard.files.items():
            file = Path(rel) if Path(rel).is_absolute() else root / rel
            doc = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            binding.aux[slice_id] = {
                str(k): str(v) for k, v in (doc.get("blackboard") or {}).items()
            }
        return binding

    def aux_key(self, field: str, slice_id: str | None) -> str | None:
        if field in self.blackboard.fields:
            return self.blackboard.fields[field]
        if slice_id is not None and field in self.aux.get(slice_id, {}):
            return self.aux[slice_id][field]
        for table in self.aux.values():
            if field in table:
                return table[field]
        return None


class RunPlatform(_Strict):
    name: str
    role: str
    instance: int = 0
    body: str = ""


class RunEvent(_Strict):
    at_s: float
    event: str
    platform: str | None = None  # None: every platform of the run


class RunExpect(_Strict):
    sequence: list[str] | None = None
    # regular expressions the console must contain (the scenario's own checks, e.g. that
    # cleanup completed); a missing one fails the run
    markers: list[str] = Field(default_factory=list)
    # with a handwritten reference: the time spent in each state may differ from the
    # reference by at most this much (a state left far too early or too late fails the run)
    max_interval_delta_s: float | None = None


class Reference(_Strict):
    console: str
    state_pattern: str


class RunConfig(_Strict):
    id: str
    start_time: str = "00:00:00"
    end_time_s: float
    random_seed: int = 1
    includes: list[str] = Field(default_factory=list)
    preamble: str = ""
    extra: str = ""
    platforms: list[RunPlatform] = Field(min_length=1)
    events: list[RunEvent] = Field(default_factory=list)
    # model parameters the scenario supplies (e.g. a route name), replacing placeholders
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)
    expect: RunExpect = Field(default_factory=RunExpect)
    # write an event-pipe recording (output/<run>.aer) that mystic replays
    record: bool = True
    reference: Reference | None = None

    @classmethod
    def load(cls, path: Path) -> RunConfig:
        return cls.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def as_dict(obj: BaseModel) -> dict[str, Any]:
    return obj.model_dump()
