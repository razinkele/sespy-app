"""Boolean panel: feedback-core search in a background task (spec 2026-11-21 §4).

The attractor search runs as an ExtendedTask: Run shows `boolean.computing`
at once, the result replaces it when the task settles, a result made stale
by a later Run is dropped, and a task error becomes the module's error alert.
The library's `boolean_attractors_core` is called with its defaults; only on
its `core_too_large` sentinel does the panel fall back to
`boolean_attractors_sampled` with the fixed app arguments.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import sespy
from _paths import TRANSLATIONS
from sespy import dynamics, i18n
from sespy.data_structure import Project
from sespy.persistent_storage import load_project
from shiny import reactive
from shiny.testserver import test_server

from sespy_app import i18n as app_i18n
from sespy_app.event_bus import create_event_bus
from sespy_app.modules import analysis_boolean as mod

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample_ses.json"
FISHERIES = (Path(sespy.__file__).parent / "templates" / "marinesabres"
             / "Fisheries_SES_Template.json")

COMPUTING = "Computing attractors"          # boolean.computing, en
ALL_OFF = "no feedback loop keeps any element active"   # boolean.all_off, en


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(i18n, "_default_translator", app_i18n.Translator(
        translations=i18n.load_translations(TRANSLATIONS)))


def _sample() -> Project:
    return Project.from_dict(json.loads(SAMPLE.read_text(encoding="utf-8")))


def _server_for(project: Project | reactive.Value):
    def server(input, output, session):
        pv = project if isinstance(project, reactive.Value) else reactive.value(project)
        mod.analysis_boolean_server("boolean", project_data=pv,
                                    event_bus=create_event_bus())
    return server


def _panel(scope) -> str:
    v = scope.get_output("attractor_panel")
    return v.value["html"] if v.status == "ok" else f"<{v.status}: {v.error}>"


def _settle(ts, scope, done, secs=60.0) -> None:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        ts.flush()
        if done(_panel(scope)):
            return
        time.sleep(0.05)


def _not_computing(html: str) -> bool:
    return COMPUTING not in html and "<silent" not in html


def _run(ts, secs=60.0):
    """Click Run (0 then 1, as a browser does) and wait for the task."""
    scope = ts.make_scope("boolean")
    scope.set_inputs(direction="cols", run_boolean=0)
    scope.set_inputs(run_boolean=1)
    _settle(ts, scope, _not_computing, secs=secs)
    return scope


def _spy(monkeypatch, name):
    calls: list[tuple[tuple, dict]] = []
    real = getattr(dynamics, name)

    def spy(*args, **kw):
        calls.append((args, kw))
        return real(*args, **kw)

    monkeypatch.setattr(dynamics, name, spy)
    return calls


def _sentinel(rules, **kw):
    return {"method": "core", "attractors": [], "error": "core_too_large",
            "n_nodes": len(rules), "core_size": 23, "max_core": 20}


# ---------------------------------------------------------------- routing ---

def test_routes_to_core_search(monkeypatch):
    core = _spy(monkeypatch, "boolean_attractors_core")
    sampled = _spy(monkeypatch, "boolean_attractors_sampled")
    old = _spy(monkeypatch, "boolean_attractors")
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        _run(ts)
    assert len(core) == 1
    args, kw = core[0]
    assert len(args) == 1 and kw == {}          # library defaults, nothing from the client
    assert sampled == [] and old == []


def test_core_too_large_falls_back_to_sampled_with_fixed_args(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core", _sentinel)
    sampled = _spy(monkeypatch, "boolean_attractors_sampled")
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        _run(ts)
    assert len(sampled) == 1
    args, kw = sampled[0]
    assert len(args) == 1
    assert kw == {"n_samples": 2000, "seed": 0, "max_steps": 1000}


# ----------------------------------------------------------------- output ---

def test_all_off_renders_sentence_not_table():
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert ALL_OFF in html, html
    assert "alert-info" in html
    assert "<table" not in html


def test_core_info_line():
    project = load_project(FISHERIES)
    with test_server(_server_for(project), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert "26 nodes · 14 in the feedback core" in html, html
    assert "basin sizes exact" in html
    assert ALL_OFF in html


def test_sampled_renders_note_and_hits_column(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core", _sentinel)
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert ("The feedback core has 23 nodes, above the exact limit of 20."
            in html), html
    assert "Sampled search: 2000 random start states (seed 0)." in html
    assert "0 trajectories did not settle within 1000 steps." in html
    # the core_too_large note comes above the sampled note
    assert html.index("The feedback core has 23") < html.index("Sampled search:")
    assert "<table" in html
    assert "Hits" in html and "Basin fraction (%)" in html
    assert "basin_size" not in html
    assert ALL_OFF not in html
    # spec §4.3: the header line is core_info plus basins_sampled
    assert ("17 nodes · 23 in the feedback core · basin fractions estimated "
            "from 2000 random starts") in html, html


def test_sampled_header_reads_samples_from_sampled_result(monkeypatch):
    # The basins_sampled suffix uses the sampled result's echoed n_samples,
    # not the app's requested value.
    monkeypatch.setattr(dynamics, "boolean_attractors_core", _sentinel)

    def fake_sampled(rules, n_samples=2000, *, max_steps=1000, seed=0):
        return {"method": "sampled",
                "attractors": [{"type": "fixed", "states": [0], "period": 1,
                                "hits": 1234, "basin_fraction": 1.0}],
                "n_samples": 1234, "n_unconverged": 0, "seed": seed,
                "max_steps": max_steps, "n_nodes": len(rules)}

    monkeypatch.setattr(dynamics, "boolean_attractors_sampled", fake_sampled)
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert ("23 in the feedback core · basin fractions estimated from 1234 "
            "random starts") in html, html


def _fake_core(n_att, total, basin_method="exact"):
    def fake(rules, **kw):
        n = len(rules)
        atts = []
        for i in range(n_att):
            a = {"type": "fixed", "states": [i], "period": 1,
                 "basin_size": 2 if basin_method == "exact" else None,
                 "basin_fraction": 1.0 / total}
            atts.append(a)
        res = {"method": "core", "n_nodes": n, "core_size": 5, "upstream_size": 0,
               "readout_size": n - 5, "basin_method": basin_method,
               "attractors": atts, "all_off_only": False,
               "n_attractors": total, "truncated": total > n_att}
        if basin_method == "sampled":
            res["basin_samples"] = 4321
            res["basin_seed"] = 0
        return res
    return fake


def test_multi_attractor_table_columns(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core", _fake_core(3, 3))
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert "17 nodes · 5 in the feedback core" in html, html
    assert "basin sizes exact" in html
    for col in ("type", "period", "Basin fraction (%)", "basin_size",
                "representative state"):
        assert col in html, col
    assert "33.33" in html                       # 1/3 as a percentage
    assert html.count("<tr") == 4                # header + 3 rows
    assert "Showing the" not in html


def test_sampled_basins_header_reads_samples_from_result(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core",
                        _fake_core(2, 2, basin_method="sampled"))
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert "basin fractions estimated from 4321 random starts" in html, html
    assert "basin_size" not in html


def test_rendered_rows_are_capped(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core", _fake_core(150, 1500))
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert html.count("<tr") == mod._MAX_ROWS + 1, html.count("<tr")
    assert ("Showing the 100 attractors with the largest basins, of 1500 found."
            in html), html


def test_sampled_rows_are_capped(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core", _sentinel)

    def fake_sampled(rules, n_samples=2000, *, max_steps=1000, seed=0):
        atts = [{"type": "fixed", "states": [i], "period": 1, "hits": 1,
                 "basin_fraction": 1 / 120} for i in range(120)]
        return {"method": "sampled", "attractors": atts, "n_samples": n_samples,
                "n_unconverged": 3, "seed": seed, "max_steps": max_steps,
                "n_nodes": len(rules)}

    monkeypatch.setattr(dynamics, "boolean_attractors_sampled", fake_sampled)
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert html.count("<tr") == mod._MAX_ROWS + 1
    assert "Showing the 100 attractors with the largest basins, of 120 found." in html
    assert "3 trajectories did not settle" in html


def test_too_large_renders_warning(monkeypatch):
    monkeypatch.setattr(dynamics, "boolean_attractors_core",
                        lambda rules, **kw: {"method": "core", "attractors": [],
                                             "error": "too_large", "n_nodes": 250})
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
    assert "alert-warning" in html
    assert (f"The network has 250 nodes, above the limit of "
            f"{dynamics.BOOLEAN_MAX_NODES_TOTAL} for Boolean analysis." in html), html


# -------------------------------------------------------------- the task ---

def test_run_shows_computing_while_the_task_runs(monkeypatch):
    release = threading.Event()
    real = dynamics.boolean_attractors_core

    def gated(rules, **kw):
        release.wait(timeout=10)
        return real(rules, **kw)

    monkeypatch.setattr(dynamics, "boolean_attractors_core", gated)
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = ts.make_scope("boolean")
        scope.set_inputs(direction="cols", run_boolean=0)
        scope.set_inputs(run_boolean=1)
        assert COMPUTING in _panel(scope), _panel(scope)
        # The Laplacian is synchronous: it is there while the search runs.
        assert scope.get_output("stability_summary").value["html"].count("<dt") == 3
        release.set()
        _settle(ts, scope, _not_computing)
        assert ALL_OFF in _panel(scope)


def test_stale_generation_is_dropped(monkeypatch):
    # Run on Fisheries (26 nodes), then Run on the sample (17 nodes) while the
    # first search is still blocked. Each call has its own gate. When the
    # first (stale) search finishes, the panel must keep showing "computing"
    # for the second Run, never Fisheries' result under the sample's Laplacian.
    gates = [threading.Event(), threading.Event()]
    calls: list[int] = []
    real = dynamics.boolean_attractors_core

    def gated(rules, **kw):
        i = len(calls)
        calls.append(len(rules))
        gates[i].wait(timeout=10)
        return real(rules, **kw)

    monkeypatch.setattr(dynamics, "boolean_attractors_core", gated)
    holder: list = []

    def server(input, output, session):
        p = reactive.value(load_project(FISHERIES))
        holder.append(p)
        mod.analysis_boolean_server("boolean", project_data=p,
                                    event_bus=create_event_bus())

    def pump(secs):
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            ts.flush()
            time.sleep(0.05)

    with test_server(server, timeout_secs=90) as ts:
        scope = ts.make_scope("boolean")
        scope.set_inputs(direction="cols", run_boolean=0)
        scope.set_inputs(run_boolean=1)
        assert COMPUTING in _panel(scope)
        with reactive.isolate():
            holder[0].set(_sample())
        scope.set_inputs(run_boolean=2)
        assert COMPUTING in _panel(scope)
        gates[0].set()                       # the stale Fisheries search finishes
        pump(3.0)
        mid = _panel(scope)
        gates[1].set()
        _settle(ts, scope, _not_computing)
        final = _panel(scope)
    assert calls == [26, 17]
    assert "26 nodes" not in mid, mid
    assert COMPUTING in mid, mid
    assert "17 nodes · 16 in the feedback core" in final, final


def test_queued_stale_runs_skip_the_search(monkeypatch):
    # ExtendedTask queues invocations made while one runs, and runs them in
    # order. Three Run clicks behind a gate: the first search is already
    # running; of the two queued, only the latest may search. The middle one
    # is stale before it starts and must return without running the core search.
    release = threading.Event()
    calls: list[int] = []
    real = dynamics.boolean_attractors_core

    def gated(rules, **kw):
        calls.append(len(rules))
        release.wait(timeout=10)
        return real(rules, **kw)

    monkeypatch.setattr(dynamics, "boolean_attractors_core", gated)
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = ts.make_scope("boolean")
        scope.set_inputs(direction="cols", run_boolean=0)
        scope.set_inputs(run_boolean=1)
        scope.set_inputs(run_boolean=2)
        scope.set_inputs(run_boolean=3)
        assert COMPUTING in _panel(scope)
        release.set()
        _settle(ts, scope, _not_computing)
        # let any (wrongly) queued extra search run before counting
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            ts.flush()
            time.sleep(0.05)
        html = _panel(scope)
    assert len(calls) == 2, calls             # the first click and the last
    assert ALL_OFF in html, html


def test_no_data_after_run_is_not_overwritten(monkeypatch):
    release = threading.Event()
    real = dynamics.boolean_attractors_core

    def gated(rules, **kw):
        release.wait(timeout=10)
        return real(rules, **kw)

    monkeypatch.setattr(dynamics, "boolean_attractors_core", gated)
    holder: list = []

    def server(input, output, session):
        p = reactive.value(_sample())
        holder.append(p)
        mod.analysis_boolean_server("boolean", project_data=p,
                                    event_bus=create_event_bus())

    with test_server(server, timeout_secs=90) as ts:
        scope = ts.make_scope("boolean")
        scope.set_inputs(direction="cols", run_boolean=0)
        scope.set_inputs(run_boolean=1)
        assert COMPUTING in _panel(scope)
        # Swap to an empty model and Run again: the synchronous no_data
        # message must not be overwritten by the in-flight (stale) search.
        with reactive.isolate():
            holder[0].set(Project.from_dict({}))
        scope.set_inputs(run_boolean=2)        # synchronous no_data
        no_data = app_i18n.t("boolean.no_data")
        assert no_data in _panel(scope), _panel(scope)
        release.set()
        # Give the stale task ample time to finish and (wrongly) land.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ts.flush()
            time.sleep(0.05)
        html = _panel(scope)
    assert no_data in html, html
    assert ALL_OFF not in html


def test_task_exception_shows_error_not_crash(monkeypatch):
    def boom(rules, **kw):
        raise RuntimeError("kaboom in the core search")

    monkeypatch.setattr(dynamics, "boolean_attractors_core", boom)
    with test_server(_server_for(_sample()), timeout_secs=90) as ts:
        scope = _run(ts)
        html = _panel(scope)
        assert ts.is_ok, ts.error
    assert "alert-danger" in html, html
    assert "kaboom in the core search" in html


# --------------------------------------------------------------------- UI ---

def test_no_max_nodes_input_in_ui():
    html = str(mod.analysis_boolean_ui("boolean"))
    assert 'id="boolean-max_nodes"' not in html
    assert 'id="boolean-direction"' in html
    assert 'id="boolean-run_boolean"' in html
