# MeteoRisk Light — Report Tecnico HOTFIX3: Rendering Temporale delle Mappe (slider orario)

- Rilascio: 1.0.0.9 (HOTFIX3)
- Data: 2026-09-07
- File applicato: `mri-light-1.0.0.9.html`
- Task: "Audit e fix del rendering temporale delle mappe"
- Esito: il rendering poligonale e la sfumatura (vista continua) seguono ora in modo
  coerente la coppia `currentDay` + `currentHour` selezionata dallo slider.
- Suite di test: `.mjs` 8/8 PASS · test pipeline Python 9/9 PASS (eseguiti via `py`,
  con `PYTHONIOENCODING=utf-8` per test_fetch_stateless).

---

## 1. Root cause

### 1.1 Causa primaria (la mappa non cambia al cambio ora — Dual Model)

`getDualMergedAggForZone(i)` chiavava la cache `dualAggCache[provinceIdx]` SOLO per la
provincia + `currentDay`:

```
PRIMA:  dualAggCache[i] = { day, agg, dominant, disagreement }
        if (dualAggCache[i].day === currentDay) return dualAggCache[i].agg;
```

ma l'aggregato che viene memorizzato è prodotto da `computeAggregateForStore(store)`,
che è DIPENDENTE dall'ora:

```
idx = currentDay * 24 + parseInt(currentHour, 10);
```

Conseguenza: al primo accesso di una giornata (qualsiasi ora fosse selezionata) la cache
memorizzava l'aggregato di QUELL'ORA e lo restituiva per TUTTE le ore successive della
stessa giornata. Slider fermo sui colori della prima ora.

Sintomi osservati (corrispondenti al bug segnalato):

- **Poligonale fissa**: `updateMapColors() → zoneFillColor(zoneIdx) → getAggregateForZone(idx)
  → (modalità dual, zonePreview=null) → getDualMergedAggForZone(zoneIdx)` → valore congelato
  all'ora della prima lettura del giorno.
- **Sfumatura parzialmente congelata**: in `renderContinuousOverlay()` i punti REALI (e i
  densificati, che sono records in `rawPointStores`) vengono ricalcolati a ogni render tramite
  `computeAggregateForStore(rec)` diretto → corretti; le ANCORE VIRTUALI di provincia
  (centroidi, v2/v3) usano invece `getAggregateForZone(vz.zoneIdx)` → in dual il
  `getDualMergedAggForZone` congelato → zone intere "bloccate" all'ora della prima visita.

### 1.2 Cause secondarie / difese in profondità (audit 2-7 del task, risultato)

Audit mirato di TUTTE le cache/memo che alimentano il colore della mappa:

| Costrutto | Sede | Dipende da `currentHour`? | Esito |
|---|---|---|---|
| `dualAggCache` | `getDualMergedAggForZone` | SI | ⚠️ **Era la causa** — fix applicato (giorno+ora) |
| `store.__dmCache` | `computeAggregateForStore` (ramo `all`) | NO (usata SOLO se `currentHour==='all'`, che è indipendente dall'ora) | sicura |
| `reticulationCache` | swap v1/v2 e cache modello | NO (geometria + records con tutti i giorni/ore) | sicura |
| `continuousGeo` (`renderContinuousOverlay`) | geometria IDW | NO (bbox/pesi, non valori) | sicura |
| `dualMergedAgg` | persistenze per swap modello | SOLO scrittura/ripristino; i lettori passano da `getDualMergedAggForZone` | sicura dopo il fix |
| `iemCache` / tile cache sat/radar/lightning | non previsionale | NO | fuori scope |

Nessun'altra cache restituiva valori temporalmente stale. Il percorso reale
(punti reali + densificati) non aveva cache → già corretto; il problema era esclusivamente
l'aggregato DUAL congelato usato da poligoni e ancore.

### 1.3 Rischi architetturali osservati

1. **Selezione worst-point provinciale temporale**: `scorePointForProvince(d)` è definito sul
   MASSIMO GIORNALIERO (max CAPE + bonus ore temporale/grandine) → il punto rappresentativo
   scelto per provincia è **fisso per la giornata**, indipendente dall'ora. A un'ora X la
   provincia mostra il valore orario del punto "peggiore sul giorno", non il punto peggiore
   all'ora X. È una limitazione CONCETTUALE documentata (vedi §6), non un regression benintenzionato.
2. **Cache per provincia monovalore**: `dualAggCache[i]` conserva una sola coppia (day,hour)
   per provincia (la più recente). Un ritorno a una coppia già vista dopo visite intermedie
   RICALCOLA (valore sempre corretto, mai stale). È la semantica voluta (cache bounded a 107
   entry) e allinea il dual al percorso single-model che non cache-aggia affatto.
3. **`all` vs ore**: in vista giornaliera `computeAggregateForStore` usa il ramo day-max
   (`computeDayMaxForStore`); la chiave `'all'` è ora DISTINTA da `'0'..'23'` → nessuna
   collisione tra massimo del giorno e ora reale.

---

## 2. File modificati (per file: motivo + funzioni coinvolte)

| File | Motivo | Funzioni/sedi toccate |
|---|---|---|
| `mri-light-1.0.0.9.html` | Fix del bug (causa primaria) | `getDualMergedAggForZone()`, `assembleDualModelStores()`, nuovo helper `dualHourKey()`, commenti di contratto aggiornati |
| `mri-light-1.0.0.9.html` | Marker estrazione test | `//#pure# BEGIN dualCache … END dualCache` attorno al blocco |
| `mri-light-1.0.0.9.html` | Changelog | entry `HOTFIX RENDERING TEMPORALE DELLE MAPPE (1.0.0.9)` nella voce `1.0.0.9` di `APP_CHANGELOG` |
| `VERSION` | Convenzione rilascio | riga `HOTFIX3=…` |
| `scripts/tests/test_temporal_slider.mjs` | TEST NUOVO (regressione temporale) | 22 check |
| `docs/METEORISK_LIGHT_1.0.0.9_RENDERING_TEMPORALE_REPORT.md` | Relazione tecnica (questo file) | — |

Non modificati (per vincolo di task e rispetto dei blocchi esistenti): logica di mapping
giorno-dataset, palette/soglie/formule, algoritmi V1/V2/V3, loader dataset, pipeline `.py`.

---

## 3. Diff logico (PRIMA → DOPO)

```
PRIMA — cache per provincia + GIORNO
  var dualAggCache = {};                            // [provinceIdx] = { day, agg, dominant, disagreement }
  function getDualMergedAggForZone(i) {
      if (dualAggCache[i] && dualAggCache[i].day === currentDay) return dualAggCache[i].agg;
      ...
      dualAggCache[i] = { day: currentDay, agg: merged, ... };
      return merged;
  }
  // assembleDualModelStores:  dualAggCache[p] = { day: currentDay, agg: merged, ... };

DOPO — cache per provincia + GIORNO + ORA
  var dualAggCache = {};                            // [provinceIdx] = { day, hour, agg, dominant, disagreement }
  function dualHourKey() { return (currentHour === 'all') ? 'all' : String(currentHour); }
  function getDualMergedAggForZone(i) {
      var hourKey = dualHourKey();
      if (dualAggCache[i] && dualAggCache[i].day === currentDay && dualAggCache[i].hour === hourKey)
          return dualAggCache[i].agg;
      ...
      dualAggCache[i] = { day: currentDay, hour: hourKey, agg: merged, ... };
      return merged;
  }
  // assembleDualModelStores:  dualAggCache[p] = { day: currentDay, hour: dualHourKey(), agg: merged, ... };
```

Effetto: al cambio dello slider il merge DUAL viene RICALCOLATO e il valore/colore di
poligoni e sfumatura seguono subito `currentDay*24+currentHour`.

---

## 4. Conferma pipeline dati (item 4 e 5 del task)

Audit statico + verifica sul dataset LIVE pubblicato:

- `scripts/validate_dataset.py` impone (check PASS sul dataset pubblicato):
  hourly array == **72** (3 giorni × 24) per `best_match` ed `ecmwf_ifs`, daily == **3**,
  id univoci contigui 0..N-1, 107 province coperte, selezione worst-point ricalcolata == dataset.
- Coerenza tempo/indice: le serie orarie arrivano da Open-Meteo con `forecast_days=3`,
  `timezone=Europe/Rome`; `metadata.day0` = data del run driver (ECMWF IFS);
  `day_label(i) = day0 + i`; il client usa `idx = currentDay*24 + ora` con `currentDay`
  INDICE del dataset (0..2) risolto da `datasetIndexForDayLabelAt` → **corrispondenza
  esatta**, nessuna variabile "time" da leggere lato client (correttamente assente).
- Pubblicazione atomica `_staging → latest` (`publish_dataset.py`), last-known-good
  preservato, `metadata.json` + `meteorisk-points.json` + `meteorisk-provinces.json` co-pubblicati.
- Dataset LIVE verificato (2026-09-07T05:47:15Z): `day0=2026-09-06`, arrays 72/3 per punto,
  `summary[0..2]` con label giorno — coerenza confermata.
- **`meteorisk-provinces.json`**: struttura reale `{schema_version, status, generated_at,
  province_count, provinces:[{idx,sigla,prov,region,selected_point:{id,score,coordIdx,lat,lon},
  days:[…]}]}` (NON `{selected_point:{},days:[]}`). L'app **non lo carica affatto** (nessun
  fetch/loader nell'HTML): è un artefatto di pipeline per verifica/fingerprint. **Non può
  contaminare il rendering orario.**

---

## 5. Test eseguiti

### 5.1 Nuovo test di regressione `scripts/tests/test_temporal_slider.mjs` (22 check — PASS)

Estrae il blocco puro (`//#pure# BEGIN/END dualCache`) e simula `currentDay`/`currentHour`,
`computeAggregateForStore` (dipendente da `day*24+ora`) e `mergeModelAnalyses` (max
risk-preserving + dominant):

1. **Poligonale**: stessa giornata, ore 00/06/12/18/23 → valori/colori cambiano (624/630/636/642/647
   attesi), indipendenza dall'ora di partenza, ritorno a ora visitata → valore FRESCO (mai la
   prima ora), entry cache taggata con l'ultima coppia (day,hour).
2. **Sfumatura/ancore**: ancore (via `getAggregateForZone`) seguono l'ora (03→17 cambia di +14),
   ritorno → valore fresco, riuso cache per coppia consecutiva.
3. **Cambio giorno 0→1→2→0**: aggregati distinti per giorno, nessuna collisione, entry taggata
   (day, hour) corretta.
4. **Modalità `all`**: chiave `all` distinta da `0..23` (oggetti/tag distinti, nessun riuso improprio).
5. **Dual model**: provincia senza store → null (nessuna fabbricazione), solo ecmwf(B) → dominant
   `b`, valori monotoni sulle 24 ore.
6. **Struttura sorgente**: guardie che testano GIORNO+ORA presenti, guardia vecchia "solo giorno" assente.

### 5.2 Suite completa

- `.mjs` (Node 22): `contract_dataset_loader`, `test_color_coherence`, `test_day_mapping`,
  `test_fascia_slots`, `test_forecast_slot_e2e`, `test_live_panel_gating`, `test_mobile_responsive`,
  `test_temporal_slider` → **8/8 PASS** (0 errori).
- `.py` (python 3.13 via `py`, `PYTHONIOENCODING=utf-8`): best_match_canary, bootstrap,
  decide_cycle, fetch_stateless, metadata_retry, negative_validation, planner_budget, sample_port,
  workflow_gate → **9/9 PASS** (planner_budget: RESULT OK; fetch_stateless: PASS, richiedeva la
  variabile d'ambiente per il codepage).
- Parse JS del file editato: `node --check` su script inline estratto → **OK**.

---

## 6. Problemi residui / limitazioni (comportamento attuale → desiderato)

1. **LIMITAZIONE CONCETTUALE — worst-point provinciale fisso sull'ora** (vedi §1.3-1):
   - Attuale: una provincia è rappresentata dal punto con max convettivo SULL'INTERA giornata;
     all'ora X viene mostrato il valore orario di quel punto.
   - Desiderato (esempio): selezionare per ogni ora il punto peggiore A QUELL'ORA.
   - Soluzione minima: nessuna (cambierebbe la semantica "provincia=giorno" già documentata e
     la coerenza con pannello/badge).
   - Soluzione architetturale (NON introdotta per budget/rischio): refactor di
     `assembleProvinceStores/worstPointForProvince` per produrre un "worst-point per (provincia,ora)"
     con `scorePointForProvince` parametrizzato sull'ora e merge Dual per-slot; impatto su
     performance (107 × 24 valutazioni al cambio giorno), compatibilità con la pipeline
     (`validate_dataset.py` ricalcola il worst-point GIORNALIERO = dato pubblicato), tooltip e
     debug-colore. Da pianificare come macrolettura separata con test dedicati.
2. **Cache monovalore per provincia**: riuso dentro la stessa coppia (day,hour); ritorni su coppie
   intermedie ricalcolano (corretto, costo `computeSevereIndices`/`computeHailMetrics`/merge,
   identico al percorso single-model che non usa cache). Non è un problema di correttezza.
3. **Verifica in-browser da eseguire su deploy**: il fix è coperto da test di unità estratti; il
   ciclo E2E headless (Edge) con dataset live e slider a ore multiple resta suggerito nella
   verifica di accettazione manuale sul deploy (lo slider su Dual Model: poligoni e sfumatura
   devono cambiare colore a ogni ora).

---

## Appendice — Percorso dati temporali mappato (audit)

```
slider (#hour-slider) → selectHourFromSlider → currentHour = '0'..'23'|'all'
  → updateUI() → updateMapColors()
        → (non continuous) provinceGeoJsonLayer.setStyle(zoneFillColor(idx))
              → getAggregateForZone(idx) → [dual] getDualMergedAggForZone(idx)   ← FIX
              → colorForStoreForCurrentMetric(agg)
        → (continuous) renderContinuousOverlay()
              → punti reali+densificati: computeAggregateForStore(rec) [+ merge ecmwf per-point]
              → ancore virtuali: getAggregateForZone(vz.zoneIdx) → getDualMergedAggForZone  ← FIX
              → metricSurfaceValue/colorForStoreForCurrentMetric + IDW colors (v1/v2/v3)
  → refreshRiskSummary() (pannello/badge)
```