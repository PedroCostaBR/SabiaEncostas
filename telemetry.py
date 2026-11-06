"""Assinante MQTT e buffer thread-safe para o painel SabiáEncostas."""
from collections import deque
import copy
from datetime import datetime, timezone
import json
import math
import ssl
import threading
import time
import uuid

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

VALID_STATES = ('NORMAL', 'ATENCAO', 'ALERTA', 'FALHA_SENSOR')


def _valid_num(val):
    return (
        isinstance(val, (int, float))
        and not isinstance(val, bool)
        and not math.isnan(val)
        and not math.isinf(val)
    )


def parse_reading(payload, expected_device_id):
    if not isinstance(payload, str):
        raise ValueError('Payload deve ser string')
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError('Formato JSON inválido')
    if data.get('device_id') != expected_device_id:
        raise ValueError('device_id incorreto')

    boot_id = data.get('boot_id')
    if not isinstance(boot_id, str) or not boot_id:
        raise ValueError('boot_id ausente')

    seq = data.get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ValueError('seq inválido')

    estado = data.get('estado')
    if estado not in VALID_STATES:
        raise ValueError('estado inválido')

    umidade = data.get('umidade_pct')
    if not _valid_num(umidade) or not (0.0 <= float(umidade) <= 100.0):
        raise ValueError('umidade_pct inválida')

    inclinacao = data.get('inclinacao_deg')
    if estado == 'FALHA_SENSOR':
        if inclinacao is not None and not _valid_num(inclinacao):
            raise ValueError('inclinacao_deg inválida')
    else:
        if not _valid_num(inclinacao):
            raise ValueError('inclinacao_deg obrigatória')

    for key in ('chuva_60s_mm', 'chuva_total_boot_mm'):
        val = data.get(key, 0.0)
        if not _valid_num(val) or float(val) < 0.0:
            raise ValueError(f'{key} inválido')

    intervalo = data.get('intervalo_envio_ms')
    if not _valid_num(intervalo) or float(intervalo) <= 0:
        raise ValueError('intervalo_envio_ms inválido')

    out = dict(data)
    out['umidade_pct'] = float(umidade)
    out['inclinacao_deg'] = None if inclinacao is None else float(inclinacao)
    out['chuva_60s_mm'] = float(data.get('chuva_60s_mm', 0.0))
    out['chuva_total_boot_mm'] = float(data.get('chuva_total_boot_mm', 0.0))
    out['intervalo_envio_ms'] = int(intervalo)
    out['recebido_em'] = datetime.now(timezone.utc).isoformat()
    return out


class TelemetryBuffer:
    def __init__(self, max_history=1000, max_events=100, max_seen=2000):
        self._lock = threading.Lock()
        self._history = deque(maxlen=max_history)
        self._events = deque(maxlen=max_events)
        self._seen_keys = deque(maxlen=max_seen)
        self._seen_events = deque(maxlen=max_events)
        self._latest = None
        self._latest_mono = None
        self._invalid = 0
        self._status = 'Aguardando conexão'

    def set_status(self, status):
        with self._lock:
            self._status = status

    def ingest(self, payload, expected_device_id, alert=False):
        try:
            reading = parse_reading(payload, expected_device_id)
        except Exception:
            with self._lock:
                self._invalid += 1
            return

        with self._lock:
            key = (reading['device_id'], reading['boot_id'], reading['seq'])
            if key not in self._seen_keys:
                self._seen_keys.append(key)
                self._history.append(copy.deepcopy(reading))

            if self._latest is None:
                self._latest = copy.deepcopy(reading)
                self._latest_mono = time.monotonic()
            else:
                same_boot = (reading['boot_id'] == self._latest['boot_id'])
                if not same_boot or reading['seq'] >= self._latest['seq']:
                    self._latest = copy.deepcopy(reading)
                    self._latest_mono = time.monotonic()

            event_id = reading.get('event_id')
            if alert or event_id is not None:
                eid = event_id or f"{reading['boot_id']}-{reading['seq']}"
                if eid not in self._seen_events:
                    self._seen_events.append(eid)
                    self._events.append(copy.deepcopy(reading))

    def snapshot(self):
        with self._lock:
            age = (
                0.0
                if self._latest_mono is None
                else max(0.0, time.monotonic() - self._latest_mono)
            )
            return {
                'latest': copy.deepcopy(self._latest),
                'history': copy.deepcopy(list(self._history)),
                'events': copy.deepcopy(list(self._events)),
                'invalid': self._invalid,
                'status': self._status,
                'age': age,
            }


class MQTTReceiver(TelemetryBuffer):
    def __init__(self, config):
        super().__init__()
        if mqtt is None:
            raise RuntimeError('paho-mqtt ausente')
        self.config = config
        cid = f'{config.DEVICE_ID}-dashboard-{uuid.uuid4().hex[:6]}'
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        if getattr(config, 'USER', ''):
            self.client.username_pw_set(config.USER, config.PASSWORD)
        if config.PORT == 8883:
            ctx = ssl.create_default_context()
            ca_pem = getattr(config, 'CA_PEM', None)
            if ca_pem:
                ctx.load_verify_locations(cadata=ca_pem)
            self.client.tls_set_context(ctx)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.set_status(f'Conectando a {config.BROKER}:{config.PORT}...')
        self.client.connect_async(config.BROKER, config.PORT, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(f'{self.config.PREFIX}/telemetria', qos=0)
            client.subscribe(f'{self.config.PREFIX}/alertas', qos=1)
            self.set_status('Conectado e assinando os tópicos')
        else:
            self.set_status(f'Erro MQTT: {reason_code}')

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.set_status('Desconectado · reconectando...')

    def _on_message(self, client, userdata, msg):
        is_alert = msg.topic.endswith('/alertas')
        payload = msg.payload.decode('utf-8', errors='replace')
        self.ingest(payload, self.config.DEVICE_ID, alert=is_alert)