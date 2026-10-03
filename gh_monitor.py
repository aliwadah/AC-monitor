"""
GitHub-Actions side of the AC monitor.

Reads the battery (Siseli), reads/controls the AC (TCL), enforces the
auto-turn-off / low-battery rules (shared logic lives in server.py), then
publishes the latest state to the public JSON file in the aliwadah/acstate
repo so the PythonAnywhere web page can show it.

Modes:
    monitor            one full auto-monitor pass (battery + AC + rules)
    on / off           manual command from the web page (battery guard for ON)

Usage (GitHub Actions job):
    python gh_monitor.py --mode monitor --pat "${{ secrets.GH_PAT }}"
    python gh_monitor.py --mode on     --pat "${{ secrets.GH_PAT }}"
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

sys.path.insert(0, HERE)


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def publish_state(state, pat):
    repo = os.environ.get("GH_STATE_REPO", "aliwadah/acstate")
    url = f"https://api.github.com/repos/{repo}/contents/state.json"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "acmon",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        sha = json.loads(resp.read().decode()).get("sha")
    except Exception as e:
        print(f"[gh_monitor] could not fetch current state sha: {e}")
        sha = None
    payload = {
        "message": f"state update {utcnow()}",
        "content": base64.b64encode(json.dumps(state).encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**headers, "Content-Type": "application/json"},
                                 method="PUT")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        print(f"[gh_monitor] published state (HTTP {resp.status})")
        return True
    except Exception as e:
        print(f"[gh_monitor] publish failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["monitor", "on", "off"], required=True)
    ap.add_argument("--pat", default=os.environ.get("GH_PAT", ""))
    ap.add_argument("--out", default=os.path.join(HERE, "state.json"))
    ap.add_argument("--skip-publish", action="store_true")
    args = ap.parse_args()

    import server

    mode = args.mode
    soc = None
    ac_on = None
    guard = None
    messages = []

    flow = {}
    try:
        if mode == "monitor":
            soc, ac_on = server.monitor_once()
            messages.append("monitor pass done")
            try:
                flow = server.read_power_flow()
            except Exception as e:  # noqa: BLE001
                messages.append(f"flow: {e}")
        elif mode == "on":
            soc = server.read_battery_soc()
            if soc < server.ON_THRESHOLD:
                guard = f"battery_too_low {soc:.1f}%"
                messages.append("denied: battery below threshold")
            else:
                server.TclClient().set_power(True)
                messages.append("AC turned ON")
            try:
                ac_on = server.TclClient().get_power_switch()
            except Exception as e:  # noqa: BLE001
                messages.append(f"state read: {e}")
        else:  # off
            try:
                soc = server.read_battery_soc()
            except Exception as e:  # noqa: BLE001
                messages.append(f"soc read: {e}")
            server.TclClient().set_power(False)
            messages.append("AC turned OFF")
            try:
                ac_on = server.TclClient().get_power_switch()
            except Exception as e:  # noqa: BLE001
                messages.append(f"state read: {e}")

        state = {
            "ok": True,
            "soc": soc,
            "ac_on": ac_on,
            "charge_w": flow.get("charge_w"),
            "discharge_w": flow.get("discharge_w"),
            "load_w": flow.get("load_w"),
            "solar_w": flow.get("solar_w"),
            "guard": guard,
            "updated": utcnow(),
            "pmessages": messages,
            "source": "gh",
        }
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        state = {
            "ok": False,
            "soc": soc,
            "ac_on": ac_on,
            "charge_w": flow.get("charge_w"),
            "discharge_w": flow.get("discharge_w"),
            "load_w": flow.get("load_w"),
            "solar_w": flow.get("solar_w"),
            "guard": guard,
            "error": str(e),
            "updated": utcnow(),
            "pmessages": messages,
            "source": "gh",
        }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(state, f)
    print("[gh_monitor]", json.dumps(state))

    if not args.skip_publish and args.pat:
        publish_state(state, args.pat)

    # Wake the PythonAnywhere page so it doesn't sleep-cold between runs.
    try:
        import httpx
        httpx.get("https://aliwadah.pythonanywhere.com/api/monitor", timeout=30)
    except Exception:  # noqa: BLE001
        pass

    sys.exit(0 if state["ok"] else 1)


if __name__ == "__main__":
    main()