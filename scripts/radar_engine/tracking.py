#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — tracking.py (Fase 1.6: stabilization)

Storm tracking multi-frame (Hungarian / linear_sum_assignment, mantenuto come
base) con cost function IMPROVED opzionale, motion prediction semplice
(NON Kalman), temporal memory e gating fisico configurabile.

P0 FIX (Fase 1.5 -> 1.6):
  La configurazione scoring è ora INIETTATA (per dependency injection) nel
  Tracker: Tracker(cfg_tracking, cfg_scoring) e/o finalize(cfg_scoring).
  Nessun mutable state globale; il fallback a CONFIG viene usato SOLO quando
  il chiamante non fornisce nulla (libreria), ed è esplicitamente deprecato.

Cost function LEGACY (Fase 1, usata anche in 1.6 come baseline):
    cost = w_distance * d_km
         + w_area     * |ΔA|/max(A_prev,A_curr)
         + w_intensity* |ΔdBZmax|
         + w_overlap  * (1 - IoU_circle)
    (vedi cell_cost; scala invariata, peso legato a max_assignment_cost)

Cost function IMPROVED (cost_formula='improved', normalizzata, opzionale):
    total_cost =
        w_distance  * min(1, d / distance_ref_km)
      + w_prediction* min(1, pred_error / prediction_ref_km)
      + w_area      * |ΔA|/max(A_prev,A_curr)
      + w_intensity * min(1, |ΔdBZmax| / intensity_ref_dbz_cost)
      + w_overlap   * (1 - IoU_circle)
    Tutte le quantità sono normalizzate 0..1; unità documentate nei commenti.

GATING (prima di Hungarian, MAI hardcodato):
    max_storm_speed_kmh * dt  -> raggio massimo di step rispetto all'ultima
    posizione; con predizione attiva anche raggio rispetto alla posizione
    prevista (prediction_gate_km, default = step gate). Candidati fuori gate
    hanno costo = INF e non vengono assegnati (status rejected_cost).

Temporal memory:
    Per il matching si usano le MEDIE degli ultimi N=memory_frames frame per
    area e max dBZ, e una velocità stimata dall'ultimo segmento (>=2 punti)
    per la predizione: predicted = last_position + velocity_hat * dt.
    Se la track ha <2 osservazioni -> fallback distanza dal centroide.

Diagnostica (Parte B, FORMALMENTE OPZIONALE):
    Tracker(diagnostics=True) o cfg 'diagnostics' -> per ogni match/frame
    registra: track_id, prev_cell_id, current_cell_id, distance_km,
    area_ratio, intensity_difference, IoU, prediction_error_km, total_cost,
    assignment_status (matched|new_track|terminated|
                       ambiguous_merge_candidate|ambiguous_split_candidate|
                       rejected_cost).
    La diagnostica NON entra nel dataset pubblico (output.py non la usa).

Merge/split (Parte E): candidati diagnostici (ambiguous_merge_candidate /
ambiguous_split_candidate) SENZA causalità meteorologica; il comportamento
'ambiguous' della Fase 1 resta invariato (EVENT_AMBIGUOUS).
"""

import math

import numpy as np
from scipy.optimize import linear_sum_assignment

from . import models

_EPS = 1e-12

# ---------------------------------------------------------------------------
# Geografia / distanza
# ---------------------------------------------------------------------------
def haversine_km(lonlat1, lonlat2):
    """Distanza great-circle in km tra due (lon, lat)."""
    lon1, lat1 = map(math.radians, lonlat1)
    lon2, lat2 = map(math.radians, lonlat2)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lonlat1, lonlat2):
    """Bearing iniziale (deg, 0=N) da p1 a p2."""
    lon1, lat1 = map(math.radians, lonlat1)
    lon2, lat2 = map(math.radians, lonlat2)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    brng = math.degrees(math.atan2(x, y))
    return (brng + 360.0) % 360.0


def dest_from(lonlat, bearing, dist_km):
    """Punto di arrivo (lon, lat) da un punto, per rotta e distanza (equirect.)."""
    R = 6371.0088
    lon1, lat1 = map(math.radians, lonlat)
    ang = dist_km / R
    brg = math.radians(bearing)
    lat2 = math.asin(math.sin(lat1) * math.cos(ang)
                     + math.cos(lat1) * math.sin(ang) * math.cos(brg))
    lon2 = lon1 + math.atan2(math.sin(brg) * math.sin(ang) * math.cos(lat1),
                             math.cos(ang) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lon2), math.degrees(lat2))


def _circle_iou(lonlat1, area1, lonlat2, area2):
    """IoU di due dischi (raggio da area) a distanza d = haversine."""
    r1 = math.sqrt(max(area1, _EPS) / math.pi)
    r2 = math.sqrt(max(area2, _EPS) / math.pi)
    d = haversine_km(lonlat1, lonlat2)
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return 1.0
    r1s, r2s = r1 * r1, r2 * r2
    a1 = r1s * math.acos((d * d + r1s - r2s) / (2 * d * r1))
    a2 = r2s * math.acos((d * d + r2s - r1s) / (2 * d * r2))
    a_region = 0.5 * math.sqrt(max(0.0,
                                   (-d + r1 + r2) * (d + r1 - r2) *
                                   (d - r1 + r2) * (d + r1 + r2)))
    inter = max(0.0, a1 + a2 - a_region)
    union = math.pi * (r1s + r2s) - inter
    return inter / max(union, _EPS)


# ---------------------------------------------------------------------------
# Cost function
# ---------------------------------------------------------------------------
def cell_cost(prev_cell, curr_cell, cfg):
    """Cost function LEGACY (Fase 1). Scala invariata, da abbinare a
    max_assignment_cost. prev_cell puo' essere TrackPoint o DetectedCell."""
    wd = float(cfg["w_distance"])
    wa = float(cfg["w_area"])
    wi = float(cfg["w_intensity"])
    wo = float(cfg["w_overlap"])

    p_lonlat = _plonlat(prev_cell)
    p_area = getattr(prev_cell, "area_km2", None) or prev_cell.area_km2
    p_max = getattr(prev_cell, "max_dbz", None) or prev_cell.max_dbz

    distance = haversine_km(p_lonlat, curr_cell.centroid_lonlat)
    area_norm = abs(p_area - curr_cell.area_km2) / \
        max(p_area, curr_cell.area_km2, _EPS)
    intensity = abs(p_max - curr_cell.max_dbz)
    overlap = 1.0 - _circle_iou(p_lonlat, p_area,
                                 curr_cell.centroid_lonlat, curr_cell.area_km2)
    return wd * distance + wa * area_norm + wi * intensity + wo * overlap


def match_cost_improved(features, cell, cfg):
    """Cost function IMPROVED (normalizzata 0..1 per termine).

    features (dict, da Tracker._features):
        last_lonlat, area_avg_km2, dbz_avg, predicted_lonlat (o None),
        last_ts_ms (timestamp ultima osservazione)
    Termini normalizzati:
        d / distance_ref_km                       [0..1, rif 100 km]
        pred_error / prediction_ref_km             [0..1, rif 50 km]
        |ΔA|/max(A)                                [0..1]
        min(1, |ΔdBZ|/intensity_ref_dbz_cost)      [0..1, rif 35 dBZ]
        1 - IoU_circle                             [0..1]
    """
    wd = float(cfg["w_distance"])
    wp = float(cfg.get("w_prediction", 0.0))
    wa = float(cfg["w_area"])
    wi = float(cfg["w_intensity"])
    wo = float(cfg["w_overlap"])

    last = features["last_lonlat"]
    d = haversine_km(last, cell.centroid_lonlat)
    dist_ref = float(cfg.get("distance_ref_km", 100.0))
    nd = min(1.0, d / max(dist_ref, _EPS))

    pred_term = 0.0
    if wp > 0 and features.get("predicted_lonlat"):
        ref_p = float(cfg.get("prediction_ref_km", 50.0))
        pe = haversine_km(features["predicted_lonlat"], cell.centroid_lonlat)
        pred_term = min(1.0, pe / max(ref_p, _EPS))

    area_norm = abs(features["area_avg_km2"] - cell.area_km2) / \
        max(features["area_avg_km2"], cell.area_km2, _EPS)
    i_ref = float(cfg.get("intensity_ref_dbz_cost", 35.0))
    int_norm = min(1.0, abs(features["dbz_avg"] - cell.max_dbz) / max(i_ref, _EPS))
    overlap = 1.0 - _circle_iou(last, features["area_avg_km2"],
                                 cell.centroid_lonlat, cell.area_km2)

    return (wd * nd + wp * pred_term + wa * area_norm
            + wi * int_norm + wo * overlap)


def _plonlat(obj):
    return getattr(obj, "centroid_lonlat", None) or obj.lonlat


class Tracker:
    """Aggrega celle in track multi-frame (Hungarian per frame).

    Fase 1.6: accetta config SCORING iniettata (cfg_scoring) e la inoltra a
    finalize()/organization_score() nel caso il chiamante non la fornisca a
    finalize(cfg_scoring=...). La diagnmat/opzionale non altera il risultato."""

    def __init__(self, cfg_tracking, cfg_scoring=None, diagnostics=False):
        self.cfg = cfg_tracking
        self.cfg_scoring = cfg_scoring
        self.tracks = []
        self._next_track_id = 1
        self.diag = []                      # diagnostica per-match (opzionale)
        self._diagnostics = bool(diagnostics or cfg_tracking.get("diagnostics", False))
        self._improved = str(cfg_tracking.get("cost_formula", "legacy")) == "improved"

    # -- feature/diagnostica helper ----------------------------------------
    def _features(self, track, cfg):
        """Mappa di matching per una track attiva (temporal memory + predizione)."""
        pts = track.points
        last = pts[-1]
        mem = int(cfg.get("memory_frames", 3))
        tail = pts[-mem:]
        area_avg = sum(p.area_km2 for p in tail) / len(tail)
        dbz_avg = sum(p.max_dbz for p in tail) / len(tail)
        predicted, speed_kmh = None, None
        if len(pts) >= 2:
            a, b = pts[-2], pts[-1]
            dt_h = (b.timestamp_ms - a.timestamp_ms) / 3600000.0
            if dt_h > 0:
                d = haversine_km(a.lonlat, b.lonlat)
                speed_kmh = d / dt_h
                if d > 1e-9:
                    brg = bearing_deg(a.lonlat, b.lonlat)
                    # predizione rispetto al delta fino al frame corrente:
                    # il valore TARGET viene calcolato in update() con il
                    # timestamp del candidato; qui forniamo velocity_hat.
                    predicted = {
                        "speed_kmh": speed_kmh, "bearing": brg,
                        "from_ts_ms": b.timestamp_ms, "from_lonlat": b.lonlat,
                    }
        return {
            "last_lonlat": last.lonlat,
            "last_ts_ms": last.timestamp_ms,
            "area_avg_km2": area_avg,
            "dbz_avg": dbz_avg,
            "predicted": predicted,
        }

    def _target_prediction(self, features, cell_ts_ms):
        """Posizione prevista al tempo della cella corrente (se velocity nota)."""
        if not features.get("predicted"):
            return None
        pr = features["predicted"]
        dt_h = (cell_ts_ms - pr["from_ts_ms"]) / 3600000.0
        if dt_h <= 0:
            return None
        dist_km = pr["speed_kmh"] * dt_h
        return dest_from(pr["from_lonlat"], pr["bearing"], dist_km)

    def _new_track(self, cell, frame_index):
        track = models.Track(self._next_track_id, cell.timestamp_ms)
        self._next_track_id += 1
        track.points.append(models.TrackPoint(track.track_id, frame_index, cell))
        track.events.append(models.EVENT_BIRTH)
        return track

    def _gate_limits(self, features, cell, cfg):
        """Raggi massimi (km): rispetto all'ultima posizione e (se predizione
        attiva) rispetto alla posizione prevista. Gate = None -> disabled."""
        max_speed = cfg.get("max_storm_speed_kmh")
        if not max_speed:
            return None, None
        dt_h = max((cell.timestamp_ms - features["last_ts_ms"]) / 3600000.0, _EPS)
        step_gate = float(max_speed) * dt_h
        pred_gate = cfg.get("prediction_gate_km")
        pred_gate = float(pred_gate) if pred_gate else step_gate
        return step_gate, pred_gate

    def _record(self, frame_index, rows):
        if self._diagnostics:
            self.diag.append({"frame": frame_index, "matches": rows})

    # -----------------------------------------------------------------------
    def update(self, cells, frame_index):
        """Frame corrente (celle rilevate) -> aggiorna le track attive."""
        active = [t for t in self.tracks if t.status == models.STATUS_ACTIVE]
        if not active or not cells:
            rows = []
            for cell in cells:
                self.tracks.append(self._new_track(cell, frame_index))
                rows.append({"track_id": self._next_track_id - 1,
                             "prev_cell_id": None, "current_cell_id": cell.cell_id,
                             "assignment_status": "new_track"})
            for t in active:
                t.status = models.STATUS_DEAD
                t.death_ms = t.points[-1].timestamp_ms
                t.events.append(models.EVENT_DEATH)
                rows.append({"track_id": t.track_id, "prev_cell_id":
                             t.points[-1].cell_id, "current_cell_id": None,
                             "assignment_status": "terminated"})
            self._record(frame_index, rows)
            return

        features_list = [self._features(t, self.cfg) for t in active]
        cost = np.full((len(active), len(cells)), np.inf, dtype=float)
        cand_meta = {}   # (i,j) -> {distance_km, area_ratio, int_diff, iou, pred_err}
        for i, (t, feats) in enumerate(zip(active, features_list)):
            for j, cell in enumerate(cells):
                sg, pg = self._gate_limits(feats, cell, self.cfg)
                d = haversine_km(feats["last_lonlat"], cell.centroid_lonlat)
                if sg is not None and d > sg:
                    continue  # fuori gate -> INF (rejected)
                pred_target = None
                if feats.get("predicted"):
                    pred_target = self._target_prediction(feats, cell.timestamp_ms)
                    if (pg is not None and pred_target is not None
                            and haversine_km(pred_target, cell.centroid_lonlat) > pg):
                        continue
                feats_here = dict(feats)
                feats_here["predicted_lonlat"] = pred_target
                if self._improved:
                    c = match_cost_improved(feats_here, cell, self.cfg)
                else:
                    c = cell_cost(_pt_as_cell(t.points[-1]), cell, self.cfg)
                if c <= float(self.cfg["max_assignment_cost"]):
                    cost[i, j] = c
                cand_meta[(i, j)] = {
                    "distance_km": round(d, 3),
                    "area_ratio": round(cell.area_km2 / max(feats["area_avg_km2"], _EPS), 3),
                    "intensity_difference": round(abs(feats["dbz_avg"] - cell.max_dbz), 2),
                    "iou": round(_circle_iou(feats["last_lonlat"], feats["area_avg_km2"],
                                             cell.centroid_lonlat, cell.area_km2), 4),
                    "prediction_error_km": round(
                        haversine_km(pred_target, cell.centroid_lonlat), 3)
                        if pred_target is not None else None,
                    "gates": (sg, pg),
                }

        max_cost = float(self.cfg["max_assignment_cost"])
        rows = []
        # candidati "vicini" (<=25 km, co-locazione fisica) per merge/split
        # diagnostico: vicini = raggiungibili (costo finito) e distanza <=25 km.
        near_row = np.zeros(len(active), dtype=int)
        near_col = np.zeros(len(cells), dtype=int)
        for (i, j), meta in cand_meta.items():
            if meta["distance_km"] <= 25.0 and np.isfinite(cost[i, j]):
                near_row[i] += 1
                near_col[j] += 1
        amb_rows = set(np.where(near_row > 1)[0].tolist())  # split candidato
        amb_cols = set(np.where(near_col > 1)[0].tolist())  # merge candidato

        assignment, reversed_assign = {}, {}
        if cost.size:
            fin = np.isfinite(cost)
            if fin.any():
                big = float(max_cost) * max(cost.shape) + 1.0
                work = np.where(fin, cost, big)
                row_idx, col_idx = linear_sum_assignment(work)
                for ti, ci in zip(row_idx, col_idx):
                    if np.isfinite(cost[ti, ci]) and cost[ti, ci] <= max_cost:
                        assignment[ti] = ci
                        reversed_assign[ci] = ti

        # ambiguous candidato (merge/split) -> EVENT_AMBIGUOUS come in Fase 1
        for ti, ci in assignment.items():
            cell = cells[ci]
            track = active[ti]
            track.points.append(models.TrackPoint(track.track_id, frame_index, cell))
            if ti in amb_rows or ci in amb_cols:
                track.events.append(models.EVENT_AMBIGUOUS)

        # righe diagnostica per ogni candidato valutato
        for ti, t in enumerate(active):
            for ci, cell in enumerate(cells):
                meta = cand_meta.get((ti, ci))
                if meta is None:
                    if self._diagnostics:
                        rows.append({"track_id": t.track_id,
                                     "prev_cell_id": t.points[-1].cell_id,
                                     "current_cell_id": cell.cell_id,
                                     "assignment_status": "rejected_cost",
                                     "total_cost": None,
                                     "distance_km": round(haversine_km(
                                         t.points[-1].lonlat, cell.centroid_lonlat), 3)})
                    continue
                is_matched = ti in assignment and assignment[ti] == ci
                status = "matched" if is_matched else "rejected_cost"
                if is_matched and ci in amb_cols:
                    status = "ambiguous_merge_candidate"
                if is_matched and ti in amb_rows:
                    status = "ambiguous_split_candidate"
                if self._diagnostics:
                    rows.append({"track_id": t.track_id,
                                 "prev_cell_id": t.points[-1].cell_id,
                                 "current_cell_id": cell.cell_id,
                                 "assignment_status": status,
                                 "total_cost": round(cost[ti, ci], 4)
                                 if np.isfinite(cost[ti, ci]) else None,
                                 **meta})

        for j, cell in enumerate(cells):
            if j not in reversed_assign:
                self.tracks.append(self._new_track(cell, frame_index))
                rows.append({"track_id": self._next_track_id - 1,
                             "prev_cell_id": None, "current_cell_id": cell.cell_id,
                             "assignment_status": "new_track"})

        for ti, track in enumerate(active):
            if ti not in assignment:
                track.status = models.STATUS_DEAD
                track.death_ms = track.points[-1].timestamp_ms
                track.events.append(models.EVENT_DEATH)
                rows.append({"track_id": track.track_id,
                             "prev_cell_id": track.points[-1].cell_id,
                             "current_cell_id": None,
                             "assignment_status": "terminated"})

        self._record(frame_index, rows)

    # -----------------------------------------------------------------------
    def finalize(self, cfg_scoring=None):
        """Calcola motion per ogni track (timestamp reali) + Organization Score
        con la configurazione scoring INIETTATA (P0 fix) + score_confidence."""
        from . import scoring
        full_frames = int(self.cfg.get("full_confidence_frames", 5))
        scfg = cfg_scoring or self.cfg_scoring or scoring._fallback_scoring()
        for track in self.tracks:
            pts = track.points
            if len(pts) < 2:
                track.tracking_confidence = "insufficient"
                track.score_confidence = "low"
                continue
            n = len(pts)
            track.tracking_confidence = ("full" if n >= full_frames else "low")
            track.score_confidence = (
                "high" if n >= full_frames and max(p.max_dbz for p in pts) >= 35.0
                else ("medium" if n >= 3 else "low"))
            t0 = pts[0].timestamp_ms
            t1 = pts[-1].timestamp_ms
            duration_min = (t1 - t0) / 60000.0
            if duration_min <= 0:
                continue
            dist = 0.0
            for a, b in zip(pts, pts[1:]):
                dist += haversine_km(a.lonlat, b.lonlat)
            vel = dist / (duration_min / 60.0)
            toward = bearing_deg(pts[0].lonlat, pts[-1].lonlat)
            from_deg = (toward + 180.0) % 360.0
            growth = 0.0
            if pts[0].area_km2 > 0:
                growth = (pts[-1].area_km2 - pts[0].area_km2) / pts[0].area_km2 * 100.0
            idelta = pts[-1].max_dbz - pts[0].max_dbz
            track.motion = {
                "distance_km": round(dist, 2),
                "velocity_kmh": round(vel, 1),
                "direction_toward_deg": round(toward, 1),
                "direction_from_deg": round(from_deg, 1),
                "duration_min": round(duration_min, 1),
                "area_growth_pct": round(growth, 1),
                "intensity_delta_dbz": round(idelta, 2),
                "_avg_solidity": round(sum(p.solidity for p in pts) / n, 4),
                "_avg_compactness": round(sum(p.compactness for p in pts) / n, 4),
            }
            score, label, probs, _comps = scoring.organization_score(track, scfg)
            track.motion["organization_score"] = score
            track.motion["classification"] = label
            track.motion["class_probs"] = probs
        return self.tracks

    def complete_cycles(self):
        """Chiude eventuali track attive (fine sequenza)."""
        for track in self.tracks:
            if track.status == models.STATUS_ACTIVE:
                track.status = models.STATUS_DEAD
                track.death_ms = track.points[-1].timestamp_ms
        return self.tracks


def _pt_as_cell(point):
    """Adatta un TrackPoint alla cost function legacy (senza copie costose)."""
    return point