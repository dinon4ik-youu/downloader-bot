# Один раз (от администратора): задача Планировщика «WDA tunnel» — туннель к iPhone с наивысшими правами,
# при входе в Windows и по запросу start.py (schtasks /run). После неё start.py больше не спрашивает UAC,
# и прогон может поднять связь сам, пока Димы нет у ноутбука (19.10.2026).
# Что пошло не так — пишется в tunnel-task.log рядом.
# Набор (21.10.2026): -Py <python.exe набора> — туннель через «python -m pymobiledevice3», -Out — куда писать лог/метку.
param([string]$Py = "", [string]$Out = $PSScriptRoot)
$log = Join-Path $Out "tunnel-task.log"
try {
    if ($Py) { $cmd = "& '$Py' -m pymobiledevice3 remote tunneld"; $py = $Py }
    else { $py = (Get-Command pymobiledevice3 -ErrorAction Stop).Source; $cmd = "& '$py' remote tunneld" }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "WDA tunnel" -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Force -ErrorAction Stop | Out-Null
    "ok" | Out-File -Encoding utf8 (Join-Path $Out "tunnel-task.ok")
    "$(Get-Date) ok: $py, $user" | Out-File -Encoding utf8 $log
    Write-Host "Gotovo: zadacha WDA tunnel sozdana"
} catch {
    "$(Get-Date) ОШИБКА: $($_.Exception.Message)`n$($_.ScriptStackTrace)" | Out-File -Encoding utf8 $log
    Write-Host "OSHIBKA: $($_.Exception.Message)"
}
Start-Sleep 8
