# Forecast V2 — mappe previsionali `.om` multi-dominio (server-side)

Motore che costruisce in GitHub Actions una **cache GitHub multi-dominio di mappe
previsionali** in formato Open-Meteo `.om`: per ogni **dominio** e per ogni istante
di previsione un file multi-variabile croppato sulla **propria area geografica**
(per-dominio), leggibile dal reader WML 0.2.2 usato dal client (formato verificato
bit-exact nel POC `spike_omfiles/`).

Ogni dominio è elaborato in modo **indipendente**: non esiste più il paradigma
"primario + secondario". Un dominio non fornisce mai frame a un altro; ognuno ha
la propria griglia, la propria cache e il proprio manifest.

## Cosa produce (layout per-dominio)

```
data/fm2/
  <dominio>/                          # es. dwd_icon_eu, ncep_gfs013
    <YYYY-MM-DDTHH00>.om              # un file per valid_time, multi-variabile, crop per-dominio
    latest.json                       # manifest del dominio
```

- `data/fm2/<dominio>/<YYYY-MM-DDTHH00>.om` — un file per `valid_time`,
  multi-variabile, crop sulla **propria area reale** (per-dominio) sulla griglia **del dominio stesso**.
- `data/fm2/<dominio>/latest.json` — manifest con `domain`, `reference_time`,
  `valid_times`, `grid` del crop, `source_grid` (griglia piena derivata),
  `grid_source`, `variables` (per frame e per variabile: dominio sorgente),
  `sizes`, `frames`, `warnings`, `pruned`.
- **Orizzonte INTERO per dominio**: si pubblicano TUTTI i `valid_times`
  del manifest **del dominio** (nessuna troncatura a un numero fisso). In
  `data/fm2/<dominio>/` resta esattamente la finestra corrente (le ancore
  pubblicate); i frame di run precedenti vengono rimossi. `V2_MAX_FRAMES`
  (default `0`) è un cap *opzionale* di sicurezza del pruning: `0` = nessun cap.
- **Nessuna decimazione (fino alla 1.2.4.8 era applicato un *thinning*)**: il
  motore pubblicava un sottoinsieme dei `valid_times` secondo la regola
  `h ≤ 48 h → passo 1 h`, `48 h < h ≤ 120 h → passo 3 h`, `h > 120 h → passo 6 h`
  (con `h` = ore dal `reference_time`). Dalla **1.2.4.9** quel filtro è
  **RIMOSSO**: la cadenza pubblicata è quella **nativa della sorgente** e il
  numero di frame di un dominio coincide con `len(valid_times)` del suo
  `latest.json` S3. Conseguenza: più frame per dominio (crescita dipendente dal
  run; misurata +23,4% complessiva il 2026-10-17 sui 10 domini allora attivi, con `ncep_gfs025`
  il dominio più penalizzato dal vecchio thinning, +78,6%), a parità di
  `--valid-frames 0`.

Il client legge un file con `readVariable(url, "<nome_var>", ranges)` dove `ranges`
sono gli indici del crop (vedi `grid` nel manifest del dominio: `nx`, `ny`,
`lonMin`, `latMin`, `dx`, `dy`).

## Uso

```bash
# Un dominio:
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains ncep_gfs013 \
  --valid-frames 0 \
  --out data/fm2

# Più domini (ciascuno indipendente), con cache persistente dei frame:
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains dwd_icon_eu ecmwf_ifs025 ncep_gfs013 \
  --valid-frames 0 \
  --out data/fm2 \
  --frames-cache .cache_fm2_frames

# Piano di TUTTI i domini senza scaricare/scrivere nulla:
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains dwd_icon_eu ncep_gfs013 ncep_gfs025 dwd_icon_d2 \
  --valid-frames 0 --dry-run
```

### Opzioni CLI

| Opzione | Descrizione |
|---|---|
| `--domains D1 D2 ...` | Domini da elaborare, ciascuno **indipendente** (nessun ordine di priorità; ognuno produce `data/fm2/<dominio>/`). Default: i 7 domini di `V2_DOMAINS` — `dwd_icon_eu dwd_icon_d2 ecmwf_ifs025 ncep_gfs025 ncep_gfs013 dwd_icon meteofrance_arpege_world025`. |
| `--valid-frames N` | Numero di `valid_times` da pubblicare a partire dal primo, **per ogni dominio**. `0` (default) o negativo = **intero orizzonte** del manifest del dominio (tutti i `valid_times`). |
| `--out DIR` | Directory di output radice (default `data/fm2`); la cache di ogni dominio va in `<DIR>/<dominio>/`. |
| `--once` | Un solo ciclo ed esce (default/compat). |
| `--no-prune` | Non rimuove i frame oltre il limite. |
| `--dry-run` | Stampa il piano di TUTTI i domini senza scaricare/scrivere. |
| `--force` | Ignora il NO-OP guard (riscrive sempre). |
| `--no-range` | Disabilita la lettura a intervalli (HTTP Range) e scarica ogni frame **a file intero**, come prima della 1.2.4.11. Utile per diagnosi o se `fsspec`/`aiohttp` non sono disponibili. |
| `--cache-dir DIR` | Directory dei download temporanei (default: temp di sistema; flat `<dominio>__<VALID>.om`, nessuna verifica). |
| `--frames-cache DIR` | Cache **persistente** dei frame full-size, layout `<DIR>/<dominio>/<VALID>.om`. Un frame in cache è riusato **senza download** se la sua `Content-Length` combacia con quella remota (HEAD leggero; fallback: size locale). Default: vuoto = disabilitata. |

## Modello multi-dominio e riconoscimento CRS

Per ogni dominio elencato in `--domains` il motore, in modo indipendente:

1. scarica `latest.json` S3 del dominio e legge `reference_time`, `valid_times`,
   `completed`, `crs_wkt`;
2. **riconosce il CRS** dal `crs_wkt`:
   - se **inizia per `GEOGCRS`** → dominio supportato;
   - altrimenti (es. un `PROJCRS` Lambert, come il vecchio
     `chmi_aladin_central_europe_2km`) → **SKIP** con log esplicito
     `SKIP dominio '<dominio>': griglia proiettata non supportata
     (PROJCRS/Lambert)`, e la run prosegue con gli altri domini;
3. **deriva la griglia**: `latMin/lonMin/latMax/lonMax` dal `BBOX[...]` del
   `crs_wkt` (regex, ordine WKT `[latMin, lonMin, latMax, lonMax]`); `nx,ny` dal
   `.shape == (ny,nx)` del **primo frame `.om`** del dominio (`OmFileReader`;
   stesso `fetch_frame`, quindi va in frames-cache). Poi
   `dx=(lonMax-lonMin)/(nx-1)`, `dy=(latMax-latMin)/(ny-1)`. Il dict griglia è
   memorizzato in una cache per-run;
4. calcola il **crop PER-DOMINIO** (`DOMAIN_BBOX`/`bbox_for`): ogni dominio è
   tagliato sulla **propria area reale** — `dwd_icon_eu` e `ncep_gfs013` → Europa/Med
   (`34.0,-15.0,62.0,40.0`), `dwd_icon_d2` → Europa centrale
   (`43.18,-3.94,58.08,20.34`); `V2_BBOX` (Italia) resta come fallback per domini non
   mappati — sulla griglia del dominio (`bbox_to_indices`/`target_latlon`, generici);
5. pubblica l'**orizzonte** (`--valid-frames`) e il set unico `V2_RISK_VARS` (24),
   con la presenza reale decisa **file-per-file** dai child del `.om`;
6. scrive `data/fm2/<dominio>/<VALID>.om` + `latest.json`, applica il **NO-OP
   guard per-dominio** e il **prune per-dominio**.

## Griglie derivate e GRIGLIE NOTE

La griglia di run è **sempre derivata** da `crs_wkt` + shape del frame. La tabella
seguente elenca le **griglie note** dei 7 domini GEOGCRS: **non è la fonte
unica**, serve solo come (a) **fallback nel `--dry-run`** (dove il frame non si
scarica, quindi `nx,ny` non è derivabile) e (b) **guardia di coerenza**: se i
`dx`/`dy` derivati si scostano **>1%** dai valori noti (e il dominio è in tabella)
si logga un warning, ma si **usano i valori derivati**.

| dominio | nx | ny | dx | dy |
|---|---|---|---|---|
| `dwd_icon_eu` | 1377 | 657 | 0.0625 | 0.0625 |
| `ecmwf_ifs025` | 1440 | 721 | 0.25 | 0.25 |
| `ncep_gfs025` | 1440 | 721 | 0.25 | 0.25 |
| `ncep_gfs013` | 3072 | 1536 | 0.1171875 | 0.1171493 |
| `dwd_icon` | 2879 | 1441 | 0.125 | 0.125 |
| `meteofrance_arpege_world025` | 1440 | 721 | 0.25 | 0.25 |
| `dwd_icon_d2` | 1215 | 746 | 0.02 | 0.02 |

Nessun dominio `PROJCRS` (Lambert) compare in tabella: un eventuale dominio
proiettato viene **SKIP**pato (come il vecchio `chmi_aladin_central_europe_2km`).

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
- **Derivate (dalla 1.2.4.11)**: `dew_point_2m` (formula di Magnus da T2m + RH2m),
  le sei `wind_speed_*`/`wind_direction_*` (calcolate dalla coppia di componenti
  `u/v`, che restano input interni) e `precipitation_cum` (cumulata dall'inizio
  della run, sommando i frame in ordine di `valid_time`, con frame 0 = 0.0 mm).
  Tutto il resto è pass-through diretto dalla sorgente.
- Le variabili assenti nel frame (es. `surface_pressure`, spesso non disponibile)
  vengono **omesse con warning** nel manifest.

## Set variabili pubblicate

Il motore pubblica **31 variabili** nello stesso ordine del manifest (`var_order`),
identiche per tutti i domini (nessun set per-dominio). Legge inoltre **6 componenti
`u/v` del vento che NON pubblica** (INPUT interni, solo per derivare):

- **Variabili di rischio** (31, base + derivate): `temperature_2m`, `dew_point_2m`,
  `temperature_850hPa`, `temperature_500hPa`, `relative_humidity_2m`,
  `relative_humidity_850hPa`, `relative_humidity_700hPa`, `pressure_msl`,
  `geopotential_height_500hPa`, `geopotential_height_850hPa`,
  `geopotential_height_1000hPa`, `surface_pressure`, `wind_speed_10m`,
  `wind_direction_10m`, `wind_speed_850hPa`, `wind_direction_850hPa`,
  `wind_speed_300hPa`, `wind_direction_300hPa`, `wind_gusts_10m`, `cloud_cover`,
  `cloud_cover_low`, `cloud_cover_mid`, `cloud_cover_high`, `cape`, `snow_depth`,
  `precipitation`, `precipitation_cum`, `rain`, `showers`, `weather_code`,
  `freezing_level_height`. Le pass-through dirette del gruppo `FM_CACHE core` sono
  `temperature_850hPa`, `temperature_500hPa`, `relative_humidity_850hPa`, le tre
  `geopotential_height_*`, le tre `cloud_cover_*` e `freezing_level_height`.
- **6 componenti `u/v` del vento (INPUT, NON pubblicate)**: `wind_u_component_10m`,
  `wind_v_component_10m`, `wind_u_component_850hPa`, `wind_v_component_850hPa`,
  `wind_u_component_300hPa`, `wind_v_component_300hPa`. Sono lette dal frame
  sorgente (`V2_INPUT_VARS`, incluse in `V2_READ_VARS`) **solo** per derivare le sei
  `wind_speed_*`/`wind_direction_*`: non hanno scala e non vengono scritte nel `.om`.

Totale: **31 variabili pubblicate** in `var_order` (le 6 `u/v` di INPUT non sono
scritte). Le variabili assenti nel frame (es. accumuli/flussi di analisi, o
`freezing_level_height`/`cape` se il dominio non le espone) sono **omesse con
warning** nel manifest del dominio, senza crash.

### Stime di dimensione (misurato + stima)

- Misura reale (run di verifica, motore 1.2.4.4) su `dwd_icon_eu`, crop
  `194 x 172` = **33.368 celle**: un frame di **23 variabili** = **327.200 byte
  ~= 319,5 KiB** → **~13,9 KiB/variabile**; stima unione piena (24 var)
  **~334 KiB/frame**.
- Misura reale (run di verifica multi-dominio, questo motore) su `jma_gsm`, crop
  `25 x 22` = **550 celle**, 2 frame: **9.720 byte (18 var)** e **10.200 byte
  (19 var)** → **~19,4 KiB** totali per 2 frame. I crop grossi producono frame
  nell'ordine dei KiB (proporzionali al numero di celle).
- Il numero di frame per dominio = **orizzonte intero** del manifest S3 del
  dominio (variabile per run). Rilevazione del **2026-10-17** (manifest
  pubblicati `data/fm2/<dom>/latest.json` vs manifest upstream
  `openmeteo.s3.amazonaws.com/data_spatial/<dom>/latest.json`). **Tabella
  storica**: fotografa i 10 domini attivi fino alla 1.2.4.10; dalla 1.2.4.11 i
  domini attivi sono 7 (`V2_DOMAINS`), con `ncep_gfs013` al posto dei quattro
  rimossi:

  | dominio | `valid_times` pubblicato (con thinning) | `valid_times` senza thinning |
  |---|---|---|
  | `dwd_icon_eu` | 31 | 93 |
  | `dwd_icon_d2` | 49 | 49 |
  | `italia_meteo_arpae_icon_2i` | 57 | 73 |
  | `ecmwf_ifs025` | 81 | 85 |
  | `ncep_gfs025` | 117 | 209 |
  | `dwd_icon` | 83 | 93 |
  | `meteofrance_arpege_world025` | 67 | 67 |
  | `ukmo_global_deterministic_10km` | 81 | 89 |
  | `jma_gsm` | 23 | 45 |
  | `cma_grapes_global` | 41 | 41 |
  | **totale** | **630** | **844** (+34,0%) |

  I due conteggi non sono sempre lo stesso run (il manifest pubblicato può
  riferirsi a un `reference_time` precedente). **A parità di run** la
  decimazione rimuoveva **684 → 844 frame, +23,4%** (verificato il 2026-10-17
  sui 5 domini col `reference_time` allineato: `italia_meteo_arpae_icon_2i`
  57→73, `ecmwf_ifs025` 81→85, `ncep_gfs025` 117→209,
  `ukmo_global_deterministic_10km` 81→89, `cma_grapes_global` 41→41). Il
  conteggio reale va comunque letto dal campo `valid_times` del `latest.json`
  del dominio.

## Quantizzazione dei frame `.om` (novità 1.2.4.10)

**Convenzione `omfiles`.** Il writer memorizza ogni valore come intero:
`int = round(valore * scale_factor)`; il lettore ricostruisce
`valore = int / scale_factor`. Il **passo** di quantizzazione effettivo è quindi
`PASSO = 1 / scale_factor` (non `scale_factor`).

**Il bug (introdotto nella 1.2.4.6).** Il motore passava al writer
`scale_factor` uguale al **passo** dichiarato (0.1 per `temperature_2m`,
`pressure_msl`, `wind_gusts_10m`, `rain`, `precipitation`, ...), ottenendo
`PASSO = 1 / 0.1 = 10`: 10 °C, 10 hPa, 10 m/s, 10 mm/h. Le mappe risultavano a
4-5 bande piatte e tutta la pioggia sotto i 5 mm/h veniva arrotondata a 0, cioè
**trasparente**.

**Il fix (1.2.4.10).** La tabella è rinominata `VARIABLE_STEP` (contiene i
**passi**) e il writer riceve `scale_factor = 1.0 / VARIABLE_STEP.get(name,
DEFAULT_STEP)` (con `DEFAULT_STEP = 1.0`): il passo ricostruito torna uguale al
passo dichiarato (0.1 per le variabili continue, 1 per le percentuali, 10 per
`cape`/`freezing_level_height`). Regressione coperta da
`scripts/tests/test_forecast_v2_scale_step.py` (round-trip `omfiles` reale, che
fallisce col codice pre-fix: `20.75 → 20.0` e `4.0 → 0.0`).

**Nota NO-OP.** Il motore **non riscrive** i frame già pubblicati se
`reference_time`/`valid_times` del dominio non cambiano (guardia NO-OP
per-dominio): per applicare il fix subito servono `--force` (CLI) /
`force_update=true` (workflow); altrimenti basta il ciclo di forecast successivo,
che cambia `reference_time`.

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
  restituisce `values = speed`, `directions = direction`; se la secondaria
  `wind_direction_*` manca, il reader **lancia** `Secondary variable ... not found`.
  Per questo, dalla 1.2.4.11, il motore pubblica ENTRAMBE le variabili (velocita' e
  direzione sono due array separati nel `.om`) per 10 m, 850 hPa e 300 hPa: i reader
  che chiedono `wind_speed_*` trovano la `wind_direction_*` che si aspettano. Le
  componenti `u/v` restano solo INPUT interni e non sono piu' pubblicate.
- Tutte le altre variabili (`temperature_2m`, `cape`, `weather_code`, ...) sono
  lette come semplici (`values` = array, niente `directions`).

## Limiti noti

- **Download selettivo via HTTP Range (1.2.4.11)**: il frame remoto viene aperto
  con `fsspec`/`aiohttp` (`OmFileReaderAsync.from_fsspec`) e si leggono **solo** le
  variabili di `V2_READ_VARS` (pubblicate + input), non il file intero. **Fallback
  esplicito**: se `fsspec` manca, il backend HTTP non è disponibile o il formato
  remoto non è leggibile a intervalli, il motore scarica il **file intero** e lo
  registra (WARN + contatore); `--no-range` forza direttamente il percorso
  classico. Con `--frames-cache` i frame già scaricati sono ammortizzati tra run
  consecutive.
- Il NO-OP guard è **per-dominio** e confronta `reference_time` + `valid_times`
  del dominio: l'insieme dei frame è deterministico a parità di run del modello.
- I chunk di scrittura `.om` sono clampati alla dimensione effettiva dell'array
  (un dominio con crop molto piccolo, es. `25x22`, ha celle < 32 e `omfiles`
  rifiuta chunk più grandi dell'array).
- `pick_secondary_valid` e la fase "dominio secondario" sono **rimosse**: ogni
  variabile è presa solo dal dominio corrente. Chi invocava `--domains` con il
  primo dominio come "primario" deve ora aspettarsi un sottodirectory per
  dominio.

## Renderer WebP (`render_fm_webp.py`)

`render_fm_webp.py` è il renderer server-side della **fast-path raster**: legge
l'output del motore (`data/fm2/<dominio>/latest.json` + frame `<VALID>.om`),
dequantizza ogni variabile con `omfiles`, riproietta le righe in spaziatura Web
Mercator, colora ogni pixel con le scale WML v0.2.2 (`color_scales.json`) e
scrive, per ogni **regione** (`italia`, `europa`, `domain`) e **tema** (`light`,
`dark`), i WebP pre-renderizzati che il client consuma via `L.imageOverlay`. Per
ogni coppia (regione, dominio) il ritaglio è l'intersezione bbox dominio × bbox
regione sulla griglia del dominio; le coppie che non si intersecano sono saltate.
Dalla **1.2.4.9** il renderer applica un'**interpolazione bilineare dei valori**
prima della colorazione (opzione `--upsample`, default `auto`): i colori restano
le bande discrete della scala (nessun blending), vedi la sezione *Regione
`domain` e upsampling dei valori*.

### CLI

| Opzione | Descrizione |
|---|---|
| `--in DIR` | Radice dell'output motore con `<dominio>/latest.json` (default `data/fm2`). |
| `--out DIR` | Radice di output (default `data/fm2-rendered`). |
| `--regions R1,R2` | Allow-list regioni, separati da virgola (default: `italia,europa,domain`). |
| `--domains D1,D2` | Allow-list domini, separati da virgola (default: tutti quelli presenti in `--in`). Con `--index-only` è l'elenco **esplicito** dei domini resi con successo dagli shard. |
| `--vars V1,V2` | Allow-list variabili (default: ordine del manifest). |
| `--themes light,dark` | Temi colore (default `light,dark`; `dark` è scritto sotto `<var>/dark/`). |
| `--quality N` | Qualità WebP lossy (default 90). |
| `--lossless` | WebP lossless. |
| `--limit-frames N` | Renderizza al massimo N frame per dominio (debug); `0` = orizzonte intero del manifest. |
| `--method N` | Metodo di encode Pillow 0-6 (default `6` = massima compressione/più lento, output byte-compatibile col comportamento storico; la CI passa `4` come bilanciamento velocità/size). |
| `--upsample N\|auto` | Upsampling dei **valori** prima della colorazione: `auto` (default) → fattore `max(1, min(--upsample-max, ceil(512 / lato_corto_del_crop)))` per portare il lato corto a ~512 px; `1` = nessun upsampling (output **storico**, byte-identico); intero ≥ 2 = fattore fisso. Vedi *Regione `domain` e upsampling dei valori*. |
| `--upsample-max N` | Cap del fattore di `--upsample auto` (default `8`). |
| `--index-only` | Ricostruisce i soli `index.json` di regione dai manifest per-variabili già presenti in `--out` (con prune dei WebP orfani contro il keep-set del proprio manifest, per tema); nessun rendering, funziona anche **senza** numpy/Pillow/omfiles. `--domains` è obbligatorio: ogni manifest variabile non valido viene saltato interamente (nessun prune, nessuna voce in indice); se nessuna regione ha almeno un dominio valido esce `1` e **nulla** viene pubblicato. |

### Output (layout per-regione)

```
data/fm2-rendered/
  <regione>/                         # italia | europa | domain
    index.json                       # version, generated_at, region, theme, themes, bbox, domains
    <dominio>/
      <var>/
        <VALID>.webp                 # tema light
        latest.json                  # manifest per-var, tema light
        dark/
          <VALID>.webp               # tema dark
          latest.json                # manifest per-var, tema dark
  color_scales.json                  # scale colori pubblicate (single source of truth)
```

Ogni directory tema viene **prunata** contro il keep-set del proprio manifest
(solo `*.webp` orfani, per nome file). L'indice di regione aggrega i domini con
almeno una variabile valida; `themes` elenca solo i temi effettivamente presenti.

### Regione `domain` e upsampling dei valori (novità 1.2.4.9)

**Regione `domain`.** `REGIONS["domain"]` è una **sentinella `None`**: la regione
non ha un bbox fisso, ma coincide con l'**intero bbox del dominio** letto dal
manifest del motore (`resolve_region_bbox`):

- ritaglio = **nessuno**: `region_crop` restituisce l'intera griglia del dominio
  (`(0, ny, 0, nx)`), quindi il WebP copre tutto il dominio;
- **saltata per i domini globali** (`DOMAIN_REGION_SKIP_GLOBAL = True`): se il
  bbox del dominio è globale (`_is_global_bbox`: lat ±90 / lon ±180 sui domini
  worldwide) la regione `domain` **non viene resa**, perché "tutto il dominio"
  sarebbe il globo (già raggiungibile via `italia`/`europa`, e troppo grande per
  un singolo WebP). La regione è resa solo per i domini a copertura regionale
  (es. `dwd_icon_eu`, `dwd_icon_d2`, `ncep_gfs013`);
- l'output va in `data/fm2-rendered/domain/<dominio>/<var>/...` (stesso layout
  di `italia`/`europa`) e il suo `index.json` aggrega i domini indicizzati con il
  bbox **unione** dei loro bbox.

**Upsampling dei valori (`--upsample`).** Il renderer, prima della lookup dei
breakpoint, può **interpolare bilinearmente i valori** su una griglia più fitta
(`((nx-1)*scale+1, (ny-1)*scale+1)`, righe Mercator + colonne lineari in
longitudine):

- `--upsample auto` (default): `scale = max(1, min(--upsample-max,
  ceil(512 / min(nx, ny))))` sul **crop** della regione, mirando a ~512 px sul
  lato corto; `--upsample-max` (default `8`) limita il fattore;
- `--upsample 1`: nessun upsampling → output **storico** (row-only Mercator,
  regola due-tap, byte-identico al pre-1.2.4.9);
- `--upsample N` (N ≥ 2): fattore fisso.

Proprietà garantite (test dedicati `test_render_fm_webp_upsample.py`):

- i colori restano le **bande discrete** della scala WML: ogni pixel **finito**
  indossa **esattamente** una riga della palette, **nessun blending** dei colori
  (l'interpolazione avviene sui **valori**, non sui colori);
- **un NaN non acquisisce mai colore**: con `scale > 1` un pixel è emesso solo se
  **tutti** i suoi vertici bilineari sono finiti, altrimenti resta trasparente
  (`0,0,0,0`); con `scale == 1` vale la regola due-tap storica.

### Architettura CI (hotfix 1.2.4.8.1: shard per-dominio + merge single-writer)

Il vecchio job `render` monolitico del workflow `forecast-maps-v2.yml` (10 domini
× 2 regioni × 2 temi ≈ 55k encode WebP a Pillow `method=6`) moriva sistematicamente
a `timeout-minutes: 90` e `data/fm2-rendered` non veniva mai pubblicato: la
fast-path WebP del client non si attivava mai. Il workflow ora esegue:

1. **`render` (matrice per-dominio)** — un job per ciascuno dei 7 domini
   (`fail-fast: false`, concurrency `forecast-maps-v2-render-<dominio>`,
   `timeout-minutes: 150`): scarica l'artifact engine **esatto** `fm2-<dominio>`,
   renderizza solo quel dominio (`--domains <dominio> --limit-frames 0 --method 4`,
   tutte le regioni — `italia`, `europa`, `domain` —, entrambi i temi) in un
   albero vuoto e carica l'artifact **`fm2r-<dominio>`** (prefisso `fm2r-` così
   il download engine `fm2-*` del merge non aggancia gli shard; le path caricate
   sono solo `<regione>/<dominio>` per `italia`/`europa`/`domain`, quindi gli
   shard non pubblicano mai `index.json` né `color_scales.json`). Un dominio
   engine fallito fa fallire il solo shard corrispondente: quel dominio manca dal
   merge e viene escluso.
2. **`render-merge` (single-writer)** — un job (concurrency
   `forecast-maps-v2-render`, `timeout-minutes: 30`) che riscarica gli artifact
   engine (`fm2-*`) e tutti gli shard (`fm2r-*`), sostituisce ogni
   `data/fm2-rendered/<regione>/<dominio>` con la versione autorevole dello shard
   (`rm -rf` + `cp -a`: le variabili droppate vengono pulite), ricostruisce i
   `index.json` con `--index-only --domains <shard riusciti>` (escludendo i
   domini senza `latest.json` engine o senza manifest variabili validi) e fa
   **un solo commit** di WebP + `index.json` insieme (nessuna finestra in cui
   l'indice punta a file non ancora pubblicati). Zero shard validi ⇒ l'indice non
   viene scritto, il passo fallisce e nulla viene committato.

## Licenze / attribuzione

- Dati: **Open-Meteo** (https://open-meteo.com), licenza **CC BY 4.0** —
  attribuzione a Open-Meteo richiesta in ogni ridistribuzione.
- `omfiles` (GPL-2.0-only): usato **solo a runtime/CI** per leggere/scrivere il
  formato `.om`; non è venduto né incluso nel repository (dipendenza pip in
  `requirements.txt`: `omfiles>=1.0`, `numpy`, `Pillow>=10.0,<12`). Nessun segreto: l'accesso a S3 è
  anonimo.
