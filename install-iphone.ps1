# Айфон-часть набора: прогон видео через настоящий Instagram Edits на iPhone (21.10.2026).
# Запускается из install.ps1 (вопрос «Ставить айфон-часть?») или «Поставить айфон-часть.bat».
# Ставит то, что нужно на компьютере; шаги на самом айфоне — в INSTALL-AI.md (там картинки и «если сломалось»).
$ErrorActionPreference = 'Stop'

function Get-File($Url, $Out) {
    & curl.exe -L -f --retry 3 --progress-bar -o $Out $Url
    if ($LASTEXITCODE -ne 0) { throw "не скачалось: $Url" }
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bin  = Join-Path $Root 'bin'
$Data = Join-Path $Root 'data'
$Wda  = Join-Path $Data 'wda'
New-Item -ItemType Directory -Force $Bin, $Wda | Out-Null
$Py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $Py)) { throw 'нет Python набора — сначала install.bat' }

Write-Host "`n[айфон 1/6] Библиотеки для iPhone (pymobiledevice3), пару минут"
& $Py -m pip install --disable-pip-version-check --no-warn-script-location -q --upgrade pymobiledevice3 psutil pillow
if ($LASTEXITCODE -ne 0) { throw 'pip не поставил pymobiledevice3' }

Write-Host '[айфон 2/6] ffprobe (размеры и длина видео)'
if (-not (Test-Path "$Bin\ffprobe.exe")) {
    $zip = Join-Path $env:TEMP 'ffmpeg-essentials.zip'
    $tmp = Join-Path $env:TEMP 'ffmpeg-essentials'
    Get-File 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $zip
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive $zip $tmp
    Copy-Item (Get-ChildItem $tmp -Recurse -Filter ffprobe.exe | Select-Object -First 1).FullName "$Bin\ffprobe.exe"
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host '      есть'

Write-Host '[айфон 3/6] Драйвер iPhone (Apple Devices из Microsoft Store)'
$hasApple = (Get-AppxPackage 'AppleInc.AppleDevices' -ErrorAction SilentlyContinue) -or
            (Get-Service 'Apple Mobile Device Service' -ErrorAction SilentlyContinue)
if (-not $hasApple) {
    $ok3 = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {   # на чистой Windows winget бывает не установлен (песочница 21.10)
        Write-Host '      ставлю через winget — если спросит, соглашайся'
        & winget install --id 9NP83LWLPZ9K --source msstore --accept-package-agreements --accept-source-agreements
        $ok3 = ($LASTEXITCODE -eq 0)
    }
    if (-not $ok3) {
        Write-Host '      winget нет или не вышло — открываю «Apple Devices» в Microsoft Store: нажми «Получить»,'
        Write-Host '      потом запусти «Поставить айфон-часть.bat» ещё раз (остальные шаги идут дальше)'
        Start-Process 'ms-windows-store://pdp/?ProductId=9NP83LWLPZ9K' -ErrorAction SilentlyContinue
    }
} else { Write-Host '      есть' }

Write-Host '[айфон 4/6] Туннель к iPhone (один раз окно Windows «Да»)'
$ok = Join-Path $Wda 'tunnel-task.ok'
Remove-Item $ok -ErrorAction SilentlyContinue
Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    "`"$Root\app\wda\tunnel-task.ps1`"", '-Py', "`"$Py`"", '-Out', "`"$Wda`""
if (Test-Path $ok) { Write-Host '      задача «WDA tunnel» создана' }
else { Write-Host "      не создана — нажми «Да» в окне Windows; подробности в $Wda\tunnel-task.log" }

Write-Host '[айфон 5/6] Sideloadly — подписать WebDriverAgent своим Apple ID (раз в 7 дней)'
if (-not (Test-Path (Join-Path $env:LOCALAPPDATA 'Sideloadly\sideloadly.exe'))) {
    $exe = Join-Path $env:TEMP 'SideloadlySetup64.exe'
    Write-Host '      скачиваю (~140 МБ)'
    Get-File 'https://sideloadly.io/SideloadlySetup64.exe' $exe
    Write-Host '      открываю установщик — «Далее» до конца'
    # не -Wait: он ждёт и Sideloadly, который установщик открывает в конце (песочница 21.10)
    (Start-Process $exe -PassThru).WaitForExit()
    Remove-Item $exe -ErrorAction SilentlyContinue
} else { Write-Host '      есть' }

Write-Host '[айфон 6/6] Включаю в боте темы «Кинуть в Edits» и «Вопросы прогона»'
& $Py -I -c "import json,pathlib; p=pathlib.Path(r'$Data\config.json'); c=json.loads(p.read_text(encoding='utf-8')); c.setdefault('edits_topic', {}); p.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding='utf-8')"
if ($LASTEXITCODE -ne 0) { throw 'не записал edits_topic в data\config.json — бот настроен? (install.bat)' }

Write-Host "`nКомпьютер готов. Дальше — шаги на самом айфоне: файл INSTALL-AI.md (или «iPhone» в README)."
Write-Host 'Проверка связи после них:'
Write-Host "  `"$Py`" `"$Root\app\wda\start.py`""
