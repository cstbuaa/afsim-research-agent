"""Replays: the AFSIM event-pipe reader (self-describing schema) and the replay JSON."""

from __future__ import annotations

import struct
from types import SimpleNamespace

from afsim_research_agent.replay.aer import Recording
from afsim_research_agent.replay.build import (
    FORMAT,
    from_aer,
    from_console,
    from_sim,
    labels_from_run,
)
from afsim_research_agent.replay.geo import Local, geodetic_to_ecef

SCHEMA = """Settings {
 double_time:  1
}Struct {
 id:  MsgBase
 message:  virtual
 Field {
  id:  simTime
  type:  time
 }
 Field {
  id:  simIndex
  type:  uint8
 }
}Struct {
 id:  Vec3d
 Field {
  id:  x
  type:  double
 }
 Field {
  id:  y
  type:  double
 }
 Field {
  id:  z
  type:  double
 }
}Struct {
 id:  Vec3f
 Field {
  id:  x
  type:  float
 }
 Field {
  id:  y
  type:  float
 }
 Field {
  id:  z
  type:  float
 }
}Struct {
 id:  EntityState
 Field {
  id:  platformIndex
  type:  index
 }
 Field {
  id:  locationWCS
  type:  Vec3d
 }
 Field {
  id:  velocityWCS
  option:  0
  type:  Vec3f
 }
}Struct {
 base:  MsgBase
 id:  MsgEntityState
 message:  1
 Field {
  id:  state
  type:  EntityState
 }
}Enum {
 id:  SpatialDomain
 Value {
  id:  land
  value:  1
 }
}Struct {
 base:  MsgBase
 id:  MsgPlatformInfo
 message:  2
 Field {
  id:  platformIndex
  type:  index
 }
 Field {
  id:  name
  option:  0
  type:  string
 }
 Field {
  id:  side
  option:  2
  type:  string
 }
 Field {
  id:  spatialDomain
  option:  5
  type:  SpatialDomain
 }
}Struct {
 base:  MsgBase
 id:  MsgSensorTrackCreated
 message:  15
 Field {
  id:  ownerIndex
  type:  index
 }
 Field {
  id:  targetIndex
  option:  1
  type:  index
 }
}Struct {
 base:  MsgBase
 id:  MsgPlatformStatus
 message:  7
 Field {
  id:  platformIndex
  type:  index
 }
 Field {
  bit:  0
  id:  broken
  type:  bool
 }
 Field {
  bit:  1
  id:  removed
  type:  bool
 }
}"""


def _s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<I", len(raw)) + raw


def _msg(mid: int, body: bytes) -> bytes:
    return struct.pack("<II", len(body) + 4, mid) + body


def _base(t: float) -> bytes:
    return struct.pack("<dB", t, 0)


def _state(t: float, index: int, lat: float, lon: float, vel=None) -> bytes:
    body = bytes([1 if vel else 0]) + struct.pack("<I", index)
    body += struct.pack("<3d", *geodetic_to_ecef(lat, lon))
    if vel:
        body += struct.pack("<3f", *vel)
    return _msg(1, _base(t) + body)


def _recording() -> bytes:
    out = b"\x00\x00WSF_PIPE\n" + SCHEMA.encode() + b"\x00"
    info = lambda i, n, side: _msg(  # noqa: E731
        2, bytes([0b100101]) + _base(0) + struct.pack("<I", i) + _s(n) + _s(side) + b"\x01"
    )
    out += info(1, "blue_car", "blue") + info(2, "red_1", "red")
    # red drives west at 10 m/s: the velocity in earth-centred axes at its start
    local = Local(45.0, -120.9)
    west = [-10 * c for c in local.e]
    out += _state(0.0, 1, 45.0, -121.0)
    out += _state(0.0, 2, 45.0, -120.9, west)
    out += _msg(15, bytes([0b10]) + _base(5.0) + struct.pack("<II", 1, 2))
    out += _msg(7, bytes([0b10]) + _base(10.0) + struct.pack("<I", 2))
    return out


def test_recording_decodes_with_its_own_schema() -> None:
    rec = Recording(_recording())
    msgs = list(rec)
    assert [m[0] for m in msgs] == [
        "MsgPlatformInfo", "MsgPlatformInfo", "MsgEntityState", "MsgEntityState",
        "MsgSensorTrackCreated", "MsgPlatformStatus",
    ]  # fmt: skip
    assert msgs[0][1] == {
        "simTime": 0.0, "simIndex": 0, "platformIndex": 1, "name": "blue_car",
        "side": "blue", "spatialDomain": "land",
    }  # fmt: skip
    assert "velocityWCS" not in msgs[2][1]["state"] and "velocityWCS" in msgs[3][1]["state"]
    assert msgs[5][1]["removed"] and not msgs[5][1]["broken"]


def test_replay_from_recording_dead_reckons_in_local_metres() -> None:
    r = from_aer(Recording(_recording()), labels={"blue_car": "car #1"}, sensor_range_m=6000)
    assert r["format"] == FORMAT and r["source"] == "afsim" and r["duration_s"] == 10.0
    assert {p["id"]: p["side"] for p in r["platforms"]} == {"car #1": "blue", "red_1": "red"}
    blue, red = r["samples"]["car #1"], r["samples"]["red_1"]
    assert blue[0][:3] == [0.0, 0.0, 0.0]
    x0, x5 = red[0][1], red[5][1]
    assert 7800 < x0 < 7900 and abs((x0 - x5) - 50) < 0.5  # 10 m/s westward, 5 s
    assert abs(red[5][4] - 270.0) < 0.5 and len(red) == 11  # heading west; removed at 10 s
    assert r["events"] == [[5.0, "detect", "car #1", "red_1"]]
    assert r["sensors"] == {"car #1": {"range_m": 6000}}


def test_states_and_actions_come_from_the_console_of_older_runs() -> None:
    console = "IR_STATE|0|blue_car|MOVE\nIR_ACT|2|blue_car|movement.follow_route\n"
    console += "IR_FAIL|3|blue_car|perception.select_nearest_target|FAILURE:NO_VALID_TRACK\n"
    r = from_aer(Recording(_recording()), console=console, labels={"blue_car": "car #1"})
    assert r["states"] == {"car #1": [[0.0, "MOVE"]]}
    assert r["actions"] == [
        [2.0, "car #1", "start", "movement.follow_route"],
        [3.0, "car #1", "fail", "perception.select_nearest_target", "FAILURE:NO_VALID_TRACK"],
    ]
    labels = labels_from_run([{"name": "blue_car", "role": "scout", "instance": 0}])
    assert labels == {"blue_car": "scout #1"}


def test_replay_from_a_2d_result() -> None:
    S = SimpleNamespace
    tracks = S(
        by_platform={
            "car_0": [S(t=0.0, x=0.0, y=0.0, speed=0.0), S(t=1.0, x=0.0, y=5.0, speed=5.0)]
        },
        side={"car_0": "blue"},
    )  # fmt: skip
    result = S(
        scenario="sc", tracks=tracks, states={"car_0": [(0.0, "MOVE")]}, duration_s=1.0,
        step_s=1.0, activations=[(0.0, "car_0", "nav.goto")], events=[(0.5, "go")],
        checks=[S(id="e1", passed=True, detail="ok")],
    )  # fmt: skip
    binding = S(
        routes={"approach": S(plane=[(0.0, 0.0), (0.0, 350.0)])},
        points_plane={"post": (0.0, 350.0)},
    )
    r = from_sim(result, binding)
    assert r["source"] == "2d" and r["samples"]["car_0"][0] == [0.0, 0.0, 0.0, 0.0, 0.0]
    assert r["routes"] == [{"id": "approach", "points": [[0.0, 0.0], [0.0, 350.0]]}]
    assert r["points"] == [{"id": "post", "x": 0.0, "y": 350.0}]
    assert r["verdicts"] == [{"id": "e1", "passed": True, "detail": "ok"}]


CONSOLE = """IR_STATE|0|blue_leader_1|SEARCH
IR_ACT|0|blue_leader_1|movement.follow_route
IR_POS_ORIGIN|45|-121
IR_POS|0.37|blue_leader_1|blue|0|0|1.1|90
IR_POS|0.37|red_1|red|0|12727.9|0|0
IR_POS|1.37|blue_leader_1|blue|0.5|11.31|8|90
IR_POS|1.37|red_1|red|0|12727.9|0|0
CAP_RADAR_TRACK 1 blue_leader_1 search_radar red_1
IR_STATE|1|blue_leader_1|TRACK
IR_FAIL|1|blue_leader_1|perception.maintain_selected_target|FAILURE:TARGET_LOST
"""


def test_replay_from_the_console_alone() -> None:
    """Recordings too big to decode: the console's 1 Hz samples, states, actions and radar
    track starts make the replay (east is recorded over cos(lat0))."""
    r = from_console(CONSOLE, labels={"blue_leader_1": "leader #1"}, sensor_range_m=6000.0)
    assert r["format"] == FORMAT and r["duration_s"] == 1.37
    lead = r["samples"]["leader #1"]
    assert lead[1][1] == 8.0 and lead[1][2] == 0.5 and lead[1][3] == 8.0
    assert abs(r["samples"]["red_1"][0][1] - 9000.0) < 1.0
    assert r["states"]["leader #1"] == [[0.0, "SEARCH"], [1.0, "TRACK"]]
    assert r["actions"][1][2:] == [
        "fail",
        "perception.maintain_selected_target",
        "FAILURE:TARGET_LOST",
    ]
    assert r["events"] == [[1.0, "detect", "leader #1", "red_1"]]
    assert {p["id"]: p["side"] for p in r["platforms"]} == {"leader #1": "blue", "red_1": "red"}
    assert r["sensors"] == {"leader #1": {"range_m": 6000.0}}
