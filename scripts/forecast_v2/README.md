# Forecast V2 — mappe previsionali `.om` multi-dominio (server-side)

Motore che costruisce in GitHub Actions una **cache GitHub multi-dominio di mappe
previsionali** in formato Open-Meteo `.om`: per ogni **dominio** e per ogni istante
di previsione un file multi-variabile croppato sull'Italia, leggibile dal reader
WML 0.2.2 usato dal client (formato verificato bit-exact nel POC `spike_omfiles/`).

Ogni dominio è elaborato in modo **indipendente**: non esiste più il paradigma
"primario + secondario". Un dominio non fornisce mai frame a un altro; ognuno ha
la propria griglia, la propria cache e il proprio manifest.

## Cosa produce (layout per-dominio)

```
data/fm2/
  <dominio>/                          # es. dwd_icon_eu, jma_gsm, ...
    <YYYY-MM-DDTHH00>.om              # un file per valid_time, multi-variabile, crop Italia
    latest.json                       # manifest del dominio
```

- `data/fm2/<dominio>/<YYYY-MM-DDTHH00>.om` — un file per `valid_time`,
  multi-variabile, crop Italia sulla griglia **del dominio stesso**.
- `data/fm2/<dominio>/latest.json` — manifest con `domain`, `reference_time`,
  `valid_times`, `grid` del crop, `source_grid` (griglia piena derivata),
  `grid_source`, `variables` (per frame e per variabile: dominio sorgente),
  `sizes`, `frames`, `warnings`, `pruned`.
- **Orizzonte INTERNO/intero per dominio**: si pubblicano TUTTI i `valid_times`
  del manifest **del dominio** (nessuna troncatura a un numero fisso). In
  `data/fm2/<dominio>/` resta esattamente la finestra corrente (le ancore
  pubblicate); i frame di run precedenti vengono rimossi. `V2_MAX_FRAMES`
  (default `0`) è un cap *opzionale* di sicurezza del pruning: `0` = nessun cap.

Il client legge un file con `readVariable(url, "<nome_var>", ranges)` dove `ranges`
sono gli indici del crop (vedi `grid` nel manifest del dominio: `nx`, `ny`,
`lonMin`, `latMin`, `dx`, `dy`).

## Uso

```bash
# Un dominio:
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains jma_gsm \
  --valid-frames 0 \
  --out data/fm2

# Più domini (ciascuno indipendente), con cache persistente dei frame:
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains dwd_icon_eu ecmwf_ifs025 jma_gsm \
  --valid-frames 0 \
  --out data/fm2 \
  --frames-cache .cache_fm2_frames

# Piano di TUTTI i domini senza scaricare/scrivere nulla:
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains dwd_icon_eu jma_gsm ncep_gfs025 chmi_aladin_central_europe_2km \
  --valid-frames 0 --dry-run
```

### Opzioni CLI

| Opzione | Descrizione |
|---|---|
| `--domains D1 D2 ...` | Domini da elaborare, ciascuno **indipendente** (nessun ordine di priorità; ognuno produce `data/fm2/<dominio>/`). Default: `dwd_icon_eu ecmwf_ifs025`. |
| `--valid-frames N` | Numero di `valid_times` da pubblicare a partire dal primo, **per ogni dominio**. `0` (default) o negativo = **intero orizzonte** del manifest del dominio (tutti i `valid_times`). |
| `--out DIR` | Directory di output radice (default `data/fm2`); la cache di ogni dominio va in `<DIR>/<dominio>/`. |
| `--once` | Un solo ciclo ed esce (default/compat). |
| `--no-prune` | Non rimuove i frame oltre il limite. |
| `--dry-run` | Stampa il piano di TUTTI i domini senza scaricare/scrivere. |
| `--force` | Ignora il NO-OP guard (riscrive sempre). |
| `--cache-dir DIR` | Directory dei download temporanei (default: temp di sistema; flat `<dominio>__<VALID>.om`, nessuna verifica). |
| `--frames-cache DIR` | Cache **persistente** dei frame full-size, layout `<DIR>/<dominio>/<VALID>.om`. Un frame in cache è riusato **senza download** se la sua `Content-Length` combacia con quella remota (HEAD leggero; fallback: size locale). Default: vuoto = disabilitata. |

## Modello multi-dominio e riconoscimento CRS

Per ogni dominio elencato in `--domains` il motore, in modo indipendente:

1. scarica `latest.json` S3 del dominio e legge `reference_time`, `valid_times`,
   `completed`, `crs_wkt`;
2. **riconosce il CRS** dal `crs_wkt`:
   - se **inizia per `GEOGCRS`** → dominio supportato;
   - altrimenti (es. il `PROJCRS` Lambert di
     `chmi_aladin_central_europe_2km`) → **SKIP** con log esplicito
     `SKIP dominio '<dominio>': griglia proiettata non supportata
     (PROJCRS/Lambert)`, e la run prosegue con gli altri domini;
3. **deriva la griglia**: `latMin/lonMin/latMax/lonMax` dal `BBOX[...]` del
   `crs_wkt` (regex, ordine WKT `[latMin, lonMin, latMax, lonMax]`); `nx,ny` dal
   `.shape == (ny,nx)` del **primo frame `.om`** del dominio (`OmFileReader`;
   stesso `fetch_frame`, quindi va in frames-cache). Poi
   `dx=(lonMax-lonMin)/(nx-1)`, `dy=(latMax-latMin)/(ny-1)`. Il dict griglia è
   memorizzato in una cache per-run;
4. calcola il **crop Italia** (`36.5, 6.5, 47.2, 18.6`) sulla griglia del
   dominio (`bbox_to_indices`/`target_latlon`, generici);
5. pubblica l'**orizzonte** (`--valid-frames`) e il set unico `V2_RISK_VARS` (27),
   con la presenza reale decisa **file-per-file** dai child del `.om`;
6. scrive `data/fm2/<dominio>/<VALID>.om` + `latest.json`, applica il **NO-OP
   guard per-dominio** e il **prune per-dominio**.

## Griglie derivate e GRIGLIE NOTE

La griglia di run è **sempre derivata** da `crs_wkt` + shape del frame. La tabella
seguente elenca le **griglie note** dei 10 domini GEOGCRS: **non è la fonte
unica**, serve solo come (a) **fallback nel `--dry-run`** (dove il frame non si
scarica, quindi `nx,ny` non è derivabile) e (b) **guardia di coerenza**: se i
`dx`/`dy` derivati si scostano **>1%** dai valori noti (e il dominio è in tabella)
si logga un warning, ma si **usano i valori derivati**.

| dominio | nx | ny | dx | dy |
|---|---|---|---|---|
| `dwd_icon_eu` | 1377 | 657 | 0.0625 | 0.0625 |
| `ecmwf_ifs025` | 1440 | 721 | 0.25 | 0.25 |
| `ncep_gfs025` | 1440 | 721 | 0.25 | 0.25 |
| `dwd_icon` | 2879 | 1441 | 0.125 | 0.125 |
| `meteofrance_arpege_world025` | 1440 | 721 | 0.25 | 0.25 |
| `ukmo_global_deterministic_10km` | 2560 | 1920 | 0.140625 | 0.09375 |
| `jma_gsm` | 720 | 361 | 0.5 | 0.5 |
| `cma_grapes_global` | 2880 | 1440 | 0.125 | 0.125 |
| `dwd_icon_d2` | 1215 | 746 | 0.02 | 0.02 |
| `italia_meteo_arpae_icon_2i` | 761 | 761 | 0.025 | 0.02 |

`chmi_aladin_central_europe_2km` è `PROJCRS` (Lambert) → **SKIP**, non compare in
tabella.

### `--dry-run`

`--dry-run` scarica solo i `latest.json` (nessun frame) e stampa, per **ogni**
dominio: dominio, `reference_time`, griglia (o riga `SKIP` con motivo),
`n_valid_times`, crop `nx×ny`, `n` frame, percorsi output. Poiché in dry-run il
frame non si scarica, `nx,ny` proviene dal **fallback `GRIDS`** ed è etichettato
`grid_source: "fallback(GRIDS)"` (in run reale è `"crs_wkt+frame"`). Se un dominio
non è in tabella e non è dry-derivabile, il piano lo segnala con `"unknown"`.

## Exit code (scelta documentata)

- `0` = almeno un dominio è `ok` (prodotto **o** NO-OP): il workflow deve poter
  committare la cache dei domini riusciti. Il summary stampa sempre le liste
  `ok`/`skip`/`fail`.
- `1` = **tutti** i domini sono `fail` (nessun dominio prodotto). Errori
  CLI (`--domains` vuoto) sono anch'essi `1`.

`skip` (CRS non supportato) non conta come `ok` ai fini dell'exit: un dominio
`skip` da solo NON basta (nessuna cache scritta → `1`).

## Cache dei frame ed egress CI

Il job CI `forecast-maps-v2` usa `actions/cache@v4` sulla directory
`.cache_fm2_frames` (chiave `fm2-frames-${{ github.run_number }}`, `restore-keys:
fm2-frames-`): ad ogni run ripristina l'ultima cache disponibile e la risalva
aggiornata a fine run. La cache GitHub è **LRU, 10 GB per repo**: quando si supera
il limite le entry più vecchie vengono sfollate automaticamente (il motore
riscarica solo ciò che manca, quindi resta corretto).

Con `--frames-cache` il motore, per ogni `<dominio>/<VALID>.om`:

1. se il file è in cache e la `Content-Length` remota combacia (HEAD) → **riuso,
   nessuna GET del frame**;
2. se manca o è stale → una sola GET, salvata in cache per le run successive.

## Fonti e formato

- S3 anonimo: `https://openmeteo.s3.amazonaws.com/data_spatial/<dominio>/<YYYY>/<MM>/<DD>/<HH00Z>/<YYYY-MM-DDTHH00.om`
  e, come indice, `<dominio>/latest.json`.
- Ogni variabile pubblicata viene **dal dominio stesso** (nessun ricampionamento
  da altri domini): il crop è esatto (nearest = identità sui centri-cella).
- Variabili **derivate** lato server quando manca la sorgente diretta:
  `wind_speed_10m = sqrt(u^2+v^2)`, `wind_direction_10m = atan2(-u,-v)` (0=N),
  `dew_point_2m = Magnus(T2m, RH2m)`. `surface_pressure` non è derivabile senza
  quota: se assente viene **omessa con warning** nel manifest.

## Set variabili pubblicate

Il motore pubblica l'**unione** di due gruppi, nello stesso ordine del manifest
(`var_order`), identica per tutti i domini (nessun set per-dominio):

- **17 variabili di rischio** (base): `temperature_2m`, `relative_humidity_2m`,
  `dew_point_2m`, `pressure_msl`, `surface_pressure`, `wind_speed_10m`,
  `wind_direction_10m`, `wind_u_component_10m`, `wind_v_component_10m`,
  `wind_gusts_10m`, `cloud_cover`, `cape`, `snow_depth`, `precipitation`, `rain`,
  `showers`, `weather_code` (di cui `dew_point_2m`, `wind_speed_10m`,
  `wind_direction_10m` sono **derivate** quando manca la sorgente diretta).
- **10 variabili `FM_CACHE core`** (pass-through dirette): `temperature_850hPa`,
  `temperature_500hPa`, `relative_humidity_850hPa`,
  `geopotential_height_500hPa`, `geopotential_height_850hPa`,
  `geopotential_height_1000hPa`, `cloud_cover_low`, `cloud_cover_mid`,
  `cloud_cover_high`, `freezing_level_height`.

Totale: **27 variabili** in `var_order`. Le variabili assenti nel frame (es.
accumuli/flussi di analisi, o `freezing_level_height`/`cape` se il dominio non le
espone) sono **omesse con warning** nel manifest del dominio, senza crash.

### Stime di dimensione (misurato + stima)

- Misura reale (run di verifica, motore 1.2.4.4) su `dwd_icon_eu`, crop
  `194 x 172` = **33.368 celle**: un frame di **23 variabili** = **327.200 byte
  ~= 319,5 KiB** → **~13,9 KiB/variabile**; stima unione piena (27 var)
  **~375-386 KiB/frame**.
- Misura reale (run di verifica multi-dominio, questo motore) su `jma_gsm`, crop
  `25 x 22` = **550 celle**, 2 frame: **9.720 byte (18 var)** e **10.200 byte
  (19 var)** → **~19,4 KiB** totali per 2 frame. I crop grossi producono frame
  nell'ordine dei KiB (proporzionali al numero di celle).
- Il numero di frame per dominio = **orizzonte intero** del manifest del dominio
  (variabile per run): osservato `dwd_icon_eu` 93, `jma_gsm` 45, `ncep_gfs025`
  209, `chmi_aladin_central_europe_2km` 73 al momento della verifica. Il conteggio
  reale va letto dal campo `valid_times` del `latest.json` del dominio.

## Come il client (reader WML 0.2.2) legge le variabili

Attenzione: il reader WML **non** restituisce sempre l'array così com'è scritto,
applica delle *derivation rules* basate sul nome:

- Nomi che contengono `_[uv]_component` o `_[uv]_current` (es.
  `wind_u_component_10m`, `wind_v_component_10m`): il reader legge **entrambe** le
  componenti e restituisce `values = sqrt(u^2+v^2)` (velocità) e
  `directions = direzione meteorologica` (0..360). Le componenti grezze NON sono
  accessibili come singolo array via `readVariable`.
- Nomi che contengono `_speed_` / `_direction_` (es. `wind_speed_10m`,
  `wind_direction_10m`): il reader legge la **coppia** (speed + direction) e
  restituisce `values = speed`, `directions = direction`. Per questo il motore
  scrive **sia** le componenti `u/v` **sia** `wind_speed_10m`/`wind_direction_10m`:
  così entrambe le convenzioni di nome funzionano.
- Tutte le altre variabili (`temperature_2m`, `cape`, `weather_code`, ...) sono
  lette come semplici (`values` = array, niente `directions`).

## Limiti noti

- Il download avviene a **file intero** dal S3 (nessuna range-read); l'output
  pubblicato è invece piccolo. Con `--frames-cache` i download a file intero sono
  ammortizzati tra run consecutive; a cache fredda il costo resta quello del file
  intero.
- Il NO-OP guard è **per-dominio** e confronta `reference_time` + `valid_times`
  del dominio: l'insieme dei frame è deterministico a parità di run del modello.
- I chunk di scrittura `.om` sono clampati alla dimensione effettiva dell'array
  (i domini a griglia grossa, es. `jma_gsm` con crop `25x22`, hanno celle < 32 e
  `omfiles` rifiuta chunk più grandi dell'array).
- `pick_secondary_valid` e la fase "dominio secondario" sono **rimosse**: ogni
  variabile è presa solo dal dominio corrente. Chi invocava `--domains` con il
  primo dominio come "primario" deve ora aspettarsi un sottodirectory per
  dominio.

## Licenze / attribuzione

- Dati: **Open-Meteo** (https://open-meteo.com), licenza **CC BY 4.0** —
  attribuzione a Open-Meteo richiesta in ogni ridistribuzione.
- `omfiles` (GPL-2.0-only): usato **solo a runtime/CI** per leggere/scrivere il
  formato `.om`; non è venduto né incluso nel repository (dipendenza pip in
  `requirements.txt`: `omfiles>=1.0`, `numpy`). Nessun segreto: l'accesso a S3 è
  anonimo.
