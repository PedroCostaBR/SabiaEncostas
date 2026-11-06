"""Execute no computador: python -m streamlit run dashboard.py"""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import config as C
from firmeware.edge import classify, interval
from telemetry import MQTTReceiver, TelemetryBuffer

st.set_page_config(
    page_title='SabiáEncostas | Centro Operacional',
    page_icon='⛰️',
    layout='wide',
    initial_sidebar_state='expanded',
)

# Estilização CSS para visual de Centro de Comando da Defesa Civil
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.4rem;
        padding-bottom: 2rem;
    }
    .hero-box {
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
        color: #f8fafc;
        padding: 1.3rem 1.6rem;
        border-radius: 14px;
        border-left: 6px solid #38bdf8;
        margin-bottom: 1rem;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.12);
    }
    .hero-title {
        font-size: 1.75rem;
        font-weight: 800;
        margin: 0;
        letter-spacing: -0.4px;
    }
    .hero-sub {
        font-size: 0.92rem;
        color: #cbd5e1;
        margin-top: 0.3rem;
    }
    .status-banner {
        padding: 1.1rem 1.4rem;
        border-radius: 12px;
        margin-bottom: 1rem;
        border-left: 8px solid;
    }
    .status-title {
        font-size: 1.18rem;
        font-weight: 800;
        margin-bottom: 0.25rem;
    }
    .status-desc {
        font-size: 0.94rem;
        margin: 0;
        opacity: 0.95;
    }
    .st-normal {
        background-color: #ecfdf5;
        color: #065f46;
        border-left-color: #10b981;
    }
    .st-atencao {
        background-color: #fffbeb;
        color: #92400e;
        border-left-color: #f59e0b;
    }
    .st-alerta {
        background-color: #fef2f2;
        color: #991b1b;
        border-left-color: #ef4444;
    }
    .st-falha {
        background-color: #f3e8ff;
        color: #6b21a8;
        border-left-color: #9333ea;
    }
    .meta-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 0.6rem;
        margin-bottom: 1rem;
        align-items: center;
    }
    .badge {
        display: inline-block;
        padding: 0.28rem 0.75rem;
        border-radius: 999px;
        font-size: 0.8rem;
        font-weight: 700;
    }
    .badge-ok { background: #dcfce7; color: #166534; }
    .badge-warn { background: #fef3c7; color: #92400e; }
    .badge-info { background: #e0f2fe; color: #075985; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def receiver():
    return MQTTReceiver(C)


# Cabeçalho Principal Institucional
st.markdown(
    """
    <div class="hero-box">
        <div class="hero-title">⛰️ SabiáEncostas · Central Operacional de Monitoramento</div>
        <div class="hero-sub">
            Sistema Preventivo de Alerta para Deslizamento de Encostas, Taludes Rodoviários e APPs com IoT,
            Computação de Borda (ESP32) e Telemetria MQTT/TLS em Tempo Real
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown('### 🎛️ Controle de Operação')
    mode = st.radio(
        'Fonte dos dados',
        ['Demonstração', 'MQTT ao vivo'],
        help='Alterne entre simulação manual no próprio navegador ou recepção real do ESP32 no Wokwi via broker MQTT.',
    )

    st.divider()
    st.markdown('### 📡 Nó de Sensoriamento')
    st.markdown(f'**ID do Dispositivo:** `{C.DEVICE_ID}`')
    st.markdown(f'**Tópico Base:** `{C.PREFIX}`')

    if mode == 'Demonstração':
        st.divider()
        st.markdown('### 🧪 Simulador de Variáveis')
        st.caption('Ajuste os controles para simular cenários geotécnicos sem publicar no broker.')
        water = st.slider('💧 Umidade do solo (%)', 0, 100, 30)
        tilt = st.slider('📐 Inclinação angular (°)', 0.0, 20.0, 0.0, 0.5)
        rain = st.slider('🌧️ Chuva em 60 s (mm)', 0.0, 10.0, 0.0, 0.2)
        fault = st.checkbox('⚠️ Simular falha física do MPU-6050')
    else:
        st.divider()
        st.markdown('### 🔒 Broker Cloud (TLS)')
        st.code(f'{C.BROKER}:{C.PORT}', language='text')
        st.caption('Protocolos ativos: QoS 0 (Telemetria) e QoS 1 com PUBACK (Alertas prioritários).')

    st.divider()
    with st.expander('📏 Limiares da PoC (Config)', expanded=False):
        st.markdown(
            f"""
            - **Umidade:** Atenção ≥ `{C.MOISTURE_WARN}%` | Alerta ≥ `{C.MOISTURE_ALERT}%`
            - **Inclinação:** Atenção ≥ `{C.TILT_WARN}°` | Alerta ≥ `{C.TILT_ALERT}°`
            - **Chuva (60s):** Atenção ≥ `{C.RAIN_WARN} mm` | Alerta ≥ `{C.RAIN_ALERT} mm`
            """
        )

if mode == 'Demonstração':
    if 'demo_buffer' not in st.session_state:
        st.session_state.demo_buffer = TelemetryBuffer()
        st.session_state.demo_seq = 0
    source = st.session_state.demo_buffer
    source.set_status('Simulação Local Interativa')
else:
    try:
        source = receiver()
    except Exception:
        st.error(
            'Não foi possível iniciar o consumidor MQTT. '
            'Confira as credenciais em config.py, o certificado CA e as dependências locais.'
        )
        st.stop()


def fmt_metric(value, unit):
    return 'Indisponível' if value is None else f'{value:.1f} {unit}'


def calc_delta(history, key, unit):
    if len(history) < 2:
        return None
    curr = history[-1].get(key)
    prev = history[-2].get(key)
    if curr is None or prev is None:
        return None
    diff = curr - prev
    if abs(diff) < 1e-6:
        return f'0.0 {unit}'
    return f'{diff:+.1f} {unit}'


STATE_UI = {
    'NORMAL': (
        'st-normal',
        '🟢 NÍVEL NORMAL · ESTABILIDADE GEOTÉCNICA',
        'Variáveis dentro dos limites basais de segurança. O nó ESP32 opera em economia de energia (transmissão a cada 15 min).',
    ),
    'ATENCAO': (
        'st-atencao',
        '🟡 NÍVEL DE ATENÇÃO · MONITORAMENTO INTENSIFICADO',
        'Elevação preventiva detectada. Amostragem dinâmica reduzida para 10 s. Recomenda-se aviso às equipes técnicas da Defesa Civil.',
    ),
    'ALERTA': (
        'st-alerta',
        '🔴 ALERTA CRÍTICO · RISCO IMINENTE DE DESLIZAMENTO',
        'Limiar crítico ultrapassado! Telemetria contínua (2 s) ativa. Acionar protocolo de evacuação preventiva e bloqueio de vias.',
    ),
    'FALHA_SENSOR': (
        'st-falha',
        '🟣 FALHA DE SENSOR · MANUTENÇÃO REQUERIDA NO NÓ DE BORDA',
        'Leitura de inclinação indisponível ou vetor inválido no barramento I2C (MPU-6050). Verificar integridade física do equipamento.',
    ),
}


@st.fragment(run_every='2s')
def render_panel():
    if mode == 'Demonstração':
        st.session_state.demo_seq += 1
        state = classify(water, tilt, rain, fault)
        previous = source.snapshot()['latest']
        data = dict(
            schema=1,
            device_id=C.DEVICE_ID,
            boot_id='demonstracao',
            seq=st.session_state.demo_seq,
            ts_utc=None,
            umidade_pct=water,
            inclinacao_deg=None if fault else tilt,
            chuva_60s_mm=rain,
            chuva_total_boot_mm=rain,
            estado=state,
            erro_sensor='Falha simulada no MPU-6050' if fault else None,
            intervalo_envio_ms=interval(state),
        )
        changed = previous is None or previous['estado'] != state
        if changed:
            data['event_id'] = f'demo-{data["seq"]}'
            data['estado_anterior'] = previous['estado'] if previous else None
        source.ingest(json.dumps(data), C.DEVICE_ID, alert=changed)

    snapshot = source.snapshot()
    latest = snapshot['latest']
    history = snapshot['history']

    # Barra superior de status e metadados de conexão
    conn_badge = 'badge-ok' if 'Conectado' in snapshot['status'] or mode == 'Demonstração' else 'badge-warn'
    st.markdown(
        f"""
        <div class="meta-bar">
            <span class="badge {conn_badge}">📡 {snapshot['status']}</span>
            <span class="badge badge-info">⚙️ Modo: {mode}</span>
            <span class="badge badge-info">📦 Leituras em Buffer: {len(history)}</span>
            <span class="badge badge-info">🚨 Eventos Registrados: {len(snapshot['events'])}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if latest is None:
        st.info(
            '⏳ **Aguardando o primeiro pacote de telemetria do ESP32...** '
            'Inicie a simulação no Wokwi e altere o potenciômetro ou o MPU-6050 para forçar uma transmissão imediata.'
        )
        if snapshot['invalid']:
            st.warning(f'Mensagens inválidas descartadas pelo parser: {snapshot["invalid"]}')
        return

    age = snapshot['age']
    stale_limit = max(15, latest['intervalo_envio_ms'] / 1000 * 2 + 5)
    stale = age > stale_limit

    if stale and mode != 'Demonstração':
        st.warning(
            f'⚠️ **Sem novas leituras há {age:.0f} s** — Os indicadores abaixo exibem o último estado conhecido '
            f'(sequência #{latest["seq"]}). Caso o nó esteja em modo NORMAL, o intervalo padrão de envio é de 900 s (15 min).'
        )

    # Banner Principal de Severidade Situacional
    state = latest['estado']
    css_class, title_txt, desc_txt = STATE_UI[state]
    st.markdown(
        f"""
        <div class="status-banner {css_class}">
            <div class="status-title">{title_txt}</div>
            <p class="status-desc">{desc_txt}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if latest.get('erro_sensor'):
        st.error(f'🔧 **Diagnóstico de Borda (ESP32):** `{latest["erro_sensor"]}`')

    # Linha Principal de KPIs com Deltas e Barras de Progresso
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        with st.container(border=True):
            st.metric(
                '💧 Saturação Hídrica (Solo)',
                fmt_metric(latest['umidade_pct'], '%'),
                delta=calc_delta(history, 'umidade_pct', '%'),
                delta_color='inverse',
            )
            val_u = min(1.0, max(0.0, float(latest['umidade_pct']) / 100.0))
            st.progress(val_u, text=f'Limiar Crítico: {C.MOISTURE_ALERT}%')

    with c2:
        with st.container(border=True):
            st.metric(
                '📐 Inclinação da Vertente',
                fmt_metric(latest['inclinacao_deg'], '°'),
                delta=calc_delta(history, 'inclinacao_deg', '°'),
                delta_color='inverse',
            )
            val_i = 0.0 if latest['inclinacao_deg'] is None else min(1.0, max(0.0, float(latest['inclinacao_deg']) / 20.0))
            st.progress(val_i, text=f'Limiar Crítico: {C.TILT_ALERT}°')

    with c3:
        with st.container(border=True):
            st.metric(
                '🌧️ Precipitação (Janela 60 s)',
                fmt_metric(latest['chuva_60s_mm'], 'mm'),
                delta=calc_delta(history, 'chuva_60s_mm', 'mm'),
                delta_color='inverse',
            )
            val_r = min(1.0, max(0.0, float(latest['chuva_60s_mm']) / 10.0))
            st.progress(val_r, text=f'Limiar Crítico: {C.RAIN_ALERT} mm/60s')

    with c4:
        with st.container(border=True):
            envio_s = latest['intervalo_envio_ms'] / 1000
            st.metric(
                '⏱️ Cadência de Envio (Borda)',
                f'{envio_s:g} s',
                delta='Alta Frequência' if envio_s <= 10 else 'Economia (15 min)',
                delta_color='off',
            )
            st.caption(
                f'Acumulado no boot: **{latest.get("chuva_total_boot_mm", 0.0):.1f} mm** · '
                f'Pacote **#{latest["seq"]}**'
            )

    received_dt = datetime.fromisoformat(latest['recebido_em']).astimezone(timezone(timedelta(hours=-3)))
    received_str = received_dt.strftime('%d/%m/%Y às %H:%M:%S')
    st.caption(
        f'🕒 Última sincronização recebida em **{received_str}** (Horário de Fortaleza/Aracaju UTC-3) · '
        f'Há **{age:.0f} s** · Boot ID: `{latest["boot_id"]}`'
    )

    # Abas Analíticas e Operacionais
    frame = pd.DataFrame(history)
    frame['recebido_em'] = pd.to_datetime(frame['recebido_em'], utc=True)

    tab_hist, tab_alerts, tab_matrix, tab_raw = st.tabs([
        '📈 Curvas de Telemetria',
        '🚨 Registro de Alertas (QoS 1)',
        '🧭 Matriz de Severidade & Borda',
        '🔍 Inspecionar Pacote JSON',
    ])

    with tab_hist:
        plot = frame.set_index('recebido_em')
        g1, g2, g3 = st.columns(3)

        with g1:
            with st.container(border=True):
                st.markdown('**💧 Evolução da Umidade do Solo (%)**')
                st.area_chart(plot[['umidade_pct']], color='#0284c7', height=220)
                st.caption(
                    f'Mín: **{frame["umidade_pct"].min():.1f}%** · '
                    f'Méd: **{frame["umidade_pct"].mean():.1f}%** · '
                    f'Máx: **{frame["umidade_pct"].max():.1f}%**'
                )

        with g2:
            with st.container(border=True):
                st.markdown('**📐 Evolução da Inclinação Angular (°)**')
                st.area_chart(plot[['inclinacao_deg']], color='#d97706', height=220)
                valid_tilt = frame['inclinacao_deg'].dropna()
                if not valid_tilt.empty:
                    st.caption(
                        f'Mín: **{valid_tilt.min():.1f}°** · '
                        f'Méd: **{valid_tilt.mean():.1f}°** · '
                        f'Máx: **{valid_tilt.max():.1f}°**'
                    )
                else:
                    st.caption('Sem leituras válidas de inclinação no período.')

        with g3:
            with st.container(border=True):
                st.markdown('**🌧️ Volume Pluviométrico em 60 s (mm)**')
                st.area_chart(plot[['chuva_60s_mm']], color='#0d9488', height=220)
                st.caption(
                    f'Pico na janela móvel: **{frame["chuva_60s_mm"].max():.1f} mm** · '
                    f'Total no boot: **{latest.get("chuva_total_boot_mm", 0.0):.1f} mm**'
                )

        col_dl, col_info = st.columns([1, 3])
        with col_dl:
            st.download_button(
                '📥 Exportar Histórico em CSV',
                frame.to_csv(index=False).encode('utf-8-sig'),
                'sabiaencostas-leituras.csv',
                'text/csv',
                use_container_width=True,
            )
        with col_info:
            st.caption(
                'Memória circular limitada às últimas 1.000 leituras. '
                'A janela móvel de 60 s reflete a intensidade instantânea e não deve ser somada entre linhas.'
            )

    with tab_alerts:
        st.markdown('#### Histórico de Transições de Estado e Eventos Prioritários')
        if snapshot['events']:
            events = pd.DataFrame(reversed(snapshot['events']))
            cols_show = ['recebido_em', 'estado_anterior', 'estado', 'umidade_pct', 'inclinacao_deg', 'chuva_60s_mm', 'event_id']
            existing_cols = [c for c in cols_show if c in events.columns]
            st.dataframe(
                events[existing_cols],
                hide_index=True,
                use_container_width=True,
                column_config={
                    'recebido_em': 'Horário (UTC)',
                    'estado_anterior': 'Estado Anterior',
                    'estado': 'Novo Estado',
                    'umidade_pct': st.column_config.NumberColumn('Umidade (%)', format='%.1f %%'),
                    'inclinacao_deg': st.column_config.NumberColumn('Inclinação (°)', format='%.1f °'),
                    'chuva_60s_mm': st.column_config.NumberColumn('Chuva 60s (mm)', format='%.1f mm'),
                    'event_id': 'ID do Evento (QoS 1)',
                },
            )
        else:
            st.info('Nenhuma transição de alerta registrada nesta sessão até o momento.')

    with tab_matrix:
        m1, m2 = st.columns([3, 2])
        with m1:
            st.markdown('#### Matriz de Severidade e Amostragem Dinâmica (Edge Computing)')
            matrix_df = pd.DataFrame([
                {
                    'Modo Operacional': '🟢 NORMAL',
                    'Gatilho (Basta 1 condição)': 'Umidade < 60%, Inclinação < 5° e Chuva < 2 mm/60s',
                    'Leitura na Borda': 'A cada 2 s',
                    'Envio MQTT': '900 s (15 min)',
                    'Protocolo / Ação': 'QoS 0 · Heartbeat de rotina e economia de bateria',
                },
                {
                    'Modo Operacional': '🟡 ATENCAO',
                    'Gatilho (Basta 1 condição)': f'Umidade ≥ {C.MOISTURE_WARN}%, Inclinação ≥ {C.TILT_WARN}° ou Chuva ≥ {C.RAIN_WARN} mm',
                    'Leitura na Borda': 'A cada 2 s',
                    'Envio MQTT': '10 s',
                    'Protocolo / Ação': 'QoS 1 no evento + Telemetria intensificada',
                },
                {
                    'Modo Operacional': '🔴 ALERTA',
                    'Gatilho (Basta 1 condição)': f'Umidade ≥ {C.MOISTURE_ALERT}%, Inclinação ≥ {C.TILT_ALERT}° ou Chuva ≥ {C.RAIN_ALERT} mm',
                    'Leitura na Borda': 'A cada 2 s',
                    'Envio MQTT': '2 s (Contínuo)',
                    'Protocolo / Ação': 'QoS 1 prioritário + Acionamento da Defesa Civil',
                },
                {
                    'Modo Operacional': '🟣 FALHA_SENSOR',
                    'Gatilho (Basta 1 condição)': 'Erro de comunicação I2C ou vetor nulo no MPU-6050',
                    'Leitura na Borda': 'A cada 2 s',
                    'Envio MQTT': '2 s (Contínuo)',
                    'Protocolo / Ação': 'Alerta imediato de manutenção de hardware',
                },
            ])
            st.dataframe(matrix_df, hide_index=True, use_container_width=True)
        with m2:
            st.markdown('#### Histerese e Resiliência na Borda')
            st.markdown(
                """
                - **Escalada Imediata:** Qualquer leitura que ultrapasse o limiar eleva instantaneamente o nível de risco e força transmissão imediata.
                - **Recuperação Segura (3 Leituras):** Para retornar a um nível inferior de risco, o firmware exige **3 leituras locais consecutivas** abaixo do limiar, evitando oscilações falsas.
                - **Deduplicação em Nuvem:** O buffer filtra pacotes repetidos pela chave `(device_id, boot_id, seq)` e preserva a ordem cronológica.
                """
            )

    with tab_raw:
        r1, r2 = st.columns([2, 1])
        with r1:
            st.markdown('#### Último Payload JSON Recebido')
            st.json(latest)
        with r2:
            st.markdown('#### Diagnóstico do Receptor')
            st.metric('Mensagens Inválidas Ignoradas', snapshot['invalid'])
            st.metric('Alertas Descartados na Fila (ESP32)', latest.get('alertas_descartados', 0))
            st.caption('O receptor valida tipos numéricos, limites de 0 a 100%, device_id e integridade do pacote antes de atualizar o painel.')


render_panel()
st.divider()
st.caption(
    'SabiáEncostas · Centro Universitário Estácio de Sergipe (Aracaju/SE) · '
    'Aplicações de Cloud, IoT e Indústria 4.0 em Python · Histórico em memória volátil.'
)