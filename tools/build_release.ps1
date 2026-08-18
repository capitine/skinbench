param(
    [string]$SourceRoot = (Split-Path $PSScriptRoot -Parent),
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Remove-PublishedFileIfOwned {
    param(
        [string]$Path,
        [AllowNull()][string]$ExpectedHash,
        [bool]$Published
    )

    if (-not $Published -or [string]::IsNullOrWhiteSpace($ExpectedHash)) {
        return
    }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if (
            $item -isnot [System.IO.FileInfo] -or
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            return
        }
        $currentHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path -ErrorAction Stop).Hash
        if ($currentHash -cne $ExpectedHash) {
            return
        }
        Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch {
        return
    }
}

$expectedVersion = '1.0.0-beta.1'
$packageFiles = @(
    'CHANGELOG.md',
    'CODE_OF_CONDUCT.md',
    'CONTRIBUTING.md',
    'LICENSE',
    'README.md',
    'SECURITY.md',
    'SUPPORT.md',
    'docs/compatibility.md',
    'docs/development.md',
    'docs/images/skinbench-overview.png',
    'docs/safety.md',
    'docs/user-guide.md',
    'install.py',
    'plug-ins/skinBench.py',
    'scripts/skinbench/__init__.py',
    'scripts/skinbench/compat.py',
    'scripts/skinbench/core.py',
    'scripts/skinbench/ui.py'
)

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "Source root does not exist: $SourceRoot"
}
$sourcePath = (Get-Item -LiteralPath $SourceRoot -Force).FullName
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $sourcePath 'dist'
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)

$versionPath = Join-Path $sourcePath 'scripts\skinbench\__init__.py'
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
    throw "Version source is missing: scripts/skinbench/__init__.py"
}
$versionText = [System.IO.File]::ReadAllText($versionPath)
$versionMatches = @([regex]::Matches(
    $versionText,
    '^\s*__version__\s*=\s*"([^"]+)"\s*(?:#.*)?$',
    [System.Text.RegularExpressions.RegexOptions]::Multiline
))
if ($versionMatches.Count -ne 1) {
    throw 'Expected exactly one package version declaration'
}
$sourceVersion = $versionMatches[0].Groups[1].Value
if ($sourceVersion -cne $expectedVersion) {
    throw "Source version mismatch: expected $expectedVersion, found $sourceVersion"
}

$archiveName = "SkinBench-$expectedVersion.zip"
$checksumName = "$archiveName.sha256"
$archivePath = Join-Path $outputPath $archiveName
$checksumPath = Join-Path $outputPath $checksumName
if ((Test-Path -LiteralPath $archivePath) -or (Test-Path -LiteralPath $checksumPath)) {
    throw "Refusing to overwrite existing release output: $archivePath or $checksumPath"
}

$trustedRoot = Split-Path $PSScriptRoot -Parent
$repositoryPolicy = Join-Path $trustedRoot 'tests\release_policy.ps1'
if (-not (Test-Path -LiteralPath $repositoryPolicy -PathType Leaf)) {
    throw "Trusted release policy is missing: $repositoryPolicy"
}
& pwsh -NoProfile -File $repositoryPolicy -Profile Repository -Root $sourcePath -RequireScreenshot
if ($LASTEXITCODE -ne 0) {
    throw "Repository release policy failed with exit code $LASTEXITCODE"
}

[void][System.IO.Directory]::CreateDirectory($outputPath)
$stagingLeaf = '.skinbench-release-' + [System.Guid]::NewGuid().ToString('N')
$stagingRoot = Join-Path $outputPath $stagingLeaf
$stagingParent = [System.IO.Path]::GetFullPath((Split-Path $stagingRoot -Parent)).TrimEnd('\', '/')
$verifiedOutput = $outputPath.TrimEnd('\', '/')
if (
    -not [string]::Equals(
        $stagingParent,
        $verifiedOutput,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -or
    $stagingLeaf -cnotmatch '^\.skinbench-release-[0-9a-f]{32}$'
) {
    throw "Unsafe release staging path: $stagingRoot"
}
if (Test-Path -LiteralPath $stagingRoot) {
    throw "Release staging path already exists: $stagingRoot"
}

$packageName = "SkinBench-$expectedVersion"
$packageContainer = Join-Path $stagingRoot 'package'
$packageRoot = Join-Path $packageContainer $packageName
$stagedArchive = Join-Path $stagingRoot $archiveName
$stagedChecksum = Join-Path $stagingRoot $checksumName
$archivePublished = $false
$checksumPublished = $false
$expectedArchiveHash = $null
$expectedChecksumHash = $null
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

try {
    [void][System.IO.Directory]::CreateDirectory($packageRoot)
    foreach ($relative in $packageFiles) {
        $sourceFile = Join-Path $sourcePath ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
            throw "Package source file is missing: $relative"
        }
        $destination = Join-Path $packageRoot ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        [void][System.IO.Directory]::CreateDirectory((Split-Path $destination -Parent))
        Copy-Item -LiteralPath $sourceFile -Destination $destination
    }

    $manifestPaths = [string[]]$packageFiles.Clone()
    [Array]::Sort($manifestPaths, [System.StringComparer]::Ordinal)
    $manifestLines = foreach ($relative in $manifestPaths) {
        $filePath = Join-Path $packageRoot ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash
        "$hash  $relative"
    }
    $manifestPath = Join-Path $packageRoot 'SHA256SUMS.txt'
    [System.IO.File]::WriteAllText(
        $manifestPath,
        (($manifestLines -join "`n") + "`n"),
        $utf8NoBom
    )

    & pwsh -NoProfile -File $repositoryPolicy -Profile Package -Root $packageRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Package release policy failed with exit code $LASTEXITCODE"
    }

    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $packageContainer,
        $stagedArchive,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $expectedArchiveHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $stagedArchive
    ).Hash.ToUpperInvariant()
    [System.IO.File]::WriteAllText(
        $stagedChecksum,
        "$expectedArchiveHash  $archiveName`n",
        $utf8NoBom
    )
    $expectedChecksumHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $stagedChecksum
    ).Hash.ToUpperInvariant()

    [System.IO.File]::Move($stagedArchive, $archivePath)
    $archivePublished = $true
    $publishedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToUpperInvariant()
    if ($publishedHash -cne $expectedArchiveHash) {
        throw 'Published release ZIP hash differs from staged ZIP'
    }

    [System.IO.File]::Move($stagedChecksum, $checksumPath)
    $checksumPublished = $true

    Write-Host "[PASS] Release ZIP: $archivePath"
    Write-Host "[PASS] Release checksum: $checksumPath"
}
catch {
    Remove-PublishedFileIfOwned `
        -Path $checksumPath `
        -ExpectedHash $expectedChecksumHash `
        -Published $checksumPublished
    Remove-PublishedFileIfOwned `
        -Path $archivePath `
        -ExpectedHash $expectedArchiveHash `
        -Published $archivePublished
    throw
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        $cleanupParent = [System.IO.Path]::GetFullPath(
            (Split-Path $stagingRoot -Parent)
        ).TrimEnd('\', '/')
        if (
            -not [string]::Equals(
                $cleanupParent,
                $verifiedOutput,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            (Split-Path $stagingRoot -Leaf) -cnotmatch '^\.skinbench-release-[0-9a-f]{32}$'
        ) {
            throw "Refusing unsafe release staging cleanup: $stagingRoot"
        }
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
