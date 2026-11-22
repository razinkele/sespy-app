"""Causal pathways view helpers (sespy_app/modules/_paths_view.py).

Hand-built `causal_paths` result dicts (the sespy 1.20.0 shape); no Shiny,
no pyvis.
"""
from __future__ import annotations

import csv
import io

import pytest
from _paths import TRANSLATIONS
from sespy import i18n

from sespy_app.i18n import Translator, load_translations
from sespy_app.modules._paths_view import path_rows, paths_csv, summary_lines

TR = Translator(translations=load_translations(TRANSLATIONS))


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    # Through the production loader, English, outside any Shiny session.
    TR._lang = "en"
    monkeypatch.setattr(i18n, "_default_translator", TR)


LABELS = {"A": "Alpha", "B": "Beta", "C": "Gamma", "D": "Delta"}

HEADER = ("rank,path_ids,path_labels,polarity,length,score,bottleneck_source,"
          "bottleneck_target,bottleneck_strength,bottleneck_confidence,"
          "delay_immediate,delay_short,delay_long,min_confidence,truncated")


def _row(path, polarity, score, bn, delays, min_conf):
    return {
        "path": list(path), "length": len(path) - 1, "polarity": polarity,
        "score": score,
        "bottleneck_edge": dict(zip(("source", "target", "strength", "confidence"), bn)),
        "delay_profile": dict(zip(("immediate", "short", "long"), delays)),
        "min_confidence": min_conf,
    }


POS = _row(["A", "D"], "+", 0.55, ("A", "D", "medium", 4), (1, 0, 0), 4)
NEG = _row(["A", "B", "D"], "-", 0.1981264785190917,
           ("B", "D", "weak", None), (0, 1, 1), 2)
AMB = _row(["A", "C", "D"], "0", 0.0123456789,
           ("A", "C", "weak", 2), (2, 0, 0), 2)


def _result(rows, *, truncated=False, contradictory=None, red=(1, 1)):
    counts = {"+": 0, "-": 0, "0": 0}
    for r in rows:
        counts[r["polarity"]] += 1
    return {
        "paths": rows, "counts": counts, "truncated": truncated,
        "strongest_positive": next((r for r in rows if r["polarity"] == "+"), None),
        "strongest_negative": next((r for r in rows if r["polarity"] == "-"), None),
        "contradictory": (counts["+"] > 0 and counts["-"] > 0
                          if contradictory is None else contradictory),
        "redundancy": {"edge_disjoint": red[0], "node_disjoint": red[1]},
    }


def test_path_rows_columns_and_formatting():
    rows = path_rows(_result([POS, NEG, AMB]), LABELS)
    assert len(rows) == 3
    assert set(rows[0]) == {"route", "score", "links", "sign", "weakest",
                            "delays", "min_confidence"}
    assert rows[0]["route"] == "Alpha → Delta"
    assert rows[1]["route"] == "Alpha → Beta → Delta"
    assert rows[0]["score"] == "0.550"
    assert rows[1]["score"] == "0.198"
    assert rows[2]["score"] == "0.012"
    assert rows[1]["links"] == "2"
    assert [r["sign"] for r in rows] == ["+", "-", "0"]
    # weakest link: labels, the scored strength level, the stored confidence
    assert rows[0]["weakest"] == "Alpha → Delta (Medium, conf 4)"
    assert rows[1]["weakest"] == "Beta → Delta (Weak, conf —)"
    assert rows[0]["delays"] == "1 immediate, 0 short, 0 long"
    assert rows[1]["delays"] == "0 immediate, 1 short, 1 long"
    assert rows[1]["min_confidence"] == "2"


def test_path_rows_unknown_label_falls_back_to_id():
    rows = path_rows(_result([NEG]), {"A": "Alpha"})
    assert rows[0]["route"] == "Alpha → B → D"
    assert rows[0]["weakest"].startswith("B → D (")


def test_summary_lines_order_and_conditionals():
    plain = summary_lines(_result([NEG], red=(1, 1)), LABELS)
    assert plain == [
        "1 paths: 0 positive, 1 negative, 0 ambiguous",
        "Strongest positive route: none. Strongest negative route: Alpha → Beta → Delta.",
        "Disjoint routes among the paths listed: 1 sharing no link, "
        "1 sharing no intermediate element.",
    ]
    full = summary_lines(_result([POS, NEG, AMB], truncated=True, red=(2, 1)), LABELS)
    assert len(full) == 5
    assert full[0] == "3 paths: 1 positive, 1 negative, 1 ambiguous"
    assert full[1] == ("Strongest positive route: Alpha → Delta. "
                       "Strongest negative route: Alpha → Beta → Delta.")
    assert full[2].startswith("Positive and negative routes both connect")
    assert full[3] == ("Disjoint routes among the paths listed: 2 sharing no link, "
                       "1 sharing no intermediate element.")
    assert full[4] == ("Ranked among the first 3 paths found; "
                       "a stronger path may exist beyond them.")


def test_strongest_none_renders_none_word():
    lines = summary_lines(_result([AMB]), LABELS)
    assert lines[1] == "Strongest positive route: none. Strongest negative route: none."
    assert not any(s.startswith("Positive and negative") for s in lines)


def test_paths_csv_matches_rows():
    result = _result([POS, NEG, AMB], truncated=True)
    text = paths_csv(result, LABELS)
    assert text.splitlines()[0] == HEADER
    recs = list(csv.reader(io.StringIO(text)))
    assert recs[0] == HEADER.split(",")
    body = recs[1:]
    assert len(body) == len(path_rows(result, LABELS)) == 3
    assert body[0] == ["1", "A > D", "Alpha > Delta", "+", "1", repr(0.55),
                       "A", "D", "medium", "4", "1", "0", "0", "4", "True"]
    assert body[1][1:3] == ["A > B > D", "Alpha > Beta > Delta"]
    assert body[1][5] == repr(0.1981264785190917)   # unrounded
    assert float(body[1][5]) == NEG["score"]
    assert body[1][9] == ""                          # stored confidence None
    assert body[2][0] == "3"
    assert all(r[-1] == "True" for r in body)


def test_paths_csv_neutralises_formula_text_cells():
    row = _row(["@cmd", "B"], "-", 0.55, ("@cmd", "B", "medium", 3), (1, 0, 0), 3)
    labels = {"@cmd": '=HYPERLINK("x")', "B": "Beta"}
    rec = list(csv.reader(io.StringIO(paths_csv(_result([row]), labels))))[1]
    assert rec[1] == "'@cmd > B"                       # path_ids
    assert rec[2] == "'=HYPERLINK(\"x\") > Beta"       # path_labels
    assert rec[6] == "'@cmd"                           # bottleneck_source
    assert rec[7] == "B" and rec[8] == "medium"        # safe text untouched
    # numeric, polarity and truncated cells are unchanged
    assert rec[0] == "1" and rec[3] == "-" and rec[4] == "1"
    assert rec[5] == repr(0.55) and rec[9] == "3"
    assert rec[10:] == ["1", "0", "0", "3", "False"]


def test_paths_csv_empty_result_is_header_only():
    empty = _result([], red=(0, 0))
    assert paths_csv(empty, LABELS).splitlines() == [HEADER]


def test_partial_ranking_line_when_truncated():
    lines = summary_lines(_result([POS, NEG], truncated=True), LABELS)
    assert lines[-1] == ("Ranked among the first 2 paths found; "
                         "a stronger path may exist beyond them.")
    not_trunc = summary_lines(_result([POS, NEG], truncated=False), LABELS)
    assert not any(s.startswith("Ranked among") for s in not_trunc)


# ------------------------------------------------- robustness (Task 6) ---

SENS = {
    "n_draws": 500, "seed": 0, "base": 0.5, "flip_mode": "confidence",
    "paths": [
        {"path": ["A", "D"], "polarity": "+", "score": 0.55,
         "rank_probability": 0.734, "polarity_probability": 0.876,
         "survival_probability": 0.996},
        {"path": ["A", "X", "D"], "polarity": "-", "score": 0.1,
         "rank_probability": 0.0, "polarity_probability": 0.004,
         "survival_probability": 1.0},
    ],
    "fragile_edges": [
        {"source": "A", "target": "D", "share": 0.214, "lift": 0.3456},
        {"source": "B", "target": "C", "share": 0.07, "lift": 0.0123},
    ],
    "truncated": False,
}


def test_sensitivity_rows_percentages():
    from sespy_app.modules._paths_view import sensitivity_rows

    rows = sensitivity_rows(SENS, LABELS)
    assert rows == [
        {"route": "Alpha → Delta", "rank": "73", "polarity": "88",
         "survival": ">99"},
        {"route": "Alpha → X → Delta", "rank": "0", "polarity": "<1",
         "survival": "100"},
    ]
    assert sensitivity_rows({"paths": []}, LABELS) == []


def test_fragile_text_lists_edges_in_library_order():
    from sespy_app.modules._paths_view import fragile_text

    s = fragile_text(SENS, LABELS)
    assert s.startswith("Links whose uncertainty most often changes the top route: ")
    assert "Alpha → Delta (share 21%, lift +0.35)" in s
    assert "Beta → Gamma (share 7%, lift +0.01)" in s
    assert s.index("Alpha → Delta") < s.index("Beta → Gamma")


def test_fragile_text_none_when_empty():
    from sespy_app.modules._paths_view import fragile_text

    assert fragile_text({**SENS, "fragile_edges": []}, LABELS) == (
        "No single link's uncertainty clearly changes the top route.")


def test_sensitivity_percent_edges_never_round_to_0_or_100():
    # A route that drops out in 2 of 500 draws must not read as never
    # dropping; only exact 0 and 1 print as "0" and "100".
    from sespy_app.modules._paths_view import sensitivity_rows

    def one(p):
        sens = {"paths": [{"path": ["A", "D"], "polarity": "+", "score": 1.0,
                           "rank_probability": p, "polarity_probability": p,
                           "survival_probability": p}]}
        return sensitivity_rows(sens, LABELS)[0]["rank"]

    assert one(0.0) == "0"
    assert one(1.0) == "100"
    assert one(0.002) == "<1"
    assert one(0.004) == "<1"
    assert one(0.996) == ">99"
    assert one(0.998) == ">99"
    assert one(0.01) == "1"
    assert one(0.99) == "99"
    assert one(0.734) == "73"


def test_sensitivity_partial_notice_only_when_truncated():
    from sespy_app.modules._paths_view import sensitivity_partial_notice

    assert sensitivity_partial_notice(SENS) is None
    assert sensitivity_partial_notice({**SENS, "truncated": True}) == (
        "Ranked among the first 2 paths found; "
        "a stronger path may exist beyond them.")
