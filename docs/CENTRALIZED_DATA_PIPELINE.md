# CENTRALIZED DATA PIPELINE — MeteoRisk Light 1.0.0.4

La versione 1.0.0.4 introduce la **Centralized Data Pipeline**: una pipeline server-side
che genera un **DATASET DERIVATO MeteoRisk** da Open-Meteo e lo pubblica come JSON statico
in `data/latest/`, servito da GitHub Pages. L'app si collega al dataset: per i modelli
coperti **zero richieste browser verso l'API Open-Meteo** (il fallback live resta come rete
di sicurezza controllata).

Open-Meteo è **SOLO una fonte dati** (input meteorologico): i payload dell'API **non** vengono
ripubblicati, specchiati o incapsulati. Vengono pubblicati esclusivamente dati **derivati
aggregati** (`dataset_type=derived_meteorological_risk_data`), come da ADDENDUM OBBLIGATORIO.

## Architettura

```
Open-Meteo (fonte dati)  ──►  Pipeline Python (GitHub Action)  ──►  data/latest/*.json  ──►  GitHub Pages  ──►  mri-light-1.0.0.4.html (loader)
```

Pipeline (per run, si veda anche `docs/` e commenti in head di ogni script):

1. `scripts/common.py` — configurazione + port fedeli della logica dell'app
   (geometria province, sampling V1/V2, `collapse_day` (11 campi), `score_point_for_province`,
   client Open-Meteo, run state).
2. `scripts/generate_points.py` — coordinate **reali** del campionamento (port bloccato dal
   golden test: v1=265, v2=257, ordine e coordinate identici all'app, seed coordIdx 0 = 107).
3. `scripts/check_model_runs.py` — rileva un NUOVO run dei modelli tramite la **Metadata API**
   (non rate-limitated); exit `0`=nuovo dataset da produrre, `10`=già processato, `1`=errore rete.
4. `scripts/fetch_source_data.py` — scarica il **raw temporaneo** in `data/_raw/`
   (MAI pubblicato): 1 richiesta per coordinata con `models=best_match,ecmwf_ifs`
   (stesso identico payload URL dell'app). Se manca un capoluogo (coordIdx 0) → exit 3, no dataset.
5. `scripts/build_meteorisk_dataset.py` — METEO-RISK DATA ENGINE: collasso "day-wide" del
   giorno, riepilogo derivato giornaliero (20 campi), serie orarie spogliate dall'envelope API,
   worst-point per provincia (port di `scorePointForProvince`, tie-break primo max in ordine slot).
6. `scripts/validate_dataset.py` — validazione **PRIMA** della pubblicazione (es. **3.619 check**):
   forma, contenuto, integrità territoriale, consistenza col port di coordinate, coerenza
   worst-point ricalcolato. Su esito negativo `data/latest` NON viene toccato (last known good).
7. `scripts/publish_dataset.py` — pubblicazione **atomica**: swap staging→latest con backup e
   rollback; aggiorna `data/state/last_model_run.json` (status live).

## Contenuti pubblici di `data/latest` (esempio: fixture dataset, day0 = 2026-09-05)

| File | Dimensione (fixture) | Contenuto |
|---|---|---|
| `metadata.json` | 951 B | schema, dataset_type, modelli coperti, attribution, run_info, day0, point_count |
| `meteorisk-points.json` | 11.962.133 B | 257 punti reali: id, provinceIdx, sigla, coordIdx, lat/lon, elevation, `models` (best_match+ecmwf_ifs, 48 variabili orarie), `summary` giornaliera |
| `meteorisk-provinces.json` | 132.035 B | 107 province: `selected_point` (worst-point + score) e riepilogo per giorno |
| `validation.json` | 388.590 B | report completo della validazione (esiti per ognuno dei 3.619 check) |

Modelli coperti dal dataset: `best_match`, `ecmwf_ifs` (2 leg del dual). L'app tratta anche
`dual_best_ecmwf` come coperto (il merge dual avviene client-side con le stesse funzioni esistenti).

## Lato app (mri-light-1.0.0.4.html)

- `DATASET_PREFIX = 'data/latest/'`; `DATASET_COVERED_MODELS = ['dual_best_ecmwf','best_match','ecmwf_ifs']`.
- `initWeatherData('startup'|'manual')` → per i modelli coperti esegue `loadStaticDataset`
  (legge metadata + points, verifica `status='live'`, `point_count>0` e coerenza generazione);
  altrimenti **fallback controllato** sul fetch live esistente (`fetchWeatherData`).
- `applyStaticDataset` ricostruisce `provinceSamplePoints` (punti reali), `rawPointStores`,
  `modelStores`, densifica i punti virtuali (IDW, stesso modulo DENSIFY, ordine
  best_match → ecmwf_ifs → dual_best_ecmwf) e rifà il merge dual risk-preserving con
  `worstPointForProvince`/`assembleDualModelStores`. Nessuna funzione esistente rimossa.
- Pulsante "🔄 Aggiorna" → `requestWeatherData('manual')` (guardie anti doppie richieste).
- Attribuzione aggiunta nel footer: "Dati previsionali: © Open-Meteo → dataset derivato
  MeteoRisk (pipeline 1.0.0.4)".

## GitHub Action (`.github/workflows/update-weather-data.yml`)

- `schedule` ogni 15 minuti + `workflow_dispatch` opzionale con `force_update`.
- Interviene SOLO se la Metadata API segnala un nuovo run del driver (ARPAE ICON-2I) oltre
  il grace period (10 min): da qui parte fetch → build → validate → publish.
- Il commit avviene **solo** quando `publish_dataset.py` ritorna 0 (nuovo dataset valido).
- Nessun download senza run check; nessuna chiave/endpoint privati; solo stdlib Python.
- Deploy GitHub Pages del sito statico (html + `data/latest`) a ogni run.

## Come usare (deploy)

1. Copiare il contenuto di questa release nella root di un repository GitHub (il folder
   `MeteoRisk-Light-1.0.0.4-Centralized-Data-Pipeline` **è** la root: contiene `.github/`, `data/`, `docs/`, `scripts/`, l'html e `VERSION`).
2. Abilitare GitHub Pages (deploy dal branch, workflow).
3. Il primo run con `workflow_dispatch` popolerà `data/latest` col dataset reale; fino ad
   allora l'app usa la rete al posto del dataset mancante (fallback controllato).

## Vincoli rispettati

- Non modifica 1.0.0.2 / 1.0.0.3 (baseline separate, immutate).
- Nessuna funzione applicativa rimossa; comportamento utente invariato per GFS/ARPAE e modi Sviluppo.
- Nessun backend, DB, API key, Vercel o proxy.
- Last known good: `data/latest` mai invalidato prima della validazione.