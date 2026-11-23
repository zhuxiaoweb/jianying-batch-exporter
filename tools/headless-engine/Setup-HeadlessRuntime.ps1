param(
    [string]$RuntimeRoot = (Join-Path $PSScriptRoot 'runtime'),
    [string]$ExistingRuntimeDirectory,
    [string]$InstallerPath,
    [string]$ExtractorPath,
    [string]$ConfigurationPath = (Join-Path $PSScriptRoot 'runtime.local.json'),
    [string]$ReportPath
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$taskVersion = '11.6.0.14552'
$taskDllHash = '8CFCBB612CCD946DF974428977B1902736C1DE0AE561E20C26CBD4EA86099CB1'
$taskInstallerHash = '410441C0CB4150216127D6CDA2E242D41A152F3E2E4A749227EA4AF52215CA48'
$taskExtractorHash = '256FECA8E274E5DA655E2A284FABAFD9F554365EB164862089DACD4E8276D282'
$taskExpandedBytes = [long]3101133186
$taskArchiveBytes = [long]923476976
$taskReserveBytes = [long]536870912
$taskReport = [ordered]@{ schema=1; phase='preflight'; success=$false; error_code=$null; runtime_root=$RuntimeRoot }

function Save-SetupReport([string]$Phase) {
    $taskReport.phase=$Phase
    if ($ReportPath) {
        $taskReportTarget=[IO.Path]::GetFullPath($ReportPath)
        $taskReportParent=[IO.Path]::GetDirectoryName($taskReportTarget)
        Assert-NotDraftDirectory $taskReportParent
        if ([IO.Path]::GetFileName($taskReportTarget) -in @('draft_content.json','draft_meta_info.json')) { throw 'Report cannot replace a draft.' }
        New-Item -ItemType Directory -Path $taskReportParent -Force | Out-Null
        [IO.File]::WriteAllText($taskReportTarget,($taskReport | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    }
}
function Assert-FreeSpace([string]$Path,[long]$Required) {
    $taskVolume=[IO.DriveInfo]::new([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path)))
    $taskReport.free_bytes=[long]$taskVolume.AvailableFreeSpace
    $taskReport.required_bytes=$Required
    Save-SetupReport 'preflight'
    if ($taskReport.free_bytes -lt $Required) {
        $taskReport.error_code='INSUFFICIENT_SPACE'
        throw ('INSUFFICIENT_SPACE: free='+$taskReport.free_bytes+' required='+$Required+' path='+$Path)
    }
}

function Test-PinnedFile([string]$Path, [string]$Hash) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    # Do not depend on Get-FileHash module autoloading in an EXE-launched PS5.
    $taskHasher=[Security.Cryptography.SHA256]::Create()
    $taskStream=[IO.File]::OpenRead([IO.Path]::GetFullPath($Path))
    try { return ([BitConverter]::ToString($taskHasher.ComputeHash($taskStream)).Replace('-','') -eq $Hash) }
    finally { $taskStream.Dispose(); $taskHasher.Dispose() }
}
function Assert-InsideRoot([string]$Path, [string]$Root) {
    $taskAbsolute = [IO.Path]::GetFullPath($Path)
    $taskBoundary = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    if (-not $taskAbsolute.StartsWith($taskBoundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path leaves the selected runtime directory: $taskAbsolute"
    }
}
function Assert-NotDraftDirectory([string]$Path) {
    $taskCandidate = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path))
    while ($taskCandidate) {
        if (Test-Path -LiteralPath (Join-Path $taskCandidate.FullName 'draft_content.json')) {
            throw 'Runtime and configuration cannot be written inside an existing draft.'
        }
        $taskCandidate = $taskCandidate.Parent
    }
}
function Get-PinnedDownload([string]$Url, [string]$Target, [string]$Hash, [string]$Boundary) {
    Assert-InsideRoot $Target $Boundary
    if (Test-PinnedFile $Target $Hash) { return }
    if (Test-Path -LiteralPath $Target) {
        $taskRejected = $Target + '.rejected-' + [Guid]::NewGuid().ToString('N')
        Assert-InsideRoot $taskRejected $Boundary
        Move-Item -LiteralPath $Target -Destination $taskRejected
    }
    $taskPartial = $Target + '.partial-' + [Guid]::NewGuid().ToString('N')
    Assert-InsideRoot $taskPartial $Boundary
    Save-SetupReport 'download'
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $taskPartial
    if (-not (Test-PinnedFile $taskPartial $Hash)) { throw "Downloaded SHA256 mismatch: $taskPartial" }
    # All resolved move targets remain inside the explicit download directory.
    Move-Item -LiteralPath $taskPartial -Destination $Target
}

try {
Save-SetupReport 'preflight'
if ($ExistingRuntimeDirectory) {
    $taskRuntime = (Resolve-Path -LiteralPath $ExistingRuntimeDirectory).Path
    if (-not (Test-PinnedFile (Join-Path $taskRuntime 'videoeditor.dll') $taskDllHash)) {
        throw 'Existing directory is not the exact supported 11.6 runtime. Nothing was loaded.'
    }
} else {
    $taskRoot = [IO.Path]::GetFullPath($RuntimeRoot)
    Assert-NotDraftDirectory $taskRoot
    New-Item -ItemType Directory -Path $taskRoot -Force | Out-Null
    $taskRuntime = Join-Path $taskRoot $taskVersion
    Assert-InsideRoot $taskRuntime $taskRoot
    if (Test-Path -LiteralPath $taskRuntime) {
        if (-not (Test-PinnedFile (Join-Path $taskRuntime 'videoeditor.dll') $taskDllHash)) {
            throw 'Runtime target already exists but differs. Existing files were retained.'
        }
    } else {
        # The fixed official package expands to 3,101,133,186 bytes. Reserve
        # space before downloading or extracting, on the selected volume.
        $taskNeeded=$taskExpandedBytes+$taskReserveBytes
        $taskCached=Join-Path $taskRoot 'downloads\Jianying_11_6_0_14552.exe'
        if (-not $InstallerPath -and -not (Test-PinnedFile $taskCached $taskInstallerHash)) { $taskNeeded+=$taskArchiveBytes }
        Assert-FreeSpace $taskRoot $taskNeeded
        $taskDownload = Join-Path $taskRoot 'downloads'
        New-Item -ItemType Directory -Path $taskDownload -Force | Out-Null
        if (-not $InstallerPath) {
            $InstallerPath = Join-Path $taskDownload 'Jianying_11_6_0_14552.exe'
            Write-Host 'Downloading the hash-pinned official Jianying package...'
            Get-PinnedDownload 'https://lf3-package.vlabstatic.com/obj/faceu-packages/Jianying_11_6_0_14552_jianyingpro_0_creatortool.exe' $InstallerPath $taskInstallerHash $taskDownload
        }
        if (-not (Test-PinnedFile $InstallerPath $taskInstallerHash)) { throw 'Installer SHA256 mismatch.' }
        $taskReport.installer_path=[IO.Path]::GetFullPath($InstallerPath)
        $taskReport.installer_verified=$true
        if (-not $ExtractorPath) {
            $taskBundledExtractor = Join-Path $PSScriptRoot 'bin\7zr.exe'
            if (Test-PinnedFile $taskBundledExtractor $taskExtractorHash) {
                $ExtractorPath = $taskBundledExtractor
            } else {
                $ExtractorPath = Join-Path $taskDownload '7zr.exe'
                Get-PinnedDownload 'https://www.7-zip.org/a/7zr.exe' $ExtractorPath $taskExtractorHash $taskDownload
            }
        }
        if (-not (Test-PinnedFile $ExtractorPath $taskExtractorHash)) { throw 'Extractor SHA256 mismatch.' }
        $taskStage = Join-Path $taskRoot ('.extract-' + [Guid]::NewGuid().ToString('N'))
        Assert-InsideRoot $taskStage $taskRoot
        New-Item -ItemType Directory -Path $taskStage | Out-Null
        Assert-FreeSpace $taskRoot ($taskExpandedBytes+$taskReserveBytes)
        $taskReport.stage_directory=$taskStage
        Save-SetupReport 'extract'
        Write-Host 'Extracting into an independent directory; the installer will not be executed...'
        & $ExtractorPath x $InstallerPath "-o$taskStage" 'JYPacket\11.6.0.14552\*' -y -bso0 -bsp0 -sccUTF-8
        if ($LASTEXITCODE -ne 0) { throw "Extraction failed; diagnostics retained at $taskStage" }
        $taskExtracted = Join-Path $taskStage 'JYPacket\11.6.0.14552'
        Save-SetupReport 'verify'
        Assert-InsideRoot $taskExtracted $taskRoot
        if (-not (Test-PinnedFile (Join-Path $taskExtracted 'videoeditor.dll') $taskDllHash)) {
            throw 'Extracted engine fingerprint mismatch. Existing editor was not changed.'
        }
        # Both resolved move targets have been verified inside the explicit runtime root.
        Move-Item -LiteralPath $taskExtracted -Destination $taskRuntime
    }
}
$taskConfiguration = [ordered]@{
    schema = 1
    engine_version = $taskVersion
    runtime_directory = $taskRuntime
    videoeditor_sha256 = $taskDllHash
    application_installation_changed = $false
    release_status = 'internal_validation_only'
}
Save-SetupReport 'configure'
$taskConfigPath = [IO.Path]::GetFullPath($ConfigurationPath)
$taskConfigRoot = [IO.Path]::GetDirectoryName($taskConfigPath)
Assert-NotDraftDirectory $taskConfigRoot
if ([IO.Path]::GetFileName($taskConfigPath) -eq 'draft_content.json') { throw 'Configuration cannot replace a draft.' }
if (Test-Path -LiteralPath $taskConfigPath) {
    $taskOldConfiguration = Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskOldConfiguration.schema -ne 1 -or $taskOldConfiguration.engine_version -ne $taskVersion) {
        throw 'Configuration target exists but was not generated by this component.'
    }
}
New-Item -ItemType Directory -Path $taskConfigRoot -Force | Out-Null
$taskConfigTemporary = $taskConfigPath + '.new-' + [Guid]::NewGuid().ToString('N')
Assert-InsideRoot $taskConfigPath $taskConfigRoot
Assert-InsideRoot $taskConfigTemporary $taskConfigRoot
[IO.File]::WriteAllText($taskConfigTemporary, ($taskConfiguration | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $taskConfigTemporary -Destination $taskConfigPath -Force
Write-Host "Verified runtime: $taskRuntime"
Write-Host "Local configuration: $taskConfigPath"
$taskReport.success=$true
$taskReport.runtime_directory=$taskRuntime
$taskReport.configuration_path=$taskConfigPath
Save-SetupReport 'complete'
} catch {
    $taskReport.error=$_.Exception.Message
    $taskReport.success=$false
    try { Save-SetupReport 'failed' } catch { Write-Host 'Setup diagnostics could not be saved.' }
    throw
}
