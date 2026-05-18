param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$SpecFile = Join-Path $ProjectRoot "shipviewer.spec"
$DistRoot = Join-Path $ProjectRoot "dist\ShipViewer"
$InternalRoot = Join-Path $DistRoot "_internal"
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

function Copy-PythonRuntimeDlls {
    param(
        [string]$PythonPath,
        [string]$TargetDir
    )

    $basePrefix = (& $PythonPath -c "import sys; print(sys.base_prefix)").Trim()
    if (-not $basePrefix) {
        throw "Unable to resolve sys.base_prefix from $PythonPath"
    }

    $dllNames = @(
        "ffi.dll",
        "ffi-7.dll",
        "ffi-8.dll",
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll",
        "libbz2.dll",
        "liblzma.dll",
        "libexpat.dll",
        "libmpdec-4.dll",
        "tcl86t.dll",
        "tk86t.dll"
    )

    $searchDirs = @(
        (Join-Path $basePrefix "Library\bin"),
        (Join-Path $basePrefix "DLLs")
    )

    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

    foreach ($dllName in $dllNames) {
        foreach ($searchDir in $searchDirs) {
            $candidate = Join-Path $searchDir $dllName
            if (Test-Path $candidate) {
                Copy-Item $candidate $TargetDir -Force
                break
            }
        }
    }
}

Copy-PythonRuntimeDlls -PythonPath $PythonExe -TargetDir $InternalRoot

$ffiDll = Join-Path $InternalRoot "ffi.dll"
if (-not (Test-Path $ffiDll)) {
    throw "Missing runtime dependency after copy: $ffiDll"
}

@'
import importlib.util
import os
import sys

dist_internal = os.path.abspath(r"dist\ShipViewer\_internal")
ctypes_pyd = os.path.join(dist_internal, "_ctypes.pyd")

if not os.path.exists(ctypes_pyd):
    raise SystemExit(f"missing packaged extension: {ctypes_pyd}")

os.add_dll_directory(dist_internal)
sys.modules.pop("_ctypes", None)
spec = importlib.util.spec_from_file_location("_ctypes", ctypes_pyd)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.__file__)
'@ | & $PythonExe - | Out-Null

$assetDirs = @(
    (Join-Path $AssetsRoot "csv"),
    (Join-Path $AssetsRoot "pdfs"),
    (Join-Path $AssetsRoot "json"),
    (Join-Path $AssetsRoot "images\devices"),
    (Join-Path $AssetsRoot "model"),
    (Join-Path $AssetsRoot "cache\models"),
    (Join-Path $AssetsRoot "backgrounds"),
    (Join-Path $AssetsRoot "water")
)

foreach ($dir in $assetDirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$csvSource = Join-Path $ProjectRoot "assets\csv"
if (Test-Path $csvSource) {
    Copy-Item (Join-Path $csvSource "*") (Join-Path $AssetsRoot "csv") -Recurse -Force
}
else {
    $csvFiles = @("damage-tree-nodes.csv")
    foreach ($csvFile in $csvFiles) {
        $source = Join-Path $ProjectRoot $csvFile
        if (Test-Path $source) {
            Copy-Item $source (Join-Path $AssetsRoot "csv") -Force
        }
    }
}

$backgroundSource = Join-Path $ProjectRoot "assets\backgrounds"
if (Test-Path $backgroundSource) {
    Copy-Item (Join-Path $backgroundSource "*.hdr") (Join-Path $AssetsRoot "backgrounds") -Force -ErrorAction SilentlyContinue
}

$jsonSource = Join-Path $ProjectRoot "assets\json"
if (Test-Path $jsonSource) {
    Copy-Item (Join-Path $jsonSource "*") (Join-Path $AssetsRoot "json") -Recurse -Force
}

$deviceImageSource = Join-Path $ProjectRoot "assets\images\devices"
if (Test-Path $deviceImageSource) {
    Copy-Item (Join-Path $deviceImageSource "*") (Join-Path $AssetsRoot "images\devices") -Recurse -Force
}

$modelSource = Join-Path $ProjectRoot "assets\model"
if (Test-Path $modelSource) {
    Copy-Item (Join-Path $modelSource "*") (Join-Path $AssetsRoot "model") -Recurse -Force
}

$modelCacheSource = Join-Path $ProjectRoot "assets\cache\models"
if (Test-Path $modelCacheSource) {
    Copy-Item (Join-Path $modelCacheSource "*") (Join-Path $AssetsRoot "cache\models") -Recurse -Force
}

$waterSource = Join-Path $ProjectRoot "assets\water"
if (Test-Path $waterSource) {
    Copy-Item (Join-Path $waterSource "*") (Join-Path $AssetsRoot "water") -Recurse -Force
}

$readmePath = Join-Path $DistRoot "README-assets.txt"
$readmeLines = @(
    "ShipViewer external assets",
    "",
    "Editable resources are stored in the assets folder next to ShipViewer.exe:",
    "- assets\csv\damage-tree-nodes.csv",
    "- assets\json\device-catalog.json",
    "- assets\json\abstract.json",
    "- assets\images\devices\*.png, *.jpg, *.jpeg",
    "- assets\model\*.3dm",
    "- assets\cache\models\*",
    "- assets\backgrounds\*.hdr",
    "- assets\water\water_diffuse.jpg, water_diffuse.png, or material.png",
    "",
    "Device highlights and detail data are loaded from assets\json\device-catalog.json.",
    "Hover summaries are loaded from assets\json\abstract.json.",
    "Prebuilt .3dm geometry cache files are loaded from assets\cache\models.",
    "Water textures are optional. If no texture is found, the app uses the default translucent water material.",
    "HDR backgrounds are scanned from assets\backgrounds. background.hdr has the highest priority, but any .hdr file can be used.",
    "Restart ShipViewer.exe after replacing CSV, JSON, images, HDR files, models, caches, or water textures."
)
$readmeLines | Set-Content -Path $readmePath -Encoding UTF8

Write-Host "Build output prepared at: $DistRoot"
