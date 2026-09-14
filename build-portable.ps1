#Requires -Version 7.0
<#
.SYNOPSIS
    Build the llm-ocr portable (zip) release.

.DESCRIPTION
    Assembles dist-portable\llm-ocr-<version>-portable\ and its .zip:

        llm-ocr-<version>-portable\
        ├── llm-ocr.exe      Tauri shell, frontend embedded (renamed from web.exe)
        ├── serve.exe        PyInstaller onedir launcher for the engine sidecar
        └── _internal\       PyInstaller onedir payload — MUST stay a sibling of serve.exe

    No MSI / NSIS installer is produced any more: the app is built with
    --no-bundle and `bundle.targets` is `[]` in web\src-tauri\tauri.conf.json.
    The sidecar is an onedir build, so it never unpacks itself into
    %TEMP%\_MEI*, which is what the onefile build used to leak on every quit
    (the shell kills the sidecar tree with a job object, so the bootloader never
    got to clean up after itself).

    The main binary is renamed to llm-ocr.exe here rather than via Cargo: the
    shell resolves its sidecar relative to the *directory* of the running exe
    (tauri-plugin-shell's relative_command_path -> current_exe().parent()), not
    relative to its own file name, so the rename cannot change sidecar lookup.

    Step 4 also smoke-tests the freshly frozen sidecar on a throwaway port: it
    must answer /api/health 200 *and* run a real engine handler.  A module that
    is imported but missing from the bundle only fails at call time, so a health
    check alone would not catch it -- the probe deliberately posts to an engine
    endpoint (/api/book/resolve) that pulls in the newest engine modules.

.PARAMETER SkipSidecar
    Reuse the existing dist\serve\ (skip PyInstaller).
.PARAMETER SkipApp
    Reuse the existing web\src-tauri\target\release\web.exe (skip the Tauri build).
.PARAMETER SkipZip
    Assemble and verify the folder, but do not produce the .zip.
.PARAMETER SkipSmoke
    Do not launch the sidecar; only check files on disk.
.PARAMETER SmokePort
    Throwaway port for the sidecar smoke test (default 45997).  Never 21139:
    that is the port a running release uses.

.EXAMPLE
    pwsh -File build-portable.ps1                 # full clean build + zip
    pwsh -File build-portable.ps1 -SkipApp        # re-assemble from existing artifacts
#>
[CmdletBinding()]
param(
    [switch]$SkipSidecar,
    [switch]$SkipApp,
    [switch]$SkipZip,
    [switch]$SkipSmoke,
    [int]$SmokePort = 45997
)

$ErrorActionPreference = 'Stop'

$RepoRoot         = $PSScriptRoot
$TauriConf        = Join-Path $RepoRoot 'web\src-tauri\tauri.conf.json'
$SidecarDir       = Join-Path $RepoRoot 'dist\serve'
$SidecarExe       = Join-Path $SidecarDir 'serve.exe'
$SidecarInternal  = Join-Path $SidecarDir '_internal'
$ExternBin        = Join-Path $RepoRoot 'web\src-tauri\binaries\serve-x86_64-pc-windows-msvc.exe'
$AppExe           = Join-Path $RepoRoot 'web\src-tauri\target\release\web.exe'
$OutRoot          = Join-Path $RepoRoot 'dist-portable'

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

function Assert-Path([string]$Path, [string]$What) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$What not found: $Path"
    }
}

function Get-FolderSizeMB([string]$Path) {
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if (-not $sum) { return 0 }
    return [math]::Round($sum / 1MB, 2)
}

# A production Tauri build embeds the Vite output, and web/dist/theme-init.js is
# part of it.  A dev-shaped binary does not contain the string.  (Do NOT test for
# localhost:1420 — that string is present in correct production builds too.)
function Test-EmbedsFrontend([string]$ExePath) {
    $bytes = [System.IO.File]::ReadAllBytes($ExePath)
    $text  = [System.Text.Encoding]::ASCII.GetString($bytes)
    return $text.Contains('theme-init.js')
}

# Raw-socket HTTP: no proxy resolution, no cmdlet HTTP client, and it talks to a
# bind address the way the Tauri shell's own health_ok() does.
function Invoke-Bridge([int]$Port, [string]$Path, [string]$Json = '', [int]$TimeoutMs = 5000) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try { $client.Connect('127.0.0.1', $Port) } catch { return $null }
    $client.ReceiveTimeout = $TimeoutMs
    if ($Json) {
        $body = [System.Text.Encoding]::UTF8.GetBytes($Json)
        $head = "POST $Path HTTP/1.1`r`nHost: 127.0.0.1:$Port`r`nContent-Type: application/json`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
    } else {
        $body = @()
        $head = "GET $Path HTTP/1.1`r`nHost: 127.0.0.1:$Port`r`nConnection: close`r`n`r`n"
    }
    $stream = $client.GetStream()
    $hb = [System.Text.Encoding]::ASCII.GetBytes($head)
    $stream.Write($hb, 0, $hb.Length)
    if ($body.Length) { $stream.Write($body, 0, $body.Length) }
    $ms = [System.IO.MemoryStream]::new()
    $buf = New-Object byte[] 8192
    while ($true) {
        try { $n = $stream.Read($buf, 0, $buf.Length) } catch { break }
        if ($n -le 0) { break }
        $ms.Write($buf, 0, $n)
    }
    $client.Close()
    $text = [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
    $parts = $text -split "`r`n`r`n", 2
    return [pscustomobject]@{
        Status = ($parts[0] -split "`r`n")[0]
        Body   = if ($parts.Count -gt 1) { $parts[1] } else { '' }
    }
}

# ---------------------------------------------------------------------------
# Version + output layout
# ---------------------------------------------------------------------------
Assert-Path $TauriConf 'tauri.conf.json'
$conf    = Get-Content -LiteralPath $TauriConf -Raw | ConvertFrom-Json
$version = $conf.version
if (-not $version) { throw "no `"version`" in $TauriConf" }
$targetsLabel = if ($conf.bundle.targets) { $conf.bundle.targets | ConvertTo-Json -Compress } else { '[] (bundle disabled)' }

$Name    = "llm-ocr-$version-portable"
$Stage   = Join-Path $OutRoot $Name
$ZipPath = Join-Path $OutRoot "$Name.zip"

Write-Host "llm-ocr portable build" -ForegroundColor Green
Write-Host "  repo      : $RepoRoot"
Write-Host "  version   : $version"
Write-Host "  targets   : bundle.targets = $targetsLabel -- no MSI/NSIS"
Write-Host "  output    : $Stage"

# ---------------------------------------------------------------------------
# 1. Engine sidecar (PyInstaller onedir)
# ---------------------------------------------------------------------------
if ($SkipSidecar) {
    Write-Step "1/6 sidecar: skipped (-SkipSidecar)"
} else {
    Write-Step "1/6 sidecar: python -m PyInstaller serve.spec --noconfirm"
    Push-Location $RepoRoot
    try {
        & python -m PyInstaller serve.spec --noconfirm
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
}

Assert-Path $SidecarExe 'sidecar launcher'
Assert-Path $SidecarInternal 'sidecar _internal folder'
# Proof the spec's datas actually landed: the shell's legacy %TEMP%\_MEI* sweep
# identifies "our" directories by exactly this file.
$Marker = Join-Path $SidecarInternal 'prompts\ocr_system.md'
Assert-Path $Marker 'bundled prompts\ocr_system.md inside _internal'
Assert-Path (Join-Path $SidecarInternal 'tests\cand_165.png') 'bundled tests\cand_165.png inside _internal'
Write-Host ("  serve.exe   : {0:N0} bytes" -f (Get-Item -LiteralPath $SidecarExe).Length)
Write-Host ("  _internal   : {0} MB" -f (Get-FolderSizeMB $SidecarInternal))

# ---------------------------------------------------------------------------
# 2. externalBin input (keeps `pnpm tauri build` working)
# ---------------------------------------------------------------------------
if (-not $SkipSidecar) {
    Write-Step '2/6 externalBin: refresh web\src-tauri\binaries\serve-x86_64-pc-windows-msvc.exe'
    Copy-Item -LiteralPath $SidecarExe -Destination $ExternBin -Force
    Write-Host ("  copied {0:N0} bytes" -f (Get-Item -LiteralPath $ExternBin).Length)
} else {
    Write-Step '2/6 externalBin: skipped (sidecar not rebuilt)'
}

# ---------------------------------------------------------------------------
# 3. Tauri app (no bundling)
# ---------------------------------------------------------------------------
if ($SkipApp) {
    Write-Step '3/6 app: skipped (-SkipApp)'
} else {
    Write-Step '3/6 app: pnpm tauri build --no-bundle  (release + lto, ~8-9 min)'
    Push-Location (Join-Path $RepoRoot 'web')
    try {
        & pnpm tauri build --no-bundle
        if ($LASTEXITCODE -ne 0) { throw "tauri build failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
}

Assert-Path $AppExe 'tauri release binary'
if (-not (Test-EmbedsFrontend $AppExe)) {
    throw "REFUSING TO SHIP: $AppExe does not embed the frontend (no 'theme-init.js'); it looks like a dev build."
}
$appSizeMB = [math]::Round((Get-Item -LiteralPath $AppExe).Length / 1MB, 2)
Write-Host ("  web.exe     : {0:N0} bytes ({1} MB), embeds theme-init.js" -f (Get-Item -LiteralPath $AppExe).Length, $appSizeMB)

# ---------------------------------------------------------------------------
# 4. Sidecar smoke test (frozen modules really import)
# ---------------------------------------------------------------------------
if ($SkipSmoke) {
    Write-Step '4/6 smoke: skipped (-SkipSmoke)'
} else {
    Write-Step "4/6 smoke: run serve.exe on throwaway port $SmokePort and call an engine handler"
    if ($SmokePort -eq 21139) { throw 'refusing to smoke-test on 21139 (that is the live release port)' }
    $probeDir = Join-Path $env:TEMP ("llmocr-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
    $outLog = Join-Path $env:TEMP 'llmocr-smoke.out'
    $errLog = Join-Path $env:TEMP 'llmocr-smoke.err'
    $smoke = Start-Process -FilePath $SidecarExe -ArgumentList '--port', "$SmokePort" `
        -WorkingDirectory $SidecarDir -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    try {
        $deadline = (Get-Date).AddSeconds(45)
        $healthy  = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $h = Invoke-Bridge -Port $SmokePort -Path '/api/health'
            if ($h -and $h.Status -like 'HTTP/* 200*') {
                Write-Host ("  /api/health         : {0} {1}" -f $h.Status, $h.Body)
                $healthy = $true
                break
            }
        }
        if (-not $healthy) { throw "sidecar never answered /api/health on port $SmokePort" }

        # Engine handler, not just the health endpoint: this is what exercises the
        # frozen module graph (batch_plan + book_id).  A missing module surfaces
        # here as HTTP 500 "ModuleNotFoundError: No module named ...".
        $payload = '{"dir":"' + ($probeDir -replace '\\', '\\') + '"}'
        $r = Invoke-Bridge -Port $SmokePort -Path '/api/book/resolve' -Json $payload
        Write-Host ("  /api/book/resolve   : {0} {1}" -f $r.Status, $r.Body)
        if ($r.Status -notlike 'HTTP/* 200*') {
            throw "engine handler failed in the frozen sidecar: $($r.Status) $($r.Body)"
        }
        if ($r.Body -match 'ModuleNotFoundError|ImportError') {
            throw "frozen sidecar is missing an engine module: $($r.Body)"
        }
        Write-Host '  smoke OK: frozen engine handlers import and run' -ForegroundColor Green
    } finally {
        if ($smoke -and -not $smoke.HasExited) { & taskkill /F /PID $smoke.Id | Out-Null }
        Start-Sleep -Milliseconds 800
        Remove-Item -LiteralPath (Join-Path $env:TEMP "llm-ocr-serve-$SmokePort.lock") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $probeDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# 5. Assemble the portable folder
# ---------------------------------------------------------------------------
Write-Step "5/6 assemble: $Stage"
if (Test-Path -LiteralPath $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

# The frontend is compiled into the exe (frontendDist ../dist), so nothing else
# from web/ belongs in the folder.
Copy-Item -LiteralPath $AppExe      -Destination (Join-Path $Stage 'llm-ocr.exe')   -Force
Copy-Item -LiteralPath $SidecarExe  -Destination (Join-Path $Stage 'serve.exe')     -Force
Copy-Item -LiteralPath $SidecarInternal -Destination (Join-Path $Stage '_internal') -Recurse -Force

# Post-assembly invariants, checked on the staged copy rather than on the inputs.
Assert-Path (Join-Path $Stage 'llm-ocr.exe') 'staged llm-ocr.exe'
Assert-Path (Join-Path $Stage 'serve.exe') 'staged serve.exe'
Assert-Path (Join-Path $Stage '_internal\prompts\ocr_system.md') 'staged _internal marker'
if (-not (Test-EmbedsFrontend (Join-Path $Stage 'llm-ocr.exe'))) {
    throw 'staged llm-ocr.exe lost the embedded frontend'
}
# serve.exe must be a *sibling* of _internal, never inside it (PyInstaller onedir).
if (Test-Path -LiteralPath (Join-Path $Stage '_internal\serve.exe')) {
    throw 'serve.exe ended up inside _internal; PyInstaller onedir requires it as a sibling'
}

$fileCount = (Get-ChildItem -LiteralPath $Stage -Recurse -File | Measure-Object).Count
Write-Host ("  files       : {0}" -f $fileCount)
Write-Host ("  total       : {0} MB" -f (Get-FolderSizeMB $Stage))
Get-ChildItem -LiteralPath $Stage | ForEach-Object {
    if ($_.PSIsContainer) {
        Write-Host ("    {0,-14} {1,10} MB" -f ($_.Name + '\'), (Get-FolderSizeMB $_.FullName))
    } else {
        Write-Host ("    {0,-14} {1,10:N0} bytes" -f $_.Name, $_.Length)
    }
}

# ---------------------------------------------------------------------------
# 6. Zip
# ---------------------------------------------------------------------------
if ($SkipZip) {
    Write-Step '6/6 zip: skipped (-SkipZip)'
} else {
    Write-Step "6/6 zip: $ZipPath"
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    # Compress-Archive stores the folder itself as the archive root, so the zip
    # extracts to llm-ocr-<version>-portable\.
    Compress-Archive -Path $Stage -DestinationPath $ZipPath -CompressionLevel Optimal

    # Read the archive back: a zip that "was written" is not a zip that carries
    # the sidecar, and the sidecar is the part that can silently go missing.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entries = $zip.Entries
        $names   = $entries | ForEach-Object { $_.FullName }
        $want = @("$Name/llm-ocr.exe", "$Name/serve.exe", "$Name/_internal/prompts/ocr_system.md")
        foreach ($w in $want) {
            if ($names -notcontains $w) { throw "zip is missing $w" }
        }
        Write-Host ("  entries     : {0} (llm-ocr.exe, serve.exe, _internal/* all present)" -f $entries.Count)
    } finally {
        $zip.Dispose()
    }

    $item = Get-Item -LiteralPath $ZipPath
    $hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
    Write-Host ("  zip         : {0:N0} bytes ({1} MB)" -f $item.Length, [math]::Round($item.Length / 1MB, 2))
    Write-Host ("  sha256      : $hash")
}

# ---------------------------------------------------------------------------
# Report: no installer artifacts may appear
# ---------------------------------------------------------------------------
$bundleDir = Join-Path $RepoRoot 'web\src-tauri\target\release\bundle'
$installers = @()
if (Test-Path -LiteralPath $bundleDir) {
    $installers = @(Get-ChildItem -LiteralPath $bundleDir -Recurse -File -Include *.msi, *-setup.exe -ErrorAction SilentlyContinue)
}
Write-Host ''
if ($installers.Count -eq 0) {
    Write-Host 'OK: no MSI/NSIS installer artifacts under target\release\bundle' -ForegroundColor Green
} else {
    Write-Warning ("stale installer artifacts still present (not produced by this run): " +
        (($installers | ForEach-Object { $_.FullName }) -join ', '))
}

Write-Host ''
Write-Host "portable folder : $Stage" -ForegroundColor Green
if (-not $SkipZip) { Write-Host "portable zip    : $ZipPath" -ForegroundColor Green }
Write-Host 'Run llm-ocr.exe from inside the folder — it spawns .\serve.exe as its engine sidecar.' -ForegroundColor Green
