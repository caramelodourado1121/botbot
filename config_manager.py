"""
config_manager.py
==================

Módulo responsável pela gestão (CRUD) das regras de busca genéricas
utilizadas pelo sistema de monitorização de oportunidades (Vinted / Wallapop).

As regras são persistidas localmente num ficheiro JSON (config.json),
para que o utilizador final (não-técnico) não precise de tocar em código
para configurar as suas pesquisas.

Estrutura de uma regra:
{
    "id": "uuid-string",
    "nome": "Fender Stratocaster",
    "termo_pesquisa": "fender stratocaster",  # opcional se houver imagem
    "preco_maximo": 350.0,
    "palavras_excluidas": ["capa", "avariado", "defeito"],
    "mensagem_proposta": "Olá! Tenho interesse no artigo...",
    "ativo": true,
    "plataforma": "ambas",   # "vinted" | "wallapop" | "olx" | "facebook" | "ambas"
    "imagens_referencia": ["imagens_regras/uuid_a.jpg"],  # opcional, várias fotos
    "similaridade_minima": 50  # 0-100; só aplica com imagens
}

Autor: Desenvolvimento modular - Etapa 1/5
"""

from __future__ import annotations

import json
import os
import uuid
import logging
from typing import Optional, List, Dict, Any

# --------------------------------------------------------------------------
# Configuração de logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("config_manager")

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------
DATA_DIR = os.environ.get("MONITOR_DATA_DIR") or os.getcwd()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
PLATAFORMAS_VALIDAS = {"vinted", "wallapop", "olx", "facebook", "ambas"}
TIPOS_PESQUISA_VALIDOS = {"imagem", "texto", "hibrida"}
CLIP_CONFIG_PADRAO = {
    "model_name": "ViT-B/16",
    "device": "auto",  # "auto", "cpu", "cuda"
    "cache_size": 1000,
    "max_concurrent_downloads": 10,
    "timeout_seconds": 10,
    "max_retries": 3
}
ESTRUTURA_PADRAO = {
    "regras": [],
    "clip_config": CLIP_CONFIG_PADRAO
}
PASTA_IMAGENS_REGRAS = os.path.join(DATA_DIR, "imagens_regras")
EXTENSOES_IMAGEM = {".jpg", ".jpeg", ".png", ".webp"}
SIMILARIDADE_PADRAO = 50

# Configuração CLIP (CLIP_CONFIG_PADRAO definido acima para ESTRUTURA_PADRAO)


# --------------------------------------------------------------------------
# Exceções personalizadas
# --------------------------------------------------------------------------
class ConfigManagerError(Exception):
    """Exceção base para erros do gestor de configuração."""


class RegraInvalidaError(ConfigManagerError):
    """Lançada quando os dados de uma regra são inválidos."""


class RegraNaoEncontradaError(ConfigManagerError):
    """Lançada quando se tenta operar sobre uma regra inexistente."""


# --------------------------------------------------------------------------
# Funções internas de leitura/escrita do ficheiro
# --------------------------------------------------------------------------
def _garantir_ficheiro_existe(caminho: str = CONFIG_FILE) -> None:
    """Cria o ficheiro config.json com a estrutura padrão, caso não exista."""
    if not os.path.exists(caminho):
        try:
            with open(caminho, "w", encoding="utf-8") as f:
                json.dump(ESTRUTURA_PADRAO, f, ensure_ascii=False, indent=4)
            logger.info(f"Ficheiro '{caminho}' criado com estrutura padrão.")
        except OSError as e:
            raise ConfigManagerError(
                f"Não foi possível criar o ficheiro de configuração: {e}"
            ) from e


def _carregar_config(caminho: str = CONFIG_FILE) -> Dict[str, Any]:
    """Carrega o conteúdo do config.json, criando-o se necessário."""
    _garantir_ficheiro_existe(caminho)
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            conteudo = json.load(f)
            if "regras" not in conteudo or not isinstance(conteudo["regras"], list):
                logger.warning(
                    "Estrutura do config.json inválida. A repor estrutura padrão."
                )
                conteudo = dict(ESTRUTURA_PADRAO)
            return conteudo
    except json.JSONDecodeError as e:
        logger.error(f"Ficheiro '{caminho}' corrompido ou mal formatado: {e}")
        # Em vez de rebentar o programa, devolvemos uma estrutura vazia,
        # e avisamos claramente o utilizador para evitar perda total de dados.
        raise ConfigManagerError(
            f"O ficheiro '{caminho}' está corrompido e não pôde ser lido. "
            "Verifique o ficheiro manualmente ou apague-o para recomeçar."
        ) from e
    except OSError as e:
        raise ConfigManagerError(f"Erro ao ler o ficheiro '{caminho}': {e}") from e


def _guardar_config(config: Dict[str, Any], caminho: str = CONFIG_FILE) -> None:
    """Persiste a configuração completa no ficheiro JSON."""
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except OSError as e:
        raise ConfigManagerError(f"Erro ao guardar o ficheiro '{caminho}': {e}") from e


# --------------------------------------------------------------------------
# Validação
# --------------------------------------------------------------------------
def determinar_tipo_pesquisa(regra: Dict[str, Any]) -> str:
    """Devolve 'imagem', 'texto' ou 'hibrida' com base nos campos da regra."""
    tipo = str(regra.get("tipo_pesquisa") or "").strip().lower()
    if tipo in TIPOS_PESQUISA_VALIDOS:
        return tipo

    tem_imagens = bool(imagens_da_regra(regra))
    tem_termo = bool(str(regra.get("termo_pesquisa") or "").strip())

    if tem_imagens and not tem_termo:
        return "imagem"
    if tem_imagens and tem_termo:
        return "hibrida"
    return "texto"


def _validar_dados_regra(
    nome: str,
    termo_pesquisa: str,
    preco_maximo: float,
    palavras_excluidas: Optional[List[str]],
    mensagem_proposta: str,
    plataforma: str,
    imagens_referencia: Optional[List[str]] = None,
    similaridade_minima: Any = SIMILARIDADE_PADRAO,
    preco_minimo: float = 0.0,
    tipo_pesquisa: str = "auto",
) -> None:
    """Valida os campos de uma regra antes de a criar/atualizar."""
    if not nome or not isinstance(nome, str) or not nome.strip():
        raise RegraInvalidaError("O campo 'nome' é obrigatório e não pode ser vazio.")

    termo = (termo_pesquisa or "").strip() if isinstance(termo_pesquisa, str) else ""
    imagens = normalizar_imagens_referencia(imagens_referencia)
    tipo = tipo_pesquisa.strip().lower() if isinstance(tipo_pesquisa, str) else "auto"

    if tipo == "imagem":
        if not imagens:
            raise RegraInvalidaError(
                "Para pesquisa por imagem, deves carregar pelo menos uma imagem de referência."
            )
    elif tipo == "texto":
        if not termo:
            raise RegraInvalidaError(
                "Para pesquisa por texto, deves indicar pelo menos um termo de pesquisa."
            )
    elif tipo == "hibrida":
        if not termo:
            raise RegraInvalidaError(
                "Para pesquisa híbrida, o termo de pesquisa é obrigatório."
            )
        if not imagens:
            raise RegraInvalidaError(
                "Para pesquisa híbrida, deves carregar pelo menos uma imagem de referência."
            )
    else:  # auto
        if not termo and not imagens:
            raise RegraInvalidaError(
                "Indica um termo de pesquisa ou carrega pelo menos uma imagem de referência."
            )

    try:
        preco_float = float(preco_maximo)
        if preco_float < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise RegraInvalidaError(
            "O campo 'preco_maximo' deve ser um número positivo (ex: 150.0)."
        )

    try:
        minimo_float = float(preco_minimo)
        if minimo_float < 0 or minimo_float > preco_float:
            raise ValueError
    except (TypeError, ValueError):
        raise RegraInvalidaError(
            "O campo 'preco_minimo' deve estar entre 0 e o preço máximo."
        )

    if palavras_excluidas is not None and not isinstance(palavras_excluidas, list):
        raise RegraInvalidaError("O campo 'palavras_excluidas' deve ser uma lista de strings.")

    if palavras_excluidas is not None and not all(
        isinstance(p, str) for p in palavras_excluidas
    ):
        raise RegraInvalidaError("Todas as 'palavras_excluidas' devem ser texto (strings).")

    if mensagem_proposta is not None and not isinstance(mensagem_proposta, str):
        raise RegraInvalidaError("O campo 'mensagem_proposta' deve ser texto (string).")

    if plataforma not in PLATAFORMAS_VALIDAS:
        raise RegraInvalidaError(
            f"Plataforma inválida: '{plataforma}'. "
            f"Valores aceites: {', '.join(sorted(PLATAFORMAS_VALIDAS))}."
        )

    try:
        similaridade = int(similaridade_minima)
        if similaridade < 0 or similaridade > 100:
            raise ValueError
    except (TypeError, ValueError):
        raise RegraInvalidaError(
            "O campo 'similaridade_minima' deve ser um número entre 0 e 100."
        )


# --------------------------------------------------------------------------
# Imagens de referência (várias por regra)
# --------------------------------------------------------------------------
def normalizar_imagens_referencia(valor: Any) -> List[str]:
    """Aceita lista, string antiga (uma foto) ou None e devolve caminhos válidos."""
    if valor is None:
        return []
    if isinstance(valor, str):
        caminho = valor.strip()
        return [caminho] if caminho else []
    if isinstance(valor, list):
        return [str(p).strip() for p in valor if str(p).strip()]
    return []


def imagens_da_regra(regra: Dict[str, Any]) -> List[str]:
    """Devolve as fotos de uma regra, incluindo o campo antigo `imagem_referencia`."""
    imagens = normalizar_imagens_referencia(regra.get("imagens_referencia"))
    if imagens:
        return imagens
    return normalizar_imagens_referencia(regra.get("imagem_referencia"))


def guardar_imagem_regra(regra_id: str, dados: bytes, nome_original: str = "foto.jpg") -> str:
    """Grava uma foto de referência extra para a regra e devolve o caminho relativo."""
    if not dados:
        raise RegraInvalidaError("A imagem de referência está vazia.")
    if len(dados) > 8 * 1024 * 1024:
        raise RegraInvalidaError("A imagem de referência não pode ter mais de 8 MB.")

    os.makedirs(PASTA_IMAGENS_REGRAS, exist_ok=True)
    ext = os.path.splitext(nome_original or "")[1].lower()
    if ext not in EXTENSOES_IMAGEM:
        ext = ".jpg"
    caminho = os.path.join(
        PASTA_IMAGENS_REGRAS, f"{regra_id}_{uuid.uuid4().hex[:8]}{ext}"
    ).replace("\\", "/")
    try:
        with open(caminho, "wb") as f:
            f.write(dados)
    except OSError as e:
        raise ConfigManagerError(f"Não foi possível guardar a imagem da regra: {e}") from e
    logger.info(f"Imagem de referência guardada em '{caminho}'.")
    return caminho


def remover_ficheiro_imagem(caminho: str) -> None:
    """Apaga um ficheiro de imagem, se existir."""
    try:
        if caminho and os.path.isfile(caminho):
            os.remove(caminho)
    except OSError as e:
        logger.warning(f"Não foi possível apagar a imagem '{caminho}': {e}")


def remover_imagens_regra(regra_id: str, caminhos: Optional[List[str]] = None) -> None:
    """Apaga as fotos indicadas, ou todas as fotos da regra se `caminhos` for None."""
    a_apagar = list(caminhos or [])
    if caminhos is None:
        pasta = PASTA_IMAGENS_REGRAS
        if os.path.isdir(pasta):
            for nome in os.listdir(pasta):
                if nome.startswith(f"{regra_id}") and os.path.splitext(nome)[1].lower() in EXTENSOES_IMAGEM:
                    a_apagar.append(os.path.join(pasta, nome))
    for caminho in a_apagar:
        remover_ficheiro_imagem(caminho)


def criar_regra(
    nome: str,
    termo_pesquisa: str = "",
    preco_maximo: float = 0.0,
    preco_minimo: float = 0.0,
    palavras_excluidas: Optional[List[str]] = None,
    mensagem_proposta: str = "",
    ativo: bool = True,
    plataforma: str = "ambas",
    imagem_referencia: str = "",
    imagens_referencia: Optional[List[str]] = None,
    similaridade_minima: int = SIMILARIDADE_PADRAO,
    tipo_pesquisa: str = "auto",
    caminho: str = CONFIG_FILE,
) -> Dict[str, Any]:
    """
    Cria uma nova regra de busca e guarda-a no config.json.

    Retorna o dicionário da regra criada (já com o 'id' gerado).
    Lança RegraInvalidaError se os dados forem inválidos.
    """
    plataforma = (plataforma or "ambas").strip().lower()
    imagens = imagens_da_regra(
        {"imagens_referencia": imagens_referencia, "imagem_referencia": imagem_referencia}
    )
    tipo = (tipo_pesquisa or "auto").strip().lower()
    if tipo == "imagem":
        termo = ""
    else:
        termo = (termo_pesquisa or "").strip()

    if tipo not in TIPOS_PESQUISA_VALIDOS:
        if imagens and not termo:
            tipo = "imagem"
        elif imagens and termo:
            tipo = "hibrida"
        else:
            tipo = "texto"

    similaridade = int(similaridade_minima if similaridade_minima is not None else SIMILARIDADE_PADRAO)
    _validar_dados_regra(
        nome,
        termo,
        preco_maximo,
        palavras_excluidas,
        mensagem_proposta,
        plataforma,
        imagens,
        similaridade,
        preco_minimo=preco_minimo,
        tipo_pesquisa=tipo,
    )

    nova_regra = {
        "id": str(uuid.uuid4()),
        "nome": nome.strip(),
        "tipo_pesquisa": tipo,
        "termo_pesquisa": termo,
        "preco_maximo": float(preco_maximo),
        "preco_minimo": float(preco_minimo),
        "palavras_excluidas": palavras_excluidas or [],
        "mensagem_proposta": mensagem_proposta or "",
        "ativo": bool(ativo),
        "plataforma": plataforma,
        "imagens_referencia": imagens,
        "similaridade_minima": similaridade,
    }

    config = _carregar_config(caminho)
    config["regras"].append(nova_regra)
    _guardar_config(config, caminho)

    logger.info(f"Regra criada com sucesso: '{nova_regra['nome']}' (id={nova_regra['id']}, tipo={tipo})")
    return nova_regra


# --------------------------------------------------------------------------
# CRUD - Ler
# --------------------------------------------------------------------------
def listar_regras(
    apenas_ativas: bool = False, caminho: str = CONFIG_FILE
) -> List[Dict[str, Any]]:
    """Devolve a lista de todas as regras (ou apenas as ativas)."""
    config = _carregar_config(caminho)
    regras = config.get("regras", [])
    if apenas_ativas:
        regras = [r for r in regras if r.get("ativo", False)]
    normalizadas = []
    for regra in regras:
        copia = dict(regra)
        copia["imagens_referencia"] = imagens_da_regra(copia)
        copia["tipo_pesquisa"] = determinar_tipo_pesquisa(copia)
        copia.pop("imagem_referencia", None)
        normalizadas.append(copia)
    return normalizadas


def obter_regra(regra_id: str, caminho: str = CONFIG_FILE) -> Optional[Dict[str, Any]]:
    """Devolve uma regra específica pelo seu id, ou None se não existir."""
    for regra in listar_regras(caminho=caminho):
        if regra.get("id") == regra_id:
            return regra
    return None


# --------------------------------------------------------------------------
# CRUD - Atualizar
# --------------------------------------------------------------------------
def atualizar_regra(
    regra_id: str, caminho: str = CONFIG_FILE, **campos_a_atualizar: Any
) -> Dict[str, Any]:
    """
    Atualiza os campos indicados de uma regra existente.

    Exemplo de uso:
        atualizar_regra(id, preco_maximo=200.0, ativo=False)

    Lança RegraNaoEncontradaError se o id não existir.
    Lança RegraInvalidaError se os novos valores forem inválidos.
    """
    config = _carregar_config(caminho)
    regras = config.get("regras", [])

    regra_encontrada = None
    for regra in regras:
        if regra.get("id") == regra_id:
            regra_encontrada = regra
            break

    if regra_encontrada is None:
        raise RegraNaoEncontradaError(f"Nenhuma regra encontrada com o id '{regra_id}'.")

    # Mescla os valores atuais com os novos, para validar o resultado final
    dados_mesclados = {**regra_encontrada, **campos_a_atualizar}
    plataforma = (dados_mesclados.get("plataforma") or "ambas").strip().lower()
    dados_mesclados["plataforma"] = plataforma

    # Quando a lista é fornecida explicitamente, uma lista vazia significa
    # mesmo "remover todas"; não deve recuperar a foto do campo legado.
    if "imagens_referencia" in campos_a_atualizar:
        dados_mesclados["imagens_referencia"] = normalizar_imagens_referencia(
            campos_a_atualizar["imagens_referencia"]
        )
    else:
        dados_mesclados["imagens_referencia"] = imagens_da_regra(dados_mesclados)

    tipo = dados_mesclados.get("tipo_pesquisa")
    if tipo == "imagem":
        dados_mesclados["termo_pesquisa"] = ""
    elif not tipo or tipo not in TIPOS_PESQUISA_VALIDOS:
        dados_mesclados["tipo_pesquisa"] = determinar_tipo_pesquisa(dados_mesclados)

    _validar_dados_regra(
        dados_mesclados.get("nome"),
        dados_mesclados.get("termo_pesquisa") or "",
        dados_mesclados.get("preco_maximo"),
        dados_mesclados.get("palavras_excluidas"),
        dados_mesclados.get("mensagem_proposta"),
        plataforma,
        dados_mesclados.get("imagens_referencia"),
        dados_mesclados.get("similaridade_minima", SIMILARIDADE_PADRAO),
        preco_minimo=dados_mesclados.get("preco_minimo", 0.0),
        tipo_pesquisa=dados_mesclados.get("tipo_pesquisa", "auto"),
    )

    # Aplica as alterações validadas (mantendo sempre o id original)
    dados_mesclados["id"] = regra_id
    dados_mesclados["termo_pesquisa"] = (dados_mesclados.get("termo_pesquisa") or "").strip()
    if dados_mesclados.get("tipo_pesquisa") == "imagem":
        dados_mesclados["termo_pesquisa"] = ""
    dados_mesclados["similaridade_minima"] = int(
        dados_mesclados.get("similaridade_minima", SIMILARIDADE_PADRAO)
    )
    dados_mesclados.pop("imagem_referencia", None)
    regra_encontrada.clear()
    regra_encontrada.update(dados_mesclados)

    _guardar_config(config, caminho)
    logger.info(f"Regra '{regra_id}' atualizada com sucesso.")
    return regra_encontrada


def alternar_estado_regra(regra_id: str, caminho: str = CONFIG_FILE) -> Dict[str, Any]:
    """Atalho para ativar/desativar rapidamente uma regra (toggle)."""
    regra = obter_regra(regra_id, caminho=caminho)
    if regra is None:
        raise RegraNaoEncontradaError(f"Nenhuma regra encontrada com o id '{regra_id}'.")
    novo_estado = not regra.get("ativo", False)
    return atualizar_regra(regra_id, caminho=caminho, ativo=novo_estado)


# --------------------------------------------------------------------------
# CRUD - Eliminar
# --------------------------------------------------------------------------
def eliminar_regra(regra_id: str, caminho: str = CONFIG_FILE) -> bool:
    """
    Elimina uma regra pelo id.

    Retorna True se a regra foi eliminada, False se não existia.
    """
    config = _carregar_config(caminho)
    regras_originais = config.get("regras", [])
    novas_regras = [r for r in regras_originais if r.get("id") != regra_id]

    if len(novas_regras) == len(regras_originais):
        logger.warning(f"Tentativa de eliminar regra inexistente: '{regra_id}'.")
        return False

    regra_apagada = next((r for r in regras_originais if r.get("id") == regra_id), {})
    remover_imagens_regra(regra_id, imagens_da_regra(regra_apagada))

    config["regras"] = novas_regras
    _guardar_config(config, caminho)
    logger.info(f"Regra '{regra_id}' eliminada com sucesso.")
    return True


def eliminar_todas_as_regras(caminho: str = CONFIG_FILE) -> None:
    """Remove todas as regras, mantendo a estrutura do ficheiro. Usar com cuidado."""
    config = _carregar_config(caminho)
    config["regras"] = []
    _guardar_config(config, caminho)
    logger.info("Todas as regras foram eliminadas.")


# ==========================================================================
# FUNÇÕES DE CONFIGURAÇÃO CLIP
# ==========================================================================
def obter_clip_config(caminho: str = CONFIG_FILE) -> Dict[str, Any]:
    """Devolve a configuração CLIP atual."""
    config = _carregar_config(caminho)
    return config.get("clip_config", CLIP_CONFIG_PADRAO)


def atualizar_clip_config(caminho: str = CONFIG_FILE, **campos) -> Dict[str, Any]:
    """
    Atualiza a configuração CLIP.
    
    Args:
        caminho: Caminho do ficheiro de configuração
        **campos: Campos a atualizar (model_name, device, cache_size, etc.)
    
    Returns:
        Configuração CLIP atualizada
    """
    config = _carregar_config(caminho)
    
    if "clip_config" not in config:
        config["clip_config"] = dict(CLIP_CONFIG_PADRAO)
    
    # Atualizar campos fornecidos
    for chave, valor in campos.items():
        if chave in CLIP_CONFIG_PADRAO:
            config["clip_config"][chave] = valor
        else:
            logger.warning(f"Campo de configuração CLIP desconhecido: {chave}")
    
    _guardar_config(config, caminho)
    logger.info("Configuração CLIP atualizada.")
    return config["clip_config"]


# ==========================================================================
# FUNÇÕES DE IMPORTAÇÃO/EXPORTAÇÃO
# ==========================================================================
def exportar_regras(caminho_ficheiro: str = "regras_export.json") -> str:
    """
    Exporta todas as regras para um ficheiro JSON.
    
    Args:
        caminho_ficheiro: Caminho do ficheiro onde guardar as regras
        
    Returns:
        Caminho do ficheiro criado
        
    Raises:
        RegraNaoEncontradaError: Se não houver regras para exportar
    """
    config = _carregar_config()
    regras = config.get("regras", [])
    
    if not regras:
        raise RegraNaoEncontradaError("Não há regras para exportar")
    
    try:
        with open(caminho_ficheiro, 'w', encoding='utf-8') as f:
            json.dump(regras, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Regras exportadas para {caminho_ficheiro}")
        return caminho_ficheiro
        
    except Exception as e:
        logger.error(f"Erro ao exportar regras: {e}")
        raise


def importar_regras(caminho_ficheiro: str, mesclar: bool = True) -> int:
    """
    Importa regras de um ficheiro JSON.
    
    Args:
        caminho_ficheiro: Caminho do ficheiro com as regras
        mesclar: Se True, mescla com regras existentes (evita duplicados pelo nome)
                 Se False, substitui todas as regras existentes
        
    Returns:
        Número de regras importadas
        
    Raises:
        RegraInvalidaError: Se o ficheiro tiver dados inválidos
    """
    try:
        with open(caminho_ficheiro, 'r', encoding='utf-8') as f:
            regras_importadas = json.load(f)
        
        if not isinstance(regras_importadas, list):
            raise RegraInvalidaError("O ficheiro deve conter uma lista de regras")
        
        # Validar cada regra importada
        for regra in regras_importadas:
            _validar_dados_regra(
                regra.get("nome"),
                regra.get("termo_pesquisa") or "",
                regra.get("preco_maximo"),
                regra.get("palavras_excluidas"),
                regra.get("mensagem_proposta"),
                regra.get("plataforma", "ambas"),
                imagens_da_regra(regra),
                regra.get("similaridade_minima", SIMILARIDADE_PADRAO),
            )
        
        config = _carregar_config()
        
        if mesclar:
            # Mesclar: evitar duplicados pelo nome
            regras_existentes = config.get("regras", [])
            nomes_existentes = {r["nome"] for r in regras_existentes}
            
            regras_novas = []
            for regra in regras_importadas:
                # Gerar novo ID para evitar conflitos
                regra["id"] = str(uuid.uuid4())
                
                if regra["nome"] not in nomes_existentes:
                    regras_novas.append(regra)
                    nomes_existentes.add(regra["nome"])
            
            config["regras"] = regras_existentes + regras_novas
            logger.info(f"Importadas {len(regras_novas)} regras (modo mescla)")
        else:
            # Substituir: gerar novos IDs para todas as regras
            for regra in regras_importadas:
                regra["id"] = str(uuid.uuid4())
            
            config["regras"] = regras_importadas
            logger.info(f"Substituídas todas as regras por {len(regras_importadas)} regras importadas")
        
        _guardar_config(config)
        return len(config["regras"])
        
    except json.JSONDecodeError as e:
        logger.error(f"Erro ao ler JSON: {e}")
        raise RegraInvalidaError(f"Ficheiro JSON inválido: {e}")
    except Exception as e:
        logger.error(f"Erro ao importar regras: {e}")
        raise


# --------------------------------------------------------------------------
# Bloco de testes práticos (executar com: python config_manager.py)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    TESTE_FICHEIRO = "config_teste.json"

    # Começamos sempre com um ficheiro limpo para o teste ser reprodutível
    if os.path.exists(TESTE_FICHEIRO):
        os.remove(TESTE_FICHEIRO)

    print("\n=== TESTE 1: Criar regras ===")
    regra1 = criar_regra(
        nome="Fender Stratocaster",
        termo_pesquisa="fender stratocaster",
        preco_maximo=350.0,
        palavras_excluidas=["capa", "avariado", "defeito"],
        mensagem_proposta="Olá! Tenho interesse na sua Fender Stratocaster. Aceita {preco}€?",
        ativo=True,
        plataforma="ambas",
        caminho=TESTE_FICHEIRO,
    )
    print("Regra criada:", regra1)

    regra2 = criar_regra(
        nome="PS5",
        termo_pesquisa="playstation 5",
        preco_maximo=250.0,
        palavras_excluidas=["avariada", "sem comando"],
        mensagem_proposta="Boa tarde, ainda tem a PS5 disponível?",
        ativo=True,
        plataforma="vinted",
        caminho=TESTE_FICHEIRO,
    )
    print("Regra criada:", regra2)

    regra3 = criar_regra(
        nome="iPhone 13",
        termo_pesquisa="iphone 13",
        preco_maximo=400.0,
        mensagem_proposta="Olá, tenho interesse no iPhone 13!",
        ativo=False,
        plataforma="wallapop",
        caminho=TESTE_FICHEIRO,
    )
    print("Regra criada:", regra3)

    print("\n=== TESTE 2: Listar todas as regras ===")
    todas = listar_regras(caminho=TESTE_FICHEIRO)
    for r in todas:
        print(f" - {r['nome']} | ativo={r['ativo']} | plataforma={r['plataforma']}")

    print("\n=== TESTE 3: Listar apenas regras ativas ===")
    ativas = listar_regras(apenas_ativas=True, caminho=TESTE_FICHEIRO)
    for r in ativas:
        print(f" - {r['nome']}")

    print("\n=== TESTE 4: Obter uma regra específica pelo id ===")
    encontrada = obter_regra(regra2["id"], caminho=TESTE_FICHEIRO)
    print("Regra encontrada:", encontrada)

    print("\n=== TESTE 5: Atualizar uma regra ===")
    atualizada = atualizar_regra(
        regra2["id"], caminho=TESTE_FICHEIRO, preco_maximo=220.0, ativo=True
    )
    print("Regra após atualização:", atualizada)

    print("\n=== TESTE 6: Alternar estado (toggle) de uma regra ===")
    toggled = alternar_estado_regra(regra3["id"], caminho=TESTE_FICHEIRO)
    print(f"Regra '{toggled['nome']}' passou a ativo={toggled['ativo']}")

    print("\n=== TESTE 7: Tentar criar uma regra inválida (deve falhar) ===")
    try:
        criar_regra(
            nome="",
            termo_pesquisa="teste",
            preco_maximo=100.0,
            plataforma="ambas",
            caminho=TESTE_FICHEIRO,
        )
    except RegraInvalidaError as e:
        print(f"Erro esperado capturado corretamente: {e}")

    print("\n=== TESTE 8: Tentar criar regra com plataforma inválida (deve falhar) ===")
    try:
        criar_regra(
            nome="Teste Plataforma",
            termo_pesquisa="teste",
            preco_maximo=50.0,
            plataforma="ebay",
            caminho=TESTE_FICHEIRO,
        )
    except RegraInvalidaError as e:
        print(f"Erro esperado capturado corretamente: {e}")

    print("\n=== TESTE 9: Eliminar uma regra ===")
    sucesso = eliminar_regra(regra1["id"], caminho=TESTE_FICHEIRO)
    print(f"Eliminação da regra '{regra1['nome']}' bem-sucedida? {sucesso}")

    print("\n=== TESTE 10: Tentar eliminar uma regra inexistente ===")
    sucesso_falso = eliminar_regra("id-que-nao-existe", caminho=TESTE_FICHEIRO)
    print(f"Eliminação de id inexistente retornou: {sucesso_falso}")

    print("\n=== TESTE 11: Estado final do config_teste.json ===")
    estado_final = listar_regras(caminho=TESTE_FICHEIRO)
    for r in estado_final:
        print(f" - {r['nome']} | ativo={r['ativo']} | plataforma={r['plataforma']}")

    print(f"\nFicheiro de teste gerado em: {os.path.abspath(TESTE_FICHEIRO)}")
    print("Todos os testes foram executados. Revê o conteúdo do ficheiro JSON acima.")