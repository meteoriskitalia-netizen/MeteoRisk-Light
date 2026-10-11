# Forecast V2 — mappe previsionali `.om` (server-side)

Motore che costruisce in GitHub Actions una **cache GitHub di mappe previsionali**
in formato Open-Meteo `.om`: per ogni istante di previsione un file multi-variabile
croppato sull'Italia, leggibile dal reader WML 0.2.2 usato dal client (formato
verificato bit-exact nel POC `spike_omfiles/`).

## Cosa produce

- `data/fm2/<YYYY-MM-DDTHH00>.om` — un file per valid_time, multi-variabile, crop
  Italia sulla griglia del **dominio primario** (`V2_DOMAINS[0]` = `dwd_icon_eu`).
- `data/fm2/latest.json` — manifest con `reference_time`, `valid_times`, `domains`,
  `grid` del crop, `variables` (per frame e per variabile: dominio sorgente),
  `sizes`, `frames`, `warnings`.
- Al massimo `V2_MAX_FRAMES` (default 25) frame: i piu' vecchi vengono rimossi.

Il client legge un file con `readVariable(url, "<nome_var>", ranges)` dove `ranges`
sono gli indici del crop (vedi `grid` nel manifest: `nx`, `ny`, `lonMin`, `latMin`,
`dx`, `dy`).

## Uso

```bash
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains dwd_icon_eu ecmwf_ifs025 \
  --valid-frames 25 \
  --out data/fm2

# Con cache persistente dei frame (riuso tra run, meno egress S3):
python scripts/forecast_v2/forecast_v2_engine.py \
  --domains dwd_icon_eu ecmwf_ifs025 \
  --valid-frames 25 \
  --out data/fm2 \
  --frames-cache .cache_fm2_frames
```

### Opzioni CLI

| Opzione | Descrizione |
|---|---|
| `--domains D1 D2 ...` | Domini in ordine di priorita'. Il primo definisce la griglia del crop. Default: `dwd_icon_eu ecmwf_ifs025`. |
| `--valid-frames N` | Numero di valid_times da pubblicare (default 25). |
| `--out DIR` | Directory di output (default `data/fm2`). |
| `--once` | Un solo ciclo ed esce (default/compat). |
| `--no-prune` | Non rimuove i frame oltre il limite. |
| `--dry-run` | Stampa il piano senza scaricare/scrivere. |
| `--force` | Ignora il NO-OP guard (riscrive sempre). |
| `--cache-dir DIR` | Directory dei download temporanei (default: temp di sistema; flat `<dominio>__<VALID>.om`, nessuna verifica). |
| `--frames-cache DIR` | Cache **persistente** dei frame full-size, layout `<DIR>/<dominio>/<VALID>.om`. Un frame in cache e' riusato **senza download** se la sua `Content-Length` combacia con quella remota (HEAD leggero; fallback: size locale se la HEAD non e' disponibile). Default: vuoto = disabilitata (comportamento invariato). |

Exit code: `0` = run pulita (anche NO-OP), `1` = errore (nessun frame scritto,
dominio/griglia ignoti, manifest primario assente).

## Cache dei frame ed egress CI

Il job CI `forecast-maps-v2` usa `actions/cache@v4` sulla directory
`.cache_fm2_frames` (chiave `fm2-frames-${{ github.run_number }}`, `restore-keys:
fm2-frames-`): ad ogni run ripristina l'ultima cache disponibile e la risalva
aggiornata a fine run. La cache GitHub e' **LRU, 10 GB per repo**: quando si
supera il limite le entry piu' vecchie vengono sfollate automaticamente (le run
piu' vecchie non sono piu' ripristinabili, ma il motore riscarica solo cio' che
manca, quindi resta corretto).

Con `--frames-cache` il motore, per ogni `<dominio>/<VALID>.om`:

1. se il file e' in cache e la `Content-Length` remota combacia (HEAD) -> **riuso,
   nessuna GET del frame**;
2. se manca o e' stale -> una sola GET, salvata in cache per le run successive.

Risultato atteso: l'egress S3 passa da ~800 MB/run (intera finestra riscaricata)
a ~40 MB/ciclo (solo i frame nuovi che entrano nella finestra ~ogni ora di run
del modello).

## Fonti e formato

- S3 anonimo: `https://openmeteo.s3.amazonaws.com/data_spatial/<dominio>/<YYYY>/<MM>/<DD>/<HH00Z>/<YYYY-MM-DDTHH00.om`
  e, come indice, `<dominio>/latest.json`.
- Le variabili pubblicate possono venire dal primario o, se assenti nel file di
  quell'istante, da un dominio secondario **ri-campionato nearest-neighbor** sulla
  griglia del primario (nessuna interpolazione).
- Variabili **derivate** lato server quando manca la sorgente diretta:
  `wind_speed_10m = sqrt(u^2+v^2)`, `wind_direction_10m = atan2(-u,-v)` (0=N),
  `dew_point_2m = Magnus(T2m, RH2m)`. `surface_pressure` non e' derivabile senza
  quota: se assente in tutti i domini viene **omessa con warning** nel manifest.

## Set variabili pubblicate

Il motore pubblica l'**unione** di due gruppi, nello stesso ordine del manifest
(`var_order`):

- **17 variabili di rischio** (base): `temperature_2m`, `relative_humidity_2m`,
  `dew_point_2m`, `pressure_msl`, `surface_pressure`, `wind_speed_10m`,
  `wind_direction_10m`, `wind_u_component_10m`, `wind_v_component_10m`,
  `wind_gusts_10m`, `cloud_cover`, `cape`, `snow_depth`, `precipitation`, `rain`,
  `showers`, `weather_code` (di cui `dew_point_2m`, `wind_speed_10m`,
  `wind_direction_10m` sono **derivate** quando manca la sorgente diretta).
- **10 variabili `FM_CACHE core`** (pass-through dirette, `sources=[nome]`,
  nessuna nuova derivazione): `temperature_850hPa`, `temperature_500hPa`,
  `relative_humidity_850hPa`, `geopotential_height_500hPa`,
  `geopotential_height_850hPa`, `geopotential_height_1000hPa`, `cloud_cover_low`,
  `cloud_cover_mid`, `cloud_cover_high`, `freezing_level_height`. Servono a far
  leggere al pannello "Mappe previsionali" la cache `data/fm2` al posto delle
  letture S3 client-side (studio FM_CACHE).

Totale: **27 variabili** in `var_order`. Le variabili assenti nel frame (es.
accumuli/flussi di analisi, o `freezing_level_height` se un dominio non la
espone) sono **omesse con warning** nel manifest, senza crash (stesso
comportamento di `surface_pressure`).

### Stime di dimensione (misurato + stima)

Crop Italia `194 x 172` = **33.368 celle**; compressione
`pfor_delta_2d_int16`. Misura reale (run di verifica): un frame di **23
variabili** (frame di analisi, con `rain`/`showers`/`surface_pressure`/
`weather_code` omesse) = **327.200 byte ~= 319,5 KiB** -> **~13,9 KiB/variabile**.

- Stima **unione piena (27 var): ~375-386 KiB/frame**.
- Stima **finestra 25 frame: ~9,2-9,4 MiB**.

Sono **stime** (dipendono dalla comprimibilita' del campo e da quante variabili
esistono davvero nel frame); il valore sui 23 var e' **misurato**.

## Come il client (reader WML 0.2.2) legge le variabili

Attenzione: il reader WML **non** restituisce sempre l'array cosi' com'e' scritto,
applica delle *derivation rules* basate sul nome:

- Nomi che contengono `_[uv]_component` o `_[uv]_current` (es.
  `wind_u_component_10m`, `wind_v_component_10m`): il reader legge **entrambe** le
  componenti e restituisce `values = sqrt(u^2+v^2)` (velocita') e
  `directions = direzione meteorologica` (0..360). Le componenti grezze NON sono
  accessibili come singolo array via `readVariable`.
- Nomi che contengono `_speed_` / `_direction_` (es. `wind_speed_10m`,
  `wind_direction_10m`): il reader legge la **coppia** (speed + direction) e
  restituisce `values = speed`, `directions = direction`. Per questo il motore
  scrive **sia** le componenti `u/v` **sia** `wind_speed_10m`/`wind_direction_10m`:
  cosi' entrambe le convenzioni di nome funzionano e nessuna richiesta di vento
  fallisce per variabile mancante.
- Tutte le altre variabili (`temperature_2m`, `cape`, `weather_code`, ...) sono
  lette come semplici (`values` = array, niente `directions`).

Verifica fatta: per un frame prodotto, una richiesta client di
`wind_u_component_10m` restituisce `values` (velocita', mean 4.3922) e
`directions` (mean 229.5351 deg); `wind_speed_10m` restituisce `values`=speed
(mean 4.3895) e `directions`=direction (mean 229.5348 deg). I valori "grezzi" nel
file restano comunque leggibili bit-exact con `omfiles` (verificato).

## Disponibilita' variabili nel frame di analisi

Il `latest.json` di un dominio elenca un superset di variabili. Nel **frame di
analisi** (`valid_time == reference_time`) alcune variabili (accumuli/flussi e
codici: `precipitation`, `rain`, `showers`, `weather_code`, `wind_gusts_10m`)
**non esistono** e compaiono solo ai passi di previsione successivi. Il motore
rileva la presenza **per-file** e, in quel caso, tenta il dominio secondario;
se anche il secondario non le ha, le **omette con warning** nel manifest.


## Limiti noti (V1 del motore)

- Il download avviene a **file intero** dal S3 (nessuna range-read): e' quanto
  Open-Meteo mette a disposizione via HTTP; l'output pubblicato e' invece piccolo
  (~320 KiB misurati per un frame di 23 variabili; ~375-386 KiB stimati per
  l'unione di 27 — vedi "Stime di dimensione"). Il secondario viene scaricato **solo** se il primario
  di quell'istante manca di una variabile richiesta (tipicamente il frame di
  analisi, `valid_time == reference_time`). Con `--frames-cache` questi download a
  file intero sono ammortizzati tra run consecutive; senza cache (o a cache fredda)
  il costo resta quello del file intero.
- Il match della variabile del secondario sceglie il valid_time piu' vicino entro
  tolleranza (`V2_SECONDARY_MAX_DT_HOURS` = 3h).
- Il NO-OP guard confronta `reference_time` + `valid_times`: deriva dal fatto che
  l'insieme dei frame e' deterministico a parita' di run del modello.

## Licenze / attribuzione

- Dati: **Open-Meteo** (https://open-meteo.com), licenza **CC BY 4.0** — attribuzione
  a Open-Meteo richiesta in ogni ridistribuzione.
- `omfiles` (GPL-2.0-only): usato **solo a runtime/CI** per leggere/scrivere il
  formato `.om`; non e' venduto ne' incluso nel repository (dipendenza pip in
  `requirements.txt`). Nessun segreto: l'accesso a S3 e' anonimo.
