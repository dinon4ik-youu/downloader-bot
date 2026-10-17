# Установка бота-качалки: Python, программы, настройка, автозапуск. Запускается из install.bat.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'   # без этого Invoke-WebRequest качает в разы медленнее

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
    Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile $exe
    Start-Process $exe -Wait -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_test=0', 'Include_launcher=0'
    Remove-Item $exe -ErrorAction SilentlyContinue
    if (-not (Test-Path $Py)) { throw 'Python не установился' }
}
Write-Host '      есть'

Write-Host '[2/6] Библиотеки Telegram'
& $Py -m pip install --disable-pip-version-check -q --upgrade 'telethon==1.45.0' cryptg
if ($LASTEXITCODE -ne 0) { throw 'pip не поставил библиотеки' }

Write-Host '[3/6] yt-dlp'
if (-not (Test-Path "$Bin\yt-dlp.exe")) {
    Invoke-WebRequest 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile "$Bin\yt-dlp.exe"
}

Write-Host '[4/6] ffmpeg (~90 МБ, пару минут)'
if (-not (Test-Path "$Bin\ffmpeg.exe")) {
    $zip = Join-Path $env:TEMP 'ffmpeg-essentials.zip'
    $tmp = Join-Path $env:TEMP 'ffmpeg-essentials'
    Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zip
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

Write-Host '[6/6] Автозапуск и ярлыки'
$action   = New-ScheduledTaskAction -Execute $PyW -Argument "-I `"$Root\app\bot.py`"" -WorkingDirectory $Root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Downloader bot' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

$desk = [Environment]::GetFolderPath('Desktop')
$sh = New-Object -ComObject WScript.Shell
foreach ($s in @(@('Включить бота-качалку', 'start.bat'), @('Выключить бота-качалку', 'stop.bat'))) {
    $l = $sh.CreateShortcut((Join-Path $desk "$($s[0]).lnk"))
    $l.TargetPath = Join-Path $Root $s[1]
    $l.WorkingDirectory = $Root
    $l.WindowStyle = 7
    $l.Save()
}

Start-Process $PyW -ArgumentList "-I `"$Root\app\bot.py`"" -WorkingDirectory $Root
Write-Host "`nГотово: бот запущен. Через минуту в группе появится закреп «🟢 На связи»."
Write-Host 'Он сам запускается при входе в Windows и сам обновляется.'
