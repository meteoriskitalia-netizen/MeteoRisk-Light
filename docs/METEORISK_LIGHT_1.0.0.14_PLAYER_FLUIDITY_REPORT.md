# METEORISK LIGHT 1.0.0.14 — REPORT PLAYER FLUIDO "SAT24-STYLE"

**Versione:** 1.0.0.14 · **Data:** 2026-09-08 · **Tipo:** release (bump da 1.0.0.13)
**File applicativo:** `mri-light-1.0.0.14.html`

## Obiettivo
Rendere l'animazione del player radar/satellite **fluida come il player di sat24.com** e
rimuovere la pressione su memoria/GC che su mobile era la causa principale dei crash
("l'app è ancora troppo pesante e crea spesso dei crash"). In più: sistemare la
**formattazione mobile dei toggle**, che quando si apre il player diventavano enormi.

**Scelta utente vincolante:** mantenere l'intera timeline (25 slot = ultime 2h, slot da
5 min) in loop durante il play. **Niente** short-loop stile sat24 e niente finestra
adattiva: la fluidità si ottiene con l'ingegneria del motore, non tagliando i dati.

## Modifiche applicate (`mri-light-1.0.0.14.html`)

### A) Scheduler `requestAnimationFrame` (sostituisce la catena setTimeout)
- Il play non usa più la catena di `setTimeout(playStep, frameDelay)` ma un **rAF-loop
  con pacing temporale** (`playPacer`): risoluzione 60fps, niente deriva dei timer,
  niente accumulo di callback in flight.
- Pacing a `frameDelay = Math.max(100, round(800 / syncPlaySpeed))`.
- Il **pacer di conferma satellite è conservato**: il passo non avanza finché il frame
  satellite corrente non è confermato, con hard-limit anti-stallo (`SAT_ACK_HARD_LIMIT_MS`).
- Hook `window.__kickSatPacer` (consumato da `resolveEumetsatLoad`) **preservato**:
  alla conferma anticipata il kick arretra `lastStepMs` così il frame avanza subito,
  ma mai più veloce del `frameDelay` scelto.
- Bonus AUDIT-2: l'rAF non gira a tab nascosta → il warm-ahead si ferma da solo in
  background.
- `playStep` interamente in `try/catch`: un frame fallito (tile 502/503, canvas,
  eccezione del provider) non uccide MAI l'animazione.
- `stopSyncPlay()` cancella l'rAF (`cancelAnimationFrame(syncRAF)`) come già faceva
  con i timer; `syncRAF` azzerato.

### B) Riuso del layer radar (niente più un layer per frame)
- Nuova variabile **`radarTileLayer`** (layer radar persistente). Nel ramo XY/DPC di
  `showRadarFrameAt` il frame viene applicato **con `setUrl()` sullo stesso tile layer**:
  Leaflet ri-requesta i soli tile dello stesso contenitore (cache browser = scambio
  istantaneo), zero churn DOM/GC, zero accumulo di layer densi di tile nel `radarPane`.
- Prima ogni frame creava **un nuovo `L.tileLayer` da aggiungere** (e i vecchi venivano
  rimossi in ritardo): decine di tile DOM nuove al minuto; su mobile era la pressione
  di memoria che portava ai crash.
- Il layer persistente viene azzerato (`radarTileLayer = null`) in
  `cleanupAllRadarLayers()` e difensivamente in `clearRadarOverlay()`, così un cambio
  provider ricompone il layer corretto.
- **Guardia anti-stale in `resolveRadarLoad`**: i callback della generazione precedente
  riconoscono il layer condiviso (`isSharedLayer = newLayer === radarTileLayer`) e non lo
  rimuovono MAI da JavaScript (`map.removeLayer` resta solo sui layer NON riusati).
- Listener `load`/`error` ri-bindati con `off()`/`on()` sul layer persistente a ogni
  frame; fallback `TILE_FALLBACK_DELAY_MS` per i tile già in cache che non ri-sparano
  `load`.
- Ramo **WMS EUMETSAT lasciato su layer-per-frame**: il riuso via `setParams`/`redraw`
  è fragile per il pacer di conferma dei tile WMS. Rischio accettato/documentato — il
  ramo WMS è sorgente non di default (`satSource` predefinito = `infoplaza_mtg`, ramo
  leggero imageOverlay).

### C) Warm-ahead bounded (prima: Image() orfani senza controllo)
- Precaricamento ora **con budget condiviso `WARM_CONCURRENCY_CAP = 16`** preload
  simultanei e pool trattenuto `warmPreloadPool` con potatura `trimWarmPool()`.
- Radar XYZ: precarica fino a **`RADAR_WARM_AHEAD = 2`** frame avanti (prima tutti i
  tile del solo frame successivo, comunque senza tetto).
- EUMETSAT WMS: `EUMETSAT_WARM_AHEAD` mantenuto ma sotto budget e con trim del pool.
- Ogni `Image()` di warm viene **trattenuto nel pool** finché non conlude
  (onload/onerror → rimozione dal pool): il browser non perde i riferimenti e può
  liberarli; in più esiste il tetto. Prima ogni tile generava un `Image()` orfano che
  partiva comunque la download → memoria decoded/image crescente e crash su mobile.
- `warmAheadEumetsatTiles()` e `warmNextRadarFrame()` in `try/catch` (non propagano MAI
  eccezioni al play).
- Pool svuotato in `fullPlayerCleanup()`.

### D) Crash-guard canvas DPC
- `getContext('2d')` null-safe (guardia + try/catch): su dispositivi a risorse ridotte
  poteva restituire `null` → crash Leaflet.
- `drawImage` / `getImageData` / `putImageData` in `try/catch` (canvas tainted → il tile
  originale già disegnato resta visibile, niente crash).

### E) Fix toggle mobile (dimensioni costanti)
- **Causa:** regola mobile `.toolbar .tool-btn { flex: 1 1 auto; }` dentro un container
  `flex-wrap`; all'apertura del player entravano i 3 sub-toggle (radar/satellite/fulmini)
  e, ricomponendo le righe, i toggle singoli si allargavano a riga piena ("enormi").
- **Fix:** la riga dei toggle è ora una CSS grid
  `.toolbar-row { display: grid !important; grid-template-columns: repeat(auto-fill,
  minmax(140px, 1fr)); }` → larghezze **fisse e identiche**, che non cambiano quando
  compaiono/scompaiono bottoni. `flex` dei tool-btn portato a `0 1 auto`.
- `min-height: var(--touch-min)` (44px) preservato; i pattern di
  `test_mobile_responsive.mjs` restano verificati.

### Nessuna modifica fuori area
- FORMULE COMPOSITI **inalterate** (SCP/EHI/WMAXSHEAR/ML-SHIP); palette e coloring
  slider invariati; SCI/AUDIT 1.0.0.12/1.0.0.13 invariati; pipeline dati/temi invariati.
- Full timeline (25 slot) mantenuta: `SYNC_NUM_SLOTS = 24` → 25 slot da 5 min.

## Test
Nuovo test strutturale **`scripts/tests/test_player_fluidity.mjs`** (38 check: scheduler
rAF, riuso layer radar + guardie stale, warm-ahead bounded, crash-guard DPC, grid
toggle mobile, versione + changelog + full timeline).

Suite .mjs completa: **9/9 PASS** (aggiunge il nuovo test ai precedenti 8:
day_mapping, forecast_slot_e2e, fascia_slots, color_coherence, live_panel_gating,
mobile_responsive, sciaudit, temporal_slider, player_fluidity) + contract
`contract_dataset_loader.mjs` PASS.

## Verifica consigliata in-browser (manuale)
1. Aprire `mri-light-1.0.0.14.html` su mobile (DevTools emulazione 320/375/430px).
2. Aprire il Radar Player e premere Play: la cadenza dei frame deve essere fluida
   (100–120 ms/frame a velocità 5=2x), **niente lampeggio**, box "frame caricati"
   in fase, timeline completa (25 slot).
3. Aprire/passare al satellite: provare anche la sorgente WMS EUMETSAT manuale.
4. Controllare che i **toggle della toolbar restino della stessa dimensione** quando si
   apre il player e quando rientra il pannello.
5. Su Android/iOS reali: 10 min di play continuo senza crash e senza crescita di
   memoria visibile; tab nascosta → play in pausa (niente warm in background).