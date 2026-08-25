@echo off
REM ============================================================
REM  build.bat
REM  Compila o Monitor de Oportunidades (Vinted & Wallapop) num
REM  executavel .exe autonomo para Windows, usando PyInstaller.
REM
REM  Como usar:
REM     1. Coloca este ficheiro na mesma pasta que:
REM        app.py, config_manager.py, scraper_engine.py, notifier.py,
REM        launcher.py e requirements.txt
REM     2. Faz duplo-clique neste ficheiro (ou corre "build.bat" numa
REM        consola cmd/PowerShell dentro dessa pasta).
REM     3. No final, o executavel fica em:  dist\MonitorOportunidades.exe
REM ============================================================

setlocal

echo ============================================
echo  Monitor de Oportunidades - Compilador .exe
echo ============================================
echo.

REM --- 1. Criar (ou reutilizar) um ambiente virtual limpo ------------
if not exist venv (
    echo [1/5] A criar ambiente virtual Python...
    python -m venv venv
) else (
    echo [1/5] Ambiente virtual ja existe, a reutilizar...
)

call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERRO: nao foi possivel ativar o ambiente virtual. Verifica se o Python esta instalado e no PATH.
    pause
    exit /b 1
)

REM --- 2. Instalar dependencias ---------------------------------------
echo [2/5] A instalar dependencias (requirements.txt + PyInstaller)...
python -m pip install --upgrade pip
pip install -r requirements.txt

REM Instala o browser Chromium do Playwright nesta maquina (nao vai
REM dentro do .exe - fica na cache local do Windows). E usado como
REM fallback quando a Wallapop bloqueia o pedido direto. Se nao for
REM possivel instalar agora (sem internet, etc.), o programa tenta
REM instalar sozinho, automaticamente, na primeira vez que for preciso.
echo [2b/5] A preparar o browser Chromium do Playwright (fallback Wallapop)...
python -m playwright install chromium
if errorlevel 1 (
    echo AVISO: nao foi possivel instalar o Chromium do Playwright agora.
    echo O programa final vai tentar instala-lo sozinho na primeira vez
    echo que precisar dele ^(precisa de ligacao a internet nessa altura^).
)

REM --- 3. Limpar builds anteriores ------------------------------------
echo [3/5] A limpar builds anteriores...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Confirma que a limpeza resultou mesmo. Se dist\MonitorOportunidades.exe
REM ainda existir aqui, esta a ser bloqueado por outro processo (o proprio
REM .exe ainda a correr, antivirus a analisa-lo, OneDrive a sincronizar,
REM etc.) e o PyInstaller so iria falhar la para o fim da compilacao, apos
REM varios minutos de trabalho. Preferimos avisar ja e parar.
if exist dist\MonitorOportunidades.exe (
    echo.
    echo ERRO: nao foi possivel apagar dist\MonitorOportunidades.exe antes de compilar.
    echo Isto normalmente significa que o ficheiro esta a ser usado por outro programa:
    echo   - Fecha o MonitorOportunidades.exe se estiver aberto ^(ve no Gestor de Tarefas^)
    echo   - Fecha o Explorador de Ficheiros se tiveres a pasta dist aberta
    echo   - Desativa temporariamente o antivirus/Windows Defender e tenta de novo
    echo   - Se o projeto estiver dentro do OneDrive, considera move-lo para uma pasta local
    pause
    exit /b 1
)

REM Mantemos o ficheiro .spec versionado; o comando abaixo gera o executavel
REM diretamente e inclui todos os modulos necessarios.

REM --- 4. Compilar com PyInstaller -------------------------------------
echo [4/5] A compilar o executavel (isto pode demorar alguns minutos)...
echo.

REM NOTA sobre o Playwright/Chromium: o "--collect-all playwright" acima
REM inclui o DRIVER do Playwright dentro do .exe (necessario para o
REM fallback da Wallapop funcionar), mas o Chromium em si (~150 MB) fica
REM sempre na cache local do Windows de cada computador, NUNCA dentro do
REM .exe (senao o executavel ficava enorme e muito lento a abrir). Por
REM isso o passo [2b/5] acima instala-o na maquina onde compilas; e em
REM qualquer outro computador onde o .exe final for usado pela primeira
REM vez, o proprio programa vai tentar descarrega-lo sozinho na primeira
REM vez que a Wallapop bloquear um pedido (fica visivel na tab "Logs do
REM Sistema"; precisa de ligacao a internet nesse momento).

REM Se tiveres um icone personalizado (icon.ico) na mesma pasta, descomenta
REM a linha "--icon=icon.ico ^" abaixo para o usar no executavel final.

python -m PyInstaller --clean --noconfirm MonitorOportunidades.spec

if errorlevel 1 (
    echo.
    echo ERRO: a compilacao falhou. Ve a seccao "Troubleshooting" do guia.
    pause
    exit /b 1
)

REM --- 5. Concluido -----------------------------------------------------
echo.
echo ============================================
echo  Compilacao concluida com sucesso!
echo  Executavel disponivel em: dist\MonitorOportunidades.exe
echo ============================================
echo.
echo Podes copiar apenas o ficheiro dist\MonitorOportunidades.exe
echo para qualquer computador Windows e corre-lo com duplo-clique.
echo.
pause