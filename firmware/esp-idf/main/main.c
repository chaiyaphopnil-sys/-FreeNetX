/*
 * FreeNet-X Fleet Node — ESP-IDF build
 * Board: DOIT ESP32 DEVKIT V1 / ESP32-WROOM-32
 *
 * This build uses ESP-IDF (not Arduino core) so that NAPT internet sharing
 * (STA uplink -> Wi-Fi AP) is supported by lwIP. It keeps the fleet telemetry
 * features: AP + STA, 30s heartbeat, config fetch, event reporting, status LED,
 * auto-reconnect.
 *
 * IMPORTANT:
 *  - USB on this board is USB-to-UART (power/programming/serial only).
 *    There is NO USB Ethernet gadget / RNDIS / ECM. Internet sharing is done
 *    exclusively over Wi-Fi (NAPT), never over USB.
 *  - Lab use only: HTTP to a trusted LAN server. Do not expose publicly.
 *  - No scanning, credential capture, OTA, mesh, or self-propagation.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_event.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_http_client.h"
#include "driver/gpio.h"
#include "esp_mac.h"
#include "lwip/ip_addr.h"
#include "lwip/ip4_addr.h"
#include "esp_adc/adc_oneshot.h"
#include "quantize_utils.h"
#include "esp_pm.h"
#include "esp_private/esp_clk.h"
/* ---------- Compile-time transport selection ---------- */
#ifndef USE_HTTPS
#define USE_HTTPS 0
#endif

#include "cJSON.h"

#if USE_HTTPS
#include "esp_crt_bundle.h"
#endif

/* ---------- Node configuration ----------
 * Values come from main/Kconfig.projbuild and can be set with
 *   idf.py menuconfig  ->  "FreeNet-X Node Configuration"
 * or by appending to sdkconfig.defaults, e.g.
 *   CONFIG_FREENETX_UPLINK_SSID="MyWiFi"
 * This keeps credentials out of the source file. */
#define UPLINK_SSID   CONFIG_FREENETX_UPLINK_SSID
#define UPLINK_PASS   CONFIG_FREENETX_UPLINK_PASSWORD

#define AP_SSID       CONFIG_FREENETX_AP_SSID
#define AP_PASS       CONFIG_FREENETX_AP_PASSWORD

#define NODE_ID       CONFIG_FREENETX_NODE_ID
#define NODE_TOKEN    CONFIG_FREENETX_NODE_TOKEN
#define API_BASE      CONFIG_FREENETX_API_BASE

/* ---------- Status LED ----------
 * On the DOIT ESP32 DEVKIT V1 the on-board LED is on GPIO2 and is blue.
 * Solid on  = node working (AP up; uplink connected when one is configured)
 * Slow blink = configured uplink is disconnected / reconnecting
 * The colour itself is fixed by the LED hardware; this firmware only
 * switches it on/off. */
#define STATUS_LED              CONFIG_FREENETX_STATUS_LED_GPIO
#ifdef CONFIG_FREENETX_STATUS_LED_ACTIVE_HIGH
#define STATUS_LED_ACTIVE_HIGH  1
#else
#define STATUS_LED_ACTIVE_HIGH  0
#endif

/* ---------- Timing ---------- */
#define SYNC_INTERVAL_MS       CONFIG_FREENETX_SYNC_INTERVAL_MS
#define CONFIG_POLL_EVERY_N    1
#define STA_RECONNECT_DELAY_MS 2000

/* DHCP server option: offer a DNS server to clients (value from lwIP). */
#define DHCPS_OFFER_DNS        0x02

/* Optional end-to-end internet reachability check (2xx/3xx = online). */
#define INTERNET_CHECK_URL     CONFIG_FREENETX_INTERNET_CHECK_URL

static const char *TAG = "FreeNetX";

static volatile bool s_wifi_connected = false;
static bool          s_uplink_configured = false;
static volatile int  s_internet_state = -1; /* -1 unknown, 0 offline, 1 online */

/* Analog sensor (ADC1) + quantized sample shared with the heartbeat task. */
static adc_oneshot_unit_handle_t s_adc = NULL;
static adc_channel_t             s_adc_channel = ADC_CHANNEL_6;
static volatile int              s_adc_mv = -1;
static volatile int8_t           s_adc_q  = 0;

/* Dynamic CPU clock scaling state. */
static SemaphoreHandle_t s_cpu_cfg_mutex = NULL;
static volatile int      s_cpu_boost     = 0; /* >0 while HTTP work is active */
static volatile int      s_cpu_freq_mhz  = 0; /* last applied frequency */
static volatile uint32_t s_cpu_load_pct  = 0; /* last measured utilization */
#if CONFIG_FREENETX_CPU_SELFTEST
static volatile bool     s_cpu_selftest  = false;
#endif
static uint32_t      s_sync_counter = 0;
static uint32_t      s_boot_at_ms   = 0;
static esp_timer_handle_t s_reconnect_timer = NULL;

/* ---------- Helpers ---------- */
static uint32_t now_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static void led_init(void) {
#if STATUS_LED >= 0
    gpio_reset_pin(STATUS_LED);
    gpio_set_direction(STATUS_LED, GPIO_MODE_OUTPUT);
#endif
}

static void led_write(bool on) {
#if STATUS_LED >= 0
#if STATUS_LED_ACTIVE_HIGH
    gpio_set_level(STATUS_LED, on ? 1 : 0);
#else
    gpio_set_level(STATUS_LED, on ? 0 : 1);
#endif
#else
    (void)on;
#endif
}

/* Drives the status LED from the node's actual state. Runs as its own task so
 * a blocking HTTP request can never leave the LED in a stale state. */
static void status_led_task(void *pv) {
    (void)pv;
    while (1) {
        /* AP-only mode (placeholder uplink) is a valid working state, so the
         * light is solid immediately after boot. Once a real uplink is
         * configured, the light only stays solid while that uplink is up. */
        if (!s_uplink_configured) {
            /* AP-only mode: solid. */
            led_write(true);
            vTaskDelay(pdMS_TO_TICKS(250));
        } else if (!s_wifi_connected) {
            /* Uplink down: slow blink. */
            led_write(false);
            vTaskDelay(pdMS_TO_TICKS(700));
            led_write(true);
            vTaskDelay(pdMS_TO_TICKS(150));
        } else if (s_internet_state == 0) {
            /* Uplink up but no internet: double blink. */
            led_write(true);
            vTaskDelay(pdMS_TO_TICKS(120));
            led_write(false);
            vTaskDelay(pdMS_TO_TICKS(120));
            led_write(true);
            vTaskDelay(pdMS_TO_TICKS(120));
            led_write(false);
            vTaskDelay(pdMS_TO_TICKS(640));
        } else {
            /* Uplink up and online (or not checked yet): solid. */
            led_write(true);
            vTaskDelay(pdMS_TO_TICKS(250));
        }
    }
}

/* ---------- Wi-Fi (AP + STA) ---------- */
static void reconnect_timer_cb(void *arg) {
    ESP_LOGI(TAG, "Reconnecting to uplink...");
    esp_wifi_connect();
}

static void schedule_reconnect(void) {
    if (!s_reconnect_timer) {
        esp_wifi_connect();
        return;
    }
    /* Restart the one-shot timer; ignore "already running". */
    esp_timer_stop(s_reconnect_timer);
    esp_timer_start_once(s_reconnect_timer,
                         (uint64_t)STA_RECONNECT_DELAY_MS * 1000ULL);
}

/* Give AP clients the uplink's DNS server. Without this the DHCP server
 * advertises 192.168.4.1 as DNS, and clients can reach IPs by number but
 * cannot resolve names. The DHCP server must be stopped while the option is
 * changed, otherwise the new value is not propagated to clients. */
static void ap_propagate_dns(void) {
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_t *ap  = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (!sta || !ap) return;

    esp_netif_dns_info_t dns = {0};
    if (esp_netif_get_dns_info(sta, ESP_NETIF_DNS_MAIN, &dns) != ESP_OK ||
        dns.ip.type != ESP_IPADDR_TYPE_V4 ||
        dns.ip.u_addr.ip4.addr == 0) {
        ESP_LOGW(TAG, "Uplink has no usable IPv4 DNS; AP clients must use an IP");
        return;
    }

    uint8_t offer = DHCPS_OFFER_DNS;
    esp_netif_dhcps_stop(ap);
    esp_netif_dhcps_option(ap, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER,
                           &offer, sizeof(offer));
    esp_err_t err = esp_netif_set_dns_info(ap, ESP_NETIF_DNS_MAIN, &dns);
    esp_netif_dhcps_start(ap);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "AP DHCP DNS -> " IPSTR, IP2STR(&dns.ip.u_addr.ip4));
    } else {
        ESP_LOGW(TAG, "Could not set AP DNS: %s", esp_err_to_name(err));
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                              int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *e = (wifi_event_sta_disconnected_t *)data;
        ESP_LOGW(TAG, "STA disconnected (reason=%d); retry in %d ms",
                 e ? e->reason : -1, STA_RECONNECT_DELAY_MS);
        s_wifi_connected = false;
        schedule_reconnect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "STA got IP " IPSTR, IP2STR(&e->ip_info.ip));
        s_wifi_connected = true;
        if (s_reconnect_timer) esp_timer_stop(s_reconnect_timer);

        /* Log RSSI and channel for diagnostics. */
        wifi_ap_record_t ap_info;
        if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
            ESP_LOGI(TAG, "STA RSSI=%d dBm, channel=%d, authmode=%d",
                     ap_info.rssi, ap_info.primary, ap_info.authmode);
        }

        /* Route this node's own HTTP traffic (heartbeat/config) via the uplink,
         * not via the AP interface. */
        esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (sta_netif) esp_netif_set_default_netif(sta_netif);

        /* DNS for AP clients + NAPT so clients share the STA uplink. */
        ap_propagate_dns();
        esp_netif_t *ap_netif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
        if (ap_netif) {
            esp_err_t err = esp_netif_napt_enable(ap_netif);
            if (err == ESP_OK) {
                ESP_LOGI(TAG, "NAPT enabled on AP interface");
            } else {
                ESP_LOGW(TAG, "NAPT enable failed: %s (check CONFIG_LWIP_IPV4_NAPT + CONFIG_LWIP_IP_FORWARD)",
                         esp_err_to_name(err));
            }
        }
    } else if (base == IP_EVENT && id == IP_EVENT_AP_STAIPASSIGNED) {
        ip_event_ap_staipassigned_t *e = (ip_event_ap_staipassigned_t *)data;
        ESP_LOGI(TAG, "AP client got IP " IPSTR " (" MACSTR ")",
                 IP2STR(&e->ip), MAC2STR(e->mac));
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *e = (wifi_event_ap_staconnected_t *)data;
        ESP_LOGI(TAG, "Client connected: " MACSTR " (AID=%d)", MAC2STR(e->mac), e->aid);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *e = (wifi_event_ap_stadisconnected_t *)data;
        ESP_LOGI(TAG, "Client disconnected: " MACSTR " (AID=%d)", MAC2STR(e->mac), e->aid);
    }
}

static void wifi_init(void) {
    /* NVS */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();

    /* Static AP IP (default is 192.168.4.1 in IDF, but set explicitly).
     * DHCP server must be stopped first, otherwise set_ip_info fails. */
    esp_netif_t *ap_netif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (ap_netif) {
        esp_netif_dhcps_stop(ap_netif);
        esp_netif_ip_info_t ip_info = {0};
        IP4_ADDR(&ip_info.ip, 192, 168, 4, 1);
        IP4_ADDR(&ip_info.gw, 192, 168, 4, 1);
        IP4_ADDR(&ip_info.netmask, 255, 255, 255, 0);
        ESP_ERROR_CHECK(esp_netif_set_ip_info(ap_netif, &ip_info));
        ESP_ERROR_CHECK(esp_netif_dhcps_start(ap_netif));
    }

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    const esp_timer_create_args_t recon_args = {
        .callback = &reconnect_timer_cb,
        .name = "sta_reconnect",
    };
    ESP_ERROR_CHECK(esp_timer_create(&recon_args, &s_reconnect_timer));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                       &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, ESP_EVENT_ANY_ID,
                                                       &wifi_event_handler, NULL, NULL));

    /* AP config */
    wifi_config_t ap_cfg = {0};
    strncpy((char *)ap_cfg.ap.ssid, AP_SSID, sizeof(ap_cfg.ap.ssid));
    ap_cfg.ap.ssid_len = strlen(AP_SSID);
    ap_cfg.ap.channel = 6;
    ap_cfg.ap.max_connection = 4;
    if (strlen(AP_PASS) == 0) {
        ap_cfg.ap.authmode = WIFI_AUTH_OPEN;
    } else {
        strncpy((char *)ap_cfg.ap.password, AP_PASS, sizeof(ap_cfg.ap.password));
        ap_cfg.ap.authmode = WIFI_AUTH_WPA2_PSK;
    }

    /* STA config.
     * Accept any security mode (open public hotspot, WPA/WPA2/WPA3).
     * The SSID must still be configured explicitly: this firmware never
     * auto-joins arbitrary networks or captures credentials. */
    wifi_config_t sta_cfg = {0};
    strncpy((char *)sta_cfg.sta.ssid, UPLINK_SSID, sizeof(sta_cfg.sta.ssid));
    strncpy((char *)sta_cfg.sta.password, UPLINK_PASS, sizeof(sta_cfg.sta.password));
    sta_cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
    sta_cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    sta_cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    sta_cfg.sta.failure_retry_cnt = 3;
    sta_cfg.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);

    ESP_LOGI(TAG, "AP+STA started; AP SSID=%s", AP_SSID);
}

/* ---------- HTTP helpers ---------- */
static int http_read_body(esp_http_client_handle_t client, char *buf, int maxlen) {
    int total = 0;
    while (total < maxlen - 1) {
        int r = esp_http_client_read(client, buf + total, maxlen - 1 - total);
        if (r <= 0) break;
        total += r;
    }
    buf[total] = '\0';
    return total;
}

static int get_sta_rssi(void) {
    wifi_ap_record_t ap = {0};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) return ap.rssi;
    return 0;
}

static void get_sta_ip_str(char *out, size_t outlen) {
    out[0] = '\0';
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (!sta) return;
    esp_netif_ip_info_t info = {0};
    if (esp_netif_get_ip_info(sta, &info) == ESP_OK) {
        esp_ip4addr_ntoa(&info.ip, out, outlen);
    }
}

static bool send_heartbeat(void) {
    if (!s_wifi_connected) return false;

    char url[256];
    snprintf(url, sizeof(url), "%s/v1/heartbeat", API_BASE);

    char ip_str[16];
    get_sta_ip_str(ip_str, sizeof(ip_str));

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "node_id", NODE_ID);
    cJSON_AddStringToObject(root, "firmware", "2.0.0-esp-idf");
    cJSON_AddNumberToObject(root, "rssi", get_sta_rssi());
    cJSON_AddStringToObject(root, "ip_address", ip_str);
    cJSON_AddNumberToObject(root, "uptime_seconds", (now_ms() - s_boot_at_ms) / 1000);
    cJSON_AddBoolToObject(root, "internet_ok", s_internet_state == 1);
    cJSON_AddNumberToObject(root, "cpu_mhz", s_cpu_freq_mhz);
    cJSON_AddNumberToObject(root, "cpu_load_pct", s_cpu_load_pct);
    if (s_adc_mv >= 0) {
        cJSON_AddNumberToObject(root, "adc_mv", s_adc_mv);
        cJSON_AddNumberToObject(root, "adc_q", s_adc_q);
    }

    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) return false;

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 10000,
    };
#if USE_HTTPS
    cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    char auth[128];
    snprintf(auth, sizeof(auth), "Bearer %s", NODE_TOKEN);
    esp_http_client_set_header(client, "Authorization", auth);
    esp_http_client_set_post_field(client, body, strlen(body));

    esp_err_t err = esp_http_client_perform(client);
    int status = 0;
    if (err == ESP_OK) status = esp_http_client_get_status_code(client);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "heartbeat HTTP err: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "[SYNC] heartbeat HTTP=%d", status);
        char resp[256];
        int n = http_read_body(client, resp, sizeof(resp));
        if (n > 0) ESP_LOGI(TAG, "%s", resp);
    }

    esp_http_client_cleanup(client);
    free(body);

    return (err == ESP_OK && status >= 200 && status < 300);
}

static bool fetch_config(void) {
    if (!s_wifi_connected) return false;

    char url[256];
    snprintf(url, sizeof(url), "%s/v1/config/%s", API_BASE, NODE_ID);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = 10000,
    };
#if USE_HTTPS
    cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    char auth[128];
    snprintf(auth, sizeof(auth), "Bearer %s", NODE_TOKEN);
    esp_http_client_set_header(client, "Authorization", auth);

    esp_err_t err = esp_http_client_perform(client);
    int status = 0;
    if (err == ESP_OK) status = esp_http_client_get_status_code(client);

    ESP_LOGI(TAG, "[CONFIG] HTTP=%d", status);

    if (err == ESP_OK && status == 200) {
        char buf[2048];
        int n = http_read_body(client, buf, sizeof(buf));
        if (n > 0) {
            cJSON *doc = cJSON_Parse(buf);
            if (doc) {
                cJSON *cfgobj = cJSON_GetObjectItem(doc, "config");
                if (cJSON_IsObject(cfgobj)) {
                    char *s = cJSON_PrintUnformatted(cfgobj);
                    ESP_LOGI(TAG, "[CONFIG] %s", s ? s : "{}");
                    if (s) free(s);
                    cJSON *si = cJSON_GetObjectItem(cfgobj, "sync_interval_ms");
                    if (cJSON_IsNumber(si)) {
                        ESP_LOGI(TAG, "[CONFIG] requested sync_interval_ms=%d (restart to apply)",
                                 si->valueint);
                    }
                }
                cJSON_Delete(doc);
            }
        }
    }

    esp_http_client_cleanup(client);
    return (err == ESP_OK && status >= 200 && status < 300);
}

static bool send_event(const char *source_type, const char *source_ref,
                      const char *event_type, cJSON *payload) {
    if (!s_wifi_connected) return false;

    char url[256];
    snprintf(url, sizeof(url), "%s/v1/events", API_BASE);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "node_id", NODE_ID);
    cJSON_AddStringToObject(root, "source_type", source_type);
    cJSON_AddStringToObject(root, "source_ref", source_ref);
    cJSON_AddStringToObject(root, "event_type", event_type);
    if (payload) {
        cJSON_AddItemToObject(root, "payload", cJSON_Duplicate(payload, 1));
    } else {
        cJSON_AddObjectToObject(root, "payload");
    }
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) return false;

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 10000,
    };
#if USE_HTTPS
    cfg.crt_bundle_attach = esp_crt_bundle_attach;
#endif
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    char auth[128];
    snprintf(auth, sizeof(auth), "Bearer %s", NODE_TOKEN);
    esp_http_client_set_header(client, "Authorization", auth);
    esp_http_client_set_post_field(client, body, strlen(body));

    esp_err_t err = esp_http_client_perform(client);
    int status = 0;
    if (err == ESP_OK) status = esp_http_client_get_status_code(client);
    ESP_LOGI(TAG, "[EVENT] HTTP=%d", status);

    esp_http_client_cleanup(client);
    free(body);
    return (err == ESP_OK && status >= 200 && status < 300);
}

static void send_rssi_event(void) {
    cJSON *p = cJSON_CreateObject();
    cJSON_AddNumberToObject(p, "rssi", get_sta_rssi());
    cJSON_AddNumberToObject(p, "uptime_seconds", (now_ms() - s_boot_at_ms) / 1000);
    send_event("owned", "esp32-rssi", "signal_sample", p);
    cJSON_Delete(p);
}

/* ---------- Analog sensor + quantization ---------- */
static bool gpio_to_adc1_channel(int gpio, adc_channel_t *ch)
{
    switch (gpio) {
        case 36: *ch = ADC_CHANNEL_0; return true;
        case 37: *ch = ADC_CHANNEL_1; return true;
        case 38: *ch = ADC_CHANNEL_2; return true;
        case 39: *ch = ADC_CHANNEL_3; return true;
        case 32: *ch = ADC_CHANNEL_4; return true;
        case 33: *ch = ADC_CHANNEL_5; return true;
        case 34: *ch = ADC_CHANNEL_6; return true;
        case 35: *ch = ADC_CHANNEL_7; return true;
        default: return false;
    }
}

static bool sensor_adc_init(void)
{
    if (CONFIG_FREENETX_ADC_SENSOR_GPIO < 0) {
        ESP_LOGI(TAG, "[SENSOR] ADC sampling disabled");
        return false;
    }
    if (!gpio_to_adc1_channel(CONFIG_FREENETX_ADC_SENSOR_GPIO, &s_adc_channel)) {
        ESP_LOGW(TAG, "[SENSOR] GPIO%d is not an ADC1 pin (use 32-39); disabled",
                 CONFIG_FREENETX_ADC_SENSOR_GPIO);
        return false;
    }

    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = ADC_UNIT_1,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_adc);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "[SENSOR] adc unit init failed: %s", esp_err_to_name(err));
        s_adc = NULL;
        return false;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    err = adc_oneshot_config_channel(s_adc, s_adc_channel, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "[SENSOR] adc channel init failed: %s", esp_err_to_name(err));
        adc_oneshot_del_unit(s_adc);
        s_adc = NULL;
        return false;
    }

    ESP_LOGI(TAG, "[SENSOR] ADC1 GPIO%d ready (quantize range %d..%d mV)",
             CONFIG_FREENETX_ADC_SENSOR_GPIO,
             CONFIG_FREENETX_ADC_SENSOR_MIN_MV,
             CONFIG_FREENETX_ADC_SENSOR_MAX_MV);
    return true;
}

static int sensor_read_mv(void)
{
    if (!s_adc) return -1;
    int raw = 0;
    if (adc_oneshot_read(s_adc, s_adc_channel, &raw) != ESP_OK) return -1;
    /* Approximate linear conversion for the demo. For calibrated values use
     * the adc_cali curve-fitting API; quantization error dominates here. */
    return (raw * 3300) / 4095;
}

static void sensor_task(void *pv)
{
    (void)pv;
    vTaskDelay(pdMS_TO_TICKS(5000));
    while (1) {
        int mv = sensor_read_mv();
        if (mv >= 0) {
            const float qmin = (float)CONFIG_FREENETX_ADC_SENSOR_MIN_MV;
            const float qmax = (float)CONFIG_FREENETX_ADC_SENSOR_MAX_MV;
            int8_t q = quantize_float_to_i8((float)mv, qmin, qmax);
            float restored = dequantize_i8_to_float(q, qmin, qmax);
            compressed_sensor_t pkt = {
                .node_id = 0x01,
                .quantized = q,
                .timestamp = now_ms(),
            };
            s_adc_mv = mv;
            s_adc_q  = q;
            ESP_LOGI(TAG, "[SENSOR] raw=%dmV q=%d restored=%.1fmV err=%.1fmV packet=%uB",
                     mv, q, restored, fabsf((float)mv - restored),
                     (unsigned)sizeof(pkt));
        }
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

/* ---------- Dynamic CPU clock scaling (DFS) ----------
 * The ESP32 can switch its CPU clock at runtime. This uses the ESP-IDF
 * power-management core (esp_pm) instead of Arduino's setCpuFrequencyMhz():
 * ESP-IDF needs esp_pm to keep Wi-Fi, the RTOS tick and the APB clock
 * consistent when the frequency changes.
 *
 * Workload is measured from FreeRTOS per-task run-time counters:
 *   < 30 %  -> idle tier   (80 MHz by default)
 *   30..70 %-> medium tier (160 MHz)
 *   > 70 %  -> high tier   (240 MHz)
 * HTTP requests also force the high tier while they run. Light sleep stays
 * disabled so the AP and NAPT never stop. */
#if CONFIG_FREENETX_CPU_SCALING
static void cpu_apply_freq(int mhz)
{
    if (s_cpu_cfg_mutex && xSemaphoreTake(s_cpu_cfg_mutex, portMAX_DELAY) != pdTRUE) {
        return;
    }

    esp_pm_config_t cfg = {
        .max_freq_mhz = mhz,
        .min_freq_mhz = mhz,
        .light_sleep_enable = false,
    };
    esp_err_t err = esp_pm_configure(&cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "[CPU] esp_pm_configure(%d MHz) failed: %s",
                 mhz, esp_err_to_name(err));
    } else {
        s_cpu_freq_mhz = esp_clk_cpu_freq() / 1000000;
    }

    if (s_cpu_cfg_mutex) xSemaphoreGive(s_cpu_cfg_mutex);
}

static void cpu_boost_begin(void)
{
    if (s_cpu_boost++ == 0) {
        cpu_apply_freq(CONFIG_FREENETX_CPU_FREQ_MAX_MHZ);
    }
}

static void cpu_boost_end(void)
{
    if (s_cpu_boost > 0) s_cpu_boost--;
}

/* Sum of the run-time counters of all non-idle tasks. On a dual-core ESP32
 * this grows by (cores x elapsed time) when every CPU is busy, so the caller
 * divides by the core count to get a 0..100 % figure. Idle tasks are skipped
 * because they otherwise consume the "rest" and always report 100 %. */
static uint32_t cpu_total_run_time_us(void)
{
    UBaseType_t n = uxTaskGetNumberOfTasks();
    TaskStatus_t *st = malloc((size_t)n * sizeof(TaskStatus_t));
    if (!st) return 0;

    uint32_t total = 0;
    UBaseType_t got = uxTaskGetSystemState(st, n, NULL);
    for (UBaseType_t i = 0; i < got; i++) {
        if (st[i].pcTaskName && strncmp(st[i].pcTaskName, "IDLE", 4) == 0) {
            continue; /* idle time is the inverse of workload */
        }
        total += (uint32_t)st[i].ulRunTimeCounter;
    }
    free(st);
    return total;
}

static uint32_t cpu_measure_load_pct(uint32_t window_ms)
{
    uint32_t t0 = cpu_total_run_time_us();
    uint32_t w0 = now_ms();

    vTaskDelay(pdMS_TO_TICKS(window_ms));

    uint32_t t1 = cpu_total_run_time_us();
    uint32_t elapsed_us = (now_ms() - w0) * 1000U;
    if (elapsed_us == 0) return 0;

    uint64_t load = ((uint64_t)(t1 - t0) * 100ULL) /
                    ((uint64_t)elapsed_us * CONFIG_FREERTOS_NUMBER_OF_CORES);
    if (load > 100ULL) load = 100ULL;
    return (uint32_t)load;
}

static void cpu_scaling_task(void *pv)
{
    (void)pv;

    /* Start at the high tier so boot and the first Wi-Fi association are fast. */
    cpu_apply_freq(CONFIG_FREENETX_CPU_FREQ_MAX_MHZ);

    while (1) {
        uint32_t load = cpu_measure_load_pct(CONFIG_FREENETX_CPU_SCALING_INTERVAL_MS);
        s_cpu_load_pct = load;

#if CONFIG_FREENETX_CPU_SELFTEST
        if (s_cpu_selftest) {
            continue; /* the self-test drives the frequency itself */
        }
#endif

        int target;
        if (s_cpu_boost > 0 || load > 70) {
            target = CONFIG_FREENETX_CPU_FREQ_MAX_MHZ;
        } else if (load >= 30) {
            target = CONFIG_FREENETX_CPU_FREQ_MED_MHZ;
        } else {
            target = CONFIG_FREENETX_CPU_FREQ_MIN_MHZ;
        }

        if (target != s_cpu_freq_mhz) {
            cpu_apply_freq(target);
        }

        ESP_LOGI(TAG, "[CPU] workload=%lu%% -> %d MHz (tiers %d/%d/%d)",
                 (unsigned long)load, s_cpu_freq_mhz,
                 CONFIG_FREENETX_CPU_FREQ_MIN_MHZ,
                 CONFIG_FREENETX_CPU_FREQ_MED_MHZ,
                 CONFIG_FREENETX_CPU_FREQ_MAX_MHZ);
    }
}

#if CONFIG_FREENETX_CPU_SELFTEST
static void cpu_selftest_task(void *pv);
#endif

static void cpu_scaling_init(void)
{
    s_cpu_cfg_mutex = xSemaphoreCreateMutex();

    esp_pm_config_t cfg = {
        .max_freq_mhz = CONFIG_FREENETX_CPU_FREQ_MAX_MHZ,
        .min_freq_mhz = CONFIG_FREENETX_CPU_FREQ_MIN_MHZ,
        .light_sleep_enable = false,
    };
    esp_err_t err = esp_pm_configure(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "[CPU] DFS init failed: %s (CPU stays at boot frequency)",
                 esp_err_to_name(err));
        return;
    }

    s_cpu_freq_mhz = esp_clk_cpu_freq() / 1000000;
    ESP_LOGI(TAG, "[CPU] DFS enabled: %d/%d/%d MHz tiers, light sleep off",
             CONFIG_FREENETX_CPU_FREQ_MIN_MHZ,
             CONFIG_FREENETX_CPU_FREQ_MED_MHZ,
             CONFIG_FREENETX_CPU_FREQ_MAX_MHZ);

    xTaskCreate(cpu_scaling_task, "cpu_scaler", 3072, NULL, 2, NULL);
#if CONFIG_FREENETX_CPU_SELFTEST
    xTaskCreate(cpu_selftest_task, "cpu_selftest", 3072, NULL, 2, NULL);
#endif
}

#if CONFIG_FREENETX_CPU_SELFTEST
static void cpu_selftest_task(void *pv)
{
    (void)pv;
    vTaskDelay(pdMS_TO_TICKS(1500));
    s_cpu_selftest = true;

    const int tiers[] = {
        CONFIG_FREENETX_CPU_FREQ_MAX_MHZ,
        CONFIG_FREENETX_CPU_FREQ_MED_MHZ,
        CONFIG_FREENETX_CPU_FREQ_MIN_MHZ,
    };
    for (size_t i = 0; i < sizeof(tiers) / sizeof(tiers[0]); i++) {
        cpu_apply_freq(tiers[i]);
        vTaskDelay(pdMS_TO_TICKS(200));
        ESP_LOGI(TAG, "[CPU] self-test: requested %d MHz, actual %d MHz",
                 tiers[i], s_cpu_freq_mhz);
        vTaskDelay(pdMS_TO_TICKS(1300));
    }

    s_cpu_selftest = false;
    ESP_LOGI(TAG, "[CPU] self-test done; normal workload scaling resumed");
    vTaskDelete(NULL);
}
#endif
#else
static void cpu_boost_begin(void) {}
static void cpu_boost_end(void) {}
static void cpu_scaling_init(void) {}
#endif /* CONFIG_FREENETX_CPU_SCALING */

/* ---------- Internet reachability check ---------- */
static bool check_internet(void) {
    if (INTERNET_CHECK_URL[0] == '\0') {
        s_internet_state = 1;   /* check disabled: assume online */
        return true;
    }
    if (!s_wifi_connected) {
        s_internet_state = -1;
        return false;
    }

    esp_http_client_config_t cfg = {
        .url = INTERNET_CHECK_URL,
        .method = HTTP_METHOD_GET,
        .timeout_ms = 8000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (!client) {
        s_internet_state = 0;
        return false;
    }

    esp_err_t err = esp_http_client_perform(client);
    int status = (err == ESP_OK) ? esp_http_client_get_status_code(client) : 0;
    esp_http_client_cleanup(client);

    bool ok = (err == ESP_OK && status >= 200 && status < 400);
    s_internet_state = ok ? 1 : 0;
    ESP_LOGI(TAG, "[NET] internet check HTTP=%d (%s)",
             status, ok ? "online" : "offline");
    return ok;
}

/* ---------- Heartbeat task ---------- */
static void heartbeat_task(void *pv) {
    vTaskDelay(pdMS_TO_TICKS(3000));
    while (1) {
        if (s_wifi_connected) {
            cpu_boost_begin();
            check_internet();
            bool ok = send_heartbeat();
            if (ok) {
                s_sync_counter++;
                if (s_sync_counter % CONFIG_POLL_EVERY_N == 0) {
                    fetch_config();
                }
                if (s_sync_counter % 5 == 0) {
                    send_rssi_event();
                }
            }
            cpu_boost_end();
            ESP_LOGI(TAG, "[MEM] free heap=%lu bytes, min=%lu",
                     (unsigned long)esp_get_free_heap_size(),
                     (unsigned long)esp_get_minimum_free_heap_size());
        }
        vTaskDelay(pdMS_TO_TICKS(SYNC_INTERVAL_MS));
    }
}

/* ---------- Background Process Queue Siphon ---------- */
/* FreeRTOS queue that buffers background tasks and dispatches them to the
 * PC over USB serial at a controlled rate, reducing micro-stuttering and
 * CPU load on the host. */

#define SIPHON_QUEUE_DEPTH     50
#define SIPHON_TASK_PAYLOAD    32
#define SIPHON_PRODUCE_MS      500   /* how often new tasks are generated */
#define SIPHON_DISPATCH_MS     100   /* throttle interval between sends */

typedef struct {
    uint32_t task_id;
    char     payload[SIPHON_TASK_PAYLOAD];
} siphon_task_t;

static QueueHandle_t s_siphon_queue = NULL;
static volatile uint32_t s_siphon_produced = 0;
static volatile uint32_t s_siphon_dispatched = 0;
static volatile uint32_t s_siphon_dropped = 0;

/* Producer: generates background tasks into the queue (non-blocking). */
static void siphon_producer_task(void *pv) {
    (void)pv;
    uint32_t id = 1;
    while (1) {
        if (uxQueueSpacesAvailable(s_siphon_queue) > 0) {
            siphon_task_t job;
            job.task_id = id++;
            snprintf(job.payload, sizeof(job.payload), "JOB_DATA_%lu", (unsigned long)job.task_id);
            if (xQueueSend(s_siphon_queue, &job, 0) == pdPASS) {
                s_siphon_produced++;
                ESP_LOGI(TAG, "[Siphon] Buffered task ID: %lu (queue: %u/%d)",
                         (unsigned long)job.task_id,
                         (unsigned)uxQueueMessagesWaiting(s_siphon_queue),
                         SIPHON_QUEUE_DEPTH);
            } else {
                s_siphon_dropped++;
            }
        } else {
            s_siphon_dropped++;
            ESP_LOGW(TAG, "[Siphon] Queue full, dropping task (dropped=%lu)",
                     (unsigned long)s_siphon_dropped);
        }
        vTaskDelay(pdMS_TO_TICKS(SIPHON_PRODUCE_MS));
    }
}

/* Consumer: dispatches tasks to PC one at a time at a throttled rate. */
static void siphon_consumer_task(void *pv) {
    (void)pv;
    siphon_task_t job;
    while (1) {
        if (xQueueReceive(s_siphon_queue, &job, pdMS_TO_TICKS(1000)) == pdPASS) {
            /* Send to PC over USB serial in a single line. */
            printf("SEND_TO_PC:%lu:%s\n", (unsigned long)job.task_id, job.payload);
            fflush(stdout);
            s_siphon_dispatched++;
            ESP_LOGI(TAG, "[Siphon] Dispatched task ID: %lu -> PC (remaining: %u)",
                     (unsigned long)job.task_id,
                     (unsigned)uxQueueMessagesWaiting(s_siphon_queue));
            /* Throttle: give the PC time to process each item. */
            vTaskDelay(pdMS_TO_TICKS(SIPHON_DISPATCH_MS));
        }
    }
}

static void siphon_init(void) {
    s_siphon_queue = xQueueCreate(SIPHON_QUEUE_DEPTH, sizeof(siphon_task_t));
    if (s_siphon_queue == NULL) {
        ESP_LOGE(TAG, "[Siphon] Failed to create task queue");
        return;
    }
    xTaskCreate(siphon_producer_task, "siphon_prod", 3072, NULL, 2, NULL);
    xTaskCreate(siphon_consumer_task, "siphon_cons", 3072, NULL, 2, NULL);
    ESP_LOGI(TAG, "[Siphon] Queue initialized (depth=%d, produce=%dms, dispatch=%dms)",
             SIPHON_QUEUE_DEPTH, SIPHON_PRODUCE_MS, SIPHON_DISPATCH_MS);
}

/* ---------- Entry point ---------- */
void app_main(void) {
    s_boot_at_ms = now_ms();
    s_uplink_configured = (strncmp(UPLINK_SSID, "YOUR_", 5) != 0);

    if (!s_uplink_configured) {
        ESP_LOGW(TAG, "UPLINK_SSID is still a placeholder; set it in menuconfig");
    }
    if (strstr(NODE_TOKEN, "REPLACE") != NULL) {
        ESP_LOGW(TAG, "NODE_TOKEN is still a placeholder; heartbeats will be rejected");
    }

    /* Light the status LED immediately: the board starts working as soon as
     * USB power is applied, with no button press or serial command needed. */
    led_init();
    led_write(true);

    cpu_scaling_init();
    wifi_init();
    ESP_LOGI(TAG, "Status LED on GPIO%d (solid = working)", STATUS_LED);

    if (sensor_adc_init()) {
        xTaskCreate(sensor_task, "sensor", 3072, NULL, 4, NULL);
    }
    xTaskCreate(status_led_task, "status_led", 2048, NULL, 3, NULL);
    xTaskCreate(heartbeat_task, "heartbeat", 8192, NULL, 5, NULL);

    siphon_init();

    ESP_LOGI(TAG, "FreeNet-X node started; AP=%s uplink=%s",
             AP_SSID, s_uplink_configured ? UPLINK_SSID : "(not configured)");
}
