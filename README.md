# Monitor de Oportunidades

Aplicação Streamlit para monitorizar anúncios em Vinted, Wallapop, OLX e Facebook Marketplace.

## Executar

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Na primeira execução, a aplicação cria `config.json`, `dados.db`, `vistos.json` e `settings.json` localmente. Estes ficheiros podem conter regras, histórico ou credenciais e não são versionados. Consulta `config.example.json` e `settings.example.json` como referência.

## Testes

```powershell
python -m pytest tests -q
```

## Executável Windows

Executa `build.bat` para gerar `dist\MonitorOportunidades.exe`. O script instala as dependências de compilação e o Chromium usado pelo fallback de browser.
