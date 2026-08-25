"""
app.py
======

Interface visual (Streamlit) do Monitor de Oportunidades Vinted / Wallapop.

Este ficheiro integra os módulos já desenvolvidos e aprovados:
    - config_manager.py  -> CRUD das regras de busca (config.json)
    - scraper_engine.py  -> motor de busca / scraping (Vinted + Wallapop)
    - notifier.py        -> notificações Telegram + geração de propostas
    - dados.py           -> histórico persistente de oportunidades (SQLite)

Funcionalidades principais:
    1. Barra lateral: controlo Iniciar/Parar da monitorização em segundo
       plano, indicador visual de estado, configuração do Telegram e
       intervalo de verificação.
    2. Tab "Oportunidades": feed visual em cartões, com filtros,
       slider de desconto em tempo real e ações rápidas (guardar, descartar,
       copiar, ver anúncio, partilhar).
    3. Tab "Regras de Pesquisa": CRUD completo + import/export + teste
       manual por regra.
    4. Tab "Logs do Sistema": consola em tempo real com o histórico de
       eventos de todos os módulos.

Dependências (ver requirements.txt / instruções no final da conversa):
    streamlit>=1.38
    requests
    streamlit-autorefresh   (opcional, ativa auto-atualização da UI)

Como executar:
    streamlit run app.py

Autor: Desenvolvimento modular — interface principal
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Any, List
from zoneinfo import ZoneInfo

import streamlit as st

import config_manager as cm
import scraper_engine as se
import notifier as nt
import dados
import visual_similarity as vs

# --------------------------------------------------------------------------
# Autorefresh (opcional) — se o pacote não estiver instalado, a app funciona
# na mesma; só perde a atualização automática da UI a cada poucos segundos.
# --------------------------------------------------------------------------
try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_DISPONIVEL = True
except ImportError:
    AUTOREFRESH_DISPONIVEL = False


# ==========================================================================
# CONFIGURAÇÃO GERAL DA PÁGINA
# ==========================================================================
st.set_page_config(
    page_title="Monitor de Oportunidades | Vinted, Wallapop, OLX & Facebook",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded",
)

PLATAFORMAS_LABELS = {
    "vinted": "Vinted",
    "wallapop": "Wallapop",
    "olx": "OLX",
    "facebook": "Facebook Marketplace",
    "ambas": "Todas",
}
PLATAFORMAS_CORES = {
    "vinted": "#09B1BA",
    "wallapop": "#FFB400",
    "olx": "#4A6FA5",
    "facebook": "#1877F2",
    "ambas": "#8B5CF6",
}
PLATAFORMAS_OPCOES = ["ambas", "vinted", "wallapop", "olx", "facebook"]

MODOS_PESQUISA = {
    "imagem": "🖼️ Apenas Imagem (Pesquisa Visual)",
    "texto": "🔤 Apenas Texto (Termos de Pesquisa)",
    "hibrida": "🔤+🖼️ Híbrida (Termos + Imagem)",
}
MODOS_PESQUISA_KEYS = list(MODOS_PESQUISA.keys())


def _injetar_css_personalizado() -> None:
    """Pequenos ajustes visuais para tornar o painel mais limpo e moderno,
    sem depender de bibliotecas extra."""
    st.markdown(
        """
        <style>
        /* Badges de plataforma nos cartões de oportunidades */
        .badge-plataforma {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 600;
            color: white;
        }
        /* Badge de similaridade CLIP */
        .badge-similaridade {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 600;
            color: white;
            margin-left: 4px;
        }
        .badge-similaridade-alta {
            background-color: #10B981; /* verde */
        }
        .badge-similaridade-media {
            background-color: #F59E0B; /* amarelo */
        }
        .badge-similaridade-baixa {
            background-color: #EF4444; /* vermelho */
        }
        /* Espaçamento mais respirável entre cartões */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            margin-bottom: 0.4rem;
        }
        /* Título principal mais compacto no topo */
        h1 { padding-top: 0.5rem; }
        /* Botões de ação rápida com cantos mais arredondados */
        .stButton > button, .stLinkButton > a {
            border-radius: 8px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ==========================================================================
# LOGGING CENTRALIZADO -> BUFFER VISÍVEL NA TAB "LOGS DO SISTEMA"
# ==========================================================================
LOG_BUFFER: deque = deque(maxlen=500)
LOG_LOCK = threading.Lock()
MONITOR_STATE_LOCK = threading.Lock()
MONITOR_STATE = {"intervalo_minutos": 10, "desconto": 0, "ultima_verificacao": None}

logger_app = logging.getLogger("app")


def _obter_estado_monitor(chave: str, padrao: Any = None) -> Any:
    with MONITOR_STATE_LOCK:
        return MONITOR_STATE.get(chave, padrao)


def _definir_estado_monitor(chave: str, valor: Any) -> None:
    with MONITOR_STATE_LOCK:
        MONITOR_STATE[chave] = valor


class _BufferLogHandler(logging.Handler):
    """Handler de logging que guarda cada mensagem num buffer em memória,
    para ser exibido em tempo real na Tab 3 (Logs do Sistema)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            mensagem = self.format(record)
            with LOG_LOCK:
                LOG_BUFFER.appendleft(mensagem)
        except Exception:
            # Um handler de log nunca deve, ele próprio, causar uma exceção.
            pass


def _configurar_logging_global() -> None:
    """
    Liga o handler de buffer aos loggers dos módulos do projeto.

    Protegido para só ser executado uma vez, mesmo que o Streamlit
    reexecute este script muitas vezes (o que acontece a cada interação).
    """
    ja_configurado = getattr(_configurar_logging_global, "_feito", False)
    if ja_configurado:
        return

    handler = _BufferLogHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )

    for nome_logger in ("app", "config_manager", "scraper_engine", "notifier", "dados"):
        logger = logging.getLogger(nome_logger)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # evita duplicar mensagens na consola

    _configurar_logging_global._feito = True


# ==========================================================================
# ESTADO DA SESSÃO (session_state)
# ==========================================================================
def _inicializar_estado() -> None:
    """Garante que todas as chaves de estado necessárias existem."""
    valores_padrao = {
        "monitor_ativo": False,
        "thread_monitor": None,
        "stop_event": None,
        "mostrar_ignoradas": False,
        "mostrar_favoritas": False,
        "ultima_verificacao": None,
        "intervalo_minutos": 10,
        "desconto_padrao_notificacao": 0,
        "resultados_teste_manual": {},  # regra_id -> lista de oportunidades (dict)
        "pagina_atual": 1,
        "pagina_arquivo": 1,
        "itens_por_pagina": 20,
    }
    for chave, valor in valores_padrao.items():
        if chave not in st.session_state:
            st.session_state[chave] = valor
    _definir_estado_monitor("intervalo_minutos", st.session_state["intervalo_minutos"])
    _definir_estado_monitor("desconto", st.session_state["desconto_padrao_notificacao"])

    dados.inicializar_bd()

    # Pré-preencher os campos do Telegram com credenciais já guardadas, se existirem
    if "bot_token_input" not in st.session_state or "chat_id_input" not in st.session_state:
        bot_token, chat_id = nt._obter_credenciais()
        st.session_state["bot_token_input"] = bot_token or ""
        st.session_state["chat_id_input"] = chat_id or ""


def _notificar_telegram_se_recente(
    oportunidade: Dict[str, Any], regra: Dict[str, Any], desconto: float
) -> None:
    """Envia Telegram apenas se o anúncio tiver sido publicado agora."""
    if not se.e_publicacao_recente_telegram(oportunidade):
        idade = se.idade_publicacao_minutos(oportunidade)
        if idade is None:
            logger_app.info(
                f"Telegram omitido para '{oportunidade.get('titulo')}': "
                "sem data de publicação (só se notifica o que acabou de ser postado)."
            )
        else:
            logger_app.info(
                f"Telegram omitido para '{oportunidade.get('titulo')}': "
                f"publicado há {idade:.0f} min (limite {se.TELEGRAM_MAX_MINUTOS_PUBLICACAO} min)."
            )
        return
    nt.notificar_oportunidade(oportunidade, regra, percentagem_desconto=desconto)


# ==========================================================================
# CICLO DE MONITORIZAÇÃO EM SEGUNDO PLANO (THREAD)
# ==========================================================================
def _ciclo_monitorizacao_background(stop_event: threading.Event) -> None:
    """
    Executado numa thread separada enquanto a monitorização estiver ativa.

    A cada iteração:
        1. Corre um ciclo completo de busca (scraper_engine).
        2. Para cada nova oportunidade, envia notificação Telegram.
        3. Atualiza o estado partilhado (lista de oportunidades) para a UI.
        4. Aguarda o intervalo configurado (de forma interrompível).

    Qualquer exceção é registada no log e NUNCA propaga para fora da
    thread, garantindo que a monitorização não morre silenciosamente por
    um erro pontual (ex: falha de rede momentânea).
    """
    logger_app.info("Monitorização em segundo plano iniciada.")

    while not stop_event.is_set():
        try:
            # Usar versão otimizada com estatísticas para monitorização de performance
            novas_oportunidades, stats = se.executar_ciclo_busca_com_stats()
            _definir_estado_monitor("ultima_verificacao", datetime.now().isoformat())

            # Log de performance para monitorização
            logger_app.info(
                f"Ciclo de busca concluído em {stats['tempo_total_segundos']:.2f}s - "
                f"{stats['total_oportunidades']} oportunidades, "
                f"cache: {stats['cache_stats']['tamanho']} entradas"
            )

            if novas_oportunidades:
                logger_app.info(
                    f"{len(novas_oportunidades)} nova(s) oportunidade(s) encontrada(s)."
                )

                # Persiste no dados.db (SQLite trata da concorrência internamente,
                # não precisa de threading.Lock manual como a antiga lista em memória)
                dados.guardar_oportunidades(novas_oportunidades)

                # Envia notificações Telegram para cada nova oportunidade
                regras_por_id = {r["id"]: r for r in cm.listar_regras()}
                desconto = _obter_estado_monitor("desconto", 0)

                for oportunidade in novas_oportunidades:
                    regra = regras_por_id.get(oportunidade["regra_id"], {})
                    _notificar_telegram_se_recente(oportunidade, regra, desconto)
            else:
                logger_app.info("Ciclo de busca concluído — sem novas oportunidades.")

        except Exception as e:
            logger_app.error(f"Erro inesperado no ciclo de monitorização: {e}")

        # Espera pelo intervalo configurado, mas acorda de imediato se
        # o utilizador carregar em "Parar Monitorização".
        intervalo_segundos = _obter_estado_monitor("intervalo_minutos", 10) * 60
        stop_event.wait(timeout=intervalo_segundos)

    logger_app.info("Monitorização em segundo plano terminada.")


def _iniciar_monitorizacao() -> None:
    """Cria e arranca a thread de monitorização em segundo plano."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_ciclo_monitorizacao_background, args=(stop_event,), daemon=True
    )
    thread.start()

    st.session_state["stop_event"] = stop_event
    st.session_state["thread_monitor"] = thread
    st.session_state["monitor_ativo"] = True
    logger_app.info("Pedido de início de monitorização recebido pelo utilizador.")


def _parar_monitorizacao() -> None:
    """Sinaliza a thread de monitorização para terminar."""
    stop_event = st.session_state.get("stop_event")
    if stop_event is not None:
        stop_event.set()
    st.session_state["monitor_ativo"] = False
    logger_app.info("Pedido de paragem de monitorização recebido pelo utilizador.")


def _verificar_agora_manualmente() -> int:
    """
    Corre um ciclo de busca completo IMEDIATAMENTE (sem esperar pelo
    intervalo configurado), notifica no Telegram e atualiza a UI.

    Útil para o utilizador confirmar que tudo está a funcionar, sem ter
    de esperar pelo próximo ciclo automático. Devolve o número de novas
    oportunidades encontradas.
    """
    novas_oportunidades, stats = se.executar_ciclo_busca_com_stats()
    _definir_estado_monitor("ultima_verificacao", datetime.now().isoformat())

    # Log de performance
    logger_app.info(
        f"Verificação manual concluída em {stats['tempo_total_segundos']:.2f}s - "
        f"{stats['total_oportunidades']} oportunidades"
    )

    if novas_oportunidades:
        dados.guardar_oportunidades(novas_oportunidades)

        regras_por_id = {r["id"]: r for r in cm.listar_regras()}
        desconto = st.session_state.get("desconto_padrao_notificacao", 0)
        for oportunidade in novas_oportunidades:
            regra = regras_por_id.get(oportunidade["regra_id"], {})
            _notificar_telegram_se_recente(oportunidade, regra, desconto)

    return len(novas_oportunidades)


# ==========================================================================
# FUNÇÕES AUXILIARES DE UI
# ==========================================================================
def _formatar_data(iso_timestamp: str) -> str:
    """Converte um timestamp ISO para a hora de Portugal continental."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/Lisbon")).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return iso_timestamp or "data desconhecida"


def _formatar_data_olx(iso_timestamp: str) -> str:
    """Mostra a data de publicação do OLX sem a hora."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return iso_timestamp or "data desconhecida"


def _idade_referencia(oportunidade: Dict[str, Any]) -> tuple[float | None, str]:
    """Devolve a idade baseada exclusivamente na data de publicação."""
    minutos = se.idade_publicacao_minutos(oportunidade)
    if minutos is not None and minutos >= 0:
        return minutos, "Carregado"
    return None, "Carregado"


def _formatar_idade_publicacao(oportunidade: Dict[str, Any]) -> str:
    """Mostra há quanto tempo o anúncio foi publicado."""
    texto = oportunidade.get("texto_publicacao")
    if texto:
        return texto[:1].upper() + texto[1:]
    minutos, origem = _idade_referencia(oportunidade)
    if minutos is None:
        return "sem data disponível"
    if minutos < 1:
        return f"{origem} agora"
    if minutos < 60:
        return f"{origem} há {int(minutos)} min"
    horas = minutos / 60
    if horas < 24:
        return f"{origem} há {int(horas)} h"
    dias = horas / 24
    if dias < 30:
        return f"{origem} há {int(dias)} dia(s)"
    meses = dias / 30
    if meses < 12:
        return f"{origem} há {int(meses)} mês(es)"
    return f"{origem} há {int(dias / 365)} ano(s)"


def _passa_filtro_tempo(oportunidade: Dict[str, Any], filtro: str) -> bool:
    """Aplica o filtro de idade de publicação escolhido na interface."""
    minutos, _ = _idade_referencia(oportunidade)
    if filtro == "Qualquer idade":
        return True
    if filtro == "Sem data de publicação":
        return minutos is None
    if minutos is None or minutos < 0:
        return False
    limites = {
        "Última hora": (None, 60),
        "Últimas 24 horas": (None, 24 * 60),
        "Últimos 7 dias": (None, 7 * 24 * 60),
        "Últimos 30 dias": (None, 30 * 24 * 60),
        "Mais de 30 dias": (30 * 24 * 60, None),
        "Mais de 6 meses": (180 * 24 * 60, None),
    }
    minimo, maximo = limites[filtro]
    return (minimo is None or minutos >= minimo) and (maximo is None or minutos <= maximo)


def _botao_copiar(texto: str, key: str) -> None:
    """
    Renderiza um pequeno botão HTML/JS que copia `texto` para a área de
    transferência do sistema, usando a Clipboard API do browser.
    """
    texto_seguro = json.dumps(texto)  # escapa aspas/quebras de linha em segurança
    html_code = f"""
    <div style="margin-top:4px;">
        <button id="btn_{key}" onclick='
            navigator.clipboard.writeText({texto_seguro});
            const btn = document.getElementById("btn_{key}");
            const original = btn.innerText;
            btn.innerText = "Copiado!";
            setTimeout(() => btn.innerText = original, 1500);
        ' style="
            width: 100%;
            padding: 0.45em 1em;
            background-color: #FF4B4B;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 600;
        ">Copiar Proposta</button>
    </div>
    """
    st.html(html_code)


def _testar_regra_manualmente(regra: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Executa uma pesquisa pontual para UMA regra, sem persistir em
    'vistos.json' e sem enviar notificações — apenas para pré-visualização
    imediata na Tab 2 ("Pesquisar Agora").
    """
    resultados = []
    alvos = se._plataformas_da_regra(regra)
    tipo = cm.determinar_tipo_pesquisa(regra)

    # Se for pesquisa exclusivamente por imagem
    if tipo == "imagem" or (not (regra.get("termo_pesquisa") or "").strip() and se._usa_filtro_imagem(regra)):
        if "vinted" in alvos:
            resultados.extend(se.buscar_vinted(regra))
        if "wallapop" in alvos:
            resultados.extend(se.buscar_wallapop(regra))
        if "olx" in alvos:
            resultados.extend(se.buscar_olx(regra))
        if "facebook" in alvos:
            resultados.extend(se.buscar_facebook(regra))
    else:
        # Criar progress bar para múltiplos termos
        termos_pesquisa = [t.strip() for t in (regra.get("termo_pesquisa") or "").split(",") if t.strip()]
        total_termos = len(termos_pesquisa)
        
        if total_termos > 1:
            progress_bar = st.progress(0, text=f"A pesquisar {total_termos} termos...")
            
            # Criar wrappers com progress tracking
            def buscar_vinted_com_progresso(regra):
                for i, termo in enumerate(termos_pesquisa):
                    progresso = (i + 1) / total_termos
                    progress_bar.progress(progresso, text=f"A pesquisar termo {i + 1}/{total_termos}: {termo}")
                    
                    # Copiar a lógica do buscar_vinted mas com termo individual
                    se._aquecer_sessao("vinted", f"https://{se.VINTED_DOMINIO}/")
                    url = f"https://{se.VINTED_DOMINIO}/api/v2/catalog/items"
                    params = {
                        "search_text": termo,
                        "order": "newest_first",
                        "per_page": 20,
                        "price_to": regra["preco_maximo"],
                        "currency": "EUR",
                    }
                    dados = se._pedido_seguro(url, params=params)
                    if dados:
                        for item in dados.get("items", []):
                            try:
                                preco_bruto = item.get("price", {})
                                preco = float(preco_bruto.get("amount", 0))
                                moeda = preco_bruto.get("currency_code", "EUR")
                                
                                data_publicacao = se._extrair_data_publicacao(item)
                                url_anuncio = item.get("url", f"https://{se.VINTED_DOMINIO}/items/{item.get('id')}")
                                if data_publicacao is None:
                                    data_publicacao = se._data_publicacao_da_pagina(url_anuncio)
                                texto_publicacao = se._texto_idade_publicacao(str(item)) or se._texto_publicacao_da_pagina(url_anuncio)
                                
                                oportunidade = se.Oportunidade(
                                    id_artigo=str(item.get("id")),
                                    titulo=item.get("title", "Sem título"),
                                    preco=preco,
                                    moeda=moeda,
                                    url_anuncio=url_anuncio,
                                    url_imagem=(item.get("photo") or {}).get("url", ""),
                                    plataforma="vinted",
                                    regra_id=regra["id"],
                                    regra_nome=regra["nome"],
                                    data_publicacao=data_publicacao,
                                    texto_publicacao=texto_publicacao,
                                )
                                resultados.append(oportunidade)
                            except (KeyError, TypeError, ValueError):
                                continue
                    se._pausa_aleatoria()
            
            def buscar_wallapop_com_progresso(regra):
                for i, termo in enumerate(termos_pesquisa):
                    progresso = (i + 1) / total_termos
                    progress_bar.progress(progresso, text=f"A pesquisar termo {i + 1}/{total_termos}: {termo}")
                    
                    # Copiar a lógica do buscar_wallapop mas com termo individual
                    se._aquecer_sessao("wallapop", "https://www.wallapop.com/")
                    url = "https://api.wallapop.com/api/v3/general/search"
                    params = {
                        "filters_source": "search_box",
                        "keywords": termo,
                        "latitude": se.WALLAPOP_LATITUDE,
                        "longitude": se.WALLAPOP_LONGITUDE,
                        "max_sale_price": regra["preco_maximo"],
                        "order_by": "newest",
                    }
                    headers_wallapop = {
                        "X-AppVersion": "73322",
                        "X-DeviceOS": "2"
                    }
                    dados = se._pedido_seguro(url, params=params, custom_headers=headers_wallapop)
                    if dados:
                        resultados_api = (dados.get("search_objects") or dados.get("data", {}).get("section", {}) or [])
                        if isinstance(resultados_api, dict):
                            resultados_api = resultados_api.get("payload", {}).get("items", [])
                        
                        for item in resultados_api if isinstance(resultados_api, list) else []:
                            try:
                                preco = float(item.get("price", 0))
                                moeda = item.get("currency", "EUR")
                                item_id = str(item.get("id"))
                                titulo = item.get("title", "Sem título")
                                
                                imagens = item.get("images", [])
                                url_imagem = ""
                                if imagens:
                                    url_imagem = imagens[0].get("urls", {}).get("big", "") or imagens[0].get("url", "")
                                
                                url_anuncio = item.get("web_slug")
                                url_anuncio = f"https://es.wallapop.com/item/{url_anuncio}" if url_anuncio else "https://www.wallapop.com"
                                
                                data_publicacao = se._extrair_data_publicacao(item)
                                if data_publicacao is None:
                                    data_publicacao = se._data_publicacao_da_pagina(url_anuncio)
                                texto_publicacao = se._texto_idade_publicacao(str(item)) or se._texto_publicacao_da_pagina(url_anuncio)
                                
                                oportunidade = se.Oportunidade(
                                    id_artigo=item_id,
                                    titulo=titulo,
                                    preco=preco,
                                    moeda=moeda,
                                    url_anuncio=url_anuncio,
                                    url_imagem=url_imagem,
                                    plataforma="wallapop",
                                    regra_id=regra["id"],
                                    regra_nome=regra["nome"],
                                    data_publicacao=data_publicacao,
                                    texto_publicacao=texto_publicacao,
                                )
                                resultados.append(oportunidade)
                            except (KeyError, TypeError, ValueError):
                                continue
                    se._pausa_aleatoria()

            def buscar_olx_com_progresso(regra):
                for i, termo in enumerate(termos_pesquisa):
                    progresso = (i + 1) / total_termos
                    progress_bar.progress(progresso, text=f"A pesquisar termo {i + 1}/{total_termos}: {termo}")
                    regra_termo = dict(regra, termo_pesquisa=termo)
                    resultados.extend(se.buscar_olx(regra_termo))
                    se._pausa_aleatoria()

            def buscar_facebook_com_progresso(regra):
                for i, termo in enumerate(termos_pesquisa):
                    progresso = (i + 1) / total_termos
                    progress_bar.progress(progresso, text=f"A pesquisar termo {i + 1}/{total_termos}: {termo}")
                    regra_termo = dict(regra, termo_pesquisa=termo)
                    resultados.extend(se.buscar_facebook(regra_termo))
                    se._pausa_aleatoria()
            
            if "vinted" in alvos:
                buscar_vinted_com_progresso(regra)
            if "wallapop" in alvos:
                buscar_wallapop_com_progresso(regra)
            if "olx" in alvos:
                buscar_olx_com_progresso(regra)
            if "facebook" in alvos:
                buscar_facebook_com_progresso(regra)
            
            progress_bar.progress(1.0, text="Pesquisa concluída!")
        else:
            # Se só há um termo ou nenhum, usar a função normal
            if "vinted" in alvos:
                resultados.extend(se.buscar_vinted(regra))
            if "wallapop" in alvos:
                resultados.extend(se.buscar_wallapop(regra))
            if "olx" in alvos:
                resultados.extend(se.buscar_olx(regra))
            if "facebook" in alvos:
                resultados.extend(se.buscar_facebook(regra))

    # Reaproveita a lógica de filtragem já validada no scraper_engine
    filtrados = [o for o in resultados if se._passa_filtros(o, regra)]
    return [o.to_dict() for o in filtrados]


# ==========================================================================
# BARRA LATERAL
# ==========================================================================
def _render_sidebar() -> None:
    st.sidebar.title("🛍️ Monitor de Oportunidades")
    st.sidebar.caption("Vinted, Wallapop, OLX & Facebook Marketplace — monitorização automática")
    st.sidebar.divider()

    # --- Controlo do Bot -------------------------------------------------
    st.sidebar.subheader("🤖 Controlo da Monitorização")

    if st.session_state["monitor_ativo"]:
        st.sidebar.success("🟢 Ativo (a monitorizar...)")
        if st.sidebar.button("⏹️ Parar Monitorização", width='stretch'):
            _parar_monitorizacao()
            st.rerun()
    else:
        st.sidebar.error("🔴 Parado")
        if st.sidebar.button("▶️ Iniciar Monitorização", width='stretch', type="primary"):
            if not cm.listar_regras(apenas_ativas=True):
                st.sidebar.warning(
                    "Não há nenhuma regra ativa. Cria ou ativa pelo menos uma "
                    "regra na tab 'Regras de Pesquisa' antes de iniciar."
                )
            else:
                _iniciar_monitorizacao()
                st.rerun()

    if st.sidebar.button("🔍 Verificar Agora", width='stretch', help="Corre uma pesquisa imediata, sem esperar pelo intervalo configurado."):
        if not cm.listar_regras(apenas_ativas=True):
            st.sidebar.warning("Não há nenhuma regra ativa para pesquisar.")
        else:
            with st.spinner("A pesquisar em todas as regras ativas..."):
                total_novas = _verificar_agora_manualmente()
            if total_novas:
                st.sidebar.success(f"{total_novas} nova(s) oportunidade(s) encontrada(s)!")
            else:
                st.sidebar.info("Verificação concluída — sem novidades desta vez.")
            st.rerun()

    if st.session_state.pop("reset_confirmar_limpeza", False):
        st.session_state["confirmar_limpeza_oportunidades"] = False

    confirmar_limpeza = st.sidebar.checkbox(
        "Confirmo apagar todas as oportunidades",
        key="confirmar_limpeza_oportunidades",
    )
    if st.sidebar.button(
        "🧹 Limpar oportunidades e recomeçar",
        width='stretch',
        disabled=not confirmar_limpeza,
        help="Apaga o histórico e permite que os resultados atuais sejam encontrados novamente.",
    ):
        if st.session_state.get("monitor_ativo"):
            st.sidebar.error("Para a monitorização antes de limpar as oportunidades.")
        else:
            removidas = dados.eliminar_todas()
            se._guardar_vistos(set())
            _definir_estado_monitor("ultima_verificacao", None)
            st.session_state["reset_confirmar_limpeza"] = True
            st.sidebar.success(f"{removidas} oportunidade(s) removida(s). Pode pesquisar novamente.")
            st.rerun()

    ultima = _obter_estado_monitor("ultima_verificacao")
    if ultima:
        st.sidebar.caption(f"🕓 Última verificação: {_formatar_data(ultima)}")
    else:
        st.sidebar.caption("🕓 Ainda sem nenhuma verificação feita.")

    st.session_state["intervalo_minutos"] = st.sidebar.slider(
        "⏱️ Intervalo de verificação (minutos)",
        min_value=1,
        max_value=60,
        value=st.session_state["intervalo_minutos"],
        step=1,
        help="De quanto em quanto tempo o sistema volta a pesquisar novas oportunidades.",
    )
    _definir_estado_monitor("intervalo_minutos", st.session_state["intervalo_minutos"])

    st.session_state["itens_por_pagina"] = st.sidebar.slider(
        "📄 Itens por página",
        min_value=5,
        max_value=50,
        value=st.session_state["itens_por_pagina"],
        step=5,
        help="Número de oportunidades a mostrar por página na tab Oportunidades.",
    )

    st.session_state["desconto_padrao_notificacao"] = st.sidebar.slider(
        "💸 Desconto sugerido nas notificações automáticas (%)",
        min_value=0,
        max_value=50,
        value=st.session_state["desconto_padrao_notificacao"],
        step=5,
        help="Percentagem de desconto aplicada por defeito na proposta enviada "
        "junto com cada notificação automática do Telegram.",
    )
    _definir_estado_monitor("desconto", st.session_state["desconto_padrao_notificacao"])

    st.sidebar.divider()

    # --- Informação CLIP -------------------------------------------------
    st.sidebar.subheader("🤖 Similaridade Visual CLIP")
    try:
        clip_info = vs.get_clip_info()
        if clip_info["loaded"]:
            device_icon = "🚀" if clip_info["device"] == "cuda" else "💻"
            device_text = f"{device_icon} {clip_info['device'].upper()}"
            st.sidebar.success(f"Modelo CLIP {clip_info['model_name']} carregado")
            st.sidebar.caption(f"Dispositivo: {device_text}")
            st.sidebar.caption(f"Cache: {clip_info['cache_size']}/{clip_info['cache_max_size']} embeddings")
        else:
            st.sidebar.warning("Modelo CLIP não carregado")
            st.sidebar.caption("O filtro visual usará fallback")
    except Exception as e:
        st.sidebar.error(f"Erro ao verificar CLIP: {e}")

    st.sidebar.divider()

    # --- Configurações do Telegram ---------------------------------------
    st.sidebar.subheader("📲 Configuração do Telegram")
    st.sidebar.caption(
        f"Só recebes alerta do que acabou de ser postado "
        f"(últimos {se.TELEGRAM_MAX_MINUTOS_PUBLICACAO} minutos). "
        "O resto aparece só na app."
    )
    st.session_state["bot_token_input"] = st.sidebar.text_input(
        "Bot Token", value=st.session_state["bot_token_input"], type="password"
    )
    st.session_state["chat_id_input"] = st.sidebar.text_input(
        "Chat ID", value=st.session_state["chat_id_input"]
    )

    if st.sidebar.button("🔗 Testar e Guardar Ligação", width='stretch'):
        token = st.session_state["bot_token_input"].strip()
        chat_id = st.session_state["chat_id_input"].strip()

        if not token or not chat_id:
            st.sidebar.error("Preenche o Bot Token e o Chat ID antes de testar.")
        else:
            with st.spinner("A testar ligação ao Telegram..."):
                resultado = nt.testar_credenciais_telegram(bot_token=token, chat_id=chat_id)

            if resultado["sucesso"]:
                nt.guardar_credenciais(token, chat_id)
                st.sidebar.success(resultado["mensagem"])
            else:
                st.sidebar.error(resultado["mensagem"])

    with st.sidebar.expander("❓ Não tens um bot? Cria um em 2 minutos"):
        st.markdown(
            """
            1. Abre o Telegram e fala com **[@BotFather](https://t.me/BotFather)**
            2. Envia o comando `/newbot` e segue as instruções
            3. Copia o **Bot Token** que ele te dá e cola-o acima
            4. Fala com **[@userinfobot](https://t.me/userinfobot)** para obteres
               o teu **Chat ID** numérico
            5. Cola o Chat ID acima e clica em "Testar e Guardar Ligação"
            """
        )

    if not AUTOREFRESH_DISPONIVEL:
        st.sidebar.caption(
            "ℹ️ Instala `streamlit-autorefresh` para a interface se atualizar "
            "sozinha (`pip install streamlit-autorefresh`)."
        )


# ==========================================================================
# TAB 1 — OPORTUNIDADES ENCONTRADAS
# ==========================================================================
def _render_tab_oportunidades(estado: str = "feed") -> None:
    """Mostra o feed ou um dos dois arquivos de oportunidades."""
    titulos = {
        "feed": "Ainda não há oportunidades novas.",
        "guardadas": "Ainda não guardaste nenhuma oportunidade.",
        "descartadas": "Ainda não descartaste nenhuma oportunidade.",
    }
    dados.limpar_imagens_placeholder()
    todas_oportunidades = dados.listar_oportunidades()
    # Atualiza em pequenos lotes para não bloquear a interface nem sobrecarregar
    # as plataformas quando existem muitos anúncios antigos sem data.
    for oportunidade in todas_oportunidades[:50]:
        if oportunidade.get("data_publicacao"):
            continue
        url_anuncio = oportunidade.get("url_anuncio", "")
        if oportunidade.get("plataforma") == "olx":
            data_publicacao = se._data_publicacao_olx_da_pagina(url_anuncio)
        else:
            data_publicacao = None
        if data_publicacao:
            dados.atualizar_publicacao(
                oportunidade["plataforma"],
                oportunidade["id_artigo"],
                data_publicacao,
                None if oportunidade.get("plataforma") == "olx" else se._texto_publicacao_da_pagina(url_anuncio),
            )
    todas_oportunidades = dados.listar_oportunidades()
    oportunidades_estado = dados.listar_oportunidades(estado=estado)
    if not oportunidades_estado:
        st.info(f"📭 {titulos[estado]}")
        return

    if estado == "descartadas":
        st.warning("A eliminação definitiva remove estes anúncios da base de dados e não pode ser desfeita.")
        confirmar = st.checkbox(
            "Confirmo que quero eliminar definitivamente todas as oportunidades descartadas.",
            key="confirmar_eliminar_descartadas",
        )
        if st.button(
            "🗑️ Eliminar definitivamente todas as descartadas",
            disabled=not confirmar,
            type="primary",
            width="stretch",
        ):
            removidas = dados.eliminar_descartadas()
            st.success(f"{removidas} oportunidade(s) eliminada(s) definitivamente.")
            st.rerun()

    chave_pagina = f"pagina_{estado}"
    st.session_state.setdefault(chave_pagina, 1)
    col_filtro1, col_filtro2, col_filtro3 = st.columns(3)
    plataformas_disponiveis = ["Todas"] + sorted({o["plataforma"] for o in todas_oportunidades})
    regras_disponiveis = ["Todas"] + sorted({o["regra_nome"] for o in todas_oportunidades})
    filtro_plataforma = col_filtro1.selectbox(
        "Filtrar por plataforma", plataformas_disponiveis, key=f"plataforma_{estado}"
    )
    filtro_regra = col_filtro2.selectbox(
        "Filtrar por regra", regras_disponiveis, key=f"regra_{estado}"
    )
    filtro_tempo = col_filtro3.selectbox(
        "Publicado", [
            "Qualquer idade", "Última hora", "Últimas 24 horas", "Últimos 7 dias",
            "Últimos 30 dias", "Mais de 30 dias", "Mais de 6 meses", "Sem data de publicação",
        ], key=f"tempo_{estado}",
    )
    oportunidades_filtradas = dados.listar_oportunidades(
        plataforma=filtro_plataforma, regra_nome=filtro_regra, estado=estado
    )
    oportunidades_filtradas = [
        oportunidade for oportunidade in oportunidades_filtradas
        if _passa_filtro_tempo(oportunidade, filtro_tempo)
    ]
    st.caption(f"A mostrar {len(oportunidades_filtradas)} oportunidade(s).")

    # Paginação
    itens_por_pagina = st.session_state["itens_por_pagina"]
    total_paginas = max(1, (len(oportunidades_filtradas) + itens_por_pagina - 1) // itens_por_pagina)
    
    # Ajustar página atual se necessário
    if st.session_state[chave_pagina] > total_paginas:
        st.session_state[chave_pagina] = total_paginas
    
    inicio = (st.session_state[chave_pagina] - 1) * itens_por_pagina
    fim = inicio + itens_por_pagina
    oportunidades_pagina = oportunidades_filtradas[inicio:fim]

    # Controles de paginação
    col_pag_prev, col_pag_info, col_pag_next = st.columns([1, 2, 1])
    with col_pag_prev:
        if st.button("← Anterior", key=f"anterior_{estado}", disabled=st.session_state[chave_pagina] == 1, width='stretch'):
            st.session_state[chave_pagina] -= 1
            st.rerun()
    with col_pag_info:
        st.write(f"Página {st.session_state[chave_pagina]} de {total_paginas}")
    with col_pag_next:
        if st.button("Próximo →", key=f"proximo_{estado}", disabled=st.session_state[chave_pagina] == total_paginas, width='stretch'):
            st.session_state[chave_pagina] += 1
            st.rerun()

    regras_por_id = {r["id"]: r for r in cm.listar_regras()}

    # --- Cartões ------------------------------------------------------------
    for oportunidade in oportunidades_pagina:
        regra = regras_por_id.get(oportunidade["regra_id"], {})
        chave_base = f"{oportunidade['plataforma']}_{oportunidade['id_artigo']}"
        esta_ignorada = bool(oportunidade.get("ignorada"))

        with st.container(border=True):
            col_img, col_info = st.columns([1, 3])

            with col_img:
                if oportunidade.get("url_imagem"):
                    st.image(oportunidade["url_imagem"], width='stretch')
                else:
                    st.markdown(
                        "<div style='text-align:center; padding-top:30px;'>📷<br>Sem imagem</div>",
                        unsafe_allow_html=True,
                    )

            with col_info:
                col_titulo, col_feed, col_guardar, col_descartar = st.columns([5, 1, 1, 1])
                with col_titulo:
                    titulo_exibido = f"~~{oportunidade['titulo']}~~" if estado == "descartadas" else f"**{oportunidade['titulo']}**"
                    st.markdown(titulo_exibido)
                with col_feed:
                    if estado != "feed" and st.button("↩️", key=f"feed_{chave_base}", help="Mover para oportunidades"):
                        dados.definir_estado(oportunidade["plataforma"], oportunidade["id_artigo"], "feed")
                        st.rerun()
                with col_guardar:
                    if estado != "guardadas" and st.button("⭐", key=f"guardar_{chave_base}", help="Guardar oportunidade"):
                        dados.definir_estado(oportunidade["plataforma"], oportunidade["id_artigo"], "guardadas")
                        st.rerun()
                with col_descartar:
                    if estado != "descartadas" and st.button("🗑️", key=f"descartar_{chave_base}", help="Descartar oportunidade"):
                        dados.definir_estado(oportunidade["plataforma"], oportunidade["id_artigo"], "descartadas")
                        st.rerun()

                cor_plataforma = PLATAFORMAS_CORES.get(oportunidade["plataforma"], "#888")
                badge = (
                    f"<span class='badge-plataforma' style='background-color:{cor_plataforma}'>"
                    f"{PLATAFORMAS_LABELS.get(oportunidade['plataforma'], oportunidade['plataforma'])}</span>"
                )
                
                # Badge de similaridade CLIP se disponível
                badge_similaridade = ""
                if oportunidade.get("score_similaridade") is not None:
                    score = oportunidade["score_similaridade"]
                    if score >= 75:
                        classe_css = "badge-similaridade-alta"
                    elif score >= 60:
                        classe_css = "badge-similaridade-media"
                    else:
                        classe_css = "badge-similaridade-baixa"
                    badge_similaridade = (
                        f"<span class='badge-similaridade {classe_css}'>"
                        f"CLIP {score:.1f}%</span>"
                    )
                
                st.markdown(
                    f"💰 **{oportunidade['preco']:.2f} {oportunidade['moeda']}**  &nbsp; {badge} {badge_similaridade}",
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"📌 Regra: {oportunidade['regra_nome']}  •  "
                    f"🕓 {('Publicado em ' + _formatar_data_olx(oportunidade['data_publicacao'])) if oportunidade.get('plataforma') == 'olx' and oportunidade.get('data_publicacao') else _formatar_idade_publicacao(oportunidade)}  •  "
                    f"Encontrado em {_formatar_data(oportunidade['data_descoberta'])}"
                )

                if estado == "descartadas":
                    st.caption("🗑️ Oportunidade descartada.")
                    continue

                desconto = st.slider(
                    "Desconto a propor (%)",
                    min_value=0,
                    max_value=50,
                    value=0,
                    step=5,
                    key=f"desconto_{chave_base}",
                )

                texto_proposta = nt.gerar_texto_proposta(oportunidade, regra, desconto)
                st.text_area(
                    "Proposta sugerida",
                    value=texto_proposta,
                    height=90,
                    # A key inclui o desconto de propósito: assim, sempre que o slider
                    # muda, o Streamlit trata isto como um campo "novo" e atualiza o
                    # texto mostrado, em vez de manter o valor antigo em cache.
                    key=f"texto_{chave_base}_{desconto}",
                )

                col_a, col_b = st.columns(2)
                with col_a:
                    st.link_button("🔗 Ver anúncio", oportunidade["url_anuncio"], width='stretch')
                with col_b:
                    _botao_copiar(texto_proposta, key=chave_base)

        st.write("")  # pequeno espaçamento entre cartões


# ==========================================================================
# TAB 2 — GESTÃO DE REGRAS DE PESQUISA (CRUD)
# ==========================================================================
def _render_tab_regras() -> None:
    # --- Exportar / Importar regras (útil para partilhar configuração) ---
    col_exp, col_imp = st.columns(2)
    with col_exp:
        regras_atuais = cm.listar_regras()
        conteudo_export = json.dumps({"regras": regras_atuais}, ensure_ascii=False, indent=2)
        st.download_button(
            "⬇️ Exportar regras (para partilhar)",
            data=conteudo_export,
            file_name="regras_monitor_oportunidades.json",
            mime="application/json",
            width='stretch',
            help="Descarrega um ficheiro com todas as tuas regras, para enviares a outra pessoa.",
        )
    with col_imp:
        ficheiro_importado = st.file_uploader(
            "⬆️ Importar regras de um ficheiro",
            type=["json"],
            help="Recebeste um ficheiro de regras de outra pessoa? Carrega-o aqui.",
        )
        if ficheiro_importado is not None:
            try:
                dados = json.loads(ficheiro_importado.read().decode("utf-8"))
                regras_importadas = dados.get("regras", [])
                adicionadas = 0
                for r in regras_importadas:
                    cm.criar_regra(
                        nome=r.get("nome", "Regra importada"),
                        tipo_pesquisa=r.get("tipo_pesquisa", "auto"),
                        termo_pesquisa=r.get("termo_pesquisa", ""),
                        preco_maximo=r.get("preco_maximo", 0),
                        preco_minimo=r.get("preco_minimo", 0),
                        palavras_excluidas=r.get("palavras_excluidas", []),
                        mensagem_proposta=r.get("mensagem_proposta", ""),
                        ativo=r.get("ativo", True),
                        plataforma=r.get("plataforma", "ambas"),
                        imagens_referencia=r.get("imagens_referencia", r.get("imagem_referencia", "")),
                        similaridade_minima=r.get("similaridade_minima", cm.SIMILARIDADE_PADRAO),
                    )
                    adicionadas += 1
                st.success(f"{adicionadas} regra(s) importada(s) com sucesso!")
                st.rerun()
            except (json.JSONDecodeError, cm.RegraInvalidaError, AttributeError) as e:
                st.error(f"Não foi possível importar o ficheiro: {e}")

    st.divider()
    st.subheader("➕ Criar nova regra")
    st.caption(
        "Podes pesquisar **apenas e só por imagem** (sem obrigação de termos), apenas por termos de texto, ou combinando ambos."
    )

    modo_novo = st.radio(
        "Modo de Pesquisa*",
        options=MODOS_PESQUISA_KEYS,
        format_func=lambda k: MODOS_PESQUISA[k],
        horizontal=True,
        key="novo_modo_pesquisa",
    )

    with st.form("form_nova_regra", clear_on_submit=True):
        if modo_novo == "imagem":
            st.info(
                "📸 **Pesquisa exclusivamente por Imagem**: A pesquisa será feita **apenas e só pela imagem** de referência "
                "(sem necessidade de termos de texto). O filtro visual analisa diretamente as fotos dos novos anúncios nas plataformas."
            )

        col1, col2 = st.columns(2)
        with col1:
            nome = st.text_input("Nome da regra*", placeholder="ex: Casaco Vintage / Guitarra Fender")
            if modo_novo != "imagem":
                termo_pesquisa = st.text_input(
                    "Termo(s) de pesquisa*",
                    placeholder="ex: fender stratocaster, squier (separados por vírgula)",
                )
            else:
                termo_pesquisa = ""
            preco_minimo = st.number_input("Preço mínimo (€)", min_value=0.0, step=5.0, value=0.0)
            preco_maximo = st.number_input("Preço máximo (€)*", min_value=0.0, step=5.0, value=100.0)
        with col2:
            plataforma = st.selectbox("Plataforma", PLATAFORMAS_OPCOES, format_func=lambda p: PLATAFORMAS_LABELS[p])
            palavras_excluidas_txt = st.text_input(
                "Palavras a excluir (separadas por vírgula)", placeholder="capa, avariado, defeito"
            )
            ativo = st.checkbox("Regra ativa", value=True)

        if modo_novo in ("imagem", "hibrida"):
            col_img1, col_img2 = st.columns([2, 1])
            with col_img1:
                rotulo_foto = "Imagens de referência (Obrigatório)*" if modo_novo == "imagem" else "Imagens de referência"
                fotos_novas = st.file_uploader(
                    rotulo_foto,
                    type=["jpg", "jpeg", "png", "webp"],
                    accept_multiple_files=True,
                    help="Carrega uma ou mais fotos do artigo. A pesquisa por imagem compara diretamente as fotos dos anúncios com estas fotos.",
                )
            with col_img2:
                similaridade_nova = st.slider(
                    "Semelhança mínima CLIP (%)",
                    min_value=0,
                    max_value=100,
                    value=50,
                    help="Valores mais altos = apenas anúncios extremamente parecidos com a foto (CLIP). Mais baixos = mais resultados. Recomendado: 60-80% para maioria dos produtos.",
                )
        else:
            fotos_novas = []
            similaridade_nova = 50

        mensagem_proposta = st.text_area(
            "Mensagem de proposta padrão",
            placeholder="Olá! Tenho interesse no artigo \"{titulo}\". Aceita {preco}€?",
            help="Podes usar {titulo}, {preco}, {preco_original} e {desconto} como variáveis.",
        )

        submeter = st.form_submit_button("✅ Criar Regra", type="primary")

        if submeter:
            try:
                if modo_novo == "imagem" and not fotos_novas:
                    st.error("Para pesquisa por imagem, deves carregar pelo menos uma imagem de referência.")
                elif modo_novo == "texto" and not termo_pesquisa.strip():
                    st.error("Para pesquisa por texto, deves indicar pelo menos um termo de pesquisa.")
                elif modo_novo == "hibrida" and (not termo_pesquisa.strip() or not fotos_novas):
                    st.error("Para pesquisa híbrida, deves indicar termos de pesquisa e carregar pelo menos uma imagem.")
                else:
                    palavras_excluidas = [
                        p.strip() for p in palavras_excluidas_txt.split(",") if p.strip()
                    ]
                    nova = cm.criar_regra(
                        nome=nome,
                        tipo_pesquisa=modo_novo,
                        termo_pesquisa="" if modo_novo == "imagem" else termo_pesquisa,
                        preco_maximo=preco_maximo,
                        preco_minimo=preco_minimo,
                        palavras_excluidas=palavras_excluidas,
                        mensagem_proposta=mensagem_proposta,
                        ativo=ativo,
                        plataforma=plataforma,
                        similaridade_minima=similaridade_nova,
                        imagens_referencia=["pending"] if fotos_novas else [],
                    )
                    if fotos_novas:
                        caminhos = [
                            cm.guardar_imagem_regra(nova["id"], foto.getvalue(), foto.name)
                            for foto in fotos_novas
                        ]
                        cm.atualizar_regra(nova["id"], imagens_referencia=caminhos)
                    st.success(f"Regra '{nome}' criada com sucesso!")
                    st.rerun()
            except (cm.RegraInvalidaError, cm.ConfigManagerError) as e:
                st.error(f"Não foi possível criar a regra: {e}")

    st.divider()
    st.subheader("📋 Regras existentes")

    regras = cm.listar_regras()
    if not regras:
        st.info("Ainda não criaste nenhuma regra de pesquisa.")
        return

    for regra in regras:
        icone_estado = "🟢" if regra["ativo"] else "🔴"
        tipo = cm.determinar_tipo_pesquisa(regra)
        imagens_atuais = [c for c in regra.get("imagens_referencia", []) if os.path.isfile(c)]
        icone_foto = f" 📷×{len(imagens_atuais)}" if imagens_atuais else ""
        
        if tipo == "imagem":
            tipo_tag = "🖼️ Apenas Imagem"
        elif tipo == "hibrida":
            tipo_tag = f"🔤+🖼️ {regra.get('termo_pesquisa', '')}"
        else:
            tipo_tag = f"🔤 {regra.get('termo_pesquisa', '')}"

        titulo_expander = (
            f"{icone_estado} {regra['nome']} — {tipo_tag} — "
            f"{PLATAFORMAS_LABELS.get(regra['plataforma'], regra['plataforma'])} — "
            f"até {regra['preco_maximo']:.2f}€{icone_foto}"
        )

        with st.expander(titulo_expander):
            if imagens_atuais:
                st.write("**Imagens de referência ativas:**")
                st.image(imagens_atuais, caption=[f"Foto {i+1}" for i in range(len(imagens_atuais))], width=160)

            with st.form(f"form_editar_{regra['id']}"):
                modo_edicao = st.radio(
                    "Modo de Pesquisa",
                    options=MODOS_PESQUISA_KEYS,
                    index=MODOS_PESQUISA_KEYS.index(tipo) if tipo in MODOS_PESQUISA_KEYS else 0,
                    format_func=lambda k: MODOS_PESQUISA[k],
                    horizontal=True,
                    key=f"modo_{regra['id']}",
                )
                if modo_edicao == "imagem":
                    st.info("📸 **Modo Apenas Imagem**: A pesquisa corre apenas pela semelhança visual das fotos. Não são necessários termos de pesquisa.")

                col1, col2 = st.columns(2)
                with col1:
                    novo_nome = st.text_input("Nome", value=regra["nome"], key=f"nome_{regra['id']}")
                    if modo_edicao != "imagem":
                        novo_termo = st.text_input(
                            "Termo de pesquisa",
                            value=regra.get("termo_pesquisa", ""),
                            key=f"termo_{regra['id']}",
                        )
                    else:
                        novo_termo = ""
                    novo_preco = st.number_input(
                        "Preço máximo (€)",
                        min_value=0.0,
                        step=5.0,
                        value=float(regra["preco_maximo"]),
                        key=f"preco_{regra['id']}",
                    )
                    novo_preco_minimo = st.number_input(
                        "Preço mínimo (€)",
                        min_value=0.0,
                        max_value=float(novo_preco),
                        step=5.0,
                        value=min(float(regra.get("preco_minimo", 0) or 0), float(novo_preco)),
                        key=f"preco_minimo_{regra['id']}",
                    )
                with col2:
                    plataformas_lista = PLATAFORMAS_OPCOES
                    nova_plataforma = st.selectbox(
                        "Plataforma",
                        plataformas_lista,
                        index=plataformas_lista.index(regra["plataforma"]) if regra.get("plataforma") in plataformas_lista else 0,
                        format_func=lambda p: PLATAFORMAS_LABELS[p],
                        key=f"plataforma_{regra['id']}",
                    )
                    novas_palavras_txt = st.text_input(
                        "Palavras a excluir",
                        value=", ".join(regra.get("palavras_excluidas", [])),
                        key=f"palavras_{regra['id']}",
                    )
                    novo_ativo = st.checkbox("Regra ativa", value=regra["ativo"], key=f"ativo_{regra['id']}")

                if modo_edicao in ("imagem", "hibrida"):
                    col_foto1, col_foto2 = st.columns([2, 1])
                    with col_foto1:
                        novas_fotos = st.file_uploader(
                            "Adicionar novas imagens de referência",
                            type=["jpg", "jpeg", "png", "webp"],
                            accept_multiple_files=True,
                            key=f"foto_{regra['id']}",
                        )
                        remover_fotos = st.checkbox(
                            "Remover imagens de referência anteriores",
                            value=False,
                            key=f"remover_foto_{regra['id']}",
                        )
                    with col_foto2:
                        nova_similaridade = st.slider(
                            "Semelhança mínima CLIP (%)",
                            min_value=0,
                            max_value=100,
                            value=int(regra.get("similaridade_minima", 50) or 50),
                            key=f"sim_{regra['id']}",
                            help="Valores mais altos = apenas anúncios extremamente parecidos com a foto (CLIP). Mais baixos = mais resultados. Recomendado: 60-80% para maioria dos produtos.",
                        )
                else:
                    novas_fotos = []
                    remover_fotos = True
                    nova_similaridade = 50

                nova_mensagem = st.text_area(
                    "Mensagem de proposta",
                    value=regra.get("mensagem_proposta", ""),
                    key=f"mensagem_{regra['id']}",
                )

                col_guardar, col_eliminar, col_testar = st.columns(3)
                guardar = col_guardar.form_submit_button("💾 Guardar Alterações", width='stretch')
                eliminar = col_eliminar.form_submit_button("🗑️ Eliminar Regra", width='stretch')
                testar = col_testar.form_submit_button("🔍 Pesquisar Agora (Teste Manual)", width='stretch')

                if guardar:
                    try:
                        imagens_finais = [] if remover_fotos else list(regra.get("imagens_referencia", []))
                        if novas_fotos:
                            imagens_finais.extend(
                                cm.guardar_imagem_regra(regra["id"], foto.getvalue(), foto.name)
                                for foto in novas_fotos
                            )
                        if modo_edicao == "imagem" and not imagens_finais:
                            st.error("Para regras de pesquisa apenas por imagem, deves manter ou carregar pelo menos uma foto de referência.")
                        elif modo_edicao == "texto" and not (novo_termo or "").strip():
                            st.error("Para regras de pesquisa por texto, o termo de pesquisa é obrigatório.")
                        elif modo_edicao == "hibrida" and (not (novo_termo or "").strip() or not imagens_finais):
                            st.error("Para regras híbridas, deves indicar termos de pesquisa e ter pelo menos uma imagem.")
                        else:
                            cm.atualizar_regra(
                                regra["id"],
                                nome=novo_nome,
                                tipo_pesquisa=modo_edicao,
                                termo_pesquisa="" if modo_edicao == "imagem" else novo_termo,
                                preco_maximo=novo_preco,
                                preco_minimo=novo_preco_minimo,
                                palavras_excluidas=[p.strip() for p in novas_palavras_txt.split(",") if p.strip()],
                                mensagem_proposta=nova_mensagem,
                                ativo=novo_ativo,
                                plataforma=nova_plataforma,
                                imagens_referencia=imagens_finais,
                                similaridade_minima=nova_similaridade,
                            )
                            if remover_fotos and not novas_fotos and modo_edicao != "imagem":
                                cm.remover_imagens_regra(regra["id"], regra.get("imagens_referencia", []))
                            st.success("Regra atualizada com sucesso!")
                            st.rerun()
                    except (cm.RegraInvalidaError, cm.RegraNaoEncontradaError, cm.ConfigManagerError) as e:
                        st.error(f"Não foi possível guardar as alterações: {e}")

                if eliminar:
                    cm.eliminar_regra(regra["id"])
                    st.success(f"Regra '{regra['nome']}' eliminada.")
                    st.rerun()

                if testar:
                    with st.spinner(f"A pesquisar '{regra['nome']}' em tempo real..."):
                        resultados = _testar_regra_manualmente(regra)
                    st.session_state["resultados_teste_manual"][regra["id"]] = resultados

            # Mostrar resultados do último teste manual, se existirem
            resultados_teste = st.session_state["resultados_teste_manual"].get(regra["id"])
            if resultados_teste is not None:
                if resultados_teste:
                    st.success(f"{len(resultados_teste)} resultado(s) encontrado(s) neste teste:")
                    for r in resultados_teste[:10]:
                        st.write(
                            f"- [{PLATAFORMAS_LABELS.get(r['plataforma'], r['plataforma'])}] "
                            f"**{r['titulo']}** — {r['preco']:.2f} {r['moeda']} "
                            f"([ver anúncio]({r['url_anuncio']}))"
                        )
                else:
                    st.warning(
                        "Nenhum resultado encontrado neste teste. Isto pode significar que não "
                        "há anúncios correspondentes agora, ou que a plataforma bloqueou o pedido "
                        "(consulta a tab 'Logs do Sistema')."
                    )


# ==========================================================================
# TAB 3 — LOGS DO SISTEMA
# ==========================================================================
def _render_tab_logs() -> None:
    col_titulo, col_botao = st.columns([4, 1])
    with col_titulo:
        st.subheader("🧾 Logs do Sistema")
        st.caption("Histórico de eventos de monitorização, scraping e notificações.")
    with col_botao:
        st.write("")
        if st.button("🗑️ Limpar Logs", width='stretch'):
            with LOG_LOCK:
                LOG_BUFFER.clear()
            st.rerun()

    with LOG_LOCK:
        conteudo_logs = "\n".join(LOG_BUFFER) if LOG_BUFFER else "Sem atividade registada ainda."

    st.code(conteudo_logs, language="log")


# ==========================================================================
# CABEÇALHO COM MÉTRICAS RÁPIDAS
# ==========================================================================
def _render_metricas() -> None:
    regras_ativas = len(cm.listar_regras(apenas_ativas=True))
    total_regras = len(cm.listar_regras())
    total_oportunidades = dados.contar_oportunidades()
    total_favoritas = dados.contar_favoritas()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Regras ativas", f"{regras_ativas} / {total_regras}")
    col2.metric("Oportunidades encontradas", total_oportunidades)
    col3.metric("⭐ Favoritas", total_favoritas)
    col4.metric("Estado do bot", "🟢 Ativo" if st.session_state["monitor_ativo"] else "🔴 Parado")


# ==========================================================================
# PONTO DE ENTRADA PRINCIPAL
# ==========================================================================
def main() -> None:
    _configurar_logging_global()
    _inicializar_estado()
    _injetar_css_personalizado()

    # Auto-atualização da interface enquanto a monitorização está ativa,
    # para que novas oportunidades e logs apareçam sem o utilizador ter
    # de clicar em nada.
    if AUTOREFRESH_DISPONIVEL and st.session_state["monitor_ativo"]:
        st_autorefresh(interval=5000, key="autorefresh_monitor")

    _render_sidebar()

    st.title("🛍️ Monitor de Oportunidades — Vinted, Wallapop, OLX & Facebook")

    if not cm.listar_regras():
        st.info(
            "👋 **Bem-vindo!** Ainda não tens nenhuma regra de pesquisa configurada. "
            "Vai à tab '⚙️ Regras de Pesquisa' abaixo e cria a tua primeira regra "
            "(ex: nome do produto, preço máximo) para começares a receber alertas."
        )

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            f"📦 Oportunidades ({dados.contar_por_estado('feed')})",
            f"⭐ Guardadas ({dados.contar_por_estado('guardadas')})",
            f"🗑️ Descartadas ({dados.contar_por_estado('descartadas')})",
            "⚙️ Regras de Pesquisa",
            "🧾 Logs do Sistema",
        ]
    )

    with tab1:
        _render_tab_oportunidades("feed")
    with tab2:
        _render_tab_oportunidades("guardadas")
    with tab3:
        _render_tab_oportunidades("descartadas")
    with tab4:
        _render_tab_regras()
    with tab5:
        _render_tab_logs()


if __name__ == "__main__":
    main()