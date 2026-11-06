import json
import unittest

from telemetry import TelemetryBuffer, parse_reading


def reading(**changes):
    data = dict(
        schema=1,
        device_id='encosta-01',
        boot_id='boot-a',
        seq=1,
        estado='NORMAL',
        umidade_pct=30,
        inclinacao_deg=0,
        chuva_60s_mm=0,
        chuva_total_boot_mm=0,
        intervalo_envio_ms=900000,
    )
    data.update(changes)
    return json.dumps(data)


class TelemetryTests(unittest.TestCase):
    def test_duplicate_alert_and_telemetry_keep_event_without_double_counting(self):
        buffer = TelemetryBuffer()
        buffer.ingest(reading(), 'encosta-01')
        buffer.ingest(reading(event_id='event-1'), 'encosta-01', alert=True)
        buffer.ingest(reading(event_id='event-1'), 'encosta-01', alert=True)
        state = buffer.snapshot()
        self.assertEqual(len(state['history']), 1)
        self.assertEqual(len(state['events']), 1)

    def test_late_event_does_not_replace_current_reading(self):
        buffer = TelemetryBuffer()
        buffer.ingest(reading(seq=10), 'encosta-01')
        buffer.ingest(reading(seq=2, estado='ALERTA', event_id='old'), 'encosta-01', True)
        self.assertEqual(buffer.snapshot()['latest']['seq'], 10)
        self.assertEqual(len(buffer.snapshot()['events']), 1)

    def test_reboot_accepts_sequence_reset(self):
        buffer = TelemetryBuffer()
        buffer.ingest(reading(seq=10), 'encosta-01')
        buffer.ingest(reading(boot_id='boot-b', seq=1), 'encosta-01')
        self.assertEqual(buffer.snapshot()['latest']['boot_id'], 'boot-b')

    def test_rejects_invalid_payloads_without_interrupting_receiver(self):
        buffer = TelemetryBuffer()
        for payload in (
            'not json',
            '[]',
            reading(umidade_pct=101),
            reading(umidade_pct=float('nan')),
            reading(device_id='outro'),
            reading(intervalo_envio_ms=0),
            reading(inclinacao_deg=None),
        ):
            buffer.ingest(payload, 'encosta-01')
        self.assertEqual(buffer.snapshot()['invalid'], 7)
        self.assertIsNone(buffer.snapshot()['latest'])
        buffer.ingest(reading(), 'encosta-01')
        self.assertIsNotNone(buffer.snapshot()['latest'])

    def test_sensor_failure_allows_missing_tilt(self):
        data = parse_reading(reading(estado='FALHA_SENSOR', inclinacao_deg=None), 'encosta-01')
        self.assertIsNone(data['inclinacao_deg'])

    def test_history_is_bounded_and_snapshot_is_independent(self):
        buffer = TelemetryBuffer()
        for seq in range(1, 1100):
            buffer.ingest(reading(seq=seq), 'encosta-01')
        state = buffer.snapshot()
        self.assertEqual(len(state['history']), 1000)
        state['latest']['estado'] = 'ALERTA'
        self.assertEqual(buffer.snapshot()['latest']['estado'], 'NORMAL')


if __name__ == '__main__':
    unittest.main()