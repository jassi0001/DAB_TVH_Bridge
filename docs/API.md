# API-Referenz

Beide Bridges laufen ungesichert (kein Passwort) - nur fürs private LAN
gedacht. IP-Adressen hier als `192.168.0.200` (Beispielwert für die
LibreELEC-Box) angegeben, in deinem Netz entsprechend anpassen.

## DAB+ Bridge (`dab_bridge.py`, Port 8081)

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/DAB_mp2.m3u` | GET | M3U-Playlist mit `pipe://ffmpeg ...`-Kommandos pro Sender (für TVHeadends IPTV-Netzwerk) |
| `/DAB_plain.m3u` | GET | M3U-Playlist mit rohen Stream-URLs (z.B. für VLC direkt) |
| `/dab/status` | GET | `{running, returncode, station_count, tvheadend_muxes, tvh_mux_scan_pending, tvh_mux_scan_active, tvh_scanning, active_location, locations, log_tail}` - `tvh_scanning` (seit 2026-09-21) fasst TVHeadends eigene Mux-Scan-Queue zusammen (siehe [SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.4): `running: false` allein heißt nicht, dass der Tuner wieder frei ist, TVHeadend kann seine eigene Queue noch minutenlang weiter abarbeiten |
| `/dab/locations` | GET | Liste gespeicherter Standorte `[{name, station_count, saved_at}, ...]` |
| `/dab/rescan` | POST | Live-Scan, kein Standort wird gespeichert, setzt aktiven Standort zurück auf "live" |
| `/dab/scan_new_location?name=<Ort>` | POST | Live-Scan, Ergebnis unter `<Ort>` speichern und aktivieren |
| `/dab/activate?name=<Ort>` | POST | Vorhandenen gespeicherten Standort ohne Rescan aktivieren (404 falls unbekannt) |
| `/dab/stations` | GET | `{"stations": [{raw_name, display_name, excluded}, ...]}` - aktuelle Senderliste (aktiver Standort oder Live-Scan) mit Exclude-/Rename-Status |
| `/dab/exclude?name=<Sendername>&excluded=true\|false` | POST | Sender aus-/wieder einschließen (schreibt `exclude.txt`); wirkt beim nächsten Neueinlesen der Playlist durch TVHeadend, nicht sofort |
| `/dab/rename?raw_name=<Sendername>&display_name=<Name>` | POST | Anzeigename setzen (schreibt `renames.txt`); leerer `display_name` entfernt den Eintrag wieder |
| `/dab/delete_location?name=<Ort>` | POST | Gespeicherten Standort unwiderruflich löschen (löscht nur die lokale JSON-Datei, kein TVHeadend-Zugriff); war es der aktive Standort, fällt der aktive Standort auf "live" zurück; `{"deleted": true\|false}`, 404 falls unbekannt |
| `/dab/signal` | GET | `{device, locked, rssi, snr, fic_quality, cnr, ensemble}` - Live-Tuner-/Ensemble-Info, direkt über Sundteks SDK (`libmcsimple.so`) gelesen statt über die HTTP-API, die das nicht anbietet. Siehe [SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4 für Details und Grenzen dieses Wegs (insbesondere: kein Now-Playing/DLS-Songtitel möglich) |
| `/dab/now_playing` | GET | `{"playing": [<Kanalname>, ...]}` - aktuell tatsächlich gestreamte(r) DAB+-Kanal/-Kanäle, direkt aus TVHeadends eigener `/api/status/subscriptions` gelesen (nicht vom Tuner/SDK - dort ließ sich das nicht zuverlässig auslesen, siehe oben) |
| `/dab/artwork_info` | GET | `{available, updated_at, channel}` - Status des zuletzt erfolgreich erfassten Cover-Bilds (MOT Slideshow) für den aktuell laufenden Sender. Siehe [SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.3 |
| `/dab/digitaldata_debug` | GET | `{log_tail}` - letzte 200 Zeilen des persistenten Digitaldata/Cover-Art-Debug-Logs (jede OPENED-Zeile, jeder Erfassungsversuch mit Ausgang, jeder Senderwechsel) - unabhängig von `print()`/journalctl, die sich als unzuverlässig erwiesen haben (siehe SUNDTEK_INSTALL.md Abschnitt 4.3) |
| `/dab/artwork` | GET | Rohes JPEG des aktuellen Sender-Covers (`Content-Type: image/jpeg`, `Cache-Control: no-cache`), 404 falls (noch) keins vorhanden |

`/rescan` und `/rescan/status` funktionieren weiterhin als Alias zu
`/dab/rescan` bzw. `/dab/status` (Altlast aus einer früheren, noch nicht
in DAB+/DVB-T getrennten Version).

Die Scan-/Aktivieren-Endpunkte (`rescan`, `scan_new_location`, `activate`)
laufen asynchron im Hintergrund (`202 {"started": true}` bei Start, `409
{"started": false}` falls schon ein Lauf aktiv ist) - Fortschritt über
`/dab/status` verfolgen. `/dab/exclude`, `/dab/rename` und
`/dab/delete_location` sind dagegen synchron (direkte `200`-Antwort).

## DVB-T Bridge (`dvbt_bridge.py`, Port 8082)

| Endpunkt | Methode | Beschreibung |
|---|---|---|
| `/dvbt/networks` | GET | Liste aller "DVB-T ..."-Netzwerke `[{uuid, name, enabled, num_mux, num_svc, num_chn}, ...]` |
| `/dvbt/status` | GET | `{running, returncode, active_network, networks, log_tail}` |
| `/dvbt/activate?network=<Name>` | POST | Zu einem bekannten Netzwerk wechseln (voller Name inkl. "DVB-T "-Präfix), kein neuer Scan |
| `/dvbt/scan_new_location?name=<Ort>` | POST | Komplett neues Netzwerk `DVB-T <Ort>` anlegen und scannen (~5-10 Min) |
| `/dvbt/services?network=<Name>` (optional) | GET | `{"stations": [{raw_name, display_name, excluded}, ...]}` - aktuell aktive Services im angegebenen (Default: dem aktiven) Netzwerk, mit Exclude-/Rename-Status. Anders als bei DAB+ eine Live-Ansicht des TVHeadend-Zustands, keine gespeicherte Scan-Momentaufnahme |
| `/dvbt/exclude?name=<Sendername>&excluded=true\|false` | POST | Sender aus-/wieder einschließen (schreibt `dvbt_exclude.txt`); wirkt erst beim nächsten Scan/Aktivieren dieses Standorts, da DVB-T (anders als DAB+) keinen periodischen Mapper-Lauf hat |
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
kein Polling nötig) - genau wie ihre DAB+-Pendants.

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
| `/storage/dab-tvh-bridge/dvbt_exclude.txt` | DVB-T | ein Suchbegriff pro Zeile, Teilstring-Match (case-insensitive) |
| `/storage/dab-tvh-bridge/dvbt_renames.txt` | DVB-T | `Roher (getidyter) Kanalname=Anzeigename`, Teilstring-Match |

Beispiel-Varianten (`*.txt.example`) liegen in [bridge/](../bridge/) - auf
die Box als `.txt` (ohne `.example`) kopieren und anpassen.
