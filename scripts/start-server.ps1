# Mail WebUI - silent starter (no window at all)
# Started by the Desktop shortcut; runs hidden, waits for the port,
# then opens the browser. Safe to run when the server is already up.
$root  = Split-Path -Parent $PSScriptRoot
$port  = 8791

# 解释器解析顺序：$env:MAILUI_PYTHON > scripts\python.local.txt > 项目内 .venv > PATH 上的 python
function Resolve-Python {
    if ($env:MAILUI_PYTHON -and (Test-Path $env:MAILUI_PYTHON)) { return $env:MAILUI_PYTHON }
    $local = Join-Path $PSScriptRoot "python.local.txt"
    if (Test-Path $local) {
        $p = (Get-Content $local -Raw).Trim()
        if ($p -and (Test-Path $p)) { return $p }
    }
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'Python not found. Set $env:MAILUI_PYTHON, write the path into scripts\python.local.txt, or put python on PATH.'
}
$py    = Resolve-Python
$url   = "http://127.0.0.1:$port/"
$log   = Join-Path $root "data\server.log"
$errLog = Join-Path $root "data\server.err.log"

function Test-Port {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-Port)) {
    Start-Process -FilePath $py `
        -ArgumentList @("run.py", "serve", "--host", "127.0.0.1", "--port", "$port") `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError $errLog | Out-Null

    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Port) { break }
    }
}

if (Test-Port) {
    Start-Process $url
} else {
    # 启动失败时留个可见提示，避免"静默地什么都没发生"
    $msg = "Mail WebUI failed to start on port $port.`nSee: $log"
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($msg, "Mail WebUI", "OK", "Error") | Out-Null
}
