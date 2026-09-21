# Architektur

Ziel des Projekts: DAB+- und DVB-T-Empfang in einem Wohnmobil, das an
wechselnden Standorten steht, ohne manuellen Eingriff in TVHeadend oder das
Sundtek-Web-UI aktuell halten - Steuerung komplett über Home Assistant.

## Überblick

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  Sundtek DAB+/FM/FM-HD USB   │        │  TurboSight TBS 5520SE        │
│  Stick (eigene Firmware,      │        │  (physischer DVB-T/T2-Tuner)  │
│  mediasrv + rtspd, Port 22000)│        │                                │
└──────────────┬────────────────┘        └───────────────┬────────────────┘
               │ HTTP (servercmd.xhx)                     │ nativ verwaltet von
               │                                           │
      ┌────────▼────────┐                          ┌───────▼──────────┐
      │ dab_bridge.py    │  ──── M3U ────►          │ TVHeadend         │
      │ (Port 8081)      │                          │ (Port 9981)       │
      └────────┬─────────┘                          └───────▲───────────┘
               │ REST API                                    │ REST API
               │                                    dvbt_bridge.py (Port 8082)
               │                                              │
               └──────────────────┬───────────────────────────┘
                                   │
                          ┌────────▼─────────┐
                          │  Home Assistant    │
                          │  (Dashboard,        │
                          │   Automatisierung)  │
                          └────────────────────┘
```

Zwei komplett unabhängige Services (unterschiedliche Hardware, unterschiedliche
Prozesse, unterschiedliche Ports) - bewusst getrennt, damit ein Ausfall/Neustart
des einen den anderen nicht beeinflusst. Die einzige Kopplung: der DAB+-Ablauf
endet mit einem Neustart des kompletten TVHeadend-Dienstes (siehe unten) -
**deshalb sollten DAB+- und DVB-T-Scans nacheinander, nicht parallel laufen**,
sonst kann ein laufender DVB-T-Scan durch den TVHeadend-Neustart abgebrochen
werden.

## DAB+ Pipeline (`bridge/dab_bridge.py`)

DAB+ hat keine "Netzwerke" im TVHeadend-Sinn - es gibt nur ein einziges
IPTV-Auto-Netzwerk ("DAB+"), das seine komplette Senderliste aus einer
dynamisch generierten M3U-Datei bezieht, die die Bridge live erzeugt.

1. **Scan auslösen**: `sundtek-mediasrv` + `sundtek-rtspd` neu starten
   (siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 2), dann
   `scan=start&deviceid=0&mode=15` an die Sundtek-API senden und per
   `cmd=scanstat` pollen, bis die Frequenz sich nicht mehr ändert.
2. **Ergebnis abfragen**: `cmd=providers` liefert den neuesten Scan-Index
   (`prid`), `cmd=chlist&mode=scanlist&prid=<n>` liefert die gefundenen
   Sender (Name + Stream-URL). Leere Namen (Ensemble-Label) und "... EPG"-
   Einträge (Datendienste, keine Audiosender) werden herausgefiltert.
3. **M3U ausliefern**: `GET /DAB_mp2.m3u` (für TVHeadend) bzw.
   `/DAB_plain.m3u` (z.B. für VLC) bauen die Playlist bei jeder Anfrage
   frisch aus dem aktuellen Scan-Ergebnis - keine Zwischenspeicherung außer
   optional einem benannten Standort (siehe unten). Dabei wird jeder Sender
   gegen `exclude.txt`/`renames.txt` geprüft (`prepare_stations()`) -
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
   automatisch zusammengeführt), `epgauto`/Mux-`epg` deaktiviert, "Radio
   channels"-Tag gesetzt (siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md)
   Punkte 6-7).

### Standort-Listen (optional)

Zusätzlich zum reinen "immer der neueste Scan"-Modus kann ein Scan-Ergebnis
unter einem Ortsnamen gespeichert werden (`POST /dab/scan_new_location`) und
später ohne erneuten Scan wieder aktiviert werden (`POST /dab/activate`) -
gespeichert als einfache JSON-Dateien unter
`/storage/dab-tvh-bridge/locations/`, **nicht** als eigene TVHeadend-
Netzwerke (im Gegensatz zu DVB-T, siehe unten). Die automatische,
GPS-getriggerte Rescan-Automatisierung (`POST /dab/rescan`, siehe
[homeassistant/packages/dab_scan.yaml](../homeassistant/packages/dab_scan.yaml))
kennt keine Ortsnamen und setzt beim Auslösen den "aktiven Standort" immer
zurück auf "live" - sie gewinnt also immer mit den frischesten Daten.

`POST /dab/delete_location?name=<Ort>` löscht nur diese lokale JSON-Datei
(kein TVHeadend-Zugriff nötig, die Sender verschwinden erst beim nächsten
Rescan/Aktivieren eines anderen Standorts aus TVHeadend, siehe oben) - war
der gelöschte der aktive Standort, fällt der aktive Standort auf "live"
zurück.

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

Beide Bridges bieten je zwei zusätzliche Endpunkte, um einzelne Sender
auszuschließen oder umzubenennen, ohne auf der Box eine Datei von Hand zu
editieren: `POST /dab/exclude` + `/dab/rename` bzw. `POST /dvbt/exclude` +
`/dvbt/rename` (siehe [API.md](API.md)). Beide schreiben nur die jeweilige
`exclude.txt`/`renames.txt` - **keine sofortige Wirkung in TVHeadend**.

Wichtig, weil sich DAB+ und DVB-T hier unterschiedlich verhalten:

- **DAB+**: Der 30s-Hintergrund-Loop (`mapper_loop()`) liest die Exclude-/
  Rename-Dateien nicht neu ein - er mappt nur bereits bei TVHeadend
  vorhandene, neue Services. Die Änderung wirkt erst, wenn TVHeadend die M3U
  neu einliest, und das passiert ausschließlich bei einem TVHeadend-Neustart
  am Ende der drei Scan-/Aktivieren-Abläufe (`_restart_tvheadend()`). Am
  schnellsten (kein echter Scan, ~10s): **"DAB+ Standort aktivieren"**.
  Da der TVHeadend-Kanalname direkt aus dem `service_name`-Feld der M3U
  kommt, sieht ein Rename für TVHeadend/Kodi wie "alter Kanal weg, neuer
  Kanal da" aus, nicht wie eine In-Place-Umbenennung - manuell gesetzte
  Kanalnummern/Favoriten auf dem alten Kanal gehen dabei verloren.
- **DVB-T**: Es gibt **keinen** periodischen Hintergrund-Loop. Die
  Anwendung passiert einzig innerhalb von `map_unmapped_dvbt_services()`,
  die nur von `/dvbt/activate` oder `/dvbt/scan_new_location` aufgerufen
  wird. Reicht auch auf den bereits aktiven Standort angewendet (kein
  echter Scan nötig). Rename ist hier ein In-Place-Update des bestehenden
  TVHeadend-Kanalobjekts (`autoname: false` sperrt den Namen) - der Kanal
  bleibt derselbe, keine Verluste.

Auf der Home-Assistant-Seite: ein Dropdown pro Bridge
(`input_select.dab_sender_auswahl`/`dvbt_sender_auswahl`), automatisch aus
`GET /dab/stations` bzw. `GET /dvbt/services` befüllt (gleiches
Sync-Automatisierungs-Muster wie die Standort-Dropdowns, siehe unten). Eine
zusätzliche Automatisierung befüllt bei jedem Dropdown-Wechsel das
Umbenennen-Textfeld mit dem aktuellen Anzeigenamen des gewählten Senders -
verhindert versehentliches Überschreiben mit einem alten Tippwert aus einer
vorherigen Auswahl.

## Home Assistant Integration

**Warum die DAB+-Bridge weiterhin auf der HTTP-`cmd=`-API basiert statt auf
Sundteks Low-Level-SDK (`libmcsimple.so`, siehe
[SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4)**: Der SDK-Zugriff
funktioniert nachweislich für einfache, lesende Abfragen (Ensemble-Name,
Tuner-Lock/Signalqualität, gezieltes Anwählen eines Senders per Frequenz+SID)
und, über den `mediaclient`-Prozess (nicht den rohen `ctypes`-Weg), auch für
Cover-Art (MOT Slideshow, siehe unten). Dynamic-Label/Songtitel (DLS)
dagegen funktioniert aktuell mit keinem der beiden Wege - laut einem
externen Hinweis (Sundtek-Discord) ist das DLS-Parsing in `mediaclient`
derzeit selbst auf Sundtek-Seite pausiert, das ist also kein Bug in dieser
Bridge. Die bestehende HTTP-`cmd=`-API plus der dokumentierte
Service-Neustart-Workaround (Punkt 2 in
[TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md)) ist seit Projektstart
durchgehend zuverlässig. **Empfehlung**: bestehende, funktionierende
Mechanismen (Scan auslösen, Senderliste holen) nicht auf das SDK umstellen
- das SDK sich nur für echte Zusatzfunktionen holen, die die HTTP-API gar
nicht bietet (z.B. `/dab/signal`, `/dab/now_playing`, `/dab/artwork` -
`now_playing` nutzt dabei bewusst TVHeadends eigene Subscriptions-API statt
des SDK, siehe dort).

**Cover-Art (Sender-Logo/Album-Cover)**: die Bridge hält im Hintergrund
genau einen `mediaclient --getdigitaldata`-Prozess für den gerade laufenden
Sender am Leben, dekodiert dessen MOT-Slideshow-Ausgabe und stellt das
aktuellste Bild unter `/dab/artwork` bereit. Da Home Assistant 2026.9.3
`camera`/`image` mit `platform: generic` nur noch als UI-Config-Flow
unterstützt (YAML-Konfiguration erzeugt keine bzw. eine fehlerhafte
Entity), wird das Bild stattdessen über eine `type: markdown`-Lovelace-
Karte mit gejinja-tem Cache-Busting eingebunden (siehe
`homeassistant/dashboard_media_tab.yaml`). Details, inklusive zweier
live gefundener und behobener Implementierungsfehler (stdout-Pufferung,
JPEG-Vollständigkeitsprüfung), in
[SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.3.

Zwei Packages ([homeassistant/packages/](../homeassistant/packages/)),
bewusst nach demselben Muster gebaut:

- `rest_command` pro Aktion (mit Jinja-Templating für Parameter wie
  Standort-/Netzwerkname)
- `rest:`-Sensor, der `/dab/status` bzw. `/dvbt/status` alle 15s pollt und
  als Attribute u.a. die Liste bekannter Standorte/Netzwerke liefert
- `input_select` pro Bridge, das per Automatisierung (`input_select.
  set_options`, getriggert bei Status-Änderung + beim HA-Start + alle 10
  Min. als Fallback) automatisch mit den aktuell bekannten Standorten/
  Netzwerken befüllt wird - kein manuelles Pflegen einer Dropdown-Liste
- gemeinsames `input_text.neuer_standort_name` für beide Bridges (ein
  Ortsname statt zweimal derselbe Eingabe)
- zusätzlicher `rest:`-Sensor pro Bridge (`/dab/stations` bzw.
  `/dvbt/services`, alle 30s), dessen `stations`-Attribut das
  Sender-Dropdown sowie einen Status-Sensor ("aktiv"/"ausgeschlossen" +
  aktueller Anzeigename für die gerade ausgewählte Dropdown-Option) speist
- nur DVB-T: ein weiterer `rest:`-Sensor (`/dvbt/channels`, alle 30s) speist
  analog ein Kanal-Dropdown + Status-Sensor ("Nr. X, aktiviert/deaktiviert")
  - inklusive deaktivierter Kanäle. Eine Vorbefüll-Automatisierung setzt bei
  jedem Dropdown-Wechsel den aktuellen Namen/die aktuelle Nummer in die
  Eingabefelder, damit man nie versehentlich vom letzten ausgewählten Kanal
  aus überschreibt

**Wichtige HA-Falle**: Home Assistant leitet die Entity-ID von
REST-Sensoren/Automatisierungen aus dem Anzeigenamen (`name:`/`alias:`) ab,
nicht aus `unique_id`. "DVB-T Status" wird z.B. zu `sensor.dvb_t_status`
(Bindestrich wird zu `_t_`), nicht zum naheliegenden `sensor.dvbt_status`.
Nach jeder Umbenennung: unter Entwicklerwerkzeuge -> Zustände nachschauen,
nicht vom Namen ausgehen.

**Zweite HA-Falle**: Die moderne `rest:`-Integration (mehrere `sensor:`-
Einträge pro `resource:`) unterstützt `json_attributes_template` **nicht**
- weder direkt noch in der alten `sensor: platform: rest`-Schreibweise (in
dieser HA-Version zumindest). Um eine ganze JSON-Liste als ein Attribut zu
speichern (siehe `sensor.dab_sender_liste`/`sensor.dvb_t_sender_liste`),
liefert die Bridge die Liste deshalb bewusst als `{"stations": [...]}`
statt als nacktes Array, damit das normale, dokumentierte
`json_attributes: [stations]` funktioniert. Vor jeder Config-Änderung
`/api/config/core/check_config` über die HA-API prüfen, nicht erst beim
Neustart merken.

### GPS-getriggerter automatischer DAB+-Rescan

Eigene Automatisierung in `dab_scan.yaml`: löst `POST /dab/rescan` aus,
sobald `sensor.rutx50_gps_speed` (RUTX50-Router-GPS, **nicht** der
"tracker_wohnwagen"-Composite-Tracker, der tatsächlich einer Person statt
dem Fahrzeug folgt) 40 Minuten am Stück unter 3 km/h liegt UND die aktuelle
Position mehr als 500 m vom letzten Scan-Ort entfernt ist - verhindert
wiederholtes Scannen bei längerem Stillstand am selben Ort.

Für DVB-T gibt es (bisher) keine entsprechende automatische Standort-
Erkennung - die 39-Muxe-Suche dauert zu lange, um sie ungefragt bei jedem
Stopp auszulösen; das bleibt eine manuelle Aktion im Dashboard.
