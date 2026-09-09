/**
 * bsec_helper.c — BME68x (BME680 / BME688) + BSEC 2.x pour Linux/ARM
 *
 * BME680 et BME688 partagent le même chip_id (0x61) et registre set.
 * La BME68x Sensor API (bme68x.h) et BSEC2 couvrent les deux de façon
 * transparente — aucun changement de code selon le modèle exact.
 *
 * Lit le capteur via /dev/i2c-1, applique BSEC2 et sort du JSON sur stdout
 * une fois par cycle LP (toutes les 300 s environ).
 *
 * Usage : ./bsec_helper [-a 0x76|0x77]
 * Sortie : {"iaq":42.1,"iaq_accuracy":3,"static_iaq":40.0,
 *           "co2_eq":500.0,"voc_eq":0.50,
 *           "temperature":22.3,"humidity":55.1,"pressure":1013.2}
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/i2c-dev.h>
#include <math.h>

#include "bme68x.h"
#include "bsec_interface.h"
#include "bsec_datatypes.h"

#define STATE_FILE   "/opt/aprs-lite/bsec/bsec_state.bin"
#define STATE_SAVE_S 43200   /* sauvegarde état toutes les 12 h */

/* ── I2C fd global ─────────────────────────────────────────── */
static int   g_i2c_fd   = -1;
static uint8_t g_dev_addr = BME68X_I2C_ADDR_LOW;

/* ── Callbacks I2C pour le BME68x API ─────────────────────── */
static BME68X_INTF_RET_TYPE i2c_read(uint8_t reg, uint8_t *data,
                                      uint32_t len, void *ptr)
{
    if (ioctl(g_i2c_fd, I2C_SLAVE, *(uint8_t*)ptr) < 0) return -1;
    if (write(g_i2c_fd, &reg, 1) != 1)                  return -1;
    if (read(g_i2c_fd, data, len) != (ssize_t)len)      return -1;
    return 0;
}

static BME68X_INTF_RET_TYPE i2c_write(uint8_t reg, const uint8_t *data,
                                       uint32_t len, void *ptr)
{
    uint8_t buf[len + 1];
    buf[0] = reg;
    memcpy(buf + 1, data, len);
    if (ioctl(g_i2c_fd, I2C_SLAVE, *(uint8_t*)ptr) < 0)         return -1;
    if (write(g_i2c_fd, buf, len + 1) != (ssize_t)(len + 1))    return -1;
    return 0;
}

static void delay_us(uint32_t us, void *ptr) { (void)ptr; usleep(us); }

/* ── Horodatage nanosecondes (CLOCK_MONOTONIC) ─────────────── */
static int64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* ── Persistance état BSEC ─────────────────────────────────── */
static void load_state(void)
{
    FILE *f = fopen(STATE_FILE, "rb");
    if (!f) return;
    fseek(f, 0, SEEK_END); long sz = ftell(f); rewind(f);
    if (sz <= 0) { fclose(f); return; }
    uint8_t *buf  = malloc((size_t)sz);
    uint8_t *work = malloc((size_t)sz);
    if (buf && work) {
        fread(buf, 1, (size_t)sz, f);
        bsec_set_state(buf, (uint32_t)sz, work, (uint32_t)sz);
        fprintf(stderr, "[bsec] état restauré (%ld octets)\n", sz);
    }
    free(buf); free(work); fclose(f);
}

static void save_state(void)
{
    uint32_t needed = 0;
    bsec_get_state(0, NULL, 0, NULL, 0, &needed);
    if (!needed) return;
    uint8_t *buf  = malloc(needed);
    uint8_t *work = malloc(needed);
    if (!buf || !work) { free(buf); free(work); return; }
    if (bsec_get_state(0, buf, needed, work, needed, &needed) == BSEC_OK) {
        FILE *f = fopen(STATE_FILE, "wb");
        if (f) { fwrite(buf, 1, needed, f); fclose(f);
                 fprintf(stderr, "[bsec] état sauvegardé\n"); }
    }
    free(buf); free(work);
}

/* ── main ─────────────────────────────────────────────────── */
int main(int argc, char *argv[])
{
    /* Adresse I2C optionnelle */
    for (int i = 1; i < argc - 1; i++)
        if (!strcmp(argv[i], "-a"))
            g_dev_addr = (uint8_t)strtol(argv[i+1], NULL, 0);

    /* Ouvrir bus I2C */
    g_i2c_fd = open("/dev/i2c-1", O_RDWR);
    if (g_i2c_fd < 0) { perror("open /dev/i2c-1"); return 1; }

    /* Init BME68x */
    struct bme68x_dev bme;
    memset(&bme, 0, sizeof(bme));
    bme.intf     = BME68X_I2C_INTF;
    bme.intf_ptr = &g_dev_addr;
    bme.read     = i2c_read;
    bme.write    = i2c_write;
    bme.delay_us = delay_us;
    bme.amb_temp = 25;
    if (bme68x_init(&bme) != BME68X_OK) {
        fprintf(stderr, "BME68x init failed\n"); return 1;
    }

    /* Init BSEC */
    if (bsec_init() != BSEC_OK) {
        fprintf(stderr, "bsec_init failed\n"); return 1;
    }
    bsec_version_t ver;
    bsec_get_version(&ver);
    fprintf(stderr, "[bsec] version %d.%d.%d.%d\n",
            ver.major, ver.minor, ver.major_bugfix, ver.minor_bugfix);
    load_state();

    /* Abonnement capteurs virtuels — mode LP (1 mesure / 300 s) */
    float lp = BSEC_SAMPLE_RATE_LP;
    bsec_sensor_configuration_t req[] = {
        { lp, BSEC_OUTPUT_IAQ },
        { lp, BSEC_OUTPUT_STATIC_IAQ },
        { lp, BSEC_OUTPUT_CO2_EQUIVALENT },
        { lp, BSEC_OUTPUT_BREATH_VOC_EQUIVALENT },
        { lp, BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_TEMPERATURE },
        { lp, BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_HUMIDITY },
        { lp, BSEC_OUTPUT_RAW_PRESSURE },
    };
    bsec_sensor_configuration_t required_phys[BSEC_MAX_PHYSICAL_SENSOR];
    uint8_t n_req = BSEC_MAX_PHYSICAL_SENSOR;
    if (bsec_update_subscription(req, 7, required_phys, &n_req) != BSEC_OK) {
        fprintf(stderr, "bsec_update_subscription failed\n"); return 1;
    }

    int64_t last_save = now_ns();

    /* ── Boucle principale ────────────────────────────────── */
    while (1) {
        bsec_bme_settings_t settings;
        memset(&settings, 0, sizeof(settings));
        int64_t t0 = now_ns();

        bsec_library_return_t rc = bsec_sensor_control(t0, &settings);
        if (rc != BSEC_OK) { usleep(10000); continue; }

        /* Attendre le prochain appel demandé par BSEC */
        int64_t wait = settings.next_call - now_ns();
        if (wait > 0) usleep((uint32_t)(wait / 1000));

        if (!settings.trigger_measurement) continue;

        /* Configurer BME68x selon les consignes BSEC */
        struct bme68x_conf conf;
        bme68x_get_conf(&conf, &bme);
        conf.os_hum  = settings.humidity_oversampling;
        conf.os_temp = settings.temperature_oversampling;
        conf.os_pres = settings.pressure_oversampling;
        bme68x_set_conf(&conf, &bme);

        struct bme68x_heatr_conf heatr;
        heatr.enable    = settings.run_gas ? BME68X_ENABLE : BME68X_DISABLE;
        heatr.heatr_temp = settings.heater_temperature;
        heatr.heatr_dur  = settings.heating_duration;
        bme68x_set_heatr_conf(BME68X_FORCED_MODE, &heatr, &bme);
        bme68x_set_op_mode(BME68X_FORCED_MODE, &bme);

        /* Attendre fin de mesure */
        uint32_t meas_us = bme68x_get_meas_dur(BME68X_FORCED_MODE, &conf, &bme)
                         + (uint32_t)heatr.heatr_dur * 1000u;
        usleep(meas_us);

        /* Lire données brutes */
        struct bme68x_data raw;
        uint8_t n_data = 0;
        if (bme68x_get_data(BME68X_FORCED_MODE, &raw, &n_data, &bme) != BME68X_OK
            || n_data == 0) continue;

        /* Construire inputs BSEC */
        int64_t ts = now_ns();
        bsec_input_t inputs[5];
        uint8_t n_in = 0;

        if (settings.process_data & BSEC_PROCESS_TEMPERATURE) {
            inputs[n_in] = (bsec_input_t){ ts, raw.temperature, 1, BSEC_INPUT_TEMPERATURE };
            n_in++;
        }
        if (settings.process_data & BSEC_PROCESS_HUMIDITY) {
            inputs[n_in] = (bsec_input_t){ ts, raw.humidity, 1, BSEC_INPUT_HUMIDITY };
            n_in++;
        }
        if (settings.process_data & BSEC_PROCESS_PRESSURE) {
            inputs[n_in] = (bsec_input_t){ ts, raw.pressure, 1, BSEC_INPUT_PRESSURE };
            n_in++;
        }
        if (settings.process_data & BSEC_PROCESS_GAS) {
            inputs[n_in] = (bsec_input_t){ ts, raw.gas_resistance, 1, BSEC_INPUT_GASRESISTOR };
            n_in++;
        }
        /* Offset thermique ambiant (0 = pas de correction externe) */
        inputs[n_in] = (bsec_input_t){ ts, 0.0f, 1, BSEC_INPUT_HEATSOURCE };
        n_in++;

        /* Exécuter le modèle BSEC */
        bsec_output_t outs[BSEC_NUMBER_OUTPUTS];
        uint8_t n_out = BSEC_NUMBER_OUTPUTS;
        if (bsec_do_steps(inputs, n_in, outs, &n_out) != BSEC_OK || n_out == 0)
            continue;

        /* Extraire les sorties */
        float iaq = -1, s_iaq = -1, co2 = -1, voc = -1;
        float temp = -999, hum = -1, pres = -1;
        uint8_t acc = 0;
        for (uint8_t i = 0; i < n_out; i++) {
            switch (outs[i].sensor_id) {
            case BSEC_OUTPUT_IAQ:
                iaq = outs[i].signal; acc = outs[i].accuracy; break;
            case BSEC_OUTPUT_STATIC_IAQ:
                s_iaq = outs[i].signal; break;
            case BSEC_OUTPUT_CO2_EQUIVALENT:
                co2 = outs[i].signal; break;
            case BSEC_OUTPUT_BREATH_VOC_EQUIVALENT:
                voc = outs[i].signal; break;
            case BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_TEMPERATURE:
                temp = outs[i].signal; break;
            case BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_HUMIDITY:
                hum = outs[i].signal; break;
            case BSEC_OUTPUT_RAW_PRESSURE:
                pres = outs[i].signal / 100.0f; break;   /* Pa → hPa */
            }
        }

        /* Sortie JSON sur stdout */
        printf("{\"iaq\":%.1f,\"iaq_accuracy\":%d,\"static_iaq\":%.1f,"
               "\"co2_eq\":%.1f,\"voc_eq\":%.2f,"
               "\"temperature\":%.1f,\"humidity\":%.1f,\"pressure\":%.1f}\n",
               iaq, acc, s_iaq, co2, voc, temp, hum, pres);
        fflush(stdout);

        /* Sauvegarde périodique de l'état BSEC */
        if (now_ns() - last_save > (int64_t)STATE_SAVE_S * 1000000000LL) {
            save_state();
            last_save = now_ns();
        }
    }
    return 0;
}
