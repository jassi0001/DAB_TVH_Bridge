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
automatisiert `bridge/dab_bridge2.py`/`bridge/fm_bridge2.py`.

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

## 4. Live-Signalqualität/Ensemble-Info, Now-Playing, Cover-Art/DLS (DAB+) und RDS (FM) per SDK

Sundtek liefert unter `/storage/dabplus/opt/include/` (`mcsimple.h`,
`mediaclient.h`, `mediacmds.h`) und `/storage/dabplus/opt/lib/libmcsimple.so`
das komplette C-SDK mit, das auch `mediaclient` selbst nutzt. Kein Compiler
auf der Box nötig - Python `ctypes` ruft die exportierten Funktionen
(`net_open`, `net_ioctl`, `net_close`, alle `EXPORT`-markiert) direkt gegen
die `.so`-Datei auf, keine besonderen Abhängigkeiten (`ldd` zeigt nur
Standard-Systembibliotheken, kein `LD_LIBRARY_PATH` nötig). Beide Bridges
(`dab_bridge2.py`, `fm_bridge2.py`) nutzen ausschließlich diesen direkten
Weg - kein externer `mediaclient`-Prozess ist an irgendeiner Stelle
beteiligt.

### 4.1 Signalqualität/Ensemble-Info (SDK, direkt per ctypes)

**Funktioniert zuverlässig, live bestätigt** (siehe
`bridge/dab_bridge2.py:get_dab_signal_info()`, Endpunkt `/dab/signal`;
FM hat dasselbe über `fm_bridge2.py:get_fm_signal_info()`/`/fm/signal`,
allerdings nur die rohe V4L2-Signalstärke, kein Ensemble-Äquivalent):
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

**Was ist ein guter SNR-Wert?** Keine offizielle Kalibrierungs-Doku von
Sundtek für dieses Feld gefunden - es ist unklar, ob es sich um echtes,
kalibriertes dB handelt oder um einen rohen internen Treiberwert. Empirisch
auf dieser Box beobachtet: `snr≈17` → sauberer, vollständiger
Slideshow-Empfang (RTL RADIO); `snr≈6-7` → durchgehend beschädigte/
unvollständige Slideshow-Bilder (ANTENNE NDS, ffn). Allerdings: diese
schwachen Messungen fielen zeitlich mit einem laufenden TVHeadend-Mux-Scan
zusammen (siehe Abschnitt 4.4) - der Tuner war also mutmaßlich zusätzlich
durch den Scan blockiert, nicht zwingend nur durch schwachen Empfang. Die
Zahlen sind also ein Anhaltspunkt, keine verlässliche Kalibrierung. Für
DAB/DAB+ allgemein gilt in der Fachliteratur grob: unter ~5 dB C/N =
Aussetzer/kein Empfang, ~8-12 dB = brauchbar, >15 dB = komfortabel stabil -
deckt sich grob mit dem hier beobachteten Muster.

### 4.2 Now-Playing (welcher Sender läuft gerade)

**Nicht** vom Tuner/SDK gelesen, sondern direkt aus TVHeadends eigener
`/api/status/subscriptions` - TVHeadend weiß ohnehin exakt, welcher Kanal an
welchen Client gestreamt wird, da es die Subscription selbst verwaltet
(`GET /dab/now_playing`/`/fm/now_playing`). Ein eigener `ctypes`-Client, der
versucht den aktuell getunten Service selbst auszulesen, bekommt keinen
zuverlässigen Wert - TVHeadends eigene Buchführung ist die einzige robuste
Quelle.

### 4.3 Cover-Art (MOT Slideshow) und DLS bei DAB+ - direkt über die SDK, ohne externen Prozess

Beide Bridges lesen Digital-Service-Daten (MOT-Slideshow-Bilder, Dynamic-
Label-Text) direkt über `DAB_GET_DIGITAL_SERVICE_DATA` (Ioctl-Nummer 119)
aus - ein zweiphasiger Probe-dann-Fetch-Aufruf gegen dasselbe
`/dev/dabN`-Gerät, das auch für Signalqualität/Ensemble-Info genutzt wird.
Kein `mediaclient`-Subprozess ist beteiligt.

**Segment-Reassemblierung (MOT Slideshow)**: die zurückgelieferte
`dab_service_data`-Struktur liefert einzelne Data-Group-Fragmente. Das
Data-Group-Header-Parsing (Extension-/CRC-/Segment-/User-Access-Flags,
Segmentindex, Transport-ID) folgt exakt ETSI EN 300 401 §5.3.3.1 und wurde
gegen echte, aufgezeichnete Rohdaten dieser Box (RTL RADIO, NDR 2 NDS)
bit-für-bit verifiziert. Die MOT-Header-**internen**
Body-Size-/Header-Size-Felder ließen sich dagegen nicht zuverlässig auf
sinnvolle Werte dekodieren (eine Diskrepanz in der Berechnung des
User-Access-Feld-Anteils, nicht weiter verfolgt) - statt einem einmal
falsch bestätigten Offset zu vertrauen, werden die beiden einzigen
tatsächlich benötigten Dinge stattdessen direkt und selbstverifizierend
gesucht:
- der Start des Bildkörpers, über dessen eigene Magic-Bytes (JPEG
  `\xff\xd8\xff` bzw. PNG-Signatur)
- der `ContentName`-Parameter (Dateiname/Bildtyp), über sein
  charakteristisches erstes Byte (PLI=3 + Parameter-ID=12 ergibt
  `0xCC`), gefolgt von einer plausiblen Kurzform-Länge und druckbarem Text

Beide Heuristiken sind selbstprüfend (Magic-Bytes bzw. Druckbarkeits-Check),
ein Fehltreffer ist damit praktisch ausgeschlossen, auch ohne die exakten
Header-Offsets zu kennen.

**Mehrere Bildtypen pro Sender**: manche Sender (z.B. NDR 2, NDR Info)
senden nicht ein einzelnes, rotierendes Cover, sondern mehrere
verschiedene Inhaltstypen verschachtelt im 15-20s-Takt - Content-Namen wie
`20260923_051518Z_PROGRAMMTIPP_47698.jpg` zeigen das Typ-Segment explizit
(`NEWS`, `SENDUNG`, `TEASER`, `TEASER2`, `COVER`, `WETTER`, `NEWS2`,
`PROGRAMMTIPP` beobachtet; ein einfacher Zählername wie `37.JPG` fällt auf
den Typ `cover` zurück). `/dab/artwork_info` liefert den erkannten Typ des
zuletzt aktualisierten Bilds; `/dab/artwork` liefert immer nur das eine
aktuellste Bild (`current.jpg`, atomar per `os.replace()` ersetzt), keinen
Typ-für-Typ-Puffer - die Home-Assistant-Karte zeigt also je nach Sender
mal ein Cover, mal ein Wetter- oder Nachrichtenbild, aber immer nur eins
auf einmal.

**DLS (Dynamic Label, Songtitel/Interpret)**: strukturell implementiert
(`type == DLS_TYPE` in derselben Datenstruktur, Textfeld ab Byte 2 nach
EN 300 401 §7.4.5.2 dekodiert), aber bisher bei **keinem** auf dieser Box
getesteten Sender tatsächlich mit echten Daten beobachtet - alle bisher
empfangenen Digital-Service-Daten waren MOT-Typ. Das Parsing verwirft
defensiv alles, was nicht zu druckbarem Text mit plausibler Länge dekodiert,
statt etwas Falsches anzuzeigen. `/dab/now_playing_text` liefert deshalb
aktuell durchgehend `text: null`.

### 4.4 RDS bei FM - ebenfalls direkt über die SDK

FM nutzt statt Digital-Service-Daten den V4L2-nahen `FM_RDS_STATUS`-Ioctl,
der pro Aufruf eine RDS-Gruppe liefert. Programmname (PS, Gruppe 0) und
Radiotext (RT, Gruppe 2, mit A/B-Flag-Umschaltung für neuen vs.
fortgesetzten Text) werden daraus zeichenweise zusammengesetzt (inkl. der
EBU-Sonderzeichen-Tabelle für deutsche Umlaute), dazu Sender-ID (PI) und
Programmtyp (PTY), sobald das Gerät `rdssync` meldet. `GET /fm/rds` liefert
das Ergebnis. Genau wie bei DAB+ (Abschnitt 4.1/4.3) wird das Gerät nur
geöffnet, während TVHeadend laut eigener Subscriptions-API tatsächlich
gerade FM streamt - andernfalls würde allein das Öffnen des Geräteknotens
den gemeinsamen Tuner ungefragt in den FM-Modus zwingen (siehe
[ARCHITECTURE.md](ARCHITECTURE.md#gemeinsamer-tuner-dab-und-fm-schliessen-sich-gegenseitig-aus)).

### 4.5 Persistentes Debug-Log

`GET /dab/digitaldata_debug` liefert die letzten 200 Zeilen eines
persistenten Logs (`dab-tvh-bridge/digitaldata_debug2.log`), das jeden
erfolgreichen Bild-Fund und jeden erkannten Senderwechsel protokolliert -
unabhängig von `print()`/journalctl, deren Ausgaben sich auf dieser Box als
unzuverlässig erwiesen haben (journalctl rotiert innerhalb weniger Minuten
alles weg).

### 4.6 TVHeadend-eigener Mux-Scan läuft nach dem eigenen Scan-Skript weiter

Wichtig für alles, was den Tuner direkt anspricht (SDK-Frequenz-Sweeps,
Signalqualitäts-Vergleiche etc.): ein von einer der beiden Bridges
ausgelöster Scan-Lauf (`/dab/status`/`/fm/status` → `running: false`,
`returncode: 0`) bedeutet **nicht**, dass der Tuner wieder frei ist.
TVHeadend arbeitet nach einem (Neu-)Scan seine eigene Mux-Scan-Queue im
Hintergrund weiter ab - `scan_state` pro Mux (`/api/mpegts/mux/grid`, bzw.
Feld-Enum über `/api/mpegts/mux/class`): `0`=INAKTIV (fertig),
`1`=AUSSTEHEND (wartet in der Queue), `2`=IDLE PEND, `3`=AKTIV (wird
gerade gescannt). `/dab/status`/`/fm/status` liefern deshalb zusätzlich
`tvh_mux_scan_pending`, `tvh_mux_scan_active` und den zusammengefassten
Bool `tvh_scanning` - jeder Code, der den Tuner direkt (per SDK/ctypes)
anspricht, sollte das prüfen, nicht nur den eigenen `running`-Status.

### 4.7 Einordnung

Dieser SDK-Weg (Abschnitte 4.1-4.4) wird bewusst nur für Zusatz-
informationen genutzt, die die HTTP-`cmd=`-API gar nicht anbietet
(Signalqualität, Ensemble-Name, Cover-Art/DLS, RDS) - alles, was der Rest
des Projekts ohnehin schon zuverlässig über die HTTP-API und die
dokumentierten Service-Neustarts löst (Scan auslösen, Senderliste holen),
bleibt bewusst dabei. Siehe [ARCHITECTURE.md](ARCHITECTURE.md), Abschnitt
"Home Assistant Integration", für die Einordnung, warum keine generelle
Umstellung auf diesen Weg sinnvoll ist.

## 5. Zusammenfassung: alle Sundtek-bezogenen systemd-Units

| Unit | Zweck | Port |
|---|---|---|
| `sundtek-mediasrv.service` | Tuner-Ansteuerung, Scan-Engine | intern (kein TCP) |
| `sundtek-rtspd.service` | RTSP/HTTP-Streaming, Web-Admin-UI, Sender-DB | 22000 |
| `sundtek-ui-proxy.service` | Offline-Picon-Mirror für das Web-UI | 80 |

Dazu kommen die drei Projekt-eigenen Bridges (siehe
[docs/ARCHITECTURE.md](ARCHITECTURE.md)):

| Unit | Zweck | Port |
|---|---|---|
| `bridge-dab.service` | DAB+ Scan-Automatisierung, Cover-Art/DLS, M3U für TVHeadend | 8091 |
| `bridge-fm.service` | FM Scan-Automatisierung, RDS, M3U für TVHeadend | 8093 |
| `bridge-dvbt.service` | DVB-T Standort-Umschaltung/-Scan-Automatisierung | 8082 |
