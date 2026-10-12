# Weishaupt Heizung – Home Assistant Integration

Custom Integration für Weishaupt-Heizungen mit lokaler `parameter.json`-Schnittstelle
(Protokoll `coco`). Die Werte werden lokal per HTTP abgefragt, es wird keine Cloud benötigt.

Portierung eines früheren C#-Dienstes (RabbitMQ/LiteDB) auf eine native Home-Assistant-Integration.

## Installation

### Manuell

1. Ordner `custom_components/weishaupt` nach `<config>/custom_components/weishaupt` kopieren.
2. Home Assistant neu starten.
3. **Einstellungen → Geräte & Dienste → Integration hinzufügen → Weishaupt Heizung**.

### HACS (Custom Repository)

1. HACS → ⋮ → *Benutzerdefinierte Repositories*.
2. Repository `https://github.com/ThomasKeller/HomeAssistant-Weishaupt` eintragen, Kategorie *Integration*.
3. *Weishaupt Heizung* installieren und Home Assistant neu starten.
4. **Einstellungen → Geräte & Dienste → Integration hinzufügen → Weishaupt Heizung**.

## Konfiguration

| Feld | Beschreibung | Standard |
|---|---|---|
| IP-Adresse / Hostname | Adresse des Weishaupt-Moduls, z. B. `192.168.1.50` | – |
| Abfrageintervall | Sekunden zwischen zwei Abfragen (10–3600) | 60 |

Das Intervall lässt sich nachträglich über *Konfigurieren* an der Integration ändern.
Beim Einrichten wird einmalig `http://<host>/parameter.json` abgefragt, um die Verbindung zu prüfen.

## Entitäten

| Entität | Index | Faktor | Einheit | Hinweis |
|---|---|---|---|---|
| Außentemperatur | 12 | 0,1 | °C | |
| Gedämpfte Außentemperatur | 2572 | 0,1 | °C | |
| Vorlauftemperatur | 3101 | 0,1 | °C | |
| Abgastemperatur | 325 | 0,1 | °C | |
| Warmwassertemperatur | 14 | 0,1 | °C | |
| Wärmeanforderung | 2 | 0,1 | °C | |
| Raumtemperatur | 17 (Modul 6) | 0,1 | °C | Raumgerät/Fernbedienung |
| Soll-Raumtemperatur | 4 (Modul 6) | 0,1 | °C | Raumgerät/Fernbedienung |
| Laststellung | 138 | 1 | % | |
| Betriebsphase | 373 | 1 | – | Diagnose, Rohwert (das Gerät liefert keine Klartexte) |
| Fehlercode | 1 | 1 | – | Diagnose, 0 = kein Fehler |
| Störung | 1 | – | – | Binärsensor, an wenn Fehlercode ≠ 0 |
| Flamme | 81 | – | – | Binärsensor, Zuordnung aus der Web-UI (`idT0051`) |
| Gasventil 1 / 2 | 1497 / 1498 | – | – | Binärsensoren (`idT05D9`/`idT05DA`) |
| Schaltspiele Brenner | 3158 | 1000 | – | `total_increasing` |
| Betriebsstunden Brenner | 3159 | 100 | h | `total_increasing` |
| Zeit seit letzter Wartung | 700 | 10 | h | Diagnose |

Ist ein Fühler nicht angeschlossen, meldet das Gerät `0x8000`. Die Entität zeigt dann *unbekannt*.

## Protokoll

Anfrage per `POST /parameter.json`:

```json
{"prot": "coco", "telegramm": [[10, 0, 1, 12, 0, 0, 0, 0], [10, 0, 1, 14, 0, 0, 0, 0]]}
```

Jede Zeile `[<modul>, 0, 1, <index>, 0, 0, <low>, <high>]` (Modul 10 = Wärmeerzeuger, 6 = Raumgerät) wird in der Antwort mit dem Wert befüllt.
Der Wert ist ein vorzeichenbehafteter 16-Bit-Integer (`high * 256 + low`), skaliert mit dem
Faktor aus der Tabelle oben.

## Historie aus dem Gerät importieren

Der WCM-COM führt einen eigenen Datenlogger (1 Sample/Minute, 8 Kanäle, mehrere Jahre Verlauf).
Der Service `weishaupt.import_history` übernimmt diese Daten **einmalig** als stündliche
Langzeitstatistik (Mittel/Min/Max) in die vorhandenen Sensoren:

| Logger-Kanal | Sensor |
|---|---|
| Soll Temp 1 | Soll-Raumtemperatur |
| Raumtemperatur 1 | Raumtemperatur |
| I34 | Warmwassertemperatur |
| I33 | Außentemperatur |
| I30 (eSTB) | Vorlauftemperatur |
| I13 | Wärmeanforderung |
| I12 | Gedämpfte Außentemperatur |
| I11 | Laststellung |

```yaml
service: weishaupt.import_history
data:
  months: 12   # 1..36, Standard 12
```

Ablauf und Schutzmaßnahmen:

- Läuft im Hintergrund. Das Gerät liefert etwa 150 Samples/s, 12 Monate dauern grob eine Stunde
  (Blöcke zu 1000 Samples mit 2 s Pause). Das Ergebnis erscheint als Benachrichtigung.
  Für einen ersten Test eignet sich `months: 1`.
- **Vorprüfung** (bei Fehler Abbruch ohne Schreibzugriff):
  - letzter Logger-Zeitstempel weicht höchstens 15 min von der aktuellen Zeit ab
    (Gerätezeit wird als Home-Assistant-Zeitzone interpretiert),
  - Außen-, Raum- und gedämpfte Außentemperatur stimmen mit den Live-Werten überein
    (prüft die Kanalzuordnung des Loggers).
- Importiert wird nur **vor** der Stunde, in der der Konfigurationseintrag angelegt wurde.
  Bereits von Home Assistant aufgezeichnete Statistiken werden so nicht überschrieben.
- Die Umstellung von Sommer-/Winterzeit wird berücksichtigt (die doppelte Stunde im Herbst
  wird anhand der Reihenfolge der Samples aufgelöst).
- Der Import lässt sich wiederholen; gleiche Stunden werden dabei überschrieben.
- Betriebsphase, Flamme und Gasventile sind **nicht** im Logger und werden nicht importiert.
  Möchte man die Betriebsphase mitloggen, muss am Gerät unter *Wärmeerzeuger → Prozesswerte*
  die Datalogger-Checkbox bei I10 gesetzt werden (ändert die Gerätekonfiguration).

## Verhalten bei Gerätefehlern

Der Webserver des WCM-COM ist empfindlich. Er antwortet bei Last mit HTTP 200 und einer HTML-Seite
(„server is busy“), Nullbytes, abgeschnittenem JSON oder Telegrammen anderer Clients.
Die Integration:

- sendet pro Abfrage **eine** Anfrage mit allen Werten,
- verwirft ungültige Antworten und wiederholt einmal nach 2 s,
- ordnet Zeilen über (Modul, Index) zu und ignoriert fremde Zeilen,
- behält bei bis zu 3 aufeinanderfolgenden Fehlern die letzten Werte, danach werden die Entitäten „nicht verfügbar“.

Ein offener Browser-Tab mit der Web-UI (z. B. die Grafik-Seite) pollt das Gerät selbst und kann
„busy“-Antworten verursachen.

## Entwicklung

Die Protokoll-Logik (`protocol.py`) hat keine Home-Assistant-Abhängigkeiten und ist getestet:

```bash
python -m pytest tests
```

## Bekannte Einschränkungen

- Negative Temperaturen sind nur per Unit-Test abgesichert, nicht gegen ein Gerät.
- Betriebsphase und Fehlercode sind Rohzahlen: Das Webinterface des Geräts kennt dafür keine Klartexte.
  Eine Zuordnung müsste aus der Weishaupt-Dokumentation ergänzt werden.
- Nur lesender Zugriff, Schreiben von Parametern wird nicht unterstützt.
- Der Historie-Import ist gegen das Gerät als Trockenlauf geprüft, aber noch nicht gegen die
  Recorder-API einer laufenden Home-Assistant-Instanz (Version 2026.10).
