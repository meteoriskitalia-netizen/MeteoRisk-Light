# MeteoRisk Light — AUDIT 2 · Radar Player (Report B)

**File auditato:** `mri-light-1.0.0.9.html` (poi bump **1.0.0.11**)
**Data:** 2026-09-08
**Stato:** AUDIT + FIX APPLICATI in 1.0.0.11

---

## 1. Ambito e scopo

Audit del **Radar Player** (timeline sincronizzata satellite + radar DPC/altri provider + fulmini) con focus su:
- ciclo di vita play/pausa/stop;
- warm-ahead / precaricamento frame (radar e satellite EUMETSAT);
- refresh automatico dei dati live (ogni 5 min);
- comportamento con la tab in background (visibilità / battery / rete).

Mappatura funzioni chiave (coordinate riga post-fix):
`toggleRadarPlayer` (~L5756), `startSyncPlay` (~L5105), `stopSyncPlay` (~L5158), `toggleSyncPlay` (~L5097), `applySyncFrame` (~L5014), `showRadarFrameAt` (~L6254), `warmNextRadarFrame` (~L5263), `warmAheadEumetsatTiles` (~L5230), `buildSyncTimeline` (~L4850), `refreshLiveData` (~L6391), `startLiveRefreshTimer` (~L6426).

---

## 2. Reperti e gravità

| # | Gravità | Reperto |
|---|---|---|
| R1 | 🟠 IMPORTANTE | **Nessuna gestione `visibilitychange`/`pagehide`**: con la tab nascosta il play continua ad avanzare, il warm-ahead continua a scaricare frame futuri e il timer `refreshLiveData` (5 min) continua a girare → spreco di dati/batteria in background (critico su mobile). |
| R2 | 🟡 MIGLIORAMENTO | **`refreshLiveData` durante il play**: il refresh automatico chiamava `buildSyncTimeline(true)` a metà giro, azzerando timeline, cache dei frame e layer (`cleanupAll*Layers`) → scatto visibile e rischio di desync con l'animazione in corso. |
| R3 | 🟢 OK | **Warm-ahead radar**: `warmNextRadarFrame` preriscalda solo il frame successivo (bounded, sequenziale) — corretto. |
| R4 | 🟢 OK | **Warm-ahead EUMETSAT**: `warmAheadEumetsatTiles` con finestra `EUMETSAT_WARM_AHEAD=4` e URL `max-age=604800` — corretto e bounded. |
| R5 | 🟢 OK | **Play pacer / anti-stallo**: hard-limit `SAT_ACK_HARD_LIMIT_MS` + `SAT_ACK_POLL_MS` e continua anche su frame non confermati — corretto. |
| R6 | 🟢 OK | **Stop pulito**: `stopSyncPlay` invalida generazione satellite e pulisce i timer/ layer pendenti (ricco `stopSyncPlay` con cleanup crossfade/orfani) — corretto. |
| R7 | 🟢 OK | **Refresh idempotente**: `liveRefreshInProgress` evita reentrancy del fetch. |

---

## 3. Fix applicati (1.0.0.11)

### F3-R1 — Visibilità / risparmio risorse in background
Aggiunto un listener `visibilitychange` globale (+ `pagehide` per iOS Safari / Chrome Android dove `visibilitychange` può arrivare in ritardo):

- **Al passaggio della pagina a `hidden`:**
  1. se `isSyncPlaying` → `stopSyncPlay()` (ferma play e il relativo warm-ahead);
  2. se il timer `liveRefreshTimer` è attivo → lo sospende (`clearInterval`) e memorizza in `liveRefreshTimerSuspend = true` che era attivo.
- **Al ritorno a `visible`:**
  - se `liveRefreshTimerSuspend` ed è ancora attiva una modalità che lo richiede (`isRadarActive || isSatelliteActive || isLightningActive`) → ripristina il timer con `startLiveRefreshTimer()` (dati freschi al rientro).
  - Il **play NON riparte da solo**: resta in pausa e l'utente lo riavvia dal bottone (UX prevedibile, nessun burst di rete inatteso).

Flag nuovi: `liveRefreshTimerSuspend` (dichiarato accanto a `liveRefreshTimer`).

### F3-R2 — Refresh live non distruttivo durante il play
In `refreshLiveData`, la sezione che ricostruiva la timeline è ora condizionata:

```
if (!isSyncPlaying) {
    buildSyncTimeline(true);
    applySyncFrame(syncIndex);
    updateSyncCaption();
}
```

- Durante l'animazione il refresh aggiorna **solo i frame radar in background** (step 1, già al di fuori della sezione condizionata): i nuovi frame diventano disponibili al prossimo `buildSyncTimeline` non-conflittuale.
- L'animazione non viene più mai interrotta/desincronizzata dal refresh automatico dei 5 minuti.

---

## 4. Verifica

- Fix R1: tratti **null-safe** e racchiusi in `try/catch` nel listener di visibilità → nessuna eccezione fuori controllo.
- Fix R2: la condizione `!isSyncPlaying` evita il `buildSyncTimeline(true)` a metà giro senza cambiare il comportamento quando il player è fermo.
- Nessuna modifica alle formule, alle soglie, alla palette, al coloring delle mappe o alla pipeline dati.

---

## 5. Stato finale

| Voce | Esito |
|---|---|
| R1 visibility/background | 🟢 **FIXATO (1.0.0.11)** |
| R2 refresh durante play | 🟢 **FIXATO (1.0.0.11)** |
| R3–R7 | 🟢 già corrette, nessuna modifica |

Report chiuso. Versione bump: **1.0.0.11** (VERSION + APP_VERSION + APP_RELEASE_DATE + APP_CHANGELOG voci 1.0.0.10/1.0.0.11).
