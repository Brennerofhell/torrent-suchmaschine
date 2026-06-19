# Torrent Suchmaschine

Privacy-first Torrent-Suchmaschine mit Desktop-GUI (PyQt6) und CLI. Sucht parallel über mehrere Quellen, unterstützt Proxy/DNS-over-HTTPS und sendet Magnets direkt an qBittorrent oder Transmission.

## Features

- **Quellen:** ApiBay, Nyaa, RSS-Feeds, Web-Scraping (eztv, 1337x, tgx, limetorrents, btdigg, torrentproject), passiver DHT-Sniffer
- **Privacy:** SOCKS5/HTTP-Proxy mit DNS-Leak-Schutz, DNS-over-HTTPS (Cloudflare), User-Agent-Rotation, zufällige Delays
- **Filter:** Seeds (min/max), Dateigröße (MB/GB), Quelle — auch als Inline-Syntax direkt in der Suche
- **Watchlist & Favoriten:** gespeichert in SQLite, RSS-Auto-Refresh im Hintergrund
- **Torrent-Client:** qBittorrent REST-API und Transmission RPC
- **Export:** JSON und CSV im Batch-Modus

## Installation

**Voraussetzungen:** Python 3.10+

```bash
pip install -r requirements.txt
```

Für reine CLI-Nutzung ohne GUI reicht:

```bash
pip install requests
# Optional:
pip install beautifulsoup4 feedparser psutil
```

## Verwendung

### GUI

```bash
python gui.py
```

### CLI (interaktiv)

```bash
python search_engine.py
```

Suche direkt eingeben. Inline-Filter-Syntax:

```
ubuntu seeds>50 size<2GB source=apibay
```

### Batch-Modus (nicht-interaktiv)

```bash
# JSON-Ausgabe
python search_engine.py --query "ubuntu 24.04" --json

# CSV-Ausgabe
python search_engine.py --query "ubuntu 24.04" --csv
```

### CLI-Optionen

| Flag | Beschreibung |
|------|-------------|
| `--proxy URL` | SOCKS5- oder HTTP-Proxy (z.B. `socks5://127.0.0.1:9050`) |
| `--doh` | DNS-over-HTTPS via Cloudflare aktivieren |
| `--no-delay` | Zufällige Delays zwischen Requests deaktivieren |
| `--dht-sec N` | DHT-Sniffer für N Sekunden laufen lassen (0 = deaktiviert) |
| `--top-k K` | Maximale Ergebnisanzahl |
| `--query TEXT` | Nicht-interaktive Suche (kombinierbar mit `--json`/`--csv`) |
| `--json` | Ergebnisse als JSON ausgeben (benötigt `--query`) |
| `--csv` | Ergebnisse als CSV ausgeben (benötigt `--query`) |

## Torrent-Client einrichten

### qBittorrent

1. qBittorrent → Einstellungen → Web-UI aktivieren
2. In den App-Einstellungen: URL `http://localhost:8080`, Benutzername/Passwort eintragen

### Transmission

1. Transmission → Einstellungen → Remote-Zugriff aktivieren
2. In den App-Einstellungen: URL `http://localhost:9091/transmission/rpc`

## Datenspeicherung

SQLite-Datenbank unter `~/.torrent_search.db` mit drei Tabellen:

- `searches` — Suchverlauf
- `results` — Cache der Ergebnisse (TTL 1 Stunde)
- `favorites` — Watchlist/Favoriten

## Tests

```bash
pytest tests/
```

## Lizenz

MIT — siehe [LICENSE](LICENSE)
