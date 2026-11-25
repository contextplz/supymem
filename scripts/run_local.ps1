#requires -Version 5.1
$ErrorActionPreference = 'Stop'

$mode = $null
foreach ($option in $args) {
    if ($option -eq '--yes') { continue }
    if ($option -notin @('--check', '--build', '--desktop', '--web') -or $mode) {
        throw 'Run .\start.ps1 for your desktop, .\start.ps1 --web for your browser, .\start.ps1 --build for an installer, or .\start.ps1 --check to inspect your prerequisites.'
    }
    $mode = $option.Substring(2).ToLowerInvariant()
}
if (-not $mode) { $mode = 'desktop' }
if ($env:OS -ne 'Windows_NT') {
    throw 'Run ./start in your Mac or Linux terminal, then retry.'
}
function Test-Python312($command, $prefix) {
    try {
        & $command @prefix -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$root = (Resolve-Path -LiteralPath "$PSScriptRoot/..").Path
Push-Location -LiteralPath $root
$mutex = $null
$locked = $false
$previousMode = $env:SUPYMEM_MODE
$previousEnvironment = $env:APP_ENV
try {
    $missing = @()
    $python = $null
    foreach ($candidate in @('py', 'python', 'python3')) {
        $probe = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue
        $prefix = @()
        if ($candidate -eq 'py') { $prefix = @('-3.12') }
        if ($probe -and (Test-Python312 $probe.Source $prefix)) {
            $python = $probe.Source
            break
        }
    }
    if (-not $python) {
        $missing += 'Python 3.12: Run winget install --id Python.Python.3.12 --exact. Without WinGet, open Python Releases for Windows, run the Python 3.12.10 Windows installer (64-bit), and include the Python launcher or add Python to PATH.'
    }
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $nodeVersion = if ($node) { & $node.Source --version }
    $localCorepack = Join-Path $root 'node_modules\.corepack\node_modules\.bin\corepack.cmd'
    $corepack = if (Test-Path -LiteralPath $localCorepack -PathType Leaf) { $localCorepack }
        else { (Get-Command corepack.cmd -ErrorAction SilentlyContinue).Source }
    if ($nodeVersion -notmatch '^v(\d+)\.' -or [int]$Matches[1] -lt 22 -or
        (-not $corepack -and -not (Get-Command npm.cmd -ErrorAction SilentlyContinue))) {
        $missing += 'Node 22 or newer: Run winget install --id OpenJS.NodeJS.LTS --exact. Without WinGet, open the Node.js Download page and run the Windows Installer (.msi) for Node 22 or newer with npm selected.'
    }

    if ($mode -ne 'web') {
        $programFiles = ${env:ProgramFiles(x86)}
        if (-not $programFiles) { $programFiles = $env:ProgramFiles }
        $vswhere = Join-Path $programFiles 'Microsoft Visual Studio\Installer\vswhere.exe'
        $msvc = 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64'
        if (-not ((Test-Path -LiteralPath $vswhere) -and (& $vswhere -latest -products '*' -requires $msvc -property installationPath))) {
            $missing += 'Visual Studio C++ Build Tools: Run winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.26100". Without WinGet, get Build Tools for Visual Studio 2022 from Visual Studio Downloads and run .\vs_BuildTools.exe --wait --passive --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.26100. For an existing installation, use Visual Studio Installer > Modify > Desktop development with C++ and select the MSVC x64/x86 build tools.'
        }
        $sdkRoot = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -Name KitsRoot10 -ErrorAction SilentlyContinue).KitsRoot10
        if (-not $sdkRoot) {
            $sdkRoot = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots' -Name KitsRoot10 -ErrorAction SilentlyContinue).KitsRoot10
        }
        $sdkReady = $false
        if ($sdkRoot) {
            foreach ($include in (Get-ChildItem -LiteralPath (Join-Path $sdkRoot 'Include') -Directory -ErrorAction SilentlyContinue)) {
                if ((Test-Path -LiteralPath (Join-Path $include.FullName 'um\Windows.h')) -and
                    (Test-Path -LiteralPath (Join-Path $sdkRoot "Lib\$($include.Name)\um\x64\kernel32.lib")) -and
                    (Test-Path -LiteralPath (Join-Path $sdkRoot "Lib\$($include.Name)\ucrt\x64\ucrt.lib")) -and
                    (Test-Path -LiteralPath (Join-Path $sdkRoot "bin\$($include.Name)\x64\rc.exe"))) {
                    $sdkReady = $true
                    break
                }
            }
        }
        if (-not $sdkReady) {
            $missing += 'Windows SDK: Run winget install --id Microsoft.WindowsSDK.10.0.26100 --exact. Without WinGet, get the Windows 11 SDK 10.0.26100 installer from Windows SDK Downloads and run .\winsdksetup.exe.'
        }
        $webviewReady = $false
        foreach ($registry in @('HKLM:\SOFTWARE\WOW6432Node', 'HKLM:\SOFTWARE', 'HKCU:\SOFTWARE')) {
            $runtime = "$registry\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
            $webviewVersion = (Get-ItemProperty -LiteralPath $runtime -Name pv -ErrorAction SilentlyContinue).pv
            if ($webviewVersion -match '^[1-9]\d*\.\d+\.\d+\.\d+$') { $webviewReady = $true; break }
        }
        if (-not $webviewReady) {
            $missing += 'WebView2 Runtime: Run winget install --id Microsoft.EdgeWebView2Runtime --exact. Without WinGet, get the Evergreen Standalone Installer (x64) from Download the WebView2 Runtime and run .\MicrosoftEdgeWebView2RuntimeInstallerX64.exe.'
        }
        if (-not (Get-Command cargo -ErrorAction SilentlyContinue) -or -not (Get-Command rustc -ErrorAction SilentlyContinue)) {
            $missing += 'Rust: Run winget install --id Rustlang.Rustup --exact. Without WinGet, open Install Rust, download rustup-init.exe (64-bit), and run .\rustup-init.exe -y --default-host x86_64-pc-windows-msvc.'
        } else {
            $lock = Get-Content -LiteralPath src-tauri/Cargo.lock -Raw
            if ($lock -notmatch '(?m)^name = "tauri"\r?\nversion = "([0-9.]+)"') {
                throw 'Restore src-tauri/Cargo.lock from your source release, then rerun .\start.ps1.'
            }
            $tauriVersion = $Matches[1]
            try { $installedTauri = & cargo tauri --version 2>$null }
            catch { $installedTauri = '' }
            if ($installedTauri -ne "tauri-cli $tauriVersion") {
                $missing += "Tauri CLI: Run cargo install tauri-cli --version $tauriVersion --locked after installing the desktop prerequisites. This command does not use WinGet."
            }
        }
    }
    if ($missing.Count) {
        $retry = if ($mode -eq 'desktop') { '.\start.ps1' } else { ".\start.ps1 --$mode" }
        throw ("Your setup is missing the following:`n" + ($missing -join "`n") + "`nComplete the steps above, open a new PowerShell window, then rerun $retry.")
    }
    if ($mode -eq 'check') {
        Write-Host 'Your prerequisites are ready. Run .\start.ps1 to open your desktop.'
        exit 0
    }
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try { $identity = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '') }
    finally { $hash.Dispose() }
    $mutex = New-Object System.Threading.Mutex($false, "Local\DesktopSetup-$identity")
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Your setup is already running. Wait for your other setup window to finish, then retry.' }
    $env:SUPYMEM_MODE = 'local'
    $env:APP_ENV = 'local'
    $retry = if ($mode -eq 'desktop') { '.\start.ps1' } else { ".\start.ps1 --$mode" }
    Write-Host 'Preparing your local app. Your first setup can take 5 to 15 minutes.'
    if (-not $corepack) {
        Write-Host 'Installing Corepack for your interface packages.'
        & npm.cmd install --prefix .\node_modules\.corepack corepack
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $localCorepack -PathType Leaf)) {
            throw "Your Corepack setup stopped. Check your connection to the npm registry, then rerun $retry. Your saved work is retained."
        }
        $corepack = $localCorepack
    }
    & $corepack pnpm@10.11.0 --dir apps/console install --frozen-lockfile --node-linker=hoisted
    if ($LASTEXITCODE -ne 0) { throw "Your interface setup stopped. Check your connection to the npm registry. If Corepack reports a signature error, run npm.cmd install --prefix .\node_modules\.corepack corepack, then rerun $retry. Your saved work is retained." }
    if ($mode -eq 'web') {
        $venvPython = '.\.venv\Scripts\python.exe'
        if (-not ((Test-Path -LiteralPath $venvPython) -and (Test-Python312 $venvPython @()))) {
            if (Get-Item -LiteralPath '.venv' -Force -ErrorAction SilentlyContinue) {
                $savedVenv = '.venv-old-' + [guid]::NewGuid().ToString('N')
                Rename-Item -LiteralPath '.venv' -NewName $savedVenv
                Write-Host "Your previous Python environment is saved in $savedVenv. Setup continues with Python 3.12."
            }
            & $python @prefix -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw 'Your Python setup stopped. Repair Python 3.12 with pip selected, then rerun .\start.ps1 --web.' }
        }
        $locks = @('services/company_os/requirements.runtime.lock', 'services/company_os/requirements.build.lock', 'packages/desktop_runtime/requirements.local.lock')
        $inputs = $locks + @('services/company_os/pyproject.toml') + @((Get-ChildItem packages/*/pyproject.toml).FullName)
        $signature = $root + ':' + ((Get-FileHash -LiteralPath $inputs).Hash -join ':')
        $stamp = '.venv/.local-dependencies-windows'
        if ($signature -cne (Get-Content -LiteralPath $stamp -Raw -ErrorAction SilentlyContinue)) {
            Write-Host 'Installing your locked Python packages.'
            foreach ($lock in $locks) {
                & $venvPython -m pip install --require-hashes -r $lock
                if ($LASTEXITCODE -ne 0) { throw 'Your Python package setup stopped. Fix the installer error above, then rerun .\start.ps1 --web. Your saved work is retained.' }
            }
            $harness = @()
            if (Test-Path -LiteralPath 'packages/harness/pyproject.toml' -PathType Leaf) { $harness = @('-e', 'packages/harness') }
            & $venvPython -m pip install --no-deps --no-build-isolation -e packages/core -e packages/coding_agents -e packages/huginn_watch -e packages/llm_gateway -e packages/guardrails -e packages/connectors -e packages/desktop_runtime -e services/company_os @harness
            if ($LASTEXITCODE -ne 0) { throw 'Your Python package setup stopped. Fix the installer error above, then rerun .\start.ps1 --web. Your saved work is retained.' }
            Set-Content -LiteralPath $stamp -Value $signature -Encoding UTF8 -NoNewline
        }
        & $venvPython scripts/desktop/run_local_web.py $node.Source
        exit $LASTEXITCODE
    }
    & $node.Source scripts/desktop/prepare_local_stack.cjs
    if ($LASTEXITCODE -ne 0) { throw "Your desktop payload stopped. Fix the Python or runtime error above, then rerun $retry. Your saved work is retained." }
    $mutex.ReleaseMutex()
    $locked = $false
    Push-Location -LiteralPath src-tauri
    try {
        if ($mode -eq 'build') {
            & cargo tauri build --config tauri.local.json --features local-stack --bundles nsis
        } else {
            & cargo tauri dev --config tauri.local.json --features local-stack --no-dev-server-wait
        }
        if ($LASTEXITCODE -ne 0) { throw "Your desktop build stopped. Fix the build error above, then rerun $retry. Run .\start.ps1 --check to inspect your prerequisites." }
    } finally { Pop-Location }
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
    $env:SUPYMEM_MODE = $previousMode
    $env:APP_ENV = $previousEnvironment
    Pop-Location
}
