# Sundtek DAB+/FM/FM-HD Stick: Installation auf LibreELEC

Wie der Sundtek-Treiber/-Server auf der LibreELEC-Box ("Wohnwagen",
192.168.0.200) installiert und eingebunden wurde. Rein rekonstruiert aus
dem aktuellen Zustand der Box (Dateien, systemd-Units) - falls du das auf
einer neuen Box nachbaust, entspricht das den tatsächlich vorhandenen
Artefakten, nicht einer nachträglich idealisierten Anleitung.

## 1. Treiber-/Server-Paket installieren

Sundtek liefert für ihre Geräte keinen Kernel-Treiber im klassischen Sinn,
sondern einen eigenen Userspace-Server (`mediasrv` + `rtspd`), der über ein
Netzwerk-Installationsskript bezogen wird:

```sh
# auf der LibreELEC-Box, als root
cd /storage
wget https://www.sundtek.de/media/sundtek/sundtek_netinst.sh
sh sundtek_netinst.sh -use-custom-path /storage/dabplus
```

**Vorsicht bei DNS-Filterung (Pi-hole, DNS4EU o.ä.)**: `sundtek.de` steht
bei mehreren gängigen DNS-Blocklisten (u.a. Pi-hole-Standardlisten und
DNS4EU) auf der Sperrliste - vermutlich wegen des Trackings/der Werbung
auf der eigentlichen Firmenwebsite, nicht wegen der Downloads selbst. Falls
im Netz ein solcher DNS-Filter aktiv ist, schlägt sowohl der `wget`-Aufruf
oben als auch jeder spätere Download **innerhalb** von `sundtek_netinst.sh`
fehl (das Skript lädt selbst weitere Pakete nach). Vorher eine Ausnahme für
`sundtek.de` im DNS-Filter eintragen, sonst bricht die Installation ab.

**Wichtig: `-use-custom-path /storage/dabplus` ist zwingend nötig.** Ohne
diesen Parameter installiert das Skript standardmäßig nach `/opt/...` -
dieser Pfad existiert unter LibreELEC gar nicht, und das Root-Dateisystem
ist ohnehin schreibgeschützt (nur `/storage` ist persistent und
beschreibbar). `-use-custom-path <ZIELPFAD>` weist den Installer an, die
komplette Installation (Binaries, Config, spätere Datenbank) unter diesem
Pfad abzulegen statt unter `/opt`.

Das Skript (`/storage/sundtek_netinst.sh` auf der Box, Copyright Sundtek
2009-2020) lädt die passende Multi-Architektur-Binärdistribution herunter
und installiert sie nach `/storage/dabplus/`.

**Zusätzlich notwendig: den RTSP-Streaming-Server nachinstallieren.** Die
Grundinstallation allein liefert `mediaclient` (CLI) und `mediasrv`, aber
**nicht** den eigentlichen Streaming-Server, den `sundtek-rtspd.service`
braucht - der kommt erst über einen separaten `mediaclient`-Aufruf:

```sh
/storage/dabplus/opt/bin/mediaclient --installstreamer
```

(`mediaclient --help` listet das unter "Streaming Server":
`--installstreamer` "will install an rtsp streaming server",
`--installplayer` "will install the player for the streamingserver" - der
Player wird hier nicht gebraucht, da TVHeadend/Kodi das Abspielen
übernehmen). Ohne diesen Schritt existiert `/storage/dabplus/opt/bin/rtspd`
schlicht nicht und `sundtek-rtspd.service` kann nicht starten.

Ergebnislayout nach beiden Schritten:

```
/storage/dabplus/
├── etc/                    # mitgelieferte Konfiguration
├── opt/
│   ├── bin/
│   │   ├── mediasrv        # low-level Tuner-Daemon
│   │   ├── mediaclient     # CLI-Tool (Scan/Status via Kommandozeile)
│   │   ├── rtspd            # RTSP/HTTP-Streaming-Server + Web-UI (Port 22000)
│   │   └── ...
│   ├── doc/                 # README, sundtek.conf-Beispiel, LIRC-Configs
│   └── include/             # C-Header (libmediaclient), hier ungenutzt
├── usr/
└── sundtek.db                # Stations-/Scan-Datenbank (binäres, proprietäres Format)
```

**Achtung, potenzielle Verwechslungsfalle**: `--installstreamer` legt
`rtspd` korrekt unter `opt/bin/` ab - **das** ist die Version, die
`sundtek-rtspd.service` startet (`ExecStart=/storage/dabplus/opt/bin/rtspd
...`). Bei einer händischen/wiederholten Installation kann zusätzlich eine
zweite `rtspd`-Kopie direkt unter `/storage/dabplus/rtspd` (Hauptverzeichnis,
nicht `opt/bin/`) liegen bleiben - das ist ein ungenutzter Überbleibsel
aus der Grundinstallation, nicht die tatsächlich laufende Version
(unterschiedliche Dateigröße/MD5/Datum). Gefahrlos löschbar; vor dem
Löschen kurz per `lsof | grep rtspd` prüfen, dass wirklich nur die
`opt/bin/`-Version geöffnet ist.

`/storage/dabplus/opt/doc/README` (Originaldatei von Sundtek) listet die
mitgelieferten Beispieldateien (LIRC-Configs, `sundtek.conf`, systemd-Unit-
Vorlage) - wurde hier nicht 1:1 übernommen, siehe Abschnitt 2.

## 2. systemd-Services (LibreELEC-Variante)

LibreELEC nutzt eigene Unit-Verzeichnisse
(`/storage/.config/system.d/...`, aktiviert über `kodi.target`) statt der
Standard-systemd-Pfade. Drei eigene, angepasste Units statt der von Sundtek
mitgelieferten Vorlage:

**`sundtek-mediasrv.service`** - der eigentliche Tuner-Daemon:
```ini
[Unit]
Description=Sundtek DAB+/FM mediasrv (low-level tuner daemon)
After=local-fs.target

[Service]
Environment=TZ=Europe/Berlin
Type=forking
WorkingDirectory=/storage/dabplus/opt/bin
ExecStart=/storage/dabplus/opt/bin/mediasrv -d
Restart=always
RestartSec=5
TimeoutStartSec=15

[Install]
WantedBy=kodi.target
```
`-d` = Daemon-Modus. Die Datenbank (`sundtek.db`) wird nicht explizit per
Flag angegeben, sondern relativ zum `WorkingDirectory` erwartet (landet
eine Ebene über `opt/bin`, also direkt in `/storage/dabplus/`).
`TZ=Europe/Berlin` wurde nachträglich ergänzt in dem Versuch, die intern in
den Scan-Labels verwendete UTC-Zeitstempelung auf lokale Zeit umzustellen -
**hat nachweislich keine Wirkung** (mediasrv formatiert offenbar hart
codiert in UTC, unabhängig von der Umgebungsvariable). Kann drinbleiben
(schadet nicht), behebt das Problem aber nicht - siehe
[docs/TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md).

**`sundtek-rtspd.service`** - RTSP/HTTP-Streaming + Web-Admin-UI (Port 22000):
```ini
[Unit]
Description=Sundtek DAB+/FM rtspd (RTSP/HTTP streaming + station DB)
After=sundtek-mediasrv.service
Wants=sundtek-mediasrv.service

[Service]
Type=simple
ExecStart=/storage/dabplus/opt/bin/rtspd -c /storage/dabplus
Restart=always
RestartSec=5

[Install]
WantedBy=kodi.target
```
`-c /storage/dabplus` gibt das Verzeichnis an, in dem `rtspd` die
Stations-/Scan-Datenbank (`sundtek.db`) erwartet bzw. anlegt - das ist die
"Datenbank beim Starten angeben"-Stelle.

Beide Dienste müssen für einen **echten, persistenten Scan** unmittelbar
vor dem Scan-Trigger neu gestartet werden (siehe
[docs/TVHEADEND_QUIRKS.md](TVHEADEND_QUIRKS.md)) - das übernimmt
automatisiert `bridge/dab_bridge.py`.

## 3. Web-Admin-UI offline nutzbar machen (Picon-Mirror + DNS)

Das Sundtek-Web-UI (`http://<box>:22000/`, genutzt für manuelle Scans,
Sender-Listen-Verwaltung etc.) lädt Sender-Picons standardmäßig live von
`sundtek.de` nach - im Wohnwagen unterwegs oft ohne Internetzugang nicht
verfügbar. Lösung: ein lokaler Mirror + DNS-Umleitung.

**`sundtek-ui-proxy.service`**:
```ini
[Unit]
Description=Local mirror of sundtek.de/picons for offline admin UI access
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /storage/dab-tvh-bridge/local_ui_proxy.py
Restart=always
RestartSec=5

[Install]
WantedBy=kodi.target
```

`bridge/local_ui_proxy.py` (22 Zeilen) ist ein simpler
`http.server`-basierter statischer Fileserver, der auf **Port 80** der Box
lauscht und den Inhalt von `/storage/dab-tvh-bridge/sundtek-ui-cache/`
ausliefert (Verzeichnisstruktur spiegelt `sundtek.de/picons/...`).

Damit das greift, braucht es zusätzlich einen **DNS-Override für
`sundtek.de`** auf die IP der LibreELEC-Box (192.168.0.200) - eingetragen
am Router (Teltonika RUTX50, 192.168.0.1), damit er für **alle** Geräte
im Wohnwagen-LAN gilt, nicht nur für die Box selbst. Jedes Gerät im LAN
(inkl. Handy/Laptop, die das Sundtek-Web-UI aufrufen), löst `sundtek.de`
dadurch lokal auf und bekommt die gecachten Picons von der Box statt vom
echten Sundtek-Server - Web-UI bleibt damit auch ohne Internetverbindung
voll nutzbar. (Die genaue Konfiguration am RUTX50 selbst ist hier nicht
dokumentiert - über dessen eigenes Web-UI bzw. DNS/DHCP-Einstellungen
vorzunehmen, z.B. als lokaler Host-Eintrag im DNS-Server des Routers.)

**Nebenwirkung, unbedingt beachten**: Sobald der DNS-Override für
`sundtek.de` aktiv ist, kann das Sundtek-Web-UI selbst **keine aktuellen
Versionen mehr von der echten Sundtek-Website laden** - jedes Gerät im LAN
landet für jede `sundtek.de`-Anfrage beim lokalen, eingefrorenen Mirror,
nicht nur für die Picons. Web-UI-Updates von Sundtek (neue Firmware-
Hinweise, geänderte Assets etc.) kommen dadurch nicht mehr automatisch an.
Falls eine echte Web-UI-Aktualisierung ansteht: DNS-Override am Router
vorübergehend deaktivieren (Internetzugang vorausgesetzt), Update laden,
danach Override wieder aktivieren.

**Hinweis**: `/storage/dab-tvh-bridge/sundtek-ui-cache/picons/` (Sundtek-
Web-UI-Picons) ist ein **anderes** Verzeichnis als `/storage/picons/`
(3546 Dateien, von TVHeadend selbst für Kanal-Icons genutzt, per
`icon: file:///storage/picons/...` in den Kanal-Konfigurationen
referenziert) - zwei getrennte Picon-Sammlungen für zwei getrennte Zwecke.
Herkunft von `/storage/picons/` (z.B. per USB-Stick kopiert oder von einem
Picon-Repository heruntergeladen) ist im aktuellen Boxzustand nicht mehr
eindeutig nachvollziehbar.

## 4. Live-Signalqualität/Ensemble-Info per SDK, und warum Now-Playing/DLS nicht funktioniert

Sundtek liefert unter `/storage/dabplus/opt/include/` (`mcsimple.h`,
`mediaclient.h`, `mediacmds.h`) und `/storage/dabplus/opt/lib/libmcsimple.so`
das komplette C-SDK mit, das `mediaclient` selbst nutzt. Kein Compiler auf
der Box nötig - Python `ctypes` kann die exportierten Funktionen
(`net_open`, `net_ioctl`, `net_close`, alle `EXPORT`-markiert) direkt gegen
die `.so`-Datei aufrufen, keine besonderen Abhängigkeiten (`ldd` zeigt nur
Standard-Systembibliotheken, kein `LD_LIBRARY_PATH` nötig).

**Funktioniert zuverlässig, live bestätigt** (siehe
`bridge/dab_bridge.py:get_dab_signal_info()`, Endpunkt `/dab/signal`):
- `DAB_GET_ENSEMBLE_INFO` (128) - Ensemble-Name
- `DAB_GET_TUNER` (68) - Lock-Status, RSSI/SNR/FIC-Qualität/CNR
- `DAB_GET_TIME` (129) - aktuelle DAB-Zeit
- `DAB_SET_FREQUENCY` (67) mit gesetzter `sid` - Sender gezielt anwählen
  (funktioniert nachweislich, entspricht `mediaclient --sid`)

Die `/dev/dabN`-Gerätedateien (major 81) sind reine virtuelle
Client-Schnittstellen für `net_open()` - alte Instanzen aus früheren
Scans/Tunes häufen sich an (`/dev/dab0` … `/dev/dab13` o.ä.) und lassen sich
nicht mehr öffnen; nur die zuletzt angelegte funktioniert. Deshalb probiert
`get_dab_signal_info()` `/dev/dab*` sortiert nach `mtime` (neueste zuerst).
Die eigentliche Hardware-Kommunikation von `mediasrv` läuft ohnehin über ein
rohes USB-Gerät (`/dev/sundtek/usb/...`), nicht über diese virtuellen Nodes.

**Funktioniert NICHT, ausführlich getestet**: `DAB_GET_DIGITAL_SERVICE_DATA`
(119), das laut SDK-Header Dynamic-Label-Text (Songtitel/Interpret) liefern
soll. Getestet mit echtem, aktivem Stream (u.a. SUNSHINE LIVE, die
garantiert DLS sendet), mit und ohne vorherigem `DAB_SET_FREQUENCY`+`sid`
im selben `ctypes`-Prozess - immer ein leeres Ergebnis (`status=0, len=0`).
Per `media_set_loglevel("max")` (ebenfalls SDK-exportiert) aktiviertes
Server-Logging (`/var/log/mediasrv.log`) zeigte beim echten HTTP-Stream-
Request klare Einträge (`DAB SET FREQUENCY`, `Tuning Service ID`, `Unmute
Audio`) - aber **keine einzige Zeile** für die eigenen
`DAB_GET_DIGITAL_SERVICE_DATA`-Aufrufe, selbst während eines aktiven
Streams. Das zeigt: der eigene `ctypes`-Client erreicht offenbar nicht
denselben serverseitigen Codepfad wie ein "echter" Client (z.B. weil
`mediaclient`/die Stream-HTTP-Schnittstelle ein anderes, nicht
dokumentiertes Protokoll-Detail beim Öffnen der Session mitschickt). Ein
`gdb`-Trace direkt gegen den laufenden `mediasrv`-Prozess (safe nur bei
inaktivem Stream, sauber wieder `detach`t) bestätigte: kein `ioctl`-Aufruf
mit Request 119 im Server während eines Streams messbar. Auch das
`--getdigitaldata`-Flag, auf das ein Forenbeitrag verwies, existiert in der
installierten `mediaclient`-Version (Build `2026-06-25`, per
`media_build_version()` ausgelesen) nachweislich nicht (komplette
315-zeilige `--help`-Ausgabe durchsucht, null Treffer).

**Lösung für "was läuft gerade"**: nicht vom Tuner/SDK, sondern direkt aus
TVHeadends eigener `/api/status/subscriptions` gelesen - TVHeadend weiß
ohnehin exakt, welcher Kanal an welchen Client gestreamt wird, da es die
Subscription selbst verwaltet. Live bestätigt (`GET /dab/now_playing`,
`bridge/dab_bridge.py:get_dab_now_playing()`): korrekt `["Schwarzwaldradio"]`
während eines echten Streams, `[]` im Leerlauf.

**Einordnung**: dieser SDK-Weg wird bewusst nur für read-only
Zusatzinformationen genutzt (Signalqualität, Ensemble-Name), die die
HTTP-API gar nicht anbietet - alles, was der Rest des Projekts ohnehin
schon zuverlässig über die HTTP-`cmd=`-API und die dokumentierten
Service-Neustarts löst (Scan auslösen, Senderliste holen), bleibt bewusst
dabei. Siehe [ARCHITECTURE.md](ARCHITECTURE.md), Abschnitt "Home Assistant
Integration", für die Einordnung, warum keine generelle Umstellung auf
diesen Weg sinnvoll ist.

## 5. Zusammenfassung: alle Sundtek-bezogenen systemd-Units

| Unit | Zweck | Port |
|---|---|---|
| `sundtek-mediasrv.service` | Tuner-Ansteuerung, Scan-Engine | intern (kein TCP) |
| `sundtek-rtspd.service` | RTSP/HTTP-Streaming, Web-Admin-UI, Sender-DB | 22000 |
| `sundtek-ui-proxy.service` | Offline-Picon-Mirror für das Web-UI | 80 |

Dazu kommen die beiden Projekt-eigenen Bridges (siehe
[docs/ARCHITECTURE.md](ARCHITECTURE.md)):

| Unit | Zweck | Port |
|---|---|---|
| `dab-bridge.service` | DAB+ Rescan-Automatisierung, M3U für TVHeadend | 8081 |
| `dvbt-bridge.service` | DVB-T Standort-Umschaltung/-Scan-Automatisierung | 8082 |
