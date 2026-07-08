#!/bin/bash
# ============================================================
# aprs-aioc-relay-lite — Script d'installation depuis zéro
# Usage : curl -fsSL https://... | sudo bash
#      ou: sudo bash install.sh
# ============================================================
set -e
export DEBIAN_FRONTEND=noninteractive

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()  { echo -e "${GREEN}[OK]${NC} $1"; }
err() { echo -e "${RED}[ERR]${NC} $1"; exit 1; }
inf() { echo -e "${CYAN}[>>]${NC} $1"; }
ask() { echo -e "${YELLOW}[??]${NC} $1"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  aprs-aioc-relay-lite — Installation    ║${NC}"
echo -e "${CYAN}║  V1 Beta 2                              ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

[ "$(id -u)" != "0" ] && err "Lancer avec : sudo bash install.sh"

# ── Wizard de configuration ──────────────────────────────────
echo -e "${CYAN}── Configuration de la station ────────────────────${NC}"
echo ""

ask "Indicatif (MYCALL, ex: F0CALL-10) :"
read -r WIZARD_CALLSIGN
WIZARD_CALLSIGN="${WIZARD_CALLSIGN:-F0CALL}"

ask "Passcode APRS-IS (ex: 12345) :"
read -r WIZARD_PASSCODE
WIZARD_PASSCODE="${WIZARD_PASSCODE:-0}"

ask "Latitude décimale (ex: 48.8566, positif=Nord) :"
read -r WIZARD_LAT
WIZARD_LAT="${WIZARD_LAT:-48.8566}"

ask "Longitude décimale (ex: 2.3522, négatif=Ouest) :"
read -r WIZARD_LON
WIZARD_LON="${WIZARD_LON:-2.3522}"

ask "Commentaire beacon (ex: iGate/Digi ARPA 1650m) :"
read -r WIZARD_COMMENT
WIZARD_COMMENT="${WIZARD_COMMENT:-Relais APRS}"

echo ""
echo -e "${CYAN}── Récapitulatif ───────────────────────────────────${NC}"
echo -e "  Indicatif  : ${GREEN}${WIZARD_CALLSIGN}${NC}"
echo -e "  Passcode   : ${GREEN}${WIZARD_PASSCODE}${NC}"
echo -e "  Latitude   : ${GREEN}${WIZARD_LAT}${NC}"
echo -e "  Longitude  : ${GREEN}${WIZARD_LON}${NC}"
echo -e "  Commentaire: ${GREEN}${WIZARD_COMMENT}${NC}"
echo ""
ask "Confirmer ? [O/n]"
read -r CONFIRM
CONFIRM="${CONFIRM:-O}"
if [[ "$CONFIRM" =~ ^[Nn] ]]; then
    err "Installation annulée."
fi

# ── 1. Mise à jour système ──────────────────────────────────
inf "1/7 Mise à jour système..."
apt-get update -qq
apt-get upgrade -y -qq
ok "Système à jour"

# ── 2. Dépendances ──────────────────────────────────────────
inf "2/7 Installation dépendances..."
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    direwolf \
    alsa-utils \
    kissutils \
    curl wget \
    rsync \
    usbutils \
    exfatprogs \
    2>/dev/null || true
ok "Dépendances installées"

# ── 3. Installer le .deb ────────────────────────────────────
inf "3/7 Installation aprs-aioc-relay-lite..."
DEB=$(find /home -name "aprs-aioc-relay-lite*.deb" 2>/dev/null | head -1)
[ -z "$DEB" ] && DEB=$(find /tmp -name "aprs-aioc-relay-lite*.deb" 2>/dev/null | head -1)
[ -z "$DEB" ] && err "Fichier .deb introuvable. Copiez-le dans /home/pi/ puis relancez."
dpkg -i "$DEB" || { apt-get install -f -y -qq; dpkg -i "$DEB"; }
ok "aprs-aioc-relay-lite installé"

# ── 4. Appliquer la configuration du wizard ─────────────────
inf "4/7 Application de la configuration..."

_set_env() {
    local key="$1" val="$2" file="/opt/aprs-lite/config.env"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=\"${val}\"|" "$file"
    else
        echo "${key}=\"${val}\"" >> "$file"
    fi
}

_set_dw() {
    local key="$1" val="$2" file="/opt/aprs-lite/direwolf.conf"
    if grep -qE "^${key}\s" "$file" 2>/dev/null; then
        sed -i "s|^${key}\s.*|${key} ${val}|" "$file"
    fi
}

_set_env "CALLSIGN" "$WIZARD_CALLSIGN"
_set_env "PASSCODE" "$WIZARD_PASSCODE"
_set_env "LAT"      "$WIZARD_LAT"
_set_env "LON"      "$WIZARD_LON"
_set_env "COMMENT"  "$WIZARD_COMMENT"

_set_dw "MYCALL"   "$WIZARD_CALLSIGN"
_set_dw "IGLOGIN"  "$WIZARD_CALLSIGN $WIZARD_PASSCODE"

# Calculer position APRS DDmm.mmN/DDDmm.mmW pour PBEACON
python3 - <<PYEOF
import re, math
lat = float("$WIZARD_LAT")
lon = float("$WIZARD_LON")
lat_d, lat_m = int(abs(lat)), (abs(lat) % 1) * 60
lon_d, lon_m = int(abs(lon)), (abs(lon) % 1) * 60
lat_s = f"{lat_d:02d}{lat_m:05.2f}{'N' if lat >= 0 else 'S'}"
lon_s = f"{lon_d:03d}{lon_m:05.2f}{'E' if lon >= 0 else 'W'}"
comment = "$WIZARD_COMMENT".replace('"', '')
pbeacon = (f'delay=1 every=30 overlay=R symbol="digi" '
           f'lat={lat_s} long={lon_s} '
           f'power=10 height=10 gain=5 dir=0 '
           f'comment="{comment}"')
conf = open('/opt/aprs-lite/direwolf.conf').read()
conf = re.sub(r'^PBEACON\s+.*$', f'PBEACON {pbeacon}', conf, flags=re.MULTILINE)
open('/opt/aprs-lite/direwolf.conf', 'w').write(conf)
PYEOF

ok "Configuration appliquée"

# ── 5. Clé USB (optionnel) ──────────────────────────────────
inf "5/7 Détection clé USB..."
USB_DEV=$(lsblk -o NAME,TRAN | grep usb | awk '{print "/dev/"$1}' | grep "[0-9]$" | head -1)
if [ -n "$USB_DEV" ]; then
    mkdir -p /mnt/usb
    mount -o uid=$(id -u aprs),gid=$(id -g aprs),umask=0002 "$USB_DEV" /mnt/usb 2>/dev/null || \
        mount "$USB_DEV" /mnt/usb 2>/dev/null || true
    if mountpoint -q /mnt/usb; then
        mkdir -p /mnt/usb/aprs-lite/logs
        ARPA_UID=$(id -u aprs)
        ARPA_GID=$(id -g aprs)
        sed -i "/aprs-lite/d" /etc/fstab 2>/dev/null || true
        echo "${USB_DEV} /mnt/usb exfat defaults,uid=${ARPA_UID},gid=${ARPA_GID},umask=0002,nofail 0 0" >> /etc/fstab
        ok "Clé USB montée ($USB_DEV)"
    else
        ok "Pas de clé USB — logs sur SD uniquement"
    fi
else
    ok "Pas de clé USB — logs sur SD uniquement"
fi

# ── 6. Autodetection AIOC ───────────────────────────────────
inf "6/7 Détection AIOC..."
python3 /opt/aprs-lite/aioc_detect.py && ok "AIOC détecté et configuré" || \
    echo -e "${RED}[!!]${NC} AIOC non détecté — branchez-le et relancez : python3 /opt/aprs-lite/aioc_detect.py"

# ── 7. Démarrage services ───────────────────────────────────
inf "7/7 Démarrage services..."
systemctl daemon-reload
systemctl enable aprs-direwolf aprs-watchdog 2>/dev/null || true
systemctl restart aprs-watchdog 2>/dev/null || true
systemctl start aprs-direwolf 2>/dev/null || true
sleep 5

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  Installation terminée !                 ║${NC}"
echo -e "${CYAN}║                                          ║${NC}"
echo -e "${CYAN}║  Lancez l'interface TUI :                ║${NC}"
echo -e "${CYAN}║    aprs-lite                             ║${NC}"
echo -e "${CYAN}║                                          ║${NC}"
echo -e "${CYAN}║  Services :                              ║${NC}"
for s in aprs-direwolf aprs-watchdog; do
    ST=$(systemctl is-active $s 2>/dev/null || echo "?")
    [ "$ST" = "active" ] && \
        echo -e "${CYAN}║  ${GREEN}✅ $s${CYAN}$(printf '%*s' $((30-${#s})) '')║${NC}" || \
        echo -e "${CYAN}║  ${RED}❌ $s${CYAN}$(printf '%*s' $((30-${#s})) '')║${NC}"
done
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
