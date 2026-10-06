#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — validation.idswitch (Fase 1.6, Parte C)

Classificazione PROXY degli ID-switch in 9 categorie operative.

Per "ID-switch" si intende il pattern osservato in metrics.track_id_switches_by_frame:
coppie di celle (a, b) in frame consecutivi, spazialmente compatibili (<=25 km),
che appartengono a TRACK DIVERSE (ta != tb).

NON si assume che uno switch sia per forza un bug: alcune classi sono conseguenze
fisiche plausibili (merge/split/disappearance). La classificazione usa SOLO
informazione osservata (posizione, area, intensità, cicli di vita delle track)
e la diagnostica per-match del Tracker quando disponibile (traits diag).

Classi:
    centroid_crossing      : il centroide di b è dentro il segmento di moto di b
                             (b contigua alla continua) ma assegnata ad altra track
    merge                  : la cella b è il risultato di una convergenza multi-track
                             (più prev cella candidate -> b, diag ambiguous_merge_candidate)
    split                  : la track di a si ramifica (più curr candidate, diag
                             ambiguous_split_candidate)
    disappearance          : a è l'ultimo punto della sua track e b è il primo della
                             sua (interruzione -> nuova identità)
    intensity_jump         : |ΔdBZ(a->b)| >= 15 (salto di intensità non fisico vector?)
    area_jump              : area_ratio >= 2 oppure <= 0.5 (salto d'area non fisico)
    assignment_ambiguity   : a ha >1 candidato compatibile nel frame successivo
                             (chiunque abbia invaso il suo intorno)
    edge_of_window         : a o b sul bordo finestra (primo/ultimo frame o track
                             troncata) -> artefatto di finestra
    unknown                : nessuna spiegazione dominante
"""

from ..tracking import haversine_km
from . import metrics

RADIUS_KM = metrics.MATCH_RADIUS_KM
INTENSITY_JUMP_DBZ = 15.0
AREA_RATIO_JUMP = 2.0

CLASSES = (
    "centroid_crossing", "merge", "split", "disappearance",
    "intensity_jump", "area_jump", "assignment_ambiguity",
    "edge_of_window", "unknown",
)


def _track_of(tracks):
    mapping = {}
    for t in tracks:
        for p in t.points:
            mapping[(p.frame_index, p.cell_id)] = t
    return mapping


def _first_last_of(tracks):
    first = {}
    last = {}
    for t in tracks:
        for p in t.points:
            first.setdefault(t.track_id, p.frame_index)
            last.setdefault(t.track_id, p.frame_index)
        if t.points:
            first[t.track_id] = min(first.get(t.track_id, 10**9),
                                    t.points[0].frame_index)
            last[t.track_id] = max(last.get(t.track_id, -1),
                                   t.points[-1].frame_index)
    return first, last


def _diag_statuses(result):
    """Map {(prev_cell_id, current_cell_id)} -> set(status diagnostici)."""
    out = {}
    tracker = getattr(result, "tracker", None)
    if tracker is None:
        return out
    for frame_rows in tracker.diag:
        for r in frame_rows["matches"]:
            key = (r.get("prev_cell_id"), r.get("current_cell_id"))
            if key not in out:
                out[key] = set()
            out[key].add(r["assignment_status"])
    return out


def switch_pairs(result):
    """Elenco grezzo delle coppie (a, b) che sono ID-switch (a in fa, b in fb)."""
    cells_by_frame = result["cells_by_frame"]
    tracks = result["tracks"]
    mapping = _track_of(tracks)
    pairs = []
    for fa in range(len(cells_by_frame) - 1):
        cells_a, cells_b = cells_by_frame[fa], cells_by_frame[fa + 1]
        for a in cells_a:
            for b in cells_b:
                if haversine_km(a.centroid_lonlat, b.centroid_lonlat) > RADIUS_KM:
                    continue
                ta = mapping.get((fa, a.cell_id))
                tb = mapping.get((fa + 1, b.cell_id))
                if ta is not None and tb is not None and ta.track_id != tb.track_id:
                    pairs.append({
                        "frame": fa,
                        "cell_a": a.cell_id, "track_a": ta.track_id,
                        "cell_b": b.cell_id, "track_b": tb.track_id,
                        "distance_km": round(haversine_km(
                            a.centroid_lonlat, b.centroid_lonlat), 2),
                        "area_ratio": round(b.area_km2 / max(a.area_km2, 1e-9), 3),
                        "dint_dbz": round(b.max_dbz - a.max_dbz, 1),
                    })
    return pairs


def classify_id_switches(result):
    """Ritorna {class: count} + lista esempi per ogni classe."""
    pairs = switch_pairs(result)
    tracks = result["tracks"]
    mapping = _track_of(tracks)
    first, last = _first_last_of(tracks)
    n_frames = result["n_frames"]
    diag = _diag_statuses(result)

    counts = {c: 0 for c in CLASSES}
    examples = {c: [] for c in CLASSES}

    # candidati compatibili per cella (assignment_ambiguity): quante celle nel
    # frame successivo cadono entro RADIUS_KM
    cells_by_frame = result["cells_by_frame"]
    depth = []
    for fa, cells_a in enumerate(cells_by_frame[:-1]):
        fb_cells = cells_by_frame[fa + 1]
        row = {}
        for a in cells_a:
            row[a.cell_id] = sum(
                1 for b in fb_cells
                if haversine_km(a.centroid_lonlat, b.centroid_lonlat) <= RADIUS_KM)
        depth.append(row)

    for sw in pairs:
        fa = sw["frame"]
        a, b = sw["cell_a"], sw["cell_b"]
        ta = mapping[(fa, a)]
        tb = mapping[(fa + 1, b)]
        cls = "unknown"
        reasons = []

        # b è ultimo di a? (a è l'ultimo punto della sua track)
        a_is_last = last.get(ta.track_id) == fa
        b_is_first = first.get(tb.track_id) == fa + 1
        if a_is_last and b_is_first:
            cls = "disappearance"
            reasons.append("disappearance")

        key = (a, b)
        st = diag.get(key, set())
        if "ambiguous_merge_candidate" in st:
            cls, reasons = "merge", reasons + ["merge"]
        if "ambiguous_split_candidate" in st:
            cls, reasons = "split", reasons + ["split"]

        if abs(sw["dint_dbz"]) >= INTENSITY_JUMP_DBZ and cls != "merge":
            cls, reasons = "intensity_jump", reasons + ["intensity_jump"]
        ratio = sw["area_ratio"]
        if (ratio >= AREA_RATIO_JUMP or ratio <= 1.0 / AREA_RATIO_JUMP) \
                and cls not in ("merge", "split"):
            cls, reasons = "area_jump", reasons + ["area_jump"]

        n_cand = depth[fa].get(a, 1)
        if n_cand > 1 and cls != "unknown":
            cls = "assignment_ambiguity"
            reasons = ["assignment_ambiguity"] + reasons

        # centroid_crossing: b dentro il segmento di moto di altre track che
        # "passano" sopra l'area di a (versione operativa: track di b continua
        # oltre fa, quindi b appartiene ad un'altra traiettoria stabile)
        if cls == "unknown":
            b_keeps_moving = last.get(tb.track_id, fa) > fa + 1
            if b_keeps_moving:
                cls = "centroid_crossing"
                reasons.append("centroid_crossing")

        if fa == 0 or fa + 1 == n_frames - 1:
            if cls == "unknown":
                cls = "edge_of_window"
                reasons.append("edge_of_window")

        # l'ultimo fallback: se la categoria scelta è "unknown" ma c'era un edge
        # possibile, prevale edge_of_window per onestà sull'artefatto
        counts[cls] += 1
        ex = dict(sw, classification=cls, reasons=reasons)
        if len(examples[cls]) < 30:
            examples[cls].append(ex)

    return {"counts": counts, "examples": examples,
            "total": len(pairs),
            "rate_classified": round(
                sum(v for k, v in counts.items() if k != "unknown") /
                max(len(pairs), 1), 4)}