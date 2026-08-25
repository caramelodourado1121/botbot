"""
dados.py
========

Camada de persistência (SQLite) para o histórico de oportunidades.

Substitui a lista em memória (`session_state["oportunidades"]`) como
fonte de verdade para estatísticas e histórico — os dados agora
sobrevivem ao fechar a aplicação, num único ficheiro `dados.db` ao lado
do `config.json` / `vistos.json`.

Usa apenas a biblioteca padrão (`sqlite3`), de propósito: evita
introduzir uma nova dependência externa que complique o
empacotamento com o PyInstaller.

Responsabilidades:
    1. Guardar oportunidades encontradas (sem duplicar).
    2. Listar/filtrar oportunidades (por plataforma, regra, estado).
    3. Marcar oportunidades como ignoradas/restauradas.
    4. Calcular estatísticas agregadas (para a tab "Estatísticas").
    5. Exportar para CSV.

Nota de integração: este módulo é independente do `vistos.json` usado
pelo `scraper_engine.py` para deduplicação de notificações — continuam
a coexistir. O `vistos.json` decide "já notifiquei isto?"; o `dados.db`
guarda "o que já vi, para sempre, para consultar depois".

Autor: Desenvolvimento modular - Etapa 6 (evolução pós-entrega)
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger("dados")

NOME_BD_PADRAO = os.path.join(os.environ.get("MONITOR_DATA_DIR") or os.getcwd(), "dados.db")


class DadosError(Exception):
    """Exceção base para erros da camada de persistência."""


# --------------------------------------------------------------------------
# Ligação à base de dados
# --------------------------------------------------------------------------
@contextmanager
def _conectar(caminho_bd: str = NOME_BD_PADRAO):
    """
    Context manager que abre uma ligação nova por operação (em vez de
    manter uma ligação persistente partilhada), o que é a forma mais
    simples e segura de usar SQLite quando várias threads (UI + thread
    de monitorização em segundo plano) podem aceder aos dados ao mesmo
    tempo. O `timeout=10` faz o SQLite esperar até 10s antes de falhar
    com "database is locked", em vez de falhar imediatamente.
    """
    conn = sqlite3.connect(caminho_bd, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def inicializar_bd(caminho_bd: str = NOME_BD_PADRAO) -> None:
    """Cria a tabela de oportunidades (e índices) se ainda não existirem."""
    try:
        with _conectar(caminho_bd) as conn:
            # Criar tabela com estrutura atualizada
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oportunidades (
                    plataforma TEXT NOT NULL,
                    id_artigo TEXT NOT NULL,
                    titulo TEXT,
                    preco REAL,
                    moeda TEXT,
                    url_anuncio TEXT,
                    url_imagem TEXT,
                    regra_id TEXT,
                    regra_nome TEXT,
                    data_descoberta TEXT,
                    ignorada INTEGER NOT NULL DEFAULT 0,
                    favorita INTEGER NOT NULL DEFAULT 0,
                    data_publicacao TEXT,
                    texto_publicacao TEXT,
                    PRIMARY KEY (plataforma, id_artigo)
                )
                """
            )
            
            # Migração: adicionar colunas se não existirem (para bases de dados antigas)
            try:
                conn.execute("ALTER TABLE oportunidades ADD COLUMN favorita INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                # Coluna já existe, ignorar erro
                pass
            
            try:
                conn.execute("ALTER TABLE oportunidades ADD COLUMN data_publicacao TEXT")
            except sqlite3.OperationalError:
                # Coluna já existe, ignorar erro
                pass

            try:
                conn.execute("ALTER TABLE oportunidades ADD COLUMN texto_publicacao TEXT")
            except sqlite3.OperationalError:
                pass
            
            # Criar índices (após garantir que as colunas existem)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_oportunidades_regra ON oportunidades(regra_nome)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_oportunidades_plataforma ON oportunidades(plataforma)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_oportunidades_favorita ON oportunidades(favorita)"
            )
    except sqlite3.Error as e:
        raise DadosError(f"Não foi possível inicializar a base de dados '{caminho_bd}': {e}") from e


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------
def guardar_oportunidades(
    oportunidades: List[Dict[str, Any]], caminho_bd: str = NOME_BD_PADRAO
) -> int:
    """
    Guarda uma lista de oportunidades na base de dados, ignorando as que
    já existem (mesma combinação plataforma + id_artigo).

    Devolve o número de oportunidades REALMENTE novas que foram inseridas
    (pode ser diferente de len(oportunidades) se algumas já existiam).

    Nunca lança exceção por causa de UMA oportunidade com dados
    inesperados — regista o erro e continua com as restantes.
    """
    if not oportunidades:
        return 0

    inicializar_bd(caminho_bd)
    novas = 0

    try:
        with _conectar(caminho_bd) as conn:
            for o in oportunidades:
                try:
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO oportunidades
                        (plataforma, id_artigo, titulo, preco, moeda, url_anuncio,
                         url_imagem, regra_id, regra_nome, data_descoberta, ignorada, favorita, data_publicacao, texto_publicacao)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                        """,
                        (
                            o.get("plataforma"),
                            str(o.get("id_artigo")),
                            o.get("titulo"),
                            o.get("preco"),
                            o.get("moeda"),
                            o.get("url_anuncio"),
                            o.get("url_imagem"),
                            o.get("regra_id"),
                            o.get("regra_nome"),
                            o.get("data_descoberta"),
                            o.get("data_publicacao"),
                            o.get("texto_publicacao"),
                        ),
                    )
                    if cursor.rowcount:
                        novas += 1
                except sqlite3.Error as e:
                    logger.error(
                        f"Erro ao guardar oportunidade '{o.get('id_artigo')}': {e}"
                    )
    except sqlite3.Error as e:
        logger.error(f"Erro ao aceder à base de dados '{caminho_bd}': {e}")

    return novas


def atualizar_publicacao(
    plataforma: str,
    id_artigo: str,
    data_publicacao: Optional[str],
    texto_publicacao: Optional[str] = None,
    caminho_bd: str = NOME_BD_PADRAO,
) -> bool:
    """Preenche a publicação de um anúncio já guardado, sem alterar o resto."""
    if not data_publicacao:
        return False

    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute(
                "UPDATE oportunidades SET data_publicacao = ?, "
                "texto_publicacao = COALESCE(?, texto_publicacao) "
                "WHERE plataforma = ? AND id_artigo = ? "
                "AND (data_publicacao IS NULL OR data_publicacao = '')",
                (data_publicacao, texto_publicacao, plataforma, str(id_artigo)),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"Erro ao atualizar publicação '{id_artigo}': {e}")
        return False


def limpar_imagens_placeholder(caminho_bd: str = NOME_BD_PADRAO) -> int:
    """Remove URLs de placeholders guardadas como imagens reais."""
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute(
                "UPDATE oportunidades SET url_imagem = '' "
                "WHERE url_imagem LIKE '%no_thumbnail%' OR url_imagem LIKE '/app/static/%'"
            )
            return cursor.rowcount
    except sqlite3.Error as e:
        logger.error(f"Erro ao limpar placeholders de imagem: {e}")
        return 0
def marcar_ignorada(
    plataforma: str, id_artigo: str, ignorada: bool = True, caminho_bd: str = NOME_BD_PADRAO
) -> bool:
    """Marca (ou desmarca) uma oportunidade como ignorada. Devolve True se afetou alguma linha."""
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute(
                "UPDATE oportunidades SET ignorada = ? WHERE plataforma = ? AND id_artigo = ?",
                (1 if ignorada else 0, plataforma, str(id_artigo)),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"Erro ao marcar oportunidade como ignorada: {e}")
        return False


def marcar_favorita(
    plataforma: str, id_artigo: str, favorita: bool = True, caminho_bd: str = NOME_BD_PADRAO
) -> bool:
    """Marca (ou desmarca) uma oportunidade como favorita. Devolve True se afetou alguma linha."""
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute(
                "UPDATE oportunidades SET favorita = ? WHERE plataforma = ? AND id_artigo = ?",
                (1 if favorita else 0, plataforma, str(id_artigo)),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"Erro ao marcar oportunidade como favorita: {e}")
        return False


def definir_estado(
    plataforma: str,
    id_artigo: str,
    estado: str,
    caminho_bd: str = NOME_BD_PADRAO,
) -> bool:
    """Move uma oportunidade para o feed, guardadas ou descartadas.

    Os estados s\u00e3o mutuamente exclusivos, evitando que uma oportunidade
    apare\u00e7a simultaneamente nas abas Guardadas e Descartadas.
    """
    valores = {
        "feed": (0, 0),
        "guardadas": (0, 1),
        "descartadas": (1, 0),
    }
    if estado not in valores:
        raise ValueError("Estado inv\u00e1lido. Use: feed, guardadas ou descartadas.")

    ignorada, favorita = valores[estado]
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute(
                "UPDATE oportunidades SET ignorada = ?, favorita = ? "
                "WHERE plataforma = ? AND id_artigo = ?",
                (ignorada, favorita, plataforma, str(id_artigo)),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.error(f"Erro ao atualizar o estado da oportunidade: {e}")
        return False


def eliminar_descartadas(caminho_bd: str = NOME_BD_PADRAO) -> int:
    """Elimina definitivamente todas as oportunidades descartadas.

    Devolve o número de registos removidos. Esta operação não pode ser
    revertida pela aplicação.
    """
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute("DELETE FROM oportunidades WHERE ignorada = 1")
            return cursor.rowcount
    except sqlite3.Error as e:
        logger.error(f"Erro ao eliminar oportunidades descartadas: {e}")
        return 0


def eliminar_todas(caminho_bd: str = NOME_BD_PADRAO) -> int:
    """Elimina todas as oportunidades guardadas e devolve o total removido."""
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute("DELETE FROM oportunidades")
            return cursor.rowcount
    except sqlite3.Error as e:
        logger.error(f"Erro ao eliminar todas as oportunidades: {e}")
        return 0


# --------------------------------------------------------------------------
# Leitura / consulta
# --------------------------------------------------------------------------
def listar_oportunidades(
    caminho_bd: str = NOME_BD_PADRAO,
    plataforma: Optional[str] = None,
    regra_nome: Optional[str] = None,
    apenas_nao_ignoradas: bool = False,
    apenas_favoritas: bool = False,
    estado: Optional[str] = None,
    limite: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Lista oportunidades guardadas, mais recentes primeiro, com filtros
    opcionais. Devolve sempre uma lista (vazia em caso de erro ou de
    a base de dados ainda não existir), nunca lança exceção.
    """
    inicializar_bd(caminho_bd)

    condicoes = []
    parametros: List[Any] = []

    if plataforma and plataforma != "Todas":
        condicoes.append("plataforma = ?")
        parametros.append(plataforma)
    if regra_nome and regra_nome != "Todas":
        condicoes.append("regra_nome = ?")
        parametros.append(regra_nome)
    if estado == "feed":
        condicoes.append("ignorada = 0")
        condicoes.append("favorita = 0")
    elif estado == "guardadas":
        condicoes.append("ignorada = 0")
        condicoes.append("favorita = 1")
    elif estado == "descartadas":
        condicoes.append("ignorada = 1")
    else:
        if apenas_nao_ignoradas:
            condicoes.append("ignorada = 0")
        if apenas_favoritas:
            condicoes.append("favorita = 1")

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ""
    limite_sql = f"LIMIT {int(limite)}" if limite else ""

    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute(
                f"SELECT * FROM oportunidades {where_sql} "
                # Prioriza a data de publicação REAL do anúncio na
                # plataforma (quando conseguimos extraí-la); só usa a data
                # em que o nosso programa o descobriu como último recurso,
                # para anúncios onde a plataforma não indicou data nenhuma.
                # Isto evita que um anúncio antigo "reaparecer" num ciclo
                # posterior o faça saltar para o topo à frente de anúncios
                # genuinamente mais recentes.
                f"ORDER BY COALESCE(data_publicacao, data_descoberta) DESC {limite_sql}",
                parametros,
            )
            return [dict(linha) for linha in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Erro ao listar oportunidades: {e}")
        return []


def contar_oportunidades(caminho_bd: str = NOME_BD_PADRAO) -> int:
    """Devolve o número total de oportunidades guardadas (0 em caso de erro)."""
    inicializar_bd(caminho_bd)
    try:
        with _conectar(caminho_bd) as conn:
            cursor = conn.execute("SELECT COUNT(*) AS total FROM oportunidades")
            return cursor.fetchone()["total"]
    except sqlite3.Error as e:
        logger.error(f"Erro ao contar oportunidades: {e}")
        return 0


def contar_por_estado(estado: str, caminho_bd: str = NOME_BD_PADRAO) -> int:
    """Conta oportunidades no feed, guardadas ou descartadas."""
    inicializar_bd(caminho_bd)
    if estado == "feed":
        sql = "SELECT COUNT(*) AS total FROM oportunidades WHERE ignorada = 0 AND favorita = 0"
    elif estado == "guardadas":
        sql = "SELECT COUNT(*) AS total FROM oportunidades WHERE ignorada = 0 AND favorita = 1"
    elif estado == "descartadas":
        sql = "SELECT COUNT(*) AS total FROM oportunidades WHERE ignorada = 1"
    else:
        sql = "SELECT COUNT(*) AS total FROM oportunidades"
    try:
        with _conectar(caminho_bd) as conn:
            return conn.execute(sql).fetchone()["total"]
    except sqlite3.Error as e:
        logger.error(f"Erro ao contar oportunidades ({estado}): {e}")
        return 0


def contar_favoritas(caminho_bd: str = NOME_BD_PADRAO) -> int:
    """Devolve o número de oportunidades marcadas como favoritas (0 em caso de erro)."""
    return contar_por_estado("guardadas", caminho_bd)


# --------------------------------------------------------------------------
# Exportação para CSV
# --------------------------------------------------------------------------
def exportar_csv_conteudo(
    caminho_bd: str = NOME_BD_PADRAO,
    apenas_nao_ignoradas: bool = False,
) -> str:
    """Devolve o histórico em CSV (UTF-8 com BOM) ou string vazia se não houver dados."""
    linhas = listar_oportunidades(caminho_bd, apenas_nao_ignoradas=apenas_nao_ignoradas)
    if not linhas:
        return ""

    buf = io.StringIO()
    escritor = csv.DictWriter(buf, fieldnames=list(linhas[0].keys()))
    escritor.writeheader()
    escritor.writerows(linhas)
    return buf.getvalue()


def exportar_csv(
    caminho_destino: str,
    caminho_bd: str = NOME_BD_PADRAO,
    apenas_nao_ignoradas: bool = False,
) -> int:
    """
    Exporta o histórico de oportunidades para um ficheiro CSV.

    Devolve o número de linhas escritas (0 em caso de erro ou histórico
    vazio). Usa apenas o módulo `csv` da biblioteca padrão.
    """
    linhas = listar_oportunidades(caminho_bd, apenas_nao_ignoradas=apenas_nao_ignoradas)
    conteudo = exportar_csv_conteudo(caminho_bd, apenas_nao_ignoradas=apenas_nao_ignoradas)
    if not conteudo:
        return 0

    try:
        with open(caminho_destino, "w", newline="", encoding="utf-8-sig") as f:
            f.write(conteudo)
        return len(linhas)
    except OSError as e:
        logger.error(f"Erro ao exportar CSV para '{caminho_destino}': {e}")
        return 0


# --------------------------------------------------------------------------
# Bloco de testes práticos (executar com: python dados.py)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    TESTE_BD = "dados_teste.db"
    TESTE_CSV = "export_teste.csv"
    for f in (TESTE_BD, TESTE_CSV):
        if os.path.exists(f):
            os.remove(f)

    print("\n=== TESTE 1: Inicializar base de dados ===")
    inicializar_bd(TESTE_BD)
    print(f"Base de dados criada em: {os.path.abspath(TESTE_BD)}")

    print("\n=== TESTE 2: Guardar oportunidades (com um duplicado propositado) ===")
    exemplos = [
        {
            "plataforma": "vinted", "id_artigo": "1", "titulo": "PS5 A", "preco": 200.0,
            "moeda": "EUR", "url_anuncio": "http://x/1", "url_imagem": "", "regra_id": "r1",
            "regra_nome": "PS5", "data_descoberta": "2026-08-23T10:00:00",
        },
        {
            "plataforma": "vinted", "id_artigo": "2", "titulo": "PS5 B", "preco": 180.0,
            "moeda": "EUR", "url_anuncio": "http://x/2", "url_imagem": "", "regra_id": "r1",
            "regra_nome": "PS5", "data_descoberta": "2026-08-23T11:00:00",
        },
        {
            "plataforma": "wallapop", "id_artigo": "3", "titulo": "Fender Strat", "preco": 300.0,
            "moeda": "EUR", "url_anuncio": "http://x/3", "url_imagem": "", "regra_id": "r2",
            "regra_nome": "Fender", "data_descoberta": "2026-08-23T12:00:00",
        },
    ]
    novas1 = guardar_oportunidades(exemplos, TESTE_BD)
    print(f"Novas oportunidades guardadas (1ª vez): {novas1} (esperado: 3)")

    novas2 = guardar_oportunidades(exemplos, TESTE_BD)
    print(f"Novas oportunidades guardadas (2ª vez, duplicadas): {novas2} (esperado: 0)")

    print("\n=== TESTE 3: Listar oportunidades ===")
    todas = listar_oportunidades(TESTE_BD)
    print(f"Total no histórico: {len(todas)}")
    for o in todas:
        print(f" - [{o['plataforma']}] {o['titulo']} — {o['preco']}€ (regra: {o['regra_nome']})")

    print("\n=== TESTE 4: Filtrar por plataforma ===")
    so_vinted = listar_oportunidades(TESTE_BD, plataforma="vinted")
    print(f"Só Vinted: {len(so_vinted)} (esperado: 2)")

    print("\n=== TESTE 5: Marcar como ignorada e filtrar ===")
    marcar_ignorada("vinted", "1", ignorada=True, caminho_bd=TESTE_BD)
    nao_ignoradas = listar_oportunidades(TESTE_BD, apenas_nao_ignoradas=True)
    print(f"Não ignoradas: {len(nao_ignoradas)} (esperado: 2)")

    print("\n=== TESTE 6: Exportar para CSV ===")
    linhas_exportadas = exportar_csv(TESTE_CSV, TESTE_BD)
    print(f"Linhas exportadas: {linhas_exportadas} (esperado: 3)")
    print(f"Ficheiro CSV gerado em: {os.path.abspath(TESTE_CSV)}")

    print("\nTodos os testes concluídos.")