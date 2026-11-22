"""Causal pathways robustness check in a background task (Task 6).

"Test robustness" runs `sespy.network.path_sensitivity` in an ExtendedTask
with the generation-counter / stale-run guard of `analysis_boolean.py`: the
run uses the TRACED pair (never the current selects), a re-trace or a model
change bumps the generation and clears the result, queued stale clicks skip
the search, and a task error becomes an alert inside the output.
"""
from __future__ import annotations

import html as html_lib
import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("pyvis.shiny")

from _paths import TRANSLATIONS  # noqa: E402
from sespy import i18n, network  # noqa: E402
from sespy.data_structure import Project  # noqa: E402
from shiny import reactive  # noqa: E402
from shiny.testserver import test_server  # noqa: E402

from sespy_app import i18n as app_i18n  # noqa: E402
from sespy_app.event_bus import create_event_bus  # noqa: E402
from sespy_app.modules import analysis_metrics as mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample_ses.json"

COMPUTING = "Running 500 perturbed versions of the network"   # paths_sens_computing, en
CAPTION = "500 perturbed versions (seed 0)"                    # paths_sens_caption, en
FIXED = {"n_draws": 500, "seed": 0, "base": 0.5, "flip_mode": "confidence"}

# Pairs in data/sample_ses.json: D001 -> R002 has 2 paths, D002 -> P001 has 4,
# D001 -> D002 has none.
A, B = "D001", "R002"
C, D = "D002", "P001"


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(i18n, "_default_translator", app_i18n.Translator(
        translations=i18n.load_translations(TRANSLATIONS)))


def _sample() -> Project:
    return Project.from_dict(json.loads(SAMPLE.read_text(encoding="utf-8")))


def _label(node: str) -> str:
    labels = {el.id: el.label for el in _sample().isa_data.elements}
    return html_lib.escape(labels[node])


class Harness:
    def __init__(self):
        self.project = None
        self.bus = None

    def server(self, input, output, session):
        self.project = reactive.value(_sample())
        self.bus = create_event_bus()
        mod.analysis_metrics_server("metrics", project_data=self.project,
                                    event_bus=self.bus)

        # A model change, emitted inside the session's reactive cycle (a bare
        # emit from the test thread is not flushed); prime with 0 first.
        @reactive.effect
        @reactive.event(input.t_isa, ignore_init=True)
        def _isa():
            if input.t_isa():
                self.bus.emit_isa_change()


def _html(scope, name) -> str:
    v = scope.get_output(name)
    if v.status != "ok":
        return f"<{v.status}: {v.error}>"
    return "" if v.value is None else v.value["html"]


def _sens(scope) -> str:
    return _html(scope, "paths_sensitivity")


def _pump(ts, secs) -> None:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        ts.flush()
        time.sleep(0.05)


def _settle(ts, scope, done, secs=60.0) -> None:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        ts.flush()
        if done(_sens(scope)):
            return
        time.sleep(0.05)


def _has_table(html: str) -> bool:
    return "<table" in html


def _start(ts):
    ts.set_inputs(t_isa=0)
    scope = ts.make_scope("metrics")
    scope.set_inputs(metric="degree", top_n=10, paths_source=A,
                     paths_target=B, trace_paths=0, paths_sens_run=0)
    return scope


def _trace(scope, src, tgt, n):
    scope.set_inputs(paths_source=src, paths_target=tgt, trace_paths=n)


def _fake(isa, source, target, **kw):
    base = network.causal_paths(isa, source, target)
    return {"n_draws": kw.get("n_draws", 500), "seed": kw.get("seed", 0),
            "base": 0.5, "flip_mode": "confidence",
            "paths": [{"path": r["path"], "polarity": r["polarity"],
                       "score": r["score"], "rank_probability": 0.5,
                       "polarity_probability": 0.75,
                       "survival_probability": 1.0} for r in base["paths"]],
            "fragile_edges": [], "truncated": False}


def _spy(monkeypatch, impl=_fake, gate=None, returned=None):
    calls: list[tuple[tuple, dict]] = []

    def spy(*args, **kw):
        calls.append((args, kw))
        if gate is not None:
            gate.wait(timeout=10)
        try:
            return impl(*args, **kw)
        finally:
            if returned is not None:
                returned.append(len(calls))

    monkeypatch.setattr(network, "path_sensitivity", spy)
    return calls


# ------------------------------------------------------------- arguments ---

def test_sensitivity_called_with_fixed_defaults(monkeypatch):
    calls = _spy(monkeypatch)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        _settle(ts, scope, _has_table)
        assert _has_table(_sens(scope)), _sens(scope)
    assert len(calls) == 1
    args, kw = calls[0]
    assert args[1:] == (A, B)
    assert kw == FIXED


def test_sensitivity_uses_traced_pair_not_current_selects(monkeypatch):
    calls = _spy(monkeypatch)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_source=C, paths_target=D)   # no re-trace
        scope.set_inputs(paths_sens_run=1)
        _settle(ts, scope, _has_table)
    assert [c[0][1:] for c in calls] == [(A, B)]


# ---------------------------------------------------------------- output ---

def test_run_button_only_with_paths(monkeypatch):
    _spy(monkeypatch)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        assert 'id="metrics-paths_sens_run"' not in _html(scope, "paths_summary")
        _trace(scope, "D001", "D002", 1)                   # no path
        assert 'id="metrics-paths_sens_run"' not in _html(scope, "paths_summary")
        _trace(scope, A, B, 2)
        summary = _html(scope, "paths_summary")
        assert 'id="metrics-paths_sens_run"' in summary
        assert "Test robustness" in summary
        assert 'id="metrics-paths_sensitivity"' in summary


def test_sensitivity_shows_computing_then_table(monkeypatch):
    gate = threading.Event()
    _spy(monkeypatch, impl=network.path_sensitivity, gate=gate)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        assert _sens(scope) == "" or COMPUTING not in _sens(scope)
        scope.set_inputs(paths_sens_run=1)
        assert COMPUTING in _sens(scope), _sens(scope)
        gate.set()
        _settle(ts, scope, _has_table)
        html = _sens(scope)
    assert CAPTION in html, html
    for col in ("Route", "Top route of its sign (%)", "Keeps its sign (%)",
                "Survives (%)"):
        assert col in html, col
    assert _label(B) in html
    assert html.count("<tr") == 3                    # header + the 2 paths
    assert ("Links whose uncertainty most often changes the top route:" in html
            or "No single link's uncertainty clearly changes the top route." in html)
    assert COMPUTING not in html


def test_sensitivity_fragile_none_line(monkeypatch):
    _spy(monkeypatch)                                 # fake: fragile_edges == []
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        _settle(ts, scope, _has_table)
        html = _sens(scope)
    assert "No single link&#x27;s uncertainty" in html or \
        "No single link's uncertainty" in html, html
    assert ">50<" in html and ">75<" in html and ">100<" in html


def test_sensitivity_error_shows_alert_not_crash(monkeypatch):
    def boom(*args, **kw):
        raise RuntimeError("kaboom in the robustness run")

    monkeypatch.setattr(network, "path_sensitivity", boom)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        _settle(ts, scope, lambda s: "alert-danger" in s)
        html = _sens(scope)
        assert ts.is_ok, ts.error
    assert "alert-danger" in html, html
    assert "The robustness check failed: kaboom in the robustness run" in html


# ------------------------------------------------------- stale-run guard ---

def test_stale_result_dropped_when_pair_changes(monkeypatch):
    gate = threading.Event()
    returned: list[int] = []
    calls = _spy(monkeypatch, gate=gate, returned=returned)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        assert COMPUTING in _sens(scope)
        _trace(scope, C, D, 2)                          # re-trace before it finishes
        assert COMPUTING not in _sens(scope), _sens(scope)
        gate.set()                                      # the stale A->B run finishes
        deadline = time.monotonic() + 10
        while not returned and time.monotonic() < deadline:
            ts.flush()
            time.sleep(0.05)
        assert returned, "the stale run never returned"
        _pump(ts, 3.0)
        mid = _sens(scope)
        scope.set_inputs(paths_sens_run=2)              # robustness for C->D
        _settle(ts, scope, _has_table)
        final = _sens(scope)
    assert not _has_table(mid), mid
    assert CAPTION not in mid
    assert [c[0][1:] for c in calls] == [(A, B), (C, D)]
    assert _has_table(final) and final.count("<tr") == 5   # header + 4 paths
    assert _label(D) in final


def test_queued_clicks_run_at_most_two_searches(monkeypatch):
    gate = threading.Event()
    calls = _spy(monkeypatch, gate=gate)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        scope.set_inputs(paths_sens_run=2)
        scope.set_inputs(paths_sens_run=3)
        assert COMPUTING in _sens(scope)
        gate.set()
        _settle(ts, scope, _has_table)
        _pump(ts, 2.0)                    # let a (wrongly) queued extra search run
        html = _sens(scope)
    assert len(calls) == 2, calls         # the first click and the last
    assert _has_table(html)


def test_isa_change_clears_sensitivity(monkeypatch):
    _spy(monkeypatch)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        _settle(ts, scope, _has_table)
        assert _has_table(_sens(scope))
        ts.set_inputs(t_isa=1)
        assert not _has_table(_sens(scope)), _sens(scope)
        assert 'id="metrics-paths_sens_run"' not in _html(scope, "paths_summary")


def test_isa_change_drops_in_flight_run(monkeypatch):
    gate = threading.Event()
    returned: list[int] = []
    _spy(monkeypatch, gate=gate, returned=returned)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        assert COMPUTING in _sens(scope)
        ts.set_inputs(t_isa=1)
        assert COMPUTING not in _sens(scope), _sens(scope)
        gate.set()
        deadline = time.monotonic() + 10
        while not returned and time.monotonic() < deadline:
            ts.flush()
            time.sleep(0.05)
        assert returned
        _pump(ts, 3.0)
        html = _sens(scope)
    assert not _has_table(html), html
    assert CAPTION not in html


def test_truncated_result_shows_partial_notice_above_table(monkeypatch):
    def truncated(*args, **kw):
        return {**_fake(*args, **kw), "truncated": True}

    _spy(monkeypatch, impl=truncated)
    h = Harness()
    with test_server(h.server, timeout_secs=90) as ts:
        scope = _start(ts)
        _trace(scope, A, B, 1)
        scope.set_inputs(paths_sens_run=1)
        _settle(ts, scope, _has_table)
        html = _sens(scope)
    notice = "Ranked among the first 2 paths found"
    assert notice in html, html
    assert html.index(notice) < html.index("<table")
    assert "text-muted" in html[:html.index(notice)]
