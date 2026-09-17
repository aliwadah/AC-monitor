"""
Local AC control server (one-page website on your own machine).

Provides a web page with an ON/OFF toggle switch for the AC, mirroring the
rules of the GitHub workflow:
    * Turning ON requires battery >= ON_THRESHOLD (default 50%); otherwise refused.
    * Turning OFF always works.
    * Status shows the live battery SOC and the AC's current on/off state.

Endpoints:
    GET  /             -> serves the one-page UI (index.html)
    GET  /api/status   -> {soc, ac_on, thresholds, ...} (live battery + AC read)
    POST /api/turn_on  -> turn AC on (battery check enforced)
    POST /api/turn_off -> turn AC off

Run:  python server.py
Then open:
    on this PC:      http://localhost:8000
    on your phone:   http://<this-PC-LAN-IP>:8000   (same Wi-Fi)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import time

import boto3
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve

# ---------------------------------------------------------------------------
# Config (from .env / environment)
# ---------------------------------------------------------------------------
SISELI_USER = os.getenv("SISELI_USER", "")
SISELI_PASSWORD = os.getenv("SISELI_PASSWORD", "")
SISELI_DEVICE_ID = os.getenv("SISELI_DEVICE_ID", "")

def _num_env(name, default):
    """Parse an env var as a number, falling back to `default` on empty/bad value."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


ON_THRESHOLD = _num_env("ON_THRESHOLD", 50.0)
OFF_THRESHOLD = _num_env("OFF_THRESHOLD", 50.0)
ALERT_THRESHOLD = _num_env("ALERT_THRESHOLD", 30.0)
POLL_SECONDS = int(_num_env("POLL_SECONDS", 180))

# Preset applied to the AC every time it is turned on: cool mode, 20 C, fan 7.
AC_PRESET = {
    "workMode": int(_num_env("AC_MODE", 1)),          # 1 = cool
    "targetTemperature": int(_num_env("AC_TARGET_TEMP", 20)),
    "windSpeed7Gear": int(_num_env("AC_FAN_SPEED", 7)),
    "windSpeedAutoSwitch": 0,
}

# ntfy notifications (used by the background auto-monitor)
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

TCL_USER = os.getenv("TCL_USER", "")
TCL_PASSWORD = os.getenv("TCL_PASSWORD", "")
TCL_AC_NICKNAME = os.getenv("TCL_AC_NICKNAME", "").strip()

PORT = int(_num_env("PORT", 8000))
HOST = os.getenv("HOST", "0.0.0.0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("solar-ac-local")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Siseli (battery) -- IOT-Open signed auth
# ---------------------------------------------------------------------------
SISELI_APP_ID = os.getenv("SISELI_APP_ID", "rBrTRfAPXz")
_SISELI_APP_SECRET_ENC = os.getenv("SISELI_APP_SECRET_ENC",
                                  "I4D0KRr2339z3pQ/at91V9BpFAOe54DaTafwSm6suIQ=")


def _decrypt_app_secret(app_id, enc):
    from Crypto.Cipher import AES
    m = hashlib.md5(app_id.encode("utf-8")).hexdigest()
    return AES.new(m[:16].encode("ascii"), AES.MODE_CBC, m[16:].encode("ascii")).decrypt(
        base64.b64decode(enc)).rstrip(b"\x00").decode("utf-8")


def _iot_headers(body_bytes):
    secret = _decrypt_app_secret(SISELI_APP_ID, _SISELI_APP_SECRET_ENC)
    nonce = os.urandom(16).hex()
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    sh = {"IOT-Open-AppID": SISELI_APP_ID, "IOT-Open-Body-Hash": body_hash, "IOT-Open-Nonce": nonce}
    qs = "&".join(f"{k}={sh[k]}" for k in sorted(sh))
    b64 = base64.b64encode(qs.encode("utf-8")).decode("ascii")
    sig = hashlib.md5(hmac.new(secret.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).digest()).hexdigest()
    return {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
            "Origin": "https://solar.siseli.com", "Referer": "https://solar.siseli.com/",
            "IOT-Open-AppID": SISELI_APP_ID, "IOT-Open-Nonce": nonce,
            "IOT-Open-Body-Hash": body_hash, "IOT-Open-Sign": sig}


def _fetch_siseli_fields():
    """Login to Siseli and return the live device 'fields' dict."""
    if not SISELI_USER:
        raise RuntimeError("Siseli login: SISELI_USER is empty (missing env on host).")
    if not SISELI_PASSWORD:
        raise RuntimeError("Siseli login: SISELI_PASSWORD is empty (missing env on host).")
    payload = {"account": SISELI_USER,
               "password": hashlib.md5(SISELI_PASSWORD.encode("utf-8")).hexdigest()}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    r = httpx.post("https://solar.siseli.com/apis/login/account", content=body,
                   headers=_iot_headers(body), timeout=30)
    d = r.json()
    if r.status_code != 200 or d.get("code") not in (0, None, "0"):
        raise RuntimeError(f"Siseli login failed: {d.get('message') or d} | raw={r.text[:200]} | soc-user-set={bool(SISELI_USER)}")
    data = d.get("data") or d
    token = data.get("accessToken") or data.get("iotToken") or data.get("token") or ""

    r = httpx.get(
        "https://solar.siseli.com/apis/deviceState/simple/state/latest/v1",
        params={"deviceId": SISELI_DEVICE_ID, "dataSource": 1},
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
                 "IOT-Token": token, "IOT-Time-Zone": "Etc/UTC",
                 "Origin": "https://solar.siseli.com", "Referer": "https://solar.siseli.com/"},
        timeout=30,
    )
    d = r.json()
    if r.status_code != 200 or d.get("code") not in (0, None):
        raise RuntimeError(f"Siseli device state failed: {d.get('message') or d} | http={r.status_code} "
                           f"| raw={r.text[:200]} | device_id='{SISELI_DEVICE_ID}'")
    return (d.get("data") or {}).get("fields") or {}


def _field_num(fields, *keys):
    for k in keys:
        a = fields.get(k)
        if a is not None and a.get("value") is not None:
            try:
                return float(a["value"])
            except (TypeError, ValueError):
                continue
    return None


def read_battery_soc():
    fields = _fetch_siseli_fields()
    for key in ("batteryCapacity", "batterySOC", "batteryStateOfCharge"):
        attr = fields.get(key)
        if attr and attr.get("value") is not None:
            try:
                return float(attr["value"])
            except (TypeError, ValueError):
                continue
    raise RuntimeError("No battery SOC attribute found.")


def read_power_flow():
    """Return power flows in watts: battery charge (in), discharge (out), load, solar."""
    fields = _fetch_siseli_fields()
    volts = _field_num(fields, "batteryVoltage")
    charge_cur = _field_num(fields, "batteryChargingCurrent")
    discharge_cur = _field_num(fields, "batteryDischargeCurrent")
    load_kw = _field_num(fields, "acOutputActivePower")
    solar_kw = _field_num(fields, "generationPower")
    charge_w = volts * charge_cur if (volts is not None and charge_cur is not None) else None
    discharge_w = volts * discharge_cur if (volts is not None and discharge_cur is not None) else None
    return {
        "charge_w": round(charge_w, 1) if charge_w is not None else None,
        "discharge_w": round(discharge_w, 1) if discharge_w is not None else None,
        "load_w": round(load_kw * 1000.0, 1) if load_kw is not None else None,
        "solar_w": round(solar_kw * 1000.0, 1) if solar_kw is not None else None,
    }


# ---------------------------------------------------------------------------
# TCL (AC) auth + control
# ---------------------------------------------------------------------------
APP_LOGIN_URL = "https://pa.account.tcl.com/account/login?clientId=54148614"
APP_CLOUD_URL = "https://prod-center.aws.tcljd.com/v3/global/cloud_url_get"
APP_ID = "wx6e1af3fa84fbe523"


def md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sign_request(saas):
    ts = str(int(time.time() * 1000))
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return ts, nonce, md5_hex(ts + nonce + saas)


def tcl_auth():
    payload = {"equipment": 2, "password": md5_hex(TCL_PASSWORD), "osType": 1, "username": TCL_USER,
               "clientVersion": "4.8.1", "osVersion": "6.0",
               "deviceModel": "AndroidAndroid SDK built for x86", "captchaRule": 2, "channel": "app"}
    headers = {"th_platform": "android", "th_version": "4.8.1", "th_appbulid": "830",
               "user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = httpx.post(APP_LOGIN_URL, json=payload, headers=headers, timeout=20)
    d = r.json()
    if r.status_code != 200 or d.get("status") != 1:
        raise RuntimeError(f"TCL login failed: {d.get('message') or d}")
    user = d["user"]
    return {"token": d["token"], "username": user.get("username") or user.get("email")}


def tcl_cloud_urls(username, token):
    r = httpx.post(APP_CLOUD_URL, json={"ssoId": username, "ssoToken": token},
                   headers={"user-agent": "Android", "content-type": "application/json; charset=UTF-8"}, timeout=20)
    d = r.json()
    if r.status_code != 200 or len(d.get("data", {})) == 0:
        raise RuntimeError(f"TCL cloud_url failed: {d}")
    return d["data"]


def tcl_refresh_tokens(cloud_url, username, token):
    r = httpx.post(f"{cloud_url}/v3/auth/refresh_tokens",
                   json={"userId": username, "ssoToken": token, "appId": APP_ID},
                   headers={"user-agent": "Android", "content-type": "application/json; charset=UTF-8"}, timeout=20)
    d = r.json()
    if r.status_code != 200 or not d.get("data"):
        raise RuntimeError(f"TCL refresh_tokens failed: {d}")
    data = d["data"]
    return {"saas": data.get("saas_token") or data.get("saasToken"),
            "cognito": data.get("cognito_token") or data.get("cognitoToken")}


def tcl_get_things(device_url, saas):
    ts, nonce, sig = sign_request(saas)
    headers = {"platform": "android", "appversion": "5.4.1", "thomeversion": "4.8.1",
               "accesstoken": saas, "accept-language": "en", "timestamp": ts, "nonce": nonce,
               "sign": sig, "user-agent": "Android", "content-type": "application/json; charset=UTF-8"}
    r = httpx.post(f"{device_url}/v3/user/get_things", json={}, headers=headers, timeout=20)
    d = r.json()
    if r.status_code != 200 or d.get("code") != 0:
        raise RuntimeError(f"TCL get_things failed: {d.get('message') or d}")
    return d.get("data") or []


def jwt_sub(jwt_token):
    import jwt as pyjwt
    try:
        return pyjwt.decode(jwt_token, options={"verify_signature": False}).get("sub", "")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not decode JWT: {e}") from e


class TclClient:
    """Authenticates to TCL, finds the AC, and reads/sets its shadow power state."""

    def __init__(self):
        self.ac_id = None
        self.region = None

    def _auth_chain(self):
        auth = tcl_auth()
        urls = tcl_cloud_urls(auth["username"], auth["token"])
        tokens = tcl_refresh_tokens(urls["cloud_url"], auth["username"], auth["token"])
        things = tcl_get_things(urls["device_url"], tokens["saas"])
        if not things:
            raise RuntimeError("No TCL devices found on account.")
        target = None
        for t in things:
            name = t.get("nick_name") or t.get("nickName") or ""
            if TCL_AC_NICKNAME and name == TCL_AC_NICKNAME:
                target = t
                break
        if target is None:
            target = things[0]
        ac_id = target.get("deviceId") or target.get("device_id")
        aws_region = urls.get("cloud_region") or "ap-southeast-1"
        return ac_id, aws_region, tokens["cognito"]

    def _iot(self):
        if self.ac_id is None:
            ac_id, region, cognito = self._auth_chain()
            self.ac_id, self.region = ac_id, region
        else:
            _, region, cognito = self._auth_chain()
            self.region = region
        identity_pool = jwt_sub(cognito)
        r = httpx.post(f"https://cognito-identity.{region}.amazonaws.com/",
                       json={"IdentityId": identity_pool,
                             "Logins": {"cognito-identity.amazonaws.com": cognito}},
                       headers={"User-Agent": "aws-sdk-android/2.22.6",
                                "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
                                "content-type": "application/x-amz-json-1.1"}, timeout=20)
        aws = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"TCL cognito failed: {aws}")
        creds = aws["Credentials"]
        return boto3.client("iot-data", region_name=region,
                            aws_access_key_id=creds["AccessKeyId"],
                            aws_secret_access_key=creds["SecretKey"],
                            aws_session_token=creds["SessionToken"])

    def get_power_switch(self):
        """Return True/False/None for the AC's current reported power."""
        iot = self._iot()
        resp = iot.get_thing_shadow(thingName=self.ac_id)
        shadow = json.loads(resp["payload"].read().decode("utf-8"))
        reported = (shadow.get("state") or {}).get("reported") or {}
        val = reported.get("powerSwitch")
        if val is None:
            delta = (shadow.get("state") or {}).get("delta") or {}
            val = delta.get("powerSwitch")
        if val is None:
            return None
        return int(val) == 1

    def set_power(self, on: bool):
        iot = self._iot()
        desired = {"powerSwitch": 1 if on else 0}
        if on:
            desired.update(AC_PRESET)
        payload = json.dumps({"state": {"desired": desired},
                              "clientToken": f"mobile_{int(time.time())}"})
        topic = f"$aws/things/{self.ac_id}/shadow/update"
        iot.publish(topic=topic, qos=1, payload=payload)
        log.info("Sent AC %s (%s)%s", "ON" if on else "OFF", self.ac_id,
                 " with preset" if on else "")


# ---------------------------------------------------------------------------
# ntfy notifications
# ---------------------------------------------------------------------------
def send_notification(title, msg, priority=3):
    if not NTFY_TOPIC:
        return False
    try:
        r = httpx.post(f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
                       data=msg.encode("utf-8"),
                       headers={"Title": title, "Priority": str(priority)}, timeout=20)
        ok = r.status_code == 200
        log.info("[ntfy] sent: %s (HTTP %s)", title, r.status_code)
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("[ntfy] send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Background auto-monitor (runs forever on the always-on host)
# Mirrors the GitHub workflow: every POLL_SECONDS seconds, read battery + AC
# state, turn the AC off automatically when it's on and battery dips below
# OFF_THRESHOLD, and alert when battery falls below ALERT_THRESHOLD.
# ---------------------------------------------------------------------------
def auto_monitor_loop():
    log.info("Auto-monitor started (poll every %ss, off<%s%%, alert<%s%%).",
             POLL_SECONDS, OFF_THRESHOLD, ALERT_THRESHOLD)
    last_low_alert = None
    while True:
        try:
            soc = read_battery_soc()
            tcl = TclClient()
            ac_on = tcl.get_power_switch()
            log.info("Monitor: battery %.1f%%, AC on=%s", soc, ac_on)

            # low-battery alert (throttled: only when value changes or level crossed)
            if soc < ALERT_THRESHOLD:
                level = int(soc)
                if last_low_alert != level:
                    send_notification("Low battery",
                                      f"Battery is {soc:.1f}% (< {ALERT_THRESHOLD:.0f}%).",
                                      priority=4)
                    last_low_alert = level
            else:
                last_low_alert = None

            # auto turn-off when AC on and battery below off-threshold
            if ac_on is True and soc < OFF_THRESHOLD:
                log.info("Auto: AC on but battery %.1f%% < %.0f%% -> turning off.",
                         soc, OFF_THRESHOLD)
                tcl.set_power(False)
                send_notification("AC turned OFF automatically",
                                  f"Battery reached {soc:.1f}% (< {OFF_THRESHOLD:.0f}%).")
        except Exception as e:  # noqa: BLE001
            log.warning("Monitor pass error: %s", e)
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(app.root_path, "index.html")


@app.route("/api/status")
def api_status():
    soc = None
    flow = {"charge_w": None, "discharge_w": None, "load_w": None, "solar_w": None}
    ac_on = None
    err = None
    try:
        flow = read_power_flow()
    except Exception as e:  # noqa: BLE001
        err = f"battery: {e}"
    try:
        soc = read_battery_soc()
    except Exception as e:  # noqa: BLE001
        err = (err + "; " if err else "") + f"soc: {e}"
    try:
        ac_on = TclClient().get_power_switch()
    except Exception as e:  # noqa: BLE001
        err = (err + "; " if err else "") + f"ac: {e}"
    return jsonify({
        "soc": soc,
        "ac_on": ac_on,
        "charge_w": flow["charge_w"],
        "discharge_w": flow["discharge_w"],
        "load_w": flow["load_w"],
        "solar_w": flow["solar_w"],
        "on_threshold": ON_THRESHOLD,
        "error": err,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/turn_on", methods=["POST"])
def api_turn_on():
    try:
        soc = read_battery_soc()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"Battery read failed: {e}"}), 500
    if soc < ON_THRESHOLD:
        return jsonify({"ok": False,
                        "message": f"Battery {soc:.1f}% is below {ON_THRESHOLD:.0f}% — AC not turned on."})
    try:
        TclClient().set_power(True)
        return jsonify({"ok": True,
                        "message": f"Battery {soc:.1f}% >= {ON_THRESHOLD:.0f}% — AC turned ON (cool 20°, fan 7)."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"AC control failed: {e}"}), 500


@app.route("/api/turn_off", methods=["POST"])
def api_turn_off():
    try:
        TclClient().set_power(False)
        return jsonify({"ok": True, "message": "AC turned OFF."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"AC control failed: {e}"}), 500


if __name__ == "__main__":
    import threading

    # Start the always-on auto-monitor (auto turn-off + low-battery alerts).
    threading.Thread(target=auto_monitor_loop, daemon=True).start()

    import socket
    host_ip = None
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except Exception:  # noqa: BLE001
        pass
    log.info("Server running on http://%s:%s", host_ip or "localhost", PORT)
    if host_ip:
        log.info("Open on your phone (same Wi-Fi): http://%s:%s", host_ip, PORT)
    serve(app, host=HOST, port=PORT, threads=8)