# MeteoRisk Light — dashboard meteorologica statica-first

Dashboard meteorologica statica-first per l'Italia: radar delle tempeste (DPC),
satellite EUMETSAT e dati Open-Meteo, servita interamente da GitHub Pages — nessun
back-end applicativo, nessuna dipendenza runtime oltre agli asset statici.

Repository: https://github.com/meteoriskitalia-netizen/MeteoRisk-Light

## Versione attiva: 1.2.4.11

- File applicativo: `mri-light-1.2.4.11.html` (`APP_VERSION = '1.2.4.11'`; ultimo blocco
  changelog in-app `{ version: '1.2.4.11', date: '2026-10-20' }`).
- Contenuto della 1.2.4.11 (release sopra la 1.2.4.10):
  - **Renderer/Client - palette pioggia riscritta**: 18 bande da 0 a 50 mm, di cui 17 opache
    (alpha 1.0; la sola banda sotto 0,1 mm resta trasparente = nessuna pioggia), contro le 15
    precedenti con le prime trasparenti o semitrasparenti e le ultime 3 identiche. I breakpoint
    0.01/0.055 erano sotto il quanto di 0.1 mm raggiungibile nei dati e il valore minimo positivo
    cadeva su alpha 0.4: la pioggia debole era un velo quasi invisibile.
  - **Renderer/Client - geopotenziale riscritto** sul modello Meteociel: 31 bande con alpha 1.0
    (era 0.7 su tutte), Z500 4920-6120 m passo 40, Z850 700-1900 m, Z1000 -800-400 m. Prima Z500
    aveva 11 bande con salti di 200 m, Z850 lasciava fuori il 23,5% dei valori e Z1000 l'82,4%.
  - **Renderer/Client - isobare e `pressure_msl`**: isobare ogni 5 hPa (erano 4); la banda di
    bianco pieno `[1010, 1016)` di `pressure_msl` (32,9% delle celle) diventa grigio chiaro nel
    solo tema chiaro (nel tema scuro il bianco resta).
  - **Client - palette unica**: legenda e WebP usano le stesse scale nostre, iniettate nel registro
    della libreria (`defaultOmProtocolSettings.colorScales`) e passate come terzo argomento a
    `getColorScale`. Prima la legenda leggeva la scala della libreria e la mappa quella del
    renderer: due palette diverse per la stessa variabile.
  - **Client - legenda a bande di uguale larghezza** (come Meteociel) con tooltip e intervallo, e
    unita' `mm · <passo>` calcolata dai `valid_times` della serie (1 h, 3 h o 6 h secondo il
    dominio e la fascia di orizzonte), omessa se non determinabile.
  - **Motore V2 - domini da 10 a 7**: restano `dwd_icon`, `dwd_icon_eu`, `dwd_icon_d2`,
    `ecmwf_ifs025`, `ncep_gfs025`, `meteofrance_arpege_world025`; rimossi
    `ukmo_global_deterministic_10km`, `jma_gsm`, `cma_grapes_global`, `italia_meteo_arpae_icon_2i`.
  - **Motore V2 - nuovo dominio `ncep_gfs013`**: GFS di superficie a 0,117° (griglia reale
    3072x1536, il nome "013" e' fuorviante), croppato sull'Europa con bbox `(34, -15, 62, 40)` come
    `dwd_icon_eu`. `ncep_gfs025` e' un prodotto solo upper-air e non contiene alcuna variabile di
    precipitazione (0 su 24): la sovrapposizione fra i due e' 0 variabili, quindi si tengono
    entrambi. Con `ncep_gfs013` arrivano precipitazione, nuvole a 3 livelli, neve e vento a 10 m a
    risoluzione doppia.
  - **Motore V2 - download selettivo via HTTP Range**: `fsspec` + `aiohttp` con
    `OmFileReaderAsync.from_fsspec` leggono dal frame remoto solo le variabili necessarie invece del
    file intero, con **fallback esplicito** al download completo se `fsspec` manca o il formato
    remoto non funziona (`--no-range` forza il percorso classico).
  - **Motore V2 - variabili derivate** (prima il motore risolveva solo nomi esatti): `dew_point_2m`
    (Magnus da T2m + RH2m), `wind_speed_*` e `wind_direction_*` (coppia calcolata dalle componenti
    u/v) e `precipitation_cum` (cumulata dall'inizio della run, sommando i frame in ordine di
    `valid_time`).
  - **Motore V2 - contratto del vento**: si pubblicano `wind_speed_10m/850hPa/300hPa` e
    `wind_direction_10m/850hPa/300hPa` in coppia; le componenti `u/v` non sono piu' pubblicate
    (restano letture interne). La libreria client `@openmeteo/weather-map-layer@0.2.2` tratta i nomi
    `_speed_`/`_direction_` come coppia e lancia se manca la secondaria; inoltre per i nomi
    `_[uv]_component` il client derivava gia' `sqrt(u^2+v^2)`, quindi pubblicare u/v mostrava cose
    diverse fra percorso `.om` e percorso WebP.
  - **Motore V2 - 31 variabili pubblicate** (erano 24): aggiunge `dew_point_2m`,
    `relative_humidity_700hPa`, le sei `wind_speed_*`/`wind_direction_*` e `precipitation_cum`,
    togliendo le componenti `u/v` a 10 m.
  - **Client - filtro disponibilita' variabili**: il selettore mostra solo le variabili realmente
    presenti nel dominio (campo `variables` del manifest, unione sui frame), invece di offrire mappe
    destinate a restare vuote (es. `surface_pressure`, assente in tutti i domini).
  - **Client - domini ed etichette**: `FM_DOMAINS` = 7 domini; nuove etichette 'Punto di rugiada
    2 m', 'Umidita' 700 hPa', 'Vento 10 m', 'Vento 850 hPa', 'Jet Stream 300 hPa', 'Cumulata
    precipitazione'.
  - **Test**: `test_forecast_v2_scale_step.py` (quantizzazione `.om`), `test_fm_webp_palette_sync.mjs`
    (coerenza palette renderer/client, con prova di sensibilita'), test della coppia vento e delle
    derivate nel motore.
  - **Nota dati**: i dati dei 4 domini rimossi **saranno potati dopo il deploy**, non in questa
    release.
- Contenuto della 1.2.4.10 (release sopra la 1.2.4.9):
  - **Motore V2 - fix quantizzazione `.om`**: `scale_factor` era passato uguale al passo (0.1)
    mentre la convenzione `omfiles` e' `valore = int / scale_factor`, quindi il passo effettivo
    diventava 10 (10 °C, 10 hPa, 10 m/s, 10 mm/h); le mappe risultavano a 4-5 bande piatte e la
    pioggia sotto i 5 mm/h finiva a 0, cioe' TRASPARENTE. La tabella diventa `VARIABLE_STEP`
    (passo) e il writer riceve `scale_factor = 1.0 / passo`.
  - **Motore V2 - guardia NO-OP**: il motore non riscrive i frame gia' pubblicati se il
    `reference_time` non cambia: servono `--force`/`force_update=true` oppure il ciclo di
    forecast successivo.
  - **Client - filtro disponibilita' variabili**: il selettore variabili mostra solo le variabili
    realmente presenti nel dominio (campo `variables` del manifest, unione sui frame),
    eliminando le mappe vuote (`surface_pressure` e' assente in tutti i 10 domini;
    `ncep_gfs025` pubblica 10 variabili su 24).
  - **Test**: `scripts/tests/test_forecast_v2_scale_step.py` (15 test, round-trip omfiles che
    fallisce col codice vecchio).
- Contenuto della 1.2.4.9 (release sopra la 1.2.4.8):
  - **CI - Mappe previsionali**: nuova regione di rendering WebP `domain` (intero dominio)
    accanto a Europa e Italia nel workflow `forecast-maps-v2.yml` (shard `render` + single-writer
    `render-merge`, indice `domain/index.json`).
  - **Client - legenda a segmenti con tick numerici**: la legenda delle Mappe previsionali
    e' composta da una banda per colore, posizionata in modo proporzionale ai valori reali,
    con tooltip dell'intervallo `[lo - hi)` e un asse a tick adattivi (max 9); fallback
    invariato per le scale non-breakpoint.
  - **Client - preload WebP parallelo (pool)**: il preload usa un pool a concorrenza
    (`FM_PRELOAD_CONCURRENCY`) con fast-path WebP (`new Image`) e fallback automatico al
    layer `.om` su errore/timeout.
  - **Client - fix Mappe previsionali**: gate tema scuro (`themes` array + path per-tema),
    race su metadati/indice (token di generazione), leak Chart.js (un solo `setInterval`
    condiviso + `clearInterval`/timeout), regione `domain` per il rendering per-dominio,
    upsampling mappe e rimozione thinning.
- Contenuto della 1.2.4.8 (release sopra la 1.2.4.7):
  - **Mappe previsionali - fast-path raster WebP pre-renderizzati**: il client serve le
    mappe come raster via `L.imageOverlay` dalla cache `data/fm2-rendered` (non piu' i frame
    `.om` scaricati per il layer raster).
  - **Renderer server-side**: nuovo `scripts/forecast_v2/render_fm_webp.py` (Pillow) che
    renderizza i WebP per regione, con indice per-regione `index.json` e `color_scales.json`.
  - **Regioni di rendering**: Europa e Italia; per ogni coppia regione/dominio il ritaglio
    e' l'intersezione tra il bbox del dominio e il bbox della regione.
  - **Workflow**: `forecast-maps-v2.yml` renderizza i WebP dopo il motore e li pubblica in
    `data/fm2-rendered`; i 3 workflow che copiano `data/fm2` in `_site` copiano anche
    `data/fm2-rendered`.
- Contenuto della 1.2.4.6 (release sopra la 1.2.4.5):
  - **Mappe previsionali - fix "schermata bianca"**: `fmRegisterDomain` ora registra/aggiorna
    la grid Italia sul dominio attivo (`fmDomain`) oltre al fallback legacy `fm2`
    (`applyGrid` in-place); le mappe previsionali tornano a visualizzarsi.
  - **Mappe previsionali - dominio rimosso**: rimosso il dominio CHMI ALADIN (2 km)
    (`chmi_aladin_central_europe_2km`) dall'elenco `FM_DOMAINS` delle Mappe previsionali
    (da 11 a 10 domini); nessun'altra modifica funzionale.
  - **Mappe previsionali cache-only**: rimossa la sorgente S3 (switch `fm-source-select`,
    fallback `useS3`, `fmSourcePref`, `fmSourceChanged`, `FM_BASE`); la cache `data/fm2`
    e' l'unica fonte; gli errori di `fmRegisterDomain`/`fm2FetchMeta` e di frame mancante
    sono ora visibili via `fmSetStatus` (prima muti -> possibile causa "mappe bianche");
    filtro variabili `FM_CURATED_VARS` applicato sempre.
  - **Mappe previsionali V2**: rimosso il client V2 dall'app; pannello "Mappe
    previsionali" con registro domini (`fmRegisterDomain`) nel blocco FM; set variabili curate.
  - **Motore forecast_v2**: orizzonte intero (`V2_MAX_FRAMES=0`, `--valid-frames 0`)
    con workflow `forecast-maps-v2.yml` allineato.
  - **Popup mappe previsionali in-frame**: su mobile il popup delle mappe previsionali
    non copre piu' l'intera pagina (in-frame nel viewport).
  - **Download tile piu' veloce**: block cache da 512 KB nelle richieste tile
    (da 9 a 3 richieste/tile).
  - **Formato numerico**: i valori meteo della UI passano dall'helper `fmtNum(v,d)`
    (blocco `//#pure# BEGIN numFormat`) con precisione fisica: temperatura/vento/pioggia
    1 decimale, umidita/pressione/probabilita interi; i float lunghi della pipeline IDW
    non vengono piu' stampati raw (in `updateUI`, `renderModelDaily`, tooltip chart e
    pannelli MNW); null/non-finiti -> `--`.
  - **Etichette zona**: le province mostrano "Provincia di <nome>" (pannello zona,
    tooltip rischio, riepilogo rischio, status); citta/comune restano con il nome nudo.
  - **Supercelle (LIVE)**: attivazione spostata nel pannello LIVE (rimosso il toggle
    dalla main); sub-toggle Traccia/Forecast/Fenomeni/Grandine nel LIVE, visibili a
    supercelle attive.
  - **Grandine**: nuova metrica sotto Sviluppo con due sottolivelli (Probabilita
    potenziale 0-5 e Dimensione stimata in mm) che colora province/celle (dati
    client-side da `hailMetrics`; soglie stima non calibrate).
  - **Player**: rimosso il "kick" di velocita' (il pacing al 1x resta 1000 ms/frame).
  - **Bordi/confini**: i confini regionali/provinciali sono ora visibili sopra il
    satellite (nuovo pane dedicato, stroke-only).
  - **Grandine**: nuovo toggle dedicato in UI.
  - **CAPPI**: CAPPI_2/CAPPI_6 attivati (overhang non piu' neutro).
  - **Vortex**: proxy hook echo su K frame coerenti + morfologia per-frame persistita
    (`tracks.json`).
  - **Grandine multi-prodotto**: POH primario + struttura verticale (VIL/ETM/CAPPI/overhang)
    + H0 per-cella + gate fulmini ricalibrato.
  - **Vortex**: riduzione falsi positivi + deduplica eventi (badge come evento, non per-frame).
  - **UI**: distinzione esplicita tra rischio ambientale NWP (potenziale) ed evento
    osservato radar.
  - **Nuovo**: logica overshooting top da IR_108 (DPC) sulla griglia radar; entra in
    SSI v2 e come corroboratore satellite di hail/vortex.
  - **Multifonte**: prototipo isolato adapter OPERA CIRRUS (`radar_engine/fusion/`), non attivo.
  - **Pannello LIVE**: ospita la funzione "Supercelle radar" (sub-toggle nel LIVE),
    raggiungibile anche senza fulmini.
  - **Mappe previsionali (1.2.4.0)**: nuova sezione basata su Open-Meteo Weather Map
    Layer (maps.open-meteo.com / openmeteo.s3.amazonaws.com, CC BY 4.0), nata al posto
    del vecchio modulo Meteociel (dismesso); solo visualizzazione sinottica, nessun
    impatto sui calcoli. Selettore modello/dominio (default DWD ICON EU 0.0625,
    `dwd_icon_eu`; 10 domini dopo la rimozione del dominio CHMI ALADIN nella 1.2.4.5), variabile e ora di previsione; selettore area
    geografica (11 voci: 10 aree bbox predefinite + "Intero dominio"); player con
    velocita' (Lenta/Normale/Veloce) e slider; tasto "Preload" con contatore di
    avanzamento + warm-ahead del frame successivo; layer serviti via protocollo `om://`;
    flag `forecastMaps` attivo di default.
- **Limiti noti**: l'overshooting top e' un proxy IR-only (senza WV/BTD); l'adapter
  OPERA CIRRUS e' un prototipo isolato e non attivo; le soglie hail/vortex sono
  sperimentali.
- Le release vivono in `releases\MeteoRisk-Light-X.Y.Z-GitHub-Production-Hardening\`.
- Regola dello storico: si PRESERVA. Mai rinominare una cartella di release esistente;
  per una nuova versione si crea una nuova cartella copiando lo stato attivo e bumpando
  solo i file sensibili (html, VERSION, workflow, test).

## Architettura dati

Sito statico piu' sei workflow GitHub Actions che aggiornano i dati e pubblicano su Pages:

| Workflow | Ruolo |
| --- | --- |
| `radar-engine.yml` | ogni 5 min scarica i prodotti radar DPC (VMI), rileva e traccia le celle convettive, committa **solo** i derivati in `data/radar/` (`latest.json`, `storms.geojson`, `tracks.json`, `supercells.json`) e l'archivio rolling su branch orfano `radar-history`, build dell'artefatto Pages e deploy |
| `satellite-engine.yml` | ogni 5 min scarica i frame WMS EUMETSAT (6 sorgenti: 3 Europa + 3 Italia), li ri-codifica in WebP e li pubblica con force-push sul branch orfano `satellite-cache` (finestra rolling di 25 slot = 2 ore). **Niente** build/deploy Pages in questo workflow: i frame arrivano su Pages perche' radar-engine e update-weather-data la restaurano da `satellite-cache` nell'artefatto di deploy |
| `update-weather-data.yml` | ogni 10 min: check del run ECMWF IFS + canary Best Match -> fetch Open-Meteo -> build, validazione e pubblicazione del dataset derivato in `data/latest/` (commit e deploy **solo** con nuovo dataset valido) |
| `phenomena-verify.yml` | ogni 10 min valuta i fenomeni grandine/rotazione dai derivati radar + fulmini MLI EUMETSAT (WMS anonimo, CC-BY-4.0) e committa `data/phenomena/{events,badges}.json` su `main` (niente build/deploy: i file arrivano su Pages nell'artefatto di radar-engine e update-weather-data) |
| `forecast-maps-v2.yml` | ogni 10 min genera la cache mappe previsionali V2 in `data/fm2/` (`<VALID>.om` + `latest.json`, crop Italia multi-variabile, orizzonte intero `--valid-frames 0`, prune), con cache CI dei frame full-size S3 (`actions/cache@v4` su `.cache_fm2_frames`) |
| `deploy-pages.yml` | deploy manuale/consolidamento: ricostruisce `_site/` con app + `data/latest`, `data/geography`, `data/radar` (incl. archivio rolling `radar-history`), `data/phenomena`, `data/fm2` e `satellite/` (finestra `satellite-cache`) e pubblica su Pages (`configure-pages` + `upload-pages-artifact@v4` + `deploy-pages@v4`, concurrency dedicata `pages-deploy` come gli altri) |

- I frame satellitari sono WebP a **risoluzione invariata**: stesso slot, stesso
  orologio e stessi formati dati della codifica precedente, solo meno byte.
- GeoTIFF radar raw e payload Open-Meteo non vengono mai committati: solo derivati.

## Trigger

Ogni workflow parte da `workflow_dispatch` e `repository_dispatch`:

- `ci-run` -> radar-engine + satellite-engine (dispatch esterno ogni 5 min);
- `ci-weather` -> update-weather-data (dispatch esterno ogni 10 min).

Il dispatch esterno e' **best-effort**: se non arriva, l'unico recupero e' un
trigger manuale via `workflow_dispatch`. Nessuna garanzia di latenza fissa sui dati.

## Test

Verifiche di riferimento (eseguite su questa release):

```bash
python -m pytest scripts\tests\radar_engine -q    # 167 test
python -m pytest scripts\tests\phenomena -q       # 126 test
python -m pytest scripts\tests -q                 # 460 test (totale)
python -m py_compile scripts\satellite_engine.py  # OK
python -c "import yaml,glob; [yaml.safe_load(open(f,encoding='utf-8')) for f in glob.glob('.github/workflows/*.yml')]"  # 6 workflow
node --test "scripts/tests/**/*.mjs"              # suite .mjs: 15 file test_*.mjs (più contract_dataset_loader.mjs, NON eseguibile senza dataset baseline)
```

Comandi e conteggi aggiornati sono quelli documentati in `AGENTS.md`.

## Convenzioni del repo

- **Push**: lo fa SEMPRE l'utente manualmente (tramite GitHub Desktop), oppure solo su
  esplicita richiesta dell'utente. Nessun push/commit/clone verso il remote senza
  richiesta esplicita.
- **Staging**: `GITHUB UPLOAD\` contiene SOLO il delta ADD+UPDATE, secondo la nota
  interna `_NOTE_UPLOAD_README.txt` (da leggere prima di operare).
- **Mai in staging**: `data/`, `__pycache__/`, `.pytest_cache/`, `.git/`, cartelle
  `releases\` vecchie.
- **Niente token/PAT** salvati nei file di configurazione o istruzioni.

## File chiave

- `mri-light-1.2.4.11.html` — applicazione (html/css/js unico, nessuna dipendenza runtime)
- `VERSION` — bump di versione e storico note di rilascio
- `.github/workflows/{radar-engine,satellite-engine,update-weather-data,phenomena-verify,deploy-pages,forecast-maps-v2}.yml` — i 6 workflow
- `scripts/radar_engine/` — motore radar DPC (Fase 1 + candidati supercelle, sperimentali)
- `scripts/satellite_engine.py` + `scripts/satellite_engine_requirements.txt` — download
  dei frame EUMETSAT e ri-codifica WebP
- `scripts/*.py` — pipeline meteo (check, decision, fetch, build, validate, publish)
- `scripts/tests/` — test pytest e suite `.mjs`
- `data/latest/` — dataset derivato (nello snapshot locale solo `.gitkeep`: il dataset
  reale e' generato e committato dai workflow)
- `docs/` — report e documentazione storica
