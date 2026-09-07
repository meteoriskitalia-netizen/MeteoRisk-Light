# MeteoRisk Light — Report A · Audit Responsive (HOTFIX4)

- Rilascio: 1.0.0.9 (HOTFIX4)
- Data: 2026-09-07
- File: `mri-light-1.0.0.9.html` (file canonico della release)
- Ambito: layout/rendering responsive su viewport mobili/tablet/desktop.
  Chiaramente escluso per vincolo di task: RADAR PLAYER come causa di problemi
  di layout (trattato nel Report B separato).
- Limite del fix: nessuna modifica a mapping giorno/dataset,
  palette/soglie/formule o algoritmi V1/V2/V3 (solo CSS di layout).

---

## 1. Metodo di verifica

1. **Suite strutturale** esistente `scripts/tests/test_mobile_responsive.mjs`
   (1.0.0.8, check CSS+JS per classe viewport): invariata, **PASS**.
2. **Probe live CDP** (headless Edge — Chrome DevTools Protocol):
   - 10 viewport emulati (portrait e landscape): 320×568, 375×667, 430×932,
     600×750, 667×375, 896×414, 768×1024, 1024×700, 1280×800, 1670×900.
   - Rete SSL bloccata (`Network.setBlockedURLs`) per render deterministico
     (niente fetch live/pacing): la prova misura il CSS di layout senza dipendere
     dai dati; `Runtime.exceptionThrown` contato (0 eccezioni a ogni viewport).
   - Per viewport si misuravano: overflow orizzontale pagina
     (`scrollWidth`−`clientWidth`), classe `app-*` su `<html>`/`<body>`,
     numero colonne del `.container`, rettangolo `#leaflet-map`
     vs attese per classe (mobile/tablet/desktop/landscape), `position` della
     barra riepilogo, `overflow-x` e scroll `.metric-tabs`.

Risultato baseline: **9/10 PASS, 1 FAIL — 320×568 con overflow di 53px**
(pagina scrollabile in orizzontale). Landscape 667×375/896×414 risultavano FAIL
solo per un errore di ATTESA della probe (48dvh con `min-height:240px` dominante:
il `min-height` fa prevalere 240px, comportamento corretto del CSS — vedi §4).

## 2. Causa radice dell'overflow a 320px

Catena esatta (misurata con `getBoundingClientRect` + `width:min-content` su
309 nodi):

1. `canvas#hourlyChart` nasce con attributi nativi `width="300"` (default del
   browser prima che Chart.js lo ridimensioni) → **min-content 300px**.
2. `.chart-box` (`padding:10px` + `border:1px`) → **min-content 322px**.
3. `.details-card` (flex column, grid-item con `min-width:auto`) → il pavimento
   min-content del figlio diventa il min-content della scheda: **348px**
   (322 + padding 24 + bordi 2).
4. `.container` grid `1fr` a colonna singola: il track eredita il min-content
   del grid-item → track **348px** contro i ~300px disponibili (320 − 2×10 body)
   → **53px di overflow orizzontale di pagina** (nessun `overflow-x:hidden`
   applicato al body dell'app).

Effetto: a 320px (e fino a ~368px, dove il pavimento 348 supera la larghezza
disponibile) la pagina scrolla in orizzontale; a 375px in su il pavimento
rientra e il difetto era invisibile — per questo non era emerso nei test
strutturali.

**Secondo overflow latente** (coperto a valle dello stesso fix): il
`select#spaghetti-model` (opzione più lunga "ECMWF 0.25° (CEP) — Runs complets",
~280px di min-content) e i select della sezione modello superavano la colonna
(~254px a 320px di contenuto utile) anche dopo aver rimosso il pavimento della
scheda.

## 3. Fix applicato (solo HTML/CSS — blocco `@media (max-width: 767px)`)

```
PRIMA (mobile block):
  .details-card { padding: 12px; }
  .map-card { padding: 10px; border-radius: 10px; }

DOPO (mobile block, aggiunte):
  .map-card, .details-card { min-width: 0; }      /* 1 */
  .chart-box canvas { max-width: 100%; height: auto; }  /* 2 */
  .details-card select { max-width: 100%; }          /* 3 */
```

Effetti:
1. `min-width:0` sui grid-item → il `min-content` dei figli non pilota più la
   larghezza della colonna (la scheda si adatta alla colonna, non viceversa).
2. Il canvas si cappa alla larghezza del box; Chart.js è `responsive:true`
   + `maintainAspectRatio:false` → si ridimensiona da solo (nessuna distorsione).
3. I select lunghi si cappano alla colonna (box chiuso troncato; il dropdown
   mostra per intero le opzioni — pattern standard mobile).
   Note: i select della toolbar mappa vivono in `.map-toolbar-scroll`
   (overflow-x) e NON sono toccati dal fix (restano scrollabili sulla barra).

## 4. Verifica post-fix (rendering reale, headless)

| Viewport | Classe | Colonne | Mappa | Overflow | Esito |
|---|---|---|---|---|---|
| 320×568 | app-mobile | 1 | 263×318 (56dvh ok) | 0px | PASS |
| 375×667 | app-mobile | 1 | 318×374 | 0px | PASS |
| 430×932 | app-mobile | 1 | 373×522 | 0px | PASS |
| 600×750 | app-mobile | 1 | 543×420 | 0px | PASS |
| 667×375 (land) | app-mobile | 1 | 618×240 (48dvh→min240) | 0px | PASS |
| 896×414 (land) | app-tablet | 1 | 847×240 (48dvh→min240) | 0px | PASS |
| 768×1024 | app-tablet | 1 | 699×614 (clamp 380-640) | 0px | PASS |
| 1024×700 | app-tablet | 2 | 539×420 | 0px | PASS |
| 1280×800 | app-desktop | 2 | 754×560 | 0px | PASS |
| 1670×900 | app-desktop | 2 | 754×560 | 0px | PASS |

→ **10/10 PASS, 0 eccezioni JS.** Nota landscape: la media
`orientation:landscape && max-height:520` porta la mappa a `48dvh` ma il
`min-height:240px` prevale per viewport <500px di altezza (240px sono sopra il
48% di 375): comportamento voluto del CSS (mappa mai <240px anche in landscape).

Suite strutturale post-fix: `contract_dataset_loader`, `test_color_coherence`,
`test_day_mapping`, `test_fascia_slots`, `test_forecast_slot_e2e`,
`test_live_panel_gating`, `test_mobile_responsive`, `test_temporal_slider`
→ **8/8 PASS**; `node --check` JS inline → OK.

## 5. Coerenza con la convenzione release

- `VERSION`: riga `HOTFIX4=OVERFLOW ORIZZONTALE 320-368px (audit responsive)…`
  aggiunta dopo HOTFIX3.
- `APP_CHANGELOG` (voce 1.0.0.9): entry `HOTFIX OVERFLOW ORIZZONTALE 320-368px`
  con causa/fix/verifica. Nessun identificatore riservato nei testi
  (`rasterZoneField`, `cell.zone`, `computeV3CellValues`) — `test_color_coherence`
  PASS.
- Nessuna modifica a mapping giorno/dataset, palette/soglie/formule, V1/V2/V3.

## 6. Elementi rimasti volontariamente alla verifica manuale

1. **Rotazione live / resize dinamico**: il flusso `resize/orientationchange →
   onViewportChanged → scheduleInvalidate → map.invalidateSize()` (debounced) è
   già coperto dal test strutturale P11; la misura qui è a load per viewport.
   Consigliata una rotazione fisica su dispositivo vero (la safe-area
   `env(safe-area-inset-*)` è già in CSS su body/risk-summary/caletta-popup).
2. **Verifica tattile su 320px**: touch target (--touch-min 44px) già imposti;
   overlap dei corner-switch col bottone "Legenda badge" e della barra
   riepilogo sticky sono da cross-check visivo (la scheda a 320px è ora 263px
   utili al posto di 348).
3. **Font iOS**: `.search-input font-size:16px` (anti-zoom) già in posto.
4. **Ultra-narrow <320px**: fuori dal range supportato dichiarato (breakpoint
   entry-level 320px); a <320 la colonna resta scrollabile — scelta documentata,
   nessun `overflow-x:hidden` mascherante aggiunto.

## 7. Esito

L'audit responsive conferma che le priorità 1.0.0.8 (mappa dominante dvh, touch
44px, legende singola colonna, forecast-slot nel viewport, safe-area, popup nei
limiti) sono già implementate e verificabili; l'UNICO difetto reale emerso dal
rendering live è l'overflow orizzontale a 320-368px, corretto (HOTFIX4) con 4
righe CSS nel blocco mobile, comprovato 10/10 viewport a overflow 0. Il Radar
Player è escluso da questo report per vincolo di task (vedi Report B).