#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — validation.report (Fase 1.5)

Persistenza dei risultati della validation:
  - data/validation/summary.json   (output strutturato, versione + timestamp)
  - tabelle markdown riusabili per i report docs/*.

Gli output derivati SONO nel repo; i raw radar mai.
"""

import datetime as _dt
import json
import os

from . import VALIDATION_PHASE, VALIDATION_VERSION


def utcnow_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_summary(summary_path, payload):
    """Scrittura ATOMICA (tmp + replace) di summary.json."""
    payload = dict(payload)
    payload.setdefault("phase", VALIDATION_PHASE)
    payload.setdefault("validation_version", VALIDATION_VERSION)
    payload.setdefault("generated_at", utcnow_iso())
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    tmp = summary_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, summary_path)
    return summary_path


def markdown_table(headers, rows, float_fmt="{:.2f}"):
    """Riga markdown per tabella (evita colonne di sola lista)."""
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        cells = []
        for h in headers:
            v = row.get(h, "")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(float_fmt.format(v))
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def stability_idshift_tables(windows):
    """Analisi ID switching inter-finestra: utile per report Fase 6."""

    def cells_sorted(wn):
        seen = {}
        for c in [c for cells in wn["run"]["cells_by_frame"] for c in cells]:
            seen[(c.timestamp_ms, c.cell_id)] = c
        return list(seen.values())

    ids = {}
    for w in windows:
        for t in w["run"]["tracks"]:
            ids[(w["window"][0], t.track_id)] = t.motion.get("organization_score")
    rows = []
    for i, w in enumerate(windows):
        others = [x for x in windows if x is not w]
        jumps = []
        for t in w["run"]["tracks"]:
            if len(t.points) < 2:
                continue
            best = None
            for o in others:
                for ot in o["run"]["tracks"]:
                    if len(ot.points) < 2:
                        continue
                    if abs(t.points[0].timestamp_ms - ot.points[0].timestamp_ms) <= 60000 \
                            and abs(t.points[-1].timestamp_ms - ot.points[-1].timestamp_ms) <= 60000:
                        d = abs((t.motion.get("organization_score") or 0)
                                - (ot.motion.get("organization_score") or 0))
                        if best is None or d < best[0]:
                            best = (d, ot.track_id)
            if best:
                jumps.append({"track": t.track_id, "other_window": o["window"][0],
                              "other_track": best[1], "delta_score": best[0]})
        rows.append({"window": w["window"][0], "tracks": len(w["run"]["tracks"]),
                     "compared": len(jumps),
                     "max_delta_score": round(max((j["delta_score"] for j in jumps), default=0.0), 1),
                     "n_reid": len([j for j in jumps if j["delta_score"] == 0])})
    return rows