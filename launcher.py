"""
launcher.py
===========

Ponto de entrada usado para gerar o executável (.exe) final via PyInstaller.

Este script NÃO substitui o `app.py` — ele apenas arranca-o de forma
programática, sem precisar do comando `streamlit run` na linha de comandos,
o que é essencial para que o PyInstaller consiga gerar um .exe autónomo
que um utilizador não-técnico possa correr com um duplo-clique.

Responsabilidades:
    1. Detetar se está a correr "normal" (durante o desenvolvimento) ou já
       "empacotado" dentro de um .exe gerado pelo PyInstaller.
    2. Garantir que os ficheiros de dados do utilizador (config.json,
       vistos.json, settings.json) são sempre guardados ao lado do
       executável — nunca numa pasta temporária que desaparece ao fechar.
    3. Escolher uma porta local livre (evita conflitos se a porta 8501 já
       estiver a ser usada por outro programa).
    4. Arrancar o servidor Streamlit programaticamente, apontando para o
       `app.py` (Etapa 4).
    5. Abrir automaticamente o browser padrão do utilizador no endereço
       local da aplicação, pouco depois do servidor arrancar.

Autor: Desenvolvimento modular - Etapa 5/5
"""

from __future__ import annotations

import os
import sys
import socket
import logging
import threading
import webbrowser

# --------------------------------------------------------------------------
# Logging simples (visível na consola, se o build usar --console em vez de
# --noconsole, útil para diagnóstico durante os testes de compilação)
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [LAUNCHER] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("launcher")

PORTA_PADRAO = 8501
NOME_FICHEIRO_APP = "app.py"


# --------------------------------------------------------------------------
# Deteção do modo de execução (script normal vs. .exe empacotado)
# --------------------------------------------------------------------------
def _esta_empacotado() -> bool:
    """Indica se este script está a correr dentro de um .exe do PyInstaller."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _obter_pasta_recursos() -> str:
    """
    Devolve a pasta onde procurar app.py, config_manager.py, scraper_engine.py
    e notifier.py.

    - Modo .exe: pasta temporária de extração do PyInstaller (`sys._MEIPASS`),
      onde o `build.bat` coloca estes ficheiros como dados (`--add-data`).
    - Modo desenvolvimento: a pasta onde este próprio ficheiro está.
    """
    if _esta_empacotado():
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _obter_pasta_dados_utilizador() -> str:
    """
    Devolve a pasta onde os ficheiros de dados do utilizador
    (config.json, vistos.json, settings.json) devem ser lidos/guardados.

    É SEMPRE a pasta onde o .exe (ou o script, em desenvolvimento)
    realmente reside — nunca a pasta temporária `_MEIPASS`, que é apagada
    assim que o programa fecha. Isto garante que as regras configuradas e
    o histórico de artigos já vistos sobrevivem entre execuções.
    """
    if _esta_empacotado():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Gestão de portas
# --------------------------------------------------------------------------
def _porta_esta_livre(porta: int) -> bool:
    """Verifica se uma porta TCP local está livre para usar."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", porta)) != 0


def _encontrar_porta_disponivel(porta_inicial: int = PORTA_PADRAO) -> int:
    """
    Procura a primeira porta livre a partir de `porta_inicial`.

    Evita que o utilizador tenha um erro confuso caso já tenha outro
    programa (ou outra instância desta app) a usar a porta 8501.
    """
    porta = porta_inicial
    for _ in range(20):
        if _porta_esta_livre(porta):
            return porta
        porta += 1
    logger.warning(
        f"Não foi encontrada nenhuma porta livre entre {porta_inicial} e {porta}. "
        f"A tentar mesmo assim com a porta {porta_inicial}."
    )
    return porta_inicial


# --------------------------------------------------------------------------
# Abertura automática do browser
# --------------------------------------------------------------------------
def _abrir_browser_apos_arranque(url: str, atraso_segundos: float = 2.5) -> None:
    """
    Abre o browser padrão do utilizador após um pequeno atraso.

    O atraso dá tempo ao servidor Streamlit de arrancar completamente
    antes do browser tentar aceder ao endereço. Corre numa thread separada
    para não bloquear o arranque do servidor.
    """
    def _abrir():
        try:
            webbrowser.open(url)
            logger.info(f"Browser aberto automaticamente em {url}")
        except Exception as e:
            # Nunca deve impedir o funcionamento da app — o utilizador pode
            # sempre abrir o link manualmente.
            logger.warning(
                f"Não foi possível abrir o browser automaticamente ({e}). "
                f"Abre manualmente este link: {url}"
            )

    threading.Timer(atraso_segundos, _abrir).start()


# --------------------------------------------------------------------------
# Ponto de entrada principal
# --------------------------------------------------------------------------
def main() -> None:
    pasta_recursos = _obter_pasta_recursos()
    pasta_dados = _obter_pasta_dados_utilizador()
    os.environ["MONITOR_DATA_DIR"] = pasta_dados

    # Garante que "import config_manager / scraper_engine / notifier" dentro
    # do app.py funciona, independentemente de onde o .exe for corrido.
    if pasta_recursos not in sys.path:
        sys.path.insert(0, pasta_recursos)

    # Muda a pasta de trabalho atual para junto do executável, para que
    # config.json / vistos.json / settings.json fiquem sempre visíveis e
    # persistentes ao lado do programa (e não numa pasta temporária).
    os.chdir(pasta_dados)
    logger.info(f"Pasta de dados do utilizador (config/vistos/settings): {pasta_dados}")
    with open(os.path.join(pasta_dados, "launcher-started.log"), "w", encoding="utf-8") as ficheiro:
        ficheiro.write("launcher iniciado\n")

    caminho_app = os.path.join(pasta_recursos, NOME_FICHEIRO_APP)
    if not os.path.exists(caminho_app):
        logger.error(
            f"Não foi possível encontrar '{NOME_FICHEIRO_APP}' em '{pasta_recursos}'. "
            "Verifica se o build.bat incluiu corretamente todos os ficheiros "
            "com a flag --add-data."
        )
        input("Prime ENTER para fechar...")
        sys.exit(1)

    porta = _encontrar_porta_disponivel()
    url_final = f"http://localhost:{porta}"

    logger.info("A iniciar o Monitor de Oportunidades Vinted & Wallapop...")
    logger.info(f"A aplicação vai abrir automaticamente em: {url_final}")
    logger.info("(Se o browser não abrir sozinho, copia e cola este endereço manualmente.)")

    _abrir_browser_apos_arranque(url_final)

    # Equivale a correr, na linha de comandos:
    #   streamlit run app.py --server.port=<porta> --server.headless=true ...
    sys.argv = [
        "streamlit",
        "run",
        caminho_app,
        "--global.developmentMode=false",
        f"--server.port={porta}",
        "--server.headless=true",
        "--server.address=localhost",
        "--browser.gatherUsageStats=false",
    ]

    from streamlit.web import bootstrap

    bootstrap.run(
        caminho_app,
        False,
        [],
        {
            "global.developmentMode": False,
            "server.port": porta,
            "server.headless": True,
            "server.address": "localhost",
            "browser.gatherUsageStats": False,
        },
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        pasta = os.path.dirname(os.path.abspath(sys.executable))
        caminho_log = os.path.join(pasta, "launcher-crash.log")
        try:
            with open(caminho_log, "w", encoding="utf-8") as ficheiro:
                import traceback
                ficheiro.write(traceback.format_exc())
        except Exception:
            pass
        logger.exception("Falha fatal no arranque. Detalhes em %s", caminho_log)
        raise
