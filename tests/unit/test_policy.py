from __future__ import annotations

import pytest

from bear_agent.core.modes import PermissionMode, ToolSideEffect
from bear_agent.policy.engine import PermissionEngine
from bear_agent.policy.models import DecisionType


class DummyTool:
    def __init__(self, effect: ToolSideEffect) -> None:
        self.side_effect = effect


@pytest.mark.parametrize(
    ("mode", "effect", "expected"),
    [
        (PermissionMode.PLAN, ToolSideEffect.READ, DecisionType.ALLOW),
        (PermissionMode.PLAN, ToolSideEffect.WRITE, DecisionType.DENY),
        (PermissionMode.PLAN, ToolSideEffect.EXEC, DecisionType.DENY),
        (PermissionMode.SUPERVISED, ToolSideEffect.READ, DecisionType.ALLOW),
        (PermissionMode.SUPERVISED, ToolSideEffect.WRITE, DecisionType.ASK),
        (PermissionMode.SUPERVISED, ToolSideEffect.EXEC, DecisionType.ASK),
        (PermissionMode.AUTO, ToolSideEffect.READ, DecisionType.ALLOW),
        (PermissionMode.AUTO, ToolSideEffect.WRITE, DecisionType.ALLOW),
        (PermissionMode.AUTO, ToolSideEffect.EXEC, DecisionType.ASK),
        (PermissionMode.UNRESTRICTED, ToolSideEffect.EXEC, DecisionType.ALLOW),
    ],
)
def test_permission_matrix(
    mode: PermissionMode, effect: ToolSideEffect, expected: DecisionType
) -> None:
    engine = PermissionEngine(mode)
    assert engine.decide(DummyTool(effect)).decision == expected


def test_mode_can_change() -> None:
    engine = PermissionEngine(PermissionMode.AUTO)
    assert engine.decide(DummyTool(ToolSideEffect.WRITE)).decision == DecisionType.ALLOW
    engine.set_mode(PermissionMode.PLAN)
    assert engine.decide(DummyTool(ToolSideEffect.WRITE)).decision == DecisionType.DENY
