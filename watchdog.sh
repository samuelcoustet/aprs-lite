#!/bin/bash
# aprs-watchdog.sh v2 — Watchdog robuste site isolé
# Surveille : Direwolf, disque, température
# Reboot hebdomadaire dimanche 3h00
# Snapshots système toutes les heures
# Ne relance jamais Direwolf pour absence internet

LOG="/opt/aprs-lite/logs/watchdog.log"
REBOOT_FLAG="/tmp/aprs_weekly_reboot_done"
HEALTH_SCRIPT="/opt/aprs-lite/health_snapshot.sh"
LOG_MAX_SIZE_KB=10240  # 10 MB

mkdir -p /opt/aprs-lite/logs

# ── Helpers ───────────────────────────────────────────────────────────────────

log()      { echo "$(date '+%Y-%m-%d %H:%M:%S') INFO     $1" | tee -a "$LOG"; }
log_warn() { echo "$(date '+%Y-%m-%d %H:%M:%S') WARNING  $1" | tee -a "$LOG"; }
log_crit() { echo "$(date '+%Y-%m-%d %H:%M:%S') CRITICAL $1" | tee -a "$LOG"; }

rotate_log() {
    local f="$1"
    [ -f "$f" ] || return
    local sz
    sz=$(du -k "$f" 2>/dev/null | cut -f1)
    [ "${sz:-0}" -gt "$LOG_MAX_SIZE_KB" ] && mv "$f" "${f}.old"
}

# ── Charger la config ─────────────────────────────────────────────────────────

load_cfg() {
    TEMP_WARN=75
    DISK_WARN_PCT=90
    SITE_MODE="online"
    if [ -f /opt/aprs-lite/config.env ]; then
        while IFS='=' read -r key val; do
            [[ "$key" =~ ^[[:space:]]*# ]] && continue
            [[ -z "$key" ]] && continue
            val="${val//\"/}"; val="${val//\'/}"
            case "$key" in
                TEMP_WARN)     TEMP_WARN="$val"     ;;
                DISK_WARN_PCT) DISK_WARN_PCT="$val" ;;
                SITE_MODE)     SITE_MODE="$val"     ;;
            esac
        done < /opt/aprs-lite/config.env
    fi
}

# ── Contrôles ─────────────────────────────────────────────────────────────────

check_direwolf() {
    if ! systemctl is-active --quiet aprs-direwolf; then
        log_crit "Direwolf inactif — relance"
        systemctl restart aprs-direwolf
    fi
}

check_temperature() {
    local temp_raw
    temp_raw=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)
    local temp_c=$(( temp_raw / 1000 ))
    if [ "$temp_c" -ge 80 ]; then
        log_crit "Température CPU critique : ${temp_c}°C"
    elif [ "$temp_c" -ge "${TEMP_WARN:-75}" ]; then
        log_warn "Température CPU élevée : ${temp_c}°C"
    fi
}

check_disk() {
    local pct
    pct=$(df / 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}')
    if [ -n "$pct" ] && [ "$pct" -ge "${DISK_WARN_PCT:-90}" ] 2>/dev/null; then
        log_crit "Disque SD presque plein : ${pct}%"
    fi
}

check_aioc() {
    if ! lsusb 2>/dev/null | grep -qi "1209:7388"; then
        log_warn "AIOC non détecté sur USB (Direwolf continue en attente)"
    fi
}

sync_usb() {
    if mountpoint -q /mnt/usb 2>/dev/null; then
        # Timeout 30s pour éviter de bloquer le watchdog
        timeout 30 rsync -a --delete \
            /opt/aprs-lite/logs/ /mnt/usb/aprs-lite/logs/ 2>/dev/null || \
            log_warn "Sync USB échoué ou timeout"
        # Copier aussi config + stats
        timeout 10 cp /opt/aprs-lite/config.env \
            /mnt/usb/aprs-lite/config.env 2>/dev/null || true
    fi
}

do_health_snapshot() {
    [ -x "$HEALTH_SCRIPT" ] && bash "$HEALTH_SCRIPT" 2>/dev/null || true
}

do_log_rotation() {
    rotate_log "$LOG"
    # Supprimer les fichiers .log plus vieux que 30 jours (rétention courte SD)
    find /opt/aprs-lite/logs -name "*.log" -mtime +30 -delete 2>/dev/null
    find /opt/aprs-lite/logs -name "*.old" -mtime +7  -delete 2>/dev/null
}

check_weekly_reboot() {
    local DOW HOUR MIN
    DOW=$(date +%u)
    HOUR=$(date +%H)
    MIN=$(date +%M)
    if [ "$DOW" = "7" ] && [ "$HOUR" = "03" ] && [ "$MIN" -lt "2" ]; then
        if [ ! -f "$REBOOT_FLAG" ]; then
            touch "$REBOOT_FLAG"
            log "Reboot hebdomadaire dimanche 3h00"
            sleep 30
            reboot
        fi
    fi
    # Effacer le flag le lundi à 04h00
    if [ "$DOW" = "1" ] && [ "$HOUR" = "04" ] && [ -f "$REBOOT_FLAG" ]; then
        rm -f "$REBOOT_FLAG"
    fi
}

# ── Boucle principale ─────────────────────────────────────────────────────────

load_cfg
log "Watchdog démarré (SITE_MODE=${SITE_MODE} TEMP_WARN=${TEMP_WARN}°C DISK_WARN=${DISK_WARN_PCT}%)"

LOOP=0

while true; do
    load_cfg   # recharger la config à chaque itération (changements à chaud)

    check_direwolf
    check_temperature
    check_weekly_reboot

    # Contrôles toutes les 5 minutes (toutes les 5 itérations de 60s)
    if [ $(( LOOP % 5 )) -eq 0 ]; then
        check_disk
        check_aioc
    fi

    # Snapshot + rotation + sync USB toutes les heures (à la minute 00)
    if [ "$(date +%M)" = "00" ]; then
        do_health_snapshot
        do_log_rotation
        sync_usb
    fi

    LOOP=$(( LOOP + 1 ))
    sleep 60
done
