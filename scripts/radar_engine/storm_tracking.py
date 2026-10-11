#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — storm_tracking.py (Fase 1.7, Parti B/C/D)

Layer di IDENTITÀ PRINCIPALE: lo StormObjectTracker lavora sugli storm object
aggregati (aggregation.aggregate_frame). Il CellTracker (tracking.py) resta il
layer LOCALE e NON viene sostituito (multi-layer).

Parte B — cost function normalizzata 0..1 per termine:
    cost = w_distance * min(1, d/distance_ref)
         + w_prediction * min(1, pred_error/prediction_ref)   (motion continuity)
         + w_iou * (1 - IoU_circle)
         + w_area * |ΔA|/max(A)
         + w_intensity * min(1, |ΔdBZmax|/intensity_ref)
         + w_cells * min(1, |Δcell_count|/cell_count_ref)
    PRIORITÀ: geometry overlap (w_iou) > centroid continuity (w_distance) >
    motion continuity (predizione, NON Kalman). Tutti i pesi configurabili.

Parte C — motion sullo storm object (velocità/direzione) con motion_confidence
    low/medium/high (track age, n osservazioni, continuità geometrica mean-IoU,
    coerenza velocità/CV + std circolare heading, ambiguità residua). Campo
    SEPARATO da organization_score e score_confidence (scoring rimane Fase 1).
    La motion object-level è la scala significativa nei cluster densi (dove la
    singola cella ha score_confidence=low o cell density alta).

Parte D — tracking_ambiguity (DIAGNOSTICA, qualità dell'identificazione):
    competing candidates + cell density locale + merge/split candidati +
    assignment cost separation. Mai usata come firma meteorologica.
"""

import numpy as np
from scipy.optimize import linear_sum_assignment

from . import models
from .tracking import haversine_km, bearing_deg, dest_from, _circle_iou

_EPS = 1e-12

# Fase 1.9 — cinematica: FONTE UNICA in kinematics.py (estrazione 1.2.4.1).
# I simboli sono ri-esportati qui per retro-compatibilità dell'API storica
# (storm_tracking.segment_velocities_kmh, .assess_motion, .MOTION_STATUS_*, ...).
from .kinematics import (  # noqa: F401
    _AMB_CODE, _AMB_TEXT,
    MOTION_STATUS_VALID, MOTION_STATUS_REJECTED,
    MOTION_STATUS_AMBIGUOUS_GEOMETRY, MOTION_STATUS_NOT_ESTIMABLE,
    REASON_SPEED_GATE, REASON_INSUFFICIENT_OBS,
    segment_deltas, segment_velocities_kmh, robust_track_velocity,
    ambiguous_segments, assess_motion, apply_ambiguity_penalty,
    motion_confidence,
)


def storm_cost(prev, obj, cfg):
    """Cost normalizzata (0..1 per termine) per l'Hungarian su storm objects."""
    wd = float(cfg["w_distance"])
    wp = float(cfg.get("w_prediction", 0.0))
    wiou = float(cfg["w_iou"])
    wa = float(cfg["w_area"])
    wi = float(cfg["w_intensity"])
    wc = float(cfg.get("w_cells", 0.0))

    d = haversine_km(prev.lonlat, obj.centroid_lonlat)
    dist_ref = float(cfg.get("distance_ref_km", 100.0))
    nd = min(1.0, d / max(dist_ref, _EPS))

    pred_term = 0.0
    if wp > 0 and prev.predicted_lonlat is not None:
        ref_p = float(cfg.get("prediction_ref_km", 50.0))
        pe = haversine_km(prev.predicted_lonlat, obj.centroid_lonlat)
        pred_term = min(1.0, pe / max(ref_p, _EPS))

    iou = _circle_iou(prev.lonlat, prev.area_km2,
                      obj.centroid_lonlat, obj.area_km2)
    area_norm = abs(prev.area_km2 - obj.area_km2) / \
        max(prev.area_km2, obj.area_km2, _EPS)
    # default dalla config base (fonte unica), non un magic number
    from . import config as _config
    i_ref = float(cfg.get(
        "intensity_ref_dbz",
        _config.CONFIG["storm"]["tracking"]["intensity_ref_dbz"]))
    int_norm = min(1.0, abs(prev.max_dbz - obj.max_dbz) / max(i_ref, _EPS))
    c_ref = float(cfg.get("cell_count_ref", 10.0))
    cell_norm = min(1.0, abs(prev.cell_count - obj.cell_count) / max(c_ref, _EPS))

    return (wd * nd + wp * pred_term + wiou * (1.0 - iou)
            + wa * area_norm + wi * int_norm + wc * cell_norm)


# ---------------------------------------------------------------------------
# Parte D — tracking_ambiguity (metrica diagnostica)
# ---------------------------------------------------------------------------
def ambiguity_class(competing, density, cost_sep, merge_split, cfg_amb):
    """Classifica la qualità dell'identificazione per uno storm object.

    competing  : n track attive con costo finito per questo oggetto
    density    : n celle entro density_radius_km (cell_density aggregata)
    cost_sep    : separation ratio (second_best - best)/max(second_best, best)
                  in (0,1]; 1 = best chiaramente distinto.
    merge_split : True se l'oggetto è merge/split candidato (<=25 km, co-loc.)
    Ritorna (class, reason dict)."""
    score = 0
    reason = {"competing": int(competing), "cell_density": int(density),
              "cost_separation": round(cost_sep, 3), "merge_split":
              bool(merge_split)}
    if int(competing) >= int(cfg_amb.get("competing_high", 3)):
        score += 2
    elif int(competing) >= 2:
        score += 1
    dhi = int(cfg_amb.get("density_high", 10))
    dmed = int(cfg_amb.get("density_medium", 5))
    if density >= dhi:
        score += 2
    elif density >= dmed:
        score += 1
    if cost_sep < float(cfg_amb.get("cost_sep_high", 0.12)):
        score += 2
    elif cost_sep < float(cfg_amb.get("cost_sep_medium", 0.30)):
        score += 1
    if merge_split:
        score += 2
    klass = "high" if score >= 4 else ("medium" if score >= 2 else "low")
    reason["class"] = klass
    reason["score"] = score
    return klass, reason


class StormObjectTracker:
    """Tracker indipendente su storm object (Hungarian per frame)."""

    def __init__(self, cfg_storm, cfg_scoring=None, diagnostics=None):
        self.cfg = cfg_storm
        self.tracking_cfg = cfg_storm["tracking"]
        self.amb_cfg = cfg_storm["ambiguity"]
        self.motion_cfg = cfg_storm.get("motion", {})
        self.cfg_scoring = cfg_scoring
        self.tracks = []
        self._next_track_id = 1
        self.diag = []
        self._diagnostics = bool(
            diagnostics if diagnostics is not None
            else self.tracking_cfg.get("diagnostics", False))

    # ------------------------------------------------------------------
    # matching helpers
    # ------------------------------------------------------------------
    def _features(self, track):
        pts = track.points
        last = pts[-1]
        predicted = None
        if len(pts) >= 2:
            a, b = pts[-2], pts[-1]
            dt_h = (b.timestamp_ms - a.timestamp_ms) / 3600000.0
            if dt_h > 0:
                d = haversine_km(a.lonlat, b.lonlat)
                speed = d / dt_h
                if d > 1e-9:
                    predicted = {"speed_kmh": speed,
                                 "bearing": bearing_deg(a.lonlat, b.lonlat),
                                 "from_ts_ms": b.timestamp_ms,
                                 "from_lonlat": b.lonlat}
        return {
            "last_lonlat": last.lonlat,
            "last_ts_ms": last.timestamp_ms,
            "area": last.area_km2,
            "max_dbz": last.max_dbz,
            "cell_count": last.cell_count,
            "predicted": predicted,
        }

    def _target_prediction(self, feats, obj_ts_ms):
        pr = feats.get("predicted")
        if not pr:
            return None
        dt_h = (obj_ts_ms - pr["from_ts_ms"]) / 3600000.0
        if dt_h <= 0:
            return None
        return dest_from(pr["from_lonlat"], pr["bearing"],
                         pr["speed_kmh"] * dt_h)

    def _new_track(self, obj, frame_index):
        track = models.StormTrack(self._next_track_id, obj.timestamp_ms)
        self._next_track_id += 1
        pt = models.StormTrackPoint(track.track_id, frame_index, obj)
        track.points.append(pt)
        track.events.append(models.EVENT_BIRTH)
        obj.track_id = track.track_id
        # ambiguità di nascita: solo cell density locale (nessun competitor)
        klass, reason = ambiguity_class(0, obj.cell_density, 1.0, False,
                                        self.amb_cfg)
        obj.tracking_ambiguity = klass
        obj.ambiguity_reason = reason
        pt.ambiguity = klass
        return track

    def _gate_limits(self, feats, obj):
        max_speed = self.tracking_cfg.get("max_storm_speed_kmh")
        if not max_speed:
            return None, None
        dt_h = max((obj.timestamp_ms - feats["last_ts_ms"]) / 3600000.0, _EPS)
        step_gate = float(max_speed) * dt_h
        pred_gate = self.tracking_cfg.get("prediction_gate_km")
        pred_gate = float(pred_gate) if pred_gate else step_gate
        return step_gate, pred_gate

    # ------------------------------------------------------------------
    def update(self, objs, frame_index):
        active = [t for t in self.tracks if t.status == models.STATUS_ACTIVE]
        if not active or not objs:
            rows = []
            for obj in objs:
                track = self._new_track(obj, frame_index)
                self.tracks.append(track)
                rows.append({"track_id": track.track_id,
                             "prev_storm_object_id": None,
                             "current_storm_object_id": obj.storm_object_id,
                             "status": "new_track"})
            max_coast = int(self.tracking_cfg.get("max_coast_frames", 0) or 0)
            for t in active:
                if not objs and max_coast > 0 and \
                        getattr(t, "_coast_frames", 0) < max_coast:
                    t._coast_frames = getattr(t, "_coast_frames", 0) + 1
                    rows.append({"track_id": t.track_id,
                                 "prev_storm_object_id": t.points[-1].storm_object_id,
                                 "current_storm_object_id": None,
                                 "status": "coasting"})
                    continue
                t.status = models.STATUS_DEAD
                t.death_ms = t.points[-1].timestamp_ms
                t.events.append(models.EVENT_DEATH)
                rows.append({"track_id": t.track_id,
                             "prev_storm_object_id": t.points[-1].storm_object_id,
                             "current_storm_object_id": None,
                             "status": "terminated"})
            self._record(frame_index, rows)
            return

        feats_list = [self._features(t) for t in active]
        n_r, n_c = len(active), len(objs)
        cost = np.full((n_r, n_c), np.inf, dtype=float)
        meta = {}
        for i, (t, feats) in enumerate(zip(active, feats_list)):
            for j, obj in enumerate(objs):
                sg, pg = self._gate_limits(feats, obj)
                d = haversine_km(feats["last_lonlat"], obj.centroid_lonlat)
                if sg is not None and d > sg:
                    continue
                pred_target = self._target_prediction(feats, obj.timestamp_ms)
                if (pg is not None and pred_target is not None
                        and haversine_km(pred_target, obj.centroid_lonlat) > pg):
                    continue
                prev = _PtAdapter(
                    lonlat=feats["last_lonlat"],
                    area=feats["area"], max_dbz=feats["max_dbz"],
                    cell_count=feats["cell_count"],
                    predicted_lonlat=pred_target)
                c = storm_cost(prev, obj, self.tracking_cfg)
                if c <= float(self.tracking_cfg["max_assignment_cost"]):
                    cost[i, j] = c
                meta[(i, j)] = {
                    "distance_km": round(d, 3),
                    "iou": round(_circle_iou(feats["last_lonlat"], feats["area"],
                                             obj.centroid_lonlat, obj.area_km2), 4),
                    "area_ratio": round(obj.area_km2 / max(feats["area"], _EPS), 3),
                    "intensity_difference": round(abs(feats["max_dbz"] - obj.max_dbz), 2),
                    "cell_count_diff": abs(feats["cell_count"] - obj.cell_count),
                    "prediction_error_km": round(
                        haversine_km(pred_target, obj.centroid_lonlat), 3)
                        if pred_target is not None else None,
                    "gates": (sg, pg),
                }

        max_cost = float(self.tracking_cfg["max_assignment_cost"])
        # merge/split co-locazione (<=25 km) come nel layer cella
        near_row = np.zeros(n_r, dtype=int)
        near_col = np.zeros(n_c, dtype=int)
        for (i, j), m in meta.items():
            if m["distance_km"] <= 25.0 and np.isfinite(cost[i, j]):
                near_row[i] += 1
                near_col[j] += 1
        amb_rows = set(np.where(near_row > 1)[0].tolist())
        amb_cols = set(np.where(near_col > 1)[0].tolist())

        assignment, reversed_assign = {}, {}
        if cost.size:
            fin = np.isfinite(cost)
            if fin.any():
                big = max_cost * max(cost.shape) + 1.0
                work = np.where(fin, cost, big)
                r_idx, c_idx = linear_sum_assignment(work)
                for ti, ci in zip(r_idx, c_idx):
                    if np.isfinite(cost[ti, ci]) and cost[ti, ci] <= max_cost:
                        assignment[ti] = ci
                        reversed_assign[ci] = ti

        # -- matched: cost separation + ambiguity (Parte D) ---------------- *
        for ti, ci in assignment.items():
            obj = objs[ci]
            track = active[ti]
            prev_last = track.points[-1]
            pt = models.StormTrackPoint(track.track_id, frame_index, obj)
            d = haversine_km(prev_last.lonlat, obj.centroid_lonlat)
            iou = _circle_iou(prev_last.lonlat, prev_last.area_km2,
                              obj.centroid_lonlat, obj.area_km2)
            dt_h = max((obj.timestamp_ms - prev_last.timestamp_ms) / 3600000.0, _EPS)
            pt.iou_to_prev = iou
            pt.speed_to_prev_kmh = d / dt_h
            pt.bearing_to_prev_deg = bearing_deg(prev_last.lonlat, obj.centroid_lonlat)
            track.points.append(pt)
            track._coast_frames = 0
            if ti in amb_rows or ci in amb_cols:
                track.events.append(models.EVENT_AMBIGUOUS)
            obj.track_id = track.track_id

            # cost separation per colonna (best vs second-best)
            col = cost[:, ci]
            vals = np.sort(col[np.isfinite(col)])
            if len(vals) >= 2:
                sep = (vals[1] - vals[0]) / max(vals[1], _EPS)
            else:
                sep = 1.0
            competing = int(np.isfinite(col).sum())
            merge_split = (ti in amb_rows) or (ci in amb_cols)
            klass, reason = ambiguity_class(
                competing, obj.cell_density, sep, merge_split, self.amb_cfg)
            obj.tracking_ambiguity = klass
            obj.ambiguity_reason = reason
            pt.ambiguity = klass

        # -- diag rows -------------------------------------------------------
        rows = []
        for ti, t in enumerate(active):
            for ci, obj in enumerate(objs):
                m = meta.get((ti, ci))
                if m is None:
                    if self._diagnostics:
                        rows.append({"track_id": t.track_id,
                                     "prev_storm_object_id": t.points[-1].storm_object_id,
                                     "current_storm_object_id": obj.storm_object_id,
                                     "status": "rejected_cost",
                                     "distance_km": round(haversine_km(
                                         t.points[-1].lonlat, obj.centroid_lonlat), 3)})
                    continue
                is_matched = ti in assignment and assignment[ti] == ci
                status = "matched" if is_matched else "rejected_cost"
                if is_matched and ci in amb_cols:
                    status = "ambiguous_merge_candidate"
                if is_matched and ti in amb_rows:
                    status = "ambiguous_split_candidate"
                if self._diagnostics:
                    rows.append({"track_id": t.track_id,
                                 "prev_storm_object_id": t.points[-1].storm_object_id,
                                 "current_storm_object_id": obj.storm_object_id,
                                 "status": status,
                                 "total_cost": round(cost[ti, ci], 4)
                                 if np.isfinite(cost[ti, ci]) else None,
                                 **m})

        for j, obj in enumerate(objs):
            if j not in reversed_assign:
                track = self._new_track(obj, frame_index)
                self.tracks.append(track)
                rows.append({"track_id": track.track_id,
                             "prev_storm_object_id": None,
                             "current_storm_object_id": obj.storm_object_id,
                             "status": "new_track"})
        for ti, t in enumerate(active):
            if ti not in assignment:
                t.status = models.STATUS_DEAD
                t.death_ms = t.points[-1].timestamp_ms
                t.events.append(models.EVENT_DEATH)
                rows.append({"track_id": t.track_id,
                             "prev_storm_object_id": t.points[-1].storm_object_id,
                             "current_storm_object_id": None,
                             "status": "terminated"})
        self._record(frame_index, rows)

    def _record(self, frame_index, rows):
        if self._diagnostics:
            self.diag.append({"frame": frame_index, "matches": rows})

    def complete_cycles(self):
        for track in self.tracks:
            if track.status == models.STATUS_ACTIVE:
                track.status = models.STATUS_DEAD
                track.death_ms = track.points[-1].timestamp_ms
        return self.tracks

    # ------------------------------------------------------------------
    # Parte C — motion + motion_confidence a livello storm object
    # ------------------------------------------------------------------
    def _motion_confidence(self, track):
        # delega alla FONTE UNICA (kinematics.py)
        return motion_confidence(track, self.motion_cfg, self.tracking_cfg)

    def finalize(self, cfg_scoring=None):
        from . import scoring
        full_frames = int(self.tracking_cfg.get("full_confidence_frames", 5))
        scfg = cfg_scoring or self.cfg_scoring or scoring._fallback_scoring()
        for track in self.tracks:
            pts = track.points
            n = len(pts)
            if n < 2:
                track.tracking_confidence = "insufficient"
                track.score_confidence = "low"
                track.motion_confidence = "low"
                continue
            track.tracking_confidence = ("full" if n >= full_frames else "low")
            track.score_confidence = (
                "high" if n >= full_frames and max(p.max_dbz for p in pts) >= 35.0
                else ("medium" if n >= 3 else "low"))
            track.motion_confidence = self._motion_confidence(track)
            amb_codes = [_AMB_CODE.get(p.ambiguity, 1) for p in pts
                         if p.ambiguity is not None]
            track.tracking_ambiguity = _AMB_TEXT.get(max(amb_codes), "high") \
                if amb_codes else "high"

            t0, t1 = pts[0].timestamp_ms, pts[-1].timestamp_ms
            duration_min = (t1 - t0) / 60000.0
            if duration_min <= 0:
                track.motion = {
                    "velocity_kmh": None,
                    "raw_velocity_kmh": None,
                    "net_velocity_kmh": None,
                    "median_segment_velocity_kmh": None,
                    "velocity_valid": False,
                    "motion_status": MOTION_STATUS_NOT_ESTIMABLE,
                    "motion_rejection_reason": REASON_INSUFFICIENT_OBS,
                    "rejected_segments": 0,
                    "total_segments": max(n - 1, 0),
                    "ambiguous_segments": 0,
                    "duration_min": round(duration_min, 1),
                }
                continue
            kin = robust_track_velocity(pts)
            # GATE DI VALIDAZIONE (Fase 1.9, Parte B): legge da storm.motion,
            # disaccoppiato dal matching. default None (calibrazione).
            gate = self.motion_cfg.get("max_validated_velocity_kmh")
            gate = float(gate) if gate else None
            amb_seg = ambiguous_segments(pts)
            assess = assess_motion(kin, gate, amb_seg)
            # Parte E: penalty motion_confidence se merge/split coinvolti
            penalty, penalized = apply_ambiguity_penalty(
                track.motion_confidence, amb_seg)
            track.motion_confidence = penalty
            toward = bearing_deg(pts[0].lonlat, pts[-1].lonlat)
            from_deg = (toward + 180.0) % 360.0
            growth = (pts[-1].area_km2 - pts[0].area_km2) / pts[0].area_km2 * 100.0 \
                if pts[0].area_km2 > 0 else 0.0
            idelta = pts[-1].max_dbz - pts[0].max_dbz
            vel = assess["velocity_kmh"]
            track.motion = {
                "distance_km": round(sum(d for d, _ in segment_deltas(pts)), 2),
                "velocity_kmh": round(vel, 1) if vel is not None else None,
                "raw_velocity_kmh": round(kin["raw_velocity_kmh"], 1)
                if kin["raw_velocity_kmh"] is not None else None,
                "net_velocity_kmh": round(kin["net_velocity_kmh"], 1)
                if kin["net_velocity_kmh"] is not None else None,
                "median_segment_velocity_kmh":
                    round(kin["median_segment_velocity_kmh"], 1)
                    if kin["median_segment_velocity_kmh"] is not None else None,
                "velocity_valid": bool(assess["velocity_valid"]),
                "motion_status": assess["motion_status"],
                "motion_rejection_reason": assess["motion_rejection_reason"],
                "rejected_segments": int(assess["rejected_segments"]),
                "total_segments": int(kin["n_segments"]),
                "ambiguous_segments": len(amb_seg),
                "motion_confidence_ambiguity_penalty": penalized,
                "direction_toward_deg": round(toward, 1),
                "direction_from_deg": round(from_deg, 1),
                "duration_min": round(duration_min, 1),
                "area_growth_pct": round(growth, 1),
                "intensity_delta_dbz": round(idelta, 2),
                "mean_cell_count": round(sum(p.cell_count for p in pts) / n, 2),
                "max_cell_count": int(max(p.cell_count for p in pts)),
                "mean_iou_km": round(sum(p.iou_to_prev for p in pts[1:]
                                         if p.iou_to_prev is not None) / max(n - 1, 1), 4),
                "_avg_solidity": round(sum(p.solidity for p in pts) / n, 4),
                "_avg_compactness": round(sum(p.compactness for p in pts) / n, 4),
            }
            score, label, probs, _c = scoring.organization_score(track, scfg)
            track.motion["organization_score"] = score
            track.motion["classification"] = label
            track.motion["class_probs"] = probs
            # reflection PUNTO -> oggetto (per storm_objects.geojson)
            for p in pts:
                if p._obj is not None:
                    p._obj.motion_speed_kmh = round(vel, 2) \
                        if vel is not None else None
                    p._obj.raw_motion_speed_kmh = round(kin["raw_velocity_kmh"], 2) \
                        if kin["raw_velocity_kmh"] is not None else None
                    p._obj.motion_direction = round(toward, 1)
                    p._obj.motion_confidence = track.motion_confidence
                    p._obj.velocity_valid = bool(assess["velocity_valid"])
                    p._obj.motion_status = assess["motion_status"]
                    p._obj.motion_rejection_reason = assess["motion_rejection_reason"]
                    p._obj.motion_ambiguity_penalty = penalized
                    p._obj.organization_score = score
                    p._obj.score_confidence = track.score_confidence
        return self.tracks


class _PtAdapter:
    """Vista minima prev-like per storm_cost (ultimo punto di una track attiva)."""
    def __init__(self, lonlat, area, max_dbz, cell_count, predicted_lonlat):
        self.lonlat = lonlat
        self.area_km2 = area
        self.max_dbz = max_dbz
        self.cell_count = cell_count
        self.predicted_lonlat = predicted_lonlat