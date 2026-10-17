# Установка бота-качалки: Python, программы, настройка, автозапуск. Запускается из install.bat.
$ErrorActionPreference = 'Stop'

# curl.exe встроен в Windows 10/11 и показывает прогресс; Invoke-WebRequest на чистой Windows
# виснет до начала загрузки (ждёт движок IE) — так было в песочнице 17.10.2026
function Get-File($Url, $Out) {
    & curl.exe -L -f --retry 3 --progress-bar -o $Out $Url
    if ($LASTEXITCODE -ne 0) { throw "не скачалось: $Url" }
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bin  = Join-Path $Root 'bin'
$Data = Join-Path $Root 'data'
New-Item -ItemType Directory -Force $Bin, $Data | Out-Null

$PyDir = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312'
$Py    = Join-Path $PyDir 'python.exe'
$PyW   = Join-Path $PyDir 'pythonw.exe'

Write-Host "`n[1/6] Python 3.12"
if (-not (Test-Path $Py)) {
    $exe = Join-Path $env:TEMP 'python-3.12.7-amd64.exe'
    Write-Host '      скачиваю (~25 МБ)'
    Get-File 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' $exe
    Write-Host '      устанавливаю, 1–3 минуты, окно не закрывай'
    Start-Process $exe -Wait -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_test=0', 'Include_launcher=0', `
        'Include_doc=0', 'Include_tcltk=0', 'Include_dev=0'
    Remove-Item $exe -ErrorAction SilentlyContinue
    if (-not (Test-Path $Py)) { throw 'Python не установился' }
}
Write-Host '      есть'

Write-Host '[2/6] Библиотеки Telegram'
& $Py -m pip install --disable-pip-version-check --no-warn-script-location -q --upgrade 'telethon==1.45.0' cryptg certifi
if ($LASTEXITCODE -ne 0) { throw 'pip не поставил библиотеки' }

Write-Host '[3/6] yt-dlp'
if (-not (Test-Path "$Bin\yt-dlp.exe")) {
    Get-File 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' "$Bin\yt-dlp.exe"
}

Write-Host '[4/6] ffmpeg (~90 МБ, пару минут)'
if (-not (Test-Path "$Bin\ffmpeg.exe")) {
    $zip = Join-Path $env:TEMP 'ffmpeg-essentials.zip'
    $tmp = Join-Path $env:TEMP 'ffmpeg-essentials'
    Get-File 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' $zip
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive $zip $tmp
    Copy-Item (Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName "$Bin\ffmpeg.exe"
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '[5/6] Настройка'
if (-not (Test-Path "$Data\config.json")) {
    & $Py -I "$Root\app\bot.py" --wizard
    if ($LASTEXITCODE -ne 0) { throw 'настройка не закончена — запусти install.bat ещё раз' }
} else {
    Write-Host '      уже настроен (чтобы настроить заново — удали папку data)'
}

Write-Host '[6/6] Автозапуск'
$action   = New-ScheduledTaskAction -Execute $PyW -Argument "-I `"$Root\app\bot.py`"" -WorkingDirectory $Root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Downloader bot' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Start-Process $PyW -ArgumentList "-I `"$Root\app\bot.py`"" -WorkingDirectory $Root
Write-Host "`nГотово: бот запущен. Через минуту в группе появится закреп «🟢 На связи»."
Write-Host 'Он сам запускается при входе в Windows и сам обновляется.'
