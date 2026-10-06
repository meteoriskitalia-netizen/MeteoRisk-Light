#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — aggregation.py (Fase 1.7, Parte A)

Layer multiscala CELL -> STORM OBJECT. L'aggregazione utile alla identità
principale del sistema; le celle originali NON vengono modificate (la maschera
pixel non è conservata in DetectedCell per design: geometry = dischi da
centroide + area, coerente con tracking._circle_iou).

Metodi di clustering radice (tutti confrontati, nessuna scelta automatica):
    distance_cc        - componenti connesse: celle con distanza min(centroids)
                         <= soglia (km) nello stesso storm object. O(1) linkage.
    dbscan             - DBSCAN di GOVERNO RIFERIMENTO sui centroids (eps_km,
                         min_cells). Implementazione indipendente (nessuna
                         dipendenza sklearn): regione-query sulla matrice delle
                         distanze precomputata.
    dilation_overlap   - (DEFAULT sperimentale) unione se il GAP tra i cerchi
                         (d - r1 - r2) <= merge_gap_km OPPURE i cerchi dilatati
                         di dilate_km si sovrappongono. Rispetta la fisica della
                         geometria (celle vicine con area -> Le celle contigue
                         senza gap si uniscono anche se distanza centroide alta).

Limiti (documentati): la geometry è un'approssimazione circolare; per celle
fortemente eccentriche il gap reale può differire; l'unione per overlap assegna
le celle a PIÙ storm object (uno solo quando i cluster si toccano ai bordi).

Output principale:
    aggregate_frame(cells, frame_index, cfg)   -> list[StormObject]
    compare_aggregation_methods(cells_by_frame, cfg) -> summary dict
"""

import math
import time

from . import models
from .tracking import haversine_km

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Geometria approssimata (dischi da area)
# ---------------------------------------------------------------------------
def cell_radius_km(area_km2):
    """Raggio del cerchio equivalente (area -> disco)."""
    return math.sqrt(max(float(area_km2), _EPS) / math.pi)


def circle_gap_km(lonlat1, area1, lonlat2, area2):
    """Gap tra due dischi: max(0, d - r1 - r2). 0 = overlap/touch."""
    d = haversine_km(lonlat1, lonlat2)
    return max(0.0, d - cell_radius_km(area1) - cell_radius_km(area2))


def dilated_overlap(lonlat1, area1, lonlat2, area2, dilate_km):
    """True se i cerchi a+b dilatazione si sovrappongono."""
    d = haversine_km(lonlat1, lonlat2)
    r1 = cell_radius_km(area1) + float(dilate_km)
    r2 = cell_radius_km(area2) + float(dilate_km)
    return d <= r1 + r2


def cell_bbox_lonlat(cell):
    """bbox (lon_min, lat_min, lon_max, lat_max) del disco approssimato."""
    lon, lat = cell.centroid_lonlat
    r = cell_radius_km(cell.area_km2)
    dlat = r / 111.32
    dlon = r / max(111.32 * math.cos(math.radians(lat)), _EPS)
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


# ---------------------------------------------------------------------------
# Clustering di riferimento: union-find + DBSCAN indipendente
# ---------------------------------------------------------------------------
class _DSU:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _clusters_from_dsu(dsu, n):
    groups = {}
    for i in range(n):
        groups.setdefault(dsu.find(i), []).append(i)
    return [group for group in groups.values()]


def distance_cc_clusters(cells, threshold_km):
    """Componenti connesse su distanza centroide <= threshold_km."""
    n = len(cells)
    if n == 0:
        return []
    if n == 1:
        return [[0]]
    dsu = _DSU(n)
    for i in range(n):
        for j in range(i + 1, n):
            if haversine_km(cells[i].centroid_lonlat,
                            cells[j].centroid_lonlat) <= threshold_km:
                dsu.union(i, j)
    return _clusters_from_dsu(dsu, n)


def dilation_overlap_clusters(cells, merge_gap_km, dilate_km):
    """Unione se gap-cerchio <= merge_gap_km OPPURE dischi dilatati si toccano."""
    n = len(cells)
    if n == 0:
        return []
    if n == 1:
        return [[0]]
    dsu = _DSU(n)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = cells[i], cells[j]
            if circle_gap_km(a.centroid_lonlat, a.area_km2,
                             b.centroid_lonlat, b.area_km2) <= merge_gap_km:
                dsu.union(i, j)
                continue
            if dilated_overlap(a.centroid_lonlat, a.area_km2,
                               b.centroid_lonlat, b.area_km2, dilate_km):
                dsu.union(i, j)
    return _clusters_from_dsu(dsu, n)


def dbscan_clusters(cells, eps_km, min_cells):
    """DBSCAN di riferimento sui centroids (matrice di distanze precomputata).

    Implementazione O(n^2) volutamente semplice (celle per frame: decine),
    senza sklearn. 'min_cells' è min_points; celle non-core/non-riceggiano
    cluster = rumore (escluse). Identico a DBSCAN standard."""
    n = len(cells)
    if n == 0:
        return []
    dist = [[haversine_km(a.centroid_lonlat, b.centroid_lonlat)
             for b in cells] for a in cells]

    def region_query(p):
        return [q for q in range(n) if q != p and dist[p][q] <= eps_km]

    neighbors = [region_query(p) for p in range(n)]
    core = [p for p in range(n) if len(neighbors[p]) + 1 >= min_cells]
    core_set = set(core)
    label = [-1] * n              # -1 = rumore
    cluster_id = 0
    for p in core:
        if label[p] != -1:
            continue
        stack = [p]
        label[p] = cluster_id
        while stack:
            q = stack.pop()
            if q in core_set:
                for r in neighbors[q]:
                    if label[r] == -1:
                        label[r] = cluster_id
                        stack.append(r)
        cluster_id += 1
    groups = {}
    for p in range(n):
        if label[p] >= 0:
            groups.setdefault(label[p], []).append(p)
    return [g for g in groups.values()]


def cluster_cells(cells, cfg_agg):
    """Dispatcher sul metodo configurato. Ritorna liste di indici."""
    method = str(cfg_agg.get("method", "dilation_overlap"))
    if method == "distance_cc":
        return distance_cc_clusters(cells, float(cfg_agg["dbscan_eps_km"]))
    if method == "dbscan":
        return dbscan_clusters(cells, float(cfg_agg["dbscan_eps_km"]),
                               int(cfg_agg["dbscan_min_cells"]))
    return dilation_overlap_clusters(cells, float(cfg_agg.get("merge_gap_km", 5.0)),
                                     float(cfg_agg.get("dilate_km", 8.0)))


# ---------------------------------------------------------------------------
# Convex hull (monotone chain) sui centroids
# ---------------------------------------------------------------------------
def convex_hull(points):
    """Scafo convesso (monotone chain) di punti (lon, lat). Ritorna lista
    ordinata antioraria senza replica del primo punto (o <=2 punti grezzi)."""
    pts = sorted(set((float(p[0]), float(p[1])) for p in points))
    if len(pts) <= 2:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


# ---------------------------------------------------------------------------
# StormObject builder
# ---------------------------------------------------------------------------
def _area_weighted_mean(cells, attr):
    tot = sum(c.area_km2 for c in cells if getattr(c, attr, None) is not None)
    tot_w = sum(c.area_km2 * getattr(c, attr, 0.0)
                for c in cells if getattr(c, attr, None) is not None)
    return tot_w / tot if tot > 0 else None


def build_storm_object(cells, frame_index, storm_object_id,
                       cell_density, hull=True):
    """Costruisce lo StormObject aggregato dal gruppo di celle."""
    ts_ms = cells[0].timestamp_ms
    ts_iso = cells[0].timestamp_iso
    area = sum(c.area_km2 for c in cells)
    centroid_lon = sum(c.area_km2 * c.centroid_lonlat[0] for c in cells) / area
    centroid_lat = sum(c.area_km2 * c.centroid_lonlat[1] for c in cells) / area
    bb = [cell_bbox_lonlat(c) for c in cells]
    bbox = (min(b[0] for b in bb), min(b[1] for b in bb),
            max(b[2] for b in bb), max(b[3] for b in bb))
    hull_pts = convex_hull([c.centroid_lonlat for c in cells]) if hull else []
    max_dbz = max(c.max_dbz for c in cells)
    mean_dbz = _area_weighted_mean(cells, "mean_dbz")
    p90 = _area_weighted_mean(cells, "p90_dbz")
    solidity = sum(c.solidity for c in cells) / len(cells)
    compactness = sum(c.compactness for c in cells) / len(cells)
    return models.StormObject(
        storm_object_id=storm_object_id,
        timestamp_ms=ts_ms, timestamp_iso=ts_iso, frame_index=frame_index,
        cells=cells, centroid_lonlat=(centroid_lon, centroid_lat),
        area_km2=area, equiv_radius_km=math.sqrt(area / math.pi),
        bbox_lonlat=bbox, convex_hull_lonlat=hull_pts,
        max_dbz=float(max_dbz), mean_dbz=float(mean_dbz or 0.0),
        p90_dbz=float(p90 or 0.0), cell_density=cell_density,
        solidity=float(solidity), compactness=float(compactness),
    )


def _cell_density(cells, centroid, radius_km):
    return sum(1 for c in cells
               if haversine_km(centroid, c.centroid_lonlat) <= radius_km)


def aggregate_frame(cells, frame_index, cfg_storm, obj_id_prefix="SO"):
    """Entry point Fase 1.7: celle di un frame -> list[StormObject].

    cfg_storm: blocco CONFIG['storm'] (usato solo aggregation + density radius).
    Le celle senza gruppo (DBSCAN noise, min_cells) NON diventano storm object:
    restano a livello cella (layer 1 NON modificato)."""
    agg_cfg = cfg_storm["aggregation"]
    clusters = cluster_cells(cells, agg_cfg)
    density_radius = float(agg_cfg.get("density_radius_km", 25.0))
    min_cells = int(agg_cfg.get("min_cells", 1))
    objs = []
    for ci, group in enumerate(clusters):
        group_cells = [cells[i] for i in group]
        if len(group_cells) < min_cells:
            continue
        centroid = build_storm_object(
            group_cells, frame_index, None, 0, hull=agg_cfg.get("hull", True)
        ).centroid_lonlat
        dens = _cell_density(cells, centroid, density_radius)
        obj = build_storm_object(
            group_cells, frame_index,
            f"{obj_id_prefix}-{frame_index:03d}-{ci:02d}",
            dens, hull=agg_cfg.get("hull", True))
        objs.append(obj)
    return objs


# ---------------------------------------------------------------------------
# Confronto metodi (Parte A: nessuna scelta automatica — documento e raccomando)
# ---------------------------------------------------------------------------
def _pairwise_coassign_counts(clusters):
    """Coppie di celle co-assegnate allo stesso cluster (within-frame)."""
    return sum(len(g) * (len(g) - 1) // 2 for g in clusters)


def _rand_agreement(cells_list):
    """Persistenza temporale: frazione di coppie con co-assegnazione uguale
    tra frame consecutivi (proxy di stabilità del metodo)."""
    agrees = total = 0
    for prev_objs, curr_objs in zip(cells_list, cells_list[1:]):
        prev_ids = {id(c): i for i, objs in enumerate(prev_objs) for c in objs}
        curr_ids = {id(c): i for i, objs in enumerate(curr_objs) for c in objs}
        shared = [c for c in prev_ids if c in curr_ids]
        for a in range(len(shared)):
            for b in range(a + 1, len(shared)):
                same = (prev_ids[shared[a]] == prev_ids[shared[b]]) == \
                       (curr_ids[shared[a]] == curr_ids[shared[b]])
                agrees += int(same)
                total += 1
    return agrees / total if total else 1.0


def compare_aggregation_methods(cells_by_frame, cfg_storm):
    """Confronta distance_cc / dbscan / dilation_overlap sui frame forniti.

    Ritorna dict per data/validation/aggregation_comparison.json: per metodo
    object count/coverage, stabilità temporale, runtime, vantaggi/limiti/
    parametri di sensibilità. La scelta del default NON è automatica."""
    agg = cfg_storm["aggregation"]
    methods = [
        ("distance_cc", {"method": "distance_cc",
                         "dbscan_eps_km": agg["dbscan_eps_km"]}),
        ("dbscan", {"method": "dbscan",
                    "dbscan_eps_km": agg["dbscan_eps_km"],
                    "dbscan_min_cells": agg["dbscan_min_cells"]}),
        ("dilation_overlap", {"method": "dilation_overlap",
                              "merge_gap_km": agg["merge_gap_km"],
                              "dilate_km": agg["dilate_km"]}),
    ]
    results = {}
    for name, mc in methods:
        t0 = time.perf_counter()
        per_frame = []
        total_objs = 0
        total_cells = 0
        covered = 0
        for frame_cells in cells_by_frame:
            clusters = cluster_cells(frame_cells, mc)
            per_frame.append((frame_cells, clusters))
            total_cells += len(frame_cells)
            covered += sum(len(cl) for cl in clusters)
            total_objs += len(clusters)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        npf = [len(cl) for _, cl in per_frame]
        stability = _rand_agreement(
            [[[frame_cells[i] for i in cluster] for cluster in clusters_]
             for frame_cells, clusters_ in per_frame])
        within = sum(_pairwise_coassign_counts(cl) for _, cl in per_frame)
        possible = sum(len(fc) * (len(fc) - 1) // 2 for fc, _ in per_frame)
        cocl_coeff = within / possible if possible else 0.0
        results[name] = {
            "object_counts_by_frame": npf,
            "total_objects_all_frames": total_objs,
            "cells_covered_pct": round(100.0 * covered / total_cells, 2)
            if total_cells else 0.0,
            "mean_objects_per_frame": round(sum(npf) / len(npf), 2) if npf else 0.0,
            "coclustering_coeff": round(cocl_coeff, 4),
            "temporal_stability_rand": round(stability, 4),
            "runtime_ms": round(elapsed_ms, 2),
            "params": mc,
        }

    base = dict(agg)
    base.pop("method", None)
    return {
        "methods": results,
        "default_method": agg["method"],
        "frames": len(cells_by_frame),
        "note": ("confronto DIAGNOSTICO; la scelta del default resta "
                 "configurabile e NON è automatica (vedi report Fase 1.7)"),
        "documentation": {
            "distance_cc": {
                "pro": "semplice, deterministico, un solo parametro; buono per "
                       "oggetti ben separati.",
                "lim": "valuta SOLO i centroidi; celle grandi vicine con "
                       "centroidi lontani non si uniscono; sensibile all'eps.",
                "sensitivity": f"eps_km in [{float(agg['dbscan_eps_km'])/2:.1f}, "
                               f"{float(agg['dbscan_eps_km'])*2:.1f}] cambia "
                               f"fortemente il numero di oggetti (see results).",
                "cost": "O(n^2) distanze, no runtime significativo (decine di celle)."},
            "dbscan": {
                "pro": "gestisce il rumore (celle isolate escluse) e la densità "
                       "variabile con eps/min_cells regolabili.",
                "lim": "soglie di densità; cluster a catena dipendono da eps; "
                       "senza sklearn (implementazione interna di riferimento, "
                       "matrice O(n^2)); il rumore NON diventa storm object.",
                "sensitivity": "eps_km e min_cells da calibrare per intensità/"
                       "morfologia della convezione.",
                "cost": "O(n^2) memoria distanza; costante le osservazioni per "
                       "frame (10-50 celle)."},
            "dilation_overlap": {
                "pro": "usa la GEOMETRIA (cerchi reali da area): unisce celle "
                       "adiacenti anche se i centroidi distano poco; il default "
                       "merge_gap/dilate unisce in modo ragionevolmente fisico.",
                "lim": "solo approssimazione circolare (mask non conservata); "
                       "due parametri interagiscono; celle a STRISCE dilatate "
                       "possono fondere oggetti separati.",
                "sensitivity": "merge_gap_km e dilate_km agiscono insieme; "
                       "con dilate_km=0 diventa gap-only.",
                "cost": "O(n^2) come gli altri; nessuna dipendenza esterna."},
        },
    }