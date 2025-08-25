# PSU REST Bridge (Raspberry Pi → Wi‑Fi‑PSU)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-2.3%2B-green.svg)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](#lizenz)
[![Status](https://img.shields.io/badge/Status-Production-brightgreen.svg)](#)

Eine kleine Bridge, die ein **Netzteil mit eigenem WLAN-Access-Point** (z. B. HEXA/Benfu) über einen **Raspberry Pi** ins LAN bringt.  
Du sendest einfache HTTP‑Requests an den Pi, die Bridge spricht intern mit der PSU‑Web‑API.  
Typische Anwendung: **Spannung setzen** und **maximalen Ausgangsstrom begrenzen**, Live‑Werte abfragen, **Home‑Assistant‑Integration**.

> Alle Spannungs-/Stromwerte in Beispielen sind **nur Beispiele** – wähle selbst sinnvolle Grenzen für dein Gerät.

---

## Inhalt
- [Architektur](#architektur)
- [Features](#features)
- [Voraussetzungen](#voraussetzungen)
- [Installieren](#installieren)
- [Konfiguration (ENV)](#konfiguration-env)
- [Systemd‑Service](#systemd-service)
- [WLAN zum PSU‑AP (NetworkManager)](#wlan-zum-psu-ap-networkmanager)
- [Healthcheck & Tests](#healthcheck--tests)
- [REST‑API der Bridge](#rest-api-der-bridge)
- [Home‑Assistant‑Integration](#home-assistant-integration)
- [Sicherheit](#sicherheit)
- [Monitoring & Logs](#monitoring--logs)
- [Troubleshooting](#troubleshooting)
- [Performance‑Hinweise](#performance-hinweise)
- [Contributing](#contributing)
- [Lizenz](#lizenz)

---

## Architektur

```
[ Home Assistant / Clients ]  <--LAN-->  [ Raspberry Pi ]
                                          |  REST Bridge (Flask)
                                          |  eth0: LAN‑IP (z. B. 172.16.8.65)
                                          |  wlan0: verbindet sich zum PSU‑AP
                                          v
                                     [ PSU Access Point ]
                                     IP z. B. 192.168.4.1, HTTP‑API
```

- Pi bietet im LAN eine REST‑API (HTTP/JSON).
- Pi verbindet sich per **wlan0** zum **PSU‑AP** und ruft die Hersteller‑API auf.
- Die Bridge speichert die zuletzt gesetzten Sollwerte lokal (JSON‑Statefile).

---

## Features

- **🔐 Sichere API** – optionaler Token‑Header `X-Api-Key`
- **⚡ Live‑Abfrage** – REST‑Endpunkte für aktuelle Spannung/Strom
- **🛡️ Safety First** – Validierung von Spannungs-/Strombereichen
- **🔄 Retry‑Logic** – automatische Wiederholungen bei Netzfehlern (Tenacity)
- **📊 Rate Limiting** – Schutz vor Missbrauch (Flask‑Limiter)
- **💾 State‑Management** – persistiert letzte Sollwerte (thread‑safe)
- **🧰 Production‑ready** – systemd‑Service, Logging, Health Checks

---

## Voraussetzungen

- Raspberry Pi (Linux, systemd, NetworkManager empfohlen)
- Python **3.10+**
- Netzteil mit Web‑UI/HTTP‑API (z. B. `/api/send_data`, `/api/chargeStatus`)
- **Tools:** `curl`, **`jq`** (für formatiertes JSON in Beispielen)

Installiere Basis‑Pakete :

```bash
sudo apt update
sudo apt install -y python3-venv network-manager curl jq
```

---

## Installieren

```bash
# 1) Projektverzeichnis
mkdir -p ~/psu-bridge && cd ~/psu-bridge

# 2) Virtuelle Umgebung
python3 -m venv .venv
source .venv/bin/activate

# 3) Abhängigkeiten
cat > requirements.txt <<'EOF'
Flask
requests
tenacity
flask-limiter
EOF
pip install -r requirements.txt

# 4) App-Datei
# --> Lege hier deine app.py ab (siehe Repo-Code)
```


---

## Konfiguration (ENV)

Die Bridge liest Konfiguration aus Umgebungsvariablen:

| Variable       | Default                          | Beschreibung |
|----------------|----------------------------------|--------------|
| `PSU_BASE`     | `http://192.168.4.1`             | Basis‑URL der PSU (im AP‑Netz) |
| `HTTP_TIMEOUT` | `5.0`                            | HTTP‑Timeout (Sekunden) |
| `API_TOKEN`    | *(leer)*                         | Optionaler Schutz; Header `X-Api-Key` nötig wenn gesetzt |
| `STATE_FILE`   | `/var/lib/psu-bridge/state.json` | Pfad für persistente Sollwerte |
| `BALANCED_AMP` | `1.0` (geclamped 1.0–5.0)        | Minimalwert für Pflichtfeld „balancedCurrent“ |

**Sicherheitsgrenzen (im Code anpassbar):**
```python
VOLT_MIN, VOLT_MAX = 0.0, 100.0   # Spannungsbereich
CURR_MIN, CURR_MAX = 0.0, 50.0    # Strombereich
```

---

## Systemd‑Service

`/etc/systemd/system/psu-bridge.service`:
```ini
[Unit]
Description=PSU REST Bridge
After=network-online.target
Wants=network-online.target

[Service]
User=raspberry
WorkingDirectory=/home/raspberry/psu-bridge
Environment=PSU_BASE=http://192.168.4.1
Environment=STATE_FILE=/var/lib/psu-bridge/state.json
ExecStart=/home/raspberry/psu-bridge/.venv/bin/python /home/raspberry/psu-bridge/app.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

State‑Verzeichnis anlegen & Service starten:
```bash
sudo install -d -o raspberry -g raspberry /var/lib/psu-bridge
sudo systemctl daemon-reload
sudo systemctl enable --now psu-bridge
journalctl -u psu-bridge -n 50 --no-pager
```

---

## WLAN zum PSU‑AP (NetworkManager)

AP‑Profil anlegen (Beispiel):
```bash
sudo nmcli dev wifi rescan
sudo nmcli dev wifi connect "BF_Tech_XXXXXX" password "DEIN_PASS" name psu-ap

# Autoconnect, keine Default-Route, IPv6 aus
sudo nmcli con modify psu-ap connection.autoconnect yes
sudo nmcli con modify psu-ap ipv4.never-default yes
sudo nmcli con modify psu-ap ipv6.method ignore

# WLAN-Powersave dauerhaft aus
echo -e "[connection]
wifi.powersave=2" | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf
sudo systemctl restart NetworkManager
```

Schnellchecks:
```bash
iw dev wlan0 link
ip -4 addr show wlan0
ip route get 192.168.4.1
curl --interface wlan0 -sS http://192.168.4.1/api/chargeStatus | jq .
curl -sS http://127.0.0.1:8000/health | jq .
```

**Optionaler Watchdog** (Reconnect alle 60 s)
```bash
sudo tee /usr/local/bin/psu-wifi-watchdog.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -e
SSID="psu-ap"
AP_IP="192.168.4.1"
if ! ping -I wlan0 -c2 -W2 "$AP_IP" >/dev/null 2>&1; then
  nmcli dev disconnect wlan0 || true
  nmcli con up "$SSID" || true
fi
EOF
sudo chmod +x /usr/local/bin/psu-wifi-watchdog.sh

sudo tee /etc/systemd/system/psu-wifi-watchdog.timer >/dev/null <<'EOF'
[Unit]
Description=PSU WiFi Watchdog Timer
[Timer]
OnBootSec=15
OnUnitActiveSec=60
Unit=psu-wifi-watchdog.service
[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/psu-wifi-watchdog.service >/dev/null <<'EOF'
[Unit]
Description=PSU WiFi Watchdog
[Service]
Type=oneshot
ExecStart=/usr/local/bin/psu-wifi-watchdog.sh
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now psu-wifi-watchdog.timer
```

---

## Healthcheck & Tests

```bash
# Bridge-Health (zeigt u. a. PATH zum Statefile)
curl -sS http://<PI-LAN-IP>:8000/health | jq .

# Live-Werte & gesetztes Limit (aus Statefile)
curl -sS http://<PI-LAN-IP>:8000/psu/current | jq .

# Rohstatus der PSU (durchgereicht)
curl -sS http://<PI-LAN-IP>:8000/psu/status | jq .
```

---

## REST‑API der Bridge

### `GET /health`
**200 OK**
```json
{
  "bridge_ok": true,
  "psu_reachable": true,
  "psu_base": "http://192.168.4.1",
  "state_file": "/var/lib/psu-bridge/state.json",
  "last_communication": "2025-08-25T06:30:00Z",
  "version": "1.4.1",
  "features": { "retry": true, "rate_limit": true, "websocket": false }
}
```

### `GET /psu/status`
- Reicht das JSON der PSU durch (`/api/chargeStatus`).
- Fehler: `503` (PSU nicht erreichbar), `504` (Timeout), `502/500` entsprechend.

### `GET /psu/current`
Antwort:
```json
{
  "current_now": "…",      // live aus PSU
  "voltage_now": "…",      // live aus PSU
  "set_max_current": 12.0  // aus lokalem Statefile
}
```

### `POST /set`  *(Haupt‑Endpoint)*
Setzt **Spannung** (`voltage`) und **Stromlimit** (`max_current`) an der PSU. Optional `access` (z. B. `"1"` = Force Activation).

**Beispiele:**
```bash
curl -X POST http://<PI-LAN-IP>:8000/set   -H "Content-Type: application/json"   -d '{"voltage": 54.0, "max_current": 8.0, "access": 1}'

# Query-Variante
curl -X POST "http://<PI-LAN-IP>:8000/set?voltage=54.0&max_current=8.0&access=1"
```

- Validierung: Spannung `(0, 100]`, Strom `(0, 50]` (im Code konfigurierbar).  
- Erfolgsantwort enthält das gesendete Payload sowie den neuen State.

### `POST /set_vc`
- Alias zu `/set`.

### `GET /psu/last_set`
- Letzter persistierter Soll‑State (aus Statefile).

### `POST /set_sequence` *(optional)*
Mehrere Set‑Schritte nacheinander:
```json
{
  "sequence": [
    { "voltage": 54.0, "max_current": 6, "access": 1, "delay": 3 },
    { "voltage": 54.0, "max_current": 10, "delay": 5 }
  ]
}
```

> Hinweis: Die PSU‑API erwartet Felder wie `balancedVoltage`, `balancedCurrent`, `accessibilityStatus`, `mode`. Die Bridge füllt sie automatisch minimal/konservativ.


## Home‑Assistant‑Integration

> In `configuration.yaml` dürfen `rest:`, `rest_command:` und `automation:` jeweils **nur einmal** vorkommen. Hänge neue Einträge an bestehende Sektionen an oder nutze `automations.yaml`.

**1) Eingaben (Slider) & Schreiben**
```yaml
input_number:
  psu_voltage:
    name: PSU Voltage
    min: 1
    max: 100
    step: 0.1
    unit_of_measurement: "V"
    initial: 54.0
    icon: mdi:power-plug
  psu_max_current:
    name: PSU Max Current
    min: 0
    max: 50
    step: 0.1
    unit_of_measurement: "A"
    initial: 6.0
    icon: mdi:current-dc

rest_command:
  psu_set:
    url: "http://<PI-LAN-IP>:8000/set"
    method: post
    content_type: "application/json"
    # headers:
    #   X-Api-Key: !secret psu_api_token
    payload: >
      {
        "voltage": {{ states('input_number.psu_voltage') | float }},
        "max_current": {{ states('input_number.psu_max_current') | float }},
        "access": 1
      }
```

**2) Live‑Werte lesen**
```yaml
rest:
  - resource: http://<PI-LAN-IP>:8000/psu/current
    method: GET
    timeout: 5
    scan_interval: 3
    sensor:
      - name: "PSU Current Now"
        unit_of_measurement: "A"
        value_template: "{{ value_json.current_now | float(0) }}"
      - name: "PSU Voltage Now"
        unit_of_measurement: "V"
        value_template: "{{ value_json.voltage_now | float(0) }}"
      - name: "PSU Set Max Current"
        unit_of_measurement: "A"
        value_template: "{{ value_json.set_max_current | float(0) }}"
```

**3) Bridge‑Online‑Check (optional)**
```yaml
binary_sensor:
  - platform: rest
    name: PSU Bridge Online
    device_class: connectivity
    resource: http://<PI-LAN-IP>:8000/health
    value_template: "{{ value_json.bridge_ok is true }}"
    scan_interval: 10
```

**4) Automation – Werte anwenden** (in `automations.yaml`):
```yaml
- id: psu_apply_on_change
  alias: "PSU – Apply settings on change"
  mode: restart
  trigger:
    - platform: state
      entity_id:
        - input_number.psu_voltage
        - input_number.psu_max_current
  condition:
    - condition: state
      entity_id: binary_sensor.psu_bridge_online
      state: "on"
  action:
    - service: rest_command.psu_set
```

**5) Lovelace‑Karte**
```yaml
type: entities
title: PSU
entities:
  - entity: input_number.psu_voltage
  - entity: input_number.psu_max_current
  - entity: sensor.psu_set_max_current
  - entity: sensor.psu_voltage_now
  - entity: sensor.psu_current_now
  - entity: binary_sensor.psu_bridge_online
```

---

## Sicherheit

- Setze ein **API‑Token** für die Bridge: `API_TOKEN="dein-token"`  
  In HA:  
  ```yaml
  headers:
    X-Api-Key: !secret psu_api_token
  ```
- Hersteller‑UIs schützen „Aux“-Bereiche oft nur clientseitig. Verlasse dich nicht darauf.

---

## Monitoring & Logs

```bash
# Systemd Logs
journalctl -u psu-bridge.service -f

# Bridge-Health
curl -sS http://<PI-LAN-IP>:8000/health | jq .
```

---

## Troubleshooting

- **PermissionError beim Statefile**  
  ```bash
  sudo install -d -o raspberry -g raspberry /var/lib/psu-bridge
  ```

- **Bridge läuft, PSU nicht erreichbar**  
  `iw dev wlan0 link`, `ip a show wlan0`, `ip route get 192.168.4.1`,  
  `ping -I wlan0 192.168.4.1`, ggf. `nmcli con up psu-ap`  
  → Watchdog aktivieren

- **Port 8000 belegt**  
  `sudo ss -lptn 'sport = :8000'` → Prozess beenden/Unit anpassen

- **Browser erzeugt 400er im Log**  
  HTTPS‑Only versucht TLS auf HTTP‑Port → unkritisch

- **Home‑Assistant YAML „duplicate key“**  
  `rest:`, `rest_command:`, `automation:` nur einmal top‑level. Neue Einträge anhängen bzw. `automations.yaml` nutzen.

---

## Performance‑Hinweise

- **HTTP‑Session Reuse**: Connection‑Pooling
- **State‑Caching**: reduziert File‑I/O
- **Retry‑Logic**: Exponential Backoff bei Netzfehlern
- **Rate‑Limit**: Default 10/min für `/set` (konfigurierbar)

---

## Contributing

1. Fork
2. Branch (`feature/<name>`)
3. Commits & PR

---

## Lizenz

MIT – nutze und erweitere frei, ohne Gewähr.  
Prüfe Sicherheitsgrenzen für **dein** Netzteil und **deine** Anwendung.
