"""
scraper_engine.py
==================

Motor de busca e scraping genérico para Vinted, Wallapop, OLX e Facebook Marketplace.

Responsável por:
    1. Ler as regras ATIVAS do config.json (via config_manager.py)
    2. Consultar as APIs/páginas públicas de pesquisa da Vinted, Wallapop, OLX e Facebook Marketplace
    3. Filtrar resultados (preço máximo, palavras excluídas)
    4. Evitar duplicados (persistência de IDs já vistos)
    5. Devolver apenas as oportunidades NOVAS, prontas a notificar

IMPORTANTE (Manutenção):
    As APIs públicas da Vinted e da Wallapop não são oficialmente
    documentadas e podem mudar sem aviso prévio (estrutura JSON, endpoints,
    parâmetros). Se o scraper parar de devolver resultados, o mais provável
    é ser necessário atualizar os endpoints/headers abaixo. Todo o código
    de parsing está isolado em funções próprias para facilitar essa
    manutenção futura.

Autor: Desenvolvimento modular - Etapa 2/5
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import logging
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import quote_plus, urljoin
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple, Callable, AsyncGenerator, Generator
from zoneinfo import ZoneInfo
from collections import OrderedDict

import requests
from PIL import Image

import config_manager as cm
import visual_similarity as vs

# --------------------------------------------------------------------------
# Playwright (browser real) — usado como último recurso para a Wallapop
# --------------------------------------------------------------------------
# A Wallapop tem uma proteção anti-bot bastante agressiva que consegue
# distinguir pedidos HTTP "manuais" (como os que fazemos com `requests`,
# mesmo com cabeçalhos realistas) de um browser real. A única forma fiável
# de contornar isto é usar mesmo um browser (Chromium) a correr por trás,
# através do Playwright — o site não consegue distinguir isso de uma
# pessoa a navegar normalmente.
#
# Esta dependência é OPCIONAL: se o pacote `playwright` não estiver
# instalado, o programa continua a funcionar na mesma, só perde este
# fallback extra (fica só com o pedido direto à API e o fallback HTML).
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_DISPONIVEL = True
except ImportError:
    PLAYWRIGHT_DISPONIVEL = False

try:
    from playwright_stealth import Stealth
    STEALTH_DISPONIVEL = True
except ImportError:
    STEALTH_DISPONIVEL = False

# --------------------------------------------------------------------------
# Configuração de logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scraper_engine")

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------
VISTOS_FICHEIRO = "vistos.json"

# Domínio da Vinted a usar (pode ser trocado consoante o país do utilizador)
VINTED_DOMINIO = "www.vinted.pt"

# Coordenadas usadas como referência para a pesquisa na Wallapop
# (Aveiro / região norte de Portugal, por defeito)
WALLAPOP_LATITUDE = 40.6443
WALLAPOP_LONGITUDE = -8.6455

# Localização usada no Facebook Marketplace (slug da cidade no URL).
FACEBOOK_MARKETPLACE_LOCAL = os.environ.get("FACEBOOK_MARKETPLACE_LOCAL", "lisbon")
FACEBOOK_STORAGE_STATE = os.path.join(
    os.environ.get("MONITOR_DATA_DIR") or os.getcwd(),
    "facebook_storage_state.json",
)

TIMEOUT_REQUEST = 15  # segundos
PAUSA_MINIMA = 1.0    # pausa mínima entre pedidos para evitar deteção
PAUSA_MAXIMA = 3.0    # pausa máxima aleatória
MAX_PLATAFORMAS_PARALELAS = 2  # reduzir concorrência para evitar bloqueios
MAX_REGRAS_PARALELAS = 2
MAX_TENTATIVAS_HTTP = 3
CONCORRENCIA_INICIAL = {
    "vinted": 2,      # reduzido de 6 para evitar bloqueios
    "wallapop": 2,    # reduzido de 5
    "olx": 3,         # reduzido de 12
    "facebook": 1,    # reduzido de 3
    "default": 2,     # reduzido de 6
}

# Cache em memória com TTL
CACHE_TTL_SEGUNDOS = 300  # 5 minutos
_cache_pesquisas: OrderedDict[str, Tuple[float, List[Dict[str, Any]]]] = OrderedDict()
_cache_lock = threading.Lock()
CACHE_MAX_SIZE = 1000

# Não excluímos anúncios antigos na recolha: a interface permite filtrá-los
# por idade, pois um anúncio antigo pode ter um preço especialmente vantajoso.
MAX_HORAS_PUBLICACAO = None
# Telegram: só anúncios que acabaram de ser publicados
TELEGRAM_MAX_MINUTOS_PUBLICACAO = 30

# Lista de User-Agents realistas (evita dependência externa como fake-useragent,
# o que simplifica o empacotamento final em .exe com PyInstaller)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Edg/124.0.0.0 Safari/537.36",
]


# --------------------------------------------------------------------------
# Estrutura de dados: Oportunidade
# --------------------------------------------------------------------------
@dataclass
class Oportunidade:
    """Representa um anúncio encontrado que corresponde a uma regra ativa."""

    id_artigo: str
    titulo: str
    preco: float
    moeda: str
    url_anuncio: str
    url_imagem: str
    plataforma: str
    regra_id: str
    regra_nome: str
    data_descoberta: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    data_publicacao: Optional[str] = None  # Data de publicação original da plataforma
    texto_publicacao: Optional[str] = None  # Texto original: "há duas semanas"
    score_similaridade: Optional[float] = None  # Score de similaridade CLIP (0-100)
    metodo_similaridade: Optional[str] = None  # Método usado: "clip"

    def chave_unica(self) -> str:
        """Chave usada para deduplicação (plataforma + id do artigo)."""
        return f"{self.plataforma}:{self.id_artigo}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Gestão de IDs já vistos (anti-duplicados)
# --------------------------------------------------------------------------
def _carregar_vistos(caminho: str = VISTOS_FICHEIRO) -> set:
    """Carrega o conjunto de chaves (plataforma:id) já notificadas anteriormente."""
    if not os.path.exists(caminho):
        return set()
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = json.load(f)
            return set(dados.get("vistos", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Não foi possível ler '{caminho}' ({e}). A começar do zero.")
        return set()


def _guardar_vistos(vistos: set, caminho: str = VISTOS_FICHEIRO) -> None:
    """Persiste o conjunto de chaves já vistas em disco."""
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump({"vistos": sorted(vistos)}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"Erro ao guardar '{caminho}': {e}")


# --------------------------------------------------------------------------
# Utilitários HTTP (headers, pausas, pedidos seguros)
# --------------------------------------------------------------------------
def _headers_aleatorios(referer: Optional[str] = None) -> Dict[str, str]:
    """Gera cabeçalhos HTTP realistas com um User-Agent aleatório.

    Por omissão usa google.com como referer (razoável para "aquecer" uma
    sessão, simulando alguém que chegou via pesquisa). Mas para descarregar
    imagens hospedadas em CDNs com proteção anti-hotlinking (comum na
    Wallapop e no OLX), é preciso passar o referer da própria plataforma,
    ou o pedido é rejeitado (403) mesmo sem ser bloqueio "anti-bot" a sério.
    """
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer or "https://www.google.com/",
        "Connection": "keep-alive",
    }


# Referers específicos por plataforma, usados sobretudo para descarregar
# imagens hospedadas em CDNs com proteção anti-hotlinking.
_REFERER_POR_PLATAFORMA = {
    "vinted": f"https://{VINTED_DOMINIO}/",
    "wallapop": "https://www.wallapop.com/",
    "olx": "https://www.olx.pt/",
    "facebook": "https://www.facebook.com/",
}


def _referer_da_url_imagem(url: str) -> str:
    """Deduz a plataforma pela URL da imagem para escolher o referer certo."""
    url_lower = (url or "").lower()
    if "vinted" in url_lower:
        return _REFERER_POR_PLATAFORMA["vinted"]
    if "wallapop" in url_lower:
        return _REFERER_POR_PLATAFORMA["wallapop"]
    if "olx" in url_lower or "olxcdn" in url_lower:
        return _REFERER_POR_PLATAFORMA["olx"]
    if "facebook" in url_lower or "fbcdn" in url_lower:
        return _REFERER_POR_PLATAFORMA["facebook"]
    return "https://www.google.com/"


def _pausa_aleatoria() -> None:
    """Mantida por compatibilidade. O ritmo dos pedidos é controlado pelo rate limit."""
    return


# --------------------------------------------------------------------------
# Sessão HTTP persistente (com "aquecimento" de cookies)
# --------------------------------------------------------------------------
# Tanto a Vinted como a Wallapop rejeitam pedidos "frios" diretos à API de
# pesquisa (erros 401/403) quando não existe uma sessão de navegação válida
# associada. A solução é usar uma requests.Session() que visita primeiro a
# página normal do site (tal como um browser faria), guarda os cookies que
# o servidor devolve, e só depois usa essa mesma sessão para consultar a
# API de pesquisa — exatamente como um utilizador real faria ao abrir o
# site e pesquisar.
_sessao_http: Optional[requests.Session] = None
_sessao_http_por_thread = threading.local()
_playwright_lock = threading.Lock()
_clip_lock = threading.Lock()
_LIMITADORES: Dict[str, "_LimitadorPlataforma"] = {}
_LIMITADORES_LOCK = threading.Lock()


class _LimitadorPlataforma:
    """Controla concorrência e espera só quando o site devolve rate limit.
    
    Implementa rate limiting adaptativo que:
    - Aumenta rapidamente a concorrência quando não há bloqueios
    - Reduz agressivamente quando recebe 429 (rate limit)
    - Mantém histórico de sucessos/falhas para ajuste dinâmico
    """

    def __init__(self, nome: str, concorrencia: int) -> None:
        self.nome = nome
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.concorrencia = max(1, concorrencia)
        self.concorrencia_max = max(1, concorrencia)
        self.em_curso = 0
        self.min_intervalo = 0.0
        self.proximo_livre = 0.0
        
        # Histórico para adaptação dinâmica
        self.sucessos_consecutivos = 0
        self.falhas_consecutivas = 0
        self.total_pedidos = 0
        self.total_sucessos = 0

    def adquirir(self) -> None:
        with self._cond:
            while True:
                agora = time.monotonic()
                espera = self.proximo_livre - agora
                if self.em_curso < self.concorrencia and espera <= 0:
                    self.em_curso += 1
                    self.proximo_livre = agora + self.min_intervalo
                    return
                self._cond.wait(timeout=max(espera, 0.02))

    def libertar(self, status_code: int = 200, retry_after: Optional[float] = None) -> None:
        with self._cond:
            self.em_curso = max(0, self.em_curso - 1)
            agora = time.monotonic()
            self.total_pedidos += 1
            
            if status_code == 429:
                self.falhas_consecutivas += 1
                self.sucessos_consecutivos = 0
                
                # Ajuste agressivo em caso de rate limit
                espera = retry_after if retry_after and retry_after > 0 else max(2.0, (self.min_intervalo * 3) or 2.0)
                self.min_intervalo = min(15.0, max(self.min_intervalo * 2.5, 0.5))
                
                # Redução mais agressiva da concorrência
                reducao = max(1, self.concorrencia // 2)
                self.concorrencia = max(1, self.concorrencia - reducao)
                self.proximo_livre = max(self.proximo_livre, agora + espera)
                
                logger.warning(
                    "Rate limit (429) em %s — a esperar %.1fs, reduzindo concorrência para %s (falhas consecutivas: %d).",
                    self.nome,
                    espera,
                    self.concorrencia,
                    self.falhas_consecutivas,
                )
            elif 200 <= status_code < 300:
                self.total_sucessos += 1
                self.sucessos_consecutivos += 1
                self.falhas_consecutivas = 0
                
                # Redução gradual do intervalo após sucessos
                self.min_intervalo *= 0.7
                if self.min_intervalo < 0.02:
                    self.min_intervalo = 0.0
                
                # Aumento progressivo da concorrência após muitos sucessos
                if self.sucessos_consecutivos > 5 and self.concorrencia < self.concorrencia_max:
                    self.concorrencia = min(self.concorrencia_max, self.concorrencia + 1)
                    self.sucessos_consecutivos = 0
                    
                # Ajuste dinâmico baseado em taxa de sucesso
                if self.total_pedidos > 10:
                    taxa_sucesso = self.total_sucessos / self.total_pedidos
                    if taxa_sucesso > 0.95 and self.concorrencia < self.concorrencia_max:
                        self.concorrencia = min(self.concorrencia_max, self.concorrencia + 1)
            else:
                # Erros não-429 também causam redução moderada
                self.falhas_consecutivas += 1
                self.sucessos_consecutivos = 0
                if self.falhas_consecutivas > 2:
                    self.concorrencia = max(1, self.concorrencia - 1)
                    
            self._cond.notify_all()
    
    def obter_estatisticas(self) -> Dict[str, Any]:
        """Retorna estatísticas atuais do limitador."""
        with self._cond:
            return {
                "nome": self.nome,
                "concorrencia_atual": self.concorrencia,
                "concorrencia_max": self.concorrencia_max,
                "em_curso": self.em_curso,
                "min_intervalo": self.min_intervalo,
                "sucessos_consecutivos": self.sucessos_consecutivos,
                "falhas_consecutivas": self.falhas_consecutivas,
                "total_pedidos": self.total_pedidos,
                "taxa_sucesso": self.total_sucessos / self.total_pedidos if self.total_pedidos > 0 else 0.0,
            }


def _plataforma_da_url(url: str) -> str:
    """Identifica o marketplace a partir da URL para aplicar o rate limit certo."""
    url_lower = (url or "").lower()
    if "vinted" in url_lower:
        return "vinted"
    if "wallapop" in url_lower:
        return "wallapop"
    if "olx" in url_lower:
        return "olx"
    if "facebook" in url_lower or "fbcdn" in url_lower:
        return "facebook"
    return "default"


def _limitador(plataforma: str) -> _LimitadorPlataforma:
    with _LIMITADORES_LOCK:
        if plataforma not in _LIMITADORES:
            _LIMITADORES[plataforma] = _LimitadorPlataforma(
                plataforma, CONCORRENCIA_INICIAL.get(plataforma, 4)
            )
        return _LIMITADORES[plataforma]


def _retry_after_segundos(resposta: Optional[requests.Response]) -> Optional[float]:
    if resposta is None:
        return None
    valor = resposta.headers.get("Retry-After")
    if not valor:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _obter_sessao_http() -> requests.Session:
    """Devolve uma sessão isolada por thread, com cookies persistentes no fluxo."""
    sessao = getattr(_sessao_http_por_thread, "sessao", None)
    if sessao is None:
        sessao = requests.Session()
        # Otimizar pooling de conexões com keep-alive
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=3,
            pool_block=False
        )
        sessao.mount('http://', adapter)
        sessao.mount('https://', adapter)
        sessao.headers.update(_headers_aleatorios())
        _sessao_http_por_thread.sessao = sessao
    return sessao


def _marcador_sessao_aquecida() -> Dict[str, bool]:
    estado = getattr(_sessao_http_por_thread, "aquecida", None)
    if estado is None:
        estado = {"vinted": False, "wallapop": False, "olx": False, "facebook": False}
        _sessao_http_por_thread.aquecida = estado
    return estado


def _aquecer_sessao(plataforma: str, url_homepage: str) -> None:
    """
    Visita a homepage da plataforma para obter os cookies de sessão
    necessários antes de consultar a respetiva API de pesquisa.

    Só faz isto uma vez por thread (por plataforma), para não gastar
    pedidos extra desnecessários em cada ciclo de busca.
    """
    aquecida = _marcador_sessao_aquecida()
    if aquecida.get(plataforma):
        return

    try:
        resposta = _executar_get(url_homepage)
        if resposta is not None and resposta.status_code == 200:
            logger.info(f"Sessão HTTP da {plataforma.capitalize()} estabelecida com sucesso.")
        else:
            codigo = resposta.status_code if resposta is not None else "sem resposta"
            logger.warning(
                f"Aquecimento de sessão da {plataforma.capitalize()} devolveu "
                f"{codigo}. Os pedidos seguintes podem falhar."
            )
    except requests.exceptions.RequestException as e:
        logger.warning(f"Não foi possível aquecer a sessão da {plataforma.capitalize()}: {e}")

    aquecida[plataforma] = True


def _executar_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    usar_sessao: bool = True,
    custom_headers: Optional[Dict[str, str]] = None,
) -> Optional[requests.Response]:
    """GET com concorrência por marketplace e retry só em rate limit."""
    plataforma = _plataforma_da_url(url)
    limitador = _limitador(plataforma)
    ultima_resposta: Optional[requests.Response] = None

    for tentativa in range(1, MAX_TENTATIVAS_HTTP + 1):
        limitador.adquirir()
        status = 0
        retry_after = None
        try:
            cliente = _obter_sessao_http() if usar_sessao else requests
            headers = None if usar_sessao else _headers_aleatorios()
            if custom_headers:
                if headers is None:
                    headers = dict(getattr(cliente, "headers", {}))
                headers.update(custom_headers)
            ultima_resposta = cliente.get(
                url, params=params, headers=headers, timeout=TIMEOUT_REQUEST
            )
            status = ultima_resposta.status_code
            retry_after = _retry_after_segundos(ultima_resposta)
            if status != 429:
                return ultima_resposta
            logger.warning(
                "Rate limit (429) em '%s' (tentativa %s/%s).",
                url,
                tentativa,
                MAX_TENTATIVAS_HTTP,
            )
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout ao contactar '{url}'.")
            return None
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Erro de ligação a '{url}': {e}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Erro inesperado no pedido a '{url}': {e}")
            return None
        finally:
            limitador.libertar(status_code=status or 200, retry_after=retry_after)

    return ultima_resposta


def _pedido_seguro(
    url: str, params: Optional[Dict[str, Any]] = None, usar_sessao: bool = True, custom_headers: Optional[Dict[str, str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Executa um GET HTTP com tratamento robusto de erros.

    Por omissão usa a sessão HTTP partilhada (com cookies já "aquecidos"),
    o que resolve a maioria dos erros 401/403 nas APIs da Vinted/Wallapop.

    Devolve o JSON da resposta em caso de sucesso, ou None em caso de falha
    (o erro é registado no log, mas nunca interrompe o programa).
    """
    try:
        resposta = _executar_get(
            url, params=params, usar_sessao=usar_sessao, custom_headers=custom_headers
        )
        if resposta is None:
            return None

        if resposta.status_code == 200:
            return resposta.json()

        if resposta.status_code == 429:
            logger.warning(f"Rate limit (429) persistente em '{url}'.")
        elif resposta.status_code == 403:
            logger.warning(
                f"Acesso bloqueado (403) em '{url}'. "
                "O site pode ter deteção anti-bot ativa nesta rede/IP."
            )
        elif resposta.status_code == 401:
            logger.warning(
                f"Não autorizado (401) em '{url}'. A sessão pode ter expirado "
                "ou o site exige autenticação adicional para este pedido."
            )
        else:
            logger.warning(f"Resposta inesperada ({resposta.status_code}) em '{url}'.")
        return None

    except ValueError:
        logger.warning(f"Resposta de '{url}' não é JSON válido.")
        return None


def _pedido_bytes(url: str) -> Optional[bytes]:
    """Descarrega conteúdo binário (foto de anúncio) com a sessão partilhada."""
    if not url:
        return None
    headers = _headers_aleatorios(referer=_referer_da_url_imagem(url))
    resposta = _executar_get(url, custom_headers=headers)
    if resposta is not None and resposta.status_code == 200 and resposta.content:
        return resposta.content
    codigo = resposta.status_code if resposta is not None else "sem resposta"
    logger.warning(f"Não foi possível descarregar imagem ({codigo}): {url}")
    return None


def _pedido_texto(url: str) -> Optional[str]:
    """Obtém o HTML público de um anúncio para ler a idade apresentada pela plataforma."""
    if not url:
        return None
    resposta = _executar_get(url, custom_headers=_headers_aleatorios())
    if resposta is not None and resposta.status_code == 200:
        return resposta.text
    codigo = resposta.status_code if resposta is not None else "sem resposta"
    logger.debug(f"Não foi possível abrir o anúncio ({codigo}): {url}")
    return None


def _estender_paralelo(func, itens: List[Any], max_workers: Optional[int] = None) -> List[Any]:
    """Corre `func` em paralelo e junta as listas devolvidas."""
    itens = list(itens)
    if not itens:
        return []
    if len(itens) == 1:
        return list(func(itens[0]) or [])
    workers = max(1, min(len(itens), max_workers or len(itens)))
    agrupados: List[Any] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futuros = [executor.submit(func, item) for item in itens]
        for futuro in as_completed(futuros):
            agrupados.extend(futuro.result() or [])
    return agrupados


# --------------------------------------------------------------------------
# Cache em memória com TTL para pesquisas frequentes
# --------------------------------------------------------------------------
def _gerar_chave_cache(regra: Dict[str, Any], plataforma: str, termo: str = "") -> str:
    """Gera uma chave única para o cache baseada nos parâmetros da pesquisa."""
    dados_chave = {
        "plataforma": plataforma,
        "termo": termo,
        "preco_max": regra.get("preco_maximo", 0),
        "preco_min": regra.get("preco_minimo", 0),
        "excluidas": sorted(regra.get("palavras_excluidas", [])),
    }
    return hashlib.md5(json.dumps(dados_chave, sort_keys=True).encode()).hexdigest()


def _obter_cache(chave: str) -> Optional[List[Dict[str, Any]]]:
    """Retorna resultados do cache se ainda válidos."""
    with _cache_lock:
        if chave in _cache_pesquisas:
            timestamp, dados = _cache_pesquisas[chave]
            if time.time() - timestamp < CACHE_TTL_SEGUNDOS:
                # Mover para o final (LRU)
                _cache_pesquisas.move_to_end(chave)
                return dados
            else:
                del _cache_pesquisas[chave]
    return None


def _guardar_cache(chave: str, dados: List[Dict[str, Any]]) -> None:
    """Guarda resultados no cache com timestamp atual."""
    with _cache_lock:
        # Remover entrada mais antiga se exceder tamanho máximo
        if len(_cache_pesquisas) >= CACHE_MAX_SIZE:
            _cache_pesquisas.popitem(last=False)
        _cache_pesquisas[chave] = (time.time(), dados)


def _limpar_cache_expirado() -> None:
    """Remove entradas expiradas do cache."""
    with _cache_lock:
        agora = time.time()
        expiradas = [chave for chave, (timestamp, _) in _cache_pesquisas.items() 
                    if agora - timestamp >= CACHE_TTL_SEGUNDOS]
        for chave in expiradas:
            del _cache_pesquisas[chave]


# --------------------------------------------------------------------------
# Semelhança visual com CLIP (foto de referência da regra vs. foto do anúncio)
# --------------------------------------------------------------------------
_cache_assinatura_clip_ref: Dict[str, List] = {}  # cache de embeddings CLIP de referência
_cache_assinatura_clip_url: Dict[str, Optional] = {}  # cache de embeddings CLIP de URLs
_cache_data_publicacao: Dict[str, Optional[str]] = {}
_cache_texto_publicacao: Dict[str, Optional[str]] = {}
_cache_data_publicacao_olx: Dict[str, Optional[str]] = {}


def _caminhos_imagens_referencia(regra: Dict[str, Any]) -> List[str]:
    """Devolve caminhos válidos das imagens de referência de uma regra."""
    caminhos = cm.imagens_da_regra(regra)
    return [p for p in caminhos if p != "pending" and os.path.isfile(p)]


def _assinaturas_clip_da_referencia(regra: Dict[str, Any]) -> List:
    """
    Gera embeddings CLIP das imagens de referência de uma regra.
    
    Usa cache para evitar recalcular embeddings das mesmas imagens.
    
    Args:
        regra: Dicionário da regra com imagens de referência
    
    Returns:
        Lista de embeddings CLIP (tensores)
    """
    embeddings = []
    
    for caminho in _caminhos_imagens_referencia(regra):
        # Cache key baseado no caminho e timestamp do ficheiro
        cache_key = f"{caminho}:{os.path.getmtime(caminho)}"
        
        if cache_key in _cache_assinatura_clip_ref:
            embeddings.append(_cache_assinatura_clip_ref[cache_key])
            continue
        
        try:
            embedding = vs.encode_image_from_path(caminho)
            if embedding is not None:
                _cache_assinatura_clip_ref[cache_key] = embedding
                embeddings.append(embedding)
            else:
                logger.warning(f"Não foi possível gerar embedding CLIP para '{caminho}'")
        except Exception as e:
            logger.warning(f"Erro ao processar imagem de referência '{caminho}': {e}")
    
    return embeddings


async def _assinatura_clip_da_url_async(url: str) -> Optional:
    """
    Gera embedding CLIP de uma imagem a partir de URL de forma assíncrona.
    
    Args:
        url: URL da imagem
    
    Returns:
        Tensor CLIP ou None se falhar
    """
    if not url:
        return None
    
    if url in _cache_assinatura_clip_url:
        return _cache_assinatura_clip_url[url]
    
    try:
        embedding = await vs.encode_image_from_url_async(url)
        _cache_assinatura_clip_url[url] = embedding
        return embedding
    except Exception as e:
        logger.warning(f"Erro ao obter embedding CLIP de URL '{url}': {e}")
        return None


def _calcular_similaridade_clip(embedding1, embedding2) -> float:
    """
    Calcula similaridade CLIP entre dois embeddings.
    
    Args:
        embedding1: Primeiro embedding CLIP
        embedding2: Segundo embedding CLIP
    
    Returns:
        Score de similaridade (0.0-1.0)
    """
    try:
        return vs.compute_cosine_similarity(embedding1, embedding2)
    except Exception as e:
        logger.error(f"Erro ao calcular similaridade CLIP: {e}")
        return 0.0


def _termos_pesquisa(regra: Dict[str, Any]) -> List[str]:
    # Se a regra for de pesquisa apenas por imagem, não pesquisa por termos de texto
    if regra.get("tipo_pesquisa") == "imagem" or (not (regra.get("termo_pesquisa") or "").strip() and _usa_filtro_imagem(regra)):
        return [""]
    termos = [t.strip() for t in str(regra.get("termo_pesquisa") or "").split(",") if t.strip()]
    return termos or [""]


def _usa_filtro_imagem(regra: Dict[str, Any]) -> bool:
    return bool(_caminhos_imagens_referencia(regra))


def _plataformas_da_regra(regra: Dict[str, Any]) -> List[str]:
    """Plataformas a consultar para uma regra ('ambas' = todas)."""
    plataforma = (regra.get("plataforma") or "ambas").strip().lower()
    if plataforma == "ambas":
        return ["vinted", "wallapop", "olx", "facebook"]
    return [plataforma]


def _parse_data_publicacao(valor: Any) -> Optional[datetime]:
    """Tenta converter a data de publicação da plataforma num datetime UTC."""
    if not valor:
        return None
    try:
        if isinstance(valor, (int, float)):
            ts = float(valor)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        texto = str(valor).strip()
        if not texto:
            return None
        if texto.replace(".", "", 1).isdigit():
            ts = float(texto)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        try:
            return datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            minutos = _minutos_de_texto_relativo(texto)
            if minutos is None:
                return None
            return datetime.now(timezone.utc) - timedelta(minutes=minutos)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _extrair_data_publicacao(item: Dict[str, Any]) -> Optional[str]:
    """Obtém e normaliza a data disponível numa resposta de plataforma."""
    campos = {
        "created_at", "created_at_ts", "createdat", "createdatts",
        "timestamp", "published_at", "published_at_ts", "publishedat",
        "published_date", "date_created", "creation_date", "upload_date",
        "dateposted", "datepublished", "created_time", "createdtime",
    }
    fontes = []
    pendentes = [item]
    while pendentes:
        fonte = pendentes.pop()
        if isinstance(fonte, list):
            pendentes.extend(fonte)
            continue
        if not isinstance(fonte, dict):
            continue
        fontes.append(fonte)
        pendentes.extend(valor for valor in fonte.values() if isinstance(valor, (dict, list)))
    for fonte in fontes:
        for nome_campo, valor in fonte.items():
            if str(nome_campo).replace("-", "_").lower() not in campos:
                continue
            data = _parse_data_publicacao(valor)
            if data is not None:
                if data.tzinfo is None:
                    data = data.replace(tzinfo=timezone.utc)
                return data.isoformat()
    return None


def _minutos_de_texto_relativo(texto: str) -> Optional[int]:
    """Converte idades relativas, incluindo números por extenso, em minutos."""
    texto = unescape(texto).replace("\\", " ").replace("\xa0", " ").lower()
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto)
    if any(expressao in texto for expressao in ("agora", "just now", "ahora mismo", "just uploaded")):
        return 0
    inicio = r"(?:há|hace|carregado\s+há|publicado\s+há|uploaded|posted)"
    numero = r"(\d+|um|uma|dois|duas|tres|três|quatro|cinco|seis|sete|oito|nove|dez|uno|una|dos|two|three|four|five|one)"
    numeros_por_extenso = {
        "um": 1, "uma": 1, "uno": 1, "una": 1, "one": 1,
        "dois": 2, "duas": 2, "dos": 2, "two": 2,
        "tres": 3, "três": 3, "three": 3,
        "quatro": 4, "four": 4,
        "cinco": 5, "five": 5,
        "seis": 6, "six": 6,
        "sete": 7, "seven": 7,
        "oito": 8, "eight": 8,
        "nove": 9, "nine": 9,
        "dez": 10, "ten": 10,
    }
    unidades = (
        (r"minuto|minutos|min|minute|minutes|m", 1),
        (r"hora|horas|hour|hours|h", 60),
        (r"dia|dias|día|días|day|days|d", 24 * 60),
        (r"semana|semanas|week|weeks|w", 7 * 24 * 60),
        (r"mês|mes|meses|month|months", 30 * 24 * 60),
        (r"ano|anos|año|años|year|years|y", 365 * 24 * 60),
    )
    for unidade, multiplicador in unidades:
        padrao = rf"{inicio}\s+(?:menos de\s+)?{numero}\s*(?:{unidade})\b"
        encontrado = re.search(padrao, texto, flags=re.IGNORECASE)
        if encontrado:
            bruto = encontrado.group(1)
            quantidade = int(bruto) if bruto.isdigit() else numeros_por_extenso[bruto]
            return quantidade * multiplicador
    return None


def _texto_idade_publicacao(texto: Any) -> Optional[str]:
    """Obtém a expressão relativa original para a apresentar sem a reescrever."""
    if not isinstance(texto, str):
        return None
    normalizado = unescape(texto).replace("\\", " ").replace("\xa0", " ")
    padrao = r"(?:carregado\s+há|publicado\s+há|há|hace|uploaded|posted)\s+(?:menos de\s+)?(?:\d+|um|uma|dois|duas|tres|três|quatro|cinco|seis|sete|oito|nove|dez|one|two|three|four|five)\s+(?:minuto\w*|minute\w*|hora\w*|hour\w*|dia\w*|day\w*|semana\w*|week\w*|mês|mes\w*|month\w*|ano\w*|year\w*)"
    encontrado = re.search(padrao, normalizado, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", encontrado.group(0)).strip() if encontrado else None


def _data_publicacao_da_pagina(url_anuncio: str) -> Optional[str]:
    """Lê a idade que a página pública do anúncio mostra ao utilizador."""
    if url_anuncio in _cache_data_publicacao:
        return _cache_data_publicacao[url_anuncio]
    pagina = _pedido_texto(url_anuncio)
    if not pagina:
        _cache_data_publicacao[url_anuncio] = None
        return None

    # Vinted usa este atributo na página renderizada; os outros marcadores
    # cobrem formatos semelhantes usados pela Wallapop.
    marcadores = ("item-attributes-upload_date", '"upload_date"', "upload_date", "published_at")
    for marcador in marcadores:
        inicio = pagina.find(marcador)
        if inicio < 0:
            continue
        minutos = _minutos_de_texto_relativo(pagina[inicio : inicio + 2500])
        if minutos is not None:
            data = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
            _cache_texto_publicacao[url_anuncio] = _texto_idade_publicacao(pagina[inicio : inicio + 2500])
            _cache_data_publicacao[url_anuncio] = data
            return data
    minutos = _minutos_de_texto_relativo(pagina)
    if minutos is not None:
        data = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
        _cache_texto_publicacao[url_anuncio] = _texto_idade_publicacao(pagina)
        _cache_data_publicacao[url_anuncio] = data
        return data
    _cache_data_publicacao[url_anuncio] = None
    return None


def _texto_publicacao_da_pagina(url_anuncio: str) -> Optional[str]:
    """Devolve o texto relativo guardado ao consultar a página do anúncio."""
    if url_anuncio not in _cache_data_publicacao:
        _data_publicacao_da_pagina(url_anuncio)
    return _cache_texto_publicacao.get(url_anuncio)


_cache_og_imagem: Dict[str, Optional[str]] = {}


def _extrair_og_imagem(pagina: str) -> Optional[str]:
    """
    Extrai a imagem principal de uma página a partir da meta tag Open Graph
    (<meta property="og:image" content="...">).

    Esta tag é um padrão universal usado por praticamente todos os
    marketplaces (OLX, Wallapop, Vinted, etc.) para pré-visualizações em
    redes sociais, e por isso é uma fonte de imagem MUITO mais estável do
    que fazer scraping das classes/atributos CSS da página de resultados —
    que mudam com frequência sempre que o site atualiza o design.

    Serve como rede de segurança quando a extração específica da
    plataforma (regex sobre os cartões de resultados) não encontra imagem
    nenhuma, seja porque o markup mudou, seja porque a imagem é carregada
    de forma "preguiçosa" (lazy-loading) e não está no HTML inicial da
    página de pesquisa.
    """
    if not pagina:
        return None
    # A ordem dos atributos varia (property antes ou depois de content)
    padroes = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    )
    for padrao in padroes:
        encontrado = re.search(padrao, pagina, flags=re.IGNORECASE)
        if encontrado:
            url_imagem = unescape(encontrado.group(1)).strip()
            if url_imagem and "placeholder" not in url_imagem.lower():
                return url_imagem
    return None


def _og_imagem_da_pagina(url_anuncio: str) -> Optional[str]:
    """Vai buscar a imagem principal da página individual do anúncio (fallback)."""
    if not url_anuncio:
        return None
    if url_anuncio in _cache_og_imagem:
        return _cache_og_imagem[url_anuncio]
    pagina = _pedido_texto(url_anuncio)
    imagem = _extrair_og_imagem(pagina) if pagina else None
    _cache_og_imagem[url_anuncio] = imagem
    if imagem:
        logger.info(f"Imagem recuperada via og:image (fallback) para '{url_anuncio}'.")
    else:
        logger.warning(
            f"Não foi possível obter imagem (nem pela pesquisa nem via og:image) para '{url_anuncio}'."
        )
    return imagem


def _data_publicacao_olx_da_pagina(url_anuncio: str) -> Optional[str]:
    """Obtém a data absoluta de publicação da página individual do OLX."""
    if url_anuncio in _cache_data_publicacao_olx:
        return _cache_data_publicacao_olx[url_anuncio]
    pagina = _pedido_texto(url_anuncio)
    if not pagina:
        _cache_data_publicacao_olx[url_anuncio] = None
        return None
    visivel = re.search(
        r'data-testid=["\']ad-posted-at["\'][^>]*>\s*Publicado\s*(?:<!--.*?-->)?\s*([^<]+)',
        pagina,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if visivel:
        meses = {
            "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
            "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
            "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
        }
        data_texto = re.sub(r"\s+", " ", unescape(visivel.group(1))).strip()
        data_encontrada = re.search(r"(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})", data_texto, re.IGNORECASE)
        if data_encontrada and data_encontrada.group(2).lower() in meses:
            data = datetime(
                int(data_encontrada.group(3)), meses[data_encontrada.group(2).lower()],
                int(data_encontrada.group(1)), tzinfo=timezone.utc,
            )
            resultado = data.isoformat()
            _cache_data_publicacao_olx[url_anuncio] = resultado
            return resultado
    encontrados = re.findall(
        r'"(?:createdTime|created)\\?"\s*:\s*\\?"([^"\\]+)', pagina,
        flags=re.IGNORECASE,
    )
    for valor in encontrados:
        data = _parse_data_publicacao(valor)
        if data is not None:
            resultado = data.isoformat()
            _cache_data_publicacao_olx[url_anuncio] = resultado
            return resultado
    _cache_data_publicacao_olx[url_anuncio] = None
    return None


def idade_publicacao_minutos(oportunidade: Any) -> Optional[float]:
    """Idade do anúncio em minutos, ou None se a data for desconhecida."""
    bruto = oportunidade.get("data_publicacao") if isinstance(oportunidade, dict) else getattr(
        oportunidade, "data_publicacao", None
    )
    data_pub = _parse_data_publicacao(bruto)
    if data_pub is None:
        return None
    if data_pub.tzinfo is None:
        data_pub = data_pub.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - data_pub).total_seconds() / 60.0


def e_publicacao_recente_telegram(oportunidade: Any) -> bool:
    """True se é recente para a plataforma e está dentro da janela definida."""
    idade = idade_publicacao_minutos(oportunidade)
    if idade is None or idade < 0:
        return False
    if isinstance(oportunidade, dict) and oportunidade.get("plataforma") == "olx":
        data_pub = _parse_data_publicacao(oportunidade.get("data_publicacao"))
        if data_pub is None:
            return False
        hoje_portugal = datetime.now(ZoneInfo("Europe/Lisbon")).date()
        return data_pub.astimezone(ZoneInfo("Europe/Lisbon")).date() == hoje_portugal
    return idade <= TELEGRAM_MAX_MINUTOS_PUBLICACAO


# --------------------------------------------------------------------------
# Scraper: OLX
# --------------------------------------------------------------------------
OLX_DOMINIO = "https://www.olx.pt"
OLX_MAX_PAGINAS = 1
OLX_CONCORRENCIA_DATAS = 8
_cache_resultados_olx: Dict[str, List[Dict[str, Any]]] = {}


def _itens_json_ld_olx(pagina: str) -> List[Dict[str, Any]]:
    """Extrai anúncios do JSON-LD público usado nas páginas de pesquisa do OLX."""
    itens: List[Dict[str, Any]] = []
    blocos = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        pagina,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for bloco in blocos:
        try:
            documento = json.loads(unescape(bloco))
        except (TypeError, json.JSONDecodeError):
            continue
        pendentes = documento if isinstance(documento, list) else [documento]
        while pendentes:
            valor = pendentes.pop()
            if isinstance(valor, list):
                pendentes.extend(valor)
            elif isinstance(valor, dict):
                if valor.get("url") and (valor.get("name") or valor.get("title")):
                    itens.append(valor)
                pendentes.extend(v for v in valor.values() if isinstance(v, (dict, list)))
    return itens


def _itens_cartoes_olx(pagina: str) -> List[Dict[str, Any]]:
    """Extrai os cartões HTML usados atualmente pela pesquisa do OLX.

    O OLX renderiza a imagem do anúncio ANTES do link card-title-link no HTML,
    por isso é necessário pesquisar para trás a partir do link para encontrar
    a imagem de cada anúncio.
    """
    itens: List[Dict[str, Any]] = []
    vistos = set()
    padrao = re.compile(
        r'<a[^>]+data-testid=["\']card-title-link["\'][^>]+href=["\']([^"\']+)["\'][^>]*>',
        re.IGNORECASE,
    )
    for encontrado in padrao.finditer(pagina):
        url_rel = unescape(encontrado.group(1))
        url = urljoin(OLX_DOMINIO, url_rel)
        if url in vistos:
            continue
        vistos.add(url)
        etiqueta = encontrado.group(0)
        titulo_encontrado = re.search(r'aria-label=["\']([^"\']*)["\']', etiqueta, re.IGNORECASE)
        inicio = encontrado.start()

        # Pesquisar para trás (imagem aparece ANTES do link na estrutura HTML do OLX)
        # e também para a frente (preço e data aparecem depois)
        trecho_antes = pagina[max(0, inicio - 3000) : inicio]
        trecho_depois = pagina[inicio : inicio + 5000]
        trecho_completo = trecho_antes + trecho_depois

        preco_encontrado = re.search(r"(\d[\d\s.,]*)\s*€", trecho_depois)

        # Prioritizar imagens do CDN da OLX (ireland.apollo.olxcdn)
        imagem_encontrada = re.search(
            r'<img[^>]+src=["\']([^"\']*ireland\.apollo\.olxcdn[^"\']+)["\']',
            trecho_completo,
            re.IGNORECASE,
        )
        if not imagem_encontrada:
            imagem_encontrada = re.search(
                r'<img[^>]+srcset=["\']([^"\']*ireland\.apollo\.olxcdn[^"\']+)["\']',
                trecho_completo,
                re.IGNORECASE,
            )
        if not imagem_encontrada:
            # fallback genérico
            imagem_encontrada = re.search(
                r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
                trecho_completo,
                re.IGNORECASE,
            )

        url_imagem = ""
        if imagem_encontrada:
            url_bruta = unescape(imagem_encontrada.group(1)).split(",")[0].strip().split(" ")[0]
            # Melhorar a resolução da thumbnail (s=216x152 → s=640x480)
            url_imagem = re.sub(r';s=\d+x\d+', ';s=640x480', url_bruta)
        if "no_thumbnail" in url_imagem.lower():
            url_imagem = ""

        minutos = _minutos_de_texto_relativo(trecho_depois)
        itens.append({
            "url": url,
            "name": unescape(titulo_encontrado.group(1) if titulo_encontrado else "Sem título").replace(" Ver Descrição", ""),
            "price": (preco_encontrado.group(1).replace(".", "").replace(",", ".")
                      if preco_encontrado else "0"),
            "image": url_imagem,
            "data_publicacao": (
                datetime.now(timezone.utc) - timedelta(minutes=minutos)
            ).isoformat() if minutos is not None else None,
        })
    return itens


def _extrair_url_imagem_olx(item: Dict[str, Any]) -> str:
    """
    Extrai a URL da imagem de um item do OLX, seja de que formato vier.

    O bloco JSON-LD do OLX nem sempre representa "image" como uma simples
    string: por vezes é um objeto ImageObject ({"url": "..."}) ou uma lista
    (de strings ou de ImageObjects). Sem tratar estes casos, a maioria dos
    anúncios ficava sem foto mesmo quando ela estava disponível.

    Também aumenta a resolução da thumbnail substituindo o parâmetro de tamanho
    do CDN ireland.apollo.olxcdn.com.
    """
    valor = item.get("image")
    url_imagem = ""

    if isinstance(valor, str):
        url_imagem = valor
    elif isinstance(valor, dict):
        url_imagem = valor.get("url") or valor.get("contentUrl") or ""
    elif isinstance(valor, list):
        for elemento in valor:
            if isinstance(elemento, str) and elemento:
                url_imagem = elemento
                break
            if isinstance(elemento, dict):
                url_candidate = elemento.get("url") or elemento.get("contentUrl")
                if url_candidate:
                    url_imagem = url_candidate
                    break

    # Melhorar a resolução da thumbnail do CDN OLX (s=216x152 → s=640x480)
    if url_imagem and "olxcdn" in url_imagem:
        url_imagem = re.sub(r';s=\d+x\d+', ';s=640x480', url_imagem)

    return url_imagem


def _buscar_olx_termo(regra: Dict[str, Any], termo: str) -> List[Oportunidade]:
    """Pesquisa um termo no OLX (páginas em sequência rápida, detalhes em paralelo)."""
    resultados: List[Oportunidade] = []
    for pagina_numero in range(1, OLX_MAX_PAGINAS + 1):
        url_pesquisa = (
            f"{OLX_DOMINIO}/ads/q-{quote_plus(termo)}/"
            if termo
            else f"{OLX_DOMINIO}/ads/"
        )
        if pagina_numero > 1:
            url_pesquisa += f"?page={pagina_numero}"
        if url_pesquisa in _cache_resultados_olx:
            itens = _cache_resultados_olx[url_pesquisa]
        else:
            pagina = _pedido_texto(url_pesquisa)
            if not pagina:
                break
            itens = _itens_json_ld_olx(pagina)
            itens = [item for item in itens if item.get("@type") != "WebSite"] or _itens_cartoes_olx(pagina)
            _cache_resultados_olx[url_pesquisa] = itens
        if not itens:
            break
        itens_validos = []
        for item in itens:
            try:
                oferta = item.get("offers") or {}
                preco = float(oferta.get("price", item.get("price", 0)) or 0)
                titulo = item.get("name") or item.get("title", "Sem título")
                minimo = float(regra.get("preco_minimo", 0) or 0)
                maximo = float(regra.get("preco_maximo", 0) or 0)
                if preco < minimo or preco > maximo:
                    continue
                if any(
                    palavra.strip().lower() in titulo.lower()
                    for palavra in regra.get("palavras_excluidas", [])
                ):
                    continue
                url_anuncio = urljoin(OLX_DOMINIO, item["url"])
                moeda = oferta.get("priceCurrency", "EUR")
                itens_validos.append((item, preco, moeda, titulo, url_anuncio))
            except (KeyError, TypeError, ValueError):
                continue

        def obter_detalhes(item_validado):
            item, preco, moeda, titulo, url_anuncio = item_validado
            data_publicacao = (
                _extrair_data_publicacao(item)
                or item.get("data_publicacao")
                or _data_publicacao_olx_da_pagina(url_anuncio)
            )
            url_imagem = _extrair_url_imagem_olx(item)
            if not url_imagem:
                url_imagem = _og_imagem_da_pagina(url_anuncio) or ""
            return item, preco, moeda, titulo, url_anuncio, data_publicacao, url_imagem

        with ThreadPoolExecutor(max_workers=OLX_CONCORRENCIA_DATAS) as executor:
            itens_com_detalhes = executor.map(obter_detalhes, itens_validos)

        for item, preco, moeda, titulo, url_anuncio, data_publicacao, url_imagem in itens_com_detalhes:
            try:
                identificador = url_anuncio.rstrip("/").rsplit("/", 1)[-1]
                resultados.append(Oportunidade(
                    id_artigo=identificador,
                    titulo=titulo,
                    preco=preco,
                    moeda=moeda,
                    url_anuncio=url_anuncio,
                    url_imagem=url_imagem,
                    plataforma="olx",
                    regra_id=regra["id"],
                    regra_nome=regra["nome"],
                    data_publicacao=data_publicacao,
                ))
            except (KeyError, TypeError, ValueError):
                continue
    return resultados


def buscar_olx(regra: Dict[str, Any]) -> List[Oportunidade]:
    """Pesquisa anúncios públicos do OLX Portugal através da página de pesquisa."""
    _aquecer_sessao("olx", f"{OLX_DOMINIO}/")
    
    # Verificar cache primeiro
    termos = _termos_pesquisa(regra)
    cache_key = _gerar_chave_cache(regra, "olx", ",".join(termos))
    resultados_cache = _obter_cache(cache_key)
    if resultados_cache:
        logger.info(f"Cache HIT para OLX: {len(resultados_cache)} resultados")
        return [Oportunidade(**r) for r in resultados_cache]
    
    resultados = _estender_paralelo(
        lambda termo: _buscar_olx_termo(regra, termo),
        termos,
        max_workers=CONCORRENCIA_INICIAL["olx"],
    )
    
    # Se não houver resultados via HTML, tentar browser real
    if not resultados and PLAYWRIGHT_DISPONIVEL:
        logger.info("HTML do OLX sem resultados, a tentar browser real...")
        resultados = _buscar_olx_via_browser(regra, termos)
    
    # Guardar no cache
    _guardar_cache(cache_key, [o.to_dict() for o in resultados])
    return resultados


def _buscar_olx_via_browser(regra: Dict[str, Any], termos: List[str]) -> List[Oportunidade]:
    """Usa browser real para contornar bloqueios do OLX."""
    if not PLAYWRIGHT_DISPONIVEL:
        return []
    
    if not _garantir_browser_playwright_instalado():
        return []
    
    url_pesquisa = f"{OLX_DOMINIO}/ads/q-{quote_plus(termos[0]) if termos else ''}/"
    
    try:
        with _playwright_lock:
            with sync_playwright() as p:
                navegador = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                try:
                    contexto = navegador.new_context(
                        user_agent=random.choice(USER_AGENTS),
                        locale="pt-PT",
                        viewport={"width": 1280, "height": 900},
                    )
                    contexto.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                    pagina = contexto.new_page()
                    pagina.goto(url_pesquisa, timeout=25000, wait_until="domcontentloaded")
                    pagina.wait_for_timeout(2000)
                    
                    # Ler anúncios do DOM
                    listings = pagina.evaluate(
                        """() => {
                            const itens = [];
                            // Tenta múltiplos seletores para encontrar os cartões
                            const selectors = [
                                '[data-testid="l-card"]',
                                '[data-testid="card"]',
                                '[class*="card"]',
                                'article',
                                '.card'
                            ];
                            
                            let cards = [];
                            for (const selector of selectors) {
                                cards = document.querySelectorAll(selector);
                                if (cards.length > 0) break;
                            }
                            
                            for (const card of cards) {
                                const link = card.querySelector('a[href*="/"]');
                                if (!link) continue;
                                
                                const href = link.href;
                                if (!href.includes('olx.pt')) continue;
                                
                                const img = card.querySelector('img');
                                let titulo = img?.alt || link.getAttribute('title') || link.textContent?.trim() || 'Sem titulo';
                                
                                // Se o título for muito curto, tentar encontrar título no cartão
                                if (titulo.length < 5) {
                                    const titleElements = card.querySelectorAll('h3, h4, [class*="title"], [class*="name"]');
                                    for (const titleEl of titleElements) {
                                        const text = titleEl.textContent?.trim();
                                        if (text && text.length > 5) {
                                            titulo = text;
                                            break;
                                        }
                                    }
                                }
                                
                                let preco = '0';
                                const priceElements = card.querySelectorAll('[class*="price"], h3, [data-testid="ad-price"], .price');
                                for (const priceEl of priceElements) {
                                    const text = priceEl.textContent?.trim();
                                    if (text && (text.includes('€') || /\\d/.test(text))) {
                                        preco = text;
                                        break;
                                    }
                                }
                                
                                let data_publicacao = '';
                                const dateElements = card.querySelectorAll('[data-testid="ad-posted-at"], time, [class*="date"], [class*="time"]');
                                for (const dateEl of dateElements) {
                                    const text = dateEl.textContent?.trim() || dateEl.getAttribute('datetime') || '';
                                    if (text && text.length > 0) {
                                        data_publicacao = text;
                                        break;
                                    }
                                }
                                
                                itens.push({
                                    id: href.split('/').pop() || href,
                                    titulo: titulo.substring(0, 100),
                                    preco: preco,
                                    url_imagem: img?.src || img?.getAttribute('data-src') || '',
                                    url: href,
                                    data_publicacao: data_publicacao
                                });
                            }
                            return itens;
                        }"""
                    )
                    
                    oportunidades = []
                    for item in listings:
                        preco_texto = item.get("preco", "0")
                        try:
                            preco_limpo = str(preco_texto).replace("€", "").replace(".", "").replace(",", ".").strip()
                            preco = float(preco_limpo) if preco_limpo else 0.0
                        except (ValueError, TypeError):
                            preco = 0.0
                        
                        data_publicacao = item.get("data_publicacao", "")
                        if data_publicacao:
                            minutos = _minutos_de_texto_relativo(data_publicacao)
                            if minutos is not None:
                                data_publicacao = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
                        
                        oportunidades.append(Oportunidade(
                            id_artigo=str(item.get("id")),
                            titulo=item.get("titulo") or "Sem título",
                            preco=preco,
                            moeda="EUR",
                            url_anuncio=item.get("url") or f"{OLX_DOMINIO}/",
                            url_imagem=item.get("url_imagem") or "",
                            plataforma="olx",
                            regra_id=regra["id"],
                            regra_nome=regra["nome"],
                            data_publicacao=data_publicacao if data_publicacao else None,
                            texto_publicacao=data_publicacao if data_publicacao else None,
                        ))
                    
                    logger.info(f"Browser real obteve {len(oportunidades)} resultados do OLX")
                    return oportunidades
                    
                finally:
                    navegador.close()
    except Exception as e:
        logger.error(f"Erro ao usar browser real para OLX: {e}")
        return []


# --------------------------------------------------------------------------
# Scraper: Vinted
# --------------------------------------------------------------------------
def buscar_vinted(regra: Dict[str, Any]) -> List[Oportunidade]:
    """
    Consulta o catálogo público da Vinted para uma dada regra de busca.

    Utiliza o endpoint público de catálogo, filtrando já por preço máximo
    diretamente na query (otimização), e reforça o filtro localmente
    para garantir consistência.

    Suporta múltiplos termos de pesquisa separados por vírgula.
    """
    _aquecer_sessao("vinted", f"https://{VINTED_DOMINIO}/")
    
    # Verificar cache primeiro
    termos = _termos_pesquisa(regra)
    cache_key = _gerar_chave_cache(regra, "vinted", ",".join(termos))
    resultados_cache = _obter_cache(cache_key)
    if resultados_cache:
        logger.info(f"Cache HIT para Vinted: {len(resultados_cache)} resultados")
        return [Oportunidade(**r) for r in resultados_cache]
    
    por_pagina = 48 if _usa_filtro_imagem(regra) else 20

    def _buscar_termo(termo: str) -> List[Oportunidade]:
        encontradas: List[Oportunidade] = []
        paginas_a_buscar = 3 if (_usa_filtro_imagem(regra) and not termo) else 1
        regra_id = regra["id"]
        regra_nome = regra["nome"]
        preco_max = regra["preco_maximo"]
        preco_min = regra.get("preco_minimo", 0)
        
        for pagina_num in range(1, paginas_a_buscar + 1):
            url = f"https://{VINTED_DOMINIO}/api/v2/catalog/items"
            params = {
                "order": "newest_first",
                "per_page": por_pagina,
                "price_to": preco_max,
                "price_from": preco_min,
                "currency": "EUR",
            }
            if termo:
                params["search_text"] = termo
            if pagina_num > 1:
                params["page"] = pagina_num

            dados = _pedido_seguro(url, params=params)
            if not dados:
                break

            items_recebidos = dados.get("items", [])
            if not items_recebidos:
                break

            # Parsing otimizado com validação mínima
            for item in items_recebidos:
                try:
                    preco_bruto = item.get("price") or {}
                    preco = float(preco_bruto.get("amount", 0) or 0)
                    moeda = preco_bruto.get("currency_code", "EUR") or "EUR"
                    
                    # Extração otimizada de imagem
                    photo = item.get("photo") or {}
                    url_imagem = photo.get("url", "") if isinstance(photo, dict) else ""
                    
                    encontradas.append(Oportunidade(
                        id_artigo=str(item.get("id", "")),
                        titulo=item.get("title", "Sem título") or "Sem título",
                        preco=preco,
                        moeda=moeda,
                        url_anuncio=item.get("url", f"https://{VINTED_DOMINIO}") or f"https://{VINTED_DOMINIO}",
                        url_imagem=url_imagem,
                        plataforma="vinted",
                        regra_id=regra_id,
                        regra_nome=regra_nome,
                        data_publicacao=_extrair_data_publicacao(item),
                        texto_publicacao=_texto_idade_publicacao(str(item)),
                    ))
                except (KeyError, TypeError, ValueError) as e:
                    logger.debug(f"Item da Vinted ignorado por erro de parsing: {e}")
                    continue
        return encontradas

    resultados = _estender_paralelo(
        _buscar_termo, termos, max_workers=CONCORRENCIA_INICIAL["vinted"]
    )
    
    # Se não houver resultados via API, tentar browser real
    if not resultados and PLAYWRIGHT_DISPONIVEL:
        logger.info("API da Vinted sem resultados, a tentar browser real...")
        resultados = _buscar_vinted_via_browser(regra, termos)
    
    # Guardar no cache
    _guardar_cache(cache_key, [o.to_dict() for o in resultados])
    return resultados


def _buscar_vinted_via_browser(regra: Dict[str, Any], termos: List[str]) -> List[Oportunidade]:
    """Usa browser real para contornar bloqueios da Vinted."""
    if not PLAYWRIGHT_DISPONIVEL:
        return []
    
    if not _garantir_browser_playwright_instalado():
        return []
    
    url_pesquisa = f"https://{VINTED_DOMINIO}/catalog?search_text={quote_plus(termos[0]) if termos else ''}"
    
    try:
        with _playwright_lock:
            with sync_playwright() as p:
                navegador = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                try:
                    contexto = navegador.new_context(
                        user_agent=random.choice(USER_AGENTS),
                        locale="pt-PT",
                        viewport={"width": 1280, "height": 900},
                    )
                    contexto.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                    pagina = contexto.new_page()
                    
                    if STEALTH_DISPONIVEL:
                        Stealth().apply_stealth_sync(pagina)
                        
                    pagina.goto(url_pesquisa, timeout=25000, wait_until="domcontentloaded")
                    pagina.wait_for_timeout(2000)
                    
                    # Ler anúncios do DOM
                    listings = pagina.evaluate(
                        """() => {
                            const itens = [];
                            // Tenta múltiplos seletores para encontrar os cartões
                            const selectors = [
                                '[class*="ItemCard"]',
                                '[class*="item-card"]', 
                                '[class*="item"]',
                                'article',
                                '.item'
                            ];
                            
                            let cards = [];
                            for (const selector of selectors) {
                                cards = document.querySelectorAll(selector);
                                if (cards.length > 0) break;
                            }
                            
                            for (const card of cards) {
                                const link = card.querySelector('a[href*="/item"]');
                                if (!link) continue;
                                
                                const href = link.href;
                                const match = href.match(/items?\\/([^-]+)/);
                                if (!match) continue;
                                
                                const img = card.querySelector('img');
                                let titulo = img?.alt || link.getAttribute('title') || link.textContent?.trim() || 'Sem titulo';
                                
                                // Se o título for muito curto, tentar encontrar título no cartão
                                if (titulo.length < 5) {
                                    const titleElements = card.querySelectorAll('h3, h4, [class*="title"], [class*="name"]');
                                    for (const titleEl of titleElements) {
                                        const text = titleEl.textContent?.trim();
                                        if (text && text.length > 5) {
                                            titulo = text;
                                            break;
                                        }
                                    }
                                }
                                
                                let preco = '0';
                                const priceElements = card.querySelectorAll('[class*="price"], h3, .price, [data-testid="price"]');
                                for (const priceEl of priceElements) {
                                    const text = priceEl.textContent?.trim();
                                    if (text && (text.includes('€') || /\\d/.test(text))) {
                                        preco = text;
                                        break;
                                    }
                                }
                                
                                let data_publicacao = '';
                                const dateElements = card.querySelectorAll('[class*="date"], time, [class*="time"], [class*="published"]');
                                for (const dateEl of dateElements) {
                                    const text = dateEl.textContent?.trim() || dateEl.getAttribute('datetime') || '';
                                    if (text && text.length > 0) {
                                        data_publicacao = text;
                                        break;
                                    }
                                }
                                
                                itens.push({
                                    id: match[1],
                                    titulo: titulo.substring(0, 100),
                                    preco: preco,
                                    url_imagem: img?.src || img?.getAttribute('data-src') || '',
                                    url: href,
                                    data_publicacao: data_publicacao
                                });
                            }
                            return itens;
                        }"""
                    )
                    
                    oportunidades = []
                    for item in listings:
                        preco_texto = item.get("preco", "0")
                        try:
                            preco_limpo = str(preco_texto).replace("€", "").replace(".", "").replace(",", ".").strip()
                            preco = float(preco_limpo) if preco_limpo else 0.0
                        except (ValueError, TypeError):
                            preco = 0.0
                        
                        data_publicacao = item.get("data_publicacao", "")
                        if data_publicacao:
                            minutos = _minutos_de_texto_relativo(data_publicacao)
                            if minutos is not None:
                                data_publicacao = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
                        
                        oportunidades.append(Oportunidade(
                            id_artigo=str(item.get("id")),
                            titulo=item.get("titulo") or "Sem título",
                            preco=preco,
                            moeda="EUR",
                            url_anuncio=item.get("url") or f"https://{VINTED_DOMINIO}",
                            url_imagem=item.get("url_imagem") or "",
                            plataforma="vinted",
                            regra_id=regra["id"],
                            regra_nome=regra["nome"],
                            data_publicacao=data_publicacao if data_publicacao else None,
                            texto_publicacao=data_publicacao if data_publicacao else None,
                        ))
                    
                    logger.info(f"Browser real obteve {len(oportunidades)} resultados da Vinted")
                    return oportunidades
                    
                finally:
                    navegador.close()
    except Exception as e:
        logger.error(f"Erro ao usar browser real para Vinted: {e}")
        return []


# --------------------------------------------------------------------------
# Scraper: Wallapop
# --------------------------------------------------------------------------

# Controla se já verificámos/instalámos o Chromium do Playwright nesta
# execução do programa, para não repetir a verificação a cada ciclo.
_playwright_browser_pronto: Optional[bool] = None


def _garantir_browser_playwright_instalado() -> bool:
    """
    Garante que o Chromium necessário ao Playwright está instalado.

    Se não estiver (primeira vez que o programa corre nesta máquina),
    tenta instalá-lo automaticamente uma única vez. Isto precisa de
    ligação à internet e demora algum tempo (~50-150 MB a descarregar),
    mas só acontece uma vez — fica guardado na máquina para sempre.

    Devolve True se o browser está pronto a usar, False caso contrário
    (nesse caso, o chamador deve desistir deste fallback sem rebentar).
    """
    global _playwright_browser_pronto
    if not PLAYWRIGHT_DISPONIVEL:
        return False
    if _playwright_browser_pronto is not None:
        return _playwright_browser_pronto

    try:
        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=True)
            navegador.close()
        logger.info("Browser Chromium do Playwright já está instalado e pronto a usar.")
        _playwright_browser_pronto = True
        return True
    except Exception:
        logger.info(
            "Browser Chromium do Playwright ainda não está instalado nesta máquina. "
            "A instalar automaticamente (só é necessário uma vez; pode demorar um "
            "minuto ou dois, é preciso ligação à internet)..."
        )

    # NOTA DE EMPACOTAMENTO (.exe via PyInstaller): dentro do executável
    # gerado, `sys.executable` aponta para o próprio MonitorOportunidades.exe
    # e não para um interpretador Python — por isso `sys.executable -m
    # playwright install` NÃO funciona aí (só funciona em modo
    # desenvolvimento, a correr com `python app.py`/`streamlit run`).
    # Para funcionar também dentro do .exe, chamamos diretamente o driver
    # interno que o próprio pacote `playwright` já traz (um executável
    # autónomo, incluído no build via `--collect-all playwright` no
    # build.bat), em vez de depender de `python -m ...`.
    comando = None
    ambiente = None
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        executavel_driver, cli_driver = compute_driver_executable()
        comando = [str(executavel_driver), str(cli_driver), "install", "chromium"]
        ambiente = get_driver_env()
    except Exception:
        # API interna indisponível/mudou de versão — cai para o modo
        # "desenvolvimento" (funciona quando corre via `python`, não dentro
        # do .exe empacotado).
        comando = [sys.executable, "-m", "playwright", "install", "chromium"]

    try:
        resultado = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=600,
            env=ambiente,
        )
        if resultado.returncode == 0:
            logger.info("Browser Chromium do Playwright instalado com sucesso.")
            _playwright_browser_pronto = True
            return True
        logger.error(
            "Falha ao instalar automaticamente o Chromium do Playwright "
            f"(código {resultado.returncode}): {resultado.stderr[-800:] if resultado.stderr else 'sem detalhes'}"
        )
    except Exception as e:
        logger.error(f"Não foi possível instalar automaticamente o Chromium do Playwright: {e}")

    _playwright_browser_pronto = False
    return False


def _construir_oportunidades_wallapop(
    resultados: List[Dict[str, Any]], regra: Dict[str, Any]
) -> List[Oportunidade]:
    """
    Converte a lista de itens devolvida pela API da Wallapop (quer tenha
    vindo de um pedido HTTP direto, quer tenha vindo capturada de um
    browser real) em objetos `Oportunidade`.

    Isolado numa função própria para que o pedido direto e o fallback via
    browser (Playwright) partilhem exatamente a mesma lógica de parsing.
    
    Otimizado para parsing mais rápido com validação mínima necessária.
    """
    oportunidades: List[Oportunidade] = []
    regra_id = regra["id"]
    regra_nome = regra["nome"]
    
    for item in resultados if isinstance(resultados, list) else []:
        try:
            # Extração direta com tratamento de erros mínimo
            preco = float(item.get("price", 0) or 0)
            moeda = item.get("currency", "EUR") or "EUR"
            item_id = str(item.get("id", ""))
            titulo = item.get("title", "Sem título") or "Sem título"

            # Extração otimizada de imagem
            imagens = item.get("images", [])
            url_imagem = ""
            if imagens and isinstance(imagens, list) and len(imagens) > 0:
                primeira_imagem = imagens[0]
                if isinstance(primeira_imagem, dict):
                    urls = primeira_imagem.get("urls", {})
                    if isinstance(urls, dict):
                        url_imagem = urls.get("big", "") or primeira_imagem.get("url", "")
                    else:
                        url_imagem = primeira_imagem.get("url", "")

            url_anuncio = item.get("web_slug", "")
            if url_anuncio:
                url_anuncio = f"https://es.wallapop.com/item/{url_anuncio}"
            else:
                url_anuncio = "https://www.wallapop.com"

            # Extração de data apenas se disponível (evita parsing desnecessário)
            data_publicacao = None
            texto_publicacao = None
            if item:
                data_publicacao = _extrair_data_publicacao(item)
                if not data_publicacao:
                    texto_publicacao = _texto_idade_publicacao(str(item))

            oportunidades.append(Oportunidade(
                id_artigo=item_id,
                titulo=titulo,
                preco=preco,
                moeda=moeda,
                url_anuncio=url_anuncio,
                url_imagem=url_imagem,
                plataforma="wallapop",
                regra_id=regra_id,
                regra_nome=regra_nome,
                data_publicacao=data_publicacao,
                texto_publicacao=texto_publicacao,
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.debug(f"Item da Wallapop ignorado por erro de parsing: {e}")
            continue
    return oportunidades


def _listings_wallapop_do_dom(pagina) -> List[Dict[str, Any]]:
    """Lê anúncios visíveis do DOM quando a API interna não é capturada."""
    try:
        return pagina.evaluate(
            """() => {
                const vistos = new Set();
                const itens = [];
                
                // Tenta encontrar os cartões de produto com seletor mais específico
                const cards = document.querySelectorAll('[class*="ItemCard"], [class*="card-item"], article');
                
                for (const card of cards) {
                    const link = card.querySelector('a[href*="/item/"]');
                    if (!link) continue;
                    
                    const href = link.href.split('?')[0];
                    const match = href.match(/item\\/([^/]+)/);
                    if (!match || vistos.has(href)) continue;
                    
                    // Extrair título do atributo alt da imagem ou do texto
                    const img = card.querySelector('img');
                    let titulo = img?.alt || link.getAttribute('aria-label') || link.textContent?.trim() || 'Sem titulo';
                    
                    // Se o título ainda for muito curto ou genérico, tentar outro seletor
                    if (titulo.length < 3 || titulo.includes('Image')) {
                        const titleElement = card.querySelector('[class*="title"], [class*="name"], h3, h4');
                        if (titleElement) {
                            titulo = titleElement.textContent?.trim() || titulo;
                        }
                    }
                    
                    // Extrair preço
                    let preco = '0';
                    const priceElement = card.querySelector('[class*="price"], [class*="amount"], .price');
                    if (priceElement) {
                        preco = priceElement.textContent?.trim() || '0';
                    }
                    
                    // Extrair imagem
                    let url_imagem = '';
                    if (img) {
                        url_imagem = img.src || img.getAttribute('data-src') || '';
                    }
                    
                    // Extrair data de publicação
                    let data_publicacao = '';
                    const dateElement = card.querySelector('[class*="date"], [class*="time"], [class*="published"], time');
                    if (dateElement) {
                        data_publicacao = dateElement.textContent?.trim() || dateElement.getAttribute('datetime') || '';
                    }
                    
                    vistos.add(href);
                    itens.push({
                        id: match[1],
                        titulo: titulo.substring(0, 100), // Limitar tamanho
                        preco: preco,
                        url_imagem: url_imagem,
                        url: href,
                        data_publicacao: data_publicacao
                    });
                }
                
                return itens;
            }"""
        ) or []
    except Exception as e:
        logger.debug(f"Não foi possível ler os cartões da Wallapop: {e}")
        return []


def _buscar_wallapop_via_browser(termo: str, regra: Dict[str, Any]) -> List[Oportunidade]:
    """
    Usa um browser Chromium real (via Playwright) para pesquisar na Wallapop.

    Em vez de tentar imitar um browser com pedidos HTTP manuais — que a
    Wallapop consegue detetar e bloquear (403) mesmo com cabeçalhos
    cuidadosamente escolhidos — esta função abre mesmo um browser real e
    deixa-o navegar até à página de pesquisa. A Wallapop não consegue
    distinguir isto de uma pessoa real a usar o site.

    Em vez de tentar "ler" o HTML já renderizado (frágil: muda sempre que
    o site atualiza o design), interceta a resposta da PRÓPRIA chamada
    que o site faz internamente à sua API de pesquisa
    (api.wallapop.com/api/v3/general/search) — o mesmo JSON que
    `buscar_wallapop()` já sabe processar. Isto dá-nos o melhor dos dois
    mundos: passamos pela proteção anti-bot como um browser real, mas
    continuamos a receber dados estruturados e fiáveis, não HTML frágil.
    """
    if not PLAYWRIGHT_DISPONIVEL:
        logger.warning(
            "O pacote 'playwright' não está instalado — a saltar o fallback de "
            "browser real para a Wallapop. Instala com: pip install playwright"
        )
        return []
    if not _garantir_browser_playwright_instalado():
        logger.warning("Browser do Playwright indisponível — a saltar este fallback para a Wallapop.")
        return []

    url_pesquisa = (
        f"https://www.wallapop.com/search?keywords={quote_plus(termo)}"
        if termo
        else "https://www.wallapop.com/"
    )

    capturado: Dict[str, Any] = {}
    listings_dom: List[Dict[str, Any]] = []

    def _ao_receber_resposta(resposta) -> None:
        try:
            if "api.wallapop.com/api/v3/general/search" in resposta.url and resposta.status == 200:
                capturado["dados"] = resposta.json()
        except Exception:
            # A resposta pode não ser JSON, ou chegar tarde/incompleta —
            # nunca deixar isto rebentar o listener de rede do browser.
            pass

    try:
        with _playwright_lock:
            with sync_playwright() as p:
                navegador = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    opcoes_contexto = {
                        "user_agent": random.choice(USER_AGENTS),
                        "locale": "pt-PT",
                        "viewport": {"width": 1280, "height": 900},
                    }
                    if os.path.isfile(FACEBOOK_STORAGE_STATE):
                        opcoes_contexto["storage_state"] = FACEBOOK_STORAGE_STATE
                    contexto = navegador.new_context(**opcoes_contexto)
                    # Remove o sinal mais óbvio de automação que os sites
                    # costumam verificar (navigator.webdriver === true).
                    contexto.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                    pagina = contexto.new_page()
                    pagina.on("response", _ao_receber_resposta)
                    try:
                        pagina.goto(url_pesquisa, timeout=25000, wait_until="domcontentloaded")
                        pagina.wait_for_timeout(2000)
                        try:
                            pagina.wait_for_selector('a[href*="/item/"]', timeout=8000)
                        except Exception:
                            pagina.wait_for_timeout(1500)
                        listings_dom = _listings_wallapop_do_dom(pagina)
                    except Exception as e:
                        logger.warning(f"Erro ao navegar na Wallapop com o browser real: {e}")
                finally:
                    navegador.close()
    except Exception as e:
        logger.error(f"Erro inesperado ao usar o browser real (Playwright) para a Wallapop: {e}")
        return []

    oportunidades = []
    dados = capturado.get("dados")
    if dados:
        resultados = dados.get("search_objects") or dados.get("data", {}).get("section", {}) or []
        if isinstance(resultados, dict):
            resultados = resultados.get("payload", {}).get("items", [])
        oportunidades.extend(_construir_oportunidades_wallapop(resultados, regra))
    if listings_dom:
        for item in listings_dom:
            if not item.get("id"):
                continue
            
            # Parsing específico para preços da Wallapop
            preco_texto = item.get("preco", "0")
            try:
                # Remover símbolos de moeda e espaços
                preco_limpo = str(preco_texto).replace("€", "").replace("EUR", "").replace(".", "").replace(",", ".").strip()
                preco = float(preco_limpo) if preco_limpo else 0.0
            except (ValueError, TypeError):
                preco = 0.0
            
            # Tentar extrair data do DOM
            data_publicacao = item.get("data_publicacao", "")
            if data_publicacao:
                # Tentar converter texto relativo para data
                minutos = _minutos_de_texto_relativo(data_publicacao)
                if minutos is not None:
                    data_publicacao = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
            
            oportunidades.append(Oportunidade(
                id_artigo=str(item.get("id")),
                titulo=item.get("titulo") or "Sem título",
                preco=preco,
                moeda="EUR",
                url_anuncio=item.get("url") or "https://www.wallapop.com",
                url_imagem=item.get("url_imagem") or "",
                plataforma="wallapop",
                regra_id=regra["id"],
                regra_nome=regra["nome"],
                data_publicacao=data_publicacao if data_publicacao else None,
                texto_publicacao=data_publicacao if data_publicacao else None,
            ))
    vistos = set()
    oportunidades = [o for o in oportunidades if not (o.id_artigo in vistos or vistos.add(o.id_artigo))]
    if not oportunidades:
        logger.warning("A Wallapop não devolveu anúncios: a página pode exigir localização ou bloquear este IP.")
    logger.info(f"Browser real (Playwright) obteve {len(oportunidades)} resultado(s) da Wallapop para '{termo or '(sem termo)'}'.")
    return oportunidades


def buscar_wallapop(regra: Dict[str, Any]) -> List[Oportunidade]:
    """
    Consulta a API pública de pesquisa da Wallapop para uma dada regra.

    A Wallapop exige coordenadas geográficas (latitude/longitude) como
    referência de localização para devolver resultados relevantes.

    Suporta múltiplos termos de pesquisa separados por vírgula.
    """
    _aquecer_sessao("wallapop", "https://www.wallapop.com/")
    
    # Verificar cache primeiro
    termos = _termos_pesquisa(regra)
    cache_key = _gerar_chave_cache(regra, "wallapop", ",".join(termos))
    resultados_cache = _obter_cache(cache_key)
    if resultados_cache:
        logger.info(f"Cache HIT para Wallapop: {len(resultados_cache)} resultados")
        return [Oportunidade(**r) for r in resultados_cache]

    def _buscar_termo(termo: str) -> List[Oportunidade]:
        url = "https://api.wallapop.com/api/v3/general/search"
        params = {
            "filters_source": "search_box",
            "latitude": WALLAPOP_LATITUDE,
            "longitude": WALLAPOP_LONGITUDE,
            "max_sale_price": regra["preco_maximo"],
            "min_sale_price": regra.get("preco_minimo", 0),
            "order_by": "newest",
        }
        if termo:
            params["keywords"] = termo

        headers_wallapop = {
            "X-AppVersion": "73322",
            "X-DeviceOS": "2",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.wallapop.com",
            "X-Requested-With": "XMLHttpRequest",
        }

        dados = _pedido_seguro(url, params=params, custom_headers=headers_wallapop)
        oportunidades_api = []
        if dados:
            resultados = (dados.get("search_objects") or dados.get("data", {}).get("section", {}) or [])
            if isinstance(resultados, dict):
                resultados = resultados.get("payload", {}).get("items", [])
            oportunidades_api = _construir_oportunidades_wallapop(resultados, regra)
            if oportunidades_api:
                return oportunidades_api
        logger.info(
            f"Pedido direto à API da Wallapop falhou para '{termo or '(sem termo)'}'. "
            "A tentar com um browser real (Playwright)..."
        )
        resultados_browser = _buscar_wallapop_via_browser(termo, regra)
        if resultados_browser:
            return resultados_browser
        logger.info(
            "Browser real também não obteve resultados da Wallapop. "
            "A tentar o fallback simples de HTML..."
        )
        return _buscar_wallapop_html(termo, regra)

    resultados = _estender_paralelo(
        _buscar_termo, termos, max_workers=CONCORRENCIA_INICIAL["wallapop"]
    )
    
    # Guardar no cache
    _guardar_cache(cache_key, [o.to_dict() for o in resultados])
    return resultados


def _buscar_wallapop_html(termo: str, regra: Dict[str, Any]) -> List[Oportunidade]:
    """Fallback para a pesquisa web quando a API do Wallapop bloqueia o acesso."""
    pagina = _pedido_texto(f"https://pt.wallapop.com/search?keywords={quote_plus(termo)}")
    if not pagina:
        return []
    candidatos = []
    vistos = set()
    padrao = re.compile(
        r'href=["\']([^"\']*/item/[^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for encontrado in padrao.finditer(pagina):
        url_anuncio = urljoin("https://pt.wallapop.com", unescape(encontrado.group(1)))
        if url_anuncio in vistos:
            continue
        vistos.add(url_anuncio)
        trecho = re.sub(r"<[^>]+>", " ", encontrado.group(2))
        titulo = re.sub(r"\s+", " ", unescape(trecho)).strip() or "Sem título"
        preco_encontrado = re.search(r"(\d[\d\s.,]*)\s*€", pagina[encontrado.start() : encontrado.start() + 5000])
        preco = float(preco_encontrado.group(1).replace(".", "").replace(",", ".")) if preco_encontrado else 0
        candidatos.append((url_anuncio, titulo, preco))

    if not candidatos:
        return []

    # A página de resultados em HTML "cru" (sem JS) raramente traz a
    # imagem do anúncio no markup inicial (é carregada via JavaScript no
    # browser real). Por isso vamos sempre buscar a imagem à página
    # individual de cada anúncio (tag og:image), em paralelo.
    with ThreadPoolExecutor(max_workers=OLX_CONCORRENCIA_DATAS) as executor:
        imagens = list(executor.map(lambda c: _og_imagem_da_pagina(c[0]) or "", candidatos))

    resultados: List[Oportunidade] = []
    for (url_anuncio, titulo, preco), url_imagem in zip(candidatos, imagens):
        resultados.append(Oportunidade(
            id_artigo=url_anuncio.rstrip("/").rsplit("/", 1)[-1],
            titulo=titulo,
            preco=preco,
            moeda="EUR",
            url_anuncio=url_anuncio,
            url_imagem=url_imagem,
            plataforma="wallapop",
            regra_id=regra["id"],
            regra_nome=regra["nome"],
        ))
    return resultados


# --------------------------------------------------------------------------
# Scraper: Facebook Marketplace
# --------------------------------------------------------------------------
def _preco_facebook(valor: Any) -> float:
    """Normaliza o preço devolvido pelo GraphQL/HTML do Facebook Marketplace."""
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, dict):
        amount = valor.get("amount")
        if amount not in (None, ""):
            try:
                return float(str(amount).replace(",", "."))
            except (TypeError, ValueError):
                pass
        texto = (
            valor.get("formatted_amount")
            or valor.get("text")
            or valor.get("label")
            or ""
        )
        return _preco_facebook(texto)
    if isinstance(valor, str):
        limpo = valor.replace("€", "").replace("EUR", "").strip()
        encontrado = re.search(r"(\d[\d\s.,]*)", limpo)
        if not encontrado:
            return 0.0
        numero = encontrado.group(1).strip()
        if "," in numero and "." in numero:
            if numero.rfind(",") > numero.rfind("."):
                numero = numero.replace(".", "").replace(",", ".")
            else:
                numero = numero.replace(",", "")
        elif "," in numero:
            partes = numero.split(",")
            numero = numero.replace(",", ".") if len(partes[-1]) <= 2 else numero.replace(",", "")
        else:
            numero = numero.replace(" ", "")
        try:
            return float(numero.replace(" ", ""))
        except ValueError:
            return 0.0
    return 0.0


def _recolher_listings_facebook(obj: Any, encontrados: Dict[str, Dict[str, Any]]) -> None:
    """Percorre JSON GraphQL do Facebook e extrai anúncios do Marketplace."""
    if isinstance(obj, dict):
        titulo = obj.get("marketplace_listing_title") or obj.get("custom_title")
        listing_id = obj.get("marketplace_listing_id") or obj.get("listing_id") or obj.get("id")
        if titulo and listing_id:
            identificador = str(listing_id)
            if identificador not in encontrados:
                foto = obj.get("primary_listing_photo") or obj.get("primary_photo") or {}
                url_imagem = ""
                if isinstance(foto, dict):
                    imagem = foto.get("image") or foto.get("photo") or foto
                    if isinstance(imagem, dict):
                        url_imagem = imagem.get("uri") or imagem.get("url") or ""
                    elif isinstance(imagem, str):
                        url_imagem = imagem
                encontrados[identificador] = {
                    "id": identificador,
                    "titulo": titulo,
                    "preco": _preco_facebook(obj.get("listing_price") or obj.get("formatted_price")),
                    "url_imagem": url_imagem,
                    "url": f"https://www.facebook.com/marketplace/item/{identificador}",
                }
        for valor in obj.values():
            _recolher_listings_facebook(valor, encontrados)
    elif isinstance(obj, list):
        for item in obj:
            _recolher_listings_facebook(item, encontrados)


def _url_pesquisa_facebook(termo: str, regra: Dict[str, Any]) -> str:
    """Constrói o URL público de pesquisa do Facebook Marketplace."""
    minimo = float(regra.get("preco_minimo", 0) or 0)
    maximo = float(regra.get("preco_maximo", 0) or 0)
    params = []
    if termo:
        params.append(f"query={quote_plus(termo)}")
    if minimo > 0:
        params.append(f"minPrice={int(minimo)}")
    if maximo > 0:
        params.append(f"maxPrice={int(maximo)}")
    params.append("sortBy=creation_time_descend")
    params.append("exact=false")
    query = "&".join(params)
    base = f"https://www.facebook.com/marketplace/{FACEBOOK_MARKETPLACE_LOCAL}"
    if termo:
        return f"{base}/search?{query}"
    return f"{base}?{query}" if query else base


def _aceitar_cookies_facebook(pagina) -> None:
    """Tenta aceitar o banner de cookies do Facebook, se aparecer."""
    for texto in (
        "Permitir todos os cookies",
        "Allow all cookies",
        "Accept all",
        "Aceitar todos os cookies",
        "Allow essential and optional cookies",
    ):
        try:
            botao = pagina.get_by_role("button", name=re.compile(texto, re.I))
            if botao.count() > 0:
                botao.first.click(timeout=2500)
                pagina.wait_for_timeout(800)
                return
        except Exception:
            continue


def _listings_facebook_do_dom(pagina) -> List[Dict[str, Any]]:
    """Lê os cartões visíveis na página de resultados do Marketplace."""
    try:
        return pagina.evaluate(
            """() => {
                const vistos = new Set();
                const itens = [];
                for (const a of document.querySelectorAll('a[href*="/marketplace/item/"]')) {
                    const href = (a.href || '').split('?')[0];
                    const match = href.match(/marketplace\\/item\\/(\\d+)/);
                    if (!match || vistos.has(match[1])) continue;
                    vistos.add(match[1]);
                    const texto = (a.innerText || '').split('\\n').map(t => t.trim()).filter(Boolean);
                    const img = a.querySelector('img');
                    let preco = '';
                    let titulo = '';
                    let data_publicacao = '';
                    
                    // Tenta encontrar data de publicação
                    const card = a.closest('[class*="card"], article');
                    if (card) {
                        const dateElements = card.querySelectorAll('time, [class*="date"], [class*="time"]');
                        for (const dateEl of dateElements) {
                            const text = dateEl.textContent?.trim() || dateEl.getAttribute('datetime') || '';
                            if (text && text.length > 0) {
                                data_publicacao = text;
                                break;
                            }
                        }
                    }
                    
                    for (const linha of texto) {
                        if (!preco && /€|EUR|\\d/.test(linha) && /\\d/.test(linha) && linha.length < 24) {
                            preco = linha;
                            continue;
                        }
                        if (!titulo && linha.length > 2 && !/^€/.test(linha)) {
                            titulo = linha;
                        }
                    }
                    itens.push({
                        id: match[1],
                        titulo: titulo || 'Sem título',
                        preco,
                        url_imagem: img ? (img.src || '') : '',
                        url: href,
                        data_publicacao: data_publicacao
                    });
                }
                return itens;
            }"""
        ) or []
    except Exception as e:
        logger.debug(f"Não foi possível ler o DOM do Facebook Marketplace: {e}")
        return []


def _construir_oportunidades_facebook(
    listings: List[Dict[str, Any]], regra: Dict[str, Any]
) -> List[Oportunidade]:
    oportunidades: List[Oportunidade] = []
    vistos = set()
    minimo = float(regra.get("preco_minimo", 0) or 0)
    maximo = float(regra.get("preco_maximo", 0) or 0)
    excluidas = [p.strip().lower() for p in regra.get("palavras_excluidas", []) if p.strip()]
    for item in listings:
        try:
            identificador = str(item.get("id") or "")
            if not identificador or identificador in vistos:
                continue
            vistos.add(identificador)
            titulo = item.get("titulo") or "Sem título"
            preco = item.get("preco")
            preco = _preco_facebook(preco) if not isinstance(preco, (int, float)) else float(preco)
            
            # Se o preço for 0 ou muito baixo, verificar se é "Gratuito" ou similar
            if preco <= 1.0 and titulo.lower() in ["gratuito", "free", "oferta"]:
                preco = 0.0  # Mantém como gratuito
            
            if maximo > 0 and (preco < minimo or preco > maximo):
                continue
            titulo_lower = titulo.lower()
            if any(palavra in titulo_lower for palavra in excluidas):
                continue
            
            # Tentar extrair data do item
            data_publicacao = item.get("data_publicacao")
            if data_publicacao:
                minutos = _minutos_de_texto_relativo(data_publicacao)
                if minutos is not None:
                    data_publicacao = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
            
            oportunidades.append(Oportunidade(
                id_artigo=identificador,
                titulo=titulo,
                preco=preco,
                moeda="EUR",
                url_anuncio=item.get("url") or f"https://www.facebook.com/marketplace/item/{identificador}",
                url_imagem=item.get("url_imagem") or "",
                plataforma="facebook",
                regra_id=regra["id"],
                regra_nome=regra["nome"],
                data_publicacao=data_publicacao if data_publicacao else None,
                texto_publicacao=data_publicacao if data_publicacao else None,
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.debug(f"Item do Facebook Marketplace ignorado: {e}")
    return oportunidades


def _buscar_facebook_via_browser(termo: str, regra: Dict[str, Any]) -> List[Oportunidade]:
    """
    Pesquisa no Facebook Marketplace com um browser real (Playwright).

    O Marketplace não tem API pública documentada e o HTML sem JavaScript
    quase não traz anúncios. Interceptamos as respostas GraphQL e, em
    fallback, lemos os cartões já renderizados no DOM.
    """
    if not PLAYWRIGHT_DISPONIVEL:
        logger.warning(
            "O pacote 'playwright' não está instalado — a saltar a pesquisa "
            "no Facebook Marketplace. Instala com: pip install playwright"
        )
        return []
    if not _garantir_browser_playwright_instalado():
        logger.warning("Browser do Playwright indisponível — a saltar o Facebook Marketplace.")
        return []

    url_pesquisa = _url_pesquisa_facebook(termo, regra)
    capturados: Dict[str, Dict[str, Any]] = {}

    def _ao_receber_resposta(resposta) -> None:
        try:
            url = resposta.url or ""
            if resposta.status != 200:
                return
            if "graphql" not in url.lower() and "api/graphql" not in url.lower():
                return
            texto = resposta.text()
            for bloco in (texto or "").split("\n"):
                bloco = bloco.strip()
                if not bloco:
                    continue
                try:
                    dados = json.loads(bloco)
                except ValueError:
                    continue
                _recolher_listings_facebook(dados, capturados)
        except Exception:
            pass

    try:
        with _playwright_lock:
            with sync_playwright() as p:
                navegador = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    contexto = navegador.new_context(
                        user_agent=random.choice(USER_AGENTS),
                        locale="pt-PT",
                        viewport={"width": 1280, "height": 900},
                    )
                    contexto.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                    pagina = contexto.new_page()
                    pagina.on("response", _ao_receber_resposta)
                    pagina.goto(url_pesquisa, timeout=25000, wait_until="domcontentloaded")
                    _aceitar_cookies_facebook(pagina)
                    try:
                        pagina.wait_for_selector('a[href*="/marketplace/item/"]', timeout=8000)
                    except Exception:
                        pagina.wait_for_timeout(2000)
                    for _ in range(3):
                        pagina.mouse.wheel(0, 2800)
                        pagina.wait_for_timeout(700)
                    listings_dom = _listings_facebook_do_dom(pagina)
                    try:
                        contexto.storage_state(path=FACEBOOK_STORAGE_STATE)
                    except Exception as e:
                        logger.debug(f"Não foi possível guardar a sessão Facebook: {e}")
                finally:
                    navegador.close()
    except Exception as e:
        logger.error(f"Erro ao pesquisar no Facebook Marketplace com Playwright: {e}")
        return []

    listings = list(capturados.values())
    if listings_dom:
        ids_graphql = {str(item.get("id")) for item in listings}
        for item in listings_dom:
            if str(item.get("id")) not in ids_graphql:
                listings.append(item)

    oportunidades = _construir_oportunidades_facebook(listings, regra)
    if not oportunidades:
        logger.warning(
            "O Facebook Marketplace não devolveu anúncios visíveis. "
            "O site pode estar a pedir login ou a bloquear o browser automático."
        )
    else:
        logger.info(
            f"Facebook Marketplace: {len(oportunidades)} resultado(s) para "
            f"'{termo or '(sem termo)'}'."
        )
    return oportunidades


def buscar_facebook(regra: Dict[str, Any]) -> List[Oportunidade]:
    """Pesquisa anúncios no Facebook Marketplace para uma regra."""
    _aquecer_sessao("facebook", "https://www.facebook.com/marketplace/")
    return _estender_paralelo(
        lambda termo: _buscar_facebook_via_browser(termo, regra),
        _termos_pesquisa(regra),
        max_workers=1,
    )


# --------------------------------------------------------------------------
# Filtragem (preço + palavras excluídas + semelhança visual CLIP)
# --------------------------------------------------------------------------
def _passa_filtros_basicos(oportunidade: Oportunidade, regra: Dict[str, Any]) -> bool:
    """Filtro rápido de preço e palavras excluídas, sem CLIP."""
    if oportunidade.preco < float(regra.get("preco_minimo", 0) or 0):
        return False
    if oportunidade.preco > float(regra["preco_maximo"]):
        return False
    titulo_lower = oportunidade.titulo.lower()
    for palavra in regra.get("palavras_excluidas", []):
        if palavra.strip().lower() in titulo_lower:
            logger.debug(
                f"Oportunidade '{oportunidade.titulo}' excluída pela palavra '{palavra}'."
            )
            return False
    return True


def _passa_filtros(oportunidade: Oportunidade, regra: Dict[str, Any]) -> bool:
    """Aplica os filtros de preço máximo, palavras excluídas e semelhança visual CLIP."""
    if not _passa_filtros_basicos(oportunidade, regra):
        return False

    # Filtro visual CLIP: compara a foto do anúncio com QUALQUER imagem de referência
    if _usa_filtro_imagem(regra):
        refs = _assinaturas_clip_da_referencia(regra)
        if not refs:
            logger.warning(
                f"Regra '{regra.get('nome')}' tem imagens de referência inválidas ou CLIP não disponível; "
                "o filtro visual foi ignorado neste ciclo."
            )
            if regra.get("tipo_pesquisa") == "imagem" or not (regra.get("termo_pesquisa") or "").strip():
                return False
        elif not oportunidade.url_imagem:
            logger.warning(f"Oportunidade '{oportunidade.titulo}' sem foto — excluída pelo filtro visual CLIP.")
            return False
        else:
            # Para filtragem síncrona, usamos a função assíncrona num event loop
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                sig = loop.run_until_complete(_assinatura_clip_da_url_async(oportunidade.url_imagem))
                loop.close()
            except Exception as e:
                logger.warning(f"Erro ao obter embedding CLIP: {e}")
                sig = None
            
            if sig is None:
                logger.warning(
                    f"Não foi possível analisar a foto de '{oportunidade.titulo}' "
                    f"({oportunidade.url_imagem}) — excluída pelo filtro visual CLIP "
                    "(falha ao descarregar ou processar a imagem)."
                )
                return False
            else:
                # Calcular similaridade com todas as referências e usar a máxima
                score = max(_calcular_similaridade_clip(sig, ref) for ref in refs)
                oportunidade.score_similaridade = vs.percentage_from_threshold(score)
                oportunidade.metodo_similaridade = "clip"
                # Converter threshold de percentagem (0-100) para valor CLIP (0.0-1.0)
                minimo_percentagem = int(regra.get("similaridade_minima", cm.SIMILARIDADE_PADRAO) or 0)
                minimo = vs.threshold_from_percentage(minimo_percentagem)
                
                if score < minimo:
                    logger.info(
                        f"Oportunidade '{oportunidade.titulo}' excluída por semelhança visual CLIP "
                        f"({vs.percentage_from_threshold(score):.1f}% < {minimo_percentagem}%)."
                    )
                    return False
                else:
                    logger.info(
                        f"Oportunidade '{oportunidade.titulo}' passou no filtro visual CLIP "
                        f"({vs.percentage_from_threshold(score):.1f}% >= {minimo_percentagem}%)."
                    )

    return True


async def _filtrar_por_similaridade_clip_async(
    oportunidades: List[Oportunidade],
    regra: Dict[str, Any],
    max_concurrent: int = 10
) -> List[Oportunidade]:
    """
    Filtra oportunidades por similaridade visual CLIP de forma assíncrona.
    
    Args:
        oportunidades: Lista de oportunidades a filtrar
        regra: Regra com imagens de referência e threshold
        max_concurrent: Número máximo de downloads simultâneos
    
    Returns:
        Lista de oportunidades que passaram no filtro visual
    """
    if not _usa_filtro_imagem(regra):
        return oportunidades
    
    refs = _assinaturas_clip_da_referencia(regra)
    if not refs:
        logger.warning(f"Regra '{regra.get('nome')}' sem imagens de referência válidas para CLIP")
        return []
    
    # Extrair URLs das imagens
    urls = [op.url_imagem for op in oportunidades if op.url_imagem]
    
    if not urls:
        logger.warning("Nenhuma imagem válida para processar com CLIP")
        return []
    
    # Codificar imagens em paralelo
    logger.info(f"A processar {len(urls)} imagens com CLIP (max_concurrent={max_concurrent})...")
    embeddings = await vs.batch_encode_images_async(
        urls,
        max_concurrent=max_concurrent,
        timeout=vs.DEFAULT_TIMEOUT,
        max_retries=3
    )
    
    # Filtrar por similaridade
    minimo_percentagem = int(regra.get("similaridade_minima", cm.SIMILARIDADE_PADRAO) or 0)
    minimo = vs.threshold_from_percentage(minimo_percentagem)
    
    aprovadas = []
    for oportunidade, embedding in zip(oportunidades, embeddings):
        if embedding is None:
            logger.warning(f"Imagem falhou: {oportunidade.titulo}")
            continue
        
        score = vs.compute_similarity_with_reference(embedding, refs)
        
        if score >= minimo:
            # Adicionar score ao objeto oportunidade
            oportunidade.score_similaridade = vs.percentage_from_threshold(score)
            oportunidade.metodo_similaridade = "clip"
            aprovadas.append(oportunidade)
            logger.info(
                f"✓ '{oportunidade.titulo}' passou (score: {vs.percentage_from_threshold(score):.1f}%)"
            )
        else:
            logger.info(
                f"✗ '{oportunidade.titulo}' excluído (score: {vs.percentage_from_threshold(score):.1f}% < {minimo_percentagem}%)"
            )
    
    logger.info(f"Filtro CLIP: {len(aprovadas)}/{len(oportunidades)} oportunidades aprovadas")
    return aprovadas


# --------------------------------------------------------------------------
# Função principal do ciclo de busca
# --------------------------------------------------------------------------
def executar_ciclo_busca(
    caminho_config: str = "config.json", caminho_vistos: str = VISTOS_FICHEIRO
) -> List[Dict[str, Any]]:
    """
    Executa um ciclo completo de busca:

        1. Lê as regras ativas do config.json
        2. Para cada regra, pesquisa na(s) plataforma(s) indicada(s)
        3. Filtra por preço máximo e palavras excluídas
        4. Remove duplicados já notificados anteriormente
        5. Atualiza o registo de "vistos" em disco
        6. Devolve apenas as oportunidades NOVAS (como lista de dicts)

    Esta função é o ponto de entrada a ser chamado periodicamente pela
    interface Streamlit (Etapa 4) ou por um agendador (scheduler).

    Args:
        caminho_config: caminho para o ficheiro de regras (config.json).
        caminho_vistos: caminho para o ficheiro de IDs já notificados
            (permite isolar testes do registo de produção).
    """
    try:
        regras_ativas = cm.listar_regras(apenas_ativas=True, caminho=caminho_config)
    except cm.ConfigManagerError as e:
        logger.error(f"Não foi possível carregar as regras: {e}")
        return []

    if not regras_ativas:
        logger.info("Nenhuma regra ativa encontrada. Nada a pesquisar.")
        return []

    # Limpar cache expirado periodicamente
    _limpar_cache_expirado()

    vistos = _carregar_vistos(caminho_vistos)
    novas_oportunidades: List[Oportunidade] = []

    for regra in regras_ativas:
        plataforma = regra.get("plataforma", "ambas")
        logger.info(f"A pesquisar '{regra['nome']}' (plataforma: {plataforma})...")

        alvos = _plataformas_da_regra(regra)
        funcoes_plataforma = {
            "vinted": buscar_vinted,
            "wallapop": buscar_wallapop,
            "olx": buscar_olx,
            "facebook": buscar_facebook,
        }

        def pesquisar_plataforma(nome: str) -> List[Oportunidade]:
            inicio = time.monotonic()
            try:
                resultados = funcoes_plataforma[nome](regra)
                logger.info(
                    "Plataforma %s: %d resultado(s) em %.1fs.",
                    nome,
                    len(resultados),
                    time.monotonic() - inicio,
                )
                return resultados
            except Exception as erro:
                logger.error("Erro na plataforma %s: %s", nome, erro)
                return []

        resultados_regra = []
        with ThreadPoolExecutor(
            max_workers=min(MAX_PLATAFORMAS_PARALELAS, len(alvos))
        ) as executor:
            futuros = [executor.submit(pesquisar_plataforma, nome) for nome in alvos]
            for futuro in as_completed(futuros):
                resultados_regra.extend(futuro.result())

        for oportunidade in resultados_regra:
            if not _passa_filtros(oportunidade, regra):
                continue

            chave = oportunidade.chave_unica()
            if chave in vistos:
                continue

            vistos.add(chave)
            novas_oportunidades.append(oportunidade)

        logger.info(
            f"Regra '{regra['nome']}': {len(resultados_regra)} resultado(s) bruto(s), "
            f"{sum(1 for o in novas_oportunidades if o.regra_id == regra['id'])} nova(s) "
            "oportunidade(s) após filtros."
        )

    _guardar_vistos(vistos, caminho_vistos)

    logger.info(f"Ciclo de busca concluído: {len(novas_oportunidades)} nova(s) oportunidade(s).")
    return [o.to_dict() for o in novas_oportunidades]


async def executar_ciclo_busca_streaming(
    caminho_config: str = "config.json", 
    caminho_vistos: str = VISTOS_FICHEIRO
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Versão assíncrona com streaming de resultados.
    
    Retorna oportunidades assim que chegam de cada marketplace, sem esperar
    que todos terminem. Permite resposta mais rápida ao frontend.
    
    Args:
        caminho_config: caminho para o ficheiro de regras (config.json).
        caminho_vistos: caminho para o ficheiro de IDs já notificados.
    
    Yields:
        Dicionários de oportunidades assim que ficam disponíveis.
    """
    try:
        regras_ativas = cm.listar_regras(apenas_ativas=True, caminho=caminho_config)
    except cm.ConfigManagerError as e:
        logger.error(f"Não foi possível carregar as regras: {e}")
        return

    if not regras_ativas:
        logger.info("Nenhuma regra ativa encontrada. Nada a pesquisar.")
        return

    _limpar_cache_expirado()
    vistos = _carregar_vistos(caminho_vistos)
    vistos_lock = asyncio.Lock()

    async def pesquisar_plataforma_async(nome: str, regra: Dict[str, Any]) -> List[Oportunidade]:
        """Wrapper assíncrono para pesquisa de plataforma."""
        loop = asyncio.get_event_loop()
        funcoes_plataforma = {
            "vinted": buscar_vinted,
            "wallapop": buscar_wallapop,
            "olx": buscar_olx,
            "facebook": buscar_facebook,
        }
        
        inicio = time.monotonic()
        try:
            # Executar função síncrona em thread pool
            resultados = await loop.run_in_executor(
                None, lambda: funcoes_plataforma[nome](regra)
            )
            logger.info(
                "Plataforma %s: %d resultado(s) em %.1fs.",
                nome,
                len(resultados),
                time.monotonic() - inicio,
            )
            return resultados
        except Exception as erro:
            logger.error("Erro na plataforma %s: %s", nome, erro)
            return []

    for regra in regras_ativas:
        plataforma = regra.get("plataforma", "ambas")
        logger.info(f"A pesquisar '{regra['nome']}' (plataforma: {plataforma})...")

        alvos = _plataformas_da_regra(regra)
        
        # Executar todas as plataformas em paralelo com asyncio
        tarefas = [
            pesquisar_plataforma_async(nome, regra) 
            for nome in alvos
        ]
        
        # Processar resultados assim que chegam
        for tarefa in asyncio.as_completed(tarefas):
            resultados_regra = await tarefa
            
            for oportunidade in resultados_regra:
                if not _passa_filtros(oportunidade, regra):
                    continue

                chave = oportunidade.chave_unica()
                async with vistos_lock:
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                
                # Yield imediato para streaming
                yield oportunidade.to_dict()

    _guardar_vistos(vistos, caminho_vistos)
    logger.info("Ciclo de busca streaming concluído.")


def executar_ciclo_busca_com_stats(
    caminho_config: str = "config.json", caminho_vistos: str = VISTOS_FICHEIRO
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Versão que também retorna estatísticas de performance.
    
    Args:
        caminho_config: caminho para o ficheiro de regras (config.json).
        caminho_vistos: caminho para o ficheiro de IDs já notificados.
    
    Returns:
        Tupla com (lista de oportunidades, dicionário de estatísticas).
    """
    inicio_total = time.monotonic()
    
    oportunidades = executar_ciclo_busca(caminho_config, caminho_vistos)
    
    # Coletar estatísticas dos limitadores
    stats = {
        "tempo_total_segundos": time.monotonic() - inicio_total,
        "total_oportunidades": len(oportunidades),
        "cache_stats": {
            "tamanho": len(_cache_pesquisas),
            "tamanho_max": CACHE_MAX_SIZE,
            "ttl_segundos": CACHE_TTL_SEGUNDOS,
        },
        "rate_limiters": {}
    }
    
    for plataforma, limitador in _LIMITADORES.items():
        stats["rate_limiters"][plataforma] = limitador.obter_estatisticas()
    
    return oportunidades, stats


# --------------------------------------------------------------------------
# Bloco de testes práticos (executar com: python scraper_engine.py)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    TESTE_CONFIG = "config_teste.json"
    TESTE_VISTOS = "vistos_teste.json"

    # Reaproveita o config_teste.json gerado na Etapa 1, se existir.
    if not os.path.exists(TESTE_CONFIG):
        print(
            f"O ficheiro '{TESTE_CONFIG}' não foi encontrado. "
            "A criar um exemplo mínimo para o teste..."
        )
        cm.criar_regra(
            nome="PS5",
            termo_pesquisa="playstation 5",
            preco_maximo=250.0,
            palavras_excluidas=["avariada", "sem comando"],
            mensagem_proposta="Boa tarde, ainda tem a PS5 disponível?",
            ativo=True,
            plataforma="vinted",
            caminho=TESTE_CONFIG,
        )

    # Isolamos o ficheiro de "vistos" para não misturar com execuções reais
    if os.path.exists(TESTE_VISTOS):
        os.remove(TESTE_VISTOS)

    print("\n=== TESTE 1: Regras ativas carregadas do config_teste.json ===")
    regras = cm.listar_regras(apenas_ativas=True, caminho=TESTE_CONFIG)
    for r in regras:
        print(f" - {r['nome']} | plataforma={r['plataforma']} | max={r['preco_maximo']}€")

    print("\n=== TESTE 2: Testar filtros localmente (sem rede) ===")
    regra_exemplo = {
        "id": "regra-teste",
        "nome": "Exemplo",
        "preco_maximo": 100.0,
        "palavras_excluidas": ["avariado", "capa"],
    }
    op_valida = Oportunidade(
        id_artigo="1", titulo="PS5 em bom estado", preco=90.0, moeda="EUR",
        url_anuncio="https://exemplo.com/1", url_imagem="", plataforma="vinted",
        regra_id="regra-teste", regra_nome="Exemplo",
    )
    op_cara = Oportunidade(
        id_artigo="2", titulo="PS5 nova", preco=150.0, moeda="EUR",
        url_anuncio="https://exemplo.com/2", url_imagem="", plataforma="vinted",
        regra_id="regra-teste", regra_nome="Exemplo",
    )
    op_excluida = Oportunidade(
        id_artigo="3", titulo="PS5 avariado, só peças", preco=50.0, moeda="EUR",
        url_anuncio="https://exemplo.com/3", url_imagem="", plataforma="vinted",
        regra_id="regra-teste", regra_nome="Exemplo",
    )
    print(f"Oportunidade dentro do preço e sem palavras excluídas -> passa filtros? "
          f"{_passa_filtros(op_valida, regra_exemplo)} (esperado: True)")
    print(f"Oportunidade acima do preço máximo -> passa filtros? "
          f"{_passa_filtros(op_cara, regra_exemplo)} (esperado: False)")
    print(f"Oportunidade com palavra excluída -> passa filtros? "
          f"{_passa_filtros(op_excluida, regra_exemplo)} (esperado: False)")

    print("\n=== TESTE 3: Testar deduplicação (anti-duplicados) ===")
    vistos_teste = set()
    vistos_teste.add(op_valida.chave_unica())
    print(f"Chave já vista antes de adicionar de novo: {op_valida.chave_unica() in vistos_teste}")

    print("\n=== TESTE 4: Ciclo de busca real com estatísticas (requer ligação à internet) ===")
    print("A contactar Vinted e/ou Wallapop... isto pode demorar alguns segundos.")
    try:
        novas, stats = executar_ciclo_busca_com_stats(caminho_config=TESTE_CONFIG, caminho_vistos=TESTE_VISTOS)
        
        print(f"\n[ESTATISTICAS DE PERFORMANCE]")
        print(f"Tempo total: {stats['tempo_total_segundos']:.2f}s")
        print(f"Total oportunidades: {stats['total_oportunidades']}")
        print(f"Cache: {stats['cache_stats']['tamanho']}/{stats['cache_stats']['tamanho_max']} entradas")
        print(f"Cache TTL: {stats['cache_stats']['ttl_segundos']}s")
        
        print(f"\n[RATE LIMITERS]")
        for plataforma, stats_plataforma in stats['rate_limiters'].items():
            print(f"  {plataforma.upper()}:")
            print(f"    Concorrencia: {stats_plataforma['concorrencia_atual']}/{stats_plataforma['concorrencia_max']}")
            print(f"    Taxa sucesso: {stats_plataforma['taxa_sucesso']:.1%}")
            print(f"    Total pedidos: {stats_plataforma['total_pedidos']}")
        
        if novas:
            print(f"\n{len(novas)} nova(s) oportunidade(s) encontrada(s):\n")
            for op in novas[:5]:
                print(f" - [{op['plataforma']}] {op['titulo']} — {op['preco']} {op['moeda']}")
                print(f"   {op['url_anuncio']}")
        else:
            print(
                "\nNenhuma oportunidade nova encontrada. Isto pode significar:\n"
                " - As plataformas bloquearam o pedido (403/429) — ver logs acima;\n"
                " - Não há resultados reais para o termo pesquisado;\n"
                " - A estrutura da API mudou e o parsing precisa de ajuste.\n"
            )
    except Exception as e:
        print(f"\nErro ao executar o ciclo de busca real: {e}")
        print("Isto é esperado se não houver ligação à internet neste ambiente de teste.")

    print("\n=== TESTE 5: Teste de cache ===")
    print("A executar segunda pesquisa para testar cache...")
    try:
        novas2, stats2 = executar_ciclo_busca_com_stats(caminho_config=TESTE_CONFIG, caminho_vistos=TESTE_VISTOS)
        print(f"Segunda execução: {stats2['tempo_total_segundos']:.2f}s (deve ser mais rápido devido ao cache)")
        print(f"Cache após segunda execução: {stats2['cache_stats']['tamanho']} entradas")
    except Exception as e:
        print(f"Erro no teste de cache: {e}")

    print("\nTestes concluídos.")