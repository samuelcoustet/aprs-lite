#!/bin/bash
# ================================================================
# install_bsec.sh — Installe bme68x Python extension + BSEC2
# Compatible BME680 et BME688 (chip_id 0x61 identique)
#
# Prérequis MANUEL (portail Bosch, inscription requise) :
#   1. Aller sur : https://www.bosch-sensortec.com/software-tools/software/bme688-software/
#   2. Télécharger : bsec2-6-1-0_generic_release.zip
#   3. Copier le zip sur le Pi dans /home/pi/ ou /tmp/
#
# Ensuite lancer : sudo bash /opt/aprs-lite/install_bsec.sh
# ================================================================
set -e

BSEC2_ZIP_NAME="bsec2-6-1-0_generic_release.zip"
PYTHON_LIB_REPO="https://github.com/mcalisterkm/bme68x-python-library-bsec2.6.1.0.git"
BUILD_DIR="/tmp/bsec_python_build"
VENV="/opt/aprs-lite/venv"
BSEC_DIR="/opt/aprs-lite/bsec"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
err()  { echo -e "${RED}[ERR]${NC} $1"; exit 1; }
inf()  { echo -e "${CYAN}[>>]${NC} $1"; }
warn() { echo -e "${YELLOW}[!!]${NC} $1"; }

[ "$(id -u)" != "0" ] && err "Lancer avec sudo"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  Installation BSEC2 — Extension Python bme68x  ║${NC}"
echo -e "${CYAN}║  BSEC2 v2.6.1.0 — BME680 / BME688              ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}LICENCE :${NC} La bibliothèque BSEC est soumise à la licence"
echo -e "Bosch Sensortec (usage non commercial / évaluation)."
echo -e "Détails : https://www.bosch-sensortec.com/software-tools/software/bme688-software/"
echo ""

# ── Vérification du zip BSEC2 ──────────────────────────────────
BSEC2_ZIP=$(find /home /root /tmp /opt -name "$BSEC2_ZIP_NAME" 2>/dev/null | head -1)

if [ -z "$BSEC2_ZIP" ]; then
    echo -e "${RED}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ZIP BSEC2 MANQUANT                             ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "Le fichier  ${BSEC2_ZIP_NAME}  est introuvable."
    echo ""
    echo "Étapes :"
    echo "  1. Aller sur : https://www.bosch-sensortec.com/software-tools/software/bme688-software/"
    echo "  2. Créer un compte Bosch (gratuit) et télécharger :"
    echo "     ${BSEC2_ZIP_NAME}"
    echo "  3. Copier le zip sur le Pi :"
    echo "     scp ${BSEC2_ZIP_NAME} pi@<ip_du_pi>:/home/pi/"
    echo "  4. Relancer ce script :"
    echo "     sudo bash /opt/aprs-lite/install_bsec.sh"
    echo ""
    exit 1
fi

ok "ZIP trouvé : $BSEC2_ZIP"
echo ""
read -r -p "Accepter la licence Bosch et continuer ? [o/N] " CONFIRM
[[ "$CONFIRM" =~ ^[oO]$ ]] || err "Installation annulée."
echo ""

# ── 1. Dépendances ─────────────────────────────────────────────
inf "1/5 Dépendances système..."
apt-get install -y -qq git gcc python3-dev i2c-tools libi2c-dev 2>/dev/null || true
ok "Dépendances OK"

# ── 2. Cloner la bibliothèque Python ───────────────────────────
inf "2/5 Clonage bme68x-python-library-bsec2.6.1.0..."
rm -rf "$BUILD_DIR"
git clone --depth=1 "$PYTHON_LIB_REPO" "$BUILD_DIR" 2>&1 \
    || err "Clonage échoué — vérifier la connexion internet"
ok "Clonage OK : $BUILD_DIR"

# ── 3. Extraire le zip BSEC2 dans le répertoire build ──────────
inf "3/5 Extraction $BSEC2_ZIP_NAME..."
cd "$BUILD_DIR"
unzip -q "$BSEC2_ZIP" -d . \
    || err "Extraction du zip BSEC2 échouée"

# Vérifier que la lib ARM64 est présente
LIB_CHECK=$(find . -path "*/RaspberryPi/*" -name "libalgobsec.a" 2>/dev/null | head -1)
if [ -z "$LIB_CHECK" ]; then
    warn "Aucun dossier RaspberryPi trouvé — structure archive :"
    find . -name "*.a" | head -10
    err "Archive BSEC2 inattendue — vérifier que le zip est bien bsec2-6-1-0_generic_release.zip"
fi
ok "Bibliothèque ARM trouvée : $LIB_CHECK"

# ── 4. Compilation de l'extension Python ───────────────────────
inf "4/5 Compilation extension Python (BSEC2=64, 64-bit Raspberry Pi OS)..."

# Déterminer l'architecture
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
    BSEC2_ARCH=64
    ok "Architecture : aarch64 → BSEC2=64"
elif [ "$ARCH" = "armv7l" ] || [ "$ARCH" = "armv8l" ]; then
    BSEC2_ARCH=32
    ok "Architecture : $ARCH → BSEC2=32"
else
    warn "Architecture $ARCH non reconnue — utilisation de BSEC2=64 par défaut"
    BSEC2_ARCH=64
fi

cd "$BUILD_DIR"
BSEC2=$BSEC2_ARCH "$VENV/bin/python3" setup.py install 2>&1 \
    || err "Compilation échouée — voir erreur ci-dessus"
ok "Extension Python compilée et installée dans $VENV"

# ── 5. Vérification rapide ─────────────────────────────────────
inf "5/5 Vérification du module..."
if "$VENV/bin/python3" -c "from bme68x import BME68X; print('bme68x OK')" 2>/dev/null; then
    ok "Module bme68x importable"
else
    err "Module bme68x non importable après installation — voir erreurs ci-dessus"
fi

if "$VENV/bin/python3" -c "import bsecConstants; print('bsecConstants OK')" 2>/dev/null; then
    ok "Module bsecConstants importable"
fi

# ── Activer I2C si nécessaire ───────────────────────────────────
if ! ls /dev/i2c-1 &>/dev/null 2>&1; then
    warn "/dev/i2c-1 non détecté — activation I2C..."
    raspi-config nonint do_i2c 0
    warn "Redémarrage nécessaire pour activer I2C : sudo reboot"
fi

# ── Groupe I2C pour users aprs et pi ───────────────────────────
for USER in aprs pi; do
    if id "$USER" &>/dev/null && ! groups "$USER" | grep -q i2c; then
        usermod -aG i2c "$USER" 2>/dev/null && ok "User $USER ajouté au groupe i2c" || true
    fi
done

# Sauvegarder les infos d'installation
mkdir -p "$BSEC_DIR"
echo "bme68x_python_lib=bme68x-python-library-bsec2.6.1.0" > "$BSEC_DIR/versions.txt"
echo "bsec2_version=2.6.1.0"                               >> "$BSEC_DIR/versions.txt"
echo "arch=BSEC2=${BSEC2_ARCH}"                            >> "$BSEC_DIR/versions.txt"
echo "installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)"           >> "$BSEC_DIR/versions.txt"
chown -R aprs:aprs "$BSEC_DIR" 2>/dev/null || true

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  BSEC2 installé avec succès.${NC}"
echo -e "${GREEN}  Extension Python bme68x active dans le venv.${NC}"
echo -e "${GREEN}${NC}"
echo -e "${GREEN}  Déployer et redémarrer :${NC}"
echo -e "${GREEN}  sudo cp /tmp/bsec_iaq.py /opt/aprs-lite/bsec_iaq.py${NC}"
echo -e "${GREEN}  sudo systemctl restart aprs-lite-tui${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
