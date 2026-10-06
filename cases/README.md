# Extended Real-World Validation — Case Catalog (Fase 1.8)

Questo catalogo estende `cases.json` (validazione retrospettiva Fase 1.5/1.7)
per la **Fase 1.8 — Extended Real-World Validation**. È **metadata-only**:
non contiene alcun raw radar. I GeoTIFF VMI PT5M del Radar-DPC (archivio
storico) risiedono in `RADAR_SAMPLES_DIR` (esterno al repository) e vengono
risolti per pattern `samples_root/<day>/VMI-HHMM.tif`.

Riferimento dello spec: la categoria è **neutra e di contesto**, assegnata dalla
struttura radar osservata nella finestra; il ground truth è attestato dal campo
`ground_truth_quality` e dalle `reference_sources`. **Nessuna categoria equivale
a una firma supercell** e il motore non viene usato per classificare supercelle.

## Contenuto

- `cases_extended.json` — catalogo operativo (25 casi).
- `docs/EXTENDED_VALIDATION_CASES.md` — versione narrativa (generata dal runner
  Fase 1.8).

## Schema (campo `cases[]`)

| Campo | Tipo | Note |
|---|---|---|
| `id` | str | `QUIET/ORD/ORG/LIN/SEV + YYYYMMDD`. Alias legacy: `SIT20260711`, `SIT20260908`, `QUIET20260823`. |
| `date`, `start`, `end` | date / ISO-8601 UTC | Finestra VMI **inclusiva** `[start, end]` su griglia PT5M. |
| `region` | enum | Zonizzazione **operativa** (non climatologica), v. sotto. |
| `season` | enum | Stagione meteorologica del giorno. |
| `category` | enum | Neutra di contesto, v. sotto. |
| `description` | str | Osservazione della struttura VMI nella finestra + sintesi fonti. |
| `ground_truth_quality` | enum | A / B / C / UNKNOWN (v. sotto). |
| `reference_sources[]` | str[] | Fonti documentali + archivio radar. |

## Copertura (n = 25 casi)

### Categorie (obiettivo spec: QUIET ≥3, ORD ≥4, ORG ≥4, LIN ≥3, SEVERE ≥3)

| Categoria | n | Casi |
|---|---|---|
| QUIET | 4 | `QUIET20260823`, `QUIET20260722`, `QUIET20260904`, `QUIET20260906` |
| ORDINARY_CONVECTION | 6 | `ORD20260720`, `ORD20260803`, `ORD20260818`, `ORD20260831`, `ORD20260902`, `SIT20260908` |
| ORGANIZED_MULTICELL | 7 | `ORG20260710`, `SIT20260711`, `ORG20260811`, `ORG20260812`, `ORG20260817`, `ORG20260821`, `ORG20260824` |
| LINEAR_CONVECTION | 5 | `LIN20260708`, `LIN20260726`, `LIN20260816`, `LIN20260828`, `LIN20260901` |
| SEVERE_DOCUMENTED | 3 | `SEV20260715`, `SEV20260721`, `SEV20260820` |
| DOCUMENTED_SUPERCELL | 0 | Esclusa: nessuna firma supercell certificata dall'archivio; v. note limiti. |

### Regioni (bande latitudinali operative)

`ALPS_OR_PREALPS` lat ≥45.0 · `NORD_ITALY` ≥44.2 · `CENTRAL_ITALY` ≥42.2 ·
`SOUTH_ITALY` ≥39.5 · `COASTAL_OR_MARITIME` altrimenti · `UNKNOWN` per i quiet
senza localizzazione. Le bande sono **operative per la validazione**, non
definiscono categorie climatiche.

- ALPS_OR_PREALPS 8 · NORD_ITALY 7 · CENTRAL_ITALY 5 · SOUTH_ITALY 3 ·
  UNKNOWN 3 · **COASTAL_OR_MARITIME 0 (limite: archivio VMI con copertura
  prevalente interna; convezione costiera/marina non rappresentata)**.

### Stagioni (meteorologiche)

SUMMER 20 · AUTUMN 5. **SPRING e WINTER non disponibili** (archivio DPC
consultabile inizia il 2026-07-05): la validazione stagionale copre sola-peak
estivo e transizione autunnale.

### Qualità del ground truth

A = fonti ufficiali multiple concordanti (allerta emesse, stato di crisi,
bollettini; n=2) · B = più fonti indipendenti concordanti (n=3) · C = fonte
parziale/previsionale o riferimento live non indipendente (n=2) ·
UNKNOWN = nessuna fonte indipendente, categoria da sola struttura VMI (n=18).
I casi SEVERE_DOCUMENTED sono solo A/B (mai UNKNOWN).

## Metodo di classificazione neutra (dalla struttura VMI)

1. **QUIET**: zero celle in tutta la finestra (0 storm object attesi).
2. **LINEAR_CONVECTION**: centroidi persistenti allungati — `axis_ratio ≥ 8`
   e banda 80° percentile perpendicolare ≤ 20 km (da `window_scan.json`).
3. **SEVERE_DOCUMENTED**: fonti esterne indipendenti di eventi violenti nel
   pomeriggio/sera del giorno + struttura VMI assegnata con la stessa procedura.
4. **ORGANIZED_MULTICELL**: attività alta/persistente con struttura compatta
   o ampia (`cells ≥ ~20/frame` su più frame, axis_ratio basso-moderato).
5. **ORDINARY_CONVECTION**: il resto (attività presente ma non coesa).

L'etichetta è di **contesto**, non predizione: va letta come "insieme di celle
con caratteristiche X/evento documentato Y", mai come output del motore.

## Limiti e discrepanze documentate

- **COASTAL_OR_MARITIME, SPRING, WINTER non disponibili** → richieste di
  validazione in quei domini restano aperte (limite dati).
- **`ORD20260831`**: il bollettino Protezione Civile Salento ODV prevedeva
  temporali forti/grandine sul Salento; la finestra radar osservata mostra echi
  intensi ma disorganizzati sul settore ALPS_OR_PREALPS. Discrepanza
  **previsione vs. registrato** documentata nel caso; la categoria resta
  struttura-osservata.
- **`QUIET20260906`**: quasi-quiet, 1 debole cella transitoria su 4/16 frame
  (max ~37.5 dBZ). Include come caso-limite della soglia di attivazione.
- **`SEV20260715`**: eventi estremi documentati nella mattina-tarda mattinata
  del 15 e di nuovo 18-19-21 luglio; la finestra 17:40–18:55 mostra celle forti
  isolate (struttura a celle singole, non lineare): il caso valida la
  caratterizzazione di una fase di **dechalment/arrivo** del sistema, non il
  picco.
- **`SEV20260820`**: fase iniziale del sistema (l'apice notturno non è coperto
  dalla finestra VMI disponibile). Il case è etichettato per la **coda
  pomeridiana documentata** in arrivo sul Tigullio.
- I casi non citati con GT UNKNOWN descrivono la **variabilità di
  comportamento del motore**, non eventi meteorologici dichiarati.

## Programma di esecuzione (validazione)

1. Runner `scripts/radar_engine/validation/run_phase18.py`: per ogni caso calcola
   le metriche Fase 1.8 (detection, tracking, motion, organization), i casi
   quiet (false positives), 3 sensitivity multi-parametriche su casi
   rappresentativi, l'analisi regionale e la failure analysis.
2. Output: `data/validation/extended_summary.json` + i due report in `docs/`.
3. Regola di congelamento: **nessuna modifica al motore**; parametri testati in
   sensitivity con default lasciati invariati.