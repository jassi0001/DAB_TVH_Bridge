# Architektur

Ziel des Projekts: DAB+-, FM- und DVB-T-Empfang in einem Wohnmobil, das an
wechselnden Standorten steht, ohne manuellen Eingriff in TVHeadend oder das
Sundtek-Web-UI aktuell halten - Steuerung komplett über Home Assistant.

## Überblick

```
┌───────────────────────────────┐        ┌────────────────────────────────┐
│  Sundtek DAB+/FM/FM-HD USB    │        │  TurboSight TBS 5520SE          │
│  Stick (eigene Firmware,      │        │  (physischer DVB-T/T2-Tuner)    │
│  mediasrv + rtspd, Port 22000)│        │                                │
└───────────────┬───────┬───────┘        └────────────────┬───────────────┘
                │        │                                  │
     ┌──────────▼──┐  ┌──▼───────────┐               ┌──────▼──────────┐
     │ dab_bridge2 │  │ fm_bridge2    │               │ TVHeadend        │
     │ .py (8091)  │  │ .py (8093)    │  ──── M3U ──► │ (Port 9981)      │
     └──────┬──────┘  └──────┬────────┘               └────────▲─────────┘
            │ REST API        │ REST API                        │ REST API
            │                 │                        dvbt_bridge.py (Port 8082)
            └────────┬────────┴──────────────────────────────────┘
                     │
             ┌───────▼─────────┐
             │  Home Assistant  │
             │  (Dashboard,     │
             │   Automatisierung)│
             └──────────────────┘
```

DAB+ und FM teilen sich **einen** physischen Sundtek-Tuner (exklusiv - laut
Sundteks eigener Doku sperrt die Nutzung des einen Modus den anderen) und
laufen deshalb nie gleichzeitig aktiv (siehe unten). DVB-T läuft auf
komplett separater Hardware und ist davon unberührt. Alle drei Bridges
generieren nur M3U-Playlists bzw. steuern TVHeadends Netzwerk-/Mux-Objekte
per REST-API - das eigentliche Streaming (ein `pipe://ffmpeg ...`-Prozess
pro Sender bei DAB+/FM, natives MPEG-TS bei DVB-T) besitzt und verwaltet
TVHeadend selbst.

## DAB+ Pipeline (`bridge/dab_bridge2.py`)

DAB+ hat keine "Netzwerke" im TVHeadend-Sinn - es gibt nur ein einziges
IPTV-Auto-Netzwerk ("DAB+ v2"), das seine komplette Senderliste aus einer
dynamisch generierten M3U-Datei bezieht, die die Bridge live erzeugt.

1. **Scan auslösen** (`/dab/rescan`): `sundtek-mediasrv` + `sundtek-rtspd`
   neu starten (siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 2),
   dann `scan=start&deviceid=0&mode=15` an die Sundtek-API senden und per
   `cmd=scanstat` pollen, bis die Frequenz sich nicht mehr ändert - ein
   voller Sweep über alle DAB+-Frequenzen. Läuft nur, wenn der gemeinsame
   Tuner-Lock frei ist (siehe unten).
2. **Senderliste beziehen**: `fetch_stations()` liest die Senderliste
   **nicht** aus Sundteks `cmd=chlist`-API (die liefert Stream-URLs ohne
   den `comp=`-Parameter und mit anders formatiertem `sid`, siehe
   [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 14), sondern direkt aus
   rtspds eigenem M3U-Export (`http://127.0.0.1:22000/dab.m3u`) - das ist
   die einzige Quelle, die durchgehend zuverlässig funktionierende
   Stream-URLs liefert.
3. **M3U ausliefern**: `GET /DAB2.m3u` baut die Playlist bei jeder Anfrage
   frisch aus der aktuellen Senderliste - jede Zeile ein
   `pipe://ffmpeg -i <rtspd-Stream-URL> ... -f mpegts pipe:1`-Kommando, das
   TVHeadend selbst startet und besitzt. Jeder Sender wird dabei gegen
   `exclude.txt`/`renames.txt` geprüft (`prepare_stations()`) -
   ausgeschlossene Sender landen gar nicht erst in der Playlist, umbenannte
   erscheinen direkt unter dem neuen Namen (siehe
   [Sender verwalten](#sender-verwalten-excluderename-aus-home-assistant)
   unten).
4. **TVHeadend synchronisieren**: nach einem Rescan wird TVHeadend neu
   gestartet, damit es die geänderte M3U sofort neu einliest (das
   IPTV-Auto-Netzwerk fragt sonst nur alle `refetch_period`-Minuten ab -
   hier auf 6000 Min. gesetzt, praktisch nie). TVHeadend entfernt dabei
   automatisch Sender, die in der neuen M3U nicht mehr vorkommen (siehe
   [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 4).
5. **Kanal-Mapping** (Hintergrund-Loop, alle 30s): neu erschienene Services
   ohne Kanal werden gemappt (SFN-Simulcasts über `merge_same_name`
   automatisch zusammengeführt), `epgauto`/Mux-`epg` deaktiviert (bei
   **jedem** Kanal, nicht nur neuen - siehe
   [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 16), "Radio
   channels"-Tag gesetzt (siehe
   [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkte 6-7).
6. **Cover-Art/DLS** (eigener Hintergrund-Loop, `_mot_dls_loop()`): solange
   TVHeadend tatsächlich einen DAB+-Sender streamt, liest ein Thread
   MOT-Slideshow-Bilder und Dynamic-Label-Text direkt über Sundteks
   `ctypes`-SDK aus - siehe
   [SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.3 für die Details.

Zusätzlich gibt es `/dab/scan_ordered` - eine feinere Alternative zu
`/dab/rescan`, die keinen vollen Hardware-Sweep auslöst, sondern nur
TVHeadend-Muxe ohne Services aktiviert, frequenzgruppenweise nacheinander
(siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 15 zur
Mux-Scan-Reihenfolge).

### Standort-Listen (optional)

Zusätzlich zum reinen "immer der neueste Scan"-Modus kann ein Scan-Ergebnis
unter einem Ortsnamen gespeichert werden (`POST /dab/scan_new_location`) und
später ohne erneuten Scan wieder aktiviert werden (`POST /dab/activate`) -
gespeichert als einfache JSON-Dateien unter
`/storage/dab-tvh-bridge/locations/`, **nicht** als eigene TVHeadend-
Netzwerke (im Gegensatz zu DVB-T, siehe unten). Die automatische,
GPS-getriggerte Rescan-Automatisierung (`POST /dab/rescan`, siehe unten)
kennt keine Ortsnamen und setzt beim Auslösen den "aktiven Standort" immer
zurück auf "live" - sie gewinnt also immer mit den frischesten Daten.

`POST /dab/delete_location?name=<Ort>` löscht nur diese lokale JSON-Datei
(kein TVHeadend-Zugriff nötig, die Sender verschwinden erst beim nächsten
Rescan/Aktivieren eines anderen Standorts aus TVHeadend, siehe oben) - war
der gelöschte der aktive Standort, fällt der aktive Standort auf "live"
zurück.

## FM Pipeline (`bridge/fm_bridge2.py`)

Strukturell identisch zur DAB+ Pipeline (eigenes IPTV-Auto-Netzwerk
"FM v2", `/FM2.m3u`, dieselben Standort-/Exclude-/Rename-Mechanismen,
`fetch_stations()` liest ebenfalls von rtspds `/dab.m3u`-Export gefiltert
nach `delsys=255`), mit drei Unterschieden:

- **Sendernamen sind Frequenzen** (z.B. `"91.50"`), keine Senderkennungen -
  relevant für die HA-Integration, siehe die entsprechende HA-Falle unten.
- **RDS statt Cover-Art/DLS**: `/fm/rds` liefert Programmname (PS),
  Radiotext, Sender-/Programmtyp-Kennung (PI/PTY), direkt über dieselbe
  `ctypes`-SDK dekodiert wie bei DAB+.
- **Zusätzliche Favoriten-Playlist** (`/FM_favorites2.m3u`,
  `/fm/favorites`): liest Sundteks eigene, über dessen Web-UI gepflegte
  Favoritengruppe "FM" aus (`cmd=chlist&mode=favlist`) - eine reine
  Durchreiche, die Bridge verwaltet diese Gruppe nicht selbst.

## Gemeinsamer Tuner: DAB+ und FM schließen sich gegenseitig aus

Da beide Bridges denselben physischen Tuner ansprechen, darf zu jedem
Zeitpunkt nur eine Seite scannen oder streamen. Zwei unabhängige
Mechanismen sorgen dafür, in beiden Bridges identisch aufgebaut:

1. **Cross-Prozess-Lock für selbst ausgelöste Scans**: `_start_background()`
   hält vor jedem Rescan/Standort-Wechsel einen `fcntl.flock()` auf
   `/storage/dab-tvh-bridge/tuner_scan.lock` - ein OS-Lock statt einer
   reinen Lock-Datei, damit ein abgestürzter Halter ihn nicht für immer
   blockiert (das Betriebssystem gibt ihn beim Prozessende automatisch
   frei, egal wie der Prozess endet). Schlägt der Versuch fehl, liefert der
   jeweilige Scan-Endpunkt sofort `409 {"started": false}`.
2. **Watchdog gegen TVHeadends eigene, autonome Mux-Scan-Queue**:
   TVHeadend verarbeitet die Scan-Warteschlangen von "DAB+ v2" und
   "FM v2" unabhängig und gleichzeitig - es weiß nichts von der geteilten
   Hardware (aus seiner Sicht zwei gewöhnliche, unabhängige IPTV-Netzwerke).
   Ein Hintergrund-Thread (`_tvh_scan_watchdog_loop()`, alle 15s) prüft
   deshalb in beiden Bridges den Mux-Grid der eigenen Netzwerk-Muxe: findet
   er dort welche mit `scan_state` in "wartend"/"aktiv", ohne dass die
   eigene Bridge selbst gerade scannt, versucht er denselben Lock zu
   bekommen - gelingt das nicht (die andere Seite hält ihn gerade), setzt
   er die eigenen Muxe per `/api/idnode/save` zurück auf `scan_state=0`
   (INAKTIV), bis die andere Seite fertig ist.
3. **Bereits bekannte Sender werden nie neu gescannt**: derselbe Watchdog
   setzt Muxe mit bereits vorhandenen Services (`num_svc > 0`) unabhängig
   vom Lock-Status immer auf `scan_state=0` zurück, sobald TVHeadend sie
   von sich aus wieder in die Warteschlange stellt (z.B. durch sein eigenes
   periodisches Idle-Scan) - nur Muxe ohne Services (neu oder tatsächlich
   geänderte Senderliste) werden je gescannt.
4. **Streaming-/Auslese-Seite**: `_fm_v2_is_playing()`/`_dab_v2_is_playing()`
   prüfen vor jedem Hardware-Zugriff (Cover-Art/DLS-Loop, RDS-Loop,
   `/dab/signal`, `/fm/signal`) zusätzlich über TVHeadends
   `/api/status/subscriptions`, dass die **eigene** Seite gerade wirklich
   streamt und die andere Seite nicht - allein das Öffnen eines
   `/dev/dab*`/`/dev/radio*`-Geräteknotens reicht bereits aus, um den
   gemeinsamen Tuner in den jeweils anderen Modus zu zwingen.

## DVB-T Pipeline (`bridge/dvbt_bridge.py`)

DVB-T braucht **keine** Stream-Konvertierung (natives MPEG-TS, direkt von
TVHeadend verwaltet) - die Bridge steuert ausschließlich TVHeadends eigene
Netzwerk-/Mux-/Scan-Objekte per REST-API.

Jeder Standort = ein eigenes TVHeadend-Netzwerk (`DVB-T <Ort>`), analog zum
manuellen Vorgehen über die TVHeadend-Web-UI (Configuration -> DVB Inputs ->
Networks). Zwei Aktionen:

- **`POST /dvbt/activate?network=<Name>`**: schaltet zwischen bereits
  bekannten (schon gescannten) Standorten um - aktiviert das Ziel-Netzwerk,
  deaktiviert alle anderen "DVB-T ..."-Netzwerke, hängt den (einzigen,
  physischen) Tuner um. Kein neuer Scan, quasi instant.
- **`POST /dvbt/scan_new_location?name=<Ort>`**: legt ein komplett neues
  Netzwerk `DVB-T <Ort>` an, erzeugt alle 39 deutschen DVB-T2-
  Kandidatenfrequenzen (474-778 MHz) als Muxe (siehe
  [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkte 5+8), aktiviert es,
  wartet bis TVHeadend alle Muxe durchgescannt hat (~5-10 Min, siehe
  [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 5 zur Zeitschwankung),
  entfernt danach Muxe ohne Empfang und mappt gefundene Services zu Kanälen.
- **`POST /dvbt/delete_network?network=<Name>`**: löscht ein `DVB-T ...`-
  Netzwerk komplett (`/api/idnode/delete` auf die Netzwerk-UUID, cascaded
  auf dessen Muxe/Services - identisch zum Papierkorb-Button in der
  TVHeadend-Web-UI). Sucht nur unter den eigenen "DVB-T ..."-Netzwerken,
  nie unter einem beliebigen TVHeadend-Netzwerknamen. Räumt zusätzlich
  Kanäle auf, die durch die gelöschten Services verwaisen - TVHeadend
  cascaded das nicht von selbst (siehe
  [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 10).

Danach zusätzlich (beide Aktionen durchlaufen das, `map_unmapped_dvbt_services()`):
- **Exclude-Filter**: Services, deren Name auf `dvbt_exclude.txt` matcht
  oder "(Internet)" im Namen enthält (deutsche Sender bieten oft eine
  separate Internet-Simulcast-Variante desselben Programms), werden
  deaktiviert statt gemappt. Der dadurch verwaiste Kanal (null Services
  mehr) wird **bewusst nicht gelöscht** - bleibt mit Nummer, Name und
  Enabled-Status genau so bestehen, damit er beim nächsten Auffinden des
  Services (Standort erneut besucht, Sender wieder empfangbar, Exclude
  aufgehoben) wieder auf denselben Kanal gemappt wird statt einen Duplikat
  zu erzeugen. Kanäle werden ausschließlich durch einen expliziten
  `/dvbt/delete_network`-Aufruf entfernt (siehe oben).
- **Service-Mapping**: neue (noch unmapped) Services werden über
  TVHeadends eigenen Service-Mapper (`merge_same_name`) auf bestehende
  oder neue Kanäle gemappt. `tidy_channel_name` ist bewusst **aus** -
  siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 11 für den
  quellcode-bestätigten Grund (sonst überschreibt jeder Mapper-Durchlauf
  den Namen auch bereits bestehender, umbenannter Kanäle).
- **Rename**: `dvbt_renames.txt` erlaubt das Umbenennen einzelner Kanäle
  nach dem Mapping (z.B. unklare technische Namen wie "NDR FS NDS" ->
  "NDR FS Niedersachsen") - In-Place-Update mit `autoname: false`, der
  Kanal bleibt derselbe.
- **Radio/TV-Tagging**: basierend auf dem echten `dvb_servicetype` des
  jeweiligen Service (siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md)
  Punkt 6 und Punkt 11c für einen live gefundenen Tagging-Bug).

**Garantien für bereits bestehende Kanäle** (Nummer, Name, Enabled-Status):
kein automatischer Durchlauf ändert diese jemals - verifiziert per
Quellcode-Analyse von TVHeadends `service_mapper.c` und live bestätigt
(siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 11). Direktes,
sofortiges Bearbeiten dieser drei Felder (unabhängig vom Scan-Rhythmus) geht
über `GET /dvbt/channels` sowie `POST /dvbt/channel_number`,
`/dvbt/channel_name` und `/dvbt/channel_enabled` (siehe [API.md](API.md)) -
Letztere findet auch deaktivierte Kanäle, dank `all=1` gegen TVHeadends
sonst standardmäßig gefilterten `/api/channel/grid`.

## Sender verwalten (Exclude/Rename) aus Home Assistant

Alle drei Bridges bieten je zwei zusätzliche Endpunkte, um einzelne Sender
auszuschließen oder umzubenennen, ohne auf der Box eine Datei von Hand zu
editieren: `POST /dab/exclude` + `/dab/rename`, `/fm/exclude` +
`/fm/rename` bzw. `/dvbt/exclude` + `/dvbt/rename` (siehe [API.md](API.md)).
Alle drei schreiben nur die jeweilige `exclude.txt`/`renames.txt` -
**keine sofortige Wirkung in TVHeadend**.

Wichtig, weil sich DAB+/FM und DVB-T hier unterschiedlich verhalten:

- **DAB+/FM**: Der 30s-Hintergrund-Loop (`mapper_loop()`) liest die
  Exclude-/Rename-Dateien nicht neu ein - er mappt nur bereits bei
  TVHeadend vorhandene, neue Services. Die Änderung wirkt erst, wenn
  TVHeadend die M3U neu einliest, und das passiert ausschließlich bei
  einem TVHeadend-Neustart am Ende der Scan-/Aktivieren-Abläufe
  (`_restart_tvheadend()`). Am schnellsten (kein echter Scan, ~10s):
  **"Standort aktivieren"**. Da der TVHeadend-Kanalname direkt aus dem
  `service_name`-Feld der M3U kommt, sieht ein Rename für
  TVHeadend/Kodi wie "alter Kanal weg, neuer Kanal da" aus, nicht wie eine
  In-Place-Umbenennung - manuell gesetzte Kanalnummern/Favoriten auf dem
  alten Kanal gehen dabei verloren.
- **DVB-T**: Es gibt **keinen** periodischen Hintergrund-Loop. Die
  Anwendung passiert einzig innerhalb von `map_unmapped_dvbt_services()`,
  die nur von `/dvbt/activate` oder `/dvbt/scan_new_location` aufgerufen
  wird. Reicht auch auf den bereits aktiven Standort angewendet (kein
  echter Scan nötig). Rename ist hier ein In-Place-Update des bestehenden
  TVHeadend-Kanalobjekts (`autoname: false` sperrt den Namen) - der Kanal
  bleibt derselbe, keine Verluste.

Auf der Home-Assistant-Seite: ein Dropdown pro Bridge, automatisch aus
`GET /dab/stations`, `/fm/stations` bzw. `/dvbt/services` befüllt (gleiches
Sync-Automatisierungs-Muster wie die Standort-Dropdowns, siehe unten). Eine
zusätzliche Automatisierung befüllt bei jedem Dropdown-Wechsel das
Umbenennen-Textfeld mit dem aktuellen Anzeigenamen des gewählten Senders -
verhindert versehentliches Überschreiben mit einem alten Tippwert aus einer
vorherigen Auswahl.

## Home Assistant Integration

**Warum die DAB+/FM-Bridges weiterhin auf der HTTP-`cmd=`-API basieren
statt komplett auf Sundteks Low-Level-SDK (`libmcsimple.so`, siehe
[SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4)**: der SDK-Zugriff
wird bewusst nur für Zusatzinformationen genutzt, die die HTTP-`cmd=`-API
gar nicht anbietet (Signalqualität, Ensemble-Name, Cover-Art/DLS, RDS) -
Scan auslösen und Senderliste holen bleiben auf der seit Projektstart
zuverlässigen HTTP-API plus dem dokumentierten Service-Neustart-Workaround
(Punkt 2 in [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md)).

**Cover-Art (Sender-Logo/Album-Cover)**: die Bridge liest MOT-Slideshow-
Bilder direkt über die `ctypes`-SDK aus (kein externer Prozess) und stellt
das aktuellste unter `/dab/artwork` bereit. Eingebunden wird das Bild in
HA über eine **Generic-Camera-Integration** (`camera.dab_v2_sender_cover`,
`still_image_url` = `/dab/artwork`) - **nicht** per YAML (Home Assistant
2026.9.3 erzeugt darüber keine bzw. eine fehlerhafte Entity), sondern
einmalig über Einstellungen -> Geräte & Dienste -> Integration hinzufügen
-> "Generic Camera" angelegt; dieser Teil lebt deshalb nur in der
Entity-Registry der Box, nicht in den YAML-Packages dieses Repos. Eine
`type: markdown`-Lovelace-Karte bindet das Bild über
`{{ state_attr('camera.dab_v2_sender_cover', 'entity_picture') }}` ein
(siehe `homeassistant/dashboard_media_tab.yaml`) - dieser Pfad ist ein von
HA selbst signierter, `/api/camera_proxy/...`-Pfad **relativ zu HA**, kein
fest verdrahteter Link auf die Box-IP. Wichtig, falls HA per NAT/Reverse-
Proxy von außerhalb des Wohnwagen-LANs erreichbar gemacht wird: eine
frühere Version dieser Karte verlinkte direkt auf
`http://<Box-IP>:8091/dab/artwork` - das funktionierte nur im lokalen LAN,
da nur HAs eigener Port nach außen freigegeben war, nicht der der Bridge.
Der Umweg über die Camera-Entity behebt das, ohne einen zusätzlichen Port
öffnen zu müssen. Details zur Bilderfassung selbst in
[SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.3.

Drei Packages ([homeassistant/packages/](../homeassistant/packages/)),
bewusst nach demselben Muster gebaut:

- `rest_command` pro Aktion (mit Jinja-Templating für Parameter wie
  Standort-/Netzwerkname)
- `rest:`-Sensor, der `/dab/status`, `/fm/status` bzw. `/dvbt/status` alle
  15s pollt und als Attribute u.a. die Liste bekannter Standorte/Netzwerke
  liefert
- `input_select` pro Bridge, das per Automatisierung (`input_select.
  set_options`, getriggert bei Status-Änderung + beim HA-Start + alle 10
  Min. als Fallback) automatisch mit den aktuell bekannten Standorten/
  Netzwerken befüllt wird - kein manuelles Pflegen einer Dropdown-Liste
- gemeinsames `input_text.neuer_standort_name` für DAB+/FM/DVB-T (ein
  Ortsname statt mehrfach derselbe Eingabe)
- zusätzlicher `rest:`-Sensor pro Bridge (`/dab/stations`, `/fm/stations`
  bzw. `/dvbt/services`, alle 30s), dessen `stations`-Attribut das
  Sender-Dropdown sowie einen Status-Sensor ("aktiv"/"ausgeschlossen" +
  aktueller Anzeigename für die gerade ausgewählte Dropdown-Option) speist
- nur DVB-T: ein weiterer `rest:`-Sensor (`/dvbt/channels`, alle 30s) speist
  analog ein Kanal-Dropdown + Status-Sensor ("Nr. X, aktiviert/deaktiviert")
  - inklusive deaktivierter Kanäle. Eine Vorbefüll-Automatisierung setzt bei
  jedem Dropdown-Wechsel den aktuellen Namen/die aktuelle Nummer in die
  Eingabefelder, damit man nie versehentlich vom letzten ausgewählten Kanal
  aus überschreibt

**Erste HA-Falle**: Home Assistant leitet die Entity-ID von
REST-Sensoren/Automatisierungen aus dem Anzeigenamen (`name:`/`alias:`) ab,
nicht aus `unique_id`. "DAB+ v2 Sender Liste" wird z.B. zu
`sensor.dab_v2_sender_liste` (das "+" fällt weg), "DVB-T Status" zu
`sensor.dvb_t_status` (Bindestrich wird zu `_t_`) - beides nicht der
naheliegende Name. Nach jeder Umbenennung: unter Entwicklerwerkzeuge ->
Zustände nachschauen, nicht vom Namen ausgehen.

**Zweite HA-Falle**: Die moderne `rest:`-Integration (mehrere `sensor:`-
Einträge pro `resource:`) unterstützt `json_attributes_template` **nicht**
- weder direkt noch in der alten `sensor: platform: rest`-Schreibweise (in
dieser HA-Version zumindest). Um eine ganze JSON-Liste als ein Attribut zu
speichern (siehe `sensor.dab_v2_sender_liste`/`sensor.dvb_t_sender_liste`),
liefert jede Bridge die Liste deshalb bewusst als `{"stations": [...]}`
statt als nacktes Array, damit das normale, dokumentierte
`json_attributes: [stations]` funktioniert. Vor jeder Config-Änderung
`/api/config/core/check_config` über die HA-API prüfen, nicht erst beim
Neustart merken.

**Dritte HA-Falle**: ein Script-`data:`-Feld, dessen Template komplett aus
einem einzigen `{{ states(...) }}`-Ausdruck besteht, wird von Home
Assistant in einen nativen Python-Typ zurückverwandelt, sobald der
gerenderte Text wie eine reine Zahl aussieht - betrifft konkret die
FM-Sendernamen (Frequenzen wie `"91.50"`): ein unbehandeltes
`{{ states('input_select.fm2_sender_auswahl') }}` als `raw_name`/`name`
für `rest_command.fm2_trigger_rename`/`fm2_trigger_exclude` wurde dabei
zur Zahl `91.5`, die Nachkommanull ging verloren - der anschließende
Vergleich gegen den echten Sendernamen `"91.50"` schlug dadurch dauerhaft
fehl (live bestätigt 2026-09-26, u.a. an einem echten, zuvor gescheiterten
Rename-Versuch). Weder `| string` noch `| tojson` verhinderten das in der
Praxis. **Fix**: der Punkt wird im Template selbst durch seine
Prozent-Kodierung ersetzt (`| replace('.', '%2E')`), das macht den Text
nicht mehr zahlenförmig; die empfangende Bridge dekodiert `%2E` beim
normalen URL-Query-Parsing automatisch zurück zu `.`, ohne eigenen Code
dafür zu brauchen. Der entsprechende `rest_command`-Baustein darf das
Feld danach nicht zusätzlich mit `| urlencode` behandeln (würde das
schon codierte `%2E` zu `%252E` doppelt kodieren).

### GPS-getriggerter automatischer DAB+-Rescan

Eigene Automatisierung in `dab_scan.yaml`: löst `POST /dab/rescan` aus,
sobald `sensor.rutx50_gps_speed` (RUTX50-Router-GPS, **nicht** der
"tracker_wohnwagen"-Composite-Tracker, der tatsächlich einer Person statt
dem Fahrzeug folgt) 40 Minuten am Stück unter 3 km/h liegt UND die aktuelle
Position mehr als 500 m vom letzten Scan-Ort entfernt ist - verhindert
wiederholtes Scannen bei längerem Stillstand am selben Ort. Da DAB+ und FM
sich denselben Tuner teilen (siehe oben), löst dieselbe Automatisierung
keinen FM-Scan aus - ein GPS-getriggerter FM-Rescan würde mit einem
laufenden DAB+-Scan kollidieren, ist deshalb bewusst nicht vorgesehen.

Für DVB-T gibt es (bisher) keine entsprechende automatische Standort-
Erkennung - die 39-Muxe-Suche dauert zu lange, um sie ungefragt bei jedem
Stopp auszulösen; das bleibt eine manuelle Aktion im Dashboard.
