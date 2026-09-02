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

## Deploy to Railway (cloud hosting)

This app is a Python back-end, so it needs a platform that can run a Python
process and pass it secrets as environment variables. Railway is a good fit.

> Security note: hosting in the cloud means your credentials live on Railway's
> servers as **Environment variables (Secret type)**, not in the repo.

Steps:
1. Sign up / log in at https://railway.app (browser).
2. "New Project" -> "Deploy from GitHub repo".
3. Choose this repo (`AC-monitor`).
4. Railway will build (Nixpacks) and start via `railway.json`
   (`python server.py`).
5. Add **Environment variables (Set as Secret)** for every key your app needs:
   - `SISELI_USER`, `SISELI_PASSWORD`, `SISELI_DEVICE_ID`
   - `TCL_USER`, `TCL_PASSWORD`, `TCL_AC_NICKNAME`
   - `ON_THRESHOLD` (e.g. `50`)
6. Redeploy. Railway auto-assigns a `PORT` env var; `server.py` binds to it.
7. Open the generated public URL (e.g. `https://ac-monitor.up.railway.app`).

The public URL serves `index.html` (the toggle switch UI) and the
`/api/status`, `/api/turn_on`, `/api/turn_off` endpoints.

## Local vs hosted
- Locally: `python server.py`, open `http://localhost:8000`.
- Hosted: use the Railway public URL. Same UI, but the back-end now runs in
  the cloud with secrets in Railway env vars.

## Files
- `server.py` — Flask server (endpoints `/api/status`, `/api/turn_on`, `/api/turn_off`).
- `index.html` — the one-page UI (toggle switch + battery gauge).
- `start_server.bat` — hidden background launcher for auto-start.
- `requirements.txt` — Python dependencies.