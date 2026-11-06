import json
import struct
import time
import ubinascii
import machine
import network

try:
    import dht
except ImportError:
    dht = None

import config as C
from edge import StateTracker, classify, compute_tilt, interval, normalize_moisture

# Pinos de Hardware conforme o ESP32 de 38 pinos
adc_moisture = machine.ADC(machine.Pin(34))
adc_moisture.atten(machine.ADC.ATTN_11DB)
adc_moisture.width(machine.ADC.WIDTH_12BIT)

i2c = machine.I2C(0, scl=machine.Pin(22), sda=machine.Pin(21), freq=400000)
MPU_ADDR = 0x68

led_alert = machine.Pin(2, machine.Pin.OUT)
dht_sensor = dht.DHT22(machine.Pin(15)) if dht else None

rain_tips_ms = []
total_tips = 0
last_tip_ms = 0
ntp_synced = False


def _on_rain_pulse(pin):
    global total_tips, last_tip_ms
    now = time.ticks_ms()
    if time.ticks_diff(now, last_tip_ms) >= 100:
        last_tip_ms = now
        total_tips += 1
        rain_tips_ms.append(now)


btn_rain = machine.Pin(27, machine.Pin.IN, machine.Pin.PULL_UP)
btn_rain.irq(trigger=machine.Pin.IRQ_FALLING, handler=_on_rain_pulse)


def init_mpu():
    try:
        i2c.writeto_mem(MPU_ADDR, 0x6B, b'\x00')
    except Exception:
        pass


def read_mpu_tilt():
    raw = i2c.readfrom_mem(MPU_ADDR, 0x3B, 6)
    ax, ay, az = struct.unpack('>hhh', raw)
    return compute_tilt(ax / 16384.0, ay / 16384.0, az / 16384.0)


def read_rain_60s(now_ms):
    while rain_tips_ms and time.ticks_diff(now_ms, rain_tips_ms[0]) > 60000:
        rain_tips_ms.pop(0)
    return round(len(rain_tips_ms) * C.MM_PER_TIP, 2)


def connect_wifi():
    global ntp_synced
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)

    if wlan.isconnected():
        return

    print('Conectando ao Wi-Fi Wokwi-GUEST...')
    try:
        wlan.connect('Wokwi-GUEST', '')
    except OSError:
        # Se já estiver tentando conectar em background, apenas aguarda concluir
        pass

    t0 = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > 20000:
            raise OSError('Timeout no Wi-Fi Wokwi-GUEST')
        time.sleep_ms(200)

    print('Wi-Fi conectado! IP:', wlan.ifconfig()[0])
    if not ntp_synced:
        try:
            import ntptime
            ntptime.settime()
            ntp_synced = True
        except Exception:
            pass


"""
def connect_wifi():
    global ntp_synced
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        return

    # Reinicia o rádio Wi-Fi limpo para evitar travamento no Wokwi
    wlan.active(False)
    time.sleep_ms(100)
    wlan.active(True)
    wlan.connect('Wokwi-GUEST', '')

    t0 = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > 8000:
            raise OSError('Timeout no Wi-Fi Wokwi-GUEST')
        time.sleep_ms(100)

    if not ntp_synced:
        try:
            import ntptime
            ntptime.settime()
            ntp_synced = True
        except Exception:
            pass
        """


def publish_batch(alert_json, telemetry_json):
    topic_tel = f'{C.PREFIX}/telemetria'
    topic_alr = f'{C.PREFIX}/alertas'

    if C.PORT == 8883:
        from mqtt_tls import Publisher
        pub = Publisher()
        try:
            pub.connect()
            if alert_json:
                pub.publish(topic_alr, alert_json, qos=1)
                print('PUBACK recebido')
            if telemetry_json:
                pub.publish(topic_tel, telemetry_json, qos=0)
        finally:
            pub.close()
    else:
        from umqtt.simple import MQTTClient
        cli = MQTTClient(
            C.DEVICE_ID, C.BROKER, port=C.PORT,
            user=C.USER or None, password=C.PASSWORD or None, keepalive=30
        )
        cli.connect()
        try:
            if alert_json:
                cli.publish(topic_alr.encode(), alert_json.encode(), qos=1)
                print('PUBACK recebido')
            if telemetry_json:
                cli.publish(topic_tel.encode(), telemetry_json.encode(), qos=0)
        finally:
            cli.disconnect()


def main():
    init_mpu()
    boot_id = ubinascii.hexlify(machine.unique_id()).decode() + f'-{time.ticks_ms()}'
    tracker = StateTracker(initial='NORMAL', recovery_reads=3)
    seq = 0
    alert_queue = []
    dropped_alerts = 0
    last_send_ms = None
    next_retry_ms = 0
    retry_delay_ms = 5000

    while True:
        t_start = time.ticks_ms()
        seq += 1

        moisture = normalize_moisture(adc_moisture.read())
        rain_60s = read_rain_60s(t_start)
        rain_total = round(total_tips * C.MM_PER_TIP, 2)

        tilt = None
        err_msg = None
        fault = False
        try:
            tilt = read_mpu_tilt()
        except Exception as e:
            fault = True
            err_msg = str(e)

        if dht_sensor:
            try:
                dht_sensor.measure()
            except Exception:
                pass

        raw_state = classify(moisture, tilt, rain_60s, fault)
        state, changed, prev_state = tracker.update(raw_state)
        send_interval = interval(state)

        led_alert.value(1 if state in ('ALERTA', 'FALHA_SENSOR') else 0)

        data = {
            'schema': 1,
            'device_id': C.DEVICE_ID,
            'boot_id': boot_id,
            'seq': seq,
            'ts_utc': None,
            'uptime_ms': t_start,
            'umidade_pct': moisture,
            'inclinacao_deg': tilt,
            'chuva_60s_mm': rain_60s,
            'chuva_total_boot_mm': rain_total,
            'estado': state,
            'erro_sensor': err_msg,
            'intervalo_envio_ms': send_interval,
            'alertas_descartados': dropped_alerts,
        }

        if changed or (seq == 1 and state != 'NORMAL'):
            ev = dict(data)
            ev['event_id'] = f'{boot_id}-{seq}'
            ev['estado_anterior'] = None if seq == 1 else prev_state
            if len(alert_queue) >= 32:
                alert_queue.pop(0)
                dropped_alerts += 1
            alert_queue.append(json.dumps(ev))

        due = (
            last_send_ms is None or
            changed or
            time.ticks_diff(t_start, last_send_ms) >= send_interval
        )

        tel_json = json.dumps(data)
        print(tel_json)

        if due or alert_queue:
            if C.OFFLINE:
                print('OFFLINE · Simulando envio sem conexão MQTT')
                if alert_queue:
                    alert_queue.pop(0)
                last_send_ms = t_start
            elif time.ticks_diff(t_start, next_retry_ms) >= 0:
                try:
                    connect_wifi()
                    alert_to_send = alert_queue[0] if alert_queue else None
                    publish_batch(alert_to_send, tel_json if due else None)
                    if alert_to_send:
                        alert_queue.pop(0)
                    if due:
                        last_send_ms = t_start
                    retry_delay_ms = 5000
                    next_retry_ms = t_start
                except Exception as e:
                    print('REDE/TLS/MQTT:', e)
                    next_retry_ms = time.ticks_add(t_start, retry_delay_ms)
                    retry_delay_ms = min(60000, retry_delay_ms * 2)

        elapsed = time.ticks_diff(time.ticks_ms(), t_start)
        time.sleep_ms(max(50, C.SCAN_MS - elapsed))


if __name__ == '__main__':
    main()