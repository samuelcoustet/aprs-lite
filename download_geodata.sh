#!/bin/bash
# Téléchargement données Natural Earth pour la carte braille
set -e
GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
DEST="$(dirname "$0")/geodata"
mkdir -p "$DEST"
BASE="https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
FILES=(
    ne_50m_coastline.geojson
    ne_50m_admin_0_boundary_lines_land.geojson
    ne_50m_populated_places_simple.geojson
    ne_110m_coastline.geojson
    ne_110m_admin_0_boundary_lines_land.geojson
    ne_110m_populated_places_simple.geojson
)
for f in "${FILES[@]}"; do
    printf "  %-52s " "$f"
    if wget -q --timeout=30 -O "$DEST/$f" "$BASE/$f"; then
        echo -e "${GREEN}OK${NC} ($(wc -c < "$DEST/$f") octets)"
    else
        echo -e "${RED}ERREUR${NC}"
    fi
done
echo "Données dans : $DEST/"
