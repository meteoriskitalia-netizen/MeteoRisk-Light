# LICENSES — MeteoRisk Italia – Light (Public Educational Edition)

**Versione:** 1.0.0.0 · **Data:** 2026-09-04

**STATO PRELIMINARE — NON SONO RILEVATE LICENZE FORMALI NEL CODICE.**

Nessuna dichiarazione di licenza completa è presente all'interno del file HTML per la maggior parte delle dipendenze. Le uniche indicazioni rilevate nel codice sono segnalate sotto. Ogni voce marcata **DA VERIFICARE ESTERNAMENTE** va confermata sui canali ufficiali del fornitore prima della pubblicazione.

> ⚠️ **Nota legale:** questo file NON è un parere legale. Lo scopo non commerciale del progetto non autorizza automaticamente l'uso di dati o servizi di terze parti.

| Componente | Licenza/termine rilevato nel codice | Stato |
|---|---|---|
| Open-Meteo API (forecast) | Nessuna dichiarata ("Servizio gratuito; API key opzionale") | DA VERIFICARE ESTERNAMENTE |
| Open-Meteo Geocoding | "Licenze: CC-BY / condizioni Open-Meteo" (help del codice) | CONFERMARE sui termini correnti |
| Modelli meteo via Open-Meteo (ECMWF, GFS, ICON, ARPAE ICON-2I) | Nessuna dichiarata | DA VERIFICARE ESTERNAMENTE |
| RainViewer radar | "Weather data © RainViewer" (attribuzione) | DA VERIFICARE ESTERNAMENTE |
| LibreWXR radar | "LibreWXR (CC-BY-4.0)" (attribuzione/nota nel codice) | CONFERMARE (CC-BY-4.0 indicata) |
| Radar DPC | "Radar-DPC (CC-BY-SA 4.0)" (attribuzione/nota nel codice) | CONFERMARE (CC-BY-SA 4.0 indicata) |
| Radar-DPC web app (iframe) | Nessuna | DA VERIFICARE ESTERNAMENTE |
| Blitzortung (WS, tile, maps) | "© Blitzortung.org" (attribuzione) | DA VERIFICARE ESTERNAMENTE |
| Limaps / LightningMaps | "© Limaps.org (Blitzortung.org)" (attribuzione) | DA VERIFICARE ESTERNAMENTE |
| Infoplaza / Sat24 (nuvole + fulmini) | "© Sat24/Infoplaza ..." (attribuzione) — servizio commerciale | PROBABILE RESTRITTIVA → DA VERIFICARE |
| EUMETSAT WMS (EUMETView) | "© EUMETSAT" (attribuzione) — layer `mtg_fd:ir105_hrfi`, `msg_fes:rgb_airmass`, `mtg_fd:rgb_geocolour` | DA VERIFICARE ESTERNAMENTE (Data Policy) |
| Iowa State Mesonet (IEM) METAR | Nessuna | DA VERIFICARE ESTERNAMENTE |
| OpenStreetMap tiles | "© OpenStreetMap contributors" + "ODbL" (help del codice) | CONFERMARE (ODbL, attribuzione rispettata) |
| ESRI basemap (Dark, Imagery) | Attribuzione Esri completa | DA VERIFICARE ESTERNAMENTE (termini Esri) |
| Confini provinciali ISTAT (locale, CC BY 4.0) | "Confini amministrativi e dati territoriali: ISTAT — CC BY 4.0" (attribuzione in help/docs) | CONFERMARE (CC BY 4.0 dichiarata da ISTAT per i confini amministrativi) |
| DPC bollettini (CSV, GitHub API, TopoJSON) | "© Dipartimento della Protezione Civile" (attribuzione) | DA VERIFICARE ESTERNAMENTE |
| Meteo&Radar / WetterOnline (iframe) | Footer: "WetterOnline (wo-cloud)" | PROBABILE RESTRITTIVA → DA VERIFICARE |
| Windy embed | Nessuna | DA VERIFICARE ESTERNAMENTE |
| PRETEMP | Link pubblico nel codice; nessuna licenza | DA VERIFICARE ESTERNAMENTE |
| Proxy CORS terze parti (cors.sh, allorigins, corsproxy.io, codetabs) | Nessuna | DA VERIFICARE ESTERNAMENTE |
| Meteociel (carte + spaghetti) | Nessuna licenza dichiarata nel codice | DA VERIFICARE ESTERNAMENTE |
| Leaflet 1.9.4 (CSS+JS) | Nessuna nel codice (progetto open-source noto) | PROBABILE OPEN — da confermare sul sito ufficiale |
| Chart.js | Nessuna nel codice (progetto open-source noto) | PROBABILE OPEN — da confermare sul sito ufficiale |

## Azioni raccomandate prima della pubblicazione

1. Verificare i termini attuali di **ogni** fornitore sopra elencato sui rispettivi siti/ToS.
2. Valutare quali integrazioni tenere attive nell'edizione pubblica tramite `PUBLIC_EDITION_FEATURES` (vedi `DATA_SOURCES_AUDIT.md` per la mappatura flag→sorgenti).
3. Sostituire/sostituire o disattivare i proxy CORS di terze parti per PRETEMP (preoccupazioni di privacy).
4. Verificare l'uso dell'infrastruttura `images.weserv.nl` come proxy (CORS workaround).
5. Mantenere le attribuzioni visibili (già presenti nel codice) e aggiungere le pagine di termini/credits nella UI.

---

*Documento preliminare di screening; da aggiornare dopo le verifiche esterne.*