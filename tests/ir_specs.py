"""Spec fixtures for Tactical Behavior IR tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "armor_platoon_terrain_formation"

_CONFIRMED = {"status": "user_specified", "confirmation": "confirmed"}


def example_data() -> dict[str, Any]:
    from afsim_research_agent.ir.loader import load_spec

    loaded = load_spec(EXAMPLE_DIR / "behavior_spec.yaml")
    assert loaded.data is not None, loaded.diagnostics
    return copy.deepcopy(loaded.data)


def minimal_data() -> dict[str, Any]:
    """Single-role scout: patrol, observe on contact, withdraw when told."""
    text = """
schema_version: "1.0"
model:
  id: scout_patrol
  display_name: Scout patrol
  version: 1.0.0
  intent: screen
  authoring_mode: user_authored
provenance: {requirements: [], references: []}
assumptions: []
execution:
  architecture: state_machine_with_behavior_trees
  update_interval: {value: 1, unit: sec}
roles:
  - id: scout
    capability_class: ground_vehicle
    dependencies:
      mover: {capability: route_following_ground}
      sensors: [{id: optics, capability: visual_search}]
    blackboards:
      - id: mission
        variables:
          - {name: withdraw_ordered, type: bool, default: false}
          - {name: observations, type: int, default: 0}
    state_machine:
      id: scout_mission
      initial_state: patrol
      states:
        - id: patrol
          tree: patrol_tree
          transitions:
            - {transition_id: patrol_to_withdraw, to: withdraw, priority: 100, when: {condition_ref: withdraw_ordered}, basis: {status: user_specified, confirmation: confirmed}}
            - {transition_id: patrol_self, to: patrol, priority: 0, when: {literal: true}}
        - id: withdraw
          tree: withdraw_tree
          transitions:
            - {transition_id: withdraw_done, to: done, priority: 50, when: {mover_value: at_destination}, basis: {status: user_specified, confirmation: confirmed}}
            - {transition_id: withdraw_self, to: withdraw, priority: 0, when: {literal: true}}
        - {id: done, terminal: true, transitions: []}
parameters:
  contact_range: {type: distance, value: 1, unit: km, basis: {status: user_specified, confirmation: confirmed}}
  observe_time: {type: time, value: 30, unit: sec, basis: {status: user_specified, confirmation: confirmed}}
  patrol_speed: {type: speed, value: 20, unit: km/h, basis: {status: user_specified, confirmation: confirmed}}
resources: []
conditions:
  - id: hostile_near
    expression:
      compare:
        left: {track_value: nearest_hostile_range}
        operator: lte
        right: {parameter_ref: contact_range}
  - id: withdraw_ordered
    expression: {blackboard_value: mission.withdraw_ordered}
behaviors:
  - id: observe
    claims: [movement_control]
    actions:
      - {command: halt, issue_policy: once_per_activation}
      - {command: wait, arguments: {duration: {parameter_ref: observe_time}}}
  - id: patrol
    claims: [movement_control]
    actions:
      - {command: fly_to_position, arguments: {position: patrol_point, speed: {parameter_ref: patrol_speed}}, issue_policy: once_per_activation}
    success: {expression: {mover_value: at_destination}}
    timeout: {value: 10, unit: min, result: failure}
  - id: withdraw
    claims: [movement_control]
    actions:
      - {command: move_to_position, arguments: {position: rally_point}, issue_policy: once_per_activation}
    success: {expression: {mover_value: at_destination}}
    failure: {expression: {not: {mover_value: available}}}
trees:
  - id: patrol_tree
    root:
      node_id: patrol_root
      type: selector
      children:
        - node_id: observe_seq
          type: sequence
          children:
            - {node_id: near_check, type: condition, condition: hostile_near}
            - {node_id: observe_node, type: behavior, behavior: observe}
        - {node_id: patrol_node, type: behavior, behavior: patrol}
  - id: withdraw_tree
    root: {node_id: withdraw_node, type: behavior, behavior: withdraw}
tests:
  - id: patrols
    category: nominal
    given: {state: patrol}
    expect: {next_state: patrol, selected_path: [patrol_root, patrol_node], issued_actions: [move_to_position]}
  - id: observes_contact
    category: precondition_failure
    given: {state: patrol, tracks: {nearest_hostile_range: {value: 800, unit: m}}}
    expect: {selected_path: [patrol_root, observe_seq, observe_node], issued_actions: [halt]}
  - id: withdraws_on_order
    category: priority_transition
    given: {state: patrol, blackboard: {mission.withdraw_ordered: true}}
    expect: {next_state: withdraw, fired_transition: patrol_to_withdraw, issued_actions: [move_to_position]}
  - id: withdraw_fails_without_mover
    category: resource_unavailable
    given: {state: withdraw, components: {mover.available: false}}
    expect: {tree_status: failure}
  - id: withdraw_completes
    category: timeout_or_loss
    given: {state: withdraw, components: {mover.at_destination: true}}
    expect: {next_state: done, fired_transition: withdraw_done, tree_status: null}
scenarios: []
"""
    return yaml.safe_load(text)


def confirmed() -> dict[str, str]:
    return dict(_CONFIRMED)


def codes(diagnostics: Any, severity: str = "error") -> set[str]:
    return {d.code for d in diagnostics if d.severity.value == severity}
