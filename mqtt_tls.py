"""Publicador MQTT 3.1.1 minimo: QoS 0/1, sem subscribe ou QoS 2.
Conexao curta por lote: dispensa keepalive durante os 15 min de repouso.
"""
import socket
try:
    import ssl
except ImportError:
    import ussl as ssl
import config as C


def field(value):
    if isinstance(value, str):
        value = value.encode()
    return bytes((len(value) >> 8, len(value) & 255)) + value


def packet(header, body):
    size = len(body)
    out = bytearray([header])
    while True:
        digit = size % 128
        size //= 128
        out.append(digit | (128 if size else 0))
        if not size:
            return bytes(out) + body


class Publisher:
    def __init__(self):
        self.sock = None
        self.pid = 0

    def write(self, data):
        while data:
            n = self.sock.write(data)
            if not n:
                raise OSError('TLS write interrompido')
            data = data[n:]

    def read(self, n):
        out = b''
        while len(out) < n:
            chunk = self.sock.read(n - len(out))
            if not chunk:
                raise OSError('TLS read interrompido')
            out += chunk
        return out

    def connect(self):
        if C.PORT != 8883:
            raise ValueError('Este projeto exige porta TLS 8883')
        ca = getattr(C, 'CA_PEM', None)
        if ca:
            if isinstance(ca, str):
                ca = ca.encode()
        else:
            with open(C.CA_FILE, 'rb') as f:
                ca = f.read()
        raw = socket.socket()
        self.sock = raw
        raw.settimeout(5)
        addr = socket.getaddrinfo(C.BROKER, C.PORT)[0][-1]
        raw.connect(addr)
        if hasattr(ssl, 'SSLContext'):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=ca)
            self.sock = ctx.wrap_socket(raw, server_hostname=C.BROKER)
        else:
            self.sock = ssl.wrap_socket(raw, cert_reqs=ssl.CERT_REQUIRED,
                                        cadata=ca, server_hostname=C.BROKER)
        flags = 2 | (192 if C.USER else 0)
        body = field(b'MQTT') + bytes((4, flags, 0, 30)) + field(C.DEVICE_ID)
        if C.USER:
            body += field(C.USER) + field(C.PASSWORD)
        self.write(packet(0x10, body))
        if self.read(4) != b'\x20\x02\x00\x00':
            raise OSError('CONNACK recusado')

    def publish(self, topic, payload, qos):
        if qos not in (0, 1):
            raise ValueError('QoS permitido: 0 ou 1')
        self.pid = self.pid % 65535 + 1
        pid = bytes((self.pid >> 8, self.pid & 255))
        body = field(topic) + (pid if qos else b'') + payload.encode()
        self.write(packet(0x30 | (qos << 1), body))
        if qos and self.read(4) != b'\x40\x02' + pid:
            raise OSError('PUBACK ausente ou incorreto')

    def close(self):
        if self.sock:
            try:
                self.write(b'\xe0\x00')
            except Exception:
                pass
            self.sock.close()
            self.sock = None