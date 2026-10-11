<#
.SYNOPSIS
  Builds Skate 3 Recomp with the music mod for Windows and packages it as a zip.

.DESCRIPTION
  The build recompiles the game's own code, so it needs your Skate 3 game files: the
  "game" folder the official release creates when you install from your ISO. Start the
  game once after installing so it applies the title update (that adds default.xexp to the
  game folder), or pass the title update package with -TitleUpdate.

  One-time setup (PowerShell):
    winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
    winget install -e --id LLVM.LLVM
    winget install -e --id Kitware.CMake
    winget install -e --id Ninja-build.Ninja
    winget install -e --id Python.Python.3.12
    winget install -e --id Git.Git
  Close and reopen PowerShell afterwards so the new tools are on PATH.

  The result is dist\Skate3Recomp-MusicMod-Windows.zip: skate3.exe, its DLLs and the
  music_mod folder. Extract it, move your "game" folder next to skate3.exe (or copy
  skate3.exe and rexruntime.dll over an existing install), add songs to "My Music" and
  double-click music_mod\Install Music Mod.bat.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -GameDir "C:\Games\Skate3\game"
#>
param(
  [Parameter(Mandatory = $true)][string]$GameDir,
  [string]$TitleUpdate = "",
  [switch]$Regenerate
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

function Fail($msg) {
  Write-Host ""
  Write-Host "Error: $msg" -ForegroundColor Red
  exit 1
}

function Run($exe, [string[]]$argv) {
  Write-Host "> $exe $($argv -join ' ')" -ForegroundColor DarkGray
  & $exe @argv
  if ($LASTEXITCODE -ne 0) { Fail "$exe failed (exit code $LASTEXITCODE)" }
}

# ---- tools
$tools = [ordered]@{
  "clang"  = "winget install -e --id LLVM.LLVM";
  "cmake"  = "winget install -e --id Kitware.CMake";
  "ninja"  = "winget install -e --id Ninja-build.Ninja";
  "python" = "winget install -e --id Python.Python.3.12";
  "git"    = "winget install -e --id Git.Git"
}
$missing = @($tools.Keys | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) })
if ($missing.Count -gt 0) {
  $hints = ($missing | ForEach-Object { "  " + $tools[$_] }) -join "`n"
  Fail "missing build tools: $($missing -join ', '). Install them with:`n$hints`nthen reopen PowerShell."
}

# ---- game files
$GameDir = (Resolve-Path $GameDir).Path
if (-not (Test-Path (Join-Path $GameDir "default.xex"))) {
  Fail "$GameDir has no default.xex - pass the game folder the official release installed from your ISO."
}
$cmakeArgs = @("-DSKATE3_GAME_DATA_ROOT=$GameDir", "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++")
if ($TitleUpdate -ne "") {
  $cmakeArgs += "-DSKATE3_TITLE_UPDATE_PACKAGE=$((Resolve-Path $TitleUpdate).Path)"
} elseif ((Test-Path (Join-Path $GameDir "default.xexp")) -and (Test-Path (Join-Path $GameDir "data\webkit\EAWebkit.xexp"))) {
  $cmakeArgs += "-DSKATE3_TITLE_UPDATE_STAGED_ROOT=$GameDir"
} else {
  Fail "the title update isn't in $GameDir yet. Start the game once with the official release so it installs the update, or pass -TitleUpdate <path to TU_12K2276_000000C000000.00000000000O3>."
}

# ---- sources
if (-not (Test-Path "third_party\rexglue-sdk\CMakeLists.txt") -or -not (Test-Path "third_party\rexglue-sdk\thirdparty\imgui\imgui.h")) {
  Run "git" @("submodule", "update", "--init", "--recursive", "--jobs", "8")
}

# ---- build (codegen first, then CMake again so it sees the generated sources)
Run "cmake" (@("--preset", "release") + $cmakeArgs)
if ($Regenerate -or -not (Test-Path "generated\sources.cmake")) {
  Run "cmake" @("--build", "--preset", "release", "--target", "generate-all", "--parallel")
  Run "cmake" (@("--preset", "release") + $cmakeArgs)
}
Run "cmake" @("--build", "--preset", "release", "--parallel")

# ---- package
$build = Join-Path $Repo "out\build\release"
$name = "Skate3Recomp-MusicMod-Windows"
$stage = Join-Path $Repo "dist\$name"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path (Join-Path $stage "music_mod") | Out-Null
Copy-Item (Join-Path $build "skate3.exe") $stage
Get-ChildItem $build -Filter *.dll | Copy-Item -Destination $stage
Get-ChildItem (Join-Path $Repo "music_mod") -File |
  Where-Object { $_.Extension -in ".py", ".json", ".bat" } |
  Copy-Item -Destination (Join-Path $stage "music_mod")
Copy-Item (Join-Path $Repo "MUSIC_MOD.md") (Join-Path $stage "README - Music Mod.md")
New-Item -ItemType Directory -Force -Path (Join-Path $stage "My Music") | Out-Null
$zip = Join-Path $Repo "dist\$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip

Write-Host ""
Write-Host "Built $zip" -ForegroundColor Green
Write-Host "Extract it, put your 'game' folder next to skate3.exe, drop songs into 'My Music'"
Write-Host "and double-click music_mod\Install Music Mod.bat."
