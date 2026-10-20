Add-Type -AssemblyName System.Drawing

$size = 256
$bmp = New-Object System.Drawing.Bitmap($size, $size)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'
$g.TextRenderingHint = 'ClearTypeGridFit'
$g.Clear([System.Drawing.Color]::Transparent)

# rounded dark square (the dock pill, squarified)
$radius = 56
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$path.AddArc(0, 0, $radius * 2, $radius * 2, 180, 90)
$path.AddArc($size - $radius * 2, 0, $radius * 2, $radius * 2, 270, 90)
$path.AddArc($size - $radius * 2, $size - $radius * 2, $radius * 2, $radius * 2, 0, 90)
$path.AddArc(0, $size - $radius * 2, $radius * 2, $radius * 2, 90, 90)
$path.CloseFigure()
$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 20, 23, 31))
$g.FillPath($brush, $path)

# green mic-activity pill
$penBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 52, 211, 153))
$g.FillRectangle($penBrush, 24, 104, 64, 48)

# status dot
$g.FillEllipse($penBrush, 98, 110, 36, 36)

# "shh" wordmark
$font = New-Object System.Drawing.Font('Segoe UI', 50, `
    [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 231, 233, 238))
$g.DrawString('shh', $font, $white, 142, 94)

$g.Dispose()

$png = Join-Path $env:TEMP 'shhdock_icon.png'
$bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

# wrap the PNG in an ICO container (PNG-in-ICO, legal since Vista)
$pngBytes = [System.IO.File]::ReadAllBytes($png)
$ms = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($ms)
$bw.Write([uint16]0)          # reserved
$bw.Write([uint16]1)          # type: icon
$bw.Write([uint16]1)          # count: 1
$bw.Write([byte]0)            # width  (0 = 256)
$bw.Write([byte]0)            # height (0 = 256)
$bw.Write([byte]0)            # colors
$bw.Write([byte]0)            # reserved
$bw.Write([uint16]1)          # planes
$bw.Write([uint16]32)         # bpp
$bw.Write([uint32]$pngBytes.Length)
$bw.Write([uint32]22)         # offset (6 + 16)
$bw.Write($pngBytes)
$bw.Flush()
$ico = Join-Path $PSScriptRoot 'shhdock.ico'
[System.IO.File]::WriteAllBytes($ico, $ms.ToArray())
$bw.Dispose(); $ms.Dispose()

Write-Output "icon written: $ico ($([math]::Round((Get-Item $ico).Length / 1KB, 1)) KB)"
