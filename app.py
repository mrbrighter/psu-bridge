#!/usr/bin/env python3
from __future__ import annotations

import os, json, time, logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional, List
import fcntl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from flask import Flask, request, jsonify, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# -------------------- Konfiguration --------------------
PSU_BASE      = os.getenv("PSU_BASE", "http://192.168.4.1")
HTTP_TIMEOUT  = float(os.getenv("HTTP_TIMEOUT", "5.0"))
API_TOKEN     = os.getenv("API_TOKEN", "")
STATE_FILE    = Path(os.getenv("STATE_FILE", "/var/lib/psu-bridge/state.json"))
PORT          = int(os.getenv("PORT", "8000"))

# Sicherheitsgrenzen (bei Bedarf an Gerät anpassen)
VOLT_MIN, VOLT_MAX = 0.0, 100.0
CURR_MIN, CURR_MAX = 0.0, 50.0

# Pflichtfeld (vom Gerät gefordert) – konservativer Default
BALANCED_AMP_ENV = float(os.getenv("BALANCED_AMP", "1.0"))

# -------------------- Flask & Limiter --------------------
app = Flask(__name__)
limiter = Limiter(key_func=get_remote_address)
limiter.init_app(app)
_limit = limiter.limit  # Alias

# -------------------- Logging --------------------
app_logger = logging.getLogger("psu_bridge")
app_logger.setLevel(logging.INFO)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
app_logger.addHandler(stream_handler)

# State-Verzeichnis + File-Logger
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
file_handler = RotatingFileHandler(STATE_FILE.parent / "psu.log", maxBytes=1_000_000, backupCount=3)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
app_logger.addHandler(file_handler)

# -------------------- HTTP Session (Reuse + Pool) --------------------
session = requests.Session()
# Keine urllib3-internen Retries -> wir steuern Retries mit tenacity
adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(total=0, redirect=0, connect=0, read=0))
session.mount("http://", adapter)
session.mount("https://", adapter)
session.headers.update({"Connection": "keep-alive"})

# -------------------- State (persist + Cache) --------------------
_state_cache: Dict[str, Any] = {}
_cache_time: float = 0.0

def _clamp_balanced_amp(v: float) -> float:
    return max(1.0, min(5.0, v))

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def load_state() -> Dict[str, Any]:
    """Thread-safe Laden mit 1s Cache zur I/O-Reduktion."""
    global _state_cache, _cache_time
    now = time.time()
    if (now - _cache_time) < 1.0 and _state_cache:
        return _state_cache.copy()

    if not STATE_FILE.exists():
        state = {"voltage": None, "max_current": None, "access": "0", "updated_at": None}
        save_state(state)
        _state_cache = state
        _cache_time = now
        return state.copy()

    with STATE_FILE.open("r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            data = json.load(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # Minimal-Validierung
    for k in ("voltage", "max_current", "access", "updated_at"):
        data.setdefault(k, None if k != "access" else "0")

    _state_cache = data
    _cache_time = now
    return data.copy()

def save_state(state: Dict[str, Any]) -> None:
    """Thread-safe Speichern + Cache aktualisieren."""
    tmp = state.copy()
    tmp["updated_at"] = _now_iso()
    with STATE_FILE.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(tmp, f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    # Cache aktualisieren
    global _state_cache, _cache_time
    _state_cache = tmp
    _cache_time = time.time()

# -------------------- Helpers --------------------
def require_token() -> None:
    if API_TOKEN and request.headers.get("X-Api-Key") != API_TOKEN:
        abort(401, description="Unauthorized")

def validate_params(voltage: float, max_current: float) -> None:
    if not (VOLT_MIN < voltage <= VOLT_MAX):
        raise ValueError(f"Voltage {voltage} V außerhalb sicherer Grenzen ({VOLT_MIN}, {VOLT_MAX}]")
    if not (CURR_MIN < max_current <= CURR_MAX):
        raise ValueError(f"Current {max_current} A außerhalb sicherer Grenzen ({CURR_MIN}, {CURR_MAX}]")

def payload_for_device(voltage: float, max_current: float, access: str) -> Dict[str, Any]:
    """
    Baut das JSON für /api/send_data. Das Gerät erwartet:
      - voltageValue, currentValue
      - accessibilityStatus (0..3)
      - balancedVoltage in [voltage-5, voltage]
      - balancedCurrent in [1..5]
      - mode "2"
    """
    balanced_amp = _clamp_balanced_amp(BALANCED_AMP_ENV)
    return {
        "voltageValue": f"{voltage:.1f}",
        "currentValue": f"{max_current:.2f}",
        "accessibilityStatus": str(access or "0"),
        "balancedVoltage": f"{voltage:.1f}",
        "balancedCurrent": f"{balanced_amp:.1f}",
        "mode": "2",
    }

def _parse_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        # evtl. Strings mit ' V'/' A' säubern
        if isinstance(v, str):
            for suf in (" V", "V", " A", "A"):
                if v.endswith(suf):
                    try:
                        return float(v.replace(suf, ""))
                    except Exception:
                        pass
    return None

# -------------------- PSU HTTP (mit Retries) --------------------
@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout))
)
def psu_get(path: str) -> Dict[str, Any]:
    r = session.get(f"{PSU_BASE}{path}", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout))
)
def psu_post(path: str, payload: Dict[str, Any]) -> str:
    r = session.post(f"{PSU_BASE}{path}", json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text

# -------------------- Routes --------------------
@app.get("/health")
def health():
    psu_ok = False
    last_comm = load_state().get("updated_at")
    try:
        # schneller Reachability-Check
        psu_get("/api/chargeStatus")
        psu_ok = True
    except Exception as e:
        app_logger.warning(f"Health PSU check failed: {e}")

    return jsonify({
        "bridge_ok": True,
        "psu_reachable": psu_ok,
        "psu_base": PSU_BASE,
        "state_file": str(STATE_FILE),
        "last_communication": last_comm,
        "version": "1.5.0",
        "features": {"retry": True, "rate_limit": True}
    })

@app.get("/psu/status")
def psu_status():
    require_token()
    try:
        return jsonify(psu_get("/api/chargeStatus"))
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "PSU nicht erreichbar"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "PSU Timeout"}), 504
    except Exception as e:
        app_logger.error(f"/psu/status error: {e}")
        return jsonify({"error": str(e)}), 502

@app.get("/psu/current")
def psu_current():
    require_token()
    state = load_state()
    set_max_current = state.get("max_current")
    try:
        js = psu_get("/api/chargeStatus")
        current_now = _parse_float(js.get("currentNow"))
        voltage_now = _parse_float(js.get("voltageNow"))
        # Fallbacks auf evtl. alternative Keys
        if current_now is None:
            current_now = _parse_float(js.get("current_now"))
        if voltage_now is None:
            voltage_now = _parse_float(js.get("voltage_now"))

        return jsonify({
            "current_now": f"{(current_now or 0.0):.2f}",
            "voltage_now": f"{(voltage_now or 0.0):.2f}",
            "set_max_current": set_max_current
        })
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "PSU nicht erreichbar"}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "PSU Timeout"}), 504
    except Exception as e:
        app_logger.error(f"/psu/current error: {e}")
        return jsonify({"error": str(e), "set_max_current": set_max_current}), 502

@app.get("/psu/last_set")
def last_set():
    require_token()
    return jsonify(load_state())

@app.post("/set")
@_limit("10 per minute")
def set_voltage_current():
    """
    Setzt Spannung und Stromlimit.
    JSON: { "voltage": <float>, "max_current": <float>, "access": <0|1|2|3> }
    Query: ?voltage=..&max_current=..&access=..
    """
    require_token()
    data = request.get_json(silent=True) or {}
    v_str = request.args.get("voltage") or data.get("voltage")
    i_str = request.args.get("max_current") or data.get("max_current")
    access = str(request.args.get("access") or data.get("access") or "0")

    if v_str is None or i_str is None:
        return jsonify({"error": "Provide 'voltage' and 'max_current'."}), 400

    try:
        voltage = float(v_str)
        max_current = float(i_str)
        validate_params(voltage, max_current)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    payload = payload_for_device(voltage, max_current, access)
    app_logger.info(f"Setting PSU: V={voltage}V, Imax={max_current}A, access={access}")

    try:
        resp_text = psu_post("/api/send_data", payload)
        # Persistierter Soll-State
        save_state({"voltage": voltage, "max_current": max_current, "access": access})
        return jsonify({"sent": payload, "psu_response": resp_text, "state": load_state()})
    except requests.exceptions.ConnectionError:
        app_logger.error("PSU communication failed: connection error")
        return jsonify({"error": "PSU nicht erreichbar", "sent": payload}), 503
    except requests.exceptions.Timeout:
        app_logger.error("PSU communication failed: timeout")
        return jsonify({"error": "PSU Timeout", "sent": payload}), 504
    except Exception as e:
        app_logger.error(f"PSU communication failed: {e}")
        return jsonify({"error": str(e), "sent": payload}), 502

# Alias, damit alte Clients weiter funktionieren
@app.post("/set_vc")
def set_vc_alias():
    return set_voltage_current()

@app.post("/set_sequence")
@_limit("5 per minute")
def set_sequence():
    """
    Führt mehrere Set-Schritte nacheinander aus.
    JSON:
      {
        "sequence": [
          { "voltage": 54.0, "max_current": 6.0, "access": 1, "delay": 3 },
          { "voltage": 54.0, "max_current": 10.0, "delay": 5 }
        ]
      }
    """
    require_token()
    body = request.get_json(silent=True) or {}
    seq: List[Dict[str, Any]] = body.get("sequence") or []
    if not isinstance(seq, list):
        return jsonify({"error": "sequence must be a list"}), 400
    if len(seq) > 10:
        return jsonify({"error": "Max 10 steps allowed"}), 400

    results = []
    for idx, step in enumerate(seq, start=1):
        try:
            v = float(step["voltage"])
            i = float(step["max_current"])
            a = str(step.get("access", "0"))
            validate_params(v, i)
            payload = payload_for_device(v, i, a)
            app_logger.info(f"[seq {idx}/{len(seq)}] Setting PSU: V={v}V, Imax={i}A, access={a}")
            psu_resp = psu_post("/api/send_data", payload)
            save_state({"voltage": v, "max_current": i, "access": a})
            results.append({"step": idx, "sent": payload, "psu_response": psu_resp, "ok": True})
        except Exception as e:
            app_logger.error(f"[seq {idx}] failed: {e}")
            results.append({"step": idx, "error": str(e), "ok": False})
            # optional: abbrechen
            break

        delay = float(step.get("delay", 0))
        if delay > 0:
            time.sleep(min(delay, 10))  # maximal 10s pro Schritt

    return jsonify({"results": results, "state": load_state()})

# -------------------- Main --------------------
if __name__ == "__main__":
    # Hinweis: Kein SocketIO, daher keine Werkzeug-Production-Warnung.
    app.run(host="0.0.0.0", port=PORT)
