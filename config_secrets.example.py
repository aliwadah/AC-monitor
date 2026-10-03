# Copy this file to config_secrets.py and fill in your real values.
# config_secrets.py is git-ignored (never commit your passwords).
#
# When running on hosts where the battery/AC clouds block datacenter IPs
# (e.g. PythonAnywhere), set GITHUB_PAT so status comes from the GitHub
# Actions monitor and ON/OFF commands are relayed through GitHub.

SECRETS = {
    "SISELI_USER": "",
    "SISELI_PASSWORD": "",
    "SISELI_DEVICE_ID": "",
    "TCL_USER": "",
    "TCL_PASSWORD": "",
    "TCL_AC_NICKNAME": "Split AC",
    "ON_THRESHOLD": "50",
    "OFF_THRESHOLD": "50",
    "ALERT_THRESHOLD": "30",
    "POLL_SECONDS": "180",
    "NTFY_SERVER": "https://ntfy.sh",
    "NTFY_TOPIC": "",
    "GITHUB_PAT": "",
    "GH_REPO": "aliwadah/AC-monitor",
    "GH_WORKFLOW": "monitor.yml",
    "GH_STATE_URL": "https://raw.githubusercontent.com/aliwadah/acstate/HEAD/state.json",
}