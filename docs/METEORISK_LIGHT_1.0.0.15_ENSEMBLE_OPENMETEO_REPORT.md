# METEORISK LIGHT 1.0.0.15 — REPORT MODULO ENSEMBLE OPEN-METEO (client-side)

**Versione:** 1.0.0.15 · **Data:** 2026-09-08 · **Tipo:** release (bump da 1.0.0.14)
**File applicativo:** `mri-light-1.0.0.15.html`

## Obiettivo
Il modulo **Ensemble** (spaghetti plots) era basato sulle **immagini remote PHP di
Meteociel** (SPAGHETTI_MODELS → `…_display.php`, `graphe_ens`, `modeles16.meteociel.fr`,
≤ 10 giorni, immagini grandi e non interattive). È stato **riscritto su base puramente
client-side** usando l'**API publica ensemble di Open-Meteo**, con grafici ORIGINALI su
canvas (zero librerie), caricamento ON-DEMAND, normalizzazione del payload, gestione
errori in italiano e attribuzione Open-Meteo. L'integrazione Meteociel è rimossa
**completamente dal modulo** (solo note storiche in changelog/docs).

**Scope intoccato:** pipeline MeteoRisk (formule compositi, palette, soglie, dataset),
GitHub Actions, radar player, satellite, fulmini, METAR, comparazione modelli,
indicatori scheda dettagli. Il modulo **"Carte modelli" Meteociel** (flag
`meteocielEmbed`, disattivato) è un modulo separato e resta intatto.

## Modifiche applicate (`mri-light-1.0.0.15.html`)

### A) Sorgente dati Open-Meteo Ensemble (client-side)
- `ENS_API_BASE = 'https://ensemble-api.open-meteo.com/v1/ensemble'` con **7 modelli**
  (codici UFFICIALI dell'API — parametro **`&models=`** al plurale, che e' OBBLIGATORIO;
  il singolare `&model=` viene IGNORATO dall'API e restituirebbe sempre il modello di
  default. Verificato live: i dati dei 7 modelli differiscono realmente). Membri rilevati
  dinamicamente per modello, tutti verificati con la richiesta completa delle 14 variabili
  dirette all'orizzonte massimo offerto:
  - `ecmwf_ifs025` — **ECMWF IFS ENS 0.25° (CEP)** — 50 membri (3/7/15 giorni)
  - `ecmwf_aifs025` — **ECMWF AIFS 0.25°** — 50 membri (3/7/15 giorni)
  - `icon_eu_eps` — **ICON-EU-EPS (DWD)** — 39 membri (1/3/5 giorni)
  - `icon_global_eps` — **ICON-EPS globale (DWD)** — 39 membri (3/5/7 giorni)
  - `ncep_gefs025` — **GEFS 0.25° (NOAA)** — 30 membri (3/7/10 giorni)
  - `ncep_gefs05` — **GEFS 0.5° (NOAA)** — 30 membri (7/15/35 giorni)
  - `ukmo_global_ensemble_20km` — **MOGREPS-G (Met Office UKMO)** — 17 membri (3/5/7 giorni)
  e `temperature_unit=celsius`, `timezone=Europe/Rome`.
- **PE ARPEGE (Météo-France) NON e' disponibile come ensemble su Open-Meteo** (Météo-France
  espone solo AROME/ARPEGE deterministici su un altro endpoint, senza membri): non offerto.
- **Coordinate dinamiche**: i selettori `latitude/longitude` provengono dallo stato
  dell'app (`customCityData` o `regionsData[selectedIndex]`) — mai hardcoded.
- Variabili richieste in **un'unica richiesta** (`hourly` = mappa di `ENS_VARS`,
  escluso il flag `derived`). Set **17 variabili**: 14 **dirette** —
  `temperature_850hPa`, `temperature_2m`, `temperature_500hPa`,
  `geopotential_height_500hPa`, `geopotential_height_850hPa`, `pressure_msl`,
  `wind_speed_10m`, `wind_gusts_10m`, `wind_speed_850hPa`, `relative_humidity_850hPa`,
  `cape`, `snow_depth`, `snowfall`, `precipitation` — più 3 **derivate calcolate in
  locale** (`derived: true`, mai inviate all'API): `freezing_level` (Iso 0°C),
  `theta_e_850` (ThetaE 850 hPa) e `cum_precipitation` (Cumul pioggia).
  Tutte le 14 dirette verificate live su **tutti e 7 i modelli** con `_memberNN`
  presenti (schema uniforme `_member01`..`_member{count}`: 50 IFS/AIFS, 39 ICON,
  30 GEFS, 17 MOGREPS — più la variabile base).

### B) Grafici ORIGINALI su canvas (zero librerie)
- `ensDraw()`: **DPR-aware** (devicePixelRatio + setTransform) e responsivo via
  `ResizeObserver` sul `.ens-chart-box`.
- **Spaghetti** (T 850 hPa / T 2 m / Z 500 hPa): linee dei membri + **media
  evidenziata** + **fascia spread p25-p75** (banda), griglia con ticks "belli"
  (`ensNiceTicks`), etichette locali `it-IT` (`ensParseT`/`ensFmtAxis`/`ensFmtLong`).
- **Barre** (precipitazioni): media per passo temporale + **linea di probabilità di
  pioggia** (>0.1 mm/h, calcolata sui membri) su asse destro 0–100%.
- **Tooltip mouse/touch** via Pointer Events (`ensOnPointer`/`ensClearHover`) con
  media/min/max/fascia/IQR e probabilità; legenda generata (`ensDrawLegend`).

### C) ON-DEMAND + dedupe + cache
- Il pannello è un **accordion chiuso all'avvio** (`#ens-body hidden`): **nessuna fetch
  prima dell'apertura** (guardia `if (!ensState.open) return` in `ensLoad`).
- Chiave di dedupe `model|days|lat|lon`: cambio località/modello/giorni → nuova fetch;
  cambio **solo variabile** → render da cache (`vSel` non fa mai fetch).
- **AbortController + timeout 25s**, risposta stale ignorata (check `ensState.key`),
  cache **solo session in memoria** (`ensState.cache`), mai persistente.

### D) Normalizzazione + membri dinamici
- `ensNormalize()` separa il provider dall'app: output
  `{key, model, location, metadata, times, vars}` con `{min,max,p25,p75}` calcolate
  dai membri.
- **Rilevamento dinamico dei membri** via pattern `^<var>_member(\d+)$` — nessun
  numero/posizione hardcoded (FASE 12).

### E) Errori user-friendly (italiano)
Offline, timeout, rate-limit (429), HTTP 404, 5xx, payload non valido, variabile
mancante — messaggi in italiano + **pulsante "Riprova"**, mai stack trace (solo log
console).

### F) Rimozione Meteociel dal modulo ensemble
Eliminati: `spaghettiLat/Lon/Ville`, `GEFS_IMG_SCRIPTS`, `SPAGHETTI_MODELS`,
`getLatestEnsRun`, `onSpaghettiModelChange`, `updateSpaghettiPlot`, URL/immagini/iframe
Meteociel, CSS `.spaghetti-*` (sostituito da CSS `.ens-*`), placeholder e statica
`.spaghetti-section`. Attribuzione "Dati ensemble: Open-Meteo.com · modelli ECMWF · ICON · GEFS · UKMO".

### G) Agganci all'app
- `updateUI()`: `ensSetLocation(lat, lon, subLabel || label || '')` (nessuna fetch a
  modulo chiuso).
- `startApp()`: `initEnsembleModule()` popola i selettori e aggancia gli eventi.
- `APP_VERSION = '1.0.0.15'`, nuova voce in `APP_CHANGELOG`, `VERSION` (BUILD=15,
  `APP_FILE=mri-light-1.0.0.15.html`, campo `ENSEMBLE1`), README e micro-fix Pages
  nel workflow aggiornati al nuovo filename.

## No-regression
Invariati: pipeline MeteoRisk (formule compositi SCP/EHI/WMAXSHEAR/ML-SHIP, palette,
soglie HSLS/CL, dataset derivati, timeline/slider, slot fascia), scheduler/player
1.0.0.14, SCIAUDIT 1.0.0.12/1.0.0.13, comparazione modelli, carte modelli Meteociel
(separate), flag `meteocielEmbed`.

## Test
Nuovo test strutturale **`scripts/tests/test_ensemble_module.mjs`** (**39 check**:
endpoint/config ENS_MODELS/ENS_VARS, on-demand+dedupe+timeout, normalizzazione+membri
dinamici, canvas DPR + band + tooltip + legend + attribuzione, errori/UX, rimozione
Meteociel, versione/changelog).

Suite `.mjs` completa: **10/10 PASS** (color_coherence, day_mapping, ensemble_module,
fascia_slots, forecast_slot_e2e, live_panel_gating, mobile_responsive,
player_fluidity, sciaudit, temporal_slider) + `contract_dataset_loader.mjs` PASS.
Syntax check del blocco `<script>` principale: OK (node --check).

## Verifica consigliata in-browser (manuale)
1. Aprire `mri-light-1.0.0.15.html` online (il modulo ensemble richiede rete).
2. Nella scheda dettagli, aprire "Ensemble (Open-Meteo)": appare il pannello e parte
   UNA sola richiesta all'API ensemble (nessuna fetch senza apertura).
3. Cambiare solo variabile → **nessuna nuova richiesta** (render da cache); cambiare
   modello (GEFS 0.25°/0.5°) o giorni → nuova richiesta.
4. Verificare tooltip mouse e touch su spaghetti (T 850 hPa/T 2 m/Z 500) e barre +
   probabilità pioggia (precipitazioni).
5. Off-line o modello/orizzonte remoto → messaggio in italiano + "Riprova".
6. Ridimensionare la finestra → il grafico si riadatta (ResizeObserver).