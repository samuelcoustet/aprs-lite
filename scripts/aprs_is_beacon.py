#!/usr/bin/env python3
"""APRS-IS beacon — lit callsign, passcode, position, altitude et commentaire
depuis les fichiers de config (dynamique, plus rien en dur)."""
import re, socket, sys
from datetime import datetime, timezone

CONF = "/opt/aprs-lite/direwolf.conf"
ENV  = "/opt/aprs-lite/config.env"
HOST = "euro.aprs2.net"
PORT = 14580

def read_login(conf):
    mycall = None; passcode = None
    with open(conf, encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"): continue
            m = re.match(r"^MYCALL\s+(\S+)", s, re.I)
            if m: mycall = m.group(1).split("-")[0]
            m = re.match(r"^IGLOGIN\s+(\S+)\s+(\S+)", s, re.I)
            if m:
                passcode = m.group(2)
                if mycall is None: mycall = m.group(1).split("-")[0]
    if not mycall or not passcode:
        raise RuntimeError("MYCALL/IGLOGIN introuvable dans direwolf.conf")
    return mycall, passcode

def read_env(env):
    d = {}
    with open(env, encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def latlon_to_aprs(lat, lon):
    ld = int(abs(lat)); lm = (abs(lat) % 1) * 60
    od = int(abs(lon)); om = (abs(lon) % 1) * 60
    ls  = f"{ld:02d}{lm:05.2f}{'N' if lat >= 0 else 'S'}"
    os_ = f"{od:03d}{om:05.2f}{'E' if lon >= 0 else 'W'}"
    return ls, os_

def main():
    call, pwd = read_login(CONF)
    cfg = read_env(ENV)
    lat = float(cfg.get("LAT", "0"))
    lon = float(cfg.get("LON", "0"))
    comment = cfg.get("COMMENT", "Relais APRS")
    try: alt_m = float(cfg.get("ALTITUDE_M", "0"))
    except: alt_m = 0
    ls, os_ = latlon_to_aprs(lat, lon)
    alt_ft = int(alt_m * 3.28084) if alt_m else 0
    suffix = f"/A={alt_ft:06d}{comment}" if alt_ft else f"# {comment}"
    login  = f"user {call} pass {pwd} vers aprs-is-beacon 1.1\n"
    packet = f"{call}>APDW17,TCPIP*:!{ls}/{os_}{suffix}\n"
    with socket.create_connection((HOST, PORT), timeout=15) as s:
        s.settimeout(15)
        s.sendall(login.encode("ascii", "ignore"))
        _ = s.recv(512)
        s.sendall(packet.encode("ascii", "ignore"))
    now = datetime.now(timezone.utc).isoformat()
    print(f"{now} sent: {packet.strip()}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)
