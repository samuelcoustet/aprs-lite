"""
Wrapper BSEC2 via extension Python bme68x (mcalisterkm/bme68x-python-library-bsec2.6.1.0).
Compatible BME680 et BME688 (chip_id 0x61 identique).

Prérequis : sudo bash /opt/aprs-lite/install_bsec.sh
"""
import threading
import time

# ── Niveaux qualité d'air IAQ ──────────────────────────────────
IAQ_LEVELS = [
    (  50, "Excellent",          "bold green"),
    ( 100, "Bon",                "green"),
    ( 150, "Légèrement pollué",  "yellow"),
    ( 200, "Modérément pollué",  "bold yellow"),
    ( 250, "Fortement pollué",   "orange1"),
    ( 350, "Très pollué",        "red"),
    (9999, "Extrêmement pollué", "bold red"),
]

def iaq_label(iaq: float) -> tuple[str, str]:
    """Retourne (description, couleur_Rich) pour une valeur IAQ."""
    for threshold, label, color in IAQ_LEVELS:
        if iaq <= threshold:
            return label, color
    return "Extrêmement pollué", "bold red"


class BsecError(Exception): pass


class BsecReader:
    """
    Lit le BME68x via l'extension Python bme68x + BSEC2.
    Interface compatible BME280/BME680 (méthodes read() / close()).

    BSEC_SAMPLE_RATE_LP = 0.333 Hz → 1 mesure / ~3 s.
    La précision IAQ s'améliore sur 24 h de fonctionnement continu.
    """

    def __init__(self, addr: int = 0x76):
        try:
            from bme68x import BME68X
            import bsecConstants as bsec_cst
        except ImportError:
            raise BsecError(
                "Module bme68x non disponible.\n"
                "Lancer : sudo bash /opt/aprs-lite/install_bsec.sh"
            )

        # bus=1 correspond à /dev/i2c-1 sur Raspberry Pi
        self._sensor = BME68X(addr, 1)

        # Mode LP : ~3 secondes par mesure — bon compromis affichage / conso
        self._sensor.set_sample_rate(bsec_cst.BSEC_SAMPLE_RATE_LP)

        self._latest: dict | None = None
        self._lock = threading.Lock()
        self._running = True
        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()

    def _read_loop(self):
        while self._running:
            try:
                d = self._sensor.get_bsec_data()
                if d and isinstance(d, dict):
                    with self._lock:
                        self._latest = d
            except Exception:
                pass
            time.sleep(1)  # poll rapide, BSEC décide lui-même quand mesurer

    def read(self) -> dict | None:
        """
        Retourne la dernière mesure BSEC ou None si pas encore disponible.
        Champs : temperature (°C), humidity (%), pressure (hPa),
                 iaq, iaq_accuracy (0-3), static_iaq,
                 co2_eq (ppm), voc_eq (ppm), gas=None.
        """
        with self._lock:
            if self._latest is None:
                return None
            d = self._latest

        # raw_pressure est en Pa → conversion hPa
        pressure_hpa = round(float(d.get("raw_pressure", 0.0)) / 100.0, 1)

        return {
            "temperature":  round(float(d.get("temperature",             0.0)), 1),
            "humidity":     round(float(d.get("humidity",                0.0)), 1),
            "pressure":     pressure_hpa,
            "gas":          None,   # remplacé par IAQ
            "iaq":          round(float(d.get("iaq",                    -1.0)), 1),
            "iaq_accuracy": int(d.get("iaq_accuracy",                    0)),
            "static_iaq":   round(float(d.get("static_iaq",             -1.0)), 1),
            "co2_eq":       round(float(d.get("co2_equivalent",         -1.0)), 1),
            "voc_eq":       round(float(d.get("breath_voc_equivalent",  -1.0)), 2),
        }

    def close(self):
        self._running = False
        try:
            self._sensor.close_i2c()
        except Exception:
            pass
