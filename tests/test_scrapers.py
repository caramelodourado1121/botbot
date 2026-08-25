import unittest
from unittest.mock import Mock, patch

import scraper_engine as se


def _regra(**campos):
    regra = {
        "id": "regra-teste",
        "nome": "Regra de teste",
        "termo_pesquisa": "piano",
        "preco_minimo": 20,
        "preco_maximo": 250,
        "palavras_excluidas": [],
    }
    regra.update(campos)
    return regra


class ScraperTests(unittest.TestCase):
    def test_preco_facebook_normaliza_formatos(self):
        self.assertEqual(se._preco_facebook("1.250,50 €"), 1250.50)
        self.assertEqual(se._preco_facebook("75 €"), 75)
        self.assertEqual(se._preco_facebook({"amount": "120,25"}), 120.25)

    def test_parser_facebook_aplica_preco_e_exclusoes(self):
        listings = [
            {"id": "1", "titulo": "Piano digital", "preco": "200 €", "url": "https://facebook.com/marketplace/item/1"},
            {"id": "2", "titulo": "Piano avariado", "preco": "100 €", "url": "https://facebook.com/marketplace/item/2"},
        ]
        resultados = se._construir_oportunidades_facebook(
            listings, _regra(palavras_excluidas=["avariado"])
        )
        self.assertEqual([item.id_artigo for item in resultados], ["1"])

    def test_url_facebook_inclui_local_e_preco(self):
        url = se._url_pesquisa_facebook("piano", _regra(preco_maximo=250))
        self.assertIn(f"/marketplace/{se.FACEBOOK_MARKETPLACE_LOCAL}/search?", url)
        self.assertIn("query=piano", url)
        self.assertIn("maxPrice=250", url)

    def test_pedido_seguro_trata_429_sem_lancar_excecao(self):
        resposta = Mock(status_code=429)
        with patch.object(se, "_obter_sessao_http") as obter_sessao:
            obter_sessao.return_value.get.return_value = resposta
            self.assertIsNone(se._pedido_seguro("https://example.test/search"))
