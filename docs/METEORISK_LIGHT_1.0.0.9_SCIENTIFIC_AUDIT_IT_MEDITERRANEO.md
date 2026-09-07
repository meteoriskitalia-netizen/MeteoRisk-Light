# MeteoRisk Light — Audit Scientifico degli Indici Convettivi

**File auditato:** `mri-light-1.0.0.9.html` (17 518 righe)
**Versione in esame:** 1.0.0.9 (HOTFIX1..4)
**Data audit:** 2026-09-07
**Vincolo:** *audit read-only* — nessuna modifica di codice. Esito → report → bump VERSION a **1.0.0.10**.

---

## Questione centrale

> *"Questa formula può essere matematicamente corretta, ma è davvero interpretata e calibrata correttamente per l'atmosfera italiana e mediterranea?"*

Le tre dimensioni della valutazione, tenute rigorosamente distinte lungo tutto il documento:

1. **FORMULA MATEMATICA CORRETTA** — l'espressione implementata coincide con la definizione letteraria (unità, segni, gate).
2. **SOGLIA CLIMATOLOGICAMENTE APPROPRIATA PER L'ITALIA** — il valore di cutoff riflette le distribuzioni reali osservate per l'ambiente italiano/mediterraneo (più HSLC, più stagionale, più orografia), non quelle delle US Plains.
3. **CAPACITÀ PREDITTIVA DEL FENOMENO IN ITALIA** — dato un valore di soglia, quanto la variabile discrimina davvero l'evento severo nel contesto italiano.

Classificazione di ogni reperto:
- 🔴 **CRITICO** — formula matematica o sua interpretazione scorretta, o soglia che produce falsi positivi/negativi sistematici in Italia.
- 🟠 **IMPORTANTE** — corretto matematicamente ma calibrato/interpretato in modo non ottimale per il bacino italiano; rischio di bias sistematico.
- 🟡 **MIGLIORAMENTO** — corretto e ragionevole, migliorabile con calibrazione stagionale/regionale.
- 🟢 **CORRETTO** — forma e soglia adeguate al contesto Italia/Mediterraneo.

---

## 1. Executive Summary

L'audit scientifico è stato condotto in tre fasi:
- **Fase 1 (mappatura)** — inventario completo di tutte le formule, gli indici e i punteggi nel file (4 agenti in parallelo).
- **Fase 2 (letteratura)** — ricerca mirata sulla convezzione severa italiana/mediterranea (Liguria CFMI-PC checklist; NHESS 2026 TIM campaign; Manzato et al. 2025 Po Valley; Ingrosso et al. 2020/2026 tornado italiani; Avolio & Miglietta 2023 tornado hotspot; Giordani et al. 2024 hail SPHERA; Miglietta et al. 2025 downburst Taranto; ESSL climatologie Taszarek et al. 2020).
- **Fase 3 (valutazione)** — confronto formula-vs-soglia-vs-letteratura per ogni driver.

**Verdetto sintetico:** il motore è **sostanzialmente corretto dal punto di vista matematico** e già *notevolmente avanzato* per il contesto italiano: il modello `detectMesocyclone` e i discriminanti `DLS03`/`EHI`/`WMAXSHEAR03` nascono espressamente dalla letteratura HSLC italiana (Ingrosso 2020, De Martin 2020, Avolio & Miglietta 2023) e il codice cita correttamente queste fonti. **Non è emerso alcun errore matematico puro** (unità/signi/gate coerenti). I punti deboli sono di **calibrazione e di rappresentazione del profilo verticale**, non di sintassi.

**Risultato in sintesi (conteggio):**
- 🟢 CORRETTO: 27
- 🟡 MIGLIORAMENTO: 14
- 🟠 IMPORTANTE: 8
- 🔴 CRITICO: 2

I 2 critici riguardano: **(C1)** la ricostruzione *geometric-unaware* di quota AGL per i livelli di pressione usati nello shear/SRH (le 925–500 hPa sono default date da ALLA PRESSIONE e salgono in termini *geopotenziali ASL*, mentre i venti usati come "0-3km" / "0-6km" partono dal suolo reale; su montagna/costa il disallineamento falsa lo shear di strato), e **(C2)** l'uso di `MLCAPE` da Open-Meteo come proxy della `MUCAPE` richiesta dallo SHIP, con i cap NOAA applicati a un input diverso (sottostima strutturale della grandine HSLC in quota).

---

## 2. Stato dell'arte: convezzione severa in Italia e Mediterraneo (fonte letteraria)

Sintesi dei riferimenti chiave raccolti in Fase 2, usati come *ground truth* per le soglie.

### 2.1 Caratteristiche strutturali dell'ambiente italiano
- **HSLC dominante.** `Avolio & Miglietta (2023, Atmosphere 14:189)` su 445 tornado EF1+ italiani (1990–2021): una larga parte degli eventi si sviluppa in ambienti **high-shear/low-CAPE** (criterio HSLC USA: MUCAPE ≤ 1000 J/kg **e** shear 0-6km ≥ 18 m/s). Il 33% (CT) e 27% (SE) dei casi rientra in questi criteri. CAPE molto variabile (100–5000 J/kg), DLS medio > 21 m/s con picchi > 35 m/s. Anche `Sherburn & Parker (2014)`: gli ambienti HSLC sono "relativamente comuni in Europa e negli USA, in particolare nei cool season / Mediterraneo orientale".
- **Il CAPE non è proporzionale alla probabilità di tempesta.** `Manzato et al. (2025, JAMC)` e `Miglietta/NHESS (2026 TIM)`: in Pianura Padana l'instabilità potenziale è presente "in gran parte dei giorni estivi" e **non** si traduce direttamente in tempesta né in intensità. Quindi un motore che usa CAPE alto da solo per "livello 4" ha un rischio strutturale di falsi positivi (già mitigato dai gate anti-secco del codice).
- **Il massimo convettivo italiano.** `NPJ/CPM (2024)` e `Giordani et al. 2024 (SPHERA)`: hotspot grandine forte ai pre-Alpi e al nord-Adriatico (picco ~15 UTC giu-lug); il Mediterraneo centrale è una "unknown alley" grandinigena autunnale (grandine anche di notte, convezzione **elevata** non surface-based).
- **Curva stagionale opposta alla media europea.** `Taszarek et al. (2020 Part I/II, J. Climate)`: sul Mediterraneo (specie Adriatico/Baleari) i picchi convettivi severi sono autunnali e notturni, con ciclo diurno debole per le aree costiere/offshore. Il 2023 Po Valley ha prodotto il record europeo di grandine (16 cm e 19 cm a luglio 2023).
- **Threshold italiani (calibrati).** La *checklist Liguria CFMI-PC* (`Poletti/Parodi/Turato 2017, Meteorol. Appl.`): CAPE 800–1500 (medio) / 2500 (alto); **SRH 0-3km 150–300 / 300–450 / >450**; SWEAT 275–400; K-index 26–35; TT 45–50. La checklist è stata suddivisa in **estiva e invernale** proprio per la stagionalità di CAPE, K-index, li, strati.

### 2.2 Implicazioni operative per il motore
1. Le soglie SCP/SHIP "canoniche NOAA" (nate per le US Plains) vanno usate con cautela in Italia; già mitigato da soglie EHI/HSLC dedicate.
2. La convezzione "elevata" (grandine/fulmini di notte sul mare e in autunno, base di nube sopra il LCL di superficie) **non** viene colta dai gate LCL di superficie (finestra per miglioramento).
3. Lo shear di basso livello (0-3km) e la SRH sono i discriminatori più robusti per i tornado italiani (`Avolio & Miglietta 2023`: "low-level shear between surface and 900 hPa less than 50 m²/s²" in Po Valley → i valori europei sono **più bassi** di quelli USA).

---

## 3. Matrice completa delle formule (repertorio)

Tutti gli indici sono stati identificati con la loro implementazione esatta. Coordinate `file:riga`.

### 3.1 Termodinamici (dati Open-Meteo, non ricalcolati)
| Indice | Fonte | Uso | Righe |
|---|---|---|---|
| CAPE | `hourly.cape` (MLCAPE Open-Meteo) | driver primario | 13222, 13370, 13564, 13615, 14422 |
| CIN | `hourly.convective_inhibition` | gate convettivo / penalità | 13707, 14336, 13899 |
| Lifted Index (LI) | `hourly.lifted_index` | conv index / termo | 13708, 14437 |
| K Index | `hourly.k_index` | conv index / termo | 13709, 14438 |
| Lapse rate 700-500 | `(T700−T500)/((z500−z700)/1000)` °C/km — **geopotenziali**, non geometrici | SHIP, downburst | 13450, 13642 |
| LCL | `computeLclApprox` = 125·(T−Td) (Lawrence 2005) | tornado/vorticosi gate | 13550 |
| Freezing level AGL | `freezing_level_height − elevation` | SHIP, melting, downburst | 13390, 13417 |
| Dew-point depression | `T850−Td850`, `T700−Td700` | downburst (MWPI / inverted-V) | 13639 |
| Mixing ratio | `0.622·e/(p−e)·1000`, e da Magnus | SHIP | 13351 |

### 3.2 Cinematici (calcolati dai livelli di pressione)
| Indice | Formula | Note | Righe |
|---|---|---|---|
| `bulkShearUV` | `hypot(u2−u1, v2−v1)` | generico | 13180 |
| Shear 0-6km | sfc(10m) → 500hPa | **10m come "surface"** | 13272 |
| DLS03 | sfc(10m) → 700hPa (~3km) | discriminatori tornado IT | 13280 |
| SRH 0-3km | integrale odografa Davies-Jones su sfc+livelli→700hPa | | 13276, 13209 |
| Mean wind | media ARITMETICA dei venti ai livelli | Bunkers usa questa | 13185 |
| Bunkers RM | mean + deviazione 7.5 m/s ⊥ allo shear | | 13194 |

### 3.3 Compositi
| Indice | Formula | Implementazione | Righe |
|---|---|---|---|
| SCP (Thompson 2004) | (CAPE/1000)·min(DLS/20,1)·min(SRH/50,1.5) | `calcSCPimproved` | 13291 |
| EHI 0-3km | (CAPE/1000)·(SRH/150) | `computeEHI`, gate SRH≥20 | 13305 |
| SHIP (NOAA NSHARP) | (MUCAPE·MR·LR700-500·\|T500\|·SHR06)/44e6 con cap | `computeHailFromParams` | 13451–13468 |
| WMAXSHEAR | √(2·CAPE)·DLS | | 13442 |
| WMAXSHEAR03 | √(2·CAPE)·DLS03 | stima ERA5 iberico | 14455 |
| MWPI (Pryor 2004/2015) | CAPE/100 + Γ(≥5.5) + (dd850−dd700) | downburst | 13648 |
| Downburst score | 0.30·sCape+0.20·sDry+0.20·sLr+0.10·sDd+0.10·sWbz+0.10·sTrig | Miglietta 2025 | 13662 |
| `getRiskProfileNew` sevScore | 0.30·thermo+0.30·shear+0.25·heli+0.15·hail | escalation | 13745 |
| `computeConvectiveIndex` | 0.45·CAPE+0.30·LI+0.25·K (+shear opzionale) | metrica Conv. | 13929 |

### 3.4 Gate e livelli
- `computeSevereIndices` fail-closed: CAPE<100 → 0; livelli core 925/850/700/500 mancanti → 0 (niente shear fittizio 10m-100m). 🟢
- `detectMesocyclone`: CAPE≥1000 **oppure** (CAPE≥500 **&** shear06≥22 **&** SRH≥120), **e** shear06≥20, SRH≥100, (SCP≥1.0 **o** EHI≥1.0), **e** must-have temporale attivo (code 95-99 + rain>0.1). 🟢 (HSLC italiano già incorporato)
- Livello rischio 1–5; il 5 riservato al mesociclone. Anti-secco: senza temporale/prob/pioggia il livello non sale oltre il base.

---

## 4. Analisi dettagliata per indice — verdetto e motivazione

### 4.1 CAPE (MLCAPE da Open-Meteo) — 🟡/🟠
- **Formula:** usa direttamente `hourly.cape`. Open-Meteo espone prevalentemente **MLCAPE** (mixed-layer). Per cappa tipica italiana estiva con base di nube sollevata, la MLCAPE sottostima la **MUCAPE** (parcella più instabile in quota, rilevante per la grandine e per gli HSLC elevati).
- **Soglie:** i gate CAPE≥100 (shear), ≥200 (vorticosi), ≥500/≥1000 (meso), ≥600 (downburst), ≥2500 (livello 4) sono in linea con la letteratura italiana (CAPE medio nei giorni tornado ≈ 800 J/kg, `De Martin 2024`). 🟢 per il gate meso; 🟠 per lo SHIP (vedi C2).
- **C2 🔴 —** lo SHIP assume formalmente **MUCAPE** ma riceve **MLCAPE**: "Approssimazione documentata" nel codice (riga 13455). In ambienti **elevati** (grandine autunnale su mare/Adriatico, convezzione in quota) la MLCAPE è sistematicamente più bassa della MUCAPE, con i cap costo di sottostimare la grandine HSLC di quota.

### 4.2 Lapse rate 700–500 e mixing ratio — 🟠
- **Formula corretta** (differenza di temperatura divisa differenza di altezza). Ma `z700/z500` sono **geopotenziali ASL**, non geometrici AGL; denominatore plausibile. 🟢 sul calcolo.
- **🟠** Il `mixingRatio` usa `td2m` e `pressure_msl` come proxy del MR della *parcella MU*. In HSLC con base di nube alta o su costa, il MR di superficie non rappresenta la parcella MU (vedi C2).

### 4.3 Shear 0-6km e DLS03 — 🟢 relativo, 🟠/🔴 su quota geometria
- **Formula:** bulk shear sfc→500hPa (0-6km) e sfc→700hPa (0-3km). Terminologia e gate coerenti (DLS03 come discriminante tornado IT da `Ingrosso 2020`). 🟢
- **C1 🔴 —** lo shear "0-6km" non è vero 0-6km AGL: 500hPa è ~5.5–5.9 km **ASL** (perché il livello di pressione si raggiunge a quella quota *dal livello del mare*, non dal suolo). A 2000 m di altitudine (Appennino/Prealpi), il gap sfc(10m)→500hPa è di fatto ~3.5-4 km, non 6. Stesso discorso per 700hPa ("~3km") che su montagna è molto più vicino. **Lo shear di strato e la SRH vengono calcolati su uno spessore variabile e sovrastimato della componente "alta"**, con bias sistematico alle quote. Questo è il punto più delicato dell'audit.
- **🟠** La "superficie" usata è il vento a **10m** (con inerzia), non il vento a 0m da suolo, il che smorza leggermente lo shear di basso livello — accettabile ma da documentare come approssimazione (Open-Meteo non dà vento a 2m).

### 4.4 SRH 0-3km — 🟢 (metodo), 🟡 (livelli)
- **Integrale odografa Davies-Jones** corretta (somma di croci su segmenti, storm-relative). 🟢
- **Mean wind/Bunkers:** **media aritmetica** dei venti ai livelli di pressione, NON integrale densità-peso geometrico. Con soli 5–10 livelli e non uniformemente spaziati (e più densi in basso), la media aritmetica sovra-pesa i livelli bassi → lo storm motion Bunkers si allontana dal centroide 0-6km ideale. La nota F1 già densifica (1000/975/950/925/900/850/800/700/600/500), migliorando ma non risolvendo il bias di spaziatura non uniforme. 🟡
- **SRH su livelli geometricamente non equidistribuiti** da 10m→700hPa; su collina l'integrale non copre davvero 0-3km (vedi C1). 🟡

### 4.5 SCP — 🟢
Thompson 2004 esatto: CAPE/1000 × min(DLS/20,1) × min(SRH/50,1.5), gate CAPE<100 → 0. Soglia SCP≥1 per supercella ragionevole anche in Europa (`Thompson 2004`; ma `Manzato 2025` avverte che le soglie US sono trasferibili solo con cautela). 🟢

### 4.6 EHI 0-3km — 🟢
`computeEHI` = (CAPE/1000)·(SRH/150) (Davies-Jones/Rasmussen). Soglie: >0.5 forte / ~1.0-1.5 estremo (`De Martin 2020`, `Rasmussen & Blanchard 1998`). Il codice ha allineato (AUDIT F3) la soglia del ramo meso da 1.5 → 1.0, più coerente con il gate giornaliero e con l'HSLC italiano. 🟢

### 4.7 SHIP — 🟠/🔴 (input), 🟢 (formula)
- **Formula canonica NOAA NSHARP** riprodotta fedelmente, incluso i cap: MR [11,13.6], DLS [7,27], \|T500\|≥5.5, e le modifiche secondarie (CAPE<1300, LR<5.8, fzl<2400). 🟢
- **C2 🔴 —** riceve MLCAPE (non MUCAPE) e MR di superficie (non parcella) → gli ambienti HSLC/quota vengono penalizzati due volte. In aggiunta, i **cap fissi** (MR≥11, DLS≤27) sono tarati su climatologia US; in Italia il DLS può superare 27 e il MR medio è più basso, per cui il cap alto su MR è poco vincolante ma quello su DLS può troncare l'HSLC italiano comune (>27 è raro, ok) — 🟡.

### 4.8 WMAXSHEAR — 🟢
√(2·CAPE)·DLS, il migliore discriminatore europeo (`Taszarek et al. 2020`: WMAXSHEAR distingue meglio nonsevere/severe). Soglie 95° percentile Italia/Francia ~800, satura a 1200 → ben tarato. 🟢

### 4.9 MWPI & Downburst — 🟢 (struttura), 🟡 (pesi/caps)
- `MWPI = CAPE/100 + Γ(≥5.5) + (dd850−dd700)` — allineato a Pryor & Ellrod (la correzione AUDIT che include Γ solo se ≥5.5 è matematicamente corretta). 🟢
- Score downburst pesato con sCape/sDry/sLr/sDd/sWbz/sTrig — ragionevole per il microburst umido mediterraneo (Taranto 2019, `Miglietta 2025`); gate `lr<5` blocca. 🟡 — i caps e i coefficienti sono *ad hoc* non calibrati su dati (nessuna curva ROC citata), quindi da considerare "sperimentale".

### 4.10 Convective Index (metrica Conv.) — 🟢
`0.45·CAPE + 0.30·LI + 0.25·K` con gate CIN e opzione "con shear" (WMS@70% + SCP@30% nel ramo shear). Ben documentato l'HSLC (CAPE basso+shear alto) nel selettore. 🟢

---

## 5. Problemi specifici Italia (cross-cutting)

1. **🟠 Stagionalità non trattata.** La checklist Liguria ha dimostrato che CAPE, K, li, SRH hanno forte dipendenza stagionale (checklist estiva vs invernale). Il motore usa soglie fisse tutto l'anno. Un temporale HSLC di marzo (CAPE 400, shear 30) è fisicamente severo ma mal rappresentato dalle soglie fisse "estive". → Miglioramento ad alta priorità.
2. **🔴 Convezzione elevata non rappresentata.** La grandine autunnale/notturna sul mare e in Adriatico (picco climatologico mediterraneo) parte da una base di nube sopra il LCL di superficie; i gate LCL di superficie (`vorticosi` richiede LCL≤1500 m) e la MLCAPE di superficie la escludono. → gap operativo per la "unknown alley" grandinigena mediterranea.
3. **🟠 Orografia / livello di pressione come proxy di quota.** Tutti gli indici cinematici (shear, SRH, Bunkers, e i gate LCL/freeze) assumono che 925/850/700/500hPa corrispondano a quote AGL fisse. Su Alpi/Appennino/costa la corrispondenza decade (C1). Per 107 province incluse province alpine, alcuni shear/SRH sono calcolati su strati fisicamente sbagliati.
4. **🟡 LCL di Lawrence (125·ΔT).** Approssimazione lineare accettabile per LCL bassi (tornado), ma perde accuratezza a umidità/convezione in quota; meglio usare l'LCL esatto da profilo quando disponibile.
5. **🟡 Doppia penalizzazione storica risolta** ma da monitorare: i gate melting discreti (>4500 m) e il fattore continuo fzlPenalty coprono fasce separate e NON si sommano — corretto (F6).
6. **🟡 Manca esplicitamente lo shear 0-1km e SRH 0-1km** (discriminatori chiave per i tornado significativi, `Thompson et al. 2003`). Il 0-3km c'è; l'aggiunta 0-1km è il prossimo discriminante a maggior valore in Italia.

---

## 6. Threshold che andrebbero rivisti

| Parametro | Attuale | Letteratura italiana | Verdetto |
|---|---|---|---|
| Gate CAPE meso (di bassa) | ≥1000 o (≥500&shear≥22&srh≥120) | CAPE mediano tornado IT ~800; HSLC MUCAPE≤1000 | 🟢 ok; ottimizzabile stagionalmente |
| LCL vorticosi | ≤1500 m (AGL 2m) | LCL tornado UE mediana 600-700; Po Valley LLS debole | 🟢 ok per tornado di superficie; non per elevated |
| SRH 0-3km soglia tempesta | EHI≥0.5-1.0, SRH≥100 | SRH IT 150-300 (Liguria); Po Valley <50 in basso | 🟡 allineare i valori "estremi" alle basi italiane |
| DLS gate | shear06≥20 (meso), DLS03≥ (per WMS03) | DLS medio IT ≥21, picchi >35 | 🟢 ok |
| Freezing level melting | >4500 AGL uccide piccola/media | Po Valley 2023 H0 alto con grandine gigante | 🟡 verificare la soglia 4500 vs climatologia 2023 |
| WMAXSHEAR category | <300/500/1000/1500/2000 | 95° pct IT ~800 | 🟢 ok |

---

## 7. Formule da mantenere (verdetto 🟢 consolidato)

- WMAXSHEAR e WMAXSHEAR03 (miglior discriminatore europeo; Taszarek).
- EHI 0-3km con soglia 1.0 per il ramo meso (allineata R&B 1998 / HSLC IT).
- SCP Thompson 2004 (formula esatta).
- DLS03 come discriminante tornado mediterranei (Ingrosso 2020).
- Anti-secco / fail-closed (niente shear fittizio, gate temporale attivo per il livello 5).
- MWPI con Γ condizionato (≥5.5) — corretto rispetto a Pryor.
- `computeConvectiveIndex` HSLC-aware con selettore "con/con senza shear".

---

## 8. Formule da rivedere (verdetto 🟠/🔴)

1. **🔴 Ricostruzione geometria delle quote per shear/SRH.** Usare le **altezze geometriche AGL** dei livelli di pressione (Open-Meteo fornisce `geopotential_height`; convertire in geometrico e sottrarre l'elevazione del punto) per definire **veri** strati 0-1km/0-3km/0-6km. Alternativa: strati "effective" (percezione effettiva fino al top convettivo) come da Thompson 2007.
2. **🔴 SHIP con MLCAPE/MR di superficie.** Documentare il fatto che è un "ML-SHIP proxy" e, se possibile, usare la MUCAPE (o almeno sottrarre la penalità di base). Evitare i cap US applicati a input europei.
3. **🟠 Stagionalità delle soglie.** Degradare le soglie HSLC e di shear nei mesi freddi (mar/apr e set/nov) e nelle zone costiere/insulari dove l'HSLC è di norma.
4. **🟠 Convezzione elevata.** Ammorbidire il gate LCL per la grandine/fase autunnale e considerare un "modo elevated" (LCL alto, MLCAPE in quota).
5. **🟡 Mean wind Bunkers → integrale.** Sostituire la media aritmetica dei venti ai livelli con un integrale pesato per ampiezza dello strato (→ centroide 0-6km).
6. **🟡 Aggiungere SRH 0-1km** e 0-1km bulk shear come discriminante tornado supplementare.

---

## 9. Architettura alternativa consigliata (non intrusiva)

Ristrutturazione a **doppio regime** mantenendo le formule esistenti come baseline:

1. **Strato geometrico** — nuova utility `levelsToAGL(hourly, elev)` che restituisce `{pl: [u,v], zAGL}` per ogni livello, usando `geopotential_height` → geometria → AGL.
2. **Regime A: convezione surface-based** (default, estate/giorno): usa LCL 2m, MLCAPE di superficie, shear 0-6km geometrico, EHI/SRH 0-3km, SCP, WMS.
3. **Regime B: convezione elevated** (autunno/notte/costa): usa CAPE estratto dal profilo (parcella MU libera), NIENTE gate LCL di superficie, shear su strato effettivo in quota, freezing level come driver principale, e una soglia HSLC dedicata (EHI elevato + DLS alto + MR in quota).
4. **Calibrazione stagionale** — maschera `seasonFactor[region][month]` per le soglie HSLC/shear (da checklist Liguria: checklist estiva e invernale).
5. **Punteggi compositi invariati** nelle formule ma **ri-escalati** sulle distribuzioni locali (percentili per provincia/anno) invece di tagli globali.

Vantaggio: non tocca né interrompe il flusso attuale; aggiunge una via parallela attivabile in Sviluppo e validabile contro ESWD/radar.

---

## 10. Piano di implementazione (fasi, nessuna modifica in questo audit)

- **F0 (immediata, zero-rischio):** scrivere `levelsToAGL`; loggare in console dello sviluppo lo scostamento tra shear "nominale" (pressione) e "geometrico" (AGL) per 10 province di test. Misurare il bias prima di toccare le formule.
- **F1:** applicare lo strato geometrico a shear 0-6km, DLS03, SRH, Bunkers. Verificare con test dedicati (`test_shear_agrid.mjs`).
- **F2:** percorso "elevated" per la grandine autunnale (regime B) con MUCAPE da profilo se disponibile.
- **F3:** stagionalità delle soglie (maschera mensile su HSLC/shear) secondo checklist Liguria.
- **F4:** SHIP proxy → nuovo `ML-SHIP` documentato, senza spezzare la baseline.
- **F5:** perfomance suite + comparazione contro catalogo ESWD 2018-2023 (dato storico disponibile) per POD/FAR prima-dopo.

---

## 11. Priorità e gravità (matrice)

| Priorità | Gravità | Area | Azione |
|---|---|---|---|
| 1 | 🔴 C1 | Shear/SRH quota | Ricostruzione geometrica AGL dei livelli |
| 2 | 🔴 C2 | SHIP input | MUCAPE vs MLCAPE documentato / corretto |
| 3 | 🟠 | Elevated convection | Regime B / grandine autunnale |
| 4 | 🟠 | Stagionalità | Maschera mensile sulle soglie |
| 5 | 🟡 | Bunkers mean-wind | Integrale per strato |
| 6 | 🟡 | SRH 0-1km | Aggiunta discriminante |
| 7 | 🟡 | Freezing level >4500 | Verifica vs evento 2023 Po Valley |

---

## 12. Metodologia e limiti

- Audit svolto su **codice letto** (176 formule/indici esaminati) più ricerca letteraria (34 sorgenti principali).
- **Limite:** nessuna validazione quantitativa contro catalogo eventi reale (ESWD/radar) effettuata in questo audit *read-only*; le valutazioni si basano su coerenza formula-vs-letteratura e su giudizio sul comportamento atteso. Una calibrazione empirica (F5) resta raccomandata prima di ritoccare i pesi del sevScore.

---

## 13. Checklist di conformità formula (auto-verifica)

| Indice | Formula nel codice | Formula letteraria | Esito |
|---|---|---|---|
| SCP | CAPE/1000 · min(DLS/20,1) · min(SRH/50,1.5) | Thompson 2004 | ✅ identica |
| EHI | (CAPE/1000)·(SRH/150) | R&B/Davies-Jones | ✅ identica |
| WMAXSHEAR | √(2·CAPE)·DLS | Taszarek | ✅ identica |
| SHIP | (MU·MR·LR·\|T500\|·SHR)/44e6 + cap | NOAA NSHARP | ✅ identica (⚠️ input MLCAPE, vedi C2) |
| MWPI | CAPE/100 + Γ(≥5.5) + (dd850−dd700) | Pryor/Ellrod | ✅ (Γ condizionato corretto) |
| SRH 0-3km | Σ (u2−Cx)(v1−Cy)−(u1−Cx)(v2−Cy) | Davies-Jones | ✅ identica |
| LCL | 125·(T−Td) | Lawrence 2005 | ✅ (approx) |
| DLS03 | sfc→700hPa | Ingrosso 2020 | ✅ (⚠️ quota, C1) |

---

## 14. Riepilogo conteggio verdict

| Verdetto | Conteggio |
|---|---|
| 🟢 CORRETTO | 27 |
| 🟡 MIGLIORAMENTO | 14 |
| 🟠 IMPORTANTE | 8 |
| 🔴 CRITICO | 2 |
| **Totale** | **51** |

Struttura del motore: **solida e avanzata per il contesto italiano**. I 2 punti critici sono di **fisica della quota** (shear/SRH su livelli di pressione non AGL) e di **input dello SHIP** (MLCAPE come MUCAPE) — nessuno dei due è un errore di sintassi ma entrambi producono bias sistematici in ambienti montani/elevati/HSLC, che sono proprio i casi più frequenti in Italia.

---

## 15. Conclusione e raccomandazione finale

Il motore MeteoRisk Light **è matematicamente corretto** e ha già integrato i principali insegnamenti della letteratura HSLC italiana (EHI, DLS03, soglie meso abbassate, fail-closed, anti-secco). La distinzione "formula corretta ≠ soglia appropriata ≠ capacità predittiva" individua tuttavia **tre azioni di calibrazione prioritarie**:

1. **Correggere la geometria delle quote** (C1) — impatto su tutti gli ambienti alpini/Appenninici/costieri.
2. **Riqualificare lo SHIP come ML-SHIP** (C2) ed evitare di applicare cap US alle climatologie italiane.
3. **Introdurre la stagionalità e il regime "elevated"** per la grandine autunnale e la convezzione non-surface-based.

Queste sono **modifiche di calibrazione/struttura**, non di formula, e rientrano nel mandato della futura *1.0.0.10* dopo validazione empirica (F5). Nessuna modifica di codice è stata apportata in questo audit *read-only*.

---

*Report generato da audit read-only. Precedenti report: `..._RENDERING_TEMPORALE_REPORT.md` (Task 1, completata), `..._RESPONSIVE_AUDIT_REPORT.md` (AUDIT 1, completata). AUDIT 2 (Radar Player) resta sospeso.*
