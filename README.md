# aprs-lite

**APRS iGate / Digipeater TUI** pour Raspberry Pi + [AIOC (All-In-One Cable)](https://github.com/skuep/AIOC).

Interface terminal (Textual) permettant de gérer un relais APRS complet :
réception RF via Direwolf, iGate vers APRS-IS, beacon météo BME28x/BME68x,
carte braille des stations, suivi de messages.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Textual](https://img.shields.io/badge/textual-TUI-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Fonctionnalités

| Onglet | Description |
|--------|-------------|
| **Principal** | Logs Direwolf temps réel, statuts système, beacon manuel |
| **Stations** | Tableau des stations entendues en RF (distance, cap, type) |
| **Carte** | Carte braille Unicode — fond géographique Natural Earth 500 km |
| **Messages** | Messages APRS reçus (hors ACK/REJ) |
| **Config** | Configuration Direwolf, capteur météo, palette couleurs |
| **WiFi** | Scan et connexion WiFi |
| **Horloge** | Synchronisation NTP / réglage manuel |
| **Météo** | Lecture capteur BME280/BME680/BME688 + beacon WX APRS |

## Matériel requis

- Raspberry Pi (testé Pi 4 / Pi Zero 2W, Raspberry Pi OS Bookworm)
- [AIOC All-In-One Cable](https://github.com/skuep/AIOC) — interface radio USB
- (optionnel) Capteur I²C BME280 / BME680 / BME688

## Installation rapide

```bash
git clone https://github.com/VOTRECOMPTE/aprs-lite.git /opt/aprs-lite
cd /opt/aprs-lite
sudo bash install.sh
cp config.env.example config.env
# Éditer config.env avec votre indicatif et coordonnées
```

## Carte géographique (fond Natural Earth)

```bash
sudo bash /opt/aprs-lite/download_geodata.sh
```

Télécharge ~4 Mo de données Natural Earth (coastlines, frontières, villes)
dans `/opt/aprs-lite/geodata/`. La carte se rafraîchit sans redémarrage.

## Capteur météo BSEC2 (BME688)

Pour l'IAQ avec le BSEC2 de Bosch :

```bash
# 1. Télécharger bsec2-6-1-0_generic_release.zip depuis le portail Bosch
# 2. Copier le zip sur le Pi, puis :
sudo bash /opt/aprs-lite/install_bsec.sh
```

## Lancement

```bash
# Via systemd (recommandé)
sudo systemctl enable --now aprs-lite-tui

# Manuel (tmux)
tmux new -s aprs
/opt/aprs-lite/venv/bin/python3 /opt/aprs-lite/aprs-lite.py
```

Raccourcis clavier : `q` détacher tmux · `x` quitter · `b` beacon · `r` restart Direwolf · `p` pause logs

## Structure

```
aprs-lite.py          # TUI principal (Textual)
aprs_decoder.py       # Décodeur paquets APRS (aprslib)
station_tracker.py    # Suivi stations RF (haversine, cap)
dedup_filter.py       # Filtre anti-doublons (fenêtre 30 s)
geo_renderer.py       # Rendu géographique braille (Natural Earth)
bme_sensor.py         # Pilote BME280 / BME680
bsec_iaq.py           # Pilote BME688 + BSEC2 IAQ
aioc_detect.py        # Détection automatique AIOC USB
install.sh            # Script d'installation
install_bsec.sh       # Installation bibliothèque BSEC2
watchdog.sh           # Watchdog Direwolf
config.env.example    # Configuration exemple
```

## Dépendances Python

```
textual>=0.52
aprslib>=0.7
smbus2
bme280
```

Voir `requirements.txt`.

## Licence

MIT — voir [LICENSE](LICENSE)
