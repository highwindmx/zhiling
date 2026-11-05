<#
 从 build/bench-result.json 画一张体积/内存/CPU/首帧的对比图，输出 docs/screenshots/bench-compare.png。
 只用 .NET System.Drawing，不引第三方库。

 用法: powershell -NoProfile -File build/bench-chart.ps1
#>
param(
  [string]$Result = (Join-Path $PSScriptRoot 'bench-result.json'),
  [string]$Out = (Join-Path (Split-Path $PSScriptRoot -Parent) 'docs\screenshots\bench-compare.png')
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$r = Get-Content $Result -Raw -Encoding UTF8 | ConvertFrom-Json
$t = $r.tauri
$m = $r.mini
$s = $r.sizes

$W = 1300; $H = 900
$bmp = New-Object System.Drawing.Bitmap($W, $H)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit

$bg = [System.Drawing.Color]::FromArgb(250, 250, 252)
$g.Clear($bg)
$ink = [System.Drawing.Color]::FromArgb(32, 36, 44)
$sub = [System.Drawing.Color]::FromArgb(110, 118, 132)
$grid = [System.Drawing.Color]::FromArgb(226, 229, 235)
$cTauri = [System.Drawing.Color]::FromArgb(52, 120, 246)
$cMini = [System.Drawing.Color]::FromArgb(240, 138, 63)

$fTitle = New-Object System.Drawing.Font('Microsoft YaHei', 20, [System.Drawing.FontStyle]::Bold)
$fHead = New-Object System.Drawing.Font('Microsoft YaHei', 13, [System.Drawing.FontStyle]::Bold)
$fBody = New-Object System.Drawing.Font('Microsoft YaHei', 11)
$fSmall = New-Object System.Drawing.Font('Microsoft YaHei', 10)

$g.DrawString('deskpet-tauri (Tauri/WebView2) vs deskpet-python (PyQt6)', $fTitle, (New-Object System.Drawing.SolidBrush($ink)), 40, 24)
$g.DrawString(("同窗口尺寸（屏幕宽 15% = 192×177 逻辑像素）、同素材（mini 的 4 段 WebP q80）、待机循环采样 {0}s" -f $r.seconds), $fSmall, (New-Object System.Drawing.SolidBrush($sub)), 42, 58)

function Draw-Panel {
  param($Title, $X, $Y, $W, $Max, $Unit, $Rows)
  $g.DrawString($Title, $fHead, (New-Object System.Drawing.SolidBrush($ink)), $X, $Y)
  $baseY = $Y + 32
  $barH = 26; $gap = 12
  $labelW = 170; $barMaxW = $W - $labelW - 130
  $i = 0
  foreach ($row in $Rows) {
    $yy = $baseY + $i * ($barH + $gap)
    $g.DrawString($row.Name, $fBody, (New-Object System.Drawing.SolidBrush($ink)), $X, $yy + 4)
    $bw = [Math]::Max(2, [int]($barMaxW * $row.Value / $Max))
    $brush = New-Object System.Drawing.SolidBrush($row.Color)
    $g.FillRectangle($brush, $X + $labelW, $yy, $bw, $barH)
    $txt = ('{0:N1} {1}' -f $row.Value, $Unit)
    if ($row.Note) { $txt += '   ' + $row.Note }
    $g.DrawString($txt, $fBody, (New-Object System.Drawing.SolidBrush($ink)), ($X + $labelW + $bw + 8), ($yy + 4))
    $i++
  }
  $g.DrawLine((New-Object System.Drawing.Pen($grid, 1)), $X, ($baseY + $i * ($barH + $gap)), ($X + $W - 40), ($baseY + $i * ($barH + $gap)))
}

Draw-Panel '体积（可分发，MB）' 40 90 1220 1150 'MB' @(
  @{ Name = 'Tauri 仅本体（exe）'; Value = $s.tauri_exe_mb; Color = $cTauri },
  @{ Name = 'mini 仅本体（py+素材）'; Value = $s.mini_own_mb; Color = $cMini },
  @{ Name = 'Tauri 含 WebView2'; Value = ($s.tauri_exe_mb + $s.webview2_mb); Color = $cTauri; Note = '（系统通常预装）' },
  @{ Name = 'mini 含 PyQt6'; Value = $s.mini_with_pyqt_mb; Color = $cMini }
)

Draw-Panel '待机内存（WorkingSetSize，含子进程，MB）' 40 310 1220 600 'MB' @(
  @{ Name = 'Tauri 进程树（8 个进程）'; Value = $t.wss_mean_mb; Color = $cTauri; Note = ('峰值 {0:N1}' -f $t.wss_max_mb) },
  @{ Name = 'mini 单进程'; Value = $m.wss_mean_mb; Color = $cMini; Note = ('峰值 {0:N1}' -f $m.wss_max_mb) }
)

Draw-Panel '待机 CPU（单核 %）' 40 470 600 30 '%' @(
  @{ Name = 'Tauri 进程树'; Value = $t.cpu_pct; Color = $cTauri },
  @{ Name = 'mini'; Value = $m.cpu_pct; Color = $cMini }
)

Draw-Panel '首帧时间（ms）' 680 470 580 1200 'ms' @(
  @{ Name = 'Tauri 自计时'; Value = $t.first_show_ms; Color = $cTauri },
  @{ Name = 'Tauri 外部可见'; Value = $t.first_visible_ms; Color = $cTauri },
  @{ Name = 'mini 自计时'; Value = $m.first_paint_ms; Color = $cMini },
  @{ Name = 'mini 外部可见'; Value = $m.first_visible_ms; Color = $cMini }
)

# ---- 分解表：Tauri 进程树（每行 3 列，避免重叠）----------------------------
$g.DrawString('Tauri 进程树内存分解（均值，MB）', $fHead, (New-Object System.Drawing.SolidBrush($ink)), 40, 660)
$i = 0
foreach ($k in $t.roles.PSObject.Properties.Name) {
  $v = $t.roles.$k
  $col = $i % 3
  $row = [Math]::Floor($i / 3)
  $g.DrawString(('{0}  {1:N1}' -f $k, $v.mean_mb), $fSmall, (New-Object System.Drawing.SolidBrush($ink)), (40 + $col * 400), (690 + $row * 24))
  $i++
}

$g.DrawString('注：CPU = GetProcessTimes 增量 / 墙钟，折算单核百分比；首帧"自计时"起点为各程序进程内主函数入口，"外部可见"为 spawn → EnumWindows 看到可见窗口。',
  $fSmall, (New-Object System.Drawing.SolidBrush($sub)), 40, 866)

New-Item -ItemType Directory -Force -Path (Split-Path $Out -Parent) | Out-Null
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "wrote $Out"
