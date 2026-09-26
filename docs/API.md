# API-Referenz

Alle drei Bridges laufen ungesichert (kein Passwort) - nur fürs private
LAN gedacht. IP-Adressen hier als `192.168.0.200` (Beispielwert für die
LibreELEC-Box) angegeben, in deinem Netz entsprechend anpassen.

## DAB+ Bridge (`dab_bridge2.py`, Port 8091)

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/DAB2.m3u` bzw. `/` | GET | M3U-Playlist mit `pipe://ffmpeg ...`-Kommandos pro Sender (für TVHeadends IPTV-Netzwerk "DAB+ v2") - TVHeadend startet/besitzt jeden ffmpeg-Prozess selbst, die Bridge streamt nichts |
| `/dab/status` | GET | `{running, returncode, station_count, tvheadend_muxes, dab_scanning, dab_scan_current_freq, dab_scan_current_stations, dab_last_scan_freq, dab_last_scan_stations, dab_last_scan_finished_at, tvh_mux_scan_pending, tvh_mux_scan_active, tvh_scanning, active_location, locations, log_tail}` - `tvh_scanning` fasst TVHeadends eigene Mux-Scan-Queue zusammen (siehe [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 12): `running: false` allein heißt nicht, dass der Tuner wieder frei ist, TVHeadend kann seine eigene Queue noch minutenlang weiter abarbeiten |
| `/dab/locations` | GET | Liste gespeicherter Standorte `[{name, station_count, saved_at}, ...]` |
| `/dab/rescan` | POST | Voller Sundtek-Hardware-Scan (alle DAB+-Frequenzen), kein Standort wird gespeichert, setzt aktiven Standort zurück auf "live" - dauert ca. 2-3 Min., läuft nur wenn der Tuner nicht bereits von FM belegt ist (siehe [ARCHITECTURE.md](ARCHITECTURE.md#gemeinsamer-tuner-dab-und-fm-schliessen-sich-gegenseitig-aus)) |
| `/dab/scan_ordered` | POST | Feingranulare Variante: aktiviert nur TVHeadend-Muxe, die noch keine Services haben (`num_svc == 0`), frequenzgruppenweise nacheinander - überspringt bereits bekannte Sender, löst also keinen vollen Hardware-Scan aus |
| `/dab/scan_new_location?name=<Ort>` | POST | Voller Hardware-Scan, Ergebnis unter `<Ort>` speichern und aktivieren |
| `/dab/activate?name=<Ort>` | POST | Vorhandenen gespeicherten Standort ohne Rescan aktivieren (404 falls unbekannt) |
| `/dab/stations` | GET | `{"stations": [{raw_name, display_name, excluded}, ...]}` - aktuelle Senderliste (aktiver Standort oder Live-Scan) mit Exclude-/Rename-Status |
| `/dab/exclude?name=<Sendername>&excluded=true\|false` | POST | Sender aus-/wieder einschließen (schreibt `exclude.txt`); wirkt beim nächsten Neueinlesen der Playlist durch TVHeadend, nicht sofort |
| `/dab/rename?raw_name=<Sendername>&display_name=<Name>` | POST | Anzeigename setzen (schreibt `renames.txt`); leerer `display_name` entfernt den Eintrag wieder |
| `/dab/delete_location?name=<Ort>` | POST | Gespeicherten Standort unwiderruflich löschen (löscht nur die lokale JSON-Datei, kein TVHeadend-Zugriff); war es der aktive Standort, fällt der aktive Standort auf "live" zurück; `{"deleted": true\|false}`, 404 falls unbekannt |
| `/dab/signal` | GET | `{device, locked, rssi, snr, fic_quality, cnr, ensemble, sid, service_name, frequency}` - Live-Tuner-/Ensemble-Info, direkt über Sundteks SDK (`libmcsimple.so`) gelesen; liefert nur echte Werte, während TVHeadend gerade tatsächlich einen DAB+-Sender streamt (siehe [ARCHITECTURE.md](ARCHITECTURE.md#home-assistant-integration)), sonst durchgehend `null`/`false` |
| `/dab/now_playing` | GET | `{"playing": [<Kanalname>, ...]}` - aktuell tatsächlich gestreamte(r) DAB+-Kanal/-Kanäle, direkt aus TVHeadends eigener `/api/status/subscriptions` gelesen |
| `/dab/now_playing_text` | GET | `{text, updated_at, channel}` - Dynamic-Label-Songtitel (DLS), direkt über die SDK dekodiert; `text` bleibt aktuell durchgehend `null` (siehe [SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.3) |
| `/dab/artwork_info` | GET | `{available, updated_at, channel, type}` - Status des aktuellen Cover-Bilds (MOT Slideshow) für den laufenden Sender; `type` ist der erkannte Bildtyp (`cover`, `wetter`, `sendung`, `news`, ... je nach Sender) |
| `/dab/artwork` | GET | Rohes JPEG des aktuellen Bilds (`Content-Type: image/jpeg`, `Cache-Control: no-cache`), 404 falls (noch) keins vorhanden |
| `/dab/digitaldata_debug` | GET | `{log_tail}` - letzte 200 Zeilen des persistenten Cover-Art/DLS-Debug-Logs (jeder Erfassungsversuch, jeder Senderwechsel) |

Die Scan-/Aktivieren-Endpunkte (`rescan`, `scan_ordered`, `scan_new_location`,
`activate`) laufen asynchron im Hintergrund (`202 {"started": true}` bei
Start, `409 {"started": false}` falls schon ein Lauf aktiv ist oder FM
gerade den gemeinsamen Tuner hält) - Fortschritt über `/dab/status`
verfolgen. `/dab/exclude`, `/dab/rename` und `/dab/delete_location` sind
dagegen synchron (direkte `200`-Antwort).

## FM Bridge (`fm_bridge2.py`, Port 8093)

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/FM2.m3u` bzw. `/` | GET | M3U-Playlist mit `pipe://ffmpeg ...`-Kommandos pro Sender (für TVHeadends IPTV-Netzwerk "FM v2") |
| `/FM_favorites2.m3u` | GET | M3U-Playlist nur der Sender, die in Sundteks eigener rtspd-Favoritengruppe "FM" stehen (über dessen Web-UI gepflegt, siehe [SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md)) |
| `/fm/favorites` | GET | `{"stations": [<Sendername>, ...]}` - reine Namensliste derselben Favoritengruppe |
| `/fm/status` | GET | Analog zu `/dab/status`, Felder `fm_scanning`/`fm_scan_current_freq`/... statt `dab_*` |
| `/fm/locations` | GET | Liste gespeicherter Standorte, analog zu `/dab/locations` |
| `/fm/rescan` | POST | Voller Sundtek-Hardware-Scan (gesamtes UKW-Band), analog zu `/dab/rescan` inkl. Tuner-Sperre gegenüber DAB+ |
| `/fm/scan_ordered` | POST | Analog zu `/dab/scan_ordered` |
| `/fm/scan_new_location?name=<Ort>` | POST | Analog zu `/dab/scan_new_location` |
| `/fm/activate?name=<Ort>` | POST | Analog zu `/dab/activate` |
| `/fm/delete_location?name=<Ort>` | POST | Analog zu `/dab/delete_location` |
| `/fm/stations` | GET | Analog zu `/dab/stations` - `raw_name` ist bei FM eine Frequenzangabe (z.B. `"91.50"`), keine Senderkennung |
| `/fm/exclude?name=<Sendername>&excluded=true\|false` | POST | Analog zu `/dab/exclude` |
| `/fm/rename?raw_name=<Sendername>&display_name=<Name>` | POST | Analog zu `/dab/rename` |
| `/fm/signal` | GET | `{device, signal}` - rohe V4L2-Signalstärke, nur während FM tatsächlich streamt (analog zu `/dab/signal`) |
| `/fm/now_playing` | GET | Analog zu `/dab/now_playing` |
| `/fm/rds` | GET | `{device, ps, radiotext, pi, pty, updated_at}` - RDS-Daten (Programmname, Radiotext, Sender-/Programmtyp-Kennung), direkt über die SDK dekodiert |

**HA-Falle bei FM-Sendernamen**: `raw_name`/`name` sind bei FM reine
Zahlen (Frequenzen wie `"91.50"`). Ein Home-Assistant-Script, das so einen
Wert unbehandelt als `data:`-Feld an einen `rest_command` weiterreicht,
verliert dabei die Nachkommanull (`"91.50"` wird zur Zahl `91.5`) - siehe
[ARCHITECTURE.md](ARCHITECTURE.md#home-assistant-integration) für den
Grund und die Umgehung (Punkt in den HA-Fallen).

## DVB-T Bridge (`dvbt_bridge.py`, Port 8082)

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/dvbt/networks` | GET | Liste aller "DVB-T ..."-Netzwerke `[{uuid, name, enabled, num_mux, num_svc, num_chn}, ...]` |
| `/dvbt/status` | GET | `{running, returncode, active_network, networks, log_tail}` |
| `/dvbt/activate?network=<Name>` | POST | Zu einem bekannten Netzwerk wechseln (voller Name inkl. "DVB-T "-Präfix), kein neuer Scan |
| `/dvbt/scan_new_location?name=<Ort>` | POST | Komplett neues Netzwerk `DVB-T <Ort>` anlegen und scannen (~5-10 Min) |
| `/dvbt/services?network=<Name>` (optional) | GET | `{"stations": [{raw_name, display_name, excluded}, ...]}` - aktuell aktive Services im angegebenen (Default: dem aktiven) Netzwerk, mit Exclude-/Rename-Status. Anders als bei DAB+/FM eine Live-Ansicht des TVHeadend-Zustands, keine gespeicherte Scan-Momentaufnahme |
| `/dvbt/exclude?name=<Sendername>&excluded=true\|false` | POST | Sender aus-/wieder einschließen (schreibt `dvbt_exclude.txt`); wirkt erst beim nächsten Scan/Aktivieren dieses Standorts, da DVB-T (anders als DAB+/FM) keinen periodischen Mapper-Lauf hat |
| `/dvbt/rename?raw_name=<Sendername>&display_name=<Name>` | POST | Anzeigename setzen (schreibt `dvbt_renames.txt`); leerer `display_name` entfernt den Eintrag wieder |
| `/dvbt/delete_network?network=<Name>` | POST | "DVB-T ..."-Netzwerk unwiderruflich löschen (wie der Papierkorb-Button in der TVHeadend-Web-UI, cascaded auf Muxe/Services); nur unter den eigenen "DVB-T ..."-Netzwerken, nie ein beliebiger TVHeadend-Netzwerkname; entfernt zusätzlich verwaiste Kanäle (siehe unten); `{"deleted": true, "network": <Name>, "orphaned_channels_removed": <n>}`, 404 falls unbekannt |
| `/dvbt/channels?network=<Name>` (optional) | GET | `{"channels": [{uuid, name, number, enabled}, ...]}` - alle Kanäle mit mindestens einem Service im angegebenen (Default: dem aktiven) Netzwerk, **inklusive deaktivierter** Kanäle |
| `/dvbt/channel_number?name=<Kanalname>&number=<n>` | POST | Kanalnummer direkt setzen - sofortige Wirkung, kein Scan/Aktivieren nötig; 404 falls kein Kanal mit diesem Namen im (aktiven) DVB-T-Netzwerk existiert |
| `/dvbt/channel_name?name=<aktueller Kanalname>&new_name=<neuer Name>` | POST | Kanalname direkt umbenennen (setzt `autoname: false`) - sofortige Wirkung, anders als `/dvbt/rename` (das nur eine Regel für den nächsten Scan/Aktivieren-Durchlauf schreibt) |
| `/dvbt/channel_enabled?name=<Kanalname>&enabled=true\|false` | POST | Kanal direkt aktivieren/deaktivieren - unabhängig vom Mux-/Netzwerk-Enable-State und vom Sender-Exclude-Mechanismus |

Auch hier: die Scan-/Aktivieren-Endpunkte laufen im Hintergrund,
`202`/`409`-Antwortmuster wie oben, Fortschritt über `/dvbt/status`.
`/dvbt/exclude`, `/dvbt/rename`, `/dvbt/delete_network` und die drei
`/dvbt/channel_*`-Endpunkte sind dagegen synchron (direkte `200`-Antwort,
kein Polling nötig) - genau wie ihre DAB+/FM-Pendants.

### Garantien für bestehende Kanäle (Kanalnummer/-name/-aktiviert-Status)

Verifiziert (Quellcode-Analyse von `service_mapper.c` + Live-Test, siehe
[ARCHITECTURE.md](ARCHITECTURE.md#sender-verwalten-excluderename-aus-home-assistant)
und [TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md) Punkt 11): kein automatischer
Scan-/Mapper-Durchlauf dieses Projekts ändert jemals die `enabled`- oder
`number`-Eigenschaft eines bereits existierenden Kanals, und (seit dem
Abschalten von `tidy_channel_name`) auch nicht dessen `name`. Ein deaktivierter
Kanal bleibt deaktiviert; ein Kanal ohne aktuell empfangbaren/aktivierten
Service wird nie gelöscht (nur ein expliziter `/dvbt/delete_network`-Aufruf
für das gesamte Netzwerk löscht Kanäle). Nur die Service-zu-Kanal-Zuordnung
selbst passiert automatisch.

## Konfigurationsdateien (auf der Box, nicht Teil dieses Repos)

| Datei | Bridge | Format |
|---|---|---|
| `/storage/dab-tvh-bridge/exclude.txt` | DAB+ | ein Sendername pro Zeile (exakter Match nach Rename), `#`-Kommentare |
| `/storage/dab-tvh-bridge/renames.txt` | DAB+ | `Roher Name=Anzeigename`, ein Eintrag pro Zeile |
| `/storage/dab-tvh-bridge/fm_exclude.txt` | FM | ein Sendername (Frequenz) pro Zeile |
| `/storage/dab-tvh-bridge/fm_renames.txt` | FM | `Rohe Frequenz=Anzeigename`, ein Eintrag pro Zeile |
| `/storage/dab-tvh-bridge/dvbt_exclude.txt` | DVB-T | ein Suchbegriff pro Zeile, Teilstring-Match (case-insensitive) |
| `/storage/dab-tvh-bridge/dvbt_renames.txt` | DVB-T | `Roher (getidyter) Kanalname=Anzeigename`, Teilstring-Match |

Beispiel-Varianten (`*.txt.example`) liegen in [bridge/](../bridge/) - auf
die Box als `.txt` (ohne `.example`) kopieren und anpassen.

## Gemeinsame Sperre (DAB+ ↔ FM)

Da beide Bridges denselben physischen Sundtek-Tuner ansprechen, hält
`_start_background()` in beiden Dateien vor jedem selbst ausgelösten Scan
einen prozessübergreifenden `flock()` auf
`/storage/dab-tvh-bridge/tuner_scan.lock` - schlägt das fehl (die andere
Bridge scannt/streamt gerade), liefert der jeweilige Scan-Endpunkt sofort
`409 {"started": false}` statt loszulaufen. Ein zusätzlicher
Hintergrund-Thread in beiden Bridges überwacht außerdem TVHeadends eigene,
autonome Mux-Scan-Queue (siehe
[ARCHITECTURE.md](ARCHITECTURE.md#gemeinsamer-tuner-dab-und-fm-schliessen-sich-gegenseitig-aus)) -
Details und Quellcode-Herleitung dazu in
[TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md).
