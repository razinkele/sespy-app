"""Presentation helpers for the Network Metrics "Causal pathways" block.

Pure functions over a `sespy.network.causal_paths` result (sespy >= 1.20.0)
so they can be unit-tested without Shiny sessions or the pyvis fork that
`analysis_metrics` imports at module load.
"""

from __future__ import annotations

import csv
import io

from sespy_app.i18n import t

CSV_HEADER = (
    "rank", "path_ids", "path_labels", "polarity", "length", "score",
    "bottleneck_source", "bottleneck_target", "bottleneck_strength",
    "bottleneck_confidence", "delay_immediate", "delay_short", "delay_long",
    "min_confidence", "truncated",
)

_LEVELS = ("weak", "medium", "strong")


_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(s: str) -> str:
    """Neutralise spreadsheet formula injection: element ids and labels come
    from project files that may be imported from others, so a text cell that
    starts with a formula character gets a leading single quote."""
    s = str(s)
    return "'" + s if s.startswith(_FORMULA_LEAD) else s


def _label(labels: dict[str, str], node: str) -> str:
    return labels.get(node, node)


def _route(path: list[str], labels: dict[str, str], sep: str = " → ") -> str:
    return sep.join(_label(labels, n) for n in path)


def _weakest(edge: dict, labels: dict[str, str]) -> str:
    # "strength" is the level the score used (unknown strength reads
    # "medium"); "confidence" is the stored raw value.
    level = edge.get("strength")
    level_txt = t(f"strength.{level}") if level in _LEVELS else str(level)
    conf = edge.get("confidence")
    conf_txt = "—" if conf is None or conf == "" else str(conf)
    return (f"{_label(labels, edge['source'])} → {_label(labels, edge['target'])}"
            f" ({level_txt}, conf {conf_txt})")


def path_rows(result: dict, labels: dict[str, str]) -> list[dict]:
    """One display dict per path, in the result's (ranked) order."""
    out = []
    for row in result.get("paths", []):
        d = row["delay_profile"]
        out.append({
            "route": _route(row["path"], labels),
            "score": f"{row['score']:.3f}",
            "links": str(row["length"]),
            "sign": row["polarity"],
            "weakest": _weakest(row["bottleneck_edge"], labels),
            "delays": t("metrics.paths_delay_fmt", immediate=d["immediate"],
                        short=d["short"], long=d["long"]),
            "min_confidence": str(row["min_confidence"]),
        })
    return out


def paths_csv(result: dict, labels: dict[str, str]) -> str:
    """Machine-readable export: English header, raw (unrounded) values."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    truncated = bool(result.get("truncated", False))
    for rank, row in enumerate(result.get("paths", []), start=1):
        bn = row["bottleneck_edge"]
        d = row["delay_profile"]
        conf = bn.get("confidence")
        w.writerow([
            rank,
            _csv_safe(" > ".join(row["path"])),
            _csv_safe(_route(row["path"], labels, " > ")),
            row["polarity"],
            row["length"],
            repr(row["score"]),
            _csv_safe(bn["source"]), _csv_safe(bn["target"]),
            _csv_safe(bn["strength"]),
            # stored raw value, unvalidated on load: text is neutralised,
            # numbers stay raw (a numeric -1 must not get a quote)
            "" if conf is None else (_csv_safe(conf) if isinstance(conf, str) else conf),
            d["immediate"], d["short"], d["long"],
            row["min_confidence"],
            truncated,
        ])
    return buf.getvalue()


def summary_lines(result: dict, labels: dict[str, str]) -> list[str]:
    """Route summary: counts, strongest +/-, contradiction, redundancy,
    and the partial-ranking notice when the enumeration was truncated."""
    c = result["counts"]
    n = len(result["paths"])
    none = t("metrics.paths_none_sign")

    def _strongest(row):
        return none if row is None else _route(row["path"], labels)

    lines = [
        t("metrics.paths_summary", n=n, pos=c["+"], neg=c["-"], amb=c["0"]),
        t("metrics.paths_strongest",
          pos=_strongest(result.get("strongest_positive")),
          neg=_strongest(result.get("strongest_negative"))),
    ]
    if result.get("contradictory"):
        lines.append(t("metrics.paths_contradictory"))
    red = result.get("redundancy") or {}
    lines.append(t("metrics.paths_redundancy",
                   edge=red.get("edge_disjoint", 0),
                   node=red.get("node_disjoint", 0)))
    if result.get("truncated"):
        lines.append(t("metrics.paths_ranked_partial", n=n))
    return lines


def _pct(p: float) -> str:
    # Only exact 0 and 1 print as "0" and "100": a route that drops out in
    # 2 of 500 draws must not read as never dropping.
    p = float(p)
    if p == 0.0:
        return "0"
    if p == 1.0:
        return "100"
    if 0.0 < p < 0.01:
        return "<1"
    if 0.99 < p < 1.0:
        return ">99"
    return f"{100.0 * p:.0f}"


def sensitivity_partial_notice(sens: dict) -> str | None:
    """The partial-ranking line for a truncated baseline (the robustness
    check only ranks the first paths found), else None."""
    if not sens.get("truncated"):
        return None
    return t("metrics.paths_ranked_partial", n=len(sens.get("paths", [])))


def sensitivity_rows(sens: dict, labels: dict[str, str]) -> list[dict]:
    """One display dict per path of a `path_sensitivity` result, in its
    (baseline) order. The probabilities become whole percentages without a
    `%` sign; the column headers carry it."""
    return [
        {"route": _route(row["path"], labels),
         "rank": _pct(row["rank_probability"]),
         "polarity": _pct(row["polarity_probability"]),
         "survival": _pct(row["survival_probability"])}
        for row in sens.get("paths", [])
    ]


def fragile_text(sens: dict, labels: dict[str, str]) -> str:
    """The fragile-links line: the result's `fragile_edges` in the library's
    order, or the none line when the list is empty."""
    edges = sens.get("fragile_edges") or []
    if not edges:
        return t("metrics.paths_sens_fragile_none")
    shown = ", ".join(
        f"{_label(labels, e['source'])} → {_label(labels, e['target'])}"
        f" (share {_pct(e['share'])}%, lift {float(e['lift']):+.2f})"
        for e in edges)
    return t("metrics.paths_sens_fragile", edges=shown)
