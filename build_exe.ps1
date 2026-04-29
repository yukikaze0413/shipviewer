param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$SpecFile = Join-Path $ProjectRoot "shipviewer.spec"
$DistRoot = Join-Path $ProjectRoot "dist\ShipViewer"
$AssetsRoot = Join-Path $DistRoot "assets"

if (-not (Test-Path $PythonExe)) {
    throw "Virtual environment not found: $PythonExe"
}

if (-not $SkipBuild) {
    Push-Location $ProjectRoot
    try {
        & $PythonExe -m PyInstaller $SpecFile --noconfirm
    }
    finally {
        Pop-Location
    }
}

$assetDirs = @(
    (Join-Path $AssetsRoot "csv"),
    (Join-Path $AssetsRoot "pdfs"),
    (Join-Path $AssetsRoot "json"),
    (Join-Path $AssetsRoot "images\devices"),
    (Join-Path $AssetsRoot "backgrounds"),
    (Join-Path $AssetsRoot "water")
)

foreach ($dir in $assetDirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$csvFiles = @("damage-tree-nodes.csv", "part-pdf-catalog.csv")
foreach ($csvFile in $csvFiles) {
    $source = Join-Path $ProjectRoot $csvFile
    if (Test-Path $source) {
        Copy-Item $source (Join-Path $AssetsRoot "csv") -Force
    }
}

$backgroundSource = Join-Path $ProjectRoot "assets\backgrounds"
if (Test-Path $backgroundSource) {
    Copy-Item (Join-Path $backgroundSource "*.hdr") (Join-Path $AssetsRoot "backgrounds") -Force -ErrorAction SilentlyContinue
}

$pdfSource = Join-Path $ProjectRoot "pdfs"
if (Test-Path $pdfSource) {
    Copy-Item (Join-Path $pdfSource "*") (Join-Path $AssetsRoot "pdfs") -Recurse -Force
}

$jsonSource = Join-Path $ProjectRoot "assets\json"
if (Test-Path $jsonSource) {
    Copy-Item (Join-Path $jsonSource "*") (Join-Path $AssetsRoot "json") -Recurse -Force
}

$deviceImageSource = Join-Path $ProjectRoot "assets\images\devices"
if (Test-Path $deviceImageSource) {
    Copy-Item (Join-Path $deviceImageSource "*") (Join-Path $AssetsRoot "images\devices") -Recurse -Force
}

$waterSource = Join-Path $ProjectRoot "assets\water"
if (Test-Path $waterSource) {
    Copy-Item (Join-Path $waterSource "*") (Join-Path $AssetsRoot "water") -Recurse -Force
}

$readmePath = Join-Path $DistRoot "README-assets.txt"
@"
ShipViewer external assets

Editable files are stored in the assets folder next to ShipViewer.exe:
- assets\csv\damage-tree-nodes.csv
- assets\csv\part-pdf-catalog.csv
- assets\json\device-catalog.json
- assets\images\devices\*.png, *.jpg, *.jpeg
- assets\pdfs\*.pdf
- assets\backgrounds\*.hdr
- assets\water\water_diffuse.jpg, water_diffuse.png, or material.png

In part-pdf-catalog.csv, document paths can be file names such as test.pdf,
or relative paths such as assets/pdfs/test.pdf.
Water textures are optional. If no water texture is found, ShipViewer uses
a simple translucent water surface.
HDR backgrounds are scanned from assets/backgrounds. Use background.hdr for
the highest priority, or place any .hdr file in that folder.
After replacing CSV, JSON, images, PDF, HDR, or water texture files, restart ShipViewer.exe
to reload them.
"@ | Set-Content -Path $readmePath -Encoding UTF8

Write-Host "Build output prepared at: $DistRoot"
