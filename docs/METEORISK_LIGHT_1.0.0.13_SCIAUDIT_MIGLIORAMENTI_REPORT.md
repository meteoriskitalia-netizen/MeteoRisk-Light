# MeteoRisk Light — Applicazione dei 14 MIGLIORAMENTI (bollettino audit)

**File in esame (dopo il bump e rename):** `mri-light-1.0.0.13.html`
**Versione:** 1.0.0.13 (BUILD 13)
**Data:** 2026-09-08
**Riferimento audit:** `METEORISK_LIGHT_1.0.0.9_SCIENTIFIC_AUDIT_IT_MEDITERRANEO.md`

---

## Quadro

L'audit scientifico ha etichettato **51 reperti**:
- 🔴 CRITICO: 2 → applicati in **1.0.0.12** (C1 geometria AGL, C2 ML-SHIP).
- 🟠 IMPORTANTE: 8 → applicati in **1.0.0.12** (stagionalità, convezione elevated, cap mediterranei, …).
- 🟡 **MIGLIORAMENTO: 14** → applicati in questa **1.0.0.13**.
- 🟢 CORRETTO: 27 → non richiedono intervento.

I 14 bollettini 🟡 sono stati suddivisi in: **modifiche di codice** (struttura/calibrazione), **documentazione/verifica**, e **già coperti in 1.0.0.12 / non attuabili con i campi Open-Meteo disponibili**. Di seguito la tabella di mappatura.

## Elenco dei 14 MIGLIORAMENTI e stato

| # | Reperto (sez. audit) | Descrizione | Esito in 1.0.0.13 |
|---|---|---|---|
| 1 | §4.4 / §8.5 | Mean-wind Bunkers come **integrale per strato** (centroide 0-6km) invece della media aritmetica che sovra-pesa i livelli bassi | **CODICE** — nuove `levelsAGLTable()` + `meanWindStrata()`; `bunkersRM(levels06, z06)` pesa per spessore geometrico AGL, con fallback aritmetico se mancano le quote. |
| 2 | §5.6 / §8.6 | Aggiungere **SRH 0-1km e bulk shear 0-1km** (discriminanti tornado significativi, Thompson 2003) | **CODICE** — `computeSevereIndices` espone `shear01` (sfc-850hPa) e `srh1`; nuovo canale low-level in `detectVorticosi` per i tornado/waterspout non-supercellari. |
| 3 | §6 | Soglie SRH 0-3km "estreme" allineate alle **basi liguri** (CFMI-PC: 150-300 / 300-450 / >450) | **CODICE** — escalation `detectVorticosi` su bands liguri; `SRH>=150` da sola → livello 2. |
| 4 | §4.9 | Downburst: caps/coefficienti **ad hoc non calibrati** ("sperimentale") | **DOC** — commento SCIAUDIT che li qualifica Mediterraneo-sperimentali; nessun peso ritoccato in attesa della validazione empirica F5 raccomandata dall'audit (§12/§15). |
| 5 | §6 / §8.7 | Freezing-level melting >4500: **verifica vs Po Valley 2023** | **VERIFICA+DOC** — il taglio >4500m AGL colpisce solo small/small-medium (→none) e retrocede di un passo la medium-large, preservando large/very-large: il caso Po Valley 2023 con grandine gigantesca e H0 alto resta coperto. Nessun cambio del gate. |
| 6 | §5.4 | LCL di Lawrence (125·ΔT): preferire **LCL esatto da profilo** quando disponibile | **NON ATTUABILE (dati)** — Open-Meteo non espone un LCL di livello: resta Lawrence, documentato; gate AGL già ammorbidito in stagione fredda (1.0.0.12, felicità 5#2). |
| 7 | §4.7 | Cap SHIP su DLS (≤27) può troncare l'HSLC italiano | **GIÀ COPERTA (1.0.0.12, C2)** — cap DLS alzato a 30, MR cap mediterranei 7-13.6. Solo verifica qui. |
| 8 | §5.5 | Doppia penalizzazione storica (gate melting + fattore continuo) | **GIÀ CORRETTO (1.0.0.12/F6) + DOC** — i due meccanismi coprono fasce separate e non si sommano; qui solo verifica. |
| 9 | §4.1 | CAPE MLCAPE vs MUCAPE (input): sottostima HSLC | **GIÀ COPERTA (1.0.0.12, C2)** — ripesata come ML-SHIP e floor CAPE ambientale abbassato in stagione fredda. |
| 10 | §4.2 | Lapse rate 700-500 su **geopotenziali** non geometrici; MR di superficie come proxy parcella | **GIÀ COPERTA (1.0.0.12, C2)** — lapse rate 700-500 ora geometrico AGL (`geoToMetres`); MR di superficie documentato come proxy. |
| 11 | §6 | Gate CAPE meso ottimizzabile **stagionalmente** | **GIÀ COPERTA (1.0.0.12, 5#1)** — stagionalità `seasonInfo()` rilassa i gate HSLC in inverno/spalle. |
| 12 | §6 | DLS gate / soglie HSLC degradate in **mesi freddi e coste** | **GIÀ COPERTA (1.0.0.12, 5#1/5#2)** — maschera stagionale + canale low-level (1.0.0.13) per la costa. |
| 13 | §4.3 / §8.6 | Shear/SRH su **livelli non equidistribuiti**; ~10m-wind come superficie | **PARZIALE** — storm-motion ora integrale per strato (riduce il bias di spaziatura, #1); il vento 10m resta l'unica superficie esposta da Open-Meteo, documentato come approssimazione. |
| 14 | §8.5 (bis) | Sostituire media aritmetica con integrale pesato (centroide 0-6km) | **CODICE** — stesso intervento del #1 (una sola modifica, due reperti). |

## Dettaglio delle modifiche di codice

### 1) Mean-wind Bunkers INTEGRALE (§4.4/§8.5)
Nel blocco cinematico (prima di `computeSevereIndices`):
- `geoToMetres(gph)` / `geoAGL(gph, elev)` — già presenti da C1 (1.0.0.12).
- **`levelsAGLTable(gph850, gph700, gph500, terrainElevation)`** (nuova): stima le quote geometriche AGL di ogni livello (1000→0, poi 850/700/500 noti dagli anchor Open-Meteo, e 975/950/925/900/800/600 interpolati in **log-pressione** tra gli anchor). Ritorna mappa `pl → zAGL(m)`; i livelli mancanti restano `null`.
- **`meanWindStrata(levels, zAGL)`** (nuova): `Σ(w·ΔH)/Σ(ΔH)` con `w` media lineare dei venti ai bordi di ogni strato e `ΔH` lo spessore geometrico — il centroide densità-approssimato. Fallback alla media aritmetica se le quote non sono disponibili.
- `bunkersRM(levels06, z06)` — ora accetta le quote e usa `meanWindStrata`.

Beneficio: su livelli di pressione NON uniformemente spaziati (più densi in basso) la vecchia media aritmetica sovra-pesava i livelli bassi allontanando lo storm-motion dal centroide 0-6km; il nuovo integrale per strato corregge quel bias.

### 2) Shear 0-1km + SRH 0-1km (§5.6/§8.6)
In `computeSevereIndices`:
- `var l850 = wnd[850]; var shear01 = l850 ? bulkShearUV(sfc, l850) : 0;`
- `var levels01 = [sfc, ...(1000..850)]; var srh1 = srh03km(levels01, sm)`.
- Nuovi campi nel profilo e nei due rami fail-closed: `shear01`, `srh1`.
- `out` di `detectVorticosi` aggiunge `shear01`/`srh1`.

In `detectVorticosi` un **canale low-level** riconosce i tornado/waterspout non-supercellari del Mediterraneo (spesso deep-shear modesto ma basso strato elicoide):
```
var lowLevelStrong = (shear01 >= 10 && srh1 >= 90) || (srh1 >= 140 && shear06 >= (cold ? 10 : 12));
if (thunder && (lowLevelStrong || (ehi >= 1.5 && srh3 >= 300 && shear06 >= 18))) level = 4;
```

### 3) SRH 0-3km allineata alle basi liguri (§6)
Escalation `detectVorticosi` con i bands CFMI-PC (150-300 moderato, 300-450 forte, >450 estremo) al posto dei valori "estremi" USA; `SRH>=150` da sola → livello 2.

### 4-5) Documentazione e verifica (§4.9, §6/§8.7)
- Downburst: commento che qualifica i pesi/caps come **Mediterraneo-sperimentali** (non da curva ROC), senza ritoccarli (attesa validazione F5).
- Melting gate: commento che documenta la verifica vs Po Valley 2023 (large/very-large preservati).

## Non modificati (per scelta, giustificata dall'audit)
- **Formule compositi**: SCP, EHI, WMAXSHEAR, ML-SHIP, WMAXSHEAR03 — l'audit (§4.5/4.6/4.8/4.10) le ha valutate 🟢 e la validazione empirica F5 è prerequisito prima di ritoccare i pesi del sevScore.
- **Palette / colorazione mappe** legata allo slider orario (nessun cambio).

## Regressione e test
- `node --check` sul blocco script principale (1.6 MB): **OK**.
- Suite `.mjs` completa: **9/9 PASS**.
- `test_sciaudit.mjs` esteso: mantiene le check C1/C2/stagionalità e aggiunge **17 nuove check** sui miglioramenti (meanWindStrata uniforme/peso/fallback, levelsAGLTable con ordine altimetrico monotono, shear01/srh1 nel profilo e nel vorticosi, bands liguri, doc downburst/melting).

## Versione e distribuzione
- **BUILD 13**, `APP_VERSION='1.0.0.13'`, `APP_RELEASE_DATE='2026-09-08'`, entry `APP_CHANGELOG` (1.0.0.13) e `VERSION` (`NOTE` + `SCIAUDIT3`).
- Rinomati: `mri-light-1.0.0.13.html` e cartella `MeteoRisk-Light-1.0.0.13-GitHub-Production-Hardening`; riferimenti funzionali (9 test, VERSION, workflow, README) ripuntati.
- Zip rigenerato: `MeteoRisk-Light-1.0.0.13-GitHub-Production-Hardening.zip` (77 entry, struttura flat); zip 1.0.0.12 rimosso.

## Raccomandazioni residue (per versioni successive)
- **F5** (audit §10/§15): validazione empirica contro catalogo ESWD 2018-2023 (POD/FAR prima-dopo) prima di ritoccare i pesi del sevScore e dei caps downburst.
- **F2 (regime 'elevated')**: quando/disponibilità futura di MUCAPE da profilo o LCL di livello, valutare condizione operativa dedicata per la grandine autunnale/notturna.
