"""
Offline tests for tests/bench_nvidia_models.py.

These import the harness module directly and exercise only pure logic
(selection, scoring, aggregation, recommendation) plus the ``--dry``
pipeline, which never touches the network. No live NVIDIA calls here.
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bench_nvidia_models as bench


def run_rec(
    suite,
    task,
    score=5,
    success=True,
    total_ms=1000,
    error_class=None,
    text="ok",
    tool_calls=None,
    checks=None,
    stream_completed=True,
    stable=True,
    tool_calls_used=0,
    est_cost=None,
    http_status=None,
):
    return {
        "suite": suite,
        "task": task,
        "success": success,
        "http_status": http_status,
        "error_class": error_class,
        "total_ms": total_ms,
        "ttft_ms": 100,
        "output_text": text,
        "tool_calls": tool_calls,
        "tool_calls_used": tool_calls_used,
        "stream_completed": stream_completed,
        "stream_stable_id": stable,
        "auto_rubric": score,
        "checks": checks or {},
        "est_cost_usd": est_cost,
    }


def accessible_probe(models):
    return {m: {"status": "accessible"} for m in models}


def weather_tool_call(name="get_weather"):
    return [{"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}]


# ---------------------------------------------------------------------------
# Selection.
# ---------------------------------------------------------------------------


def test_select_respects_slots_and_cap():
    probe = accessible_probe(bench._flatten_pool())
    chosen = bench.select_benchmark_set(probe)
    assert len(chosen) <= bench.MAX_BENCHMARK_MODELS
    covered = {p: [] for p in bench.SELECTION_ORDER}
    for model in chosen:
        for priority in bench.SELECTION_ORDER:
            if model in bench.CANDIDATE_POOL[priority]:
                covered[priority].append(model)
    for priority in bench.SELECTION_ORDER:
        assert covered[priority], f"no coverage for {priority}"


def test_select_skips_inaccessible():
    probe = accessible_probe(bench._flatten_pool())
    probe["deepseek-ai/deepseek-r1"] = {"status": "inaccessible"}
    chosen = bench.select_benchmark_set(probe)
    assert "deepseek-ai/deepseek-r1" not in chosen
    assert len(chosen) <= bench.MAX_BENCHMARK_MODELS


def test_select_no_accessible_returns_empty():
    probe = {m: {"status": "inaccessible"} for m in bench._flatten_pool()}
    assert bench.select_benchmark_set(probe) == []


def test_expand_up_to_12():
    probe = accessible_probe(bench._flatten_pool())
    base = bench.select_benchmark_set(probe)
    expanded = bench.expand_selection(probe, base)
    assert len(expanded) <= bench.MAX_EXPANDED_MODELS
    assert set(base) <= set(expanded)


# ---------------------------------------------------------------------------
# Deterministic rubric.
# ---------------------------------------------------------------------------


def test_auto_score_empty_is_zero():
    rec = run_rec("C", "qa", text="")
    out = bench.auto_score("C", "qa", {"expected": ["Canberra"]}, rec)
    assert out["score"] == 0


def test_auto_score_refusal_is_zero():
    rec = run_rec("C", "qa", text="I'm sorry, I can't answer that.")
    out = bench.auto_score("C", "qa", {"expected": ["Canberra"]}, rec)
    assert out["score"] == 0


def test_auto_score_exact_instruction():
    rec = run_rec("C", "instruction_following", text="PONG")
    out = bench.auto_score("C", "instruction_following", {"exact": "PONG"}, rec)
    assert out["score"] == 5


def test_auto_score_qa_expected():
    rec = run_rec("C", "qa", text="The capital is Canberra.")
    out = bench.auto_score("C", "qa", {"expected": ["Canberra"]}, rec)
    assert out["score"] == 5
    assert out["checks"]["expected_found"]


def test_auto_score_json_valid():
    rec = run_rec("G", "json_object", text='{"name": "Ada", "age": 36, "city": "Paris"}')
    out = bench.auto_score("G", "json_object", {"schema_keys": ["name", "age", "city"]}, rec)
    assert out["score"] == 5
    assert out["checks"]["json_parses"]
    assert out["checks"]["schema_conformant"]


def test_auto_score_json_invalid():
    rec = run_rec("G", "json_object", text="not json at all")
    out = bench.auto_score("G", "json_object", {"schema_keys": ["name"]}, rec)
    assert out["score"] <= 1
    assert not out["checks"]["json_parses"]


def test_auto_score_tool_single_and_reject():
    single = run_rec("E", "single_tool", text="", tool_calls=weather_tool_call())
    out = bench.auto_score("E", "single_tool", {"tool": "get_weather"}, single)
    assert out["score"] == 5

    reject_ok = run_rec("E", "reject", text="Tokyo")
    out = bench.auto_score("E", "reject", {}, reject_ok)
    assert out["score"] == 5

    reject_bad = run_rec("E", "reject", text="Tokyo", tool_calls=weather_tool_call())
    out = bench.auto_score("E", "reject", {}, reject_bad)
    assert out["score"] == 0


def test_auto_score_malformed_stream_error_shape():
    rec = run_rec(
        "H", "malformed_request", text="", success=False,
        error_class="http_404", http_status=404,
    )
    out = bench.auto_score("H", "malformed_request", {}, rec)
    assert out["score"] == 5
    assert out["checks"]["error_shape_ok"]


def test_accumulate_tool_calls():
    tool_calls = []
    bench._accumulate_tool_calls(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city": "L'},
                            }
                        ]
                    }
                }
            ]
        },
        tool_calls,
    )
    bench._accumulate_tool_calls(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'yon"}'}}]}}]},
        tool_calls,
    )
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"city": "Lyon"}


# ---------------------------------------------------------------------------
# Aggregation.
# ---------------------------------------------------------------------------


def test_percentile():
    assert bench.percentile([1, 2, 3, 4], 50) == 2.5
    assert bench.percentile([1, 2, 3], 50) == 2
    assert bench.percentile([], 50) is None


def test_priority_stats_formula():
    model_runs = {
        "A": {
            "codegen": [
                run_rec("A", "codegen", score=5, total_ms=1000, success=True),
                run_rec("A", "codegen", score=4, total_ms=2000, success=True),
                run_rec("A", "codegen", score=3, total_ms=3000, success=True),
            ]
        }
    }
    stats = bench.priority_stats(model_runs, "A")
    assert stats["quality"] == pytest.approx(4.0 * 20)
    assert stats["reliability"] == pytest.approx(1.0)
    assert stats["latency_p50_ms"] == pytest.approx(2000)


def test_priority_stats_reliability_drops_on_failure():
    model_runs = {
        "C": {
            "qa": [
                run_rec("C", "qa", success=True, score=5),
                run_rec("C", "qa", success=False, error_class="http_404", score=0),
            ]
        }
    }
    stats = bench.priority_stats(model_runs, "C")
    assert stats["reliability"] == pytest.approx(0.5)
    assert stats["quality"] == pytest.approx(2.5 * 20)


def test_composite_weights():
    results = {
        "runs": {
            "m1": {"A": {"t": [run_rec("A", "t", score=5, total_ms=1000, success=True)]}},
            "m2": {"A": {"t": [run_rec("A", "t", score=3, total_ms=4000, success=True)]}},
        }
    }
    aggregate = bench.build_aggregate(results)
    s1 = aggregate["coding"]["m1"]
    s2 = aggregate["coding"]["m2"]
    assert s1["latency_score"] == pytest.approx(100.0)
    assert s2["latency_score"] == pytest.approx(25.0)
    assert s1["composite"] == pytest.approx(100.0)


class RecordingTransport:
    def __init__(self, inner):
        self.inner = inner
        self.sent_payloads = []

    async def stream_chunks(self, payload):
        self.sent_payloads.append(payload)
        async for chunk in self.inner.stream_chunks(payload):
            yield chunk


def test_build_payload_f_keeps_doc_count_out_of_wire(tmp_path, monkeypatch):
    prompts = bench._load_json(bench.PROJECT_ROOT / "bench" / "prompts.json")
    suite = prompts["suites"]["F"]
    task_spec = suite["tasks"]["32k_retrieval"]

    payload = bench.build_payload("m", "F", "32k_retrieval", task_spec, suite, prompts["meta"])
    assert payload["_doc_count"] > 0  # internal metadata lives in the payload dict

    async def scenario():
        rec = await bench.run_one(
            bench.FakeTransport(), "m", "F", "32k_retrieval", task_spec, suite, prompts["meta"], 1
        )
        return rec

    rec = asyncio.run(scenario())
    assert rec["success"]
    assert rec["expected_count"] == payload["_doc_count"]

    recording = RecordingTransport(bench.FakeTransport())

    async def scenario2():
        return await bench.run_one(
            recording, "m", "F", "32k_retrieval", task_spec, suite, prompts["meta"], 1
        )

    asyncio.run(scenario2())
    assert recording.sent_payloads, "transport was called"
    for sent in recording.sent_payloads:
        assert "_doc_count" not in sent, "internal _doc_count leaked onto the wire"


def test_build_payload_json_schema_has_name():
    prompts = bench._load_json(bench.PROJECT_ROOT / "bench" / "prompts.json")
    suite = prompts["suites"]["G"]
    task_spec = suite["tasks"]["json_schema"]
    payload = bench.build_payload("m", "G", "json_schema", task_spec, suite, prompts["meta"])
    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "structured_output"
    assert rf["json_schema"]["schema"] is task_spec["schema"]
    assert rf["json_schema"]["strict"] is True


def test_gate_tool_pass_and_fail():
    parallel = run_rec(
        "E", "parallel", text="",
        tool_calls=weather_tool_call() + [weather_tool_call("get_stock_price")],
    )
    runs = {
        "E": {
            "single_tool": [run_rec("E", "single_tool", text="", tool_calls=weather_tool_call())],
            "parallel": [parallel],
            "reject": [run_rec("E", "reject", text="Tokyo")],
            "multi_turn": [run_rec("E", "multi_turn", text="Lyon is warmer.", tool_calls_used=1)],
        }
    }
    assert bench.gate_tool(runs) == "pass"
    runs["E"]["reject"] = [run_rec("E", "reject", text="Tokyo", tool_calls=weather_tool_call())]
    assert bench.gate_tool(runs) == "fail"


def test_gate_json_parse_rate_requires_90_percent():
    ok = run_rec("G", "json_object", text='{"name": "A", "age": 1, "city": "B"}',
                 checks={"json_parses": True, "schema_conformant": True})
    bad = run_rec("G", "json_object", text="nope",
                  checks={"json_parses": False, "schema_conformant": False})
    runs = {"G": {"json_object": [ok, ok, bad]}}
    assert bench.gate_json(runs) == "fail"  # 2/3 parse < 0.9
    runs["G"]["json_object"] = [ok] * 9 + [bad]
    assert bench.gate_json(runs) == "pass"  # 9/10 >= 0.9


def test_gate_long_context_and_stream():
    good = run_rec("F", "32k_retrieval", checks={"expected_found": True})
    bad = run_rec("F", "32k_retrieval", checks={"expected_found": False})
    runs = {"F": {"32k_retrieval": [good, bad]}}
    assert bench.gate_long_context(runs) == "fail"
    runs["F"]["32k_retrieval"] = [good, good]
    assert bench.gate_long_context(runs) == "pass"

    stream_runs = {
        "H": {
            "completion": [run_rec("H", "completion", stream_completed=True, stable=True)],
            "stable_id": [run_rec("H", "stable_id", stream_completed=True, stable=True)],
            "malformed_request": [run_rec("H", "malformed_request", text="", success=False,
                                          checks={"error_shape_ok": True})],
        }
    }
    assert bench.gate_stream(stream_runs) == "pass"
    stream_runs["H"]["completion"] = [run_rec("H", "completion", stream_completed=False, stable=True)]
    assert bench.gate_stream(stream_runs) == "fail"


# ---------------------------------------------------------------------------
# Recommendation.
# ---------------------------------------------------------------------------


def gate_stats(composite, reliability=0.96, p90=2000, cost=0.01, gates_pass=True):
    gates = {k: ("pass" if gates_pass else "fail") for k in ("E", "F", "G", "H")}
    return {
        "quality": 90.0,
        "latency_score": 100.0,
        "latency_p50_ms": 1000,
        "latency_p90_ms": p90,
        "reliability": reliability,
        "composite": composite,
        "gates": gates,
        "cost": cost,
    }


def test_recommend_picks_best_gate_clean():
    stats = {
        "m1": gate_stats(93.0),
        "m2": gate_stats(83.0, gates_pass=False),
        "m3": gate_stats(94.0, reliability=0.80),  # fails reliability floor
    }
    aggregate = {p: dict(stats) for p in bench.RECOMMEND_ORDER}
    recommendation, notes = bench.recommend(aggregate)
    assert recommendation["coding"] == "m1"
    assert recommendation["general"] == "m1"


def test_recommend_falls_back_when_all_below_floor():
    stats = {"m1": gate_stats(94.0, reliability=0.90, gates_pass=True)}
    aggregate = {p: dict(stats) for p in bench.RECOMMEND_ORDER}
    recommendation, notes = bench.recommend(aggregate)
    assert recommendation["general"] == "m1"
    assert any("fell back" in n for n in notes)


def test_recommend_gate_override():
    stats = {
        "m1": gate_stats(80.0, gates_pass=False),
        "m2": gate_stats(70.0, gates_pass=True),
    }
    aggregate = {p: dict(stats) for p in bench.RECOMMEND_ORDER}
    recommendation, notes = bench.recommend(aggregate)
    assert recommendation["coding"] == "m2"


def test_format_recommendation_four_entries():
    rec = {"general": "g", "coding": "c", "reasoning": "r", "fast": "f"}
    block = bench.format_recommendation(rec)
    assert block.splitlines()[0] == "NVIDIA_MODEL_PRIORITY:"
    assert "  - g   # default_general" in block
    assert "  - c   # coding" in block
    assert "  - r   # reasoning" in block
    assert "  - f   # fast" in block


# ---------------------------------------------------------------------------
# CLI (offline).
# ---------------------------------------------------------------------------


def test_no_key_refusal(monkeypatch, capsys):
    monkeypatch.setattr(bench.settings, "nvidia_api_key", "")
    rc = bench.main(["--probe"])
    assert rc == 1
    assert "NVIDIA_API_KEY" in capsys.readouterr().out


def test_list_dry(capsys):
    rc = bench.main(["--list", "--dry"])
    assert rc == 0
    assert "deepseek-ai/deepseek-v4-flash" in capsys.readouterr().out


def test_probe_dry_writes_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "PROBE_PATH", tmp_path / "probe.json")
    rc = bench.main(["--probe", "--dry"])
    assert rc == 0
    doc = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))
    assert doc["dry"]
    assert all(p["status"] == "accessible" for p in doc["models"].values())


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        bench.main(["--help"])
    assert exc_info.value.code == 0


def _patch_bench_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "BENCH_DIR", tmp_path)
    monkeypatch.setattr(bench, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(bench, "PROBE_PATH", tmp_path / "probe.json")
    monkeypatch.setattr(bench, "RESULTS_PATH", tmp_path / "results.json")
    monkeypatch.setattr(bench, "REPORT_PATH", tmp_path / "report.md")
    monkeypatch.setattr(bench, "PROMPTS_PATH", bench.PROJECT_ROOT / "bench" / "prompts.json")
    monkeypatch.setenv("BENCH_NO_PACE", "1")


def test_dry_run_end_to_end(tmp_path, monkeypatch):
    _patch_bench_paths(tmp_path, monkeypatch)
    rc = bench.main(["--run", "--dry", "--only", "deepseek-ai/deepseek-v4-flash", "--runs", "1"])
    assert rc == 0

    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert "runs" in results
    assert "deepseek-ai/deepseek-v4-flash" in results["runs"]
    assert results["probe"], "fabricated probe results recorded"
    model_runs = results["runs"]["deepseek-ai/deepseek-v4-flash"]
    assert "A" in model_runs and "H" in model_runs

    raw_files = list((tmp_path / "raw").rglob("*.json"))
    assert raw_files, "raw per-run files written"

    rc_report = bench.main(["--report"])
    assert rc_report == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "NVIDIA_MODEL_PRIORITY:" in report
    assert "deepseek-ai/deepseek-v4-flash" in report
    assert "## Per-suite results" in report
    assert "mean rubric /5" in report
    assert "mean tok/s" in report


def test_report_documents_exclusions_and_limitations(tmp_path, monkeypatch):
    _patch_bench_paths(tmp_path, monkeypatch)
    rc = bench.main(["--run", "--dry", "--only", "meta/llama-3.1-8b-instruct", "--runs", "1"])
    assert rc == 0
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    for rec in results["runs"]["meta/llama-3.1-8b-instruct"]["E"]["multi_turn"]:
        rec["tool_calls_used"] = 0
    results["probe"] = {
        "meta/llama-3.1-8b-instruct": {"status": "accessible"},
        "deepseek-ai/deepseek-v4-flash": {"status": "accessible"},
        "nvidia/llama-3.3-nemotron-super-49b-v1.5": {"status": "accessible"},
        "qwen/qwq-32b": {"status": "inaccessible"},
    }
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    rc_report = bench.main(["--report"])
    assert rc_report == 0
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Limitations" in report
    assert "deepseek-ai/deepseek-v4-flash" in report
    assert "529 overload" in report
    assert "nvidia/llama-3.3-nemotron-super-49b-v1.5" in report
    assert "reasoning_content" in report
    assert "single tool-calls at once" in report


def test_dry_run_all_suites_passes_gates(tmp_path, monkeypatch):
    _patch_bench_paths(tmp_path, monkeypatch)
    rc = bench.main(["--run", "--dry", "--only", "qwen/qwen3-coder-480b-a35b-instruct", "--runs", "1"])
    assert rc == 0
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    model_runs = results["runs"]["qwen/qwen3-coder-480b-a35b-instruct"]
    for suite in ("E", "F", "G", "H"):
        assert bench.gate_results(model_runs)[suite] == "pass", suite


def test_suite_skip_trims_f_and_gates(tmp_path, monkeypatch):
    _patch_bench_paths(tmp_path, monkeypatch)
    assert bench.SUITE_SKIP["meta/llama-3.1-8b-instruct"] == {"F"}
    rc = bench.main(["--run", "--dry", "--only", "meta/llama-3.1-8b-instruct", "--runs", "1"])
    assert rc == 0
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    model_runs = results["runs"]["meta/llama-3.1-8b-instruct"]
    assert "F" not in model_runs
    assert "E" in model_runs and "G" in model_runs and "H" in model_runs
    gates = bench.gate_results(model_runs)
    assert "F" not in gates
    assert gates["E"] == "pass" and gates["G"] == "pass" and gates["H"] == "pass"
    assert all(v == "pass" for v in gates.values())
