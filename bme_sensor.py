"""BME280 / BME68x (BME680, BME688) I2C driver — smbus2.
Auto-détecte le capteur à 0x76 ou 0x77.

Note : BME680 et BME688 partagent le même chip_id (0x61) — ils sont
indiscernables au niveau registres. Le BME688 est géré de façon identique
via la BME68x Sensor API + BSEC2.
"""
import struct
import smbus2

CHIP_ID_REG = 0xD0
BME280_ID   = 0x60
BME68X_ID   = 0x61  # commun à BME680 et BME688 (même silicium)
BME680_ID   = BME68X_ID  # alias rétrocompatibilité


class SensorError(Exception): pass


def find_chip():
    """Sonde le bus I2C 1. Retourne (addr, 'BME280'|'BME68x') ou lève SensorError."""
    try:
        bus = smbus2.SMBus(1)
    except Exception as e:
        raise SensorError(f"Bus I2C 1 inaccessible: {e}")
    for addr in (0x76, 0x77):
        try:
            cid = bus.read_byte_data(addr, CHIP_ID_REG)
            if cid == BME280_ID: bus.close(); return addr, "BME280"
            if cid == BME68X_ID: bus.close(); return addr, "BME68x"
        except OSError: pass
    bus.close()
    raise SensorError("Aucun BME280/BME68x (BME680/688) trouvé (0x76, 0x77)")


class BME280:
    """Driver BME280 pur smbus2 — compensation selon datasheet Bosch BST-BME280-DS002."""

    def __init__(self, addr=0x76):
        self.addr = addr
        self._b = smbus2.SMBus(1)
        self._load_cal()
        self._b.write_byte_data(addr, 0xF2, 0x01)  # ctrl_hum: osrs_h × 1
        self._b.write_byte_data(addr, 0xF4, 0x27)  # ctrl_meas: osrs_t/p × 1, normal mode
        self._b.write_byte_data(addr, 0xF5, 0xA0)  # config: filtre × 16, t_sb 1 s

    def _load_cal(self):
        d = bytes(self._b.read_i2c_block_data(self.addr, 0x88, 24))
        self._T = [0,
                   struct.unpack_from('<H', d, 0)[0],
                   struct.unpack_from('<h', d, 2)[0],
                   struct.unpack_from('<h', d, 4)[0]]
        self._P = [0] + list(struct.unpack_from('<Hhhhhhhhh', d, 6))
        H = [0, self._b.read_byte_data(self.addr, 0xA1)]
        e = bytes(self._b.read_i2c_block_data(self.addr, 0xE1, 7))
        H += [struct.unpack_from('<h', e, 0)[0], e[2],
              (e[3] << 4) | (e[4] & 0x0F),
              (e[5] << 4) | (e[4] >> 4),
              struct.unpack_from('b', bytes([e[6]]))[0]]
        self._H = H

    def read(self) -> dict:
        d = self._b.read_i2c_block_data(self.addr, 0xF7, 8)
        ap = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        at = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        ah = (d[6] << 8) | d[7]
        T, P, H = self._T, self._P, self._H

        v1 = (at / 16384.0 - T[1] / 1024.0) * T[2]
        v2 = (at / 131072.0 - T[1] / 8192.0) ** 2 * T[3]
        tf = v1 + v2
        temp = tf / 5120.0

        v1 = tf / 2.0 - 64000.0
        v2 = v1 * v1 * P[6] / 32768.0 + v1 * P[5] * 2.0
        v2 = v2 / 4.0 + P[4] * 65536.0
        v1 = (P[3] * v1 * v1 / 524288.0 + P[2] * v1) / 524288.0
        v1 = (1.0 + v1 / 32768.0) * P[1]
        pres = (1048576.0 - ap - v2 / 4096.0) * 6250.0 / v1 if v1 else 0.0
        if pres:
            pres += (P[9] * pres * pres / 2147483648.0 + pres * P[8] / 32768.0 + P[7]) / 16.0
        pres /= 100.0  # Pa → hPa

        x = tf - 76800.0
        if x:
            x = (ah - (H[4] * 64.0 + H[5] / 16384.0 * x)) * \
                (H[2] / 65536.0 * (1.0 + H[6] / 67108864.0 * x *
                 (1.0 + H[3] / 67108864.0 * x)))
            x = max(0.0, min(100.0, x * (1.0 - H[1] * x / 524288.0)))
        else:
            x = 0.0

        return {"temperature": round(temp, 1), "humidity": round(x, 1),
                "pressure": round(pres, 1), "gas": None}

    def close(self): self._b.close()


class BME680:
    """Driver BME68x (BME680/688) via paquet 'bme680' — pip install bme680.
    Le paquet 'bme680' est compatible BME688 car les deux puces partagent le même
    registre set. Pour l'IAQ précise, utiliser BsecReader (BSEC2) à la place.
    """

    def __init__(self, addr=0x76):
        try:
            import bme680 as B
        except ImportError:
            raise SensorError(
                "BME68x détecté — installer : /opt/aprs-lite/venv/bin/pip install bme680")
        import bme680 as B
        s = B.BME680(B.I2C_ADDR_PRIMARY if addr == 0x76 else B.I2C_ADDR_SECONDARY)
        s.set_humidity_oversample(B.OS_2X)
        s.set_pressure_oversample(B.OS_4X)
        s.set_temperature_oversample(B.OS_8X)
        s.set_filter(B.FILTER_SIZE_3)
        s.set_gas_status(B.ENABLE_GAS_MEAS)
        s.set_gas_heater_temperature(320)
        s.set_gas_heater_duration(150)
        s.select_gas_heater_profile(0)
        self._s = s

    def read(self) -> dict:
        if not self._s.get_sensor_data(): return None
        gas = round(self._s.data.gas_resistance) if self._s.data.heat_stable else None
        return {"temperature": round(self._s.data.temperature, 1),
                "humidity":    round(self._s.data.humidity,    1),
                "pressure":    round(self._s.data.pressure,    1),
                "gas": gas}

    def close(self): pass


def open_sensor():
    """
    Détecte et ouvre le capteur. Retourne (instance, chip_name).
    BME680 et BME688 ont le même chip_id (0x61) — traités identiquement.
    Pour tout BME68x, tente BSEC2 (IAQ) en priorité ; repli sur lecture brute.
    """
    addr, name = find_chip()
    if name == "BME280":
        return BME280(addr), name
    # BME68x (BME680/688) — essayer BSEC2 d'abord
    try:
        from bsec_iaq import BsecReader
        sensor = BsecReader(addr)
        return sensor, "BME68x+BSEC"
    except Exception:
        pass
    # Repli : lecture brute via paquet bme680 (compatible BME680 et BME688)
    return BME680(addr), name


if __name__ == "__main__":
    sensor, name = open_sensor()
    data = sensor.read()
    if data is None:
        print(f"{name}: en attente première mesure BSEC2…")
    else:
        line = (f"{name}: {data['temperature']}°C  {data['humidity']}%  "
                f"{data['pressure']} hPa")
        if data.get("iaq", -1) >= 0:
            from bsec_iaq import iaq_label
            label, _ = iaq_label(data["iaq"])
            line += f"  IAQ={data['iaq']} ({label}, acc={data['iaq_accuracy']})"
            if data.get("co2_eq", -1) > 0: line += f"  CO2={data['co2_eq']}ppm"
            if data.get("voc_eq", -1) > 0: line += f"  VOC={data['voc_eq']}ppm"
        elif data.get("gas"):
            line += f"  Gas:{data['gas']}Ω"
        print(line)
    sensor.close()
