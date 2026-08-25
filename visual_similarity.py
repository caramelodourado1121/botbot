"""
visual_similarity.py
====================

Módulo dedicado à gestão do modelo CLIP (Contrastive Language-Image Pre-Training)
para cálculo de similaridade visual entre imagens.

Responsabilidades:
    - Carregar o modelo CLIP uma única vez (singleton pattern)
    - Auto-detetar GPU (CUDA) ou CPU para processamento
    - Gerar embeddings de imagens usando CLIP
    - Calcular similaridade de cosseno entre embeddings
    - Fornecer interface assíncrona para processamento em lote
    - Cache de embeddings para otimização

Dependências:
    - torch, torchvision
    - CLIP (git+https://github.com/openai/CLIP.git)
    - aiohttp (para downloads assíncronos)

Autor: Implementação CLIP para similaridade visual
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from functools import lru_cache
from typing import Optional, Tuple, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor
import time
import sys
from types import ModuleType, SimpleNamespace

# openai-clip usa `pkg_resources`, removido nas setuptools recentes (Python 3.12+).
if "pkg_resources" not in sys.modules:
    try:
        import pkg_resources  # noqa: F401
    except ImportError:
        from packaging import version as _packaging_version
        _pkg_resources = ModuleType("pkg_resources")
        _pkg_resources.packaging = SimpleNamespace(version=_packaging_version)
        sys.modules["pkg_resources"] = _pkg_resources

import aiohttp
torch = None
clip = None
CLIP_DISPONIVEL = False
from PIL import Image

# --------------------------------------------------------------------------
# Configuração de logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("visual_similarity")

# --------------------------------------------------------------------------
# Configuração CLIP
# --------------------------------------------------------------------------
DEFAULT_MODEL_NAME = "ViT-B/16"
DEFAULT_CACHE_SIZE = 1000
DEFAULT_TIMEOUT = 10  # segundos
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_CONCURRENT = 10

# Thresholds recomendados por categoria de produto
THRESHOLDS_POR_CATEGORIA = {
    "eletrónica": 0.75,      # Produtos eletrónicos precisam de alta similaridade
    "roupa": 0.65,           # Roupa tolera mais variação (cores, padrões)
    "calçado": 0.70,         # Calçado precisa de similaridade moderada-alta
    "móveis": 0.60,          # Móveis toleram mais variação
    "instrumentos": 0.80,     # Instrumentos musicais precisam de alta precisão
    "livros": 0.85,          # Livros precisam de correspondência exata
    "brinquedos": 0.65,      # Brinquedos toleram variação moderada
    "desporto": 0.70,        # Equipamento desportivo - similaridade moderada
    "arte": 0.75,            # Arte e colecionáveis - alta similaridade
    "outros": 0.70,          # Categoria padrão
}

# --------------------------------------------------------------------------
# Singleton para o modelo CLIP
# --------------------------------------------------------------------------
_clip_model = None
_clip_preprocess = None
_clip_device = None
_clip_model_name = None
_clip_loaded = False
_cache_embeddings_url: Dict[str, Any] = {}


def _carregar_dependencias_clip() -> bool:
    """Carrega PyTorch/CLIP apenas quando a pesquisa visual é utilizada."""
    global torch, clip, CLIP_DISPONIVEL
    if CLIP_DISPONIVEL:
        return True
    try:
        import importlib
        torch = importlib.import_module("torch")
        clip = importlib.import_module("clip")
        CLIP_DISPONIVEL = True
        return True
    except ImportError:
        torch = None
        clip = None
        CLIP_DISPONIVEL = False
        return False


def get_clip_model(
    model_name: str = DEFAULT_MODEL_NAME,
    device: str = "auto",
    force_reload: bool = False
) -> Tuple[torch.nn.Module, Any, str]:
    """
    Devolve o modelo CLIP carregado (singleton pattern).
    
    Args:
        model_name: Nome do modelo CLIP (ex: "ViT-B/16", "ViT-B/32", "ViT-L/14")
        device: Dispositivo ("auto", "cpu", "cuda")
        force_reload: Forçar recarregamento do modelo
    
    Returns:
        Tuple com (modelo, preprocess, dispositivo)
    
    Raises:
        RuntimeError: Se não for possível carregar o modelo
    """
    global _clip_model, _clip_preprocess, _clip_device, _clip_model_name, _clip_loaded

    if not _carregar_dependencias_clip():
        raise RuntimeError(
            "A pesquisa por imagem requer PyTorch e openai-clip. "
            "Instala as dependências definidas em requirements.txt."
        )
    
    if _clip_loaded and not force_reload:
        return _clip_model, _clip_preprocess, _clip_device
    
    try:
        # Determinar dispositivo
        if device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
                logger.info("CUDA disponível - a usar GPU para CLIP")
            else:
                device = "cpu"
                logger.info("CUDA não disponível - a usar CPU para CLIP")
        elif device == "cuda" and not torch.cuda.is_available():
            logger.warning("CUDA solicitado mas não disponível - a usar CPU")
            device = "cpu"
        
        logger.info(f"A carregar modelo CLIP {model_name} no dispositivo {device}...")
        start_time = time.time()
        
        # Carregar modelo CLIP
        _clip_model, _clip_preprocess = clip.load(model_name, device=device)
        _clip_device = device
        _clip_model_name = model_name
        _clip_loaded = True
        
        load_time = time.time() - start_time
        logger.info(f"Modelo CLIP {model_name} carregado em {load_time:.2f}s no dispositivo {device}")
        
        return _clip_model, _clip_preprocess, _clip_device
        
    except Exception as e:
        logger.error(f"Erro ao carregar modelo CLIP {model_name}: {e}")
        _clip_loaded = False
        raise RuntimeError(f"Não foi possível carregar o modelo CLIP: {e}") from e


def cleanup_clip_model() -> None:
    """Liberta os recursos do modelo CLIP."""
    global _clip_model, _clip_preprocess, _clip_device, _clip_model_name, _clip_loaded
    
    if _clip_model is not None:
        del _clip_model
        _clip_model = None
    
    if _clip_preprocess is not None:
        del _clip_preprocess
        _clip_preprocess = None
    
    _clip_device = None
    _clip_model_name = None
    _clip_loaded = False
    
    # Forçar garbage collection
    _cache_embeddings_url.clear()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
    
    logger.info("Recursos do modelo CLIP libertados.")


def _abrir_imagem(dados: bytes) -> Optional[Image.Image]:
    """Abre imagem a partir de bytes e converte para RGB."""
    try:
        img = Image.open(io.BytesIO(dados))
        img.load()
        return img.convert("RGB")
    except Exception as e:
        logger.warning(f"Erro ao abrir imagem: {e}")
        return None


@lru_cache(maxsize=DEFAULT_CACHE_SIZE)
def encode_image_from_path(caminho: str) -> Optional[torch.Tensor]:
    """
    Gera embedding CLIP de uma imagem a partir do caminho do ficheiro.
    
    Usa cache LRU para otimizar chamadas repetidas.
    
    Args:
        caminho: Caminho para o ficheiro de imagem
    
    Returns:
        Tensor com embedding CLIP ou None se falhar
    """
    if not os.path.isfile(caminho):
        logger.warning(f"Ficheiro não encontrado: {caminho}")
        return None
    
    try:
        model, preprocess, device = get_clip_model()
        
        with open(caminho, "rb") as f:
            dados = f.read()
        
        img = _abrir_imagem(dados)
        if img is None:
            return None
        
        image_input = preprocess(img).unsqueeze(0).to(device)
        
        with torch.no_grad():
            embedding = model.encode_image(image_input)
        
        # Normalizar o embedding
        embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        
        return embedding.cpu().squeeze(0)  # Remover batch dimension e mover para CPU
        
    except Exception as e:
        logger.error(f"Erro ao codificar imagem {caminho}: {e}")
        return None


async def download_image_async(url: str, session: aiohttp.ClientSession, timeout: int = DEFAULT_TIMEOUT) -> Optional[bytes]:
    """
    Descarrega imagem de forma assíncrona.
    
    Args:
        url: URL da imagem
        session: Sessão aiohttp
        timeout: Timeout em segundos
    
    Returns:
        Bytes da imagem ou None se falhar
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status == 200:
                return await response.read()
            else:
                logger.warning(f"Download falhou com status {response.status}: {url}")
                return None
    except asyncio.TimeoutError:
        logger.warning(f"Timeout ao descarregar imagem: {url}")
        return None
    except Exception as e:
        logger.warning(f"Erro ao descarregar imagem {url}: {e}")
        return None


async def encode_image_from_url_async(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES
) -> Optional[torch.Tensor]:
    """
    Gera embedding CLIP de uma imagem a partir de URL de forma assíncrona.
    
    Args:
        url: URL da imagem
        timeout: Timeout em segundos
        max_retries: Número máximo de tentativas
    
    Returns:
        Tensor com embedding CLIP ou None se falhar
    """
    if not url:
        return None
    
    # Reutilizar o embedding completo evita descarregar e processar a mesma URL.
    if url in _cache_embeddings_url:
        return _cache_embeddings_url[url]
    
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                dados = await download_image_async(url, session, timeout)
                
                if dados is None:
                    if attempt < max_retries - 1:
                        # Exponential backoff
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        return None
                
                img = _abrir_imagem(dados)
                if img is None:
                    return None
                
                # Processar em thread separada para não bloquear o event loop
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as executor:
                    embedding = await loop.run_in_executor(
                        executor,
                        _encode_image_pil,
                        img
                    )
                
                if embedding is not None:
                    _cache_embeddings_url[url] = embedding
                return embedding
                
        except Exception as e:
            logger.warning(f"Tentativa {attempt + 1}/{max_retries} falhou para {url}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
    
    return None


def _encode_image_pil(img: Image.Image) -> Optional[torch.Tensor]:
    """Função auxiliar para codificar imagem PIL em thread separada."""
    try:
        model, preprocess, device = get_clip_model()
        
        image_input = preprocess(img).unsqueeze(0).to(device)
        
        with torch.no_grad():
            embedding = model.encode_image(image_input)
        
        # Normalizar o embedding
        embedding = embedding / embedding.norm(dim=-1, keepdim=True)
        
        return embedding.cpu().squeeze(0)
        
    except Exception as e:
        logger.error(f"Erro ao codificar imagem PIL: {e}")
        return None


def compute_cosine_similarity(embedding1: torch.Tensor, embedding2: torch.Tensor) -> float:
    """
    Calcula similaridade de cosseno entre dois embeddings.
    
    Os embeddings devem estar normalizados.
    
    Args:
        embedding1: Primeiro embedding
        embedding2: Segundo embedding
    
    Returns:
        Score de similaridade entre 0.0 e 1.0
    """
    try:
        if embedding1 is None or embedding2 is None:
            return 0.0
        
        # Garantir que estão na mesma device
        if embedding1.device != embedding2.device:
            embedding2 = embedding2.to(embedding1.device)
        
        # Produto escalar de embeddings normalizados = cosseno
        similarity = (embedding1 @ embedding2).item()
        
        # Garantir que está no range [0, 1]
        return max(0.0, min(1.0, similarity))
        
    except Exception as e:
        logger.error(f"Erro ao calcular similaridade de cosseno: {e}")
        return 0.0


async def batch_encode_images_async(
    urls: List[str],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES
) -> List[Optional[torch.Tensor]]:
    """
    Codifica múltiplas imagens de URLs em paralelo de forma assíncrona.
    
    Args:
        urls: Lista de URLs das imagens
        max_concurrent: Número máximo de downloads simultâneos
        timeout: Timeout por imagem em segundos
        max_retries: Número máximo de tentativas por imagem
    
    Returns:
        Lista de embeddings (mesma ordem que URLs, None para falhas)
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def encode_with_semaphore(url: str) -> Optional[torch.Tensor]:
        async with semaphore:
            return await encode_image_from_url_async(url, timeout, max_retries)
    
    tasks = [encode_with_semaphore(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    
    successful = sum(1 for r in results if r is not None)
    logger.info(f"Encoding em lote concluído: {successful}/{len(urls)} imagens processadas com sucesso")
    
    return results


def compute_similarity_with_reference(
    image_embedding: torch.Tensor,
    reference_embeddings: List[torch.Tensor]
) -> float:
    """
    Calcula a máxima similaridade entre uma imagem e múltiplas referências.
    
    Args:
        image_embedding: Embedding da imagem a comparar
        reference_embeddings: Lista de embeddings de referência
    
    Returns:
        Máxima similaridade encontrada (0.0-1.0)
    """
    if not reference_embeddings or image_embedding is None:
        return 0.0
    
    similarities = [
        compute_cosine_similarity(image_embedding, ref)
        for ref in reference_embeddings
        if ref is not None
    ]
    
    return max(similarities) if similarities else 0.0


def threshold_from_percentage(percentage: float) -> float:
    """
    Converte percentagem (0-100) para threshold CLIP (0.0-1.0).
    
    Args:
        percentage: Percentagem (0-100)
    
    Returns:
        Threshold normalizado (0.0-1.0)
    """
    return max(0.0, min(1.0, percentage / 100.0))


def percentage_from_threshold(threshold: float) -> float:
    """
    Converte threshold CLIP (0.0-1.0) para percentagem (0-100).
    
    Args:
        threshold: Threshold normalizado (0.0-1.0)
    
    Returns:
        Percentagem (0-100)
    """
    return max(0.0, min(100.0, threshold * 100))


def get_threshold_for_category(categoria: str) -> float:
    """
    Devolve o threshold recomendado para uma categoria de produto.
    
    Args:
        categoria: Nome da categoria
    
    Returns:
        Threshold recomendado (0.0-1.0)
    """
    return THRESHOLDS_POR_CATEGORIA.get(categoria.lower(), THRESHOLDS_POR_CATEGORIA["outros"])


# --------------------------------------------------------------------------
# Funções de informação do sistema
# --------------------------------------------------------------------------
def get_clip_info() -> Dict[str, Any]:
    """
    Devolve informação sobre o estado atual do modelo CLIP.
    
    Returns:
        Dicionário com informação do modelo
    """
    global _clip_model, _clip_preprocess, _clip_device, _clip_model_name, _clip_loaded
    
    return {
        "loaded": _clip_loaded,
        "model_name": _clip_model_name,
        "device": _clip_device,
        "cuda_available": bool(torch and torch.cuda.is_available()),
        "cache_size": encode_image_from_path.cache_info().currsize if _clip_loaded else 0,
        "cache_max_size": DEFAULT_CACHE_SIZE,
    }


def is_clip_available() -> bool:
    """Verifica se o CLIP está disponível e carregado."""
    try:
        get_clip_model()
        return True
    except:
        return False


# --------------------------------------------------------------------------
# Bloco de testes
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== Testes do Módulo visual_similarity.py ===\n")
    
    # Teste 1: Verificar dependências
    print("Teste 1: Verificar dependências...")
    try:
        import torch
        import clip
        import aiohttp
        print("✓ Todas as dependências estão disponíveis")
    except ImportError as e:
        print(f"✗ Dependência em falta: {e}")
        exit(1)
    
    # Teste 2: Carregar modelo
    print("\nTeste 2: Carregar modelo CLIP...")
    try:
        model, preprocess, device = get_clip_model()
        print(f"✓ Modelo carregado no dispositivo: {device}")
    except Exception as e:
        print(f"✗ Erro ao carregar modelo: {e}")
        exit(1)
    
    # Teste 3: Informação do modelo
    print("\nTeste 3: Informação do modelo...")
    info = get_clip_info()
    print(f"✓ Info: {info}")
    
    # Teste 4: Criar imagem de teste
    print("\nTeste 4: Criar imagem de teste...")
    try:
        from PIL import Image
        import numpy as np
        
        # Criar imagem simples (quadrado vermelho)
        img_array = np.zeros((100, 100, 3), dtype=np.uint8)
        img_array[:, :, 0] = 255  # Canal vermelho
        test_img = Image.fromarray(img_array, mode='RGB')
        
        # Guardar temporariamente
        test_path = "test_clip_image.png"
        test_img.save(test_path)
        print(f"✓ Imagem de teste criada: {test_path}")
    except Exception as e:
        print(f"✗ Erro ao criar imagem de teste: {e}")
        exit(1)
    
    # Teste 5: Codificar imagem
    print("\nTeste 5: Codificar imagem...")
    try:
        embedding = encode_image_from_path(test_path)
        if embedding is not None:
            print(f"✓ Embedding gerado com shape: {embedding.shape}")
        else:
            print("✗ Falha ao gerar embedding")
            exit(1)
    except Exception as e:
        print(f"✗ Erro ao codificar imagem: {e}")
        exit(1)
    
    # Teste 6: Calcular similaridade
    print("\nTeste 6: Calcular similaridade...")
    try:
        similarity = compute_cosine_similarity(embedding, embedding)
        print(f"✓ Similaridade consigo mesmo: {similarity:.4f} (deveria ser ~1.0)")
        
        # Criar outra imagem diferente
        img_array2 = np.zeros((100, 100, 3), dtype=np.uint8)
        img_array2[:, :, 1] = 255  # Canal verde
        test_img2 = Image.fromarray(img_array2, mode='RGB')
        test_path2 = "test_clip_image2.png"
        test_img2.save(test_path2)
        
        embedding2 = encode_image_from_path(test_path2)
        similarity2 = compute_cosine_similarity(embedding, embedding2)
        print(f"✓ Similaridade com imagem diferente: {similarity2:.4f} (deveria ser < 1.0)")
        
        # Limpar ficheiros de teste
        os.remove(test_path)
        os.remove(test_path2)
        print("✓ Ficheiros de teste removidos")
    except Exception as e:
        print(f"✗ Erro ao calcular similaridade: {e}")
        exit(1)
    
    # Teste 7: Thresholds por categoria
    print("\nTeste 7: Thresholds por categoria...")
    for categoria, threshold in THRESHOLDS_POR_CATEGORIA.items():
        print(f"  {categoria}: {threshold:.2f}")
    print("✓ Thresholds definidos")
    
    # Teste 8: Cleanup
    print("\nTeste 8: Cleanup do modelo...")
    try:
        cleanup_clip_model()
        print("✓ Modelo limpo com sucesso")
    except Exception as e:
        print(f"✗ Erro no cleanup: {e}")
    
    print("\n=== Todos os testes concluídos com sucesso! ===")