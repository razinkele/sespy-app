"""Boolean / Laplacian analysis module.

Mirrors `modules/analysis_boolean.R`. Two tabs:
  - Laplacian: bar chart of eigenvalue spectrum + stability summary card.
  - Boolean: attractor table from the library's feedback-core search
    (`dynamics.boolean_attractors_core`, spec 2026-11-21 §4). Only the
    feedback core is searched exhaustively, so the limit applies to the core
    (fixed by the library), not to the whole network. When the core is above
    that limit, the panel falls back to a sampled whole-network search with
    fixed arguments that the client cannot set.

The Laplacian is computed synchronously in the Run effect; the attractor
search runs in an ExtendedTask (`asyncio.to_thread`) with the generation
counter pattern of `analysis_leverage.py`, so a result made stale by a later
Run is dropped.

Pattern matches `analysis_metrics.py` (matplotlib via @render.plot, no pyvis).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from sespy import dynamics
from sespy.data_structure import Project
from shiny import Inputs, Outputs, Session, module, reactive, render, ui

from sespy_app.event_bus import EventBus
from sespy_app.i18n import Translator, t

from ._notify import notify_data_changed

_COMPUTING = object()

# Rendered-row cap for both attractor tables. The library already caps the
# core result at BOOLEAN_MAX_ATTRACTORS; a table of that size is unreadable
# and heavy to ship, so the panel shows the largest basins only.
_MAX_ROWS = 100

# Sampled fallback (spec §4.1): fixed, not client-settable.
_SAMPLED_N = 2000
_SAMPLED_SEED = 0
_SAMPLED_MAX_STEPS = 1000


def _format_state(state_int: int, n: int) -> str:
    """Format integer state as fixed-width binary string, LSB-first by node index.

    Returns an empty string for n == 0. For n > 0, the i-th character of
    the result corresponds to bit i of state_int (the value of node i).
    """
    if n == 0:
        return ""
    return format(state_int, f"0{n}b")[::-1]  # reverse so bit i = node i


def _format_pct(fraction: float) -> str:
    pct = 100.0 * float(fraction)
    if 0.0 < pct < 0.01:
        return "<0.01"
    return f"{pct:.2f}"


def _search(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Worker-thread body: plain Python, no reactives, no `t()`.

    Calls the core search with the library defaults; only on its
    `core_too_large` sentinel runs the sampled search.
    """
    core = dynamics.boolean_attractors_core(rules)
    sampled = None
    if core.get("error") == "core_too_large":
        sampled = dynamics.boolean_attractors_sampled(
            rules, n_samples=_SAMPLED_N, seed=_SAMPLED_SEED,
            max_steps=_SAMPLED_MAX_STEPS)
    return {"core": core, "sampled": sampled}


@module.ui
def analysis_boolean_ui() -> ui.Tag:
    return ui.card(
        ui.card_header(t("nav.boolean")),
        ui.layout_sidebar(
            ui.sidebar(
                ui.h5(t("boolean.controls")),
                ui.input_radio_buttons(
                    "direction",
                    t("boolean.direction"),
                    {"cols": t("boolean.cols"), "rows": t("boolean.rows")},
                    selected="cols",
                ),
                ui.tags.hr(),
                ui.input_action_button(
                    "run_boolean",
                    t("boolean.run"),
                    class_="btn btn-primary btn-block",
                ),
                width=280,
            ),
            ui.navset_tab(
                ui.nav_panel(
                    t("boolean.tab_laplacian"),
                    ui.output_plot("eigenvalue_plot", height="280px"),
                    ui.tags.hr(),
                    ui.output_ui("stability_summary"),
                ),
                ui.nav_panel(
                    t("boolean.tab_boolean"),
                    ui.output_ui("attractor_panel"),
                ),
                id="boolean_tabs",
            ),
        ),
        class_="sespy-card sespy-card-canvas",
        full_screen=True,
    )


def _table(rows: list[dict[str, Any]]) -> ui.HTML:
    import pandas as pd

    df = pd.DataFrame(rows)
    return ui.HTML(df.to_html(index=False, classes="table table-sm table-striped"))


def _rep_state(att: dict[str, Any], n: int) -> str:
    states = att.get("states") or []
    first = _format_state(states[0], n) if states else ""
    period = att.get("period", 1)
    extra = f" + {period - 1} more" if period > 1 else ""
    return first + extra


def _truncated_note(shown: int, total: int) -> list[ui.Tag]:
    if total > shown:
        return [ui.tags.p(t("boolean.truncated", shown=shown, total=total),
                          class_="text-muted")]
    return []


def _too_large(res: dict[str, Any]) -> ui.Tag:
    return ui.tags.div(
        t("boolean.too_large", n=res.get("n_nodes", 0),
          max=dynamics.BOOLEAN_MAX_NODES_TOTAL),
        class_="alert alert-warning")


def _core_view(core: dict[str, Any]) -> ui.Tag:
    n = core.get("n_nodes", 0)
    exact = core.get("basin_method") == "exact"
    basins = (t("boolean.basins_exact") if exact
              else t("boolean.basins_sampled", samples=core.get("basin_samples", 0)))
    header = ui.tags.p(
        f"{t('boolean.core_info', n=n, core=core.get('core_size', 0))} · {basins}",
        class_="text-muted boolean-core-info")
    atts = core.get("attractors") or []
    if core.get("all_off_only"):
        return ui.div(header, ui.tags.div(t("boolean.all_off"),
                                          class_="alert alert-info"))
    if not atts:
        return ui.div(header, ui.tags.p(t("boolean.no_attractors")))
    rows = []
    for att in atts[:_MAX_ROWS]:
        row: dict[str, Any] = {
            "type": att["type"],
            "period": att["period"],
            t("boolean.col_basin_fraction"): _format_pct(att["basin_fraction"]),
        }
        if exact:
            # str: basin sizes reach 2^200, beyond any numeric column.
            row["basin_size"] = str(att.get("basin_size"))
        row["representative state"] = _rep_state(att, n)
        rows.append(row)
    total = core.get("n_attractors", len(atts))
    return ui.div(header, _table(rows), *_truncated_note(len(rows), total))


def _sampled_view(core: dict[str, Any], sampled: dict[str, Any]) -> ui.Tag:
    n = sampled.get("n_nodes", core.get("n_nodes", 0))
    # Spec §4.3: core_info plus basins_sampled, with the sampled result's own
    # echoed n_samples (the effective value after the library's clamp).
    basins = t("boolean.basins_sampled", samples=sampled.get("n_samples", 0))
    core_info = t("boolean.core_info", n=core.get("n_nodes", n),
                  core=core.get("core_size", 0))
    header = ui.tags.p(f"{core_info} · {basins}",
                       class_="text-muted boolean-core-info")
    too_big = ui.tags.div(
        t("boolean.core_too_large", core=core.get("core_size", 0),
          max=core.get("max_core", 0)),
        class_="alert alert-warning")
    note = ui.tags.p(
        t("boolean.sampled_note", n=sampled.get("n_samples", 0),
          seed=sampled.get("seed", 0), unconverged=sampled.get("n_unconverged", 0),
          max_steps=sampled.get("max_steps", 0)),
        class_="text-muted")
    atts = sampled.get("attractors") or []
    if not atts:
        return ui.div(header, too_big, note, ui.tags.p(t("boolean.no_attractors")))
    rows = []
    for att in atts[:_MAX_ROWS]:
        rows.append({
            "type": att["type"],
            "period": att["period"],
            t("boolean.col_hits"): att.get("hits", 0),
            t("boolean.col_basin_fraction"): _format_pct(att["basin_fraction"]),
            "representative state": _rep_state(att, n),
        })
    return ui.div(header, too_big, note, _table(rows),
                  *_truncated_note(len(rows), len(atts)))


@module.server
def analysis_boolean_server(
    input: Inputs,
    output: Outputs,
    session: Session,
    *,
    project_data: reactive.Value[Project],
    event_bus: EventBus,
    translator: Translator | None = None,
) -> None:

    # Laplacian result and errors of the synchronous part of Run.
    result_store: reactive.Value[dict | None] = reactive.value(None)
    # Attractor search: None | _COMPUTING | {"core", "sampled"} | {"error"}.
    att_state: reactive.Value[Any] = reactive.value(None)
    _gen = [0]                                  # plain cell — NOT reactive (avoids self-loop)

    @reactive.extended_task
    async def _att_task(rules, gen):
        # Exceptions travel with their generation, so a stale failure is
        # dropped exactly like a stale success.
        # Invocations made while a search runs are queued and run serially.
        # One made stale by a later Run must not start a thread at all:
        # repeated clicks would otherwise queue full searches on the shared
        # worker. (cancel() is no fix: it stops the asyncio task, not the
        # to_thread worker.) _gen is a plain list cell, so reading it here is
        # allowed; the observer drops the (gen, None) by its own gen check.
        if gen != _gen[0]:
            return (gen, None)
        try:
            result = await asyncio.to_thread(_search, rules)
        except Exception as exc:                # noqa: BLE001 — reported by _att_observe
            return (gen, exc)
        return (gen, result)

    @reactive.effect
    @reactive.event(input.run_boolean, ignore_init=True)
    def _run() -> None:
        # First, before any early return: an in-flight search must not
        # overwrite what this Run shows (no_data, an error, or its own result).
        _gen[0] += 1
        gen = _gen[0]
        try:
            isa = project_data.get().isa_data
            if not isa.elements:
                result_store.set({"error": t("boolean.no_data"), "stability": None,
                                  "node_ids": []})
                att_state.set(None)
                return
            M, node_ids = dynamics.isa_to_numeric_matrix(isa)
            stability = dynamics.laplacian_stability(M, direction=input.direction() or "cols")
            rules = dynamics.create_boolean_rules(M)
        except (ValueError, np.linalg.LinAlgError) as exc:
            result_store.set({"error": str(exc), "stability": None, "node_ids": []})
            att_state.set(None)
            return
        result_store.set({"error": None, "stability": stability, "node_ids": node_ids})
        att_state.set(_COMPUTING)
        _att_task(rules, gen)

    @reactive.effect
    def _att_observe() -> None:
        # No `except Exception` branch here: _att_task catches every Exception
        # around the search and returns it with its generation (the code
        # outside that try is a comparison and a tuple return), so a raised
        # task error is unreachable, and a branch without a gen check could
        # only show a stale error. SilentException (not yet run / running)
        # propagates as usual.
        gen, result = _att_task.result()
        if gen != _gen[0]:
            return
        if isinstance(result, BaseException):
            logging.getLogger(__name__).error(
                "boolean attractor search failed", exc_info=result)
            att_state.set({"error": str(result) or type(result).__name__})
            return
        att_state.set(result)

    @reactive.effect
    def _stale_warning() -> None:
        # Subscribe ONLY to ISA changes. The reactive read of result_store
        # must be isolated — otherwise this effect re-fires when Run sets
        # result_store and would post a spurious "data changed" notification
        # immediately after a successful run.
        event_bus.isa_change.get()
        with reactive.isolate():
            if result_store.get() is not None:
                notify_data_changed()

    @output
    @render.plot
    def eigenvalue_plot():
        import matplotlib.pyplot as plt

        r = result_store.get()
        fig, ax = plt.subplots(figsize=(8, 2.6))
        if r is None:
            ax.text(0.5, 0.5, t("boolean.click_run"), ha="center", va="center",
                    transform=ax.transAxes)
            ax.axis("off")
            fig.tight_layout()
            return fig
        if r.get("error"):
            ax.text(0.5, 0.5, r["error"], ha="center", va="center",
                    color="#a02020", transform=ax.transAxes, wrap=True)
            ax.axis("off")
            fig.tight_layout()
            return fig
        eigvals = r["stability"]["eigenvalues"]
        reals = [v.real for v in eigvals]
        ax.bar(range(len(reals)), reals, color="#4a90b8", edgecolor="#2d5a7b")
        ax.set_xlabel(t("boolean.eigenvalue_index"))
        ax.set_ylabel(t("boolean.real_part"))
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        return fig

    @output
    @render.ui
    def stability_summary():
        r = result_store.get()
        if r is None or r.get("error") or r.get("stability") is None:
            return ui.tags.p(t("boolean.click_run"), class_="text-muted")
        s = r["stability"]
        return ui.tags.dl(
            ui.tags.dt(t("boolean.spectral_radius")),
            ui.tags.dd(f"{s['spectral_radius']:.4f}"),
            ui.tags.dt(t("boolean.algebraic_connectivity")),
            ui.tags.dd(f"{s['algebraic_connectivity']:.4f}"),
            ui.tags.dt(t("boolean.stability_class")),
            ui.tags.dd(s["stability_class"]),
        )

    @output
    @render.ui
    def attractor_panel():
        r = result_store.get()
        if r is None:
            return ui.tags.p(t("boolean.click_run"), class_="text-muted")
        if r.get("error"):
            return ui.tags.div(r["error"], class_="alert alert-danger")
        a = att_state.get()
        if a is None:
            return ui.tags.p(t("boolean.click_run"), class_="text-muted")
        if a is _COMPUTING:
            return ui.tags.p(t("boolean.computing"), class_="text-muted")
        if "error" in a:
            return ui.tags.div(f"{t('boolean.error_label')}: {a['error']}",
                               class_="alert alert-danger")
        core = a["core"]
        sampled = a.get("sampled")
        if core.get("error") == "too_large":
            return _too_large(core)
        if core.get("error") == "core_too_large":
            if sampled is None:   # defensive: _search always pairs them
                return ui.tags.p(t("boolean.no_attractors"))
            if sampled.get("error") == "too_large":
                return _too_large(sampled)
            return _sampled_view(core, sampled)
        return _core_view(core)
