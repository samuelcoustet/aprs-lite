#!/usr/bin/env python3
"""
aioc_detect.py — Détection automatique AIOC All-In-One-Cable
Méthode fiable : VID:PID USB + ALSA (regex fr/en) + hidraw udevadm
"""

import re
import subprocess
import time
from pathlib import Path


AIOC_VID_PID = "1209:7388"
CONFIG_PATH = Path("/opt/aprs-lite/config.env")
DIREWOLF_CONF = Path("/opt/aprs-lite/direwolf.conf")


def detect_usb() -> bool:
    """Vérifie la présence de l'AIOC via lsusb."""
    try:
        out = subprocess.check_output(["lsusb"], text=True, timeout=5)
        return AIOC_VID_PID.lower() in out.lower()
    except Exception:
        return False


def detect_alsa() -> dict:
    """Trouve la carte ALSA de l'AIOC — supporte fr et en."""
    result = {"card_num": None, "card_name": None, "playback": False, "capture": False}
    try:
        out_play = subprocess.check_output(
            ["aplay", "-l"], text=True, timeout=5, stderr=subprocess.DEVNULL
        )
        out_rec = subprocess.check_output(
            ["arecord", "-l"], text=True, timeout=5, stderr=subprocess.DEVNULL
        )

        # Regex compatible français (carte N : ID [Nom]) et anglais (card N: ID [Nom])
        pattern = re.compile(
            r'^(?:card|carte)\s+(\d+)\s*:\s*(\S+)\s+\[(.+?)\]',
            re.IGNORECASE
        )

        playback_cards = set()
        capture_cards = set()
        cards = {}

        for line in out_play.splitlines():
            m = pattern.match(line)
            if m:
                num, cid, name = int(m.group(1)), m.group(2), m.group(3)
                playback_cards.add(num)
                cards[num] = {"id": cid, "name": name}

        for line in out_rec.splitlines():
            m = pattern.match(line)
            if m:
                capture_cards.add(int(m.group(1)))

        # Identifier l'AIOC parmi les cartes
        for num, info in cards.items():
            combined = (info["name"] + info["id"]).lower()
            if any(k in combined for k in ("aioc", "allinone", "all-in-one", "allinonecable")):
                result["card_num"] = num
                result["card_name"] = info["name"]
                result["playback"] = num in playback_cards
                result["capture"] = num in capture_cards
                break

    except Exception:
        pass

    return result


def detect_hidraw() -> str | None:
    """Trouve /dev/hidrawN associé à l'AIOC via udevadm."""
    try:
        import glob
        for dev in sorted(glob.glob("/dev/hidraw*")):
            out = subprocess.check_output(
                ["udevadm", "info", "--query=all", f"--name={dev}"],
                text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            if "1209" in out and "7388" in out:
                return dev
    except Exception:
        pass
    return None


def full_detect() -> dict:
    """Détection complète AIOC — retourne état et chemins."""
    usb = detect_usb()
    alsa = detect_alsa()
    hidraw = detect_hidraw()

    card_num = alsa.get("card_num")
    adevice = "plughw:AllInOneCable,0" if card_num is not None else None
    ptt = f"CM108 {hidraw}" if hidraw else None

    ok = usb and card_num is not None and hidraw is not None

    return {
        "ok": ok,
        "usb": usb,
        "alsa_card": card_num,
        "alsa_name": alsa.get("card_name"),
        "playback": alsa.get("playback", False),
        "capture": alsa.get("capture", False),
        "hidraw": hidraw,
        "adevice": adevice,
        "ptt": ptt,
    }


def update_config(det: dict):
    """Met à jour config.env avec les valeurs détectées."""
    if not CONFIG_PATH.exists():
        return
    content = CONFIG_PATH.read_text()
    if det["adevice"]:
        content = re.sub(r'^ADEVICE=.*$', f'ADEVICE="{det["adevice"]}"', content, flags=re.MULTILINE)
    if det["ptt"]:
        content = re.sub(r'^PTT=.*$', f'PTT="{det["ptt"]}"', content, flags=re.MULTILINE)
    CONFIG_PATH.write_text(content)


def update_direwolf(det: dict):
    """Met à jour direwolf.conf avec ADEVICE et PTT détectés."""
    if not DIREWOLF_CONF.exists():
        return
    content = DIREWOLF_CONF.read_text()
    if det["adevice"]:
        content = re.sub(r'^ADEVICE\s+.*$', f'ADEVICE {det["adevice"]}', content, flags=re.MULTILINE)
    if det["ptt"]:
        content = re.sub(r'^PTT\s+.*$', f'PTT {det["ptt"]}', content, flags=re.MULTILINE)
    DIREWOLF_CONF.write_text(content)


def wait_for_aioc(timeout: int = 60, interval: int = 5) -> dict:
    """Attend que l'AIOC soit détecté (utilisé par ExecStartPre)."""
    elapsed = 0
    while elapsed < timeout:
        det = full_detect()
        if det["ok"]:
            print(f"[AIOC] Détecté — card:{det['alsa_card']} hidraw:{det['hidraw']}")
            update_direwolf(det)
            update_config(det)
            return det
        print(f"[AIOC] Non détecté, attente... ({elapsed}/{timeout}s)")
        time.sleep(interval)
        elapsed += interval
    print(f"[AIOC] Non détecté après {timeout}s")
    return full_detect()


if __name__ == "__main__":
    import sys
    if "--wait" in sys.argv:
        det = wait_for_aioc(timeout=60)
        sys.exit(0 if det["ok"] else 1)
    else:
        det = full_detect()
        print(f"USB     : {'✅' if det['usb'] else '❌'}")
        print(f"ALSA    : {'✅' if det['alsa_card'] is not None else '❌'} — {det['alsa_name'] or 'non détecté'}")
        print(f"Hidraw  : {'✅' if det['hidraw'] else '❌'} — {det['hidraw'] or 'non détecté'}")
        print(f"ADEVICE : {det['adevice'] or 'N/A'}")
        print(f"PTT     : {det['ptt'] or 'N/A'}")
        print(f"Global  : {'✅ OK' if det['ok'] else '❌ INCOMPLET'}")
        sys.exit(0 if det["ok"] else 1)
