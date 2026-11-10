"""Sync the "wifi" list in config.json into NetworkManager profiles (run as root).

NetworkManager then joins whichever known network is in range. Entries without
an ssid are skipped; an empty password means an open network. Profiles are
named "pitrans-<ssid>" and are only added or updated, never deleted.
"""
import json
import subprocess
import sys

config_path = sys.argv[1] if len(sys.argv) > 1 else "/home/chao/pitrans/config.json"
with open(config_path, encoding="utf-8") as f:
    networks = json.load(f).get("wifi", [])

existing = subprocess.run(["nmcli", "-t", "-f", "NAME", "connection", "show"],
                          capture_output=True, text=True, check=True).stdout.splitlines()

for net in networks:
    ssid = net.get("ssid", "").strip()
    if not ssid:
        continue
    name = f"pitrans-{ssid}"
    settings = ["802-11-wireless.ssid", ssid,
                "802-11-wireless.hidden", "yes" if net.get("hidden") else "no",
                "connection.autoconnect", "yes",
                "connection.autoconnect-priority", str(net.get("priority", 0))]
    if net.get("password"):
        settings += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", net["password"]]
    if name in existing:
        cmd = ["nmcli", "connection", "modify", name, *settings]
    else:
        cmd = ["nmcli", "connection", "add", "type", "wifi", "con-name", name, "ssid", ssid, *settings]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(f"{name}: {'ok' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()}")
