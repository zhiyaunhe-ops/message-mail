@echo off
rem Mail WebUI launcher - starts the tray icon, which pulls up the backend server.
rem Left-click tray icon  -> open mailbox
rem Right-click tray icon -> open / sync now / restart / view log / quit
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\Download\workworkwork\python_playground\Message-cli\scripts\MailTray.ps1"
