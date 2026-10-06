# Mail WebUI - system tray controller (no third-party dependency)
# Double click "MailTray.bat" on the Desktop to run this.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# 单例：避免重复启动多个托盘
$created = $false
$mtx = New-Object System.Threading.Mutex($true, "Global\MailWebUITray", [ref]$created)
if (-not $created) { exit }

$root = Split-Path -Parent $PSScriptRoot
$port = 8791

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
$py   = Resolve-Python
$url  = "http://127.0.0.1:$port/"
$log  = Join-Path $root "data\server.log"
$errLog = Join-Path $root "data\server.err.log"

function Get-ServerPid {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($c) { return ($c | Select-Object -First 1).OwningProcess }
    return $null
}

function Start-Server {
    if (Get-ServerPid) { return $false }
    Start-Process -FilePath $py `
        -ArgumentList @("run.py", "serve", "--host", "127.0.0.1", "--port", "$port") `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError $errLog | Out-Null
    return $true
}

function Stop-Server {
    $p = Get-ServerPid
    if ($p) {
        Stop-Process -Id ($p | Select-Object -First 1) -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

function Test-ServerApi {
    try {
        $r = Invoke-RestMethod -Uri "$($url)api/status" -TimeoutSec 3
        return ($r.ok -eq $true)
    } catch { return $false }
}

# ---------------- tray ----------------
$icon = New-Object System.Windows.Forms.NotifyIcon
# 应用图标：assets\mail-app.ico（水墨信封 + 朱砂印）；缺失时退回系统图标
$iconPath = Join-Path $root "assets\mail-app.ico"
if (Test-Path $iconPath) {
    $icon.Icon = New-Object System.Drawing.Icon($iconPath, 16, 16)
} else {
    $icon.Icon = [System.Drawing.SystemIcons]::Application
}
$icon.Text = "Mail WebUI"
$icon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$miOpen = $menu.Items.Add("打开信箱")
$miOpen.add_Click({ Start-Process $url })

$miStatus = $menu.Items.Add("状态：检查中…")
$miStatus.Enabled = $false

$menu.Items.Add("-") | Out-Null

$miSync = $menu.Items.Add("立即同步邮件")
$miSync.add_Click({
    try {
        # 时间窗与 WebUI 默认一致：近 1 月（滚动值，别写死某一天）
        $since = (Get-Date).AddDays(-30).ToString("yyyy-MM-dd")
        $body  = "{""all_folders"":true,""since"":""$since"",""with_body"":true}"
        Invoke-RestMethod -Method Post -Uri "$($url)api/sync" -TimeoutSec 300 `
            -ContentType "application/json" -Body $body | Out-Null
        $icon.ShowBalloonTip(3000, "Mail WebUI", "同步完成（近 1 月）", "Info")
    } catch {
        $err = $_.Exception.Message
        if ($err -match "429|正在进行") { $err = "已有同步在进行（可能是后台自动同步），稍后再试" }
        $icon.ShowBalloonTip(3000, "Mail WebUI", "同步失败：$err", "Error")
    }
    Update-Status
})

$miRestart = $menu.Items.Add("重启服务")
$miRestart.add_Click({
    Stop-Server
    Start-Server | Out-Null
    Start-Sleep -Seconds 2
    $icon.ShowBalloonTip(3000, "Mail WebUI", "服务已重启", "Info")
    Update-Status
})

$miLog = $menu.Items.Add("查看日志")
$miLog.add_Click({ if (Test-Path $log) { Start-Process notepad.exe $log } })

$menu.Items.Add("-") | Out-Null

# 退出 = 真正退出：停服务 + 移除托盘图标
$miExit = $menu.Items.Add("退出（停止服务）")
$miExit.add_Click({
    $timer.Stop()
    Stop-Server
    $icon.Visible = $false
    $icon.Dispose()
    $context.ExitThread()
})

$icon.ContextMenuStrip = $menu
$icon.add_Click({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        if (Get-ServerPid) { Start-Process $url } else { Start-Server | Out-Null; Start-Sleep -Seconds 2; Start-Process $url }
        Update-Status
    }
})

# 气泡被点击也打开信箱
$icon.add_BalloonTipClicked({ Start-Process $url })

function Update-Status {
    if (Get-ServerPid) {
        if (Test-ServerApi) {
            $icon.Text = "Mail WebUI - 运行中 ($port)"
            $miStatus.Text = "状态：运行中"
        } else {
            $icon.Text = "Mail WebUI - 启动中…"
            $miStatus.Text = "状态：启动中…"
        }
    } else {
        $icon.Text = "Mail WebUI - 已停止"
        $miStatus.Text = "状态：已停止"
    }
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.add_Tick({ Update-Status })
$timer.Start()

# 后台启动服务，然后常驻托盘
Start-Server | Out-Null
Update-Status
$icon.ShowBalloonTip(4000, "Mail WebUI", "已在后台运行（$url）`n左键图标打开信箱，右键可退出", "Info")

$context = New-Object System.Windows.Forms.ApplicationContext
[System.Windows.Forms.Application]::Run($context)
