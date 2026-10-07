# MeteoRisk Light — dashboard meteorologica statica-first

Dashboard meteorologica statica-first per l'Italia: radar delle tempeste (DPC),
satellite EUMETSAT e dati Open-Meteo, servita interamente da GitHub Pages — nessun
back-end applicativo, nessuna dipendenza runtime oltre agli asset statici.

Repository: https://github.com/meteoriskitalia-netizen/MeteoRisk-Light

## Versione attiva: 1.1.0.8

- File applicativo: `mri-light-1.1.0.8.html` (`APP_VERSION = '1.1.0.8'`, changelog
  entry `{ version: '1.1.0.8', date: '2026-10-07' }`).
- Contenuto della 1.1.0.8: frame satellite ri-codificati **WebP** (~10x piu' leggeri),
  indicatore di staleness discreto (chip + tooltip, soglia radar 20 min) e push
  resilience in `update-weather-data` (fetch + pull --rebase + retry 5x prima del push).
- Le release vivono in `releases\MeteoRisk-Light-X.Y.Z-GitHub-Production-Hardening\`.
- Regola dello storico: si PRESERVA. Mai rinominare una cartella di release esistente;
  per una nuova versione si crea una nuova cartella copiando lo stato attivo e bumpando
  solo i file sensibili (html, VERSION, workflow, test).

## Architettura dati

Sito statico piu' tre workflow GitHub Actions che aggiornano i dati e pubblicano su Pages:

| Workflow | Ruolo |
| --- | --- |
| `radar-engine.yml` | ogni 5 min scarica i prodotti radar DPC (VMI), rileva e traccia le celle convettive, committa **solo** i derivati in `data/radar/` (`latest.json`, `storms.geojson`, `tracks.json`, `supercells.json`), build dell'artefatto Pages e deploy |
| `satellite-engine.yml` | ogni 5 min scarica i frame WMS EUMETSAT (6 sorgenti: 3 Europa + 3 Italia), li ri-codifica in WebP e li pubblica con force-push sul branch orfano `satellite-cache` (finestra rolling di 25 slot = 2 ore). **Niente** build/deploy Pages in questo workflow: i frame arrivano su Pages perche' radar-engine e update-weather-data la restaurano da `satellite-cache` nell'artefatto di deploy |
| `update-weather-data.yml` | ogni 10 min: check del run ECMWF IFS + canary Best Match -> fetch Open-Meteo -> build, validazione e pubblicazione del dataset derivato in `data/latest/` (commit e deploy **solo** con nuovo dataset valido) |

- I frame satellitari sono WebP a **risoluzione invariata**: stesso slot, stesso
  orologio e stessi formati dati della codifica precedente, solo meno byte.
- GeoTIFF radar raw e payload Open-Meteo non vengono mai committati: solo derivati.

## Trigger

Ogni workflow parte da `workflow_dispatch`, `schedule` (cron) e `repository_dispatch`:

- `ci-run` -> radar-engine + satellite-engine (dispatch esterno ogni 5 min);
- `ci-weather` -> update-weather-data (dispatch esterno ogni 10 min).

Il dispatch esterno e' **best-effort**: se non arriva, il cron ridondante del workflow
fa da backup. Nessuna garanzia di latenza fissa sui dati.

## Test

Verifiche di riferimento (eseguite su questa release):

```bash
python -m pytest scripts\tests\radar_engine -q    # 108 test
python -m pytest scripts\tests -q                 # 251 test
python -m py_compile scripts\satellite_engine.py  # OK
python -c "import yaml,glob; [yaml.safe_load(open(f,encoding='utf-8')) for f in glob.glob('.github/workflows/*.yml')]"  # 3 workflow
node scripts/tests/<file>.mjs                     # suite .mjs: 12 file, 12 PASS
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

- `mri-light-1.1.0.8.html` — applicazione (html/css/js unico, nessuna dipendenza runtime)
- `VERSION` — bump di versione e storico note di rilascio
- `.github/workflows/{radar-engine,satellite-engine,update-weather-data}.yml` — i 3 workflow
- `scripts/radar_engine/` — motore radar DPC (Fase 1 + candidati supercelle, sperimentali)
- `scripts/satellite_engine.py` + `scripts/satellite_engine_requirements.txt` — download
  dei frame EUMETSAT e ri-codifica WebP
- `scripts/*.py` — pipeline meteo (check, decision, fetch, build, validate, publish)
- `scripts/tests/` — test pytest e suite `.mjs`
- `data/latest/` — dataset derivato (nello snapshot locale solo `.gitkeep`: il dataset
  reale e' generato e committato dai workflow)
- `docs/` — report e documentazione storica
