@echo off
chcp 65001 >nul
rem Подпись WebDriverAgent своим Apple ID — при установке и раз в 7 дней (21.10.2026)
echo Sideloadly: выбери iPhone, впиши свой отдельный Apple ID, перетащи WebDriverAgent.ipa в окно, нажми Start.
echo Файл WebDriverAgent.ipa сейчас откроется в Проводнике.
start "" "%LOCALAPPDATA%\Sideloadly\sideloadly.exe"
explorer /select,"%~dp0app\wda\WebDriverAgent.ipa"
pause
