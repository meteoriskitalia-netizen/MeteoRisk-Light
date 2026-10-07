#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — hook.py (HOOK ECHO / MORFOLOGIA, EXPERIMENTAL)

Detection dell'uncino (hook echo) da un campo riflettivita' sullo stesso frame
grid della ESA-live (stessa griglia/maschera del preprocess Fase 1). Il prodotto
VMI e' a singola riflettivita' (nessun Doppler): l'uncino e' rilevato come
MORFOLOGIA del core, NON come firma dinamica.

Algoritmo (funzioni PURE, determinismo, zero I/O):
  1. soglia dBZ (HOOK_DBZ_THRESHOLD, override opzionale via argomento);
  2. core = celle sopra soglia, 8-connesso con scipy.ndimage.label;
  3. bordo del core ordinato con MOORE TRACING (follow-the-wall + criterio di
     Jacob: scansione oraria dei 8 vicini ripartendo dal backtrack, chiusura
     al rientro nel pixel di partenza) -> loop chiusi deterministiche;
     le features usano il loop PIU' LUNGO (buchi interni e spike isolati non
     entrano nel conto);
  4. "BAY" = solco concavo (l'uncino avvolge una regione debole): per ogni
     punto di bordo si misura la PROFONDITA' dentro la convex hull del core;
     si prende il run circolare piu' lungo con profondita' >= soglia e si
     misura arco (px) e avvolgimento angolare rispetto al centroid del core
     (bay_arc_px, bay_wrap_deg). Un disco/ellisse convesso non ha bay
     (profondita' ~0): la somma |turn| del bordo NON e' usata come feature
     (sulle curve digitali e' gonfiata dalla scala dei pixel, empiricamente
     disco~1755 e blob rumoroso~6435: nessun valore discriminante);
  5. concavity_ratio = area/area_hull (monotone chain) su TUTTI i bordi;
  6. score fuzzy 0-100 (hook_score_from_features) + persistenza temporale
     (filter_persistence) sui punteggi della storia.

Il modulo NON importa altri moduli del package (autonomo e integrabile) e non
esegue I/O: il campo arriva gia' scaricato dal chiamante.
"""

import math

import numpy as np
from scipy import ndimage

# ---------------------------------------------------------------------------
# Costanti — EXPERIMENTAL DEFAULTS (nessuna calibrazione su casi reali)
# ---------------------------------------------------------------------------
HOOK_DBZ_THRESHOLD = 45.0          # soglia dBZ del core (config-override opz.)
HOOK_DBZ_MAX_REF = 60.0            # dBZ di riferimento per il termine intensita'
HOOK_MIN_CORE_PX = 25              # core minimo (px) per un claim di uncino
HOOK_CONNECTIVITY = np.ones((3, 3), dtype=np.uint8)  # labelling 8-connesso
HOOK_CONCAVE_DEPTH_PX = 6.0        # profondita' minima dentro l'hull = "bay"
HOOK_BAY_ARC_MIN_PX = 8.0          # arco bay minimo (px)
HOOK_BAY_ARC_REF_PX = 40.0         # arco bay di riferimento (pieno merito)
HOOK_BAY_WRAP_BASE_DEG = 60.0      # avvolgimento bay minimo utile
HOOK_BAY_WRAP_REF_DEG = 300.0      # avvolgimento bay di riferimento
HOOK_BAY_WRAP_MIN_DEG = 150.0      # gate candidate: almeno ~mezzo giro
HOOK_CONCAVITY_DEFICIT_REF = 0.25  # (1 - area/hull) di riferimento (0.75->1.0)
HOOK_SCORE_WEIGHTS = {             # somma 1.00
    "bay_wrap": 0.35,
    "concavity": 0.25,
    "bay_arc": 0.25,
    "intensity": 0.15,
}
# media pesata 2-3 sample, pesi dal piu' vecchio al piu' recente (somma 6.0)
PERSISTENCE_WEIGHTS = (1.0, 2.0, 3.0)


# ---------------------------------------------------------------------------
# Helper numerici locali (puri)
# ---------------------------------------------------------------------------
def _clamp01(x):
    return max(0.0, min(1.0, float(x)))


def _norm(x, lo, hi):
    """Normalizza x in [0,1] tra i riferimenti lo/hi."""
    return _clamp01((float(x) - float(lo)) / max(float(hi) - float(lo), 1e-9))


def _boundary(mask):
    """Bordi del maschio: celle vere con almeno un vicino sfondo (8-conn)."""
    if not mask.any():
        return np.zeros(mask.shape, dtype=bool)
    eroded = ndimage.binary_erosion(mask, structure=HOOK_CONNECTIVITY,
                                    border_value=0)
    return mask & (~eroded)


def _convex_hull(points):
    """Convex hull (Andrew monotone chain) su interi, deterministico.

    points: lista (row, col). Ritorna l'hull in ordine antiorario."""
    pts = sorted({(int(r), int(c)) for r, c in points})
    if len(pts) <= 2:
        return pts

    def _cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _poly_area(poly):
    """Area (shoelace) di un poligono px^2 (>=0)."""
    if len(poly) < 3:
        return 0.0
    acc = 0.0
    n = len(poly)
    for i in range(n):
        r0, c0 = poly[i]
        r1, c1 = poly[(i + 1) % n]
        acc += r0 * c1 - r1 * c0
    return abs(acc) / 2.0


# 8 vicini in senso orario: E, SE, S, SW, W, NW, N, NE (coordinate matrice,
# riga crescente verso il basso -> E->SE->S->... e' orario)
_OFFS = ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0),
         (-1, 1))


def _trace_loops(pts):
    """Moore tracing deterministico dei pixel di bordo -> loop chiusi.

    Per ogni loop: si parte dal pixel non visitato minimo (row, col) col
    backtrack a OVEST (garantito fuori dall'insieme per il minimo
    lessicografico), si scorrono i 8 vicini in senso orario ripartendo dal
    backtrack, il primo pixel di bordo trovato diventa corrente e il nuovo
    backtrack e' la direzione corrente->predecessore. Il loop si chiude al
    rientro nel pixel di partenza (criterio di Jacob); un loop degenere
    (catena aperta/sottile) si chiude alla guardia 4N+8 senza eccezione.

    Ritorna (loops, closed): liste parallele; closed[i] indica se loop[i] e'
    tornato al proprio inizio. Deterministico: nessuno stato, nessuna I/O,
    nessun rischio di loop infinito."""
    pset = set(pts)
    remaining = set(pset)
    loops = []
    closed_flags = []
    guard = 4 * len(pset) + 8
    while remaining:
        start = min(remaining)
        loop = [start]
        p = start
        bi = 4                       # backtrack a OVEST (indice di _OFFS)
        closed = False
        for _ in range(guard):
            found = None
            fidx = None
            for k in range(1, 9):
                idx = (bi + k) % 8
                q = (p[0] + _OFFS[idx][0], p[1] + _OFFS[idx][1])
                if q in pset:
                    found, fidx = q, idx
                    break
            if found is None:        # catena aperta (degenerata)
                break
            if found == start:       # chiusura Jacob: ritorno all'origine
                closed = True
                break
            loop.append(found)
            bi = _OFFS.index((p[0] - found[0], p[1] - found[1]))
            p = found
        loops.append(loop)
        closed_flags.append(closed)
        remaining -= set(loop)
    return loops, closed_flags


def _turn_signed(d_in, d_out):
    """Turno firmato (deg) fra due vettori direzione (coordinate matrice)."""
    n0 = math.hypot(d_in[0], d_in[1])
    n1 = math.hypot(d_out[0], d_out[1])
    if n0 <= 1e-12 or n1 <= 1e-12:
        return 0.0
    cross = d_in[0] * d_out[1] - d_in[1] * d_out[0]
    dot = d_in[0] * d_out[0] + d_in[1] * d_out[1]
    return math.degrees(math.atan2(cross, dot))


def _point_seg_dist(p, a, b):
    """Distanza (float) dal punto p al segmento a-b (plano row/col)."""
    pr, pc = p
    ar, ac = a
    br, bc = b
    dr, dc = br - ar, bc - ac
    len2 = dr * dr + dc * dc
    if len2 <= 1e-12:
        return math.hypot(pr - ar, pc - ac)
    t = max(0.0, min(1.0, ((pr - ar) * dr + (pc - ac) * dc) / len2))
    return math.hypot(pr - (ar + t * dr), pc - (ac + t * dc))


def _longest_hot_run(hot, seg_dists, ang_steps, closed):
    """Run circolare piu' lungo di punti "hot" (booleani).

    seg_dists[i] = distanza punto i -> punto i+1; ang_steps[i] = passo
    angolare (deg, intorno al centroid) fra punto i e i+1. Ritorna
    (arc_px, wrap_deg): somma dei segmenti CONNESSI del run. La finestra non
    supera mai n punti distinti (+1 per la rappresentazione del cerchio
    completo). Deterministico."""
    n = len(hot)
    if n == 0 or not any(hot):
        return 0.0, 0.0
    if closed:
        h2 = hot + hot
        d2 = list(seg_dists) + list(seg_dists)
        a2 = list(ang_steps) + list(ang_steps)
        limit = 2 * n
    else:
        h2, d2, a2 = hot, list(seg_dists), list(ang_steps)
        limit = n
    best_arc = 0.0
    best_wrap = 0.0
    left = 0
    arc = 0.0
    wrap = 0.0
    for right in range(limit):
        if not h2[right]:
            left = right + 1
            arc = 0.0
            wrap = 0.0
            continue
        if right > left:
            arc += d2[right - 1]
            wrap += a2[right - 1]
        while right - left + 1 > n + 1:
            arc -= d2[left]
            wrap -= a2[left]
            left += 1
        if left >= n:                # finestre oltre n: duplicati del passo 0
            break
        if arc > best_arc:
            best_arc = arc
            best_wrap = wrap
    return float(best_arc), float(best_wrap)


# ---------------------------------------------------------------------------
# API pure
# ---------------------------------------------------------------------------
def compute_hook_features(grid, mask_dbz, dbz_threshold=None):
    """Feature morfologiche dell'uncino su un frame grid ESA-live.

    grid:       ndarray 2D dBZ (stessa griglia del preprocess)
    mask_dbz:   ndarray 2D bool di validita' (DATA VALIDITY, non soglia meteo);
                se None si usa isfinite(grid)
    dbz_threshold: soglia dBZ del core (default HOOK_DBZ_THRESHOLD)

    Ritorna un dict deterministico con: found, n_components, dbz_max,
    core_area_px, core_perimeter_px, arc_length_px, bay_depth_px, bay_arc_px,
    bay_wrap_deg (features BAY sul loop principale = piu' lungo),
    winding_deg (turno firmato diagnostico: |val| ~ 360 SOLO su loop semplici
    chiusi; valori diversi = catena aperta o auto-intersezione. NON usato
    nello score), concavity_ratio (area/hull su tutti i bordi),
    breaks (loop distinti - 1, informativo), hook_candidate.
    Nessun core -> found=False e campi a zero (mai eccezione dati).
    ValueError solo per input malformati (errore di programmazione)."""
    g = np.asarray(grid, dtype="float64")
    if g.ndim != 2:
        raise ValueError("grid_2d_required")
    m = np.isfinite(g) if mask_dbz is None else np.asarray(mask_dbz, dtype=bool)
    if m.shape != g.shape:
        raise ValueError("shape_mismatch:mask_dbz")

    thr = float(HOOK_DBZ_THRESHOLD if dbz_threshold is None else dbz_threshold)
    empty = {
        "found": False, "n_components": 0, "dbz_max": 0.0,
        "core_area_px": 0, "core_perimeter_px": 0, "arc_length_px": 0.0,
        "bay_depth_px": 0.0, "bay_arc_px": 0.0, "bay_wrap_deg": 0.0,
        "winding_deg": 0.0, "concavity_ratio": 1.0, "breaks": 0,
        "hook_candidate": False,
    }

    core = np.isfinite(g) & m & (g >= thr)
    if not core.any():
        return empty

    labels, n_comp = ndimage.label(core, structure=HOOK_CONNECTIVITY)
    if n_comp <= 0:
        return empty
    # componente piu' grande (tie -> label minore: np.argmax e' deterministico)
    sizes = np.bincount(labels.reshape(-1))
    sizes[0] = 0
    best_label = int(np.argmax(sizes))
    comp = labels == best_label

    ys, xs = np.nonzero(comp)
    area = int(comp.sum())
    dbz_max = float(np.nanmax(g[comp]))
    bnd = _boundary(comp)
    bys, bxs = np.nonzero(bnd)
    if len(bys) == 0:
        empty.update(found=True, n_components=int(n_comp), dbz_max=dbz_max,
                     core_area_px=area)
        return empty

    pts = [(int(r), int(c)) for r, c in zip(bys, bxs)]
    loops, closed_flags = _trace_loops(pts)
    if not loops:
        empty.update(found=True, n_components=int(n_comp), dbz_max=dbz_max,
                     core_area_px=area)
        return empty
    n_breaks = len(loops) - 1          # loop distinti - 1 (informativo)
    # loop principale = piu' lungo (tie -> inizio minore: deterministico)
    li = max(range(len(loops)),
             key=lambda i: (len(loops[i]), tuple(-v for v in loops[i][0])))
    main = loops[li]
    closed = bool(closed_flags[li]) and len(main) > 2

    # --- distanze / passi angolari sul loop principale --------------------
    n = len(main)
    centroid_r = float(ys.mean())
    centroid_c = float(xs.mean())
    dists = []
    ang_steps = []
    angles = [math.atan2(p[0] - centroid_r, p[1] - centroid_c)
              for p in main]
    for i in range(1, n):
        pr, pc = main[i - 1]
        qr, qc = main[i]
        dists.append(math.hypot(qr - pr, qc - pc))
        d = angles[i] - angles[i - 1]
        while d > math.pi:
            d -= 2.0 * math.pi
        while d < -math.pi:
            d += 2.0 * math.pi
        ang_steps.append(math.degrees(abs(d)))
    if closed:
        pr, pc = main[-1]
        qr, qc = main[0]
        dists.append(math.hypot(qr - pr, qc - pc))
        d = angles[0] - angles[-1]
        while d > math.pi:
            d -= 2.0 * math.pi
        while d < -math.pi:
            d += 2.0 * math.pi
        ang_steps.append(math.degrees(abs(d)))
    seg_total = float(sum(dists))

    # --- diagnostica: turno firmato (|somma| ~ 360 su loop semplice) ------
    winding = 0.0
    if n >= 3:
        acc = 0.0
        for i in range(1, n):
            a0, a1 = main[i - 1], main[i]
            if i + 1 < n:
                a2 = main[i + 1]
            elif closed:
                a2 = main[0]
            else:
                break
            d_in = (a1[0] - a0[0], a1[1] - a0[1])
            d_out = (a2[0] - a1[0], a2[1] - a1[1])
            acc += _turn_signed(d_in, d_out)
        if closed:
            d_in = (main[-1][0] - main[-2][0], main[-1][1] - main[-2][1])
            d_out = (main[0][0] - main[-1][0], main[0][1] - main[-1][1])
            acc += _turn_signed(d_in, d_out)
        winding = abs(acc)

    # --- hull + profondita' (BAY) -----------------------------------------
    hull = _convex_hull(pts)
    hull_area = _poly_area(hull)
    concavity = float(area / hull_area) if hull_area > 0 else 1.0
    concavity = min(concavity, 1.0)

    depths = [0.0] * n
    if len(hull) >= 3:
        for i, p in enumerate(main):
            depths[i] = min(_point_seg_dist(p, hull[k], hull[(k + 1)
                                                            % len(hull)])
                            for k in range(len(hull)))
    hot = [d >= HOOK_CONCAVE_DEPTH_PX for d in depths]
    bay_arc, bay_wrap = _longest_hot_run(hot, dists, ang_steps, closed)
    bay_depth = max(depths) if depths else 0.0

    hook_candidate = bool(
        area >= HOOK_MIN_CORE_PX
        and bay_wrap >= HOOK_BAY_WRAP_MIN_DEG
        and bay_arc >= HOOK_BAY_ARC_MIN_PX
    )

    return {
        "found": True,
        "n_components": int(n_comp),
        "dbz_max": dbz_max,
        "core_area_px": area,
        "core_perimeter_px": int(len(pts)),
        "arc_length_px": round(seg_total, 2),
        "bay_depth_px": round(bay_depth, 2),
        "bay_arc_px": round(bay_arc, 2),
        "bay_wrap_deg": round(bay_wrap, 2),
        "winding_deg": round(winding, 2),
        "concavity_ratio": round(concavity, 4),
        "breaks": int(n_breaks),
        "hook_candidate": hook_candidate,
    }


def hook_score_from_features(f):
    """Score morfologico uncino 0-100 (float, 1 decimale) dalle feature.

    Pesi HOOK_SCORE_WEIGHTS (somma 1.00): bay_wrap (avvolgimento del solco),
    concavity (deficit area/hull), bay_arc (lunghezza del solco), intensita'
    dBZ. Gate: senza core, con area < HOOK_MIN_CORE_PX, o senza bay
    (bay_wrap < HOOK_BAY_WRAP_MIN_DEG) lo score e' 0 (nessuna fabbricazione
    da vuoto)."""
    if not f or not f.get("found"):
        return 0.0
    if int(f.get("core_area_px", 0)) < HOOK_MIN_CORE_PX:
        return 0.0
    if float(f.get("bay_wrap_deg", 0.0)) < HOOK_BAY_WRAP_MIN_DEG:
        return 0.0
    s_wrap = _norm(f.get("bay_wrap_deg", 0.0),
                   HOOK_BAY_WRAP_BASE_DEG, HOOK_BAY_WRAP_REF_DEG)
    s_conc = _norm(1.0 - float(f.get("concavity_ratio", 1.0)),
                   0.0, HOOK_CONCAVITY_DEFICIT_REF)
    s_arc = _norm(f.get("bay_arc_px", 0.0),
                  HOOK_BAY_ARC_MIN_PX, HOOK_BAY_ARC_REF_PX)
    s_int = _norm(f.get("dbz_max", 0.0), HOOK_DBZ_THRESHOLD, HOOK_DBZ_MAX_REF)
    w = HOOK_SCORE_WEIGHTS
    score = 100.0 * (w["bay_wrap"] * s_wrap + w["concavity"] * s_conc
                     + w["bay_arc"] * s_arc + w["intensity"] * s_int)
    return round(max(0.0, min(100.0, score)), 1)


def filter_persistence(current, history):
    """Score "persistito" = media pesata sugli ultimi 2-3 sample (2-3 storico).

    history: lista di score PRECEDENTI (dal piu' vecchio al piu' recente),
    puo' essere vuota. current: score del frame corrente. Pesi
    PERSISTENCE_WEIGHTS (1,2,3 dal piu' vecchio al piu' recente, somma 6.0):
    pesa di piu' l'osservazione recente ma richiede conferma multi-frame per
    valori alti. Deterministico: nessuno stato, nessuna I/O."""
    cur = float(current)
    if not math.isfinite(cur):
        return 0.0
    vals = []
    for v in (history or []):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            vals.append(fv)
    seq = vals[-2:] + [cur]           # fino a 3 sample totali
    w = PERSISTENCE_WEIGHTS[len(PERSISTENCE_WEIGHTS) - len(seq):]
    total_w = sum(w)
    return round(sum(s * wi for s, wi in zip(seq, w)) / total_w, 1)
