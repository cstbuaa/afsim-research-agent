from __future__ import annotations

import pytest

from afsim_research_agent.ir.loader import load_spec_text


def _codes(text: str) -> list[str]:
    return [d.code for d in load_spec_text(text).diagnostics]


def test_plain_mapping_loads() -> None:
    result = load_spec_text("a: 1\nb: [x, y]\n")
    assert result.data == {"a": 1, "b": ["x", "y"]}
    assert result.diagnostics == []


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("a:\n\t- 1\n", "IR002"),
        ("a: &x 1\nb: 2\n", "IR002"),
        ("a: &x 1\nb: *x\n", "IR002"),
        ("a: !!python/object:os.system 1\n", "IR002"),
        ("a: 1\na: 2\n", "ID001"),
        ("a: ${HOME}\n", "IR002"),
        ("a: '{{ template }}'\n", "IR002"),
        ("a: sk-abcdefghijklmnopqrstuvwxyz\n", "IR002"),
        ("api_key: hunter2\n", "IR002"),
        ("- 1\n- 2\n", "IR002"),
        ("a: [1, 2\n", "IR002"),
    ],
)
def test_forbidden_yaml_is_rejected(text: str, code: str) -> None:
    result = load_spec_text(text)
    assert result.data is None
    assert code in _codes(text)


def test_yaml_11_booleans_and_dates_stay_strings() -> None:
    data = load_spec_text("a: yes\nb: off\nc: 2026-10-13\nd: true\n").data
    assert data == {"a": "yes", "b": "off", "c": "2026-10-13", "d": True}
