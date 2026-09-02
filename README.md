# AC Monitor

A small **local** web app that runs on your own computer and gives you a one-page
switch to turn your TCL split air conditioner **on** and **off**, with a
battery-safety guard.

It talks to two cloud services directly from your PC (no third-party server involved):

- **Solar / battery** — reads the state-of-charge (SOC) from `solar.siseli.com`.
- **TCL AC** — publishes an ON/OFF command to the AC's AWS IoT Thing Shadow.

## Rules (same as the GitHub workflow)
- **Switch ON** → only allowed when battery **≥ `ON_THRESHOLD`** (default 50%). Otherwise refused.
- **Switch OFF** → always allowed.
- The page shows the live battery % and the AC's current on/off state.

## Requirements
- Python 3.11+
- Install deps: `pip install -r requirements.txt`
- Create a `.env` file (see `.env.example`) with your Siseli + TCL credentials.

## Run
```
python server.py
```
Then open:
- on this PC: `http://localhost:8000`
- on your phone (same Wi-Fi): `http://<this-PC-LAN-IP>:8000`

## Auto-start on boot (Windows)
`start_server.bat` starts the server hidden. A Scheduled Task named `SolarACServer`
(trigger: At logon) runs it each time the user logs into Windows.

## Files
- `server.py` — Flask server (endpoints `/api/status`, `/api/turn_on`, `/api/turn_off`).
- `index.html` — the one-page UI (toggle switch + battery gauge).
- `start_server.bat` — hidden background launcher for auto-start.
- `requirements.txt` — Python dependencies.