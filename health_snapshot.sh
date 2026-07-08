#!/bin/bash
# health_snapshot.sh — Snapshot état système APRS Lite
# Appelé par watchdog.sh toutes les heures
# Produit : health.log  +  direwolf_recent.log

HEALTH_LOG="/opt/aprs-lite/logs/health.log"
DIREWOLF_LOG="/opt/aprs-lite/logs/direwolf_recent.log"
MAX_SIZE_KB=10240   # 10 MB max par fichier

rotate_if_needed() {
    local f="$1"
    [ -f "$f" ] || return
    local size_kb
    size_kb=$(du -k "$f" 2>/dev/null | cut -f1)
    if [ "${size_kb:-0}" -gt "$MAX_SIZE_KB" ]; then
        mv "$f" "${f}.old"
    fi
}

rotate_if_needed "$HEALTH_LOG"
rotate_if_needed "$DIREWOLF_LOG"

TS=$(date '+%Y-%m-%d %H:%M:%S')

# Température CPU
TEMP_RAW=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)
TEMP_C=$(awk "BEGIN{printf \"%.1f\", $TEMP_RAW/1000}")

# RAM
RAM=$(free -m 2>/dev/null | awk '/Mem:/{printf "%dMB/%dMB (%.0f%%)", $3, $2, $3*100/$2}')

# Disque
DISK=$(df -h / 2>/dev/null | awk 'NR==2{print $3"/"$2" ("$5")"}')
DISK_PCT=$(df / 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}')

# IP locale
IP=$(ip -4 addr show wlan0 2>/dev/null | grep -oP '(?<=inet )[\d.]+' || echo "N/A")

# Internet
if ping -c1 -W2 8.8.8.8 >/dev/null 2>&1; then
    INTERNET="OK"
else
    INTERNET="absent"
fi

# AIOC
if lsusb 2>/dev/null | grep -qi "1209:7388"; then
    AIOC="present"
else
    AIOC="absent"
fi

# ALSA
ALSA_CARDS=$(aplay -l 2>/dev/null | grep -c "^card" || echo "0")

# Services
DW_STATUS=$(systemctl is-active aprs-direwolf 2>/dev/null || echo "unknown")
WD_STATUS=$(systemctl is-active aprs-watchdog 2>/dev/null || echo "unknown")
TUI_STATUS=$(systemctl is-active aprs-lite-tui 2>/dev/null || echo "unknown")

# Stats trames
STATS_JSON="/opt/aprs-lite/logs/stats.json"
if [ -f "$STATS_JSON" ]; then
    STATS=$(python3 -c "
import json, sys
d = json.load(open('$STATS_JSON'))
print('RF-RX={rf_rx} RF-TX={rf_tx} IS-RX={is_rx} BCN={beacons}'.format(**d))
" 2>/dev/null || echo "N/A")
else
    STATS="N/A"
fi

# Écriture du snapshot
{
printf "\n=== SNAPSHOT %s ===\n" "$TS"
printf "UPTIME  : %s\n"  "$(uptime -p 2>/dev/null || uptime)"
printf "TEMP    : %s°C\n" "$TEMP_C"
printf "RAM     : %s\n"  "$RAM"
printf "DISK    : %s\n"  "$DISK"
printf "IP      : %s\n"  "$IP"
printf "INTERNET: %s\n"  "$INTERNET"
printf "AIOC    : %s\n"  "$AIOC"
printf "ALSA    : %s carte(s)\n" "$ALSA_CARDS"
printf "DW      : %s\n"  "$DW_STATUS"
printf "WATCHDOG: %s\n"  "$WD_STATUS"
printf "TUI     : %s\n"  "$TUI_STATUS"
printf "STATS   : %s\n"  "$STATS"
} | tee -a "$HEALTH_LOG"

# Export des derniers logs Direwolf sans les lignes ig>tx
journalctl -u aprs-direwolf --no-pager -n 200 2>/dev/null \
    | grep -v 'ig>tx' \
    > "$DIREWOLF_LOG"

# Avertissement disque plein
if [ -n "$DISK_PCT" ] && [ "$DISK_PCT" -gt 90 ] 2>/dev/null; then
    printf "%s WARNING DISQUE %s%% utilisé\n" "$TS" "$DISK_PCT" | tee -a "$HEALTH_LOG"
fi
