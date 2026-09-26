# TVHeadend-Eigenheiten, die dieses Projekt umgeht

Alles hier wurde empirisch am laufenden System herausgefunden (TVHeadend
Version wie auf der Box installiert, siehe eigener LibreELEC-Kodi-Addon
`service.tvheadend43`). Kein Anspruch auf allgemeine Gültigkeit für andere
TVHeadend-Versionen, aber alles hier ist live nachvollzogen und mehrfach
reproduziert.

## 1. `delchfrmgrp` (Kanal aus Favoriten-Gruppe entfernen) ist funktionslos

Die Sundtek-eigene Web-API (`servercmd.xhx`, Port 22000 - **nicht**
TVHeadends eigene API) bietet `cmd=delchfrmgrp` zum Entfernen von Sendern
aus einer benutzerdefinierten Gruppe/Liste ("DAB" o.ä.). Dieser Aufruf
liefert immer eine leere Erfolgsantwort, entfernt aber **nie** etwas -
egal in welcher Kombination von Parametern, Timing (sofort vs. Minuten
nach der letzten Änderung) oder Encoding getestet. Sogar der Browser
selbst bekommt denselben "leeren Ensemble-Eintrag" nie sauber aus der
Liste entfernt (verifiziert per HAR-Mitschnitt echter Browser-Sessions) -
es ist also kein Bug in diesem Projekt, sondern in der Sundtek-Firmware
selbst.

**Konsequenz für dieses Projekt**: `bridge/dab_bridge2.py` verwaltet gar
keine Sundtek-seitige Favoriten-/Gruppen-Liste mehr. Stattdessen liest es
bei jeder Anfrage direkt das Ergebnis des **neuesten abgeschlossenen
Scans** (`cmd=providers` -> höchste `prid` -> `cmd=chlist&mode=scanlist`)
und baut die M3U-Playlist live daraus. Kein Lösch-Workaround nötig, weil
gar nichts mehr gelöscht werden muss.

## 2. DAB+-Scan startet nur nach Neustart von mediasrv+rtspd

`scan=start&deviceid=0&mode=15` (Sundtek-API) wird ohne Fehler
entgegengenommen, aber ohne vorherigen Neustart von
`sundtek-mediasrv.service` **und** `sundtek-rtspd.service` bleibt der Scan
bei einer festen Frequenz stehen und es erscheint nie ein neuer Eintrag in
`cmd=providers`. Grund unbekannt (vermutlich interner Zustand, der nur bei
Prozessstart korrekt initialisiert wird). `bridge/dab_bridge2.py` restartet
deshalb beide Dienste unmittelbar vor jedem Scan-Trigger.

## 3. `cmd=providers`-Historie ist nicht dauerhaft/nicht fortlaufend

Die `prid`-Werte (Scan-Index) werden von `mediasrv` umnummeriert bzw.
alte Einträge fallen irgendwann einfach raus - nicht garantiert, dass
`prid=0` nach einiger Zeit noch derselbe Scan ist wie beim ersten Abfragen.
Deshalb wird der "neueste" Scan bei jeder Nutzung frisch per `max(prid)`
ermittelt statt eine `prid` zu cachen.

## 4. DAB+ IPTV-Auto-Netzwerk räumt sich selbst auf - DVB-T-Netzwerke nicht

TVHeadends `iptv_auto_network` (unser "DAB+"-Netzwerk) entfernt beim
erneuten Einlesen der M3U-Quelle automatisch Muxe/Services, die in der
neuen Playlist nicht mehr vorkommen (empirisch bestätigt: 52 -> 47 Muxe
nach einem Rescan an bekanntem Standort, exakt die 5 nicht mehr
vorhandenen automatisch entfernt, keine Karteileichen). Für DVB-T-Netzwerke
(Typ `dvb_network_dvbt`, manuell/wizard-artig angelegt) gibt es **keinen**
entsprechenden Auto-Cleanup - candidate Muxe ohne Empfang bleiben für
immer als "Kein Empfang" (`scan_result=2`) stehen, bis sie explizit
gelöscht werden. Deshalb macht `bridge/dvbt_bridge.py` das Aufräumen nach
jedem Scan selbst (`prune_failed_dvbt_muxes`).

## 5. Neu erstellte DVB-T-Muxe werden nicht automatisch gescannt

Ein per `/api/mpegts/network/mux_create` frisch erzeugter Mux landet in
`scan_state=0` (INAKTIV) und wird von TVHeadends eigener "neuer Mux -> PEND"
Logik **nicht** erfasst - diese Automatik greift offenbar nur beim
TVHeadend-eigenen Startup-/Discovery-Pfad, nicht für zur Laufzeit per API
angelegte Muxe. `scan_state` muss manuell auf `3` (AKTIV) gesetzt werden,
um den Mux in die Scan-Warteschlange zu bekommen.

**Gelöste Ursache eines lange verwirrenden Folgefehlers**: `scan_state`
auf einen Mux zu setzen, dessen Netzwerk noch `enabled: false` ist (also
zwischen `create_dvbt_network()` und dem späteren `activate_dvbt_network()`
- ein neues Netzwerk wird bewusst zunächst deaktiviert angelegt), wird
**zuverlässig stillschweigend ignoriert**. Schlimmer: solange das Netzwerk
deaktiviert ist, liefert auch `/api/mpegts/mux/grid` gefiltert nach diesem
Netzwerknamen teils **gar keine Treffer**, obwohl die Muxe laut
`num_mux`-Feld des Netzwerk-Objekts längst existieren. Das sah zunächst
nach einem reinen Timing-/Lastproblem aus (Wartezeiten von ~10s bis über
2 Minuten wurden beobachtet, mit fehlgeschlagenen Versuchen dazwischen) -
war aber tatsächlich reine Ursache-Wirkung: **sobald das Netzwerk aktiviert
wird, sind die Muxe sofort sichtbar und `scan_state` greift beim ersten
Versuch**, unabhängig von jeder Wartezeit. `bridge/dvbt_bridge.py` ruft
`_force_mux_scan()` deshalb bewusst **nach** `activate_dvbt_network()` auf,
nicht davor/während `create_dvbt_network()` - mehrfach mit angeschlossener
Antenne reproduziert und bestätigt (vorher: unzuverlässig bis komplett
erfolglos je nach Fall; danach: durchgehend erster Versuch erfolgreich).

## 6. Kein "Radio"-Flag auf Kanal-Ebene

TVHeadends `channel`-Objekt hat kein eigenes gespeichertes Radio/TV-Flag.
Kodi (und andere HTSP-Clients) leiten Radio- vs. TV-Klassifikation rein
aus dem `dvb_servicetype` des zugrunde liegenden Service ab (10 = Advanced
codec digital radio, 31 = Advanced codec HD digital TV, etc.). Bei DAB+
wird das bewusst per ffmpeg-Flag (`-mpegts_service_type
advanced_codec_digital_radio`) gesetzt; bei DVB-T kommt es automatisch
korrekt aus den echten Sendedaten (empirisch bestätigt: ein realer Scan
lieferte 52 TV- und 8 echte Radio-Services, korrekt unterschieden).

Zusätzlich gibt es einen TVHeadend-**Channel-Tag** "Radio channels" (und
"TV channels") - eine rein manuelle/organisatorische Zusatz-Markierung,
unabhängig vom technischen `dvb_servicetype`. Beide Bridges setzen diesen
Tag automatisch passend (DAB+: immer "Radio channels"; DVB-T: je nach
`dvb_servicetype`).

## 7. EPG-Einstellungen für synthetische DAB+-Streams

Zwei **getrennte** Felder, beide standardmäßig "an", beide für DAB+
sinnlos (da die ffmpeg-erzeugten Streams keine echten EPG/EIT-Daten
enthalten):
- Kanal-Ebene: `epgauto` ("EPG Quelle automatisch zuordnen")
- Mux-Ebene: `epg` ("EPG-Abfrage", Wert `1` = "Aktivieren (auto)")

Beide werden von `bridge/dab_bridge2.py` bei jedem Mapper-Durchlauf
automatisch auf "aus" gesetzt (auch rückwirkend für bereits gemappte
Kanäle/Muxe, nicht nur neue).

## 8. `scanfile` (vordefinierte Sender-/Mux-Listen) funktioniert nicht über die API

Für DVB-T-Netzwerke bietet TVHeadends Netzwerk-Assistent (`Configuration ->
DVB inputs -> Networks -> Add network`) eine Länder-/Bundesland-Dropdown-
Auswahl vordefinierter Muxe (z.B. "Germany: de-All"). Das zugehörige Feld
`scanfile` existiert zwar in der Netzwerk-Erstellungs-API
(`/api/mpegts/network/builders`), aber weder direkt beim
`/api/mpegts/network/create` (mit oder ohne zusätzliches `wizard: true`)
noch über `/api/mpegts/network/mux_create` mit nur `scanfile` als Wert
erzeugt es tatsächlich Muxe - keine Fehlermeldung, keine Log-Aktivität in
TVHeadends eigenem Journal, einfach keine Wirkung. Vermutung: die
Muxe-Erzeugung aus der vordefinierten Liste passiert komplett clientseitig
im Browser-JavaScript (liest die Datei, ruft dann pro Kanal einzeln
`mux_create` auf) statt serverseitig.

**Workaround**: `bridge/dvbt_bridge.py` generiert die deutschen DVB-T2-
Kandidatenfrequenzen selbst (474-778 MHz in 8-MHz-Schritten, 39 Kandidaten
- exakt das, was TVHeadends eigene mitgelieferte Datei
`dvb-scan/dvb-t/de-All` enthält, siehe deren Rohinhalt/Kommentar für die
Herleitungsformel) und ruft `mux_create` selbst pro Frequenz auf. Danach
werden alle Muxe ohne Empfang wieder entfernt (siehe Punkt 4).

## 9. `/api/dvb/scanfile/list` liefert abgeschnittene Dateinamen

Die `key`-Felder in der Antwort von `cmd=dvb/scanfile/list` fehlt jeweils
das letzte Zeichen (z.B. `dvb-t_de-Al` statt `dvb-t_de-All`,
`dvb-t_de-Baden-Wuerttember` statt `...-Wuerttemberg`) - ein Anzeigefehler
in dieser TVHeadend-Version. Die echten Dateinamen findet man direkt im
Dateisystem unter `<TVHeadend-Addon-Verzeichnis>/dvb-scan/dvb-t/` (auf
dieser Box: `/storage/.kodi/addons/service.tvheadend43/dvb-scan/dvb-t/`).

## 10. Netzwerk löschen cascaded auf Muxe/Services, aber nicht auf Kanäle

`/api/idnode/delete` auf eine Netzwerk-UUID (der Papierkorb-Button in
Configuration -> DVB inputs -> Networks) entfernt zuverlässig alle Muxe und
Services dieses Netzwerks - aber die daraus gemappten **Kanäle bleiben
bestehen**, jetzt mit null verknüpften Services. TVHeadend behandelt einen
Kanal als eigenständiges, potenziell von mehreren Netzwerken gemeinsam
genutztes Objekt und löscht ihn deshalb nie automatisch mit. Live bestätigt:
nach einer Netzwerk-Löschung über `/dvbt/delete_network` blieben 45 verwaiste
Kanäle zurück (viele als `{name-not-set}`, da ihr Name via `autoname` aus dem
jetzt fehlenden Service kam); über die ganze Session mit mehreren manuellen
Löschungen über die Web-UI kumuliert waren es sogar 71.

**Konsequenz für dieses Projekt**: `bridge/dvbt_bridge.py`s
`delete_dvbt_network()` merkt sich vor dem Löschen, welche Kanäle die
Services dieses Netzwerks speisen, und entfernt danach jeden davon, der
jetzt (netzwerkübergreifend) auf null Services steht. Das ist bewusst der
**einzige** Ort im ganzen Projekt, der je einen Kanal löscht - ein einzelnes
Ausschließen eines Senders (`disable_excluded_dvbt_services()`) räumt
absichtlich nichts mehr auf, siehe Punkt 11 unten (verwandtes, aber
eigenständiges Thema zu `prune_failed_dvbt_muxes`, siehe Punkt 4).

## 11. Bestehende Kanäle sollen bei jedem Mapper-Durchlauf unangetastet bleiben - zwei Ursachen mussten dafür behoben werden

Anforderung: ein deaktivierter Kanal soll deaktiviert bleiben, ein Kanal
ohne aktuell gefundenen Service soll nicht gelöscht werden (damit er bei
Wiederauffinden erneut zugeordnet werden kann), und Kanalnummer/-name sollen
nur durch explizite Aktion geändert werden, nie automatisch durch einen
Scan-/Aktivieren-Durchlauf. Zwei echte, live bestätigte Lücken standen dem
im Weg:

**a) `channel_set_name()` überschreibt den Namen unconditional.** Quellcode-
Analyse von `service_mapper.c` (tvheadend/tvheadend auf GitHub) zeigt: wenn
`tidy_channel_name` aktiv ist, ruft der Mapper für **jeden** gemappten
Service `channel_set_name(chn, tidy_name)` auf - auch wenn `chn` ein
bereits bestehender, gerade nur gemergter Kanal ist. `channel_set_name()`
selbst prüft `ch_autoname` **nicht** (das tut nur der separate
`channel_class_autoname_set()`-Property-Setter, den der Mapper nicht
verwendet) - der Name wird also auch bei einem Kanal mit `autoname: false`
und manuell/via `/dvbt/rename` gesetztem Namen bei jedem weiteren
Mapper-Durchlauf stillschweigend auf den frisch getidyten Servicenamen
zurückgesetzt. Live bestätigt: ein Kanal manuell auf "ZDF TESTNAME"
umbenannt (mit `autoname: false`) wurde bei aktivem `tidy_channel_name` bei
der nächsten `/dvbt/activate` sofort wieder zurücküberschrieben.
**Fix**: `DVBT_MAPPER_SETTINGS["tidy_channel_name"]` steht jetzt auf
`False`. Neue Kanäle bekommen ihren Namen stattdessen über TVHeadends
eigenen `autoname`-Fallback (roher Servicename), unser eigener
`apply_dvbt_renames()`-Schritt direkt danach ist der einzige Weg, wie ein
Kanalname noch verändert wird.

**b) `/api/channel/grid` blendet deaktivierte Kanäle standardmäßig aus.**
Ohne den Zusatzparameter `all=1` liefert der Grid-Endpunkt nur aktivierte
Kanäle zurück (live bestätigt: ein manuell deaktivierter Kanal fehlte
komplett in der Antwort, obwohl er via `/api/idnode/load?uuid=...` direkt
nachweislich weiter existierte, mit `name`/`number`/`enabled: false`
unverändert). Ohne diesen Parameter wäre ein deaktivierter Kanal über
dieses Projekt nie wieder auffindbar gewesen - `_find_dvbt_channel_uuid()`
hätte ihn nie gefunden, `/dvbt/channel_enabled` hätte ihn also nie wieder
aktivieren können. **Fix**: `list_dvbt_channels()` und `tag_dvbt_channels()`
hängen jetzt beide `&all=1` an ihre `/api/channel/grid`-Aufrufe an.

**c) Nebenbefund, live bestätigt, derselbe Fund erklärte auch einen
gemeldeten Bug ("TV-Kanäle bekommen kein TV-Tag")**: `tag_dvbt_channels()`
wählte den für die Radio/TV-Entscheidung herangezogenen Service per
`setdefault()` ohne jede Präferenz für "enabled" - ein zufällig zuerst
enumerierter deaktivierter/ausgeschlossener Simulcast-Service hätte
theoretisch die Tag-Entscheidung für einen Kanal verfälschen können.
Gehärtet: bevorzugt jetzt explizit einen aktivierten Service. Zusätzlich
wurde zwischen dem Mapper-Aufruf (`/api/service/mapper/save`) und dem
nachfolgenden Rename-/Tag-Schritt eine kurze Poll-Wartezeit
(`_wait_for_mapper_settle()`) eingebaut, da frisch gemappte
Service-zu-Kanal-Verknüpfungen (dieselbe Klasse von Propagierungsverzögerung
wie in Punkt 5) sonst noch nicht zuverlässig sichtbar waren, wenn direkt im
Anschluss erneut abgefragt wurde. Live-Test nach allen drei Fixes: 17 zuvor
untaggte Kanäle bekamen beim nächsten `/dvbt/activate` korrekt ihr
"TV channels"-Tag.

## 12. TVHeadends eigene Mux-Scan-Queue läuft nach Abschluss des eigenen Scan-Skripts weiter

Der von `bridge/dab_bridge2.py` ausgelöste Scan-Lauf meldet über
`/dab/status` `running: false, returncode: 0`, sobald das eigene Skript
fertig ist (Sundtek-Scan abgewartet, TVHeadend neu gestartet). Das
bedeutet aber **nicht**, dass der Tuner danach wieder frei ist: TVHeadend
arbeitet nach einem Neustart seine eigene Mux-Scan-Queue eigenständig im
Hintergrund weiter ab, ein Mux nach dem anderen. Live bestätigt
(2026-09-21): direkt nach einem als "fertig" gemeldeten Standort-Scan
("Example") standen laut `/api/mpegts/mux/grid` noch >25 DAB+-Muxe auf
`scan_state=1` (AUSSTEHEND) und einer auf `scan_state=3` (AKTIV) - vom
eigenen Skript aus nicht sichtbar, aber im TVHeadend-Web-UI unter "Muxe"
klar erkennbar ("Suchergebnis"-Spalte).

`scan_state`-Enum (per `/api/mpegts/mux/class`, Feld `scan_state`):
`0`=INAKTIV, `1`=AUSSTEHEND, `2`=IDLE PEND, `3`=AKTIV.

**Relevanz**: jeder Code, der den physischen Tuner direkt anspricht (z.B.
ein SDK-/`ctypes`-Frequenz-Sweep zur Signalqualitätsmessung, siehe
[SUNDTEK_INSTALL.md](SUNDTEK_INSTALL.md) Abschnitt 4.1), konkurriert dabei
mit TVHeadends eigenem Scan um denselben Tuner - beide Seiten stören sich
gegenseitig, ohne dass eine Fehlermeldung entsteht (nur schlechte/
unplausible Messwerte). **Fix**: `/dab/status` liefert seit diesem Fund
zusätzlich `tvh_mux_scan_pending`, `tvh_mux_scan_active` und den
zusammengefassten Bool `tvh_scanning` (siehe
[API.md](API.md)) - vor jedem direkten Tuner-Zugriff sollte das geprüft
werden, nicht nur der eigene `running`-Status.

## 13. `iptv_auto_network`-Netzwerkeinstellungen, auf die es für zuverlässiges Scannen konkret ankommt

Sowohl "DAB+ v2" als auch "FM v2" sind TVHeadend-`iptv_auto_network`-
Netzwerke und brauchen dieselben Feldwerte, damit Scans zuverlässig
Services finden (per `/api/mpegts/network/grid` gesetzt/geprüft):
`priority`/`spriority: 50`, `max_timeout: 10`, `idlescan: false`,
`scan_create: false`, `skipinitscan: false`, `max_streams: 1`,
`remove_args: "ticket"`, `ignore_chnum: true`, `refetch_period: 6000`
(Minuten - praktisch nie, da die Bridge TVHeadend nach jedem Rescan selbst
neu startet, siehe [ARCHITECTURE.md](ARCHITECTURE.md)). Abweichungen bei
Priorität, Timeout oder den beiden Scan-Flags (`idlescan`/`scan_create`)
haben sich live als unerwartet folgenreich erwiesen - Services wurden
dabei zuverlässig **nicht** gefunden, ohne dass eine Fehlermeldung
entstand.

## 14. Nur rtspds eigener M3U-Export liefert durchgehend funktionierende Stream-URLs

Sundteks eigene `cmd=chlist`-API liefert Stream-URLs, denen der
`comp=`-Parameter (Audio-Komponenten-Index innerhalb des Ensembles) fehlt
und deren `sid` mit `0x`-Präfix formatiert ist - beides live bestätigt als
Ursache für unzuverlässiges Umschalten. `http://127.0.0.1:22000/dab.m3u`
(rtspds eigener M3U-Export, trotz des Namens DAB+ **und** FM gemischt
enthaltend, nach `delsys` filterbar: `15`=DAB+, `255`=FM) liefert dagegen
durchgehend das korrekte Format inkl. `comp=` und unpräfixiertem `sid`, in
genau der Reihenfolge `devid/freq/sid/delsys/comp`. Zusätzlich liefert
`cmd=chlist` den Host in jeder URL so, wie der zuletzt auslösende Client
verbunden war (bei einem lokal auf der Box ausgelösten Scan also
`127.0.0.1`) - auch das live bestätigt als Ursache für nicht
funktionierende Streams, wenn dieser Host unverändert übernommen wird.
`fetch_stations()`/`fetch_live_stations()` beziehen deshalb die
Senderliste beider Bridges aus `/dab.m3u`, und `build_m3u()` schreibt den
Host in jeder Stream-URL zusätzlich explizit auf die reale LAN-IP der Box
um (Konstante `RTSPD_STREAM_HOST` in beiden Bridge-Dateien).

## 15. TVHeadends Mux-Scan-Reihenfolge richtet sich nicht nach der M3U-Reihenfolge

Quellcode-Analyse von `src/input/mpegts/mpegts_network_scan.c`
(tvheadend/tvheadend auf GitHub): Pending-Muxe liegen in zwei
Warteschlangen (`mn_scan_pend` für den Init-Scan, `mn_scan_ipend` für den
Idle-Scan bei `idlescan: true`), sortiert per `mm_cmp()` primär nach
`mm_scan_weight` (bei allen frisch angelegten Init-Scan-Muxen identisch)
und erst bei Gleichstand nach einem internen Zeitstempel
(`mm_start_monoclock`) - **nicht** nach Frequenz oder Position in der
Quell-M3U. Ein Timer (`mpegts_network_scan_timer_cb`) feuert alle 120
Sekunden und nimmt sich dabei jeweils **einen** Mux vom Kopf jeder Queue
vor. Schlägt ein Versuch fehl, wird der Mux per `mm_scan_state =
MM_SCAN_STATE_IDLE` markiert (UI-Anzeige "Inaktiv") und - sofern
`idlescan: true` - automatisch in `mn_scan_ipend` zurückgehängt
(`mpegts_network_scan_mux_done0()`), aber eben in die langsamere
Idle-Queue, nicht sofort erneut in die Standard-Queue. Die Reihenfolge von
Sendern in der von der Bridge gelieferten M3U hat auf die tatsächliche
Scan-Reihenfolge deshalb keinen Einfluss - der eigentliche Hebel für
zuverlässiges Scannen ist die Netzwerk-Konfiguration selbst (Punkt 13) und
die per API erzwungene, frequenzgruppenweise Aktivierung (`/dab/scan_ordered`,
`/fm/scan_ordered`, siehe [API.md](API.md)), die TVHeadends eigene
Warteschlangen-Reihenfolge gezielt umgeht.

## 16. Gemeinsamer physischer Tuner zwischen DAB+ und FM: TVHeadend kennt die Kopplung nicht

TVHeadend verarbeitet die Mux-Scan-Warteschlangen von "DAB+ v2" und
"FM v2" komplett unabhängig voneinander (siehe Punkt 15) - aus seiner
Sicht zwei gewöhnliche, unabhängige IPTV-Netzwerke, ohne jede Kenntnis,
dass beide denselben physischen Sundtek-Tuner exklusiv nutzen. Ohne
Gegenmaßnahme scannt/tunt TVHeadend deshalb gelegentlich beide Seiten
gleichzeitig, was den gemeinsamen Tuner zuverlässig durcheinanderbringt.
Die Lösung (prozessübergreifender `flock()`-Lock für selbst ausgelöste
Scans, plus ein Hintergrund-Wächter, der TVHeadends eigene autonome
Scan-Queue-Aktivität für die jeweils andere Seite zurückstellt, plus
automatisches Zurücksetzen bereits erfolgreich gescannter Muxe, die
TVHeadend grundlos erneut in die Warteschlange stellt) ist bewusst
identisch in beiden Bridges implementiert - siehe
[ARCHITECTURE.md](ARCHITECTURE.md#gemeinsamer-tuner-dab-und-fm-schliessen-sich-gegenseitig-aus)
für die vollständige Beschreibung. Wichtig für den Betrieb: der Wächter
läuft nur, solange der jeweilige Bridge-Prozess selbst läuft (`bridge-dab.
service`/`bridge-fm.service`) - ist eine der beiden Seiten gestoppt, ist
sie insoweit auch nicht mehr gegen TVHeadends autonome Scans der anderen
Seite abgesichert.
