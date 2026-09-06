# METEORISK LIGHT — DATA ARCHITECTURE & MODEL UPDATE AUDIT

> **⚠ SUPERSEDED (1.0.0.7 — Coordinated Scheduling).** Questo documento è un
> **audit storico** della baseline 1.0.0.2/1.0.0.3: il contenuto resta valido
> come registrazione architetturale e NON è operativo per la pipeline corrente.
> Dalla **1.0.0.7** il ciclo è comandato dal **driver ECMWF IFS** (check leggero,
> Metadata API) e il segmento **Best Match** è aggiornato **coordinato** con il run
> ECMWF nello stesso ciclo di fetch. I riferimenti storici a `italia_meteo_arpae_icon_2i`
> presenti in questo documento appartengono al periodo precedente e NON devono essere
> usati come guida operativa (per la pipeline operativa aggiornata: `CENTRALIZED_DATA_PIPELINE.md`).

**Versione analizzata:** MeteoRisk Light 1.0.0.2 (`releases/Light-1.0.0.2/mri-light-1.0.0.2.html`, SHA-256 `1DBC7757…07E5`)
**Versione audit:** MeteoRisk Light 1.0.0.3 (`releases/MeteoRisk-Light-1.0.0.3-Data-Architecture-Audit/`)
**Data analisi:** 2026-09-05
**Natura:** *Architectural Audit Release* — nessuna modifica funzionale alla logica applicativa. Versione 1.0.0.2 preservata intatta (STABLE BASELINE / PRE-AUDIT VERSION).

---

## 0. REGOLE RISPETTATE

- ✅ Nessun file applicativo della versione 1.0.0.2 modificato
- ✅ Nessun cambio logico in 1.0.0.3 (solo version bump + changelog)
- ✅ Audit esclusivamente tecnico/architetturale: nessuna implementazione
- ✅ L'eventuale implementazione verrà decisa solo dopo revisione manuale di questo report

---

## 1. EXECUTIVE SUMMARY

1. **Il collo di bottiglia è Open-Meteo forecast**, non i radar/satellite: all'avvio la mappa esegue `fetchWeatherData()` (riga 16625) che, in modalità DUAL (default), scarica **1 richiesta per punto reale** di campionamento. Con 107 province e 1-4 punti per provincia il totale reale va da **~107 a ~321 coordinate** (default auto, reticolazione v2; i 220 punti *virtuali* di densificazione non fanno rete).
2. **"Decine di richieste" = il normale comportamento dell'app**: la coda con pacer a 400 ms fa uscire una richiesta ogni 0,4 s; nei primi secondi se ne osservano 9-12, ma il ciclo completo è **decine-centinaia di richieste** (107-321 forecast) + retry (2 tentativi) + attese 429.
3. **Il set di coordinate è deterministico e contenuto** (107 capoluoghi + interni); viene però **riscaricato integralmente** a: apertura, cambio modello, pulsante "🔄 Aggiorna", e — parzialmente — a ogni giorno/ora se la cache non copre (i payload sono già per giorno, quindi il cambio giornata non richiede retry).
4. **Duplicazioni residue**: (a) `current` conditions riscaricate su più trigger senza cache condivisa; (b) il pannello "Confronto ECMWF vs GFS" esegue 4 nuove richieste senza riusare i dati già in `modelStores`; (c) stesso modello scaricato di nuovo se l'utente cambia modalità senza cache modelStores valida.
5. **Esiste un metodo ufficiale e gratuito di run-detection**: la **Metadata API di Open-Meteo** (`https://api.open-meteo.com/data/{model}/static/meta.json`) espone `last_run_initialisation_time`, `last_run_availability_time`, `update_interval_seconds`. Le chiamate a questa API **non contano nei limiti** di utilizzo (documentato). Identificatore univoco del run = `last_run_initialisation_time`.
6. **Nessun run-id nel payload forecast**: la risposta `/v1/forecast` non espone l'initialisation time del run. La detection va fatta esclusivamente via metadata API (probato: `ecmwf_ifs`, `ncep_gfs013`, `dwd_icon`, `dwd_icon_eu`, `italia_meteo_arpae_icon_2i` tutti raggiungibili).
7. **Architettura target fattibile**: GitHub Action schedulata → check metadata → confronto run → (solo se nuovo) fetch multi-location in batch → collasso JSON → validazione → commit → GitHub Pages. Il browser passa da **~107-321+ richieste** a **1-3 JSON statici** (decine di KB).
8. **Non tutto va su GitHub**: radar (RainViewer/LibreWXR/DPC), satellite EUMETSAT, geocoding, basemap e condizioni `current` vanno tenuti **live**; METAR IEM è candidato a cache centrale breve (obs, non modello).
9. **Licenza Open-Meteo**: free tier *non commerciale* + **CC-BY 4.0** esplicito nei termini (quindi redistribuzione ammessa con attribuzione), limiti 600/min · 5.000/ora · 10.000/giorno · 300.000/mese. Permane un'area da **verifica manuale**: uso di GitHub Actions come backend centralizzato (bulk) e "spirito" del ToS (riserva il diritto di bloccare abusi).
10. **Documentazione condivisa con la versione precedente** (repair report) e nuove metriche: la tabella §12 stima una riduzione del ~97% delle richieste meteo al browser e l'eliminazione pratica del rischio rate-limit lato utente.

---

## 2. CURRENT DATA FLOW

```text
   UTENTE apre l'app (mri-light-1.0.0.2.html)
         │
         │  boot: Leaflet (unpkg/jsdelivr) + OSM basemap tiles
         │  READY: fetchWeatherData()  ← AIVVIO (riga 16625)
         ▼
   ┌─────── fetcher Open-Meteo ───────┐
   │  coda con priorità (enqueueApi)   │
   │  pacer 400ms · gap 350ms          │
   │  budget 560/min (finestra 60s)    │
   │  retry×2 · backoff · 429→attesa60s│
   └───────┬───────────────────────────┘
           ▼   PER OGNI punto reale (107..~321, DUAL:
               models=best_match,ecmwf_ifs · 49 hourly + 6 daily · forecast_days=3)
      api.open-meteo.com/v1/forecast?lat=..&lon=..
           ▼   splitDualModelResponse → weatherStore + rawPointStores
           ▼   refreshZonePreview (collasso per provincia, best_match + ECMWF merge)
           ▼   densificaVirtualPoints (+220 virtuali, NO rete) → sfumatura IDW
           ▼   updateMapColors / renderContinuousOverlay
         │
         ├── interazione → richieste extra:
         │     · geocoding-api (ricerca comune)          [fetchDirectJson]
         │     · fetchOmCurrent  (LIVE panel/caletta)    [forecast ?current=]
         │     · confronto ECMWF/GFS (4 richieste)       [forecast models=]
         │     · selectCity (1 forecast singola)         [fetchDirectJson]
         │
         ├── dati LIVE (solo su apertura):
         │     · radar: api.rainviewer.com / api.librewxr.net / radar-api.protezionecivile.it (manifest) + tile
         │     · satellite: view.eumetsat.int WMS GetMap (tile per frame + warm-ahead×4)
         │     · METAR: mesonet.agron.iastate.edu (1 per aeroporto, cache 10') 
         │     · allerte DPC: api.github.com commits + raw.githubusercontent.com TopoJSON
         │     · embed radar DPC: iframe radar.protezionecivile.it
         │
         ▼
   UTENTE vede la mappa colorata. Dati NON salvati localmente
   (caches session-level: iemCache, dpcJsonCache, modelStores, API budget in memoria)
```

Punti chiave del flusso attuale:

- Il download modelli è **client-side, a ogni apertura, con tutte le variabili** (49 hourly), anche se l'utente guarda solo 3 giorni e una manciata di metriche.
- Il collasso per giorno avviene nel browser (`getHourOrDailyData`, `computeAggregateForStore`, `assembleDualModelStores`, `computeDayMaxForStore`): **tutto questo lavoro può essere anticipato al build-time** senza cambiare i valori (input deterministici).
- Non esiste persistenza: `weatherStore`, `modelStores`, `rawPointStores` e le cache di riepilogo sono solo in memoria; un refresh browser ripete tutto.

---

## 3. COMPLETE API INVENTORY

Categorie: **A** — Precaricabile su GitHub · **B** — Aggiornabile periodicamente · **C** — Dipendente da model run · **D** — Real-time / live · **E** — Non necessaria / ridondante.

### 3.1 Fonti ATTIVE in 1.0.0.2 (feature flag `true`)

| Fonte | Endpoint | Metodo | Quando | N. richieste | Tipo dati | Cat. |
|------|----------|--------|--------|-------------:|-----------|------|
| Open-Meteo Forecast | `api.open-meteo.com/v1/forecast` | GET | all'avvio (16625), cambio modello, "🔄 Aggiorna" | **107..~321** (1 per punto reale, DUAL) + retry/429 | JSON (hourly 49 + daily 6, 3 gg, 2 modelli) | **A/C** |
| Open-Meteo Current | `api.open-meteo.com/v1/forecast?current=…` | GET | LIVE panel / caletta / estratto modello | 1 per apertura + ogni 5' mentre il pannello è aperto | JSON (14 variabili current) | **D** |
| Open-Meteo Confronto | `api.open-meteo.com/v1/forecast` (`models=ecmwf_ifs` / `gfs_seamless`) | GET | apertura pannello "Confronto" | 4 (2 daily + 2 hourly) per apertura | JSON | **A** (deduplicabile) |
| Open-Meteo Comune | `api.open-meteo.com/v1/forecast` | GET | `selectCity()` | 1 per comune selezionato | JSON (stesse variabili, 3 gg) | **A** |
| Open-Meteo Geocoding | `geocoding-api.open-meteo.com/v1/search` | GET | ricerca comune (debounce) | 1 per ricerca | JSON (risultati, count=5) | **D** |
| OSM Basemap | `tile.openstreetmap.org/{z}/{x}/{y}.png` | GET | sempre | ~8-20 tile per viewport | PNG raster | **D** / E |
| Leaflet CDN | `unpkg.com/leaflet*` / `cdn.jsdelivr.net/leaflet*` | GET | boot | 2-3 (css+js+markers) | CSS/JS/PNG | **E** (self-host in futuro) |
| Chart.js CDN | `cdn.jsdelivr.net/chart.js` | GET | apertura grafico (lazy) | 1 | JS | E |
| RainViewer manifest | `api.rainviewer.com/public/weather-maps.json` | GET | apertura radar/timeline/LIVE | 1 per apertura | JSON (host+frames) | **D** |
| RainViewer tile | `{host}/v2/radar/{path}/256/{z}/{x}/{y}/2/1_1.png` | GET | player radar per frame | n_tile × n_frame | PNG | **D** |
| LibreWXR manifest | `api.librewxr.net/public/weather-maps.json` | GET | radar selezionato (fallback→RainViewer) | 1 | JSON | **D** |
| LibreWXR tile | `{host}…/256/{z}/{x}/{y}/2/1_1.png` | GET | player radar | n_tile × n_frame | PNG | **D** |
| DPC radar REST | `radar-api.protezionecivile.it/findLastProductByType?type=VMI` | GET | radar DPC | 1 per apertura | JSON (ultimo prodotto) | **D** |
| DPC radar tile | `s3-prod-dpc-radar-webp-cache…/{PROD}/…/{z}/{x}/{y}.webp` | GET | player radar DPC | n_tile × n_frame | WebP | **D** |
| EUMETSAT WMS | `view.eumetsat.int/geoserver/wms?&service=WMS&…GetMap&time=…` | GET | player satellite / LIVE satellite | per frame: tile viewport + warm-ahead×4 (probe: 32 richieste su un play completo) | PNG | **D** |
| METAR IEM | `mesonet.agron.iastate.edu/json/current.py?station=…&network=IT__ASOS` | GET | LIVE panel/caletta | 1 per aeroporto (cache 10') + refresh 5' | JSON (METAR) | **B** (cache centrale opz.) |
| Allerte DPC | `api.github.com/repos/pcm-dpc/…/commits?path=files/topojson` + dettaglio commit + `raw.githubusercontent.com/…/files/topojson/{ts}_today.json` | GET | toggle "Allerte DPC" | 1 commit-list + ≤3 dettagli + 1-2 TopoJSON (cache 10') | JSON (bollettini criticità) | **A/B** |
| Embed radar DPC | `radar.protezionecivile.it` (iframe) | GET | apertura embed | 1 (page + subresource sconosciuti) | HTML | **D** |

### 3.2 Fonti DISATTIVATE (flag `false`) — nessuna richiesta emessa

| Fonte | Endpoint | Stato |
|------|----------|-------|
| Fulmini Blitzortung live | `wss://{server}.blitzortung.org/` + tile raster | flag `lightningBlitzortung=false` |
| Fulmini storico Limaps | `www.limaps.org/Maps/History/…/image_b_*.png` | flag `lightningLimaps=false` |
| Sat24 / Infoplaza | `imn-rust-lb.infoplaza.io` (tile satellite) | flag `satelliteSat24=false` |
| Basemap ESRI | `server.arcgisonline.com` (Dark/Imagery) | flag `esriBasemap=false` |
| Meteociel (carte modelli) | `www.meteociel.fr`, `modeles2/16/12.meteociel.fr` | flag `meteocielEmbed=false` |
| PRETEMP | `pretemp.it` | flag `pretempEmbed=false` |
| Windy / Meteo&Radar (wo-cloud) | `embed.windy.com`, `radar.wo-cloud.com` | flag `radarWindyEmbed=false`, `radarMeteoRadarEmbed=false` |
| METAR fallback | `mesonet.agron.iastate.edu` (feature `metarIem`) | ATTIVO (v. §3.1) |

> Fonti CDN/documentazione nel codice ma non emesse a runtime: `w3.org`, `open-meteo.com`, `github.com` (link), `openstreetmap.org` (attribuzione), `dpc-radar.readthedocs.io`, `protezionecivile.gov.it`, `eumetsat.int`, `rainviewer.com`, `librewxr.net`, `librewxr.api` (mappa dei provider).

**Nota metodologica:** il conteggio "*richieste*" per i layer tile (radar/satellite) dipende da zoom e numero di frame; nella colonna è indicato l'ordine di grandezza per il comportamento tipico (probe 1.0.0.2: EUMETSAT 32 WMS su un play; DPC 4 tile 200 + 4 slot futuri 403).

---

## 4. OPEN-METEO ANALYSIS (DEEP AUDIT)

### 4.1 Endpoint / trasporto

| Ruolo | URL | Coda | Note |
|------|-----|------|------|
| Forecast free | `https://api.open-meteo.com/v1/forecast` | **sì** (enqueueApi, priorità, gap 350ms, budget 560/min) | endpoint di default (`getApiBaseUrl()`, riga 4256) |
| Forecast a chiave | `https://customer-api.open-meteo.com/v1/forecast` | sì | attiva solo se fonte `openmeteo_key` + chiave inserita (Sviluppo) |
| Geocoding | `https://geocoding-api.open-meteo.com/v1/search` | **no** (`fetchDirectJson`) | fuori dalla coda forecast |

### 4.2 Coordinate

- **107 capoluoghi** in `regionsData` (riga 3102; conteggio verificato: 107).
- **Punti interni** per provincia: 1-4 in base ad area e classe orografica (`buildProvinceSamplesV1/V2`), redazione estesa se `samplingCoordCount` in Sviluppo è forzato.
- Default 1.0.0.2: **reticolazione v2 ON** (`reticulationV2=true`) → stessa conta di V1 ma posizionamento farthest-point con densità adattiva.
- Totale punti REALI: **da 107 a ~320-330** a seconda di area/orografia (documentato nel codice come "107-321"; changelog storico: 318-321 punti).
- + **220 punti virtuali** (densificazione, `target_virtual_points: 220`) — **zeri** richieste di rete (IDW lato browser).
- Una **richiesta = un punto** nel percorso DUAL (loop `fetchDualModel`, 1 per punto); nel percorso singolo esistono anche batch multi-location (FASE 1 capoluoghi, chunk da 20, poi a 10 per il frazionamento adattivo `fetchOmAdaptive`).

### 4.3 Modelli selezionati

| Id selettore | Parametro API | Note |
|--------------|---------------|------|
| Dual (DEFAULT) | `models=best_match,ecmwf_ifs` | una richiesta restituisce entrambi i modelli (split `_best_match`/`_ecmwf_ifs`), poi merge risk-preserving (max conservativo) |
| Best Match | `models` vuoto (default API) | composito: per l'Italia = ARPAE ICON-2I 2 km (g 1-3) + ICON-EU 7 km (g 4-5) + ICON Global 11 km |
| ECMWF IFS | `models=ecmwf_ifs` | IFS HRES 9 km |
| GFS | `models=gfs_seamless` | GFS interoperate 13 km |
| ARPAE ICON-2I | `models=italia_meteo_arpae_icon_2i` | 2 km, sola Italia |

### 4.4 Variabili e orizzonte

- **Hourly (49):** `temperature_2m, relativehumidity_2m, dew_point_2m, pressure_msl, cape, precipitation_probability, windspeed_10m/winddirection_10m, windspeed_100m/winddirection_100m, windgusts_10m, weathercode, precipitation, showers, freezing_level_height` + 14 livelli pressori (wind_speed/direction a 1000/975/950/925/900/850/800/700/600/500 hPa) + `temperature_850/700/500hPa` + `relative_humidity_850/700hPa` + `dew_point_850/700hPa` + `geopotential_height_850/700/500hPa` + `convective_inhibition + lifted_index + k_index` (HOURLY_PARAMS, riga 4448).
- **Daily (6):** `weathercode, temperature_2m_max, temperature_2m_min, precipitation_sum, windspeed_10m_max, precipitation_probability_max`.
- **Current (14):** `temperature_2m, relative_humidity_2m, apparent_temperature, is_day, precipitation, rain, weather_code, cloud_cover, pressure_msl, surface_pressure, wind_speed_10m, wind_direction_10m, wind_gusts_10m, dew_point_2m`.
- **Orizzonte:** `forecast_days=3` per la mappa (payload ridotto, ~57% del caso a 7 giorni); `timezone=Europe/Rome`.
- Modelli confronto: daily ridotto (4 campi) + hourly ridotto (5 campi) per `ecmwf_ifs` e `gfs_seamless`.

### 4.5 Tabella sintetica

| Coordinate/Area | Modello | Endpoint | Variabili | Frequenza attuale | Precaricabile |
|-----------------|---------|----------|-----------|-------------------|---------------|
| 107 capoluoghi + interni (107..~321) | best_match + ecmwf_ifs (dual) | `/v1/forecast` | 49 hourly + 6 daily, 3 gg | a ogni apertura / cambio modello / refresh | **SÌ** |
| Stesse coordinate | best_match / ecmwf_ifs / gfs_seamless / icon-2i (singolo) | `/v1/forecast` | idem | su selezione modello non in cache | **SÌ** |
| Località LIVE (pannello) | best_match (current) | `/v1/forecast?current=` | 14 current | apertura pannello + ogni 5' | solo `current` → **parziale** |
| Comune cercato | best_match/dual | `/v1/forecast` | idem, 3 gg | su selezione comune | **SÌ** |
| Località confronto | ecmwf_ifs + gfs_seamless | `/v1/forecast` | 4+4 daily/hourly ridotti | apertura confronto | **SÌ** |
| Ricerca comune | — (geocoding) | `/v1/search` | toponimi | per ricerca utente | **NO** (live) |

### 4.6 DOMANDA OBBLIGATORIA — perché "decine di richieste" nella versione precedente?

Risposta in ordine di peso:

1. **Una richiesta per coordinata reale (cause principale).** Il campionamento multi-punto genera 107 capoluoghi + punti interni (fino a ~320-321 reali). Nel percorso DUAL il fetch è **1 richiesta per punto** → cifre **decine→centinaia**, non singole. La coda/pacer (400 ms) li distribuisce nel tempo, ma il totale non cambia.
2. **Modelli multipli nello stesso punto.** Il DUAL chiede `best_match + ecmwf_ifs` simultaneamente: servita da una sola richiesta HTTP ma con peso doppio (2 modelli → payload doppio) e costo di elaborazione doppio; su scelta modello o cambio giorno senza cache si ripete.
3. **Duplicazioni applicative.**
   - `current` (`fetchOmCurrent`) sbloccata su più trigger (LIVE panel, caletta, `renderModelExtract`, `selectCity`) con caches **non condivise** → stessa località scaricata più volte.
   - Pannello **Confronto ECMWF vs GFS** = 4 richieste nuove anche quando `ecmwf_ifs` è già in `modelStores` dalla mappa (payload diversi, ma stessa coppia di modelli).
   - Cambio modalità/refresh ("🔄 Aggiorna") rilancia `fetchWeatherData()` per TUTTE le coordinate (parzialmente mitigato da `modelStores`/sampleCache layer store point? verificato: la cache evita solo il ri-fetch se `modelCacheUsable(model)` è true).
4. **Retry e rate-limit.** Ogni tentativo fallito raddoppia (retry×2 con backoff 300/600 ms); un 429 fa attendere 60 s e ritentare; il timeout server costringe lo split in batch sempre più piccoli (`fetchOmAdaptive` `20→10→1`), ogni split = più richieste.
5. **NON è colpa di "comuni multipli":** la geocoding è una richiesta per ricerca utente, esterna alla coda. **NON di "province multiple" intese come duplicati concettuali**: le province sono 107 fisse; la duplicazione vera è per-punto extra (interni) e per-trigger.

---

## 5. MODEL RUN DETECTION

### 5.1 Metodo ufficiale consigliato — Metadata API Open-Meteo

- **Endpoint:** `https://api.open-meteo.com/data/{model}/static/meta.json`
- **Chiamate NON conteggiate** nei limiti giornalieri/mensili (documentato nella pagina Model Updates).
- **Campi utilizzabili:**
  - `last_run_initialisation_time` (unix) → **identificatore univoco del run**
  - `last_run_modification_time` (unix) → termine elaborazione
  - `last_run_availability_time` (unix) → disponibilità sul server
  - `update_interval_seconds` → cadenza prevista
  - `temporal_resolution_seconds` → granularità nativa
- **Ritardo consigliato dopo la disponibilità:** **≥10 minuti** (Open-Meteo usa server ridondanti con consistenza eventuale; wait raccomandata dalla doc prima di consumare il run nuovo).

Identificatori verificati oggi (probe reale, tutti `HTTP 200`):

| Model id | Init run (verificato) | `update_interval_seconds` | `temporal_resolution_seconds` |
|----------|----------------------|--------------------------:|------------------------------:|
| `ecmwf_ifs` (IFS 9 km App) | 1788588000 (= 06Z) | 21600 (6 h) | 3600 |
| `ecmwf_ifs025` (IFS 0.25°) | 1788588000 | 21600 | 10800 |
| `ncep_gfs013` (GFS) | 1788588000 | 21600 (6 h) | 3600 |
| `dwd_icon` (ICON global) | 1788588000 | 21600 (6 h) | 3600 |
| `dwd_icon_eu` (ICON-EU) | 1788598800 | 10800 (3 h) | 3600 |
| `dwd_icon_d2` (ICON-D2) | 1788609600 | 10800 (3 h) | 3600 |
| `italia_meteo_arpae_icon_2i` (ICON-2I) | 1788609600 | 43200 (**12 h**) | 3600 |

> Nota: `best_match` **non ha** `meta.json` (composito non esposto come dataset). Poiché l'app scarica `forecast_days=3` e per l'Italia il primo segmento del Best Match è **ARPAE ICON-2I** (aggiornamento 12h), la detection del flusso "mappa 3 giorni" deve usare come driver `italia_meteo_arpae_icon_2i` (e opzionalmente `ecmwf_ifs` per il ramo DUAL).

### 5.2 Tabella per modello usato da MeteoRisk Light

| Modello | Fonte | Run frequency | Metodo detection | Identificatore run |
|---------|-------|---------------|------------------|--------------------|
| Best Match composito (default) | Open-Meteo (ICON-2I→ICON-EU→ICON global) | 12 h per il segmento driver IT | metadata `italia_meteo_arpae_icon_2i` | `last_run_initialisation_time` (utc) |
| ECMWF IFS (`ecmwf_ifs`) | Open-Meteo/ECMWF | 6 h (00/06/12/18Z; incrocio con la doc "every 6 hours") | metadata `ecmwf_ifs("025")` | `last_run_initialisation_time` |
| GFS (`gfs_seamless`) | NOAA via Open-Meteo | 6 h (00/06/12/18Z) | metadata `ncep_gfs013` | `last_run_initialisation_time` |
| ARPAE ICON-2I (`italia_meteo_arpae_icon_2i`) | ItaliaMeteo/ARPAE | **12 h** (= 2 run/giorno) | metadata `italia_meteo_arpae_icon_2i` | `last_run_initialisation_time` |

Flow di detection (NON implementato qui, solo documentato):

```text
GitHub Action (cron, es. ogni 15')
  └─ GET api.open-meteo.com/data/{model}/static/meta.json   (non conteggiato)
       └─ if last_run_initialisation_time != run memorizzato
             NO → STOP
             SÌ → wait 10' dopo last_run_availability_time
                  → download (v. §8) → build JSON → commit
```

---

## 6. PROPOSED GITHUB ARCHITECTURE

```text
      GitHub Actions  (repository MeteoRisk)
      ┌────────────────────────────────────────────────┐
      │  Scheduled workflow (cron "*/15 * * * *")       │
      │  1. Fetch metadata API per modello driver        │
      │  2. Confronta con run dell'ULTIMO DATASET         │
      │     (dato/latest/metadata.json nel repo)          │
      │  3. Se run nuovo:                                │
      │     a. wait availability + 10'                   │
      │     b. fetch forecast multi-location in batch    │
      │        (chunk ≤10 coordinate, sleep tra chunk)   │
      │     c. collasso & indici (v. §7)                 │
      │     d. validazione (schema+sanity, v. §8)        │
      │     e. commit automatico su main/branch data     │
      │     f. deploy GitHub Pages (actions/deploy-pages)│
      │  4. Log + artefatto diagnostico                  │
      └────────────────────────────────────────────────┘
                 │  push di file statici
                 ▼
      GitHub Pages  https://user.github.io/meteorisk/
                 │  GET dati/latest/*.json  (1-3 file)
                 ▼
      MeteoRisk Light nel browser: NESSUNA richiesta Open-Meteo
```

Vantaggi: zero infra da gestire; hosting già "gratuito/statico"; il commit automatico funge da audit trail irreversibile (ogni dataset è versionato); Pages serve risorse con cache HTTP standard; un solo account per Action+Pages.
Limiti noti: budget minutes Actions (pubblico: 2.000 min/mese sul tier free — più che sufficienti per poche decine di run), 1.024 MB di spazio artefatti, e la rete del runner **non** ha vincoli CORS lato GitHub→Open-Meteo (sta al server); eventuale IP pool condiviso → tenere dentro i limiti (batch + sleep).

---

## 7. PROPOSED DATA STRUCTURE

### 7.1 Directory (proposta, NON creata)

```text
data/
├── latest/
│   ├── metadata.json        ← stato del run + freshness (l'app lo legge)
│   ├── points.json          ← collasso per-coordinata reale (mappa + sfumatura)
│   └── provinces.json       ← indici già calcolati per provincia (riepilogo/badge)
├── models/
│   └── <model>/
│       └── <YYYYMMDD_HHMM>/ (opzionale, per debug/confronto storico)
└── archive/
    └── <YYYYMMDD_HHMM>/     (snapshot opzionale degli ultimi N run)
```

### 7.2 `metadata.json` (proposta)

```json
{
  "generated_at": "2026-09-05T13:15:00Z",
  "source_updated_at": "2026-09-05T13:45:00Z",
  "model": "best_match+ecmwf_ifs",
  "model_run": {
    "best_match": { "init": 1788609600, "available_at": 1788617026, "interval_s": 43200 },
    "ecmwf_ifs":  { "init": 1788588000, "available_at": 1788611288, "interval_s": 21600 }
  },
  "status": "ok",
  "points": 287,
  "sha256": "…",           // integrità di points.json/provinces.json
  "commit": "abc123…"
}
```

L'app potrà mostrare: **"Ultimo aggiornamento dati"** (`generated_at`), **"Run/modello"** (`model_run.*.init`), **"Timestamp elaborazione"** (`generated_at`, `source_updated_at`).

### 7.3 A/B/C/D/E — quale granularità

| Opzione | Vantaggi | Svantaggi | Dim. prevista | Richieste browser | Aggiornamento | Debug |
|---------|----------|-----------|---------------|-------------------|---------------|-------|
| **A. Risposte quasi-raw per punto** | identiche all'attuale; nessuna logica anticipata | JSON enorme, il client rifà collasso/merge | ~4-8 MB (320 pt × 2 mod × 49 var × 72h) | 1 | triviale (copiona) | triviale |
| **B. Aggregato per-coordinata** (consigliato) | conserva vista per-punto, sfumatura IDW e collasso incrementale | il client deve rifare pochi passaggi (agg + merge giorno) | **~60-150 KB** | 1 | facile (collasso nel builder) | buono |
| **C. Aggregato per-provincia** | minimo assoluto, riepilogo pronto | perde i punti interni → sfumatura/vista continua degradata | ~10-30 KB | 1 | facile | buono |
| **D. Separati per modello** | run indipendenti, debug per modello | più file da coordinare | ~2× la singola | 2-3 | medio | ottimo |
| **E. Indici MeteoRisk precalcolati** | azzero calcolo client (collasso, soglie, badge) | duplica la logica di soglia (risk profile) nel builder → rischio discrepanza; rigidità | ~5-15 KB | 1 | medio (copia della logica) | peggiore |

**Raccomandazione audit:** **B (per-coordinata) + D (un file a modello)** in `latest/`, quindi `points.json` (dual fuso per il display + `points_ecmwf.json` separato per il merge fine punto-per-punto) e `provinces.json` derivato a build-time dal collasso **solo come cache di display**, con la stessa funzione di collasso riutilizzata (per evitare la deriva di E). Se in futuro si vuole E, farlo come livello ulteriore sopra B, mai al posto di B.

---

## 8. GITHUB ACTIONS WORKFLOW DESIGN (solo progettazione)

```text
workflow_dispatch + schedule:
  schedule:
    - cron: "*/15 * * * *"        # ogni 15 minuti (copre sia IFS 6h sia ICON-2I 12h)
jobs:
  detect:
    - fetch meta.json dei modelli driver (non conteggiato)
    - read data/latest/metadata.json (repo)
    - run_changed = meta.init != stored.init
    - if !run_changed: exit 0 (nessun commit)
  fetch-and-build:
    needs: detect
    steps:
      - genera lista coordinate (107 capoluoghi + interni, 1-4/prov)
      - fetch forecast in chunk ≤10 location, sleep 1-2s tra chunk
        · N modelli richiesti (dual) con models=best_match,ecmwf_ifs
        · forecast_days=3 (minimo necessario)
      - risposte → collasso per punto (getHourOrDailyData logica portata nel builder)
      - build metadata.json + points.json (+ provinces.json opz.)
      - validazione: schema JSON, sane range (temp -40..50, rain ≥0, wind ≥0),
        copertura ≥95% delle province, monotonia giorni
      - if fail: esci senza commit (log nell'artefatto)
  commit-deploy:
    needs: fetch-and-build
    steps:
      - git add data/latest; git commit (se diff ≠ vuoto)
      - actions/configure-pages + actions/upload-pages-artifact + actions/deploy-pages
```

Costi stimati per run: ~11-32 richieste (dual, chunk 10) × peso ~11 ≈ 120-350 peso-unità ≤ quota minuto; sleep tra chunk; nessun 429 atteso. Il workflow totale impiega <5'.

---

## 9. STATE MANAGEMENT (evitare download ripetuti)

| Soluzione | Pro | Contro | Giudizio |
|-----------|-----|--------|----------|
| `metadata.json` nel repo (branch dati) | persistente, versionato, leggibile da browser, confronto immediato nel workflow | richiede lettura/scrittura repo | ✅ **SCELTA** |
| Workflow artifact | semplice | non persistente tra run (il requisito è proprio confrontare run precedenti) | ❌ |
| File di stato dedicato (`data/latest/run-state.json`) | isolato ma ridondante con metadata.json | due sorgenti di verità | ⚠️ (equivalente a metadata.json) |
| Git commit metadata (git log) | nessun file extra | non strutturato, parsing fragile | ❌ |

**Scelta: `data/latest/metadata.json`** è sia lo stato elaborato sia l'input del workflow: `run_changed = meta.last_run_initialisation_time != stored.init`. Nessun commit se il run è già stato processato → nessun commit inutile, nessun doppio download. Il confronto usa l'init time **del modello driver per l'orizzonte scaricato** (`italia_meteo_arpae_icon_2i` per il Best Match) e quello di `ecmwf_ifs` per il ramo DUAL.

---

## 10. REAL-TIME DATA STRATEGY

| Fonte | Classificazione | Motivazione |
|-------|-----------------|-------------|
| Modelli forecast (mappa) | **PRECARICARE** | deterministici, 12h/6h di run, 1 JSON piccolo |
| Allerte DPC | **PRECARICARE** | già statici su GitHub (TopoJSON pcm-dpc); basta mirror/fetch periodico → categoria B |
| METAR IEM (obs) | **CACHE CENTRALE (breve) / LIVE** | osservazioni ogni 5-30'; cache 10' in app ok; processo centralizzato opzionale ma non necessario |
| Radar RainViewer/LibreWXR/DPC | **LIVE DIRETTO** | frame a 5-15'; tile già ottimizzati per CDN; precaricare avrebbe poco senso (dimensione) e vita brevissima |
| Satellite EUMETSAT WMS | **LIVE DIRETTO** | pubblicazione ~15' (verificato: Dimension PT15M); il WMS produce tile on-demand |
| Geocoding comuni | **LIVE DIRETTO** | traffico per-ricerca utente; precaricabile solo se si volessero i ~7.900 comuni italiani (poco utile) |
| Basemap OSM / librerie CDN | **LIVE DIRETTO** (self-host opzionale) | non dati MeteoRisk; non è lo scopo dell'audit |
| `current` conditions | **LIVE DIRETTO** (o precache breve) | obs ultima ora; unico componente "ora" che cambia frequentemente |

---

## 11. FAILURE & FALLBACK STRATEGY

Regola d'oro: **ULTIMO DATASET VALIDO, mai dataset vuoto.**

| Fallimento | Comportamento previsto |
|------------|------------------------|
| **A. GitHub Action fallita** | nessun `latest/*` aggiornato → il browser continua a usare il dataset precedente (nessun segnale di errore oltre al timestamp fermi in `metadata.json`) |
| **B. API non risponde** | il workflow esce senza commit; il `generated_at` del dataset resta quello vecchio; niente dati corrotti |
| **C. Run incompleto** (copertura <95% province, range fuori soglia) | la validazione blocca il commit → resta il run precedente; log nel workflow artefatto |
| **D. Dataset JSON corrotto** | doppia difesa: (1) `sha256` in `metadata.json` verificato dal client; (2) se `points.json` non rispetta il contratto, l'app fa fallback su `archive/<last/good>` o su `provinces.json` minimo; corner case finale → fallback al fetch live Open-Meteo attuale (comportamento d'oggi) con banner "dati cache non validi" |
| **E. Commit GitHub fallisce** | il prossimo run confronta di nuovo e — se il run è ancora lo stesso (init invariato) — **non ritenta** il download (stato = metadata), ma tenta solo il commit bloccato; nessun doppio lavoro |
| **F. Pages non ancora aggiornato** (CDN/cache) | l'app usa il dataset che trova; `metadata.json` espone `generated_at` → l'UI mostra "dati del <timestamp>"; eventuale retry del client dopo un piccolo backoff |

In ogni caso: **l'app DEVE mostrare sempre qualcosa di già validato**, con indicazione esplicita della freschezza, mai una mappa bianca.

---

## 12. PERFORMANCE ESTIMATE

### Architettura attuale (1.0.0.2)

```text
Utente apre l'app
  └─ boot: Leaflet + OSM (8-20 tile)                        = ~10-25 richieste
  └─ fetchWeatherData() (DUAL, per punto)                    = ~107..321 richieste forecast
        + retry (×2 su errori) + attese 429 (60s)            = fino a ~2× il numero
  └─ interazioni: geocoding, current, confronto (4), comuni  = +1..8 per azione
  └─ radar/satellite (su apertura)                            = decine-centinaia di tile
Totale al primo render della mappa meteo: ~110-330+ richieste di cui ~107-321 forecast.
Dimensione forecast complessiva: ordine di ~5-15 MB (payload JSON del forecast multi-modello).
```

### Architettura futura

```text
Utente apre l'app
  └─ boot: Leaflet + OSM  (invariato)
  └─ GET data/latest/metadata.json  (~0,5 KB)
  └─ GET data/latest/points.json    (~60-150 KB)
  └─ (opz.) GET data/latest/provinces.json (~10-30 KB)
Totale richieste meteo browser: 1-3. Zero Open-Meteo dal client.
DOWNLOAD distribuito: SVG...
```

### Stime

| Metrica | Ora (1.0.0.2) | Futura | Δ |
|---------|---------------|--------|---|
| Richieste forecast al browser | 107-321 (+retry/429) | 0 | −100% |
| Richieste meteo complessive alla mappa | ~110-330 | 1-3 | ~−98/99% |
| Byte dati modelli | ~5-15 MB JSON | 60-180 KB | ~−98/99% |
| Tempo "mappa colorata" | 30 s - >5 min (pacer+retry+eventuali 429) | <1 s (cache HTTP Pages) | drasticamente migliore |
| Rischio rate-limit per l'utente finale | presente (600/min, 10k/giorno, condivisi per IP) | **nullo** | eliminato |
| Rischio rate-limit per il publisher | n/a | contenuto (≤ ~32 richieste/run con sleep) | gestito |
| Bandwidth provider (totali giornalieri) | ripetuti da ogni utente | una sola volta per run per il publisher | ottimale |

Nota: le richieste radar/satellite (live) restano per frame e non sono oggetto di precaricamento: non vengono **eliminate**, ma non sono mai state il collo di bottiglia (il problema era il forecast full-res a ogni apertura).

---

## 13. LICENSE & TERMS RISKS

### Open-Meteo (verificato oggi sui termini ufficiali https://open-meteo.com/en/terms e docs)

| Aspetto | Stato del documento | Livello certezza |
|---------|---------------------|------------------|
| Uso API free solo **non commerciale** | "You may only use the free API services for non-commercial purposes"; esempi: app private/no-profit senza pubblicità e no subscription; scopo educativo esplicitamente incluso | **CONFIRMED** |
| Licenza dati | dati concessi **CC-BY 4.0** ("You accept to the CC-BY 4.0 licence") — la redistribuzione **con attribuzione** è prevista dalla licenza stessa | **CONFIRMED** (per la licenza dati) |
| Limiti free | 600/min · 5.000/ora · 10.000/giorno · 300.000/mese | **CONFIRMED** |
| **Metadata API** | "API calls to the metadata API are not counted toward daily or monthly request limits" | **CONFIRMED** |
| Caching/redistribuzione sistematica (es. GitHub Actions → Pages) | il ToS **non vieta esplicitamente** il caching/redistribuzione, ma si riserva di "block applications and IP addresses that misuse our service". La CC-BY 4.0 consente la condivisione, ma lo *scenario aggregatore* è un caso non esplicitamente disciplinato | **UNCLEAR → REQUIRES MANUAL VERIFICATION** |
| Uso non commerciale dell'app come "publisher centralizzato" | la definizione non commerciale è soddisfatta dalla Edizione Light (didattica, senza ads/subs); va però riaffermata in ogni release | **CONFIRMED** (se resta non commerciale) |

> **Raccomandazione:** contattare Open-Meteo (info@open-meteo.com) prima dell'implementazione della pipeline centralizzata, allegando l'architettura, per ottenere conferma scritta su: (a) bulk-fetch programmato da GitHub Actions runner, (b) caching/redistribuzione su GitHub Pages, (c) attribuzione CC-BY 4.0 applicata all'interfaccia. Non dare per scontata alcuna autorizzazione aggiuntiva. In alternativa alla pipeline centralizzata, un piano commerciale elimina le ambiguità sull'uso.

**Altri provider (non preload):** restano live, nessun nuovo backend → nessuna nuova condizione introdotta dall'audit. Le fonti live già in 1.0.0.2 mantengono le proprie attribuzioni (vedi `docs/LICENSES.md`).

---

## 14. MINIMAL IMPLEMENTATION PLAN

Ordine proposto (nessun intervento qui; solo progettazione):

```text
STEP 1 — Data Fetcher
   Script Node.js (fuori repo o in scripts/) che usa le stesse coordinate
   (107 capoluoghi + interni) e la stessa formattazione API dell'app,
   con batch ≤10 location, sleep tra chunk, forecast_days=3, models=...
   Periodo: fetch DUAL default alla logica odierna.

STEP 2 — Run Detection
   Nel fetcher: lettura meta.json dei modelli driver (non conteggiato),
   confronto con data/latest/metadata.json, wait 10' dopo availability,
   salta se init invariato. Identificatore: last_run_initialisation_time.

STEP 3 — JSON Builder
   Porta nel builder il collasso per-punto CLAUDE getHourOrDailyData /
   assembleProvinceStores (stessi identici calcoli dell'app), genera
   points.json (per-coordinata, dual) + provinces.json (derivato) +
   metadata.json con sha256 e timestamps. Output su data/latest.

STEP 4 — GitHub Action
   workflow schedulato ("*/15") che esegue 1-3 in ordine, valida,
   committa solo se cambiato e fa deploy Pages (actions/deploy-pages).

STEP 5 — App Migration
   In mri-light-1.0.0.4: fetch di data/latest/{metadata,points,provinces}.json
   al posto di fetchWeatherData(); keepis fallback: se dataset assente →
   comportamento odierno (fetch live). UI: mostrare metadata (ultimo
   aggiornamento, run, timestamp). Nessuna modifica alla pipeline di
   rendering (collasso/merge consumano gli stessi oggetti {daily,hourly}).

STEP 6 — Validation
   Schema JSON + sanity-range + copertura province ≥95%. Ripetibilità
   con il calcolo prodotto dal client (confronto su un campione di
   province prima/dopo). Regressione end-to-end su localhost.

STEP 7 — Production Test
   Pubblicazione della cartella 1.0.0.4 su GitHub Pages, verifica su
   HTTP reale: open → mappa colorata <1 s, zero richieste Open-Meteo,
   fallback manualmente testato disabilitando i JSON, benchmark prima/dopo.
```

---

## 15. CRITERI DI SUCCESSO (checklist audit)

- [x] Nessun file applicativo modificato (solo version bump nella copia 1.0.0.3)
- [x] Tutte le API inventariate (attive + disattivate)
- [x] Open-Meteo analizzato completamente (endpoint, modelli, variabili, orizzonte, timing)
- [x] Coordinate identificate (107 capoluoghi + 1-4 interni/prov, 107..~321 reali + 220 virtuali)
- [x] Richieste duplicate identificate (current multi-trigger, confronto 4 richieste, refresh/cambio modello, retry/429)
- [x] Metodo ufficiale model-run verificato (Metadata API, probe reale, campi e id per modello)
- [x] GitHub Actions feasibility analizzata (workflow, costi, limiti)
- [x] Architettura JSON proposta (data/latest + metadata + points + provinces)
- [x] State management proposto (metadata.json nel repo, confronto init)
- [x] Fallback analizzato (ultimo dataset valido, A-F)
- [x] Dati real-time separati (radar, satellite, geocoding, current live)
- [x] Impatto performance stimato (−100% richieste forecast al browser, 1-3 JSON)
- [x] Licenze/Terms non date per scontate (CONFIRMED / UNCLEAR→MANUAL per aggregatore)

---

## 16. VERIFICA VERSIONE + OUTPUT

- [x] 1.0.0.2 preservata intatta (`releases/Light-1.0.0.2/`)
- [x] 1.0.0.3 creata come copia separata (`releases/MeteoRisk-Light-1.0.0.3-Data-Architecture-Audit/`)
- [x] Version bump applicato coerentemente (APP_VERSION, changelog, footer/badge auto)
- [x] Nessuna modifica funzionale (diff solo verso version/cambi changelog)
- [x] Audit report incluso (`docs/METEORISK_LIGHT_DATA_ARCHITECTURE_AUDIT.md`)