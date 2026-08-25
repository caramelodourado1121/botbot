"""
notifier.py
============

Módulo responsável pelas notificações em tempo real (Telegram Bot API) e
pela geração de textos de proposta / links de partilha rápida para as
oportunidades encontradas pelo scraper_engine.py.

Responsabilidades:
    1. Ler/gravar credenciais do Telegram (bot_token, chat_id) a partir de
       um ficheiro local de definições ou de variáveis de ambiente.
    2. Validar se as credenciais funcionam (teste de ligação ao bot).
    3. Enviar notificações formatadas (foto + texto + botão) para o Telegram
       sempre que uma nova oportunidade é encontrada.
    4. Gerar o texto de proposta a enviar ao vendedor, com suporte a
       desconto percentual configurável.
    5. Gerar links rápidos de partilha (WhatsApp / Telegram) para a UI.

Robustez: nenhuma função deste módulo lança exceções para fora — todos os
erros de rede, credenciais inválidas ou dados em falta são registados no
log e devolvidos como resultado (True/False ou dict), nunca interrompendo
o programa principal.

Autor: Desenvolvimento modular - Etapa 3/5
"""

from __future__ import annotations

import json
import os
import logging
from typing import Optional, Dict, Any, Tuple
from urllib.parse import quote_plus

import requests

# --------------------------------------------------------------------------
# Configuração de logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("notifier")

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------
SETTINGS_FICHEIRO = "settings.json"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{metodo}"
TIMEOUT_REQUEST = 10  # segundos

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"


# --------------------------------------------------------------------------
# Gestão de credenciais (ficheiro local + variáveis de ambiente)
# --------------------------------------------------------------------------
def guardar_credenciais(
    bot_token: str, chat_id: str, caminho: str = SETTINGS_FICHEIRO
) -> bool:
    """
    Guarda o bot_token e o chat_id no ficheiro de definições local.

    As variáveis de ambiente (se definidas) têm sempre prioridade sobre
    este ficheiro em tempo de execução — ver `_obter_credenciais()`.

    Retorna True se guardado com sucesso, False caso contrário.
    """
    if not bot_token or not chat_id:
        logger.error("bot_token e chat_id não podem ser vazios.")
        return False

    try:
        dados = {}
        if os.path.exists(caminho):
            try:
                with open(caminho, "r", encoding="utf-8") as f:
                    dados = json.load(f)
            except (json.JSONDecodeError, OSError):
                dados = {}

        dados["telegram_bot_token"] = bot_token.strip()
        dados["telegram_chat_id"] = str(chat_id).strip()

        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=4)

        logger.info(f"Credenciais do Telegram guardadas em '{caminho}'.")
        return True
    except OSError as e:
        logger.error(f"Erro ao guardar credenciais em '{caminho}': {e}")
        return False


def _obter_credenciais(
    caminho: str = SETTINGS_FICHEIRO,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Devolve (bot_token, chat_id).

    Prioridade: variáveis de ambiente > ficheiro settings.json.
    Devolve (None, None) se nada for encontrado.
    """
    bot_token = os.environ.get(ENV_BOT_TOKEN)
    chat_id = os.environ.get(ENV_CHAT_ID)

    if bot_token and chat_id:
        return bot_token.strip(), str(chat_id).strip()

    if os.path.exists(caminho):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                dados = json.load(f)
                bot_token = bot_token or dados.get("telegram_bot_token")
                chat_id = chat_id or dados.get("telegram_chat_id")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Não foi possível ler '{caminho}': {e}")

    if bot_token:
        bot_token = bot_token.strip()
    if chat_id:
        chat_id = str(chat_id).strip()

    return bot_token, chat_id


def credenciais_configuradas(caminho: str = SETTINGS_FICHEIRO) -> bool:
    """Indica rapidamente se existem credenciais disponíveis (sem as validar)."""
    bot_token, chat_id = _obter_credenciais(caminho)
    return bool(bot_token and chat_id)


# --------------------------------------------------------------------------
# Validação / teste de ligação ao Telegram
# --------------------------------------------------------------------------
def testar_credenciais_telegram(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    caminho: str = SETTINGS_FICHEIRO,
) -> Dict[str, Any]:
    """
    Testa se o bot_token é válido e se é possível enviar uma mensagem
    de teste para o chat_id indicado.

    Se bot_token/chat_id não forem passados diretamente, são lidos via
    `_obter_credenciais()` (ambiente ou ficheiro local).

    Retorna sempre um dicionário, nunca lança exceção:
        {"sucesso": bool, "mensagem": str}
    """
    if not bot_token or not chat_id:
        bot_token_lido, chat_id_lido = _obter_credenciais(caminho)
        bot_token = bot_token or bot_token_lido
        chat_id = chat_id or chat_id_lido

    if not bot_token or not chat_id:
        return {
            "sucesso": False,
            "mensagem": "bot_token e/ou chat_id não configurados. "
            "Define-os no painel ou nas variáveis de ambiente "
            f"'{ENV_BOT_TOKEN}' e '{ENV_CHAT_ID}'.",
        }

    # Passo 1: validar se o token do bot é válido (getMe)
    try:
        url_getme = TELEGRAM_API_BASE.format(token=bot_token, metodo="getMe")
        resposta = requests.get(url_getme, timeout=TIMEOUT_REQUEST)
        corpo = resposta.json()

        if not resposta.ok or not corpo.get("ok"):
            return {
                "sucesso": False,
                "mensagem": f"Token do bot inválido ou rejeitado pelo Telegram: "
                f"{corpo.get('description', 'erro desconhecido')}",
            }

        nome_bot = corpo.get("result", {}).get("username", "desconhecido")
    except requests.exceptions.Timeout:
        return {"sucesso": False, "mensagem": "Timeout ao contactar a API do Telegram."}
    except requests.exceptions.ConnectionError:
        return {
            "sucesso": False,
            "mensagem": "Sem ligação à internet ou API do Telegram inacessível.",
        }
    except (requests.exceptions.RequestException, ValueError) as e:
        return {"sucesso": False, "mensagem": f"Erro inesperado ao validar o token: {e}"}

    # Passo 2: tentar enviar uma mensagem de teste real para o chat_id
    sucesso_envio = enviar_mensagem_telegram(
        "✅ Ligação estabelecida com sucesso! Este é um alerta de teste do "
        "teu monitor de oportunidades Vinted/Wallapop.",
        bot_token=bot_token,
        chat_id=chat_id,
    )

    if sucesso_envio:
        return {
            "sucesso": True,
            "mensagem": f"Ligação validada com sucesso ao bot @{nome_bot}. "
            "Mensagem de teste enviada.",
        }

    return {
        "sucesso": False,
        "mensagem": f"O bot @{nome_bot} é válido, mas não foi possível enviar "
        "a mensagem de teste para o chat_id indicado. Verifica se o chat_id "
        "está correto e se já iniciaste uma conversa com o bot.",
    }


# --------------------------------------------------------------------------
# Envio de mensagens / fotos para o Telegram
# --------------------------------------------------------------------------
def enviar_mensagem_telegram(
    texto: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    parse_mode: str = "Markdown",
    botao_texto: Optional[str] = None,
    botao_url: Optional[str] = None,
) -> bool:
    """
    Envia uma mensagem de texto simples para o Telegram, com botão opcional.

    Nunca lança exceção: em caso de falha regista o erro no log e
    devolve False.
    """
    bot_token, chat_id = _resolver_credenciais(bot_token, chat_id)
    if not bot_token or not chat_id:
        logger.error("Não é possível enviar mensagem: credenciais do Telegram em falta.")
        return False

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }

    if botao_texto and botao_url:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": botao_texto, "url": botao_url}]]}
        )

    url = TELEGRAM_API_BASE.format(token=bot_token, metodo="sendMessage")
    return _executar_pedido_telegram(url, payload)


def enviar_foto_telegram(
    url_imagem: str,
    legenda: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    parse_mode: str = "Markdown",
    botao_texto: Optional[str] = None,
    botao_url: Optional[str] = None,
) -> bool:
    """
    Envia uma foto com legenda para o Telegram, com botão opcional.

    Se o envio da foto falhar (ex: URL de imagem inválida ou inacessível
    pelo Telegram), tenta automaticamente enviar apenas a mensagem de
    texto como alternativa (fallback), para garantir que o utilizador
    é sempre notificado.
    """
    bot_token, chat_id = _resolver_credenciais(bot_token, chat_id)
    if not bot_token or not chat_id:
        logger.error("Não é possível enviar foto: credenciais do Telegram em falta.")
        return False

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "photo": url_imagem,
        "caption": legenda,
        "parse_mode": parse_mode,
    }

    if botao_texto and botao_url:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": botao_texto, "url": botao_url}]]}
        )

    url = TELEGRAM_API_BASE.format(token=bot_token, metodo="sendPhoto")
    sucesso = _executar_pedido_telegram(url, payload)

    if not sucesso:
        logger.warning("Envio de foto falhou. A tentar enviar apenas texto como alternativa.")
        return enviar_mensagem_telegram(
            legenda,
            bot_token=bot_token,
            chat_id=chat_id,
            parse_mode=parse_mode,
            botao_texto=botao_texto,
            botao_url=botao_url,
        )

    return True


def _resolver_credenciais(
    bot_token: Optional[str], chat_id: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Preenche bot_token/chat_id em falta a partir do ambiente/ficheiro local."""
    if bot_token and chat_id:
        return bot_token, chat_id
    bot_token_lido, chat_id_lido = _obter_credenciais()
    return bot_token or bot_token_lido, chat_id or chat_id_lido


def _executar_pedido_telegram(url: str, payload: Dict[str, Any]) -> bool:
    """
    Executa o POST à API do Telegram com tratamento de erros robusto.

    Retorna True se o Telegram confirmou o envio (campo "ok": true),
    False em qualquer outro caso — nunca lança exceção.
    """
    try:
        resposta = requests.post(url, data=payload, timeout=TIMEOUT_REQUEST)
        corpo = resposta.json()

        if resposta.ok and corpo.get("ok"):
            return True

        descricao = corpo.get("description", "erro desconhecido")
        logger.error(f"Telegram recusou o pedido ({resposta.status_code}): {descricao}")
        return False

    except requests.exceptions.Timeout:
        logger.error("Timeout ao contactar a API do Telegram. A oportunidade não foi perdida, "
                     "mas a notificação não foi enviada.")
    except requests.exceptions.ConnectionError:
        logger.error("Sem ligação à internet. Não foi possível notificar via Telegram.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro inesperado ao contactar o Telegram: {e}")
    except ValueError:
        logger.error("Resposta do Telegram não é JSON válido.")

    return False


# --------------------------------------------------------------------------
# Geração do texto de proposta
# --------------------------------------------------------------------------
def gerar_texto_proposta(
    oportunidade: Dict[str, Any],
    regra: Dict[str, Any],
    percentagem_desconto: float = 0,
) -> str:
    """
    Gera o texto final de proposta a enviar ao vendedor, a partir do
    modelo definido em `regra["mensagem_proposta"]`.

    Placeholders suportados no modelo (substituição segura, não usa
    str.format() para evitar erros com chavetas inesperadas no texto):
        {preco}           -> preço final proposto (com desconto aplicado, se houver)
        {preco_original}  -> preço original do anúncio
        {titulo}          -> título do anúncio
        {desconto}        -> percentagem de desconto aplicada

    Se `mensagem_proposta` estiver vazio, é gerado um texto genérico
    de fallback para que nunca seja devolvida uma string vazia.
    """
    preco_original = float(oportunidade.get("preco", 0))

    if percentagem_desconto and percentagem_desconto > 0:
        preco_final = round(preco_original * (1 - percentagem_desconto / 100), 2)
    else:
        preco_final = preco_original

    modelo = (regra.get("mensagem_proposta") or "").strip()
    if not modelo:
        modelo = (
            "Olá! Tenho interesse no artigo \"{titulo}\". "
            "Estaria disponível a aceitar {preco}€?"
        )

    substituicoes = {
        "{preco}": f"{preco_final:.2f}",
        "{preco_original}": f"{preco_original:.2f}",
        "{titulo}": oportunidade.get("titulo", ""),
        "{desconto}": f"{percentagem_desconto:.0f}" if percentagem_desconto else "0",
    }

    texto_final = modelo
    for placeholder, valor in substituicoes.items():
        texto_final = texto_final.replace(placeholder, valor)

    return texto_final


# --------------------------------------------------------------------------
# Formatação da notificação completa de uma oportunidade
# --------------------------------------------------------------------------
def _formatar_legenda_oportunidade(
    oportunidade: Dict[str, Any],
    regra: Dict[str, Any],
    percentagem_desconto: float = 0,
) -> str:
    """Monta o texto formatado (Markdown) da notificação de uma oportunidade."""
    labels = {
        "vinted": "Vinted",
        "wallapop": "Wallapop",
        "olx": "OLX",
        "facebook": "Facebook Marketplace",
    }
    plataforma_raw = oportunidade.get("plataforma", "")
    plataforma_label = labels.get(plataforma_raw, str(plataforma_raw).capitalize())
    titulo = oportunidade.get("titulo", "Sem título")
    preco = oportunidade.get("preco", 0)
    moeda = oportunidade.get("moeda", "EUR")
    nome_regra = oportunidade.get("regra_nome") or regra.get("nome", "")

    texto_proposta = gerar_texto_proposta(oportunidade, regra, percentagem_desconto)

    linhas = [
        f"🔔 *Acabou de ser publicado!*",
        "",
        f"*{titulo}*",
        f"💰 {preco:.2f} {moeda}  |  🛍️ {plataforma_label}",
        f"📌 Regra: {nome_regra}",
        "",
        "✉️ *Proposta sugerida:*",
        f"_{texto_proposta}_",
    ]
    return "\n".join(linhas)


def notificar_oportunidade(
    oportunidade: Dict[str, Any],
    regra: Dict[str, Any],
    percentagem_desconto: float = 0,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """
    Envia a notificação completa de uma nova oportunidade para o Telegram:
    foto (se disponível), título, preço, plataforma, nome da regra,
    proposta sugerida e um botão com link direto para o anúncio.

    Esta é a função principal a ser chamada pelo motor de busca / painel
    Streamlit sempre que uma nova oportunidade é encontrada.

    Nunca lança exceção — em caso de falha, regista o erro e devolve False,
    permitindo que o ciclo de monitorização continue sem interrupções.
    """
    try:
        legenda = _formatar_legenda_oportunidade(oportunidade, regra, percentagem_desconto)
        url_anuncio = oportunidade.get("url_anuncio", "")
        url_imagem = oportunidade.get("url_imagem", "")

        if url_imagem:
            return enviar_foto_telegram(
                url_imagem=url_imagem,
                legenda=legenda,
                bot_token=bot_token,
                chat_id=chat_id,
                botao_texto="🔗 Ver anúncio",
                botao_url=url_anuncio,
            )
        else:
            logger.info("Oportunidade sem imagem disponível. A enviar apenas texto.")
            return enviar_mensagem_telegram(
                legenda,
                bot_token=bot_token,
                chat_id=chat_id,
                botao_texto="🔗 Ver anúncio",
                botao_url=url_anuncio,
            )
    except Exception as e:
        # Rede de segurança final: nenhuma falha de notificação pode
        # interromper o ciclo de monitorização principal.
        logger.error(f"Erro inesperado ao notificar oportunidade: {e}")
        return False


# --------------------------------------------------------------------------
# Links rápidos de partilha (para a UI do Streamlit)
# --------------------------------------------------------------------------
def gerar_link_whatsapp(texto: str, url_anuncio: str = "") -> str:
    """
    Gera um link 'wa.me' que abre o WhatsApp com o texto da proposta
    (e o link do anúncio, se fornecido) já pré-preenchido, pronto a
    escolher o contacto e enviar.
    """
    corpo_completo = texto if not url_anuncio else f"{texto}\n\n{url_anuncio}"
    return f"https://wa.me/?text={quote_plus(corpo_completo)}"


def gerar_link_telegram_share(url_anuncio: str, texto: str) -> str:
    """
    Gera um link de partilha rápida do Telegram ('t.me/share/url'), que
    abre o Telegram com o link do anúncio e o texto já preenchidos,
    permitindo ao utilizador escolher para quem reenviar.
    """
    return (
        f"https://t.me/share/url?url={quote_plus(url_anuncio)}"
        f"&text={quote_plus(texto)}"
    )


def gerar_links_partilha(
    oportunidade: Dict[str, Any],
    regra: Dict[str, Any],
    percentagem_desconto: float = 0,
) -> Dict[str, str]:
    """
    Função de conveniência para a UI: gera de uma vez o texto de proposta
    e todos os links de partilha rápida associados a uma oportunidade.

    Retorna um dicionário pronto a usar em botões da interface:
        {
            "texto_proposta": "...",
            "link_whatsapp": "...",
            "link_telegram": "...",
            "link_anuncio": "...",
        }
    """
    texto_proposta = gerar_texto_proposta(oportunidade, regra, percentagem_desconto)
    url_anuncio = oportunidade.get("url_anuncio", "")

    return {
        "texto_proposta": texto_proposta,
        "link_whatsapp": gerar_link_whatsapp(texto_proposta, url_anuncio),
        "link_telegram": gerar_link_telegram_share(url_anuncio, texto_proposta),
        "link_anuncio": url_anuncio,
    }


# --------------------------------------------------------------------------
# Bloco de testes práticos (executar com: python notifier.py)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    TESTE_SETTINGS = "settings_teste.json"
    if os.path.exists(TESTE_SETTINGS):
        os.remove(TESTE_SETTINGS)

    oportunidade_exemplo = {
        "id_artigo": "12345",
        "titulo": "Fender Stratocaster Made in Mexico",
        "preco": 320.0,
        "moeda": "EUR",
        "url_anuncio": "https://www.vinted.pt/items/12345-fender-stratocaster",
        "url_imagem": "https://images.exemplo.com/fender.jpg",
        "plataforma": "vinted",
        "regra_id": "regra-abc",
        "regra_nome": "Fender Stratocaster",
        "data_descoberta": "2026-08-22T10:00:00+00:00",
    }

    regra_exemplo = {
        "id": "regra-abc",
        "nome": "Fender Stratocaster",
        "termo_pesquisa": "fender stratocaster",
        "preco_maximo": 350.0,
        "palavras_excluidas": ["capa", "avariado"],
        "mensagem_proposta": (
            "Olá! Vi o seu anúncio da \"{titulo}\" por {preco_original}€. "
            "Teria interesse em vender por {preco}€? Obrigado!"
        ),
        "ativo": True,
        "plataforma": "vinted",
    }

    print("\n=== TESTE 1: Gerar texto de proposta (sem desconto) ===")
    texto1 = gerar_texto_proposta(oportunidade_exemplo, regra_exemplo, percentagem_desconto=0)
    print(texto1)

    print("\n=== TESTE 2: Gerar texto de proposta (com 10% de desconto) ===")
    texto2 = gerar_texto_proposta(oportunidade_exemplo, regra_exemplo, percentagem_desconto=10)
    print(texto2)

    print("\n=== TESTE 3: Gerar texto de proposta com modelo vazio (fallback) ===")
    regra_sem_modelo = {**regra_exemplo, "mensagem_proposta": ""}
    texto3 = gerar_texto_proposta(oportunidade_exemplo, regra_sem_modelo)
    print(texto3)

    print("\n=== TESTE 4: Gerar links de partilha rápida ===")
    links = gerar_links_partilha(oportunidade_exemplo, regra_exemplo, percentagem_desconto=5)
    for chave, valor in links.items():
        print(f" - {chave}: {valor}")

    print("\n=== TESTE 5: Guardar credenciais de teste em ficheiro local ===")
    sucesso_guardar = guardar_credenciais(
        "123456789:AAExemploDeTokenFicticioParaTeste", "987654321", caminho=TESTE_SETTINGS
    )
    print(f"Credenciais fictícias guardadas com sucesso? {sucesso_guardar}")

    print("\n=== TESTE 6: Testar credenciais FICTÍCIAS (deve falhar de forma controlada) ===")
    resultado_teste_falso = testar_credenciais_telegram(caminho=TESTE_SETTINGS)
    print(f"Sucesso: {resultado_teste_falso['sucesso']}")
    print(f"Mensagem: {resultado_teste_falso['mensagem']}")
    assert resultado_teste_falso["sucesso"] is False, "Era esperado que o token fictício falhasse."
    print("Comportamento correto: falha tratada sem crash do programa. ✅")

    print("\n=== TESTE 7: Notificar oportunidade com credenciais fictícias (deve falhar sem crash) ===")
    sucesso_notificacao = notificar_oportunidade(
        oportunidade_exemplo,
        regra_exemplo,
        percentagem_desconto=0,
        bot_token="token-invalido",
        chat_id="chat-invalido",
    )
    print(f"Notificação enviada com sucesso? {sucesso_notificacao} (esperado: False)")
    print("O programa continuou a executar normalmente após a falha. ✅")

    print("\n=== TESTE 8 (OPCIONAL): Testar com credenciais REAIS ===")
    print(
        "Para testar com o teu bot real, define as variáveis de ambiente antes de "
        "correr este script:\n"
        f"   export {ENV_BOT_TOKEN}='o_teu_token_aqui'\n"
        f"   export {ENV_CHAT_ID}='o_teu_chat_id_aqui'\n"
        "(no Windows PowerShell: $env:TELEGRAM_BOT_TOKEN='...')"
    )

    if os.environ.get(ENV_BOT_TOKEN) and os.environ.get(ENV_CHAT_ID):
        print("Credenciais reais detetadas no ambiente. A testar ligação real...")
        resultado_real = testar_credenciais_telegram()
        print(f"Sucesso: {resultado_real['sucesso']}")
        print(f"Mensagem: {resultado_real['mensagem']}")

        if resultado_real["sucesso"]:
            print("\nA enviar notificação de exemplo real para o teu Telegram...")
            enviado = notificar_oportunidade(oportunidade_exemplo, regra_exemplo, percentagem_desconto=10)
            print(f"Notificação de exemplo enviada com sucesso? {enviado}")
    else:
        print(
            "Nenhuma credencial real encontrada no ambiente — teste opcional ignorado "
            "(isto é normal e esperado se ainda não tiveres um bot configurado)."
        )

    print("\nTestes concluídos.")
