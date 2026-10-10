#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — events.py: store eventi ROLLING + badges, scrittura ATOMICA.

    events.json  — {"generated_at", "window_hours": 24, "events":[...]}
                   merge per id (stesso id -> aggiornato, first_seen conservato)
                   + prune degli eventi con last_seen piu' vecchio di 24h;
    badges.json  — {"generated_at", "radar_timestamp", "badges":[...]} dello
                   stato CORRENTE: SOLO eventi attivi (last_seen ==
                   radar_timestamp, cioe' visti sull'ultimo frame radar).

ID EVENTO (scelta stabile, documentata): f"{type}-{track_type}-{track_id}"
(es. "HAIL-cell-3"), con anchor = "{track_type}-{track_id}" salvato in
evidence.anchor (area_id dei badge).
  - scelto perche' i track_id del radar engine sono numerati per run: una chiave
    basata sulla posizione (type-lon-lat arrotondati) genererebbe un NUOVO id a
    ogni km di movimento della cella e crescerebbe senza limite nella finestra
    24h; il prezzo (confini fra run con stesso id numerico) e' accettato,
    dichiarato e comunque limitato dal prune.

Scrittura ATOMICA: mirror di radar_engine.output._atomic_write_text (tmp nella
stessa directory + json.loads di validazione + os.replace): mai file parziali,
mai sovrascrittura del precedente se la serializzazione fallisce.

NO-OP GUARD (anti commit-churn): write_json_if_changed riscrive SOLO se il
CONTENUTO sostanziale cambia, ignorando i campi volubili (generated_at degli
eventi; generated_at + radar_timestamp dei badge — radar_timestamp conta come
volubile perche' il suo cambio da solo, senza eventi, non e' una variazione
di fenomeno). A contenuto invariato il file NON viene toccato (stessi byte,
stesso mtime) -> il workflow non trova diff e non committa, e generated_at
resta quello dell'ultima scrittura REALE.
"""

import datetime as _dt
import hashlib
import json
import math
import os
import tempfile

from . import WINDOW_HOURS

EVENTS_NAME = "events.json"
BADGES_NAME = "badges.json"
# Aggregazione per tempesta (anti-falsi-positivi/duplicati): i track_id del
# radar engine sono rinumerati a ogni run, quindi la stessa cella puo' entrare
# nella finestra 24h con id diversi -> N eventi "diversi" per UNA tempesta.
# Due eventi dello STESSO type sono lo stesso sistema se NON distano piu' di
# EVENT_DEDUP_KM fra gli anchor e non oltre EVENT_MERGE_GAP_MIN di intervallo
# temporale; il gruppo diventa UN evento (id/first_seen del piu' vecchio,
# last_seen massimo, severita' massima) con evidenza aggregata.
EVENT_DEDUP_KM = 25.0
EVENT_MERGE_GAP_MIN = 45.0
_STATE_RANK = {"SUSPECT": 0, "CORROBORATED": 1, "VERIFIED": 2}
# Campi volubili esclusi dal confronto (cambiano a ogni run anche senza
# variazioni di fenomeno: sono l'orologio, non il dato).
VOLATILE_EVENT_KEYS = ("generated_at",)
VOLATILE_BADGE_KEYS = ("generated_at", "radar_timestamp")


class WriteError(Exception):
    """Scrittura atomica fallita (il file precedente resta integro)."""


def utc_iso(now=None):
    """ISO UTC con secondi (YYYY-MM-DDTHH:MM:SSZ). `now` iniettabile per test."""
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_ms(value):
    """ISO8601 (Z o offset) -> epoch ms; input non interpretabile -> None."""
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def ms_to_iso(ms):
    """Epoch ms -> ISO UTC (secondi); None -> None."""
    if ms is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(
            int(ms) / 1000.0, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _replace(src, dst):
    """Indirizzato (non os.replace diretto) per essere iniettabile nei test."""
    os.replace(src, dst)


def atomic_write_json(path, payload):
    """Scrive payload come JSON in modo atomico (tmp + validazione + replace).

    json.dumps PRIMA di toccare il filesystem (payload non serializzabile ->
    nessuna scrittura); json.loads di validazione prima della replace (specchio
    di radar_engine.output._atomic_write_text). Errore I/O -> WriteError con il
    file precedente intatto e tmp ripulito."""
    text = json.dumps(payload, ensure_ascii=False)
    json.loads(text)                      # validazione PRIMA della replace
    directory = os.path.dirname(str(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".phenomena-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, str(path))
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise WriteError(f"atomic_write_failed:{exc}") from exc


def content_digest(payload, volatile_keys=VOLATILE_EVENT_KEYS):
    """SHA256 del contenuto SOSTANZIALE: campi volubili esclusi, chiavi ordinate.

    Confronto semantico (non byte a byte): due payload che differiscono solo
    per generated_at/radar_timestamp hanno lo stesso digest."""
    volatili = set(volatile_keys or ())
    slim = {k: v for k, v in (payload or {}).items() if k not in volatili}
    blob = json.dumps(slim, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def write_json_if_changed(path, payload, volatile_keys=VOLATILE_EVENT_KEYS):
    """Scrive `payload` SOLO se il contenuto sostanziale cambia (no-op guard).

    Ritorna True = scritto, False = invariato (file NON toccato: nessuna
    riscrittura, nessun diff, nessun commit nel workflow; il generated_at
    resta quello dell'ultima scrittura reale). File assente/corrotto ->
    scrittura."""
    digest_new = content_digest(payload, volatile_keys)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict) and \
            content_digest(existing, volatile_keys) == digest_new:
        return False
    atomic_write_json(path, payload)
    return True


def empty_events(generated_at=None, window_hours=WINDOW_HOURS):
    """Store vuoto (file assente/corrotto -> nessun crash, nessun dato inventato)."""
    return {"generated_at": generated_at, "window_hours": int(window_hours),
            "events": []}


def load_events(path):
    """Legge events.json; file assente/corrotto/non-dict -> store vuoto."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty_events()
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return empty_events()
    return {
        "generated_at": data.get("generated_at"),
        "window_hours": int(data.get("window_hours") or WINDOW_HOURS),
        "events": [e for e in data["events"]
                   if isinstance(e, dict) and e.get("id")],
    }


def index_by_id(events):
    """Indice id -> evento (ultima occorrenza in caso di id ripetuti)."""
    return {str(e.get("id")): e for e in (events or []) if isinstance(e, dict)}


def merge_events(existing, incoming, generated_at, window_hours=WINDOW_HOURS,
                 prune_anchor=None):
    """Merge (stesso id -> aggiornamento) + prune oltre window_hours.

    - id nuovo -> appended;
    - id gia' presente -> sovrascritto con l'evento del run corrente (state,
      score, last_seen, position, evidence) e first_seen = MIN fra i due;
    - id ASSENTE dal run corrente -> conservato (diventa "storico") finche'
      non scade la finestra: e' il last_seen a farlo uscire dai badges;
    - last_seen piu' vecchio di window_hours rispetto all'ANCORA DEL PRUNE ->
      rimosso (last_seen non interpretabile -> tenuto, mai buttato un dato per
      un errore di parse).

    ANCORA: prune_anchor se fornito (engine passa il radar_timestamp = TEMPO
    DEI DATI), altrimenti generated_at (orologio). Scelta documentata: la
    finestra rolling e' in tempo dati, non in tempo parete — un orologio piu'
    avanti dei dati (dev/stallo) non puo' distruggere gli eventi appena
    scritti nella stessa run; resta comunque esposto generated_at e il badge
    si svuota da solo se il radar_timestamp non corrisponde (is_active).
    Ritorna il payload events.json con gli eventi ordinati per id (determinismo:
    stesso input -> stesso file)."""
    window_hours = int(window_hours or WINDOW_HOURS)
    events = [dict(e) for e in ((existing or {}).get("events") or [])
              if isinstance(e, dict) and e.get("id")]
    idx = index_by_id(events)
    for ev in (incoming or []):
        if not isinstance(ev, dict) or not ev.get("id"):
            continue
        cur = idx.get(str(ev["id"]))
        if cur is None:
            new = dict(ev)
            idx[str(ev["id"])] = new
            events.append(new)
            continue
        old_first = iso_to_ms(cur.get("first_seen"))
        new_first = iso_to_ms(ev.get("first_seen"))
        cur.update(ev)
        first = min([v for v in (old_first, new_first) if v is not None],
                    default=None)
        if first is not None:
            cur["first_seen"] = ms_to_iso(first)
    gen_ms = iso_to_ms(prune_anchor if prune_anchor is not None
                       else generated_at)
    if gen_ms is not None:
        cut = gen_ms - window_hours * 3600 * 1000
        kept = []
        for e in events:
            last = iso_to_ms(e.get("last_seen"))
            if last is None or last >= cut:
                kept.append(e)
        events = kept
    events.sort(key=lambda e: str(e.get("id")))
    return {"generated_at": generated_at, "window_hours": window_hours,
            "events": events}


def _num(value):
    """Numero da int/float/str; None se non interpretabile (bool escluso)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _position_lonlat(event):
    """(lon, lat) da event.position; None se assente/non numerico."""
    pos = event.get("position")
    if not (isinstance(pos, (list, tuple)) and len(pos) >= 2):
        return None
    lon = _num(pos[0])
    lat = _num(pos[1])
    if lon is None or lat is None:
        return None
    return (lon, lat)


def _haversine_km(a, b):
    """Distanza great-circle (km) fra due (lon, lat)."""
    lon1, lat1 = a
    lon2, lat2 = b
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2)
    return 2.0 * radius * math.asin(min(1.0, math.sqrt(h)))


def _event_frames(event):
    """Frame osservati dichiarati dall'evidenza (0 se assenti)."""
    ev = event.get("evidence") or {}
    for key in ("frame_count", "observed_frames", "frames_observed"):
        n = _num(ev.get(key))
        if n is not None:
            return int(n)
    return 0


def _same_storm(a, b, radius_km, max_gap_min):
    """True se due eventi sono lo stesso sistema (type + spazio + tempo)."""
    if str(a.get("type")) != str(b.get("type")):
        return False
    af, al = iso_to_ms(a.get("first_seen")), iso_to_ms(a.get("last_seen"))
    bf, bl = iso_to_ms(b.get("first_seen")), iso_to_ms(b.get("last_seen"))
    gap = None
    if al is not None and bf is not None:
        gap = bf - al
    elif bl is not None and af is not None:
        gap = af - bl
    if gap is None:
        # nessun tempo confrontabile: solo lo stesso anchor prova l'identita'
        return (str((a.get("evidence") or {}).get("anchor"))
                == str((b.get("evidence") or {}).get("anchor")))
    if gap > max_gap_min * 60000.0:
        return False
    pa, pb = _position_lonlat(a), _position_lonlat(b)
    if pa is not None and pb is not None:
        return _haversine_km(pa, pb) <= radius_km
    return (str((a.get("evidence") or {}).get("anchor"))
            == str((b.get("evidence") or {}).get("anchor")))


def _absorb_event(members):
    """Fonde un gruppo di eventi omogenei in UN evento aggregato.

    id/first_seen del membro piu' vecchio (determinismo sulla finestra),
    last_seen massimo, severita' e score massimi, posizione/evidenza del
    membro piu' forte (a parita' di severita', il piu' recente). Aggiunge
    solo dati dentro evidence (schema additivo): aggregated_events,
    aggregated_ids, frame_count (somma), event_span_min."""
    def first_ms(ev):
        return iso_to_ms(ev.get("first_seen"))

    def last_ms(ev):
        return iso_to_ms(ev.get("last_seen"))

    oldest = min(members, key=lambda e: (first_ms(e) is None,
                                         first_ms(e) or 0.0,
                                         str(e.get("id"))))
    base = max(members, key=lambda e: (_STATE_RANK.get(str(e.get("state")), -1),
                                       last_ms(e) or 0.0))
    merged = dict(base)
    merged["id"] = oldest.get("id")
    if oldest.get("first_seen") is not None:
        merged["first_seen"] = oldest.get("first_seen")
    ends = [last_ms(e) for e in members if last_ms(e) is not None]
    if ends:
        merged["last_seen"] = ms_to_iso(max(ends))
    scores = [s for s in (_num(e.get("score")) for e in members) if s is not None]
    if scores:
        merged["score"] = max(scores)
    evidence = dict(merged.get("evidence") or {})
    evidence["aggregated_events"] = len(members)
    evidence["aggregated_ids"] = sorted(str(e.get("id")) for e in members)
    evidence["frame_count"] = sum(_event_frames(e) for e in members)
    span_first = min([first_ms(e) for e in members
                      if first_ms(e) is not None], default=None)
    span_last = max(ends) if ends else None
    if span_first is not None and span_last is not None:
        evidence["event_span_min"] = round(
            (span_last - span_first) / 60000.0, 1)
    merged["evidence"] = evidence
    return merged


def aggregate_events(events, radius_km=EVENT_DEDUP_KM,
                     max_gap_min=EVENT_MERGE_GAP_MIN):
    """Collassa in UN evento per sistema le osservazioni riconosciute distinte.

    Unione transitiva (union-find) su: stesso type + entro radius_km +
    intervallo temporale <= max_gap_min. Determinismo: risultato ordinato per
    id. Non tocca i tipi diversi (HAIL e VORTEX restano separati)."""
    items = [dict(e) for e in (events or [])
             if isinstance(e, dict) and e.get("id")]
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _same_storm(items[i], items[j], radius_km, max_gap_min):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    merged = [_absorb_event(m) if len(m) > 1 else m[0]
              for m in groups.values()]
    merged.sort(key=lambda e: str(e.get("id")))
    return merged


def is_active(event, radar_timestamp):
    """Attivo = last_seen del radar_timestamp corrente (visto sull'ultimo frame)."""
    if not event or not radar_timestamp:
        return False
    a = iso_to_ms(event.get("last_seen"))
    b = iso_to_ms(radar_timestamp)
    if a is None or b is None:
        return str(event.get("last_seen")) == str(radar_timestamp)
    return a == b


def build_badges(events, radar_timestamp, generated_at):
    """badges.json: SOLO eventi attivi, in ordine di id (nessun duplicato).

    Ogni badge espone type/state/position/area_id/labels/evidence; labels vive
    in evidence.labels dell'evento e viene promossa a campo del badge."""
    badges = []
    for ev in sorted((events or []), key=lambda e: str(e.get("id"))):
        if not isinstance(ev, dict) or not is_active(ev, radar_timestamp):
            continue
        evidence = dict(ev.get("evidence") or {})
        labels = list(evidence.pop("labels", []) or [])
        badges.append({
            "id": ev.get("id"),
            "type": ev.get("type"),
            "state": ev.get("state"),
            "position": ev.get("position"),
            "area_id": evidence.get("anchor"),
            "labels": labels,
            "evidence": evidence,
        })
    return {"generated_at": generated_at,
            "radar_timestamp": radar_timestamp,
            "badges": badges}
