"""Conversões físicas, classificação de risco e histerese de recuperação."""
import math
import config as C

SEVERITY = {
    'NORMAL': 0,
    'ATENCAO': 1,
    'ALERTA': 2,
    'FALHA_SENSOR': 3,
}


def normalize_moisture(adc_val):
    """Converte leitura ADC de 12 bits (0..4095) para umidade (0.0..100.0%)."""
    span = C.ADC_WET - C.ADC_DRY
    if span <= 0:
        return 0.0
    pct = (adc_val - C.ADC_DRY) * 100.0 / span
    return max(0.0, min(100.0, round(pct, 1)))


def compute_tilt(ax, ay, az):
    """Calcula a inclinação angular em graus a partir da aceleração (X, Y, Z)."""
    norm = math.sqrt(ax * ax + ay * ay + az * az)
    if norm < 0.1:
        raise ValueError('Aceleracao invalida (vetor nulo)')
    cos_theta = max(-1.0, min(1.0, abs(az) / norm))
    return round(math.degrees(math.acos(cos_theta)), 1)


def classify(moisture_pct, tilt_deg, rain_60s_mm, sensor_fault=False):
    """Retorna NORMAL, ATENCAO, ALERTA ou FALHA_SENSOR segundo os limiares."""
    if sensor_fault or tilt_deg is None:
        return 'FALHA_SENSOR'
    if (moisture_pct >= C.MOISTURE_ALERT or
            tilt_deg >= C.TILT_ALERT or
            rain_60s_mm >= C.RAIN_ALERT):
        return 'ALERTA'
    if (moisture_pct >= C.MOISTURE_WARN or
            tilt_deg >= C.TILT_WARN or
            rain_60s_mm >= C.RAIN_WARN):
        return 'ATENCAO'
    return 'NORMAL'


def interval(state):
    """Define a frequência de envio (ms) de acordo com o estado atual."""
    if state in ('ALERTA', 'FALHA_SENSOR'):
        return C.ALERT_MS
    if state == 'ATENCAO':
        return C.ATTENTION_MS
    return C.NORMAL_MS


class StateTracker:
    """Sobe o nível de risco imediatamente; exige 3 leituras para descer."""
    def __init__(self, initial='NORMAL', recovery_reads=3):
        self.state = initial
        self.recovery_reads = recovery_reads
        self.candidate = None
        self.count = 0

    def update(self, raw_state):
        prev = self.state
        if SEVERITY[raw_state] >= SEVERITY[self.state]:
            self.state = raw_state
            self.candidate = None
            self.count = 0
        else:
            if raw_state == self.candidate:
                self.count += 1
            else:
                self.candidate = raw_state
                self.count = 1
            if self.count >= self.recovery_reads:
                self.state = raw_state
                self.candidate = None
                self.count = 0
        return self.state, (self.state != prev), prev