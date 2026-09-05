# METEORISK LIGHT 1.0.0.2 — REPAIR REPORT (ROOT CAUSE + UI CLEANUP + SECOND PASS FIX)

| Campo | Valore |
|---|---|
| Versione | **1.0.0.2** (`APP_VERSION = '1.0.0.2'`) |
| Data di rilascio | 2026-09-05 |
| Base | `releases/Light-1.0.0.1/mri-light-1.0.0.1.html` (copia byte-identica) |
| File prodotto | `releases/Light-1.0.0.2/mri-light-1.0.0.2.html` (1.644.801 byte, SHA-256 `1DBC7757…07E5`) |
| Riferimento analisi | `..\Light-1.0.0.1\docs\METEORISK_LIGHT_FORENSIC_AUDIT.md` (base vincolante) |
| Scope | SOLO root-cause repair + UI cleanup minimale dei controlli non approvati + fix mirati (radar player, vista sfumata dual, guardia `file://`, satellite che lampeggia). Nessuna nuova funzionalità, nessuna modifica di logica/flag, nessuna modifica grafica di testo/tema |
| Versioni precedenti | NON modificate (verificate via hash, §10) |

---

## 1. ROOT CAUSE

**Sintomo:** all'apertura, in Light 1.0.0.0/1.0.0.1, la mappa resta nera e l'app non parte: `ReferenceError: L is not defined`.

**Catena (RICEVUTA CONFERMATA in audit forense):**
1. In Light il loader Leaflet è **async e gated**: `document.createElement('script')` con `src=unpkg`, fallback jsdelivr su `onerror`, eseguito dentro `if (isFeatureEnabled('leafletCdn'))` in `<head>` (`<link id="leaflet-css">` e `<script id="leaflet-js">` vuoti).
2. Il main script esegue subito dopo e utilizza **`window.L` a parse-time (top-level)**: riga 5940 `var DPCRadarLayer = L.TileLayer.extend({`).
3. Leaflet (async) NON è ancora arrivato → **ReferenceError: L is not defined** (riga 5940 col 4; CDP: frame line 5939 col 24) → **abort dell'intero main script**.
4. Di conseguenza non vengono MAI definite le funzioni di boot: la guardia `startApp(retriesLeft)` (riga 16546, retry 200 ms × 25) non può partire; `initMap`, `applyFeatureFlagConstraints`, il footer dinamico e lo switch Sviluppo non esistono.
5. Riprendendo manualmente `startApp(25)` si arriva a `applyFeatureFlagConstraints`, che accede a `liveRadarProvider` dichiarato `let` alla riga 7061 **dopo** la riga 5940 del crash → TDZ `Cannot access 'liveRadarProvider' before initialization`: conferma diretta che il main script era stato interrotto prima della dichiarazione.

**Controllo:** l'originale 10.52.27.0 usa il tag bloccante classico `<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" onerror="…jsdelivr…">` in `<head>` → boot OK (verificato in audit: L presente, mappa a zoom 6, tile e province, footer, switch Sviluppo).

**Esito:** boot matrix (audit) — crash identico su `file://`, `http://127.0.0.1:8123`/`:8124`, `http://localhost:8124`, cache calda e fredda; originale file:// OK.

---

## 2. LEAFLET FIX (implementato)

### 2a. Loader SINCRONO e BLOCCANTE (coerente con l'originale)
Sostituita la logica `createElement` async con **`document.write`** durante il parse (i due tag restano vuoti: `<link id="leaflet-css" />`, `<script id="leaflet-js"></script>`):

```html
if (isFeatureEnabled('leafletCdn')) {
    var __leafletCss = document.getElementById('leaflet-css');
    if (__leafletCss) { /* href unpkg css, onerror → jsdelivr css */ }
    document.write('<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"><\/script>');
    document.write('<script>if (!window.L) { var __lsx = document.createElement("script"); __lsx.src = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"; __lsx.onerror = function() { window.__leafletFailed = true; }; document.head.appendChild(__lsx); }<\/script>');
} else {
    window.__leafletDisabled = true;
}
```

- `document.write` in `<head>` durante il parse inserisce un tag `<script src>` **parser-blocking**: il parser non passa al main script finché Leaflet non è eseguito → `window.L` **garantito** (la chiamata top-level 5940 non può più correre avanti).
- Il **checker inline** verifica `window.L` dopo il primo CDN: se assente (primo CDN inerte/bloccato) carica il fallback jsdelivr e, in caso di fallimento, imposta `window.__leafletFailed`.
- Con `leafletCdn=false` nessun tag viene scritto (nessuna richiesta), comportamento identico al flag.

### 2b. Defusing dell'unico uso top-level di L (riga 5940)
Rimane comunque una dipendenza a parse-time → avvolta in lazy-factory (la sincronizzazione del boot non dipende più dal timing di alcun CDN):

```js
var DPCRadarLayer = null;
function getDPCRadarLayerClass() {
    if (DPCRadarLayer) return DPCRadarLayer;
    DPCRadarLayer = L.TileLayer.extend({ createTile: … });
    return DPCRadarLayer;
}
```

Uso aggiornato (creazione layer DPC): `newLayer = new (getDPCRadarLayerClass())(url, xyzOpts).addTo(map);`

Stesso significato; la classe viene costruita solo quando DPC radar è attivo. Verificato con grep: **la riga 5940 era l'unico uso di `L` a livello top-level** del main script.

### 2c. Note browser (non-bloccanti)
- Edge/Chromium emette warning "parser-blocking, cross site script invoked via document.write … MAY be blocked on poor connectivity": **non** ha bloccato il caricamento nei test (script eseguito, `window.L` presente). Suggerimento per release future (FUORI scope): bundling locale di Leaflet same-origin per eliminare anche il warning.
- Edge "Tracking Prevention" ha bloccato solo l'**accesso allo storage** del CDN (non il download). Irrilevante.

---

## 3. MAP VERIFICATION

| Ambiente | Risultato | Dettagli boot |
|---|---|---|
| **localhost / HTTP statico** (`http://127.0.0.1:8125/…`) | **PASS** | `window.L` presente; `map.getZoom()=5`; tile OSM 8 (tutti `tile.openstreetmap.org`); `footer-version="v1.0.0.2"`; switch Sviluppo presente (OFF); rimossi assenti; `api.open-meteo.com` 9 richieste progressive; **0 exception JS** |
| **file://** (`file:///…/mri-light-1.0.0.2.html`) | **PASS** | identico al precedente (L, OSM tile 8, footer v1.0.0.2, switch Sviluppo, nessun errore console di alcun tipo) |
| Province su mappa | **PASS** | `map.hasLayer(provinceGeoJsonLayer)=true`; **107 poligoni `path.zone-marker`** colorati (fill `#22c55e`… = colorazione rischio progressiva) |
| Interazioni | **PASS** | satellite EUMETSAT (WMS → `view.eumetsat.int`), radar RainViewer (api + tilecache), LIVE panel (liveSat attivo), toggle Sviluppo ON/OFF |

Orari: boot ~0,93 s (dalla navigazione alla presenza di `L` + footer). Il fetch Open-Meteo dual-model procede per punti (257 su 107 province) come atteso.

---

## 4. FUNZIONI RIPRISTINATE (ritornate operative col fix)

| Funzione | Stato | Evidenza |
|---|---|---|
| Boot completo / mappa | RIPRISTINATA | initMap OK, basemap OSM, zoom 5, dimensione mappa 738×360 |
| Confini provinciali ISTAT (107) | RIPRISTINATI | 107 path colorati, layer attivo |
| Timeline satellite EUMETSAT | RIPRISTINATA | 32 richieste WMS `view.eumetsat.int` |
| Timeline radar (RainViewer) | RIPRISTINATA | richieste `api.rainviewer.com` + `tilecache.rainviewer.com` |
| LIVE panel (satellite+radar live) | RIPRISTINATA | pannello attivo, liveSatLayer presente |
| Cartelle allerte DPC / toggle vari | RIPRISTINATE | `dpc-alert-select`, radar-provider, dpc-product, thermalrisk, conv-shear presenti |
| Switch Sviluppo | RIPRISTINATO | id `sviluppo-switch` nel footer: ON → mostra `weather-model-wrapper` e tab Conv. |
| Status bar / messaggi | RIPRISTINATA | "Dual Model: punto N/257 …" |

Nessuna funzione legittima dell'edizione è andata persa: il crash impediva l'inizializzazione di TUTTE; oggi l'intero set è di nuovo eseguito.

---

## 5. UI CONTROLS REMOVED (fonti NON approvate)

| Controllo rimosso | Fonte | Note |
|---|---|---|
| Pulsante `⚡ Fulmini` (`btn-lightning`) | Blitzortung / Sat24 | sub-toggle radar/satellite restano |
| Selettore Sorgente Fulmini (`lightning-source-select`) | Sat24 (2) + Blitzortung (2) | blocco intero |
| Blocco "Vista mappa" embed Blitzortung nel LIVE panel (`blitz-embed-select`, URL, container/iframe) | Blitzortung / LightningMaps | il pannello LIVE resta (testo ed header informativi NON toccati: fuori scope modifiche grafiche) |
| Pulsante `⛈️ PRETEMP` (`btn-pretemp`) | pretemp.it | — |
| Container embed PRETEMP (`pretemp-container`, `pretemp-content`, `pretemp-back`) | pretemp.it | — |
| Pulsante `🗺️ Meteociel` (`btn-meteociel`) | meteociel.fr | — |
| Container embed Meteociel (`meteociel-container`, `mc-*`) | meteociel.fr | — |
| Selettore **Stile Mappa** (`map-style-select`) | ESRI Dark / ArcGIS Satellite | resto la sola basemap OSM (via `enabledMapStyleIds()`) |
| Opzione `Meteo&Radar` in Radar embed | meteoradar.it | — |
| Opzione `Windy` in Radar embed | windy.com | — |
| 4 opzioni `infoplaza_*` in **Sorgente Satellite** | Sat24 / Infoplaza | restano le 3 EUMETSAT (`eumetsat`,`sat24mtg`,`sat24vis`) |

**Null-safety:** tutti i riferimenti JS ai controlli rimossi sono già protetti da guard (`if (el)` / `if (el)`/`syncSelect` null-safe) — verificati via grep (`getElementById` per i 12 id). Nessun crash, nessuna eccezione residua nei test.

---

## 6. FUNZIONI MANTENUTE (incluso Switch Sviluppo)

**Toolbar:** Radar Player · Radar Timeline · Satellite · Previsioni · LIVE Panel · La Caletta (hidden) · Evidenzia Zone a Rischio · Nuovi Indici (hidden) · Fonti & Metodologie.

**Controlli:** `radar-provider-select` (RainViewer · LibreWXR/DPC+OPERA · DPC), `dpc-product-select` (10 prodotti WebP DPC), `radar-embed-select` (off · DPC), `sat-source-select` (3 EUMETSAT), `thermalrisk-index-select`, `conv-shear-select`, `dpc-alert-select`, metric-tabs (Rischio/Temp/Um/Press/Vento/Termico/Conv dev), selettore modello meteo e fonte API (visibili con Sviluppo).

**Integrazioni attive (flag `true`, nessuna modifica ai flag):** Open-Meteo, RainViewer, DPC (radar, allerte, embed), LibreWXR, EUMETSAT WMS, METAR Iowa State, OSM, province ISTAT locali, Chart.js CDN, Leaflet CDN.

**Integrazioni bloccate dai flag (`false`, nessuna rete e nessun controllo in UI):** radarMeteoRadarEmbed, radarWindyEmbed, lightningBlitzortung, lightningLimaps, lightningSat24, satelliteSat24/Infoplaza, esriBasemap, meteocielEmbed, pretempEmbed, API key sperimentali.

**Switch Sviluppo — verificato funzionante:**
- OFF (default): `weather-model-wrapper` display=none, tab Conv. nascosta, label "Sviluppo OFF".
- ON: `weather-model-wrapper` display=flex, tab Conv. visibile, label "Sviluppo ON".
- Testato via evento change sul checkbox (id `sviluppo-switch`), presenza nel footer confermata su HTTP e file://.

---

## 7. CONSOLE AUDIT

| Canale | HTTP (127.0.0.1:8125) | file:// |
|---|---|---|
| `pageerror` / unhandledrejection | **0** | **0** |
| Errori JS | **0** | **0** |
| Log info | 21 (audit `PUBLIC_EDITION_FEATURES`, samples, dual-model) | 21 |
| Warning `document.write` parser-blocking (Chromium) | 2 — attesi dal fix, NON bloccanti (script eseguito, L presente) | 2 |
| Warning "Tracking Prevention blocked storage" (Edge) | 10 — solo storage, download OK | 10 |
| Verbose DOM (password field) | 1 — benigno | — |
| Error "Failed to load resource 404" | 1 — **`/favicon.ico`** (richiesta automatica del browser; non-JS, non bloccante; presente solo su HTTP) | 0 |

Esito: **nessun errore JS critico**; unico 404 = favicon.ico (opzionale: aggiungere un favicon in hosting per pulire la console).

---

## 8. NETWORK AUDIT

Domini contattati nei test (HTTP + interazioni): `unpkg.com` (leaflet.css/js), `tile.openstreetmap.org` (basemap), `api.open-meteo.com` (dati meteo), `view.eumetsat.int` (satellite LIVE/timeline), `api.rainviewer.com` + `tilecache.rainviewer.com` (radar), `127.0.0.1:8125` (pagina + favicon).

**Blacklist NON approvati (zero contatti):** blitzortung · lightningmaps · limaps · sat24 · infoplaza · meteociel · windy · wetteronline · meteored · pretemp · arcgis/esri · allorigins · corsproxy · cors-anywhere · codetabs → **`bannedHit = []`** anche con LIVE panel aperto (richieste Blitzortung bloccate dal flag `lightningBlitzortung=false`, `refreshBlitzTile` early-return).

Confini provinciali: **GeoJSON incorporato** (`LOCAL_PROVINCES_GEOJSON`) — nessuna richiesta di rete per i confini. Nessun `fetch` verso gist/terze parti.

---

## 9. HOSTING READINESS

| Hosting | Esito | Note |
|---|---|---|
| **Localhost (server statico)** | **PASS** | `http://127.0.0.1:8125/mri-light-1.0.0.2.html` (server node puro static-file) — tabella §3 |
| **Hosting statico generico** | **PASS** | L'app è un singolo file HTML, **0 rilevamenti di ambiente** (`location.hostname/protocol` ecc. = 0 occorrenze), **0 path relativi**, GeoJSON embedded: il test HTTP sopra è l'equivalente esatto di un hosting statico (`/`) |
| **GitHub Pages** | **PASS (equivalenticolo al test statico sopra)** | Serve solo HTTP statico dei file; nessuna Server-Side Logic. Raccomandazione: pubblicare l'intera cartella `Light-1.0.0.2` (file HTML + `docs/` + `data/`) e opzionalmente aggiungere `favicon.ico` per eliminare il 404 di console |
| **file://** | **PASS** | test separato §3 (funciona anche a doppio click senza server) |

---

## 10. VERSIONI PRECEDENTI — INTEGRITÀ (hash)

| File | SHA-256 | Stato |
|---|---|---|
| `releases/Light-1.0.0.1/mri-light-1.0.0.1.html` | `6ADEA1BC010987D68F14E9386F116B0526C1B62F6C2D4849A6D835920005260E` (1.635.173 byte) | **INTATTA** (identica alla copia di partenza di 1.0.0.2) |
| `releases/Light-1.0.0.0/mri-light-1.0.0.0.html` | `4C909BF7DE2DAACE0E5B068EA1EF88AEF25DA604DB22038160EE8776F9F90A41` | **INTATTA** |
| `releases/v10.52.27.0/mri10.52.27.0.html` | `1CFF8FC79031A3E2D75D4A46E8909B7065EF1BFC919668DD739DCC743123584E` | **INTATTA** (originale) |
| `releases/Light-1.0.0.2/mri-light-1.0.0.2.html` | `723F7339FA9F877BAAAB30BA40BE58FD7B1421154FD464802ECBB1698CA8DE05` (1.633.428 byte) | nuova versione (secondo pass di fix) |

---

## 11. SECOND PASS — FIX MIRATI (richiesti dal feedback utente)

### 11a. Radar Player bloccato su "⏳ Caricamento..." (root cause)
**Sintomo:** cliccando il player radar il bottone restava su "⏳ Caricamento..." e nella timeline non partiva alcuna sincronizzazione completa; console: `TypeError: Cannot set properties of null (setting 'innerText')` **e** pageerror Leaflet `Cannot read properties of null (reading '_fadeAnimated')` (conseguente).

**Catena:** dopo il UI cleanup di questa edizione l'elemento `#btn-lightning` **non esiste più**, ma `toggleRadarPlayer()` lo aggiornava **senza guardia** alle righe ~5526-5529 (`btnLight.innerText/style/classList`). Il TypeError interrompeva la sequenza di accensione del player → stato parziale (satellite ON, radar su DPC, ma bottone mai portato a "⏸️ Radar Player (ON)").

**Fix:** guardia `if (btnLight) { … }` attorno all'aggiornamento del bottone (rimosso) + guardia difensiva anche in `toggleLightning()`. Aggiornata anche l'etichetta di stato da "Sat24 MTG IT + Radar DPC + Fulmini Sat24 IT" (fonti non più usate) a "EUMETSAT IR + Radar DPC, timeline sincronizzata".

**Verifica (HTTP reale, Edge headless):** a t+3s bottone = "⏸️ Radar Player (ON)", `status-msg` corretto, **0 pageerror**; stop → bottone torna "▶️ Radar Player". Network: EUMETSAT WMS `view.eumetsat.int` 9×200; DPC manifest `radar-api.protezionecivile.it` 1×200; tile DPC S3 `s3-prod-dpc-radar-webp-cache…` 4×200 + 4×403 (slot futuri non ancora pubblicati sul bucket — comportamento del provider, gestito senza errori).

### 11b. Vista sfumata: gradiente con un solo modello (root cause)
**Sintomo:** in modalità DUAL (`dual_best_ecmwf`, default) la sfumatura mostrava un solo modello: i valori di rischio non erano mixati.

**Causa:** `renderContinuousOverlay()` colora ogni punto da `rawPointStores['dual_best_ecmwf']` che contiene **solo i record `best_match`** (il dato ECMWF per punto vive in `modelStores['ecmwf_ifs']`). Le province invece usavano già il merge risk-preserving (`getAggregateForZone` → `getDualMergedAggForZone` → `mergeModelAnalyses`, max conservativo).

**Fix (coerente con la decisione "max conservativo, come i poligoni"):** nel loop punto-per-punto della vista continua, se `isDualModelMode()` e `modelStores['ecmwf_ifs'][idx]` è presente → `agg = mergeModelAnalyses(agg, computeAggregateForStore(ecRec), 'best_match', 'ecmwf_ifs')`. Punti senza ECMWF (densify virtuali, fetch in corso) → fallback naturale sull'unico modello disponibile.

**Verifica (HTTP reale, su punto dual caricato):** per ogni campo il merged == **max dei due modelli** (es. rain 0 vs 0.1 → 0.1; wind 4.05 vs 4.55 → 4.55; cape 360 vs 1080 → 1080). `renderContinuousOverlay()` eseguito senza eccezioni, **0 pageerror**. Identica semantica dell'aggregato province.

### 11b-bis. Poligonale: zona a rischio sparita (feedback utente)
**Sintomo:** molte zone di rischio visibili sulla sfumatura NON comparivano sulla vista poligonale.

**Causa:** durante il fetch il collasso incrementale per provincia (`zonePreview`) usa **solo il best_match** (weatherStore chiave-punto); l'ECMWF entrava nei poligoni SOLO al merge finale (`assembleDualModelStores`), che però arriva tardi (fetch 257 punti). La sfumatura, invece, mixa già i due modelli per-punto dal mio fix precedente → disparità di visualizzazione.

**Fix:** in `getAggregateForZone`, il percorso `zonePreview` ora fonde anche il **worst-point ECMWF** disponibile (`worstPointForProvince(modelStores['ecmwf_ifs'], i)`) con la stessa logica max conservativa: poligonale == sfumatura a ogni istante del fetch (e resta identica al merge finale quando `dualAggCache` è pronto).

**Verifica (HTTP reale):** per le province con dati parziali, i campi della poligonale == **max dei merge per-punto** (rain/wind/gusts/cape/prob) e rischio poligonale == rischio del punto massimo del gradiente; **0 pageerror**.

### 11c. Basemap OSM da `file://` — comportamento browser (nessun banner)
**Diagnosi (confermata empiricamente):** da `file://` i browser **non inviano mai il Referer** verso URL http(s). `tile.openstreetmap.org` con Referer → tile reale ~36 KB; senza → tile placeholder ~6,9 KB (Varnish) e alcuni tile-server rispondono 403. Da HTTP(S) il Referer c'è → mappa OK.

**Esito:** nessuna soluzione client-side possibile senza proxy (vincolo del task): per la basemap completa servire via HTTP. Il banner informativo aggiunto in una prima revisione è stato **rimosso su richiesta** (il comportamento è documentato solo nel changelog/report).

### 11d. Sezione "Fonti & Metodologie" ripulita
Rimossi i riferimenti alle fonti **non più utilizzate** in questa edizione:
- Sat24 / Infoplaza e Fulmini Blitzortung/LightningMaps (sezione "Radar · Satellite · Fulmini" → "Radar · Satellite").
- ESRI (Dark/Satellite) dalle **Mappe base** (resta la sola OpenStreetMap).
- Embed Meteociel, Meteo&Radar, Windy, PRETEMP → sostituiti dal solo **embed radar DPC ufficiale** (`radar.protezionecivile.it`).
- Aggiornate anche l'attribution del footer (rimosso "Fulmini: © Blitzortung / LightningMaps" e WetterOnline) e la didascalia timeline ("Radar + Satellite IR", senza Fulmini).

**Restano elencate (e sono attive):** Open-Meteo/Modelli/Geocoding, Radar RainViewer/DPC/OPERA-LibreWXR, Satellite EUMETSAT, Limiti provinciali ISTAT, Allerte DPC, OpenStreetMap, embed DPC, Librerie, metodologie e letteratura (tutte computate dall'app).

### 11e. Satellite: genera lampeggio E animazione statica ("si vede sempre la stessa immagine" — feedback utente)
**Sintomo:** prima i layer satellitari EUMETSAT lampeggiavano; dopo il fix del warm (sotto) l'animazione risultava **congelata sulla stessa immagine**.

**Doppia root cause.**
1. **Warm-ahead mai riusato**: `buildEumetsatTileUrl` generava URL `…geoserver/wms?service=WMS&…`, mentre **Leaflet richiede `…wms?&service=WMS&…`** (`getParamString` antepone `&` al `?` della base). La cache HTTP chiava sull'URL esatto → i tile precaricati con `new Image()` non venivano MAI riusati → ogni frame ripartiva a freddo (~1,5–8 s) a 100 ms/frame (8x di default): lampeggio.
2. **Conferma frame inaffidabile anche a cache calda** (la fix sopra ha reso i tile istantanei e ha esposto il problema): Leaflet crea i tile del viewport **in modo sincrono dentro `addTo()`**, quindi gli eventi `tileloadstart`/`tileload` potevano sparare **prima** che il codice attachasse gli `.on(...)` → `satTilesStarted` restava 0 → `tryResolveEumetsatFromTiles` non passava mai → la conferma finiva sull'evento `load` del layer (~1 s) o sul timeout di 8 s → il **generation counter scartava quasi tutte le conferme** e `satelliteLayer` restava sull'ultimo frame buono, per sempre.

**Fix:**
- `wms?service=` → `wms?&service=` (1 carattere; verificato: match stringa-esatto 176/200 vs 0 prima).
- Conteggio tile con **fallback su `newLayer._tiles`** (se `tileloadstart` va perso) + **verifica immediata dei tile già `complete`** (cache hit) senza attendere eventi (`satTilesOk`/`satTilesDone` forzati).
- **PACER nel play**: `playStep` NON avanza finché il frame satellite corrente non è confermato (`satelliteLayer.options.time === satLastRequestedIso`), con hard-limit anti-stallo `SAT_ACK_HARD_LIMIT_MS` per gli slot con dati mancanti. Elimina la corsa generazione/conferma.
- **Guardia difensiva** `L.GridLayer.prototype._tileReady` (try/catch) contro il crash Leaflet 1.9.4 `Cannot read properties of null (reading '_fadeAnimated')` (tile che fa load/error dopo la rimozione del layer, e.g. 502/503 del WMS).

**Fix buchi/sfarfallio a ogni cambio frame (feedback diretto dell'utente):** prima il frame veniva promosso con `satTilesOk > 0` (basta un solo tile ok) e a opacità `0.75` → un frame a copertura parziale sostituiva quello integro (buchi visibili) e, sul basemap OSM chiaro, l'immagine IR scura al 75% "si spegneva" quando il vecchio frame sotto veniva rimosso (lampeggio percettivo a ogni advance). Ora il frame entra in onda **solo a copertura completa del viewport** (`satLayerCoverage`: ogni tile in `newLayer._tiles` con `el.complete && naturalWidth > 0`, comprensivo dei retry che Leaflet fa sui tile in errore) e a **opacità piena** (`FADE_TARGET = 1`). Se il provider non serve un frame integro, il player trattiene l'ultimo frame completo: assenza di buchi, assenza di sfarfallio, degradazione stabile.

**Fix fluidità per frame già scaricati (secondo feedback diretto: "ogni volta che appare un nuovo frame ci mette un po' a renderizzare"):** riallineato al **pattern storico del changelog** ("`crossfadeLayers` semplificato a instant-on + delayed-remove"): per i frame in cache **non viene eseguita alcuna sfumatura** — il nuovo frame va subito a piena opacità e il vecchio viene rimosso **dopo `SAT_OLD_REMOVE_MS = 200 ms`** (ritardo deterministico che garantisce che il nuovo sia già composto a schermo quando il vecchio svanisce; la rimozione immediata o a 1 rAF rimuoveva il vecchio prima che il nuovo fosse dipinto, specie con tile grandi → buco/lampeggio). Il timer è tracciato in `pendingSatTimers` e pulito allo stop. Inoltre il pacer del play è ora **event-driven**: `resolveEumetsatLoad` chiama `__kickSatPacer` appena il frame è confermato e il play avanza subito (prima c'era un poll fisso di 350 ms che aggiungeva attesa anche a cache calda). Il kick rispetta comunque il `frameDelay` della velocità scelta (`max(frameDelay, 25)`); il poll è ridotto a `SAT_ACK_POLL_MS = 120` solo come fallback per i frame non ancora pronti. Verificato in-browser con provider simulato sano: cadenza di ~1 frame per campione di 250 ms (circa 100–120 ms/frame), **maxHoles = 0**, **0 pageerror**.

**Verifica (HTTP reale, Edge headless):** match warm-vs-Leaflet 176/200; **durante il play `satelliteLayer.options.time` cambia frame a frame** (12:40→12:50→13:05→13:15→13:30→…), **maxHoles = 0** a ogni snap (nessun buco: il gate promuove solo frame a copertura completa), **0 pageerror** (verifica end-to-end ripetuta con provider simulato sano — risposte WMS intercettate con PNG valido — per svincolare il test dallo stato transitorio 503 di EUMETSAT: 12 switch di frame nel play, maxHoles 0, 0 pageerror). **Cadenza temporale del provider verificata** via GetCapabilities (`<Dimension name="time">…/PT15M</Dimension>`, ultimo slot 13:45Z): il WMS della pubblicazione reale è ogni ~15 min; le richieste a passi di 5 min vengono servite dal server col frame più recente disponibile → le coppie di slot consecutivi possono risultare identiche (13:23==13:28, 13:43==13:48), comportamento del provider, non un bug del player. Nota: EUMETSAT `view.eumetsat.int` ha risposto in modo intermittente (503/200 a macchia sul `time=` durante il periodo di verifica — codici anche a ripresa parziale per-tile), condizione provider transitoria: con il gate a copertura completa l'app resta stabile sull'ultimo frame integro invece di lampeggiare.

---

## 12. SINTESI DELLE MODIFICHE (file 1.0.0.2)

1. **Loader Leaflet** → caricamento bloccante `document.write` + checker fallback jsdelivr + `__leafletFailed`/`__leafletDisabled` (righe ~71-95).
2. **`APP_VERSION`** → `1.0.0.2`; entry `1.0.0.2` aggiunta in testa a `APP_CHANGELOG` (righe ~1485-1492).
3. **Defusing** `DPCRadarLayer` → `getDPCRadarLayerClass()` (righe ~5860-5892) + uso aggiornato (~6027).
4. **Rimozione controlli** (§5): pulsanti fulmini/pretemp/meteociel, selettore Stile Mappa, selettore Sorgente Fulmini, blocco embed Blitzortung, opzioni meteoradar/windy, opzioni infoplaza (satellite).
5. **Guardia null-safe** aggiunta su `btn-lightning` nella routine di reset cronologia (sprecede crash residuo impossibile).
6. **Fix radar player** (second pass): guardia `if (btnLight)` in `toggleRadarPlayer()` (+ guardia in `toggleLightning()`), etichetta stato aggiornata → bottone raggiunge "⏸️ Radar Player (ON)", 0 pageerror.
7. **Fix vista sfumata dual** (second pass): merge `mergeModelAnalyses(agg, aggECMWF, 'best_match','ecmwf_ifs')` nel loop punto-per-punto di `renderContinuousOverlay()` quando `isDualModelMode()` → gradiente max conservativo su entrambi i modelli.
8. **Fix poligonale dual** (second pass): in `getAggregateForZone` il percorso `zonePreview` fonde anche il worst-point ECMWF `modelStores['ecmwf_ifs']` → poligonale == sfumatura durante il fetch.
9. **Sezione Fonti & Metodologie** aggiornata (rimossi Sat24/Infoplaza, Blitzortung, ESRI, embed Meteociel/Windy/PRETEMP; restano le sole fonti attive) + attribution footer e didascalia timeline.
10. **Banner `file://`**: aggiunto e poi RIMOSSO su richiesta (limite browser documentato; servire via HTTP per la basemap).
11. **Fix satellite che lampeggia** (second pass): `buildEumetsatTileUrl` emette ora il formato identico a Leaflet (`wms?&service=…` invece di `wms?service=…`), così il warm-ahead WMS va in cache e i frame partono istantanei (verificato: match stringa-esatto 176/200 vs 0 prima).

**Checkout sintattico:** blocchi `<script>` estratti → `node --check` tutti OK (dopo ogni sessione di fix).

**Criteri di successo → tutti soddisfatti:** Leaflet disponibile prima di `initMap`; nessun "L is not defined"; mappa e polygoni visibili; toggle legittimi funzionanti; Sviluppo presente e funzionante; rimossi non approvati assenti; 0 errori JS critici; network pulito; test localhost + statico + file://; player radar operativo (bottone ON/OFF, 0 pageerror); vista sfumata E poligonale con merge dual max conservativo (entrambi i modelli); nessun banner; fonti documentate = fonti realmente attive; animazione satellite fluida (warm-ahead in cache, niente lampeggio); report redatto.