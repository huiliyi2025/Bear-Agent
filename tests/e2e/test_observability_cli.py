"""CLI for traces, metrics, diagnostics and benchmarks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support import ScriptedProvider, text_script, tool_script
from typer.testing import CliRunner

from bear_agent.bootstrap import build_container as real_build_container
from bear_agent.ui.cli import main


def _config(tmp_path: Path) -> Path:
    data_dir = (tmp_path / "data").as_posix()
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
config_version = 1

[agent]
default_provider = "fake"
default_model = "fake-model"
max_turns = 5

[providers.fake]
type = "openai_compat"
base_url = "http://fake.local/v1"
api_key = "test-key-1234567890"
models = ["fake-model"]

[storage]
data_dir = "{data_dir}"

[hooks]
enabled = false

[plugins]
enabled = false
""",
        encoding="utf-8",
    )
    return path


def _patch_provider(monkeypatch: pytest.MonkeyPatch, scripts: list[list]) -> None:  # type: ignore[type-arg]
    def fake_build(settings, *, cwd, approver=None, provider=None, bus=None):
        scripted = ScriptedProvider([list(script) for script in scripts])
        return real_build_container(
            settings, cwd=cwd, approver=approver, provider=scripted, bus=bus
        )

    monkeypatch.setattr(main, "build_container", fake_build)


def _run_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    config = _config(tmp_path)
    (tmp_path / "hello.txt").write_text("hello cli\n", encoding="utf-8")
    _patch_provider(
        monkeypatch,
        [
            tool_script("read-1", "read_file", {"path": "hello.txt"}),
            text_script("done"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(
        main.app, ["run", "read hello", "--cwd", str(tmp_path), "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    sessions = runner.invoke(main.app, ["sessions", "list", "--config", str(config)])
    assert sessions.exit_code == 0
    match = __import__("re").search(r"ses_[0-9a-f]+", sessions.output)
    assert match is not None
    return config, match.group(0)


def test_trace_metrics_doctor_and_bench_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, session_id = _run_agent(tmp_path, monkeypatch)
    runner = CliRunner()

    listed = runner.invoke(main.app, ["trace", "list", "--config", str(config), "--json"])
    assert listed.exit_code == 0, listed.output
    traces = json.loads(listed.output)
    assert traces
    trace = traces[0]
    assert trace["session_id"] == session_id
    assert trace["status"] == "ok"

    shown = runner.invoke(
        main.app,
        ["trace", "show", trace["trace_id"], "--config", str(config), "--json", "--events"],
    )
    assert shown.exit_code == 0, shown.output
    detail = json.loads(shown.output)
    assert detail["detail"]["trace"]["trace_id"] == trace["trace_id"]
    assert detail["detail"]["roots"]
    assert detail["events"]

    by_run = runner.invoke(
        main.app, ["trace", "run", trace["run_id"], "--config", str(config), "--json"]
    )
    assert by_run.exit_code == 0, by_run.output

    export_path = tmp_path / "trace.json"
    exported = runner.invoke(
        main.app,
        [
            "trace",
            "export",
            "--trace",
            trace["trace_id"],
            "--config",
            str(config),
            "--output",
            str(export_path),
        ],
    )
    assert exported.exit_code == 0, exported.output
    document = json.loads(export_path.read_text(encoding="utf-8"))
    assert document["trace"]["trace_id"] == trace["trace_id"]
    assert document["redaction"]["prompts"] == "not-recorded"
    assert "hello.txt" not in json.dumps(document)

    metrics = runner.invoke(main.app, ["metrics", "show", "--config", str(config), "--json"])
    assert metrics.exit_code == 0, metrics.output
    report = json.loads(metrics.output)
    assert report["runs"]["total"] == 1
    assert report["models"]["calls"] == 2
    assert report["tools"]["calls"] == 1

    providers = runner.invoke(main.app, ["metrics", "providers", "--config", str(config), "--json"])
    assert providers.exit_code == 0
    assert json.loads(providers.output)["calls"] == 2

    metrics_path = tmp_path / "metrics.json"
    metrics_export = runner.invoke(
        main.app,
        ["metrics", "export", "--config", str(config), "--output", str(metrics_path)],
    )
    assert metrics_export.exit_code == 0
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["runs"]["total"] == 1

    doctor = runner.invoke(main.app, ["doctor", "--config", str(config), "--json"])
    assert doctor.exit_code == 0, doctor.output
    report_json = json.loads(doctor.output)
    assert report_json["kind"] == "bear-agent.doctor"
    assert report_json["ok"] is True
    assert any(check["id"] == "observability.traces" for check in report_json["checks"])
    assert "test-key-1234567890" not in doctor.output

    doctor_path = tmp_path / "doctor.json"
    doctor_export = runner.invoke(
        main.app,
        ["doctor", "--config", str(config), "--export", str(doctor_path)],
    )
    assert doctor_export.exit_code == 0
    assert json.loads(doctor_path.read_text(encoding="utf-8"))["ok"] is True

    filtered = runner.invoke(
        main.app,
        ["doctor", "--config", str(config), "--category", "provider", "--json"],
    )
    assert filtered.exit_code == 0
    filtered_report = json.loads(filtered.output)
    assert {check["category"] for check in filtered_report["checks"]} == {"provider"}

    bench_list = runner.invoke(main.app, ["bench", "list", "--json"])
    assert bench_list.exit_code == 0
    assert any(item["id"] == "e2e.single_tool" for item in json.loads(bench_list.output))

    bench_run = runner.invoke(
        main.app,
        [
            "bench",
            "run",
            "kernel.tool_registry_lookup",
            "--iterations",
            "1",
            "--warmup",
            "0",
            "--param",
            "count=50",
            "--config",
            str(config),
            "--json",
            "--output",
            str(tmp_path / "bench-run.json"),
        ],
    )
    assert bench_run.exit_code == 0, bench_run.output
    first = json.loads(bench_run.output)[0]
    assert (
        json.loads((tmp_path / "bench-run.json").read_text(encoding="utf-8"))["results"][0][
            "run_id"
        ]
        == first["run_id"]
    )
    assert first["status"] == "completed"
    assert first["stats"]["successful"] == 1

    second = runner.invoke(
        main.app,
        [
            "bench",
            "run",
            "kernel.tool_registry_lookup",
            "--iterations",
            "1",
            "--warmup",
            "0",
            "--param",
            "count=50",
            "--config",
            str(config),
            "--json",
        ],
    )
    assert second.exit_code == 0
    second_result = json.loads(second.output)[0]

    history = runner.invoke(
        main.app,
        [
            "bench",
            "history",
            "--config",
            str(config),
            "--scenario",
            "kernel.tool_registry_lookup",
            "--json",
        ],
    )
    assert history.exit_code == 0
    assert len(json.loads(history.output)) == 2

    comparison = runner.invoke(
        main.app,
        [
            "bench",
            "compare",
            first["run_id"],
            second_result["run_id"],
            "--config",
            str(config),
            "--json",
        ],
    )
    assert comparison.exit_code == 0, comparison.output
    assert json.loads(comparison.output)["scenario_id"] == "kernel.tool_registry_lookup"

    report = runner.invoke(
        main.app,
        ["bench", "report", first["run_id"], "--config", str(config), "--json"],
    )
    assert report.exit_code == 0
    assert json.loads(report.output)["run_id"] == first["run_id"]

    bench_path = tmp_path / "bench.json"
    bench_export = runner.invoke(
        main.app,
        ["bench", "export", first["run_id"], "--config", str(config), "--output", str(bench_path)],
    )
    assert bench_export.exit_code == 0
    assert json.loads(bench_path.read_text(encoding="utf-8"))["run_id"] == first["run_id"]
