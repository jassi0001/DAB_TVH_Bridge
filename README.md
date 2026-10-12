# TVHead-Projekt: DAB+, FM & DVB-T für ein reisendes Wohnmobil

Automatisierte Sender-Suche für DAB+ und FM (Sundtek USB-Stick) und DVB-T/T2
(TurboSight PCIe-Tuner) auf einer LibreELEC-Box mit TVHeadend, gesteuert
über Home Assistant - gedacht für ein Wohnmobil, das regelmäßig an neuen
Standorten steht und dort jeweils die lokal empfangbaren Sender braucht,
ohne dass jemand die TVHeadend- oder Sundtek-Web-UI manuell bedienen muss.

## Warum dieses Projekt existiert

TVHeadend und die Sundtek-Firmware sind beide für **einen festen
Empfangsstandort** gebaut - Sender werden einmal gesucht und bleiben dann
so, wie sie sind. Für ein reisendes Fahrzeug ist das unpraktisch: an jedem
neuen Ort muss man eigentlich manuell im Sundtek-Web-UI bzw. in TVHeadends
DVB-Eingängen einen neuen Scan starten und die Ergebnisse übernehmen. Die
drei Bridges in diesem Projekt automatisieren genau das - per Knopfdruck
(DAB+ zusätzlich auch GPS-getriggert) im Home-Assistant-Dashboard, ohne
Browser-Umweg über die jeweilige Web-UI.

DAB+ und FM teilen sich denselben physischen Sundtek-Tuner (exklusiv - nur
einer von beiden kann gleichzeitig aktiv sein); DVB-T läuft auf einer
komplett separaten Tuner-Karte.

## Struktur

```
bridge/                Die drei Python-Bridges + systemd-Units + Beispiel-Configs
  dab_bridge2.py          DAB+ Scan-Automatisierung, Cover-Art/DLS, M3U fuer TVHeadend (Port 8091)
  fm_bridge2.py           FM Scan-Automatisierung, RDS, M3U fuer TVHeadend (Port 8093)
  dvbt_bridge.py          DVB-T Standort-Netzwerk-Automatisierung (Port 8082)
  tuner_watchdog.py       Tuner-Watchdog: Gesundheitspruefung, Auto-Wiederherstellung, HA-API (Port 8094)
  local_ui_proxy.py       Offline-Picon-Mirror fuer das Sundtek-Web-UI (Port 80)
  bridge-*.service         systemd-Units (LibreELEC-Variante, /storage/.config/system.d/)
  *.txt.example            Beispiel-Konfiguration (Exclude-/Rename-Listen)

homeassistant/         Home-Assistant-Packages + Dashboard-Auszug
  packages/dab_scan.yaml    DAB+ Steuerung (rest_command, Sensoren, Scripts, GPS-Automation,
                             Sender ausschliessen/umbenennen per Dropdown)
  packages/fm_scan.yaml     FM Steuerung (analog, ohne GPS-Automation)
  packages/dvbt_scan.yaml   DVB-T Steuerung (analog, inkl. Sender ausschliessen/umbenennen)
  packages/standort_input_snippet.yaml  gemeinsames Ortsnamen-Eingabefeld
  dashboard/dashboard_media_tab.yaml  Lovelace-Karten-Auszug

docs/
  ARCHITECTURE.md         Gesamtarchitektur, Ablauf aller drei Pipelines
  API.md                  Alle HTTP-Endpunkte aller drei Bridges
  TVHEADEND_QUIRKS.md      Alle TVHeadend-/Sundtek-Eigenheiten, die umgangen werden
  SUNDTEK_INSTALL.md       Wie der Sundtek-Treiber/-Server auf LibreELEC installiert wurde
```

## Voraussetzungen

- LibreELEC-Box mit TVHeadend-Addon (`service.tvheadend43` o.ä.)
- Sundtek DAB+/FM/FM-HD USB-Stick
- Ein DVB-T/T2-fähiger Tuner, von TVHeadend nativ unterstützt (hier:
  TurboSight TBS 5520SE)
- Home Assistant im selben Netz
- Python 3 auf der LibreELEC-Box (normalerweise vorhanden)

## Setup-Reihenfolge

1. Sundtek-Treiber installieren - siehe
   [docs/SUNDTEK_INSTALL.md](docs/SUNDTEK_INSTALL.md)
2. TVHeadend: die IPTV-Auto-Netzwerke "DAB+" und "FM" sowie mindestens ein
   DVB-T-Netzwerk einmal manuell über die Web-UI anlegen (siehe
   [docs/TVHEADEND_QUIRKS.md](docs/TVHEADEND_QUIRKS.md) Punkt 13 für die
   Netzwerk-Einstellungen, auf die es dabei ankommt) und einen initialen
   Scan durchführen - die Bridges automatisieren nur spätere/weitere
   Standorte, nicht die Ersteinrichtung von TVHeadend selbst
3. `bridge/*.py`, `bridge/*.service` und die `.txt.example`-Dateien (als
   `.txt` ohne `.example`, angepasst) nach `/storage/dab-tvh-bridge/` auf
   die Box kopieren
4. Die IP-Adressen/Pfade in den `.service`-Dateien und in den
   HA-Packages an deine Umgebung anpassen (in diesem Repo überall als
   `192.168.0.200` platzgehalten). **Sicherheitshinweis**: Die TVHeadend-Zugangsdaten stehen nicht im Code, sondern in `/storage/dab-tvh-bridge/credentials.env` auf der Box (Vorlage: `bridge/credentials.env.example`, `chmod 600`, nicht im Repo). Ohne diese Datei laufen die Bridges mit den Platzhaltern `claude`/`claude`. Vorher einen eigenen TVHeadend-Benutzer anlegen (Configuration -> Users -> Access Entries). Alle drei Bridges selbst
   laufen komplett ungesichert (kein Passwort) - nur für ein
   vertrauenswürdiges privates LAN gedacht, nicht für den Einsatz in einem
   größeren/offenen Netz.
5. Services aktivieren:
   ```sh
   systemctl daemon-reload
   systemctl enable --now bridge-dab.service bridge-fm.service bridge-dvbt.service tuner-watchdog.service
   ```
6. HA-Packages nach `<config>/packages/` kopieren, `packages:
   !include_dir_named packages` in `configuration.yaml` sicherstellen,
   HA neu starten
7. Dashboard-Karten aus `homeassistant/dashboard/dashboard_media_tab.yaml`
   übernehmen

Details, Ablauf und alle Stolpersteine unterwegs: siehe
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) und
[docs/TVHEADEND_QUIRKS.md](docs/TVHEADEND_QUIRKS.md) - letzteres lohnt
sich zu lesen, bevor man eigene Anpassungen an der TVHeadend-API-Nutzung
vornimmt, da mehrere Dinge dort (z.B. `scanfile`, sofortiges
`scan_state`-Setzen, die genauen `iptv_auto_network`-Feldwerte) einfach
nicht wie naiv erwartet funktionieren.

## Status / bekannte offene Punkte

- DVB-T-Standort-Scan braucht gelegentlich länger als das eingebaute
  4-Minuten-Zeitfenster für das "Mux tatsächlich in die Scan-Warteschlange
  bekommen" (siehe [TVHEADEND_QUIRKS.md](docs/TVHEADEND_QUIRKS.md) Punkt
  5) - in seltenen Fällen bisher noch manuelles Nachsetzen von
  `scan_state=3` nötig.
- Kein automatischer, GPS-getriggerter Rescan für FM oder DVB-T (nur
  DAB+) - bei FM wäre der Konflikt mit einem laufenden DAB+-Scan (gleicher
  physischer Tuner) zu berücksichtigen, bei DVB-T dauert die Suche zu
  lange, um sie ungefragt bei jedem Stopp auszulösen.
- Dynamic-Label-Songtitel (DLS) bei DAB+ ist im Code implementiert, aber
  bisher bei keinem getesteten Sender tatsächlich mit echten Daten
  beobachtet worden (siehe [SUNDTEK_INSTALL.md](docs/SUNDTEK_INSTALL.md)
  Abschnitt 4.3).
- `/storage/picons/` (TVHeadend-Kanal-Icons) - Herkunft/Pflegeprozess
  nicht dokumentiert, da nicht mehr nachvollziehbar.
