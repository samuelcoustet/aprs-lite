#!/usr/bin/env python3
"""
aprs-lite.py v1.0.10
"""
import re, subprocess, socket, time, math, sys as _sys, threading, sqlite3
from enum import Enum
from dataclasses import dataclass, field as _field
from pathlib import Path
from datetime import datetime, timezone as _tz
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (Header, Footer, Static, Button, Label, Input, Log,
                              TabbedContent, TabPane, Rule, Select, DataTable)
from textual.binding import Binding
from textual import work

CONFIG_PATH   = Path("/opt/aprs-lite/config.env")
DIREWOLF_CONF = Path("/opt/aprs-lite/direwolf.conf")
SIDECAR_DB    = Path("/home/pi/aprs-sidecar-dashboard/data/sidecar.db")
LOG_MAX_LINES = 500
_MSG_MAX      = 100
_OUT_MAX      = 30

# Délais entre tentatives (secondes) : immédiat, 30s, 60s, 120s, 120s
_MSG_RETRY_DELAYS = [0, 30, 60, 120, 120]

class MsgState(Enum):
    PENDING  = "⏳"
    ACKED    = "✓ ACK"
    FAILED   = "✗ Echec"
    REJECTED = "✗ REJ"

@dataclass
class TrackedMsg:
    msg_id:    str
    addressee: str
    text:      str
    state:     MsgState        = MsgState.PENDING
    attempts:  int             = 0
    created:   float           = _field(default_factory=time.monotonic)
    acked_at:  float | None    = None
    row_id:    int             = 0   # ID dans chat_messages (sidecar DB)

_config_cache: dict = {}
_config_mtime: float = 0.0

def load_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mt = CONFIG_PATH.stat().st_mtime
        if mt == _config_mtime:
            return _config_cache
        _config_mtime = mt
    except:
        pass
    cfg = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    _config_cache = cfg
    return cfg

def save_config(key, value):
    if not CONFIG_PATH.exists(): return
    content = CONFIG_PATH.read_text()
    pattern = re.compile(rf'^{re.escape(key)}=.*$', re.MULTILINE)
    new_line = f'{key}="{value}"'
    content = pattern.sub(new_line, content) if pattern.search(content) else content + f'\n{new_line}\n'
    CONFIG_PATH.write_text(content)

def save_direwolf(key, value):
    if not DIREWOLF_CONF.exists(): return
    content = DIREWOLF_CONF.read_text()
    pattern = re.compile(rf'^{re.escape(key)}\s+.*$', re.MULTILINE)
    if pattern.search(content): content = pattern.sub(f'{key} {value}', content)
    DIREWOLF_CONF.write_text(content)

DIREWOLF_SERVICE = Path("/etc/systemd/system/aprs-direwolf.service")

COLOR_OPTIONS = [
    ("0 — Fond noir, ANSI standard",       "0"),
    ("1 — Fond blanc, RGB 24-bit (defaut)", "1"),
    ("2 — Fond blanc, ANSI standard",       "2"),
    ("3 — Fond blanc, ANSI clignotant",     "3"),
    ("4 — Fond blanc, ANSI gras",           "4"),
    ("5 — Fond sombre, RGB 24-bit",         "5"),
    ("6 — Fond sombre, ANSI standard",      "6"),
    ("7 — Fond sombre, ANSI clignotant",    "7"),
    ("8 — Fond sombre, ANSI gras",          "8"),
]

def get_direwolf_color() -> str:
    try:
        m = re.search(r'-t\s+(\d+)', DIREWOLF_SERVICE.read_text())
        return m.group(1) if m else "1"
    except: return "1"

def save_direwolf_color(value: str):
    try:
        content = DIREWOLF_SERVICE.read_text()
        if re.search(r'-t\s+\d+', content):
            new_content = re.sub(r'(-t\s+)\d+', rf'\g<1>{value}', content)
        else:
            new_content = re.sub(
                r'(ExecStart=.*direwolf\b[^\n]*)',
                rf'\1 -t {value}',
                content, flags=re.MULTILINE)
        subprocess.run(["sudo","tee",str(DIREWOLF_SERVICE)], input=new_content, text=True, capture_output=True, timeout=10)
        subprocess.run(["sudo","systemctl","daemon-reload"], timeout=10)
    except: pass

def service_status(name):
    try:
        r = subprocess.run(["systemctl","is-active",name], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except: return "unknown"

def kiss_port_open():
    try:
        s = socket.create_connection(("127.0.0.1", 8001), timeout=2); s.close(); return True
    except: return False

def encode_aprs_message(addressee: str, text: str, msg_id: str) -> str:
    return f":{addressee.upper().ljust(9)[:9]}:{text[:67]}{{{msg_id}"

def encode_aprs_ack(addressee: str, msg_id: str) -> str:
    return f":{addressee.upper().ljust(9)[:9]}:ack{msg_id}"

def _encode_msg_packet(callsign: str, addressee: str, text: str, msg_id: str, via: str = "rf") -> str:
    """Retourne un paquet APRS complet (RF ou IS)."""
    dst_pad = addressee.upper().ljust(9)
    if via == "rf":
        return f"{callsign.upper()}>APRS,WIDE1-1::{dst_pad}:{text[:67]}{{{msg_id}"
    else:
        return f"{callsign.upper()}>APNW01,TCPIP*::{dst_pad}:{text[:67]}{{{msg_id}"

def _encode_ack_packet(callsign: str, addressee: str, msg_id: str, via: str = "is") -> str:
    """Retourne un paquet ACK complet."""
    dst_pad = addressee.upper().ljust(9)
    if via == "rf":
        return f"{callsign.upper()}>APRS,WIDE1-1::{dst_pad}:ack{msg_id}"
    else:
        return f"{callsign.upper()}>APNW01,TCPIP*::{dst_pad}:ack{msg_id}"

def _aprs_is_server() -> str:
    try:
        m = re.search(r'^IGSERVER\s+(\S+)', DIREWOLF_CONF.read_text(), re.MULTILINE)
        if m: return m.group(1)
    except: pass
    return "euro.aprs2.net"

def _send_packet_is(packet: str, callsign: str, passcode: str, server: str) -> tuple[bool, str]:
    """Envoie un paquet APRS complet via TCP APRS-IS."""
    try:
        with socket.create_connection((server, 14580), timeout=10) as s:
            f = s.makefile("rb")
            f.readline()  # bannière
            s.sendall(f"user {callsign} pass {passcode} vers aprs-lite 1.0.9\r\n".encode())
            f.readline()  # réponse login
            s.sendall(f"{packet}\r\n".encode())
            time.sleep(0.5)
        return True, ""
    except Exception as e:
        return False, str(e)

def send_aprs_is(info: str) -> tuple[bool, str]:
    """Compat: envoie un info-field via IS (utilisé pour ACK auto inbound)."""
    cfg = load_config()
    callsign = cfg.get("CALLSIGN", "N0CALL").strip()
    passcode = cfg.get("PASSCODE", "0").strip()
    server   = _aprs_is_server()
    packet   = f"{callsign.upper()}>APNW01,TCPIP*:{info}"
    return _send_packet_is(packet, callsign, passcode, server)

def _send_msg_rf_is(callsign: str, passcode: str, server: str,
                    addressee: str, text: str, msg_id: str) -> tuple[bool, str, str]:
    """RF-first (kissutil WIDE1-1), fallback IS. Retourne (ok, err, via)."""
    packet_rf = _encode_msg_packet(callsign, addressee, text, msg_id, via="rf")
    ok, err = send_beacon(packet_rf)
    if ok:
        return True, "", "rf"
    packet_is = _encode_msg_packet(callsign, addressee, text, msg_id, via="is")
    ok, err = _send_packet_is(packet_is, callsign, passcode, server)
    return ok, err, "is"

# --- DB partagée sidecar (écriture directe sqlite3, sans importer db.py) ---

def _chat_insert(direction: str, src: str, dst: str, text: str, msg_no: str, via: str = "rf") -> int:
    """Insère dans chat_messages de la sidecar DB. Retourne row_id ou 0 si DB absente."""
    if not SIDECAR_DB.exists():
        return 0
    try:
        now = datetime.now(_tz.utc).isoformat()
        status = "pending" if direction == "out" else "in"
        with sqlite3.connect(str(SIDECAR_DB), timeout=5) as conn:
            cur = conn.execute(
                "INSERT INTO chat_messages (timestamp,direction,src,dst,text,msg_no,via,status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, direction, src.upper(), dst.upper(), text, msg_no, via, status),
            )
            return cur.lastrowid or 0
    except Exception:
        return 0

def _chat_ack(src: str, msg_no: str):
    """Marque un message sortant comme ACKé dans la sidecar DB."""
    if not SIDECAR_DB.exists():
        return
    try:
        now = datetime.now(_tz.utc).isoformat()
        with sqlite3.connect(str(SIDECAR_DB), timeout=5) as conn:
            conn.execute(
                "UPDATE chat_messages SET acked=1, ack_at=?, status='acked'"
                " WHERE dst=? AND msg_no=? AND direction='out'",
                (now, src.upper(), msg_no),
            )
    except Exception:
        pass

def _chat_set_status(row_id: int, status: str):
    """Met à jour le statut d'un message dans la sidecar DB."""
    if not SIDECAR_DB.exists() or not row_id:
        return
    try:
        with sqlite3.connect(str(SIDECAR_DB), timeout=5) as conn:
            conn.execute("UPDATE chat_messages SET status=? WHERE id=?", (status, row_id))
    except Exception:
        pass

def _telemetry_insert(bme_data: dict, box_data: dict | None = None):
    """Insère une ligne de telemetry dans sidecar.db (capteur + système)."""
    if not SIDECAR_DB.exists():
        return
    try:
        # CPU température numérique
        cpu_temp = None
        try:
            cpu_temp = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip()) / 1000
        except Exception:
            pass
        # CPU usage
        cpu_usage = None
        try:
            r = subprocess.run(["top", "-bn1"], capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if "Cpu" in line or "cpu" in line:
                    m = re.search(r'(\d+\.\d+)\s+id', line)
                    if m:
                        cpu_usage = round(100.0 - float(m.group(1)), 1)
                    break
        except Exception:
            pass
        # RAM usage %
        ram_usage = None
        try:
            mem = {}
            for line in Path('/proc/meminfo').read_text().splitlines():
                k, _, v = line.partition(':')
                mem[k.strip()] = int(v.split()[0])
            total = mem.get('MemTotal', 0)
            if total:
                ram_usage = round((total - mem.get('MemAvailable', 0)) * 100 / total, 1)
        except Exception:
            pass
        # Disk usage %
        disk_usage = None
        try:
            r = subprocess.run(["df", "/"], capture_output=True, text=True, timeout=3)
            lines = r.stdout.splitlines()
            if len(lines) > 1:
                disk_usage = float(lines[1].split()[4].rstrip('%'))
        except Exception:
            pass
        # Load avg
        load_avg = None
        try:
            load_avg = round(float(Path('/proc/loadavg').read_text().split()[0]), 2)
        except Exception:
            pass

        now = datetime.now(_tz.utc).isoformat()
        row = (
            now, cpu_temp, cpu_usage, ram_usage, disk_usage, "", load_avg,
            bme_data.get("temperature"), bme_data.get("humidity"), bme_data.get("pressure"),
            box_data.get("temperature") if box_data else None,
            box_data.get("humidity")    if box_data else None,
            box_data.get("pressure")    if box_data else None,
        )
        with sqlite3.connect(str(SIDECAR_DB), timeout=5) as conn:
            conn.execute(
                "INSERT INTO telemetry (timestamp,cpu_temp,cpu_usage,ram_usage,disk_usage,"
                "direwolf_status,load_avg_1m,bme_temp,bme_humidity,bme_pressure,"
                "box_temp,box_humidity,box_pressure) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
    except Exception:
        pass

def send_beacon(packet):
    if not kiss_port_open(): return False, "Port KISS 8001 inaccessible"
    try:
        r = subprocess.run(["kissutil","-h","127.0.0.1","-p","8001"], input=packet+"\n", capture_output=True, text=True, timeout=5)
        return (True,"") if r.returncode==0 else (False, r.stderr.strip() or "kissutil erreur")
    except Exception as e: return False, str(e)

def detect_aioc():
    try:
        import sys; sys.path.insert(0,"/opt/aprs-lite")
        from aioc_detect import full_detect; return full_detect()
    except: return {"ok":False,"usb":False,"alsa_card":None,"hidraw":None,"alsa_name":None,"playback":False,"capture":False,"adevice":None,"ptt":None}

def get_system_stats():
    stats = {"cpu_temp":"?","ram_pct":"?","sd_pct":"?"}
    try: stats["cpu_temp"] = f"{int(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip())//1000}C"
    except: pass
    try:
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            k, _, v = line.partition(':')
            mem[k.strip()] = int(v.split()[0])
        total = mem.get('MemTotal', 0)
        if total:
            stats["ram_pct"] = f"{(total - mem.get('MemAvailable', 0)) * 100 // total}%"
    except: pass
    try:
        r = subprocess.run(["df","-h","/"], capture_output=True, text=True, timeout=3)
        lines = r.stdout.splitlines()
        if len(lines)>1: stats["sd_pct"] = lines[1].split()[4]
    except: pass
    return stats

ANSI_RE    = re.compile(r'\x1b\[[0-9;]*m')
RF_RE      = re.compile(r'^\[0[L]?\]\s+(.+)$')
_DW_PKT_RE = re.compile(
    r'(?:Digipeating\s+)?'
    r'\[\d+(?:\.\d+[^\]]*?)?\]\s+'
    r'([A-Z0-9*-]+>[A-Z0-9,*-]+:.+)',
    re.IGNORECASE,
)

_sys.path.insert(0, "/opt/aprs-lite")
try:
    from aprs_decoder  import decode_packet
    from station_tracker import StationTracker, bearing_cardinal, time_ago
    from dedup_filter  import DeduplicationFilter
    _APRS_OK = True
except ImportError:
    _APRS_OK = False

try:
    from geo_renderer import draw_geo, bbox_500km, geo_available
    _GEO_OK = True
except ImportError:
    _GEO_OK = False
    def geo_available(): return False
    def bbox_500km(lat, lon): return lat-4.5, lat+4.5, lon-6.0, lon+6.0
    def draw_geo(canvas, *a): pass


def make_weather_packet(callsign, lat, lon, data):
    ld, lm = int(abs(lat)), (abs(lat) % 1) * 60
    od, om = int(abs(lon)), (abs(lon) % 1) * 60
    ls  = f"{ld:02d}{lm:05.2f}{'N' if lat >= 0 else 'S'}"
    os_ = f"{od:03d}{om:05.2f}{'E' if lon >= 0 else 'W'}"
    tf  = round(data["temperature"] * 9 / 5 + 32)
    hh  = int(data["humidity"]) % 100
    bp  = min(99999, round(data["pressure"] * 10))
    wx  = f"c...s...g...t{tf:03d}h{hh:02d}b{bp:05d}"
    extras = []
    if data.get("iaq", -1) >= 0:
        extras.append(f"IAQ={data['iaq']:.0f}/{data.get('iaq_accuracy',0)}")
    if data.get("co2_eq", -1) > 0:
        extras.append(f"CO2={data['co2_eq']:.0f}ppm")
    if data.get("voc_eq", -1) > 0:
        extras.append(f"VOC={data['voc_eq']:.2f}ppm")
    if extras:     wx += " " + " ".join(extras)
    elif data.get("gas"): wx += f" Gas:{data['gas']}ohm"
    return f"{callsign}>APNW01,WIDE1-1:!{ls}/{os_}_{wx}"

def make_packet():
    cfg = load_config()
    callsign = cfg.get("CALLSIGN","F5ZVO"); comment = cfg.get("COMMENT","Relais APRS")
    try:
        lat=float(cfg.get("LAT","42.9783")); lon=float(cfg.get("LON","-0.7493"))
        ld,lm=int(abs(lat)),(abs(lat)%1)*60; od,om=int(abs(lon)),(abs(lon)%1)*60
        ls=f"{ld:02d}{lm:05.2f}{'N' if lat>=0 else 'S'}"; os_=f"{od:03d}{om:05.2f}{'E' if lon>=0 else 'W'}"
        return f"{callsign}>APNW01,WIDE1-1:!{ls}/{os_}# {comment}"
    except: return f"{callsign}>APNW01,WIDE1-1:!4258.70N/00044.96W# {comment}"

_SYMBOL_LABELS = {
    ">": "Mobile",  "-": "QTH",     "#": "Digipeat", "_": "Météo",
    "^": "Avion",   "=": "Train",   "k": "Camion",   "[": "Piéton",
    "b": "Vélo",    "8": "Bateau",  "Y": "Voilier",  "R": "Urgence",
    "g": "Ballon",  "U": "Bus",     "j": "Jeep",     "/": "Point",
    "s": "Nav.",    "O": "Ballon",
}

def _stn_type(sym: str|None, info_type: str) -> str:
    if info_type == "weather": return "Météo"
    if info_type == "object":  return "Objet"
    if info_type == "message": return "Message"
    return _SYMBOL_LABELS.get(sym or "", "Station")


class StatusPanel(Static):
    _direwolf_status="..."; _internet_status="..."; _last_frame="—"
    _aioc_status="..."; _uptime="..."; _cpu_temp="?"; _ram_pct="?"; _sd_pct="?"
    _rf_rx=0; _rf_tx=0; _is_rx=0; _beacons=0

    def compose(self):
        yield Static("", id="status_text")

    def on_mount(self): self._refresh_display()

    def _render_status(self):
        cfg=load_config(); cs=cfg.get("CALLSIGN","?")
        dc="green" if self._direwolf_status=="active" else "red"
        nc="green" if "OK" in self._internet_status else "yellow"
        ac="green" if "OK" in self._aioc_status else "red"
        return (
            f"[bold white]APRS-AIOC-RELAY-LITE v1.0.7[/] [dim]— {cs}[/]\n"
            f"[{dc}]DW:{self._direwolf_status}[/]  [{ac}]AIOC:{self._aioc_status}[/]  [{nc}]Net:{self._internet_status}[/]\n"
            f"[dim]Trame:[/] [cyan]{self._last_frame}[/]\n"
            f"[dim]Up:[/]{self._uptime}  [dim]CPU:[/]{self._cpu_temp}  [dim]RAM:[/]{self._ram_pct}  [dim]SD:[/]{self._sd_pct}  "
            f"[dim]RF-RX:[/]{self._rf_rx} [dim]RF-TX:[/]{self._rf_tx} [dim]IS-RX:[/]{self._is_rx} [dim]BCN:[/]{self._beacons}"
        )

    def _refresh_display(self):
        try: self.query_one("#status_text", Static).update(self._render_status())
        except: pass

    def set_direwolf_status(self,v): self._direwolf_status=v; self._refresh_display()
    def set_internet_status(self,v): self._internet_status=v; self._refresh_display()
    def set_last_frame(self,v): self._last_frame=v; self._refresh_display()
    def set_aioc_status(self,v): self._aioc_status=v; self._refresh_display()
    def set_uptime(self,v): self._uptime=v; self._refresh_display()
    def set_sys_stats(self,cpu,ram,sd): self._cpu_temp=cpu; self._ram_pct=ram; self._sd_pct=sd; self._refresh_display()
    def _update_stats(self):
        try:
            self.app.query_one("#stats_panel", Static).update(
                f"RF-RX : {self._rf_rx}\nRF-TX : {self._rf_tx}\nIS-RX : {self._is_rx}\nBeacons : {self._beacons}"
            )
        except Exception:
            pass

    def inc_rf_rx(self): self._rf_rx+=1; self._refresh_display(); self._update_stats()
    def inc_rf_tx(self): self._rf_tx+=1; self._refresh_display(); self._update_stats()
    def inc_is_rx(self): self._is_rx+=1; self._refresh_display(); self._update_stats()
    def inc_beacon(self): self._beacons+=1; self._refresh_display(); self._update_stats()

class ConfigPanel(Static):
    def compose(self):
        cfg=load_config()
        yield Label("[bold]Configuration Direwolf[/]"); yield Rule()
        yield Label("Indicatif (MYCALL)"); yield Input(value=cfg.get("CALLSIGN",""), id="cfg_callsign", placeholder="F5ZVO")
        yield Label("TXDELAY (x10ms)"); yield Input(value=cfg.get("TXDELAY","50"), id="cfg_txdelay", placeholder="50")
        yield Label("Latitude decimale"); yield Input(value=cfg.get("LAT",""), id="cfg_lat", placeholder="42.9783")
        yield Label("Longitude decimale"); yield Input(value=cfg.get("LON",""), id="cfg_lon", placeholder="-0.7493")
        yield Label("Commentaire beacon"); yield Input(value=cfg.get("COMMENT",""), id="cfg_comment", placeholder="iGate/Digi ARPA")
        yield Label("ADEVICE ALSA"); yield Input(value=cfg.get("ADEVICE","plughw:AllInOneCable,0"), id="cfg_adevice")
        yield Label("PTT"); yield Input(value=cfg.get("PTT","CM108 /dev/hidraw0"), id="cfg_ptt")
        yield Label("Palette couleurs Direwolf (-t)")
        yield Select(options=COLOR_OPTIONS, value=get_direwolf_color(), id="cfg_color")
        yield Rule()
        yield Label("[bold]Capteur météo BME280 / BME680 / BME688[/]")
        yield Label("Lecture capteur activée")
        yield Select(options=[("Oui","1"),("Non","0")],
                     value=cfg.get("SENSOR_ENABLED","1"), id="cfg_sensor_enabled")
        yield Label("Diffusion APRS — beacon WX (/ _)")
        yield Select(options=[("Oui — envoi RF + relai iGate","1"),("Non — lecture seule","0")],
                     value=cfg.get("SENSOR_TX_APRS","1"), id="cfg_sensor_tx")
        yield Label("Intervalle beacon météo (5 – 60 min)")
        yield Input(value=cfg.get("SENSOR_INTERVAL","10"), id="cfg_sensor_interval", placeholder="10")
        yield Label("Adresse I2C capteur")
        yield Select(options=[("Auto-détection","auto"),
                               ("0x76 — SDO à la masse (défaut)","0x76"),
                               ("0x77 — SDO sur 3.3V","0x77")],
                     value=cfg.get("SENSOR_I2C_ADDR","auto"), id="cfg_sensor_addr")
        yield Rule()
        yield Button("Sauvegarder et redemarrer Direwolf", id="btn_save_config", variant="success")
        yield Static("", id="cfg_status")

    def on_button_pressed(self, event):
        if event.button.id == "btn_save_config": self._save()

    def _save(self):
        status = self.query_one("#cfg_status", Static)
        fields={"CALLSIGN":"cfg_callsign","TXDELAY":"cfg_txdelay","LAT":"cfg_lat","LON":"cfg_lon","COMMENT":"cfg_comment","ADEVICE":"cfg_adevice","PTT":"cfg_ptt"}
        cfg={}
        for key,wid in fields.items():
            try: val=self.query_one(f"#{wid}",Input).value.strip(); cfg[key]=val; save_config(key,val)
            except: pass
        if cfg.get("CALLSIGN"): save_direwolf("MYCALL",cfg["CALLSIGN"]); save_direwolf("IGLOGIN",f"{cfg['CALLSIGN']} {load_config().get('PASSCODE','0')}")
        if cfg.get("TXDELAY"): save_direwolf("TXDELAY",cfg["TXDELAY"])
        if cfg.get("ADEVICE"): save_direwolf("ADEVICE",cfg["ADEVICE"])
        if cfg.get("PTT"): save_direwolf("PTT",cfg["PTT"])
        try:
            color_val = self.query_one("#cfg_color", Select).value
            if color_val and color_val is not Select.BLANK:
                save_direwolf_color(str(color_val))
        except: pass
        if cfg.get("LAT") and cfg.get("LON"):
            lat=float(cfg["LAT"]); lon=float(cfg["LON"])
            ld,lm=int(abs(lat)),(abs(lat)%1)*60; od,om=int(abs(lon)),(abs(lon)%1)*60
            ls=f"{ld:02d}{lm:05.2f}{'N' if lat>=0 else 'S'}"; os_=f"{od:03d}{om:05.2f}{'E' if lon>=0 else 'W'}"
            save_direwolf("PBEACON",f'delay=1 every=30 overlay=R symbol="digi" lat={ls} long={os_} power=10 height=10 gain=5 dir=0 comment="{cfg.get("COMMENT","Relais APRS")}"')
        for key, wid in [("SENSOR_ENABLED","cfg_sensor_enabled"),
                          ("SENSOR_TX_APRS","cfg_sensor_tx"),
                          ("SENSOR_I2C_ADDR","cfg_sensor_addr")]:
            try:
                v = self.query_one(f"#{wid}", Select).value
                if v and v is not Select.BLANK: save_config(key, str(v))
            except: pass
        try:
            iv = self.query_one("#cfg_sensor_interval", Input).value.strip()
            if iv:
                if iv.isdigit() and 5 <= int(iv) <= 60:
                    save_config("SENSOR_INTERVAL", iv)
                else:
                    status.update("[yellow]Intervalle capteur invalide — saisir 5 à 60 min[/]"); return
        except: pass
        if cfg.get("LAT") and cfg.get("LON"):
            try:
                app = self.app
                lat = float(cfg["LAT"]); lon = float(cfg["LON"])
                if hasattr(app, "_station_tracker") and app._station_tracker:
                    app._station_tracker.set_own_position(lat, lon)
                app._own_lat = lat
                app._own_lon = lon
            except: pass
        try:
            status.update("[yellow]Redemarrage Direwolf...[/]")
            subprocess.run(["sudo","systemctl","restart","aprs-direwolf"],timeout=15)
            time.sleep(2); st=service_status("aprs-direwolf")
            status.update("[green]OK — Direwolf actif[/]" if st=="active" else f"[red]Direwolf:{st}[/]")
        except Exception as e: status.update(f"[red]Erreur:{e}[/]")

class WiFiPanel(Static):
    def compose(self):
        yield Label("[bold]Reseaux WiFi[/]"); yield Rule()
        yield Button("Scanner les reseaux", id="btn_wifi_scan", variant="primary")
        yield Static("Appuyez sur Scanner...", id="wifi_list"); yield Rule()
        yield Label("SSID"); yield Input(id="wifi_ssid", placeholder="Nom du reseau")
        yield Label("Mot de passe"); yield Input(id="wifi_password", placeholder="Mot de passe", password=True)
        yield Button("Connecter", id="btn_wifi_connect", variant="success")
        yield Button("Deconnecter", id="btn_wifi_disconnect", variant="warning")
        yield Static("", id="wifi_status")

    def on_button_pressed(self, event):
        if event.button.id=="btn_wifi_scan": self._scan()
        elif event.button.id=="btn_wifi_connect": self._connect()
        elif event.button.id=="btn_wifi_disconnect": self._disconnect()

    @work(thread=True)
    def _scan(self):
        self.app.call_from_thread(self.query_one("#wifi_list",Static).update,"[yellow]Scan en cours...[/]")
        try:
            r=subprocess.run(["sudo","iwlist","wlan0","scan"],capture_output=True,text=True,timeout=15)
            essids=sorted(set(m.group(1) for m in re.finditer(r'ESSID:"(.+?)"',r.stdout)))
            cur=subprocess.run(["iwgetid","wlan0","--raw"],capture_output=True,text=True,timeout=3).stdout.strip()
            txt="\n".join(f"{i}. {s}{' [green](connecte)[/]' if s==cur else ''}" for i,s in enumerate(essids,1)) if essids else "[dim]Aucun reseau[/]"
            self.app.call_from_thread(self.query_one("#wifi_list",Static).update,txt)
        except Exception as e: self.app.call_from_thread(self.query_one("#wifi_list",Static).update,f"[red]Erreur:{e}[/]")

    @work(thread=True)
    def _connect(self):
        try: ssid=self.query_one("#wifi_ssid",Input).value.strip(); pw=self.query_one("#wifi_password",Input).value.strip()
        except: return
        if not ssid: self.app.call_from_thread(self.query_one("#wifi_status",Static).update,"[red]SSID requis[/]"); return
        self.app.call_from_thread(self.query_one("#wifi_status",Static).update,"[yellow]Connexion...[/]")
        conf=f'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\ncountry=FR\n\nnetwork={{\n    ssid="{ssid}"\n    psk="{pw}"\n}}\n'
        try:
            subprocess.run(["sudo","tee","/etc/wpa_supplicant/wpa_supplicant.conf"],input=conf,text=True,capture_output=True,timeout=5)
            subprocess.run(["sudo","wpa_cli","-i","wlan0","reconfigure"],capture_output=True,timeout=10)
            for _ in range(12):
                time.sleep(1)
                cur=subprocess.run(["iwgetid","wlan0","--raw"],capture_output=True,text=True,timeout=3).stdout.strip()
                if cur==ssid: self.app.call_from_thread(self.query_one("#wifi_status",Static).update,f"[green]Connecte a {ssid}[/]"); return
            self.app.call_from_thread(self.query_one("#wifi_status",Static).update,"[red]Echec — SSID/MDP incorrect ?[/]")
        except Exception as e: self.app.call_from_thread(self.query_one("#wifi_status",Static).update,f"[red]Erreur:{e}[/]")

    @work(thread=True)
    def _disconnect(self):
        try:
            subprocess.run(["sudo","ip","link","set","wlan0","down"],timeout=5); time.sleep(1)
            subprocess.run(["sudo","ip","link","set","wlan0","up"],timeout=5)
            self.app.call_from_thread(self.query_one("#wifi_status",Static).update,"[yellow]Deconnecte[/]")
        except Exception as e: self.app.call_from_thread(self.query_one("#wifi_status",Static).update,f"[red]Erreur:{e}[/]")

class ClockPanel(Static):
    def compose(self):
        yield Label("[bold]Synchronisation NTP[/]"); yield Rule()
        yield Static("", id="ntp_status")
        yield Button("Synchroniser via internet", id="btn_ntp_sync", variant="primary"); yield Rule()
        yield Label("[bold]Reglage manuel[/]")
        yield Label("Format : YYYY-MM-DD HH:MM:SS")
        yield Input(id="clock_input", placeholder="2026-05-03 00:00:00")
        yield Button("Appliquer", id="btn_clock_set", variant="warning")
        yield Static("", id="clock_status")

    def on_mount(self): self._refresh_ntp()

    def on_button_pressed(self, event):
        if event.button.id=="btn_ntp_sync": self._sync_ntp()
        elif event.button.id=="btn_clock_set": self._set_clock()

    def _refresh_ntp(self):
        try:
            r=subprocess.run(["timedatectl","status"],capture_output=True,text=True,timeout=5)
            lines=[l.strip() for l in r.stdout.splitlines() if any(k in l for k in ("Local time","NTP service","RTC time","System clock"))]
            try: self.query_one("#ntp_status",Static).update("\n".join(lines) or r.stdout.strip())
            except: pass
        except: pass

    @work(thread=True)
    def _sync_ntp(self):
        self.app.call_from_thread(self.query_one("#clock_status",Static).update,"[yellow]Synchronisation...[/]")
        try:
            subprocess.run(["sudo","systemctl","restart","systemd-timesyncd"],timeout=10)
            time.sleep(3); self.app.call_from_thread(self._refresh_ntp)
            self.app.call_from_thread(self.query_one("#clock_status",Static).update,"[green]NTP relance[/]")
        except Exception as e: self.app.call_from_thread(self.query_one("#clock_status",Static).update,f"[red]Erreur:{e}[/]")

    @work(thread=True)
    def _set_clock(self):
        try: val=self.query_one("#clock_input",Input).value.strip()
        except: return
        if not re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$',val):
            self.app.call_from_thread(self.query_one("#clock_status",Static).update,"[red]Format invalide — ex: 2026-05-03 23:00:00[/]"); return
        try:
            subprocess.run(["sudo","date","-s",val],capture_output=True,timeout=5)
            self.app.call_from_thread(self._refresh_ntp)
            self.app.call_from_thread(self.query_one("#clock_status",Static).update,f"[green]Heure regle : {val}[/]")
        except Exception as e: self.app.call_from_thread(self.query_one("#clock_status",Static).update,f"[red]Erreur:{e}[/]")

class MeteoPanel(Static):
    def compose(self):
        yield Label("[bold]Station Météo — BME280 / BME680 / BME688[/]"); yield Rule()
        yield Static("[dim]Recherche capteur I2C...[/]", id="meteo_chip")
        yield Rule()
        yield Static("", id="meteo_readings")
        yield Static("", id="meteo_iaq")
        yield Rule()
        yield Static("[dim]Prochain beacon WX : —[/]", id="meteo_countdown")
        yield Static("[dim]Dernier paquet     : —[/]", id="meteo_last_pkt")
        yield Rule()
        yield Button("Envoyer beacon météo maintenant", id="btn_meteo_now", variant="primary")
        yield Static("", id="meteo_tx_status")

    def on_button_pressed(self, event):
        if event.button.id != "btn_meteo_now": return
        cfg = load_config()
        if cfg.get("SENSOR_TX_APRS","1") == "0":
            self.query_one("#meteo_tx_status",Static).update("[yellow]Diffusion APRS désactivée — activer dans l'onglet Config[/]"); return
        if cfg.get("SENSOR_ENABLED","1") == "0":
            self.query_one("#meteo_tx_status",Static).update("[yellow]Capteur désactivé — activer dans l'onglet Config[/]"); return
        try: self.app._send_weather_now()
        except Exception as e: self.query_one("#meteo_tx_status",Static).update(f"[red]{e}[/]")

    def update_sensor(self, data: dict, chip: str, ts: str):
        t, h, p = data["temperature"], data["humidity"], data["pressure"]
        tf = round(t * 9 / 5 + 32)
        try: self.query_one("#meteo_chip",Static).update(f"[bold green]{chip}[/]  [dim]— mesure {ts}[/]")
        except: pass
        try: self.query_one("#meteo_readings",Static).update(
            f"[dim]Température :[/] [bold cyan]{t} °C[/]  [dim]({tf} °F)[/]\n"
            f"[dim]Humidité    :[/] [bold cyan]{h} %[/]\n"
            f"[dim]Pression    :[/] [bold cyan]{p} hPa[/]")
        except: pass
        iaq = data.get("iaq", -1)
        if iaq is not None and iaq >= 0:
            try:
                from bsec_iaq import iaq_label
                label, color = iaq_label(iaq)
            except Exception:
                label, color = "", "cyan"
            acc     = data.get("iaq_accuracy", 0)
            acc_lbl = ["En calibration…","Faible","Moyenne","Haute"][min(acc, 3)]
            acc_col = "green" if acc == 3 else ("yellow" if acc >= 2 else "red")
            filled  = max(0, min(20, round(iaq / 500 * 20)))
            gauge   = "█" * filled + "░" * (20 - filled)
            acc_bar = "█" * acc + "░" * (3 - acc)
            iaq_txt = (
                f"[dim]── Qualité de l'air (BSEC) ─────────────[/]\n"
                f"[dim]IAQ         :[/] [{color}]{iaq:.0f}  {label}[/]\n"
                f"[dim]Indice      :[/] [{color}]{gauge}[/] [dim]0▸500[/]\n"
                f"[dim]Calibration :[/] [{acc_col}]{acc_bar} {acc}/3 — {acc_lbl}[/]"
            )
            if data.get("co2_eq", -1) > 0:
                iaq_txt += f"\n[dim]CO₂ éq.    :[/] [cyan]{data['co2_eq']:.0f} ppm[/]"
            if data.get("voc_eq", -1) > 0:
                iaq_txt += f"\n[dim]VOC éq.    :[/] [cyan]{data['voc_eq']:.2f} ppm[/]"
        elif data.get("gas"):
            iaq_txt = (f"[dim]── Gaz (résistance brute) ──────────────[/]\n"
                       f"[dim]Résistance  :[/] [cyan]{data['gas']} Ω[/]\n"
                       f"[dim]BSEC non actif — valeur non calibrée[/]")
        else:
            iaq_txt = ""
        try: self.query_one("#meteo_iaq",Static).update(iaq_txt)
        except: pass

    def set_countdown(self, seconds: int):
        if seconds <= 0: txt = "[dim]Prochain beacon WX :[/] [green]mesure en cours…[/]"
        else:
            m, s = divmod(seconds, 60)
            txt = f"[dim]Prochain beacon WX :[/] [yellow]dans {m}m {s:02d}s[/]"
        try: self.query_one("#meteo_countdown",Static).update(txt)
        except: pass

    def set_last_packet(self, packet: str, ok: bool, ts: str):
        color = "green" if ok else "red"
        body  = packet.split(":", 1)[1] if ":" in packet else packet
        if len(body) > 62: body = body[:59] + "…"
        try: self.query_one("#meteo_last_pkt",Static).update(
            f"[dim]Dernier paquet     :[/] [{color}]{body}[/]\n[dim]                     {ts}[/]")
        except: pass

    def set_tx_status(self, text: str):
        try: self.query_one("#meteo_tx_status",Static).update(text)
        except: pass

    def set_chip_status(self, text: str):
        try: self.query_one("#meteo_chip",Static).update(text)
        except: pass

    def set_error(self, text: str):
        try:
            self.query_one("#meteo_chip",Static).update("[red]Capteur non disponible[/]")
            self.query_one("#meteo_readings",Static).update(f"[red]{text}[/]")
            self.query_one("#meteo_iaq",Static).update("")
        except: pass


# ── StationsPanel ─────────────────────────────────────────────
class StationsPanel(Static):
    def compose(self):
        yield Label("[bold]Stations entendues en RF[/]"); yield Rule()
        yield Static("[dim]En attente de trames Direwolf...[/]", id="stn_header")
        yield DataTable(id="stn_table", zebra_stripes=True, cursor_type="row")

    def on_mount(self):
        t = self.query_one("#stn_table", DataTable)
        t.add_columns("Callsign", "Vu il y a", "Distance", "Cap", "Pkts", "Type", "Commentaire")
        if not _APRS_OK:
            try: self.query_one("#stn_header", Static).update(
                "[yellow]aprslib manquant — installer dans le venv :[/]\n"
                "[dim]/opt/aprs-lite/venv/bin/pip install aprslib[/]")
            except: pass

    def refresh_table(self, stations: list) -> None:
        t = self.query_one("#stn_table", DataTable)
        total    = len(stations)
        with_pos = sum(1 for s in stations if s.latitude is not None)
        try: self.query_one("#stn_header", Static).update(
            f"[dim]Total :[/] [bold cyan]{total}[/] [dim]stations  ·  "
            f"{with_pos} avec position GPS[/]  [dim](actualisation auto toutes les 10 s)[/]")
        except: pass
        try:
            t.clear()
            for s in stations:
                dist = f"{s.distance_km:.1f} km" if s.distance_km is not None else "?"
                cap  = bearing_cardinal(s.bearing) if s.bearing is not None else "?"
                t.add_row(s.callsign, time_ago(s.last_heard), dist, cap,
                          str(s.packet_count), _stn_type(s.symbol_code, s.last_info_type),
                          (s.comment or "")[:35])
        except: pass


# ── BrailleCanvas ──────────────────────────────────────────────
# Braille dot layout (2 cols x 4 rows per char cell):
#   dot1(bit0) dot4(bit3)
#   dot2(bit1) dot5(bit4)
#   dot3(bit2) dot6(bit5)
#   dot7(bit6) dot8(bit7)
_BRAILLE_BITS = [[0, 1, 2, 6], [3, 4, 5, 7]]   # [dot_col][dot_row]

class BrailleCanvas:
    """Grille braille Unicode U+2800-U+28FF. Chaque char terminal = 2x4 dots."""

    def __init__(self, char_cols: int, char_rows: int):
        self._cw   = max(1, char_cols)
        self._ch   = max(1, char_rows)
        self._data = bytearray(self._cw * self._ch)

    def clear(self):
        self._data[:] = b'\x00' * len(self._data)

    def set_dot(self, px: int, py: int):
        char_x, dot_x = divmod(px, 2)
        char_y, dot_y = divmod(py, 4)
        if 0 <= char_x < self._cw and 0 <= char_y < self._ch:
            self._data[char_y * self._cw + char_x] |= 1 << _BRAILLE_BITS[dot_x][dot_y]

    def render(self) -> str:
        rows = []
        for r in range(self._ch):
            off = r * self._cw
            rows.append("".join(chr(0x2800 + b) for b in self._data[off:off + self._cw]))
        return "\n".join(rows)


# ── MapPanel ───────────────────────────────────────────────────
class MapPanel(Static):
    """Carte braille stations RF. Projection equirectangulaire, auto-zoom."""
    _stations = []
    _own_lat  = None
    _own_lon  = None

    def compose(self):
        yield Label("[bold]Carte des stations RF[/]")
        yield Rule()
        yield Static("[dim]En attente de positions...[/]", id="map_header")
        yield Static("", id="map_canvas")

    def refresh_map(self, stations: list, own_lat, own_lon) -> None:
        self._stations = [s for s in stations if s.latitude is not None]
        self._own_lat  = own_lat
        self._own_lon  = own_lon
        self._render_map()

    def _render_map(self) -> None:
        if self._own_lat is None:
            try: self.query_one("#map_canvas", Static).update(
                "[dim]Position propre non configurée (LAT/LON dans Config)[/]")
            except: pass
            return
        try:
            cv = self.query_one("#map_canvas", Static)
            cw = cv.size.width  if cv.size.width  > 4 else 60
            ch = cv.size.height if cv.size.height > 2 else 18
        except Exception:
            cw, ch = 60, 18

        # Bounding box fixe 500 km autour de la position propre
        min_lat, max_lat, min_lon, max_lon = bbox_500km(self._own_lat, self._own_lon)
        lat_span = max_lat - min_lat
        lon_span = max_lon - min_lon
        dot_w = cw * 2; dot_h = ch * 4

        stns  = self._stations; total = len(stns)
        geo_hint = " · geo" if geo_available() else ""
        try:
            self.query_one("#map_header", Static).update(
                f"[dim]{total} station(s) dans 500 km[/]"
                f"  [dim](braille {cw}x{ch}{geo_hint})[/]")
        except Exception: pass

        canvas = BrailleCanvas(cw, ch)

        # Fond géographique (coastlines, frontières, villes)
        if _GEO_OK and geo_available():
            try: draw_geo(canvas, min_lat, max_lat, min_lon, max_lon)
            except Exception: pass

        def to_dot(lat, lon):
            x = int((lon - min_lon) / lon_span * (dot_w - 1))
            y = int((1.0 - (lat - min_lat) / lat_span) * (dot_h - 1))
            return x, y

        # Stations RF (bloc 3x3 dots)
        for s in stns:
            if not (min_lat <= s.latitude <= max_lat and min_lon <= s.longitude <= max_lon):
                continue
            x, y = to_dot(s.latitude, s.longitude)
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    canvas.set_dot(x + dx, y + dy)

        # Position propre — croix 9 dots
        ox, oy = to_dot(self._own_lat, self._own_lon)
        for d in range(-4, 5):
            canvas.set_dot(ox + d, oy); canvas.set_dot(ox, oy + d)

        try: self.query_one("#map_canvas", Static).update(canvas.render())
        except: pass


# ── MessagesPanel ─────────────────────────────────────────────
class MessagesPanel(Static):
    """Messagerie APRS P2P : envoi, ack tracking, retry, réception."""

    def compose(self):
        yield Label("[bold]Nouveau message[/]")
        with Horizontal(id="msg_compose_row"):
            yield Input(placeholder="A (ex: F0CALL-9)", id="msg_to", max_length=9)
            yield Input(placeholder="Message (67 car. max)", id="msg_text", max_length=67)
            yield Button("Envoyer", id="btn_msg_send", variant="primary")
        yield Static("", id="msg_send_status")
        yield Rule()
        yield Label("[bold]Envoyés[/]", id="msg_out_label")
        yield DataTable(id="msg_out_table", zebra_stripes=True, cursor_type="none")
        yield Rule()
        yield Label("[bold]Reçus[/]", id="msg_in_label")
        yield DataTable(id="msg_in_table", zebra_stripes=True, cursor_type="row")

    def on_mount(self):
        out = self.query_one("#msg_out_table", DataTable)
        out.add_columns("N°", "A", "Message", "Statut", "Essais", "il y a")
        inn = self.query_one("#msg_in_table", DataTable)
        inn.add_columns("De", "A", "Message", "Reçu il y a")

    def refresh_messages(self, inbound: list, outbound: list) -> None:
        # label reçus
        try: self.query_one("#msg_in_label", Label).update(f"[bold]Reçus[/] [dim]({len(inbound)})[/]")
        except: pass
        inn = self.query_one("#msg_in_table", DataTable)
        inn.clear()
        for mono, pkt in reversed(inbound):
            inn.add_row((pkt.source or "?")[:9], (pkt.addressee or "?")[:9],
                        (pkt.message_text or "")[:55], time_ago(mono))
        # label envoyés
        try: self.query_one("#msg_out_label", Label).update(f"[bold]Envoyés[/] [dim]({len(outbound)})[/]")
        except: pass
        out = self.query_one("#msg_out_table", DataTable)
        out.clear()
        for tm in reversed(outbound):
            state_style = {
                MsgState.ACKED:    f"[green]{tm.state.value}[/]",
                MsgState.FAILED:   f"[red]{tm.state.value}[/]",
                MsgState.REJECTED: f"[red]{tm.state.value}[/]",
            }.get(tm.state, f"[yellow]{tm.state.value}[/]")
            out.add_row(tm.msg_id, tm.addressee, tm.text[:40], state_style,
                        str(tm.attempts), time_ago(tm.created))

    def set_send_status(self, msg: str) -> None:
        try: self.query_one("#msg_send_status", Static).update(msg)
        except: pass


# ── Application principale ────────────────────────────────────
class APRSLiteApp(App):
    CSS = """
    Screen { background: #0a0e14; }
    StatusPanel { height: 5; border: solid #1e2d40; padding: 0 1; margin-bottom: 1; }
    #log_panel { border: solid #1e2d40; height: 1fr; }
    #sidebar { width: 30; border: solid #1e2d40; padding: 1; margin-left: 1; }
    #sidebar Button { width: 100%; margin-top: 1; }
    #stats_panel { margin-top: 1; color: #8899aa; }
    ConfigPanel, WiFiPanel, ClockPanel, MeteoPanel { padding: 1; overflow-y: auto; }
    ConfigPanel Button, WiFiPanel Button, ClockPanel Button, MeteoPanel Button { width: 100%; margin-top: 1; }
    StationsPanel { padding: 1; overflow-y: auto; }
    #stn_header { margin-bottom: 1; color: #8899aa; }
    #stn_table { height: 1fr; }
    MapPanel { padding: 1; overflow-y: auto; }
    #map_header { margin-bottom: 1; color: #8899aa; }
    #map_canvas { height: 1fr; }
    MessagesPanel { padding: 1; overflow-y: auto; }
    #msg_compose_row { height: 3; margin-bottom: 1; }
    #msg_compose_row Input { width: 1fr; margin-right: 1; }
    #msg_compose_row #msg_to { max-width: 14; }
    #msg_compose_row Button { width: 12; }
    #msg_send_status { height: 1; color: #8899aa; margin-bottom: 1; }
    #msg_out_table { height: 8; }
    #msg_in_table { height: 1fr; }
    """
    BINDINGS = [
        Binding("q","detach","Detacher"), Binding("x","quit","Quitter"), Binding("b","send_beacon","Beacon"),
        Binding("r","restart_direwolf","Restart DW"), Binding("p","toggle_pause","Pause logs"),
    ]
    _paused=False; _last_frame=""; _aioc_was_ok=None; _log_lines=0
    _sensor_data=None; _sensor_chip=""; _next_beacon_ts=0.0
    _stopping=False; _journal_proc=None; _sensor_obj=None
    _station_tracker=None; _dedup=None; _stations_dirty=False
    _aprs_messages=[]; _messages_dirty=False; _own_lat=None; _own_lon=None
    _msg_tracked={}; _msg_next_id=1; _msg_dirty=False; _msg_lock=None

    def compose(self):
        yield Header(show_clock=True)
        with TabbedContent(initial="tab_main"):
            with TabPane("Principal",  id="tab_main"):
                with Horizontal():
                    with Vertical():
                        yield StatusPanel(id="status_panel")
                        yield Log(id="log_panel", highlight=True)
                    with Vertical(id="sidebar"):
                        yield Label("[bold]Actions[/]"); yield Rule()
                        yield Button("Beacon", id="btn_beacon", variant="primary")
                        yield Button("Restart Direwolf", id="btn_restart", variant="warning")
                        yield Button("Detecter AIOC", id="btn_detect", variant="default")
                        yield Rule()
                        yield Label("[bold]Statistiques trames[/]")
                        yield Static("RF-RX : 0\nRF-TX : 0\nIS-RX : 0\nBeacons : 0", id="stats_panel")
            with TabPane("Stations",   id="tab_stations"):
                yield StationsPanel(id="stations_panel")
            with TabPane("Carte",      id="tab_map"):
                yield MapPanel(id="map_panel")
            with TabPane("Messages",   id="tab_messages"):
                yield MessagesPanel(id="messages_panel")
            with TabPane("Config",     id="tab_config"):
                yield ConfigPanel(id="config_panel")
            with TabPane("WiFi",       id="tab_wifi"):
                yield WiFiPanel(id="wifi_panel")
            with TabPane("Horloge",    id="tab_clock"):
                yield ClockPanel(id="clock_panel")
            with TabPane("Météo",      id="tab_meteo"):
                yield MeteoPanel(id="meteo_panel")
        yield Footer()

    def on_mount(self):
        self.title = "APRS-AIOC-RELAY-LITE v1.0.10"
        self._aprs_messages = []
        self._msg_tracked   = {}
        self._msg_next_id   = 1
        self._msg_lock      = threading.Lock()
        self._init_stations()
        self._poll_status(); self._follow_journal(); self._run_aioc_detect(); self._sensor_worker()
        self.set_interval(10, self._refresh_panels)

    def _init_stations(self) -> None:
        if not _APRS_OK:
            return
        try:
            cfg = load_config()
            lat = float(cfg.get("LAT", "48.8566"))
            lon = float(cfg.get("LON", "2.3522"))
            self._station_tracker = StationTracker(own_lat=lat, own_lon=lon)
            self._dedup           = DeduplicationFilter(window=30.0)
            self._own_lat         = lat
            self._own_lon         = lon
        except Exception:
            pass

    def _refresh_panels(self) -> None:
        if not _APRS_OK or self._station_tracker is None:
            return
        stns = []
        try: stns = self._station_tracker.get_stations(sort_by="last_heard")
        except Exception: pass

        # Stations + Messages : seulement si nouvelles données
        if (self._station_tracker.count > 0 or self._stations_dirty
                or self._messages_dirty or self._aprs_messages or self._msg_dirty):
            self._stations_dirty = False; self._messages_dirty = False
            self._msg_dirty = False
            try:
                self.query_one("#stations_panel", StationsPanel).refresh_table(stns)
            except Exception: pass
            try:
                with self._msg_lock:
                    outbound = list(self._msg_tracked.values())
                self.query_one("#messages_panel", MessagesPanel).refresh_messages(
                    self._aprs_messages, outbound)
            except Exception: pass

        # Carte : toujours si la position propre est connue (fond géo + croix)
        if self._own_lat is not None:
            try:
                self.query_one("#map_panel", MapPanel).refresh_map(
                    stns, self._own_lat, self._own_lon)
            except Exception: pass

    @work(exclusive=True, thread=True)
    def _poll_status(self):
        while not self._stopping:
            try:
                sp=self.query_one("#status_panel",StatusPanel)
                self.call_from_thread(sp.set_direwolf_status, service_status("aprs-direwolf"))
                hi_r=subprocess.run(["hostname","-I"],capture_output=True,text=True,timeout=3)
                hi=hi_r.stdout.split()[0] if hi_r.stdout.split() else "-"
                ts_r=subprocess.run(["tailscale","ip","-4"],capture_output=True,text=True,timeout=3)
                ts=ts_r.stdout.splitlines()[0] if ts_r.stdout.splitlines() else "-"
                self.call_from_thread(sp.set_internet_status, f"{hi} TS:{ts}")
                r=subprocess.run(["uptime","-p"],capture_output=True,text=True,timeout=3)
                self.call_from_thread(sp.set_uptime, r.stdout.strip().replace("up ",""))
                s=get_system_stats()
                self.call_from_thread(sp.set_sys_stats, s["cpu_temp"], s["ram_pct"], s["sd_pct"])
            except: pass
            time.sleep(15)

    @work(exclusive=True, thread=True)
    def _follow_journal(self):
        while not self._stopping:
            try:
                proc=subprocess.Popen(
                    ["journalctl","-u","aprs-direwolf","-f","--output=cat","-n","50"],
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding="utf-8",errors="replace"
                )
                self._journal_proc = proc
                for line in proc.stdout:
                    if self._stopping: break
                    if self._paused: continue
                    line=line.strip()
                    if not line: continue
                    clean=ANSI_RE.sub("",line)
                    try:
                        sp=self.query_one("#status_panel",StatusPanel)
                        if RF_RE.match(clean):
                            if '[0L]' in line: self.call_from_thread(sp.inc_rf_tx)
                            else: self.call_from_thread(sp.inc_rf_rx)
                        elif re.match(r'^\[ig\]',clean): self.call_from_thread(sp.inc_is_rx)
                        if re.search(r'PBEACON|beacon',clean,re.IGNORECASE): self.call_from_thread(sp.inc_beacon)
                        m=RF_RE.match(clean)
                        if m: self._last_frame=m.group(1)[:65]; self.call_from_thread(sp.set_last_frame,self._last_frame)
                    except: pass

                    # Decodage APRS pour Stations / Carte / Messages
                    if _APRS_OK and self._dedup is not None and '[0L]' not in clean:
                        m2 = _DW_PKT_RE.search(clean)
                        if m2:
                            raw_pkt = m2.group(1).strip()
                            if not self._dedup.is_duplicate(raw_pkt):
                                try:
                                    pkt = decode_packet(raw_pkt, transport="RF")
                                    if not pkt.parse_error:
                                        if self._station_tracker:
                                            self._station_tracker.update(pkt)
                                            self._stations_dirty = True
                                        if pkt.info_type == "message":
                                            # ACK pour un de nos messages sortants
                                            if pkt.is_ack and pkt.message_id and self._msg_lock:
                                                with self._msg_lock:
                                                    tm = self._msg_tracked.get(pkt.message_id)
                                                    if tm and tm.state == MsgState.PENDING:
                                                        tm.state    = MsgState.ACKED
                                                        tm.acked_at = time.monotonic()
                                                        threading.Thread(
                                                            target=_chat_ack,
                                                            args=(pkt.source or "", pkt.message_id),
                                                            daemon=True,
                                                        ).start()
                                                self._msg_dirty = True
                                            # REJ pour un de nos messages sortants
                                            elif pkt.is_rej and pkt.message_id and self._msg_lock:
                                                with self._msg_lock:
                                                    tm = self._msg_tracked.get(pkt.message_id)
                                                    if tm and tm.state == MsgState.PENDING:
                                                        tm.state = MsgState.REJECTED
                                                        threading.Thread(
                                                            target=_chat_set_status,
                                                            args=(tm.row_id, "rejected"),
                                                            daemon=True,
                                                        ).start()
                                                self._msg_dirty = True
                                            # Message reçu adressé à nous → auto-ack + stockage DB
                                            elif not pkt.is_ack and not pkt.is_rej and pkt.message_text:
                                                cfg = load_config()
                                                mycall = cfg.get("CALLSIGN","").strip().upper()
                                                if mycall and (pkt.addressee or "").strip().upper() == mycall:
                                                    if pkt.message_id and pkt.source:
                                                        ack_info = encode_aprs_ack(pkt.source, pkt.message_id)
                                                        threading.Thread(
                                                            target=send_aprs_is, args=(ack_info,), daemon=True
                                                        ).start()
                                                    # Stocker en DB partagée
                                                    threading.Thread(
                                                        target=_chat_insert,
                                                        args=("in", pkt.source or "", mycall,
                                                              pkt.message_text, pkt.message_id or "", "rf"),
                                                        daemon=True,
                                                    ).start()
                                                self._aprs_messages.append((time.monotonic(), pkt))
                                                if len(self._aprs_messages) > _MSG_MAX:
                                                    self._aprs_messages = self._aprs_messages[-_MSG_MAX:]
                                                self._messages_dirty = True
                                except Exception: pass

                    if not self._stopping:
                        try:
                            log=self.query_one("#log_panel",Log)
                            self.call_from_thread(log.write_line, line)
                            self._log_lines+=1
                            if self._log_lines>LOG_MAX_LINES: self.call_from_thread(log.clear); self._log_lines=0
                        except: pass
                proc.wait()
            except: pass
            if not self._stopping: time.sleep(3)

    @work(exclusive=True, thread=True)
    def _run_aioc_detect(self):
        while not self._stopping:
            det=detect_aioc(); ok=det["ok"]
            try:
                sp=self.query_one("#status_panel",StatusPanel)
                self.call_from_thread(sp.set_aioc_status, f"OK card:{det['alsa_card']} {det['hidraw']}" if ok else "Non detecte")
                if self._aioc_was_ok is False and ok:
                    try: self.call_from_thread(self.query_one("#log_panel",Log).write_line,"[green]AIOC rebranché — redemarrage Direwolf...[/]")
                    except: pass
                    subprocess.run(["sudo","systemctl","restart","aprs-direwolf"],timeout=15)
                self._aioc_was_ok=ok
            except: pass
            time.sleep(30)

    @work(exclusive=True, thread=True)
    def _sensor_worker(self):
        import sys; sys.path.insert(0, "/opt/aprs-lite")
        sensor = None; chip = ""; last_addr_cfg = None
        while not self._stopping:
            cfg = load_config()
            if cfg.get("SENSOR_ENABLED","1") != "1":
                if not self._stopping:
                    try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).set_error,"Capteur désactivé — activer dans l'onglet Config")
                    except: pass
                time.sleep(30); continue
            try: interval = max(5, min(60, int(cfg.get("SENSOR_INTERVAL","10")))) * 60
            except: interval = 600
            addr_cfg = cfg.get("SENSOR_I2C_ADDR","auto")
            if sensor is not None and addr_cfg != last_addr_cfg:
                try: sensor.close()
                except: pass
                sensor = None
            if sensor is None:
                last_addr_cfg = addr_cfg
                try:
                    from bme_sensor import find_chip, BME280, BME680, SensorError
                    from bme_sensor import BME280_ID, BME68X_ID, CHIP_ID_REG
                    if addr_cfg == "auto":
                        addr, name = find_chip()
                    else:
                        import smbus2
                        addr = int(addr_cfg, 16)
                        bus  = smbus2.SMBus(1)
                        cid  = bus.read_byte_data(addr, CHIP_ID_REG)
                        bus.close()
                        if   cid == BME280_ID: name = "BME280"
                        elif cid == BME68X_ID: name = "BME68x"
                        else: raise SensorError(f"Chip inconnu ID=0x{cid:02X} à {addr_cfg}")
                    if name == "BME280":
                        sensor = BME280(addr); chip = "BME280"
                    else:
                        try:
                            from bsec_iaq import BsecReader
                            sensor = BsecReader(addr); chip = "BME68x+BSEC"
                        except Exception:
                            sensor = BME680(addr); chip = "BME68x"
                    self._sensor_chip = chip; self._sensor_obj = sensor
                    if not self._stopping:
                        try: self.call_from_thread(self.query_one("#log_panel",Log).write_line,f"[green]Capteur {chip} @ {hex(addr)} détecté[/]")
                        except: pass
                except Exception as e:
                    try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).set_error, str(e))
                    except: pass
                    time.sleep(60); continue
            # Lecture capteur boîtier (0x77) pour telemetry DB
            box_data = None
            try:
                import smbus2 as _smbus2
                from bme_sensor import BME280 as _BME280, BME280_ID as _BME280_ID, CHIP_ID_REG as _CID_REG
                _bus = _smbus2.SMBus(1)
                try:
                    _cid = _bus.read_byte_data(0x77, _CID_REG)
                    _bus.close()
                    if _cid == _BME280_ID:
                        _s = _BME280(0x77); box_data = _s.read(); _s.close()
                except Exception:
                    try: _bus.close()
                    except: pass
            except Exception:
                pass

            try:
                data = sensor.read()
                if data is not None:
                    self._sensor_data = data
                    ts = time.strftime("%H:%M:%S")
                    try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).update_sensor, data, chip, ts)
                    except: pass
                    # Stocker telemetry dans la DB partagée
                    threading.Thread(
                        target=_telemetry_insert, args=(data, box_data), daemon=True
                    ).start()
                    if cfg.get("SENSOR_TX_APRS","1") == "1":
                        self._wx_send_from_thread(data)
                elif "BSEC" in chip and not self._stopping:
                    try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).set_chip_status,
                        f"[bold yellow]{chip}[/]  [dim]— initialisation (~5 min pour 1re mesure)[/]")
                    except: pass
            except Exception as e:
                sensor = None
                if not self._stopping:
                    try: self.call_from_thread(self.query_one("#log_panel",Log).write_line, f"[red]Capteur: {e}[/]")
                    except: pass
                time.sleep(30); continue
            next_t = time.time() + interval
            self._next_beacon_ts = next_t
            while not self._stopping and time.time() < next_t:
                remaining = max(0, int(next_t - time.time()))
                try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).set_countdown, remaining)
                except: pass
                time.sleep(min(30, max(1, remaining)))

    def _wx_send_from_thread(self, data: dict):
        try:
            cfg    = load_config(); cs = cfg.get("CALLSIGN","F0CALL")
            lat    = float(cfg.get("LAT","48.8566")); lon = float(cfg.get("LON","2.3522"))
            packet = make_weather_packet(cs, lat, lon, data)
            ok, err = send_beacon(packet); ts = time.strftime("%H:%M:%S")
            try: self.call_from_thread(self.query_one("#log_panel",Log).write_line,
                f"[bold yellow]WX RF: {packet}[/]" if ok else f"[red]WX beacon: {err}[/]")
            except: pass
            try:
                mp = self.query_one("#meteo_panel",MeteoPanel)
                self.call_from_thread(mp.set_last_packet, packet, ok, ts)
                self.call_from_thread(mp.set_tx_status, "[green]Beacon envoyé[/]" if ok else f"[red]{err}[/]")
            except: pass
        except: pass

    @work(thread=True)
    def _send_weather_now(self):
        cfg = load_config()
        if cfg.get("SENSOR_TX_APRS","1") != "1":
            try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).set_tx_status,
                "[yellow]Diffusion APRS désactivée — activer dans Config[/]")
            except: pass
            return
        if self._sensor_data: self._wx_send_from_thread(self._sensor_data)
        else:
            msg = ("[yellow]BSEC en initialisation — 1re mesure dans ~5 min[/]"
                   if "BSEC" in self._sensor_chip else
                   "[yellow]Pas encore de donnée capteur disponible[/]")
            try: self.call_from_thread(self.query_one("#meteo_panel",MeteoPanel).set_tx_status, msg)
            except: pass

    def _send_message(self, addressee: str, text: str) -> None:
        if not addressee or not text: return
        if self._msg_lock is None: return
        cfg = load_config()
        mycall = cfg.get("CALLSIGN", "N0CALL").strip().upper()
        with self._msg_lock:
            msg_id = str(self._msg_next_id)
            self._msg_next_id += 1
            tm = TrackedMsg(msg_id=msg_id, addressee=addressee.upper(), text=text)
            # Stockage immédiat en DB partagée (status=pending, via=? sera mis à jour)
            tm.row_id = _chat_insert("out", mycall, addressee, text, msg_id, via="rf")
            self._msg_tracked[msg_id] = tm
            if len(self._msg_tracked) > _OUT_MAX:
                oldest = next(iter(self._msg_tracked))
                del self._msg_tracked[oldest]
        self._msg_dirty = True
        self._msg_retry_worker(msg_id)

    @work(thread=True)
    def _msg_retry_worker(self, msg_id: str) -> None:
        """Envoie et réessaie un message APRS jusqu'à ACK ou épuisement (RF-first)."""
        cfg = load_config()
        callsign = cfg.get("CALLSIGN", "N0CALL").strip().upper()
        passcode = cfg.get("PASSCODE", "0").strip()
        server   = _aprs_is_server()

        for attempt, delay in enumerate(_MSG_RETRY_DELAYS):
            if delay > 0:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self._stopping:
                    if self._msg_lock:
                        with self._msg_lock:
                            tm = self._msg_tracked.get(msg_id)
                            if tm and tm.state != MsgState.PENDING:
                                return
                    time.sleep(1)

            if self._stopping: return
            if self._msg_lock is None: return
            with self._msg_lock:
                tm = self._msg_tracked.get(msg_id)
                if tm is None or tm.state != MsgState.PENDING:
                    return
                tm.attempts += 1
                addressee, text, row_id = tm.addressee, tm.text, tm.row_id

            ok, err, via = _send_msg_rf_is(callsign, passcode, server, addressee, text, msg_id)
            if ok and attempt == 0:
                # Première envoi réussi : mettre à jour le via dans la DB
                _chat_set_status(row_id, "pending")
                try:
                    with sqlite3.connect(str(SIDECAR_DB), timeout=5) as conn:
                        conn.execute("UPDATE chat_messages SET via=? WHERE id=?", (via, row_id))
                except Exception:
                    pass
            self._msg_dirty = True

            label = "RF" if via == "rf" else "IS"
            status = (f"[dim]#{msg_id} → {addressee} : {'envoyé ' + label if ok else 'erreur: ' + err}"
                      f" (essai {attempt+1}/{len(_MSG_RETRY_DELAYS)})[/]")
            try:
                self.call_from_thread(
                    self.query_one("#messages_panel", MessagesPanel).set_send_status, status)
            except Exception: pass

        # Tous les essais épuisés
        if self._msg_lock:
            with self._msg_lock:
                tm = self._msg_tracked.get(msg_id)
                if tm and tm.state == MsgState.PENDING:
                    tm.state = MsgState.FAILED
                    _chat_set_status(tm.row_id, "failed")
        self._msg_dirty = True

    def on_button_pressed(self, event):
        bid=event.button.id
        if bid=="btn_beacon": self.action_send_beacon()
        elif bid=="btn_restart": self.action_restart_direwolf()
        elif bid=="btn_detect": self._do_detect_aioc()
        elif bid=="btn_msg_send":
            try:
                panel = self.query_one("#messages_panel", MessagesPanel)
                to_inp  = panel.query_one("#msg_to",   Input)
                txt_inp = panel.query_one("#msg_text",  Input)
                to_val  = to_inp.value.strip().upper()
                txt_val = txt_inp.value.strip()
                if to_val and txt_val:
                    to_inp.value = ""; txt_inp.value = ""
                    self._send_message(to_val, txt_val)
                else:
                    panel.set_send_status("[red]Remplissez le destinataire et le message.[/]")
            except Exception: pass

    def action_send_beacon(self):
        packet=make_packet(); ok,err=send_beacon(packet)
        try:
            log=self.query_one("#log_panel",Log)
            if ok:
                log.write_line(f"[bold yellow]Beacon envoye : {packet}[/]")
                sp=self.query_one("#status_panel",StatusPanel); sp.inc_rf_tx(); sp.inc_beacon()
            else: log.write_line(f"[bold red]Echec beacon — {err}[/]")
        except: pass

    @work(thread=True)
    def action_restart_direwolf(self):
        try:
            log=self.query_one("#log_panel",Log)
            self.call_from_thread(log.write_line,"[yellow]Redemarrage Direwolf...[/]")
            subprocess.run(["sudo","systemctl","restart","aprs-direwolf"],timeout=15)
            time.sleep(3); st=service_status("aprs-direwolf")
            self.call_from_thread(log.write_line,"[green]Direwolf actif[/]" if st=="active" else f"[red]Direwolf:{st}[/]")
            sp=self.query_one("#status_panel",StatusPanel); self.call_from_thread(sp.set_direwolf_status,st)
        except: pass

    def _do_detect_aioc(self):
        det=detect_aioc()
        try:
            log=self.query_one("#log_panel",Log)
            if det["ok"]:
                log.write_line(f"[green]AIOC OK — card:{det['alsa_card']} {det['hidraw']}[/]")
                import sys; sys.path.insert(0,"/opt/aprs-lite")
                from aioc_detect import update_direwolf,update_config
                update_direwolf(det); update_config(det)
                log.write_line("[green]Config mise a jour — redemarrage Direwolf...[/]")
                subprocess.run(["sudo","systemctl","restart","aprs-direwolf"],timeout=15)
            else: log.write_line("[red]AIOC non detecte[/]")
        except: pass

    def action_toggle_pause(self):
        self._paused=not self._paused
        try:
            log=self.query_one("#log_panel",Log)
            log.write_line("[bold yellow]Logs en pause[/]" if self._paused else "[bold green]Logs repris[/]")
        except: pass

    def _cleanup(self):
        self._stopping = True
        if self._journal_proc:
            try: self._journal_proc.terminate()
            except: pass
        if self._sensor_obj:
            try: self._sensor_obj.close()
            except: pass

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        if event.worker.state == WorkerState.ERROR and self._stopping:
            event.stop()

    def on_unmount(self): self._cleanup()

    def action_detach(self):
        try: subprocess.run(["tmux","detach-client"], timeout=3, check=False)
        except Exception:
            try: self.query_one("#log_panel",Log).write_line("[bold yellow]Detachement tmux indisponible[/]")
            except: pass

    def action_quit(self):
        self._cleanup()
        self.exit()

if __name__ == "__main__":
    APRSLiteApp().run()
