"""Benchmark scenario registry."""

from __future__ import annotations

from collections.abc import Iterable

from bear_agent.benchmarks.models import BenchmarkScenario, ScenarioInfo


class BenchmarkRegistry:
    def __init__(self) -> None:
        self._scenarios: dict[str, BenchmarkScenario] = {}

    def register(self, scenario: BenchmarkScenario) -> None:
        if not getattr(scenario, "id", ""):
            raise ValueError("benchmark scenario requires an id")
        if scenario.id in self._scenarios:
            raise ValueError(f"duplicate benchmark scenario id: {scenario.id}")
        self._scenarios[scenario.id] = scenario

    def register_all(self, scenarios: Iterable[BenchmarkScenario]) -> None:
        for scenario in scenarios:
            self.register(scenario)

    def get(self, scenario_id: str) -> BenchmarkScenario:
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._scenarios)) or "(none)"
            raise KeyError(f"unknown benchmark scenario {scenario_id!r}; known: {known}") from exc

    def list(self, *, category: str | None = None) -> list[ScenarioInfo]:
        items = [self.scenario_info(scenario_id) for scenario_id in sorted(self._scenarios)]
        if category:
            items = [item for item in items if item.category == category]
        return items

    def scenario_info(self, scenario_id: str) -> ScenarioInfo:
        scenario = self.get(scenario_id)
        return ScenarioInfo(
            id=scenario.id,
            name=scenario.name,
            category=scenario.category,
            kind=scenario.kind,
            description=scenario.description,
            requires_network=scenario.requires_network,
            default_params=dict(scenario.default_params),
        )

    def __len__(self) -> int:
        return len(self._scenarios)


def build_registry() -> BenchmarkRegistry:
    # Imports are local so importing a scenario module never registers twice.
    from bear_agent.benchmarks.scenarios import all_scenarios

    registry = BenchmarkRegistry()
    registry.register_all(all_scenarios())
    return registry
