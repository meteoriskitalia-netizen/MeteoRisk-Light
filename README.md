# MeteoRisk Light — dashboard meteorologica statica-first

Dashboard meteorologica statica-first per l'Italia: radar delle tempeste (DPC),
satellite EUMETSAT e dati Open-Meteo, servita interamente da GitHub Pages — nessun
back-end applicativo, nessuna dipendenza runtime oltre agli asset statici.

Repository: https://github.com/meteoriskitalia-netizen/MeteoRisk-Light

## Versione attiva: 1.2.0

- File applicativo: `mri-light-1.2.0.html` (`APP_VERSION = '1.2.0'`, changelog
  entry `{ version: '1.2.0', date: '2026-10-08' }`).
- Contenuto della 1.2.0: nowcasting e tracking radar (archivio rolling 2h su branch
  `radar-history` con tracce storiche nel client, previsione percorso/intensita' fino a
  2h con cono di incertezza e confidenza), marker supercelle ridisegnati (pennant SVG
  direzionale con severita' SSI e stato grandine/rotazione), engine di verifica fenomeni
  (grandine/rotazione) con provider fulmini MLI EUMETSAT (WMS anonimo, CC-BY-4.0),
  badge fenomeni nel client con legenda aggiornata e guardia anti-commit-churn sugli
  output phenomena.
- Le release vivono in `releases\MeteoRisk-Light-X.Y.Z-GitHub-Production-Hardening\`.
- Regola dello storico: si PRESERVA. Mai rinominare una cartella di release esistente;
  per una nuova versione si crea una nuova cartella copiando lo stato attivo e bumpando
  solo i file sensibili (html, VERSION, workflow, test).

## Architettura dati

Sito statico piu' quattro workflow GitHub Actions che aggiornano i dati e pubblicano su Pages:

| Workflow | Ruolo |
| --- | --- |
| `radar-engine.yml` | ogni 5 min scarica i prodotti radar DPC (VMI), rileva e traccia le celle convettive, committa **solo** i derivati in `data/radar/` (`latest.json`, `storms.geojson`, `tracks.json`, `supercells.json`) e l'archivio rolling su branch orfano `radar-history`, build dell'artefatto Pages e deploy |
| `satellite-engine.yml` | ogni 5 min scarica i frame WMS EUMETSAT (6 sorgenti: 3 Europa + 3 Italia), li ri-codifica in WebP e li pubblica con force-push sul branch orfano `satellite-cache` (finestra rolling di 25 slot = 2 ore). **Niente** build/deploy Pages in questo workflow: i frame arrivano su Pages perche' radar-engine e update-weather-data la restaurano da `satellite-cache` nell'artefatto di deploy |
| `update-weather-data.yml` | ogni 10 min: check del run ECMWF IFS + canary Best Match -> fetch Open-Meteo -> build, validazione e pubblicazione del dataset derivato in `data/latest/` (commit e deploy **solo** con nuovo dataset valido) |
| `phenomena-verify.yml` | ogni 10 min valuta i fenomeni grandine/rotazione dai derivati radar + fulmini MLI EUMETSAT (WMS anonimo, CC-BY-4.0) e committa `data/phenomena/{events,badges}.json` su `main` (niente build/deploy: i file arrivano su Pages nell'artefatto di radar-engine e update-weather-data) |

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
python -m pytest scripts\tests\radar_engine -q    # 140 test
python -m pytest scripts\tests\phenomena -q       # 73 test
python -m pytest scripts\tests -q                 # 362 test (totale)
python -m py_compile scripts\satellite_engine.py  # OK
python -c "import yaml,glob; [yaml.safe_load(open(f,encoding='utf-8')) for f in glob.glob('.github/workflows/*.yml')]"  # 4 workflow
node --test "scripts/tests/**/*.mjs"              # suite .mjs: 13 file, 13 PASS
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

- `mri-light-1.2.0.html` — applicazione (html/css/js unico, nessuna dipendenza runtime)
- `VERSION` — bump di versione e storico note di rilascio
- `.github/workflows/{radar-engine,satellite-engine,update-weather-data,phenomena-verify}.yml` — i 4 workflow
- `scripts/radar_engine/` — motore radar DPC (Fase 1 + candidati supercelle, sperimentali)
- `scripts/satellite_engine.py` + `scripts/satellite_engine_requirements.txt` — download
  dei frame EUMETSAT e ri-codifica WebP
- `scripts/*.py` — pipeline meteo (check, decision, fetch, build, validate, publish)
- `scripts/tests/` — test pytest e suite `.mjs`
- `data/latest/` — dataset derivato (nello snapshot locale solo `.gitkeep`: il dataset
  reale e' generato e committato dai workflow)
- `docs/` — report e documentazione storica
