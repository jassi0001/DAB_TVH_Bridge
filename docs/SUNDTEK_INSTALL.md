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

## 4. Live-Signalqualität/Ensemble-Info per SDK, Now-Playing, und Cover-Art (MOT Slideshow)

Sundtek liefert unter `/storage/dabplus/opt/include/` (`mcsimple.h`,
`mediaclient.h`, `mediacmds.h`) und `/storage/dabplus/opt/lib/libmcsimple.so`
das komplette C-SDK mit, das `mediaclient` selbst nutzt. Kein Compiler auf
der Box nötig - Python `ctypes` kann die exportierten Funktionen
(`net_open`, `net_ioctl`, `net_close`, alle `EXPORT`-markiert) direkt gegen
die `.so`-Datei aufrufen, keine besonderen Abhängigkeiten (`ldd` zeigt nur
Standard-Systembibliotheken, kein `LD_LIBRARY_PATH` nötig).

### 4.1 Signalqualität/Ensemble-Info (SDK, direkt per ctypes)

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
welchen Client gestreamt wird, da es die Subscription selbst verwaltet.
Live bestätigt (`GET /dab/now_playing`,
`bridge/dab_bridge.py:get_dab_now_playing()`): korrekt `["Schwarzwaldradio"]`
während eines echten Streams, `[]` im Leerlauf. Grund: ein eigener
`ctypes`-Client, der versucht den aktuell getunten Service selbst
auszulesen, bekommt keinen zuverlässigen Wert (siehe 4.3 für dieselbe
Grundproblematik bei den Digital-Service-Daten) - TVHeadends eigene
Buchführung ist die einzige robuste Quelle.

### 4.3 Cover-Art (MOT Slideshow) - funktioniert; DLS (Songtitel) - funktioniert nicht

**Korrektur einer früheren, falschen Annahme in dieser Doku**: hier stand
zuvor, `mediaclient --getdigitaldata` existiere nicht (angeblich keine
Treffer in der `--help`-Ausgabe). Das war falsch - der Befehl existiert und
funktioniert, ist nur aus unbekanntem Grund nicht in `--help` dokumentiert.
Aufruf: `mediaclient --getdigitaldata -d <device>` (z.B. `/dev/dab13`) - ein
**langlaufender Poll-Befehl**, kein One-Shot.

**Live bestätigt über mehrere Sender und lange Testfenster**:
- **MOT Slideshow (Cover-Bilder) funktioniert zuverlässig.** `mediaclient`
  schreibt dekodierte JPEGs nach `/tmp/<n>.JPG` und meldet `"OPENED:
  <pfad>"`, sobald es eine neue Datei zu schreiben beginnt.
- **DLS (Songtitel/Interpret) funktioniert dagegen nie**, weder über die
  CLI noch über den direkten SDK-Weg:
  - CLI-Ausgabe zeigte über mehrere Sender und lange Aufnahmefenster
    ausschließlich `data_group_type: 3` (MOT-Header) und `4` (MOT-Body),
    nie den DLS-Typ.
  - Direkter Test per `ctypes`/`DAB_GET_DIGITAL_SERVICE_DATA` (119)
    bestätigt dasselbe auf niedrigerer Ebene: das `type`-Feld der
    zurückgegebenen Struktur ist konstant `2` (MOT), nie `1` (DLS) - auch
    bei exklusivem Gerätezugriff (kein konkurrierender Prozess) und sehr
    hoher Poll-Rate (~19.000 Polls/Sekunde).
  - **Externe Bestätigung**: laut einem Discord-Beitrag, den der Nutzer
    gefunden hat, ist das DLS-Parsing in `mediaclient` aktuell auf
    Sundtek-Seite pausiert/deaktiviert - das deckt sich exakt mit dem hier
    beobachteten Verhalten. Es gibt dazu bereits eine offene Anfrage im
    Sundtek-Forum; sobald ein zukünftiger `mediaclient`-Build DLS wieder
    liefert, ist `_digitaldata_reader()` in `bridge/dab_bridge.py` die
    Stelle, an der das Parsing ergänzt werden muss (siehe TODO-Kommentar
    dort).
- Installierte `mediaclient`-Version: Build `2026-06-25` (per
  `media_build_version()`, SDK-exportiert, ausgelesen).

**Zwei nicht-triviale Implementierungsprobleme, live gefunden und behoben**
(siehe `bridge/dab_bridge.py`, Funktionen `digital_data_loop()`,
`_digitaldata_reader()`, `_copy_artwork_when_stable()`):

1. **glibc-Stdout-Pufferung**: `mediaclient`s eigenes stdout wird von glibc
   blockweise gepuffert, wenn es kein TTY ist (Standardverhalten bei
   `subprocess.PIPE`) - ein Reader-Thread bekam über die gesamte
   Prozesslaufzeit **keine einzige Zeile** Output, obwohl der Prozess
   nachweislich echte JPEG-Dateien schrieb. Lösung: `pty.openpty()`
   verwenden und den Slave-fd als stdout/stderr übergeben - dadurch erkennt
   `mediaclient` ein "interaktives Terminal" und schaltet auf
   Zeilenpufferung um.
2. **JPEG-Vollständigkeit**: MOT-Segmentdateien werden über mehrere Sekunden
   inkrementell geschrieben. Ein zu früher Kopiervorgang direkt bei
   `"OPENED:"` liefert nur ein ~500-Byte-Fragment. Auch "Größe ist seit N
   Checks stabil" reichte allein nicht aus - live beobachtet, dass eine
   "stabile" Datei trotzdem den EOI-Marker am Ende fehlte. Endgültige
   Lösung: sowohl SOI (`\xff\xd8`, erste 2 Bytes) als auch EOI (`\xff\xd9`,
   letzte 2 Bytes) prüfen, mit einem kurzen Retry, bevor eine Datei als
   vollständig akzeptiert wird.
3. **Timeout-Budget zu knapp für große Bilder**: das ursprüngliche Budget
   (`checks=4, interval=1.5s, max_attempts=20`, ~30s) war für Sender mit
   größeren Slideshow-Bildern zu kurz - live beobachtet (Sender "ffn"): eine
   Datei wuchs noch bei Sekunde 36 linear über 24 KB, war also bei Weitem
   nicht fertig. Das ursprüngliche 30s-Budget verwarf dadurch **jedes
   einzelne** Slide dieses Senders, unbemerkt. Behoben durch Erhöhung auf
   `checks=4, interval=2.0s, max_attempts=90` (~3 Min. Budget).

**Live gefundener Ausfall (2026-09-21): `digital_data_loop()`-Thread blieb
hängen, kein Bezug zum Code selbst.** Nach längerer, intensiver Testerei
(viele direkte SDK-Zugriffe über den Tag verteilt) reagierte die Cover-
Erfassung irgendwann gar nicht mehr auf Senderwechsel - derselbe
`mediaclient`-Prozess lief unverändert weiter, obwohl `/dab/now_playing`
längst einen anderen Sender zeigte. Ein reiner Neustart von
`dab-bridge.service` allein behob es NICHT (der Thread hing kurz danach
erneut fest) - erst ein Neustart der ganzen Kette
(`sundtek-mediasrv.service` + `sundtek-rtspd.service` + `dab-bridge.service`
zusammen, dieselbe Reihenfolge wie beim Scan-Trigger) brachte es zuverlässig
wieder zum Laufen, live verifiziert über einen erfolgreichen Sender-
wechsel danach. Deutet darauf hin, dass der eigentliche verklemmte Zustand
in `mediasrv` selbst lag (ein Low-Level-Ressourcenzustand, den die
ungewöhnlich hohe Zahl an `ctypes`-`net_open()`-Aufrufen aus den vielen
Testskripten dieser Session ausgelöst haben könnte), nicht im
Python-Code der Bridge. Falls die Cover-Erfassung wieder dauerhaft
"einfriert" (Sendername in `/dab/artwork_info` bleibt trotz echtem
Senderwechsel stehen): erst prüfen, ob `mediaclient --getdigitaldata`
weiterhin neue `/tmp/<n>.JPG`-Dateien schreibt (`ls -lat /tmp/*.JPG` -
**nach Zeit sortiert, nicht alphabetisch**, sonst werden neue niedrige
Nummern wie `11.JPG` übersehen); falls ja, aber `/dab/artwork_info` bleibt
trotzdem stehen, hilft der beschriebene Dreifach-Neustart.

**Weiterer live gefundener und behobener Bug (2026-09-21, "eingefrorener
Sendername")**: nach einem Senderwechsel (z.B. RTL RADIO -> Absolut BELLA)
blieb `/dab/artwork_info` dauerhaft auf dem alten Sendernamen ("channel":
"RTL RADIO") hängen, obwohl `/dab/now_playing` korrekt den neuen Sender
zeigte - die Karte in HA hätte dadurch potenziell ein Cover vom alten
Sender fälschlich als aktuell ausgegeben. Ursache: `_copy_artwork_when_
stable()`-Watcher-Threads sind unabhängig von `digital_data_loop()` und
können bis zu ihr volles ~3-Minuten-Budget brauchen; ein Watcher, der noch
für den ALTEN Sender läuft, wenn der Wechsel passiert, überschreibt nach
Abschluss die Artwork-Variablen mit dem alten Sendernamen und einem
täuschend aktuellen Zeitstempel - selbst nachdem der Kanalwechsel den
Zustand bereits korrekt zurückgesetzt hatte. **Fix**: ein Generation-
Zähler (`_digitaldata_generation`), der bei jedem Start/Stop eines
`mediaclient`-Prozesses erhöht wird; jeder Watcher merkt sich seine eigene
Generation beim Start und verwirft sein Ergebnis kommentarlos, falls sich
die Generation bis zum Abschluss geändert hat. Zusätzlich, als
client-seitiges Sicherheitsnetz: die Markdown-Karte in
`homeassistant/dashboard_media_tab.yaml` zeigt das Cover jetzt nur noch an,
wenn dessen `channel`-Attribut mit `sensor.dab_aktueller_sender`
übereinstimmt.

**Vereinzelt beobachtet, live bestätigt (2026-09-21), Ursache nicht
abschließend geklärt**: ein einzelnes erfasstes Cover-Bild zeigte visuell
eine Überlagerung/Verschmelzung zweier unterschiedlicher Slides (zwei
"RADIO"-Logos, zwei Textblöcke übereinander) - strukturell aber ein
vollständig valides JPEG (korrekter SOI/EOI, keine Abbruch-/Timeout-Datei).
Die Quelldatei auf der Box (`/tmp/N.JPG`) war zum Zeitpunkt der Erfassung
bereits stabil und veränderte sich danach nicht mehr - der Fehler steckte
also schon vor unserer Kopie im Ergebnis von `mediaclient`, nicht in einem
Timing-Problem beim eigenen Stability-Check. In einem direkt anschließenden
Test über mehrere Minuten (guter Empfang, SNR≈16-18) kamen 3 von 3 weiteren
Slides sauber und einzeln an - deutet auf einen seltenen Einzelfall hin
(z.B. eine kurze Empfangsstörung genau während der MOT-Segment-
Zusammensetzung, oder ein seltener interner mediaclient-Fehler), nicht auf
einen reproduzierbaren Bug in dieser Bridge. Falls das gehäuft auftritt,
wäre der nächste Schritt eine striktere Validierung (z.B. Prüfung auf genau
ein SOI/EOI-Paar ohne eingebettete zweite SOI-Marker im Datenstrom).

**Architektur** (`bridge/dab_bridge.py:digital_data_loop()`): läuft als
Hintergrund-Thread, pollt `get_dab_now_playing()` alle 5s und hält genau
einen `mediaclient --getdigitaldata`-Prozess am Leben, solange ein Sender
läuft - neu gestartet bei Senderwechsel, komplett gestoppt im Leerlauf.
Ein Senderwechsel wird erst nach **zwei aufeinanderfolgenden** übereinstim-
menden Polls akzeptiert (Entprellung) - ein einzelner, transienter
TVHeadend-Subscription-Ausreißer reichte in Tests aus, um einen laufenden
Capture-Vorgang grundlos neu zu starten und ein bereits fertig geladenes
Bild wegzuwerfen.

**Live gefundener Ausfall (2026-09-23): mehrere Bildtypen pro Sender statt
eines einzelnen Covers.** Ein persistentes Debug-Log (`/dab/digitaldata_debug`,
siehe unten) zeigte: manche Sender (live bestätigt bei NDR 2 und NDR Info)
senden nicht ein einzelnes, rotierendes Cover, sondern **mehrere
verschiedene Inhaltstypen** verschachtelt im 15-20s-Takt - Content-Namen wie
`20260923_051518Z_PROGRAMMTIPP_47698.jpg` zeigen das explizit (`NEWS`,
`SENDUNG`, `TEASER`, `TEASER2`, `COVER`, `WETTER`, `NEWS2`, `PROGRAMMTIPP`
beobachtet). Die alte Logik (ein einzelnes `artwork.jpg`, bei jedem neuen
Slide überschrieben) verwarf dadurch effektiv fast alles - ein neuer Typ
überschrieb potenziell einen noch validen älteren, ohne dass zwischen den
Typen unterschieden wurde. Zusätzlich zeigte sich bei diesen beiden
Sendern: praktisch **jeder** Slide blieb bei Dateigröße 0 hängen (nie
fertig übertragen), nur 2 von ~45 beobachteten kamen überhaupt mit Inhalt
an, beide dann noch mit falschem EOI-Marker (abgeschnitten) - vermutlich
Bandbreiten-/Timing-Problem bei diesen speziellen, inhaltsreichen Sendern,
nicht in dieser Bridge behebbar.

**Fix (Bild-Puffer statt Einzelbild)**: `ARTWORK_DIR` (statt einer festen
`DAB_ARTWORK_PATH`-Datei) hält jetzt eine Datei pro erkanntem Typ
(`_artwork_type_from_filename()` - erkennt das `<TYPE>`-Segment aus
Content-Namen wie oben; einfache Zählernamen wie `37.JPG`, die die meisten
Sender verwenden, fallen auf den Typ `cover` zurück). `/dab/artwork_info`
liefert zusätzlich `types` (Liste aller aktuell gehaltenen Typen mit
eigenem Zeitstempel); `/dab/artwork/<type>` liefert einen bestimmten Typ
einzeln, `/dab/artwork` weiterhin den zuletzt aktualisierten (Rückwärts-
kompatibilität). Die Home-Assistant-Markdown-Karte zeigt jetzt alle
aktuell verfügbaren Typen des laufenden Senders statt nur eines Bildes.

**Live gefundener Methodenfehler bei der eigenen Diagnose (2026-09-23):**
während der Untersuchung oben lief zeitweise ein zweiter, unabhängiger
`mediaclient --getdigitaldata`-Prozess parallel zur Produktions-Bridge auf
demselben Gerät (zum rohen Mitschneiden). Das Debug-Log zeigt eindeutig:
mit dem parallelen Prozess lieferte selbst RTL RADIO (sonst zuverlässig)
zweimal hintereinander **korrupte** Dateien (SOI-Marker bereits falsch,
nicht nur fehlendes EOI); unmittelbar danach, ganz ohne parallelen
Prozess, gelang derselbe Sender wieder sauber. **Zwei gleichzeitige
`mediaclient`-Instanzen auf demselben `/dev/dabN`-Gerät stören sich
offenbar gegenseitig die Daten** - bei künftigen Rohdaten-Mitschnitten zur
Fehlersuche nie parallel zur laufenden Bridge, sondern nur bei gestopptem
`dab-bridge.service` oder nach dessen Neustart, wenn sicher kein eigener
Capture-Vorgang für denselben Sender aktiv ist.

**Persistentes Debug-Log** (`bridge/dab_bridge.py:_debug_log()`,
`DIGITALDATA_DEBUG_LOG` = `dab-tvh-bridge/digitaldata_debug.log`, abrufbar
über `GET /dab/digitaldata_debug`): protokolliert jede "OPENED:"-Zeile,
jeden Erfassungsversuch (Start/Erfolg/Verwerfen mit Grund) und jeden
Senderwechsel dauerhaft in eine Datei - eingeführt, weil sich `print()`-
Ausgaben dieses Threads als unzuverlässig erwiesen (journalctl rotiert auf
dieser Box innerhalb von ~15 Min. alles weg, siehe oben).

### 4.4 TVHeadend-eigener Mux-Scan läuft nach dem eigenen Scan-Skript weiter

Wichtig für alles, was den Tuner direkt anspricht (SDK-Frequenz-Sweeps,
Signalqualitäts-Vergleiche etc.): der von `bridge/dab_bridge.py`
ausgelöste Scan-Lauf (`/dab/status` → `running: false`, `returncode: 0`)
bedeutet **nicht**, dass der Tuner wieder frei ist. TVHeadend arbeitet nach
einem (Neu-)Scan seine eigene Mux-Scan-Queue im Hintergrund weiter ab -
`scan_state` pro Mux (`/api/mpegts/mux/grid`, bzw. Feld-Enum über
`/api/mpegts/mux/class`): `0`=INAKTIV (fertig), `1`=AUSSTEHEND (wartet in
der Queue), `2`=IDLE PEND, `3`=AKTIV (wird gerade gescannt). Live
beobachtet: direkt nach einem abgeschlossenen Standort-Scan standen noch
>25 DAB+-Muxe auf `AUSSTEHEND` und einer auf `AKTIV` - der Tuner war also
noch minutenlang weiter beschäftigt.

**Deshalb liefert `/dab/status` seit 2026-09-21 zusätzliche Felder**:
`tvh_mux_scan_pending`, `tvh_mux_scan_active` und der zusammengefasste
Bool `tvh_scanning` - jeder Code, der den Tuner direkt (per SDK/ctypes)
anspricht, sollte vorher `tvh_scanning: false` prüfen, nicht nur den
eigenen `running`-Status. Der HA-Sensor `sensor.dab_scan_status` wertet
das bereits mit aus (Zustand `scanning`, solange `running` **oder**
`tvh_scanning` wahr ist).

### 4.5 Einordnung

Dieser SDK-Weg (Abschnitte 4.1-4.3) wird bewusst nur für Zusatz-
informationen genutzt, die die HTTP-`cmd=`-API gar nicht anbietet
(Signalqualität, Ensemble-Name, Cover-Art) - alles, was der Rest des
Projekts ohnehin schon zuverlässig über die HTTP-API und die dokumentierten
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
