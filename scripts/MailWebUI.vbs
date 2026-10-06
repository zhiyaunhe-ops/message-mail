' Mail WebUI launcher - double-click, zero window.
' Starts the tray icon (scripts\MailTray.ps1) which pulls up the backend server.
'
'   Left-click tray icon  -> open mailbox
'   Right-click tray icon -> open / sync now / restart / view log / quit
'
' To use a mailbox-style icon: right-click this file > Properties > Change Icon,
'   point to C:\Windows\System32\imageres.dll (envelope icons inside).
' NOTE: keep this file pure ASCII (no Chinese comments) - wscript reads .vbs
'   as ANSI/GBK and Chinese text causes runtime error 800A0005.
Option Explicit

Dim fso, root, ps, cmd, sh, q
' Derive the project root from this script's own location (scripts\ is one level down),
' so the launcher keeps working if the project folder is moved or renamed.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
ps   = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
q    = Chr(34)

Set sh = CreateObject("WScript.Shell")
' MailTray.ps1 starts the server if not already running, then stays resident hidden.
cmd = q & ps & q & " -ExecutionPolicy Bypass -WindowStyle Hidden -NoProfile -File " & q & root & "\scripts\MailTray.ps1" & q
sh.Run cmd, 0, False
