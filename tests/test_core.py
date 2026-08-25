import tempfile
import unittest
from pathlib import Path

import dados
import scraper_engine as se


class CoreTests(unittest.TestCase):
    def test_atualizar_publicacao_persiste_data(self):
        with tempfile.TemporaryDirectory() as pasta:
            base = str(Path(pasta) / "teste.db")
            oportunidade = {
                "plataforma": "wallapop",
                "id_artigo": "abc",
                "titulo": "Piano",
                "preco": 100,
                "moeda": "EUR",
                "url_anuncio": "https://example.test/abc",
                "url_imagem": "",
                "regra_id": "regra",
                "regra_nome": "Teste",
                "data_descoberta": "2026-08-25T00:00:00+00:00",
            }
            self.assertEqual(dados.guardar_oportunidades([oportunidade], base), 1)
            self.assertTrue(dados.atualizar_publicacao("wallapop", "abc", "2026-08-24", "ontem", base))
            linhas = dados.listar_oportunidades(caminho_bd=base)
            self.assertEqual(linhas[0]["data_publicacao"], "2026-08-24")
            self.assertEqual(linhas[0]["texto_publicacao"], "ontem")

    def test_parser_wallapop_normaliza_item(self):
        regra = {"id": "r", "nome": "Teste", "preco_maximo": 250}
        resultados = se._construir_oportunidades_wallapop([
            {
                "id": "123",
                "title": "Piano Digital",
                "price": 150,
                "currency": "EUR",
                "web_slug": "piano-digital-123",
                "images": [{"urls": {"big": "https://example.test/piano.jpg"}}],
            }
        ], regra)
        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0].id_artigo, "123")
        self.assertEqual(resultados[0].url_imagem, "https://example.test/piano.jpg")


if __name__ == "__main__":
    unittest.main()