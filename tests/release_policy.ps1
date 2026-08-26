param(
    [ValidateSet('Repository', 'Package')]
    [string]$Profile = 'Repository',
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [switch]$RequireScreenshot,
    [string]$PrivacySelfTestValuesPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$expectedPackage = @(
    'CHANGELOG.md',
    'CODE_OF_CONDUCT.md',
    'CONTRIBUTING.md',
    'LICENSE',
    'README.md',
    'SECURITY.md',
    'SHA256SUMS.txt',
    'SUPPORT.md',
    'docs/compatibility.md',
    'docs/development.md',
    'docs/images/skinbench-readme-hero.png',
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

$expectedRepository = @(
    '.github/ISSUE_TEMPLATE/bug.yml',
    '.github/ISSUE_TEMPLATE/config.yml',
    '.github/ISSUE_TEMPLATE/feature.yml',
    '.github/PULL_REQUEST_TEMPLATE.md',
    '.github/dependabot.yml',
    '.github/workflows/ci.yml',
    '.github/workflows/release.yml',
    '.gitignore',
    'CHANGELOG.md',
    'CODE_OF_CONDUCT.md',
    'CONTRIBUTING.md',
    'LICENSE',
    'README.md',
    'SECURITY.md',
    'SUPPORT.md',
    'docs/compatibility.md',
    'docs/development.md',
    'docs/images/skinbench-readme-hero.png',
    'docs/images/skinbench-overview.png',
    'docs/safety.md',
    'docs/user-guide.md',
    'install.py',
    'plug-ins/skinBench.py',
    'requirements-dev.txt',
    'scripts/skinbench/__init__.py',
    'scripts/skinbench/compat.py',
    'scripts/skinbench/core.py',
    'scripts/skinbench/ui.py',
    'tests/release_policy.ps1',
    'tests/smoke_mayapy.py',
    'tools/build_release.ps1'
)

$forbiddenPathPatterns = @(
    '(^|/)__pycache__(/|$)',
    '\.pyc$', '\.pyo$', '\.ma$', '\.mb$', '\.fbx$',
    '\.zip$', '\.7z$', '\.rar$', '\.log$', '\.tmp$',
    'snapshot', 'weight[-_ ]?backup'
)

$privateSignatureRecords = @(
    [pscustomobject]@{ Id = 'sig-01'; Length = 7; Mode = 'raw'; Sha256 = '7bac563946331385a24a9331ecc82aaac68cb74f2f2812ace3006a7b1e692fea' },
    [pscustomobject]@{ Id = 'sig-02'; Length = 9; Mode = 'raw'; Sha256 = 'ea070f89bf270cce825de1fa975d4e61f4bb1ca02231cd274820396ebd762fc3' },
    [pscustomobject]@{ Id = 'sig-03'; Length = 8; Mode = 'raw'; Sha256 = '567781541aa1d043041ec08be14e1c519629784e3952d0d079ac87640242ba5e' },
    [pscustomobject]@{ Id = 'sig-04'; Length = 7; Mode = 'raw'; Sha256 = '776af9401679287d8d332e26e99062919d58a135f4ef5ba85d4ec981b50f58ae' },
    [pscustomobject]@{ Id = 'sig-05'; Length = 17; Mode = 'raw'; Sha256 = '9908f1656ad029ef5feccd35448a4416a4e5bb50ed0843ead8593eda69b7b980' },
    [pscustomobject]@{ Id = 'sig-06'; Length = 11; Mode = 'raw'; Sha256 = '2ad4abed293ea31a3394e5c7762924d60c04fed5a524e80c3f00423cf439eb4a' },
    [pscustomobject]@{ Id = 'sig-07'; Length = 3; Mode = 'word'; Sha256 = '84313ef39b0a979f0608491608870b3f2065f447d73e4373ba75ae2330aa82b5' },
    [pscustomobject]@{ Id = 'sig-08'; Length = 4; Mode = 'raw'; Sha256 = '74fa5327cc0f4e947789dd5e989a61a8242986a596f170640ac90337b1da1ee4' },
    [pscustomobject]@{ Id = 'sig-09'; Length = 4; Mode = 'raw'; Sha256 = 'a9f2a7587d7c05747c2a6c0fb25a45bf608b3dd7182b8f5b44867f4963214be6' }
)

Add-Type -TypeDefinition @'
using System;
using System.Security.Cryptography;
using System.Text;

public static class SkinBenchPrivacyHashScan
{
    public static bool ContainsRaw(string text, int length, string expectedHex)
    {
        var expected = new byte[expectedHex.Length / 2];
        for (var index = 0; index < expected.Length; index++)
        {
            expected[index] = Convert.ToByte(expectedHex.Substring(index * 2, 2), 16);
        }

        using (var hasher = SHA256.Create())
        {
            for (var index = 0; index <= text.Length - length; index++)
            {
                var candidate = Encoding.UTF8.GetBytes(text.Substring(index, length).ToLowerInvariant());
                var actual = hasher.ComputeHash(candidate);
                if (actual.Length != expected.Length)
                {
                    continue;
                }
                var different = 0;
                for (var byteIndex = 0; byteIndex < actual.Length; byteIndex++)
                {
                    different |= actual[byteIndex] ^ expected[byteIndex];
                }
                if (different == 0)
                {
                    return true;
                }
            }
        }
        return false;
    }
}
'@

$violations = [System.Collections.Generic.List[string]]::new()

function Add-Violation {
    param([string]$Message)
    $violations.Add($Message)
}

function Get-LowerSha256 {
    param([string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $hasher.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($null -ne $hasher) {
            $hasher.Dispose()
        }
    }
}

function Resolve-PowerShellApplication {
    try {
        $currentPath = (Get-Process -Id $PID -ErrorAction Stop).Path
        if (-not [string]::IsNullOrWhiteSpace($currentPath) -and (Test-Path -LiteralPath $currentPath -PathType Leaf)) {
            return (Get-Item -LiteralPath $currentPath -Force).FullName
        }
    }
    catch {
        # Fall through to installed application lookup.
    }

    foreach ($name in @('pwsh', 'powershell')) {
        $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
            return (Get-Item -LiteralPath $command.Source -Force).FullName
        }
    }
    return $null
}

function Test-PrivateSignatureRecord {
    param(
        [string]$Text,
        [pscustomobject]$Record
    )

    if ($Record.Mode -ceq 'word') {
        foreach ($match in [regex]::Matches($Text, '\b\w+\b')) {
            if ($match.Value.Length -eq $Record.Length -and (Get-LowerSha256 $match.Value) -ceq $Record.Sha256) {
                return $true
            }
        }
        return $false
    }

    return [SkinBenchPrivacyHashScan]::ContainsRaw($Text, $Record.Length, $Record.Sha256)
}

function Get-PrivacySelfTestRecord {
    param([string]$Value)

    $hash = Get-LowerSha256 $Value
    $matches = @($privateSignatureRecords | Where-Object {
        $_.Length -eq $Value.Length -and $_.Sha256 -ceq $hash
    })
    if ($matches.Count -ne 1) {
        throw 'Privacy self-test input does not map to exactly one protected signature'
    }
    return $matches[0]
}

function Copy-RepositoryPolicyFixture {
    param(
        [string]$SourceRoot,
        [string]$DestinationRoot
    )

    foreach ($relative in $expectedRepository) {
        $sourcePath = Join-Path $SourceRoot ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            continue
        }
        $destinationPath = Join-Path $DestinationRoot ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        $destinationDirectory = Split-Path -Parent $destinationPath
        [void][System.IO.Directory]::CreateDirectory($destinationDirectory)
        Copy-Item -LiteralPath $sourcePath -Destination $destinationPath
    }
}

function Invoke-PrivacySelfTest {
    param([string]$ValuesPath)

    if (-not (Test-Path -LiteralPath $ValuesPath -PathType Leaf)) {
        throw 'Privacy self-test values file does not exist'
    }
    $values = @(Get-Content -Raw -LiteralPath $ValuesPath | ConvertFrom-Json)
    if ($values.Count -ne $privateSignatureRecords.Count -or @($values | Where-Object { $_ -isnot [string] }).Count -gt 0) {
        throw 'Privacy self-test requires one string for every protected signature'
    }

    $records = @($values | ForEach-Object { Get-PrivacySelfTestRecord $_ })
    if (@($records.Id | Select-Object -Unique).Count -ne $privateSignatureRecords.Count) {
        throw 'Privacy self-test inputs do not cover every protected signature exactly once'
    }

    $literalRuns = @(
        foreach ($line in (Get-Content -LiteralPath $PSCommandPath)) {
            $pieces = @([regex]::Matches($line, "'([^']*)'") | ForEach-Object { $_.Groups[1].Value })
            if ($pieces.Count -gt 0) {
                [regex]::Replace(($pieces -join ''), '[^A-Za-z0-9]', '').ToLowerInvariant()
            }
        }
    )
    foreach ($index in 0..($values.Count - 1)) {
        $normalizedValue = ([regex]::Replace($values[$index], '[^A-Za-z0-9]', '')).ToLowerInvariant()
        if ($normalizedValue.Length -gt 0 -and @($literalRuns | Where-Object {
            $_.Contains($normalizedValue)
        }).Count -gt 0) {
            throw "Privacy self-test found reconstructable protected signature $($records[$index].Id) in policy source"
        }
    }

    $fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('skinbench-policy-fixture-' + [guid]::NewGuid().ToString('N'))
    $cleanRoot = Join-Path $fixtureRoot 'clean'
    $privateRoot = Join-Path $fixtureRoot 'private'
    try {
        Copy-RepositoryPolicyFixture -SourceRoot $rootPath -DestinationRoot $cleanRoot
        Copy-RepositoryPolicyFixture -SourceRoot $rootPath -DestinationRoot $privateRoot
        $powershellPath = Resolve-PowerShellApplication
        if ([string]::IsNullOrWhiteSpace($powershellPath)) {
            throw 'Privacy self-test cannot resolve a PowerShell application'
        }
        $cleanOutput = @(& $powershellPath -NoProfile -File $PSCommandPath -Profile Repository -Root $cleanRoot 2>&1)
        if ($LASTEXITCODE -ne 0 -or -not (($cleanOutput -join "`n") -match '\[PASS\] SkinBench Repository release policy')) {
            throw 'Privacy self-test clean Repository fixture did not pass'
        }

        $privateReadme = Join-Path $privateRoot 'README.md'
        [System.IO.File]::AppendAllText(
            $privateReadme,
            [System.Environment]::NewLine + ($values -join [System.Environment]::NewLine),
            [System.Text.UTF8Encoding]::new($false)
        )
        $privateOutput = @(& $powershellPath -NoProfile -File $PSCommandPath -Profile Repository -Root $privateRoot 2>&1)
        $privateText = $privateOutput -join "`n"
        if ($LASTEXITCODE -eq 0) {
            throw 'Privacy self-test private Repository fixture did not fail'
        }
        foreach ($record in $privateSignatureRecords) {
            if ($privateText -notmatch ("Private signature " + [regex]::Escape($record.Id) + " found in: README\.md")) {
                throw "Privacy self-test did not report protected signature $($record.Id)"
            }
        }
        foreach ($value in $values) {
            if ($privateText.IndexOf($value, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                throw 'Privacy self-test output disclosed a protected value'
            }
        }
    }
    finally {
        if (Test-Path -LiteralPath $fixtureRoot) {
            Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
        }
    }
}

function Get-RelativePath {
    param(
        [string]$BasePath,
        [string]$Path
    )

    $baseUri = [System.Uri]::new(($BasePath.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar))
    $pathUri = [System.Uri]::new($Path)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($pathUri).ToString()).Replace('\', '/')
}

function Get-Utf8Text {
    param([string]$Path)

    $utf8 = [System.Text.UTF8Encoding]::new($false, $true)
    try {
        return [System.IO.File]::ReadAllText($Path, $utf8)
    }
    catch [System.Text.DecoderFallbackException] {
        return $null
    }
}

function New-OrdinalSet {
    param([string[]]$Values)

    $set = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($value in $Values) {
        [void]$set.Add($value)
    }
    return ,$set
}

function Get-ExpectedDirectories {
    param([string[]]$FilePaths)

    $directories = New-OrdinalSet
    foreach ($filePath in $FilePaths) {
        $segments = $filePath.Split('/')
        for ($index = 1; $index -lt $segments.Count; $index++) {
            [void]$directories.Add(($segments[0..($index - 1)] -join '/'))
        }
    }
    return ,$directories
}

function Test-NetworkImport {
    param([string]$Line)

    $code = ($Line -replace '\s+#.*$', '').Trim()
    $fromMatch = [regex]::Match($code, '^from\s+([A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*\s+import\s+')
    if ($fromMatch.Success) {
        return $fromMatch.Groups[1].Value -in @('requests', 'urllib', 'httpx', 'aiohttp', 'socket')
    }

    $importMatch = [regex]::Match($code, '^import\s+(.+)$')
    if (-not $importMatch.Success) {
        return $false
    }
    foreach ($clause in ($importMatch.Groups[1].Value -split ',')) {
        $moduleMatch = [regex]::Match($clause.Trim(), '^([A-Za-z_]\w*)')
        if ($moduleMatch.Success -and $moduleMatch.Groups[1].Value -in @('requests', 'urllib', 'httpx', 'aiohttp', 'socket')) {
            return $true
        }
    }
    return $false
}

function Get-ProcessCalls {
    param([string]$Line)

    $callNames = @(
        'os\.(?:system|popen|startfile|spawn\w*|exec\w*)',
        'subprocess\.\w+'
    )
    $callPattern = '(?<![\w.])(?:' + ($callNames -join '|') + ')\s*\('
    $calls = [System.Collections.Generic.List[string]]::new()
    foreach ($match in [regex]::Matches($Line, $callPattern)) {
        $openParen = $Line.IndexOf('(', $match.Index)
        $depth = 0
        for ($index = $openParen; $index -lt $Line.Length; $index++) {
            if ($Line[$index] -eq '(') {
                $depth++
            }
            elseif ($Line[$index] -eq ')') {
                $depth--
                if ($depth -eq 0) {
                    $calls.Add($Line.Substring($match.Index, $index - $match.Index + 1))
                    break
                }
            }
        }
        if ($depth -ne 0) {
            $calls.Add($Line.Substring($match.Index))
        }
    }
    return $calls.ToArray()
}

$pythonImportScanner = @'
import ast
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8-sig") as source_file:
        tree = ast.parse(source_file.read(), filename=sys.argv[1])
except SyntaxError as error:
    print(json.dumps({"error": "syntax error: " + error.msg, "line": error.lineno or 1}))
    raise SystemExit(2)
except Exception as error:
    print(json.dumps({"error": type(error).__name__ + ": " + str(error), "line": 1}))
    raise SystemExit(2)

findings = [
    {"kind": "process_from", "line": node.lineno}
    for node in ast.walk(tree)
    if isinstance(node, ast.ImportFrom)
    and node.level == 0
    and node.module in {"os", "subprocess"}
]
findings.sort(key=lambda finding: finding["line"])
print(json.dumps({"findings": findings}))
'@

function Resolve-PythonInterpreter {
    foreach ($name in @('python3', 'python')) {
        $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $command) {
            continue
        }

        try {
            $interpreterOutput = @(& $command.Source -c 'import ast, sys; print("SKINBENCH_AST_OK"); raise SystemExit(0 if sys.version_info[0] == 3 else 1)' 2>&1)
            if ($LASTEXITCODE -eq 0 -and $interpreterOutput[-1].ToString().Trim() -eq 'SKINBENCH_AST_OK') {
                return $command.Source
            }
        }
        catch {
            continue
        }
    }
    return $null
}

function Invoke-ProcessImportScan {
    param(
        [string]$Path,
        [string]$Relative,
        [AllowNull()][string]$Interpreter
    )

    if ([string]::IsNullOrWhiteSpace($Interpreter)) {
        Add-Violation "Python import scan failed in $Relative`:1 (Python 3 interpreter not found)"
        return
    }

    try {
        $scanOutput = @(& $Interpreter -c $script:pythonImportScanner $Path 2>&1)
        $scanExitCode = $LASTEXITCODE
    }
    catch {
        Add-Violation "Python import scan failed in $Relative`:1 ($($_.Exception.Message))"
        return
    }

    $scanText = ($scanOutput | ForEach-Object { $_.ToString() }) -join "`n"
    try {
        $scanResult = $scanText | ConvertFrom-Json -NoEnumerate -ErrorAction Stop
    }
    catch {
        Add-Violation "Python import scan failed in $Relative`:1 (invalid scanner output)"
        return
    }

    if (
        $null -eq $scanResult -or
        $scanResult.GetType() -ne [System.Management.Automation.PSCustomObject]
    ) {
        Add-Violation "Python import scan failed in $Relative`:1 (invalid scanner result)"
        return
    }

    if ($scanExitCode -ne 0) {
        $lineProperty = $scanResult.PSObject.Properties['line']
        $errorProperty = $scanResult.PSObject.Properties['error']
        if (
            $null -eq $lineProperty -or $lineProperty.Value -isnot [long] -or $lineProperty.Value -le 0 -or
            $null -eq $errorProperty -or $errorProperty.Value -isnot [string] -or
            [string]::IsNullOrWhiteSpace($errorProperty.Value)
        ) {
            Add-Violation "Python import scan failed in $Relative`:1 (invalid scanner error)"
            return
        }
        $errorLine = $lineProperty.Value
        $errorMessage = $errorProperty.Value
        Add-Violation "Python import scan failed in $Relative`:$errorLine ($errorMessage)"
        return
    }

    $findingsProperty = $scanResult.PSObject.Properties['findings']
    if ($null -eq $findingsProperty -or $findingsProperty.Value -isnot [System.Array]) {
        Add-Violation "Python import scan failed in $Relative`:1 (invalid findings)"
        return
    }

    foreach ($finding in $findingsProperty.Value) {
        if ($null -eq $finding -or $finding -isnot [pscustomobject]) {
            Add-Violation "Python import scan failed in $Relative`:1 (invalid finding)"
            return
        }
        $kindProperty = $finding.PSObject.Properties['kind']
        $lineProperty = $finding.PSObject.Properties['line']
        if (
            $null -eq $kindProperty -or $kindProperty.Value -isnot [string] -or
            $kindProperty.Value -cne 'process_from' -or
            $null -eq $lineProperty -or $lineProperty.Value -isnot [long] -or
            $lineProperty.Value -le 0
        ) {
            Add-Violation "Python import scan failed in $Relative`:1 (invalid finding)"
            return
        }
        Add-Violation "Process-capable alias import in $Relative`:$($lineProperty.Value)"
    }
}

function Test-ProcessModuleAliasImport {
    param([string]$Line)

    $code = ($Line -replace '\s+#.*$', '')
    foreach ($statement in ($code -split ';')) {
        $importMatch = [regex]::Match($statement.Trim(), '^import\s+(.+)$')
        if (-not $importMatch.Success) {
            continue
        }
        foreach ($clause in ($importMatch.Groups[1].Value -split ',')) {
            if ($clause.Trim() -cmatch '^(?:os|subprocess)\s+as\s+[A-Za-z_]\w*$') {
                return $true
            }
        }
    }
    return $false
}

function Test-AllowedProcessCall {
    param([string]$Call)

    return (
        $Call -cmatch '^os\.startfile\(\s*core\.pose_dir\(\s*\)\s*\)$' -or
        $Call -cmatch '^os\.startfile\(\s*folder\s*\)$' -or
        $Call -cmatch '^subprocess\.Popen\(\s*\[\s*"xdg-open"\s*,\s*folder\s*\]\s*\)$'
    )
}

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    Add-Violation "Root directory does not exist: $Root"
}
else {
    $rootItem = Get-Item -LiteralPath $Root -Force
    $rootPath = $rootItem.FullName
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Add-Violation "Reparse point is not allowed: ."
    }

    if ($Profile -eq 'Package' -and $rootItem.Name -ne 'SkinBench-1.0.0-beta.1') {
        Add-Violation "Package root must be SkinBench-1.0.0-beta.1, found: $($rootItem.Name)"
    }

    $items = @(Get-ChildItem -LiteralPath $rootPath -Force -Recurse)
    $repositoryVenvPath = $null
    if ($Profile -eq 'Repository') {
        $repositoryVenv = @(
            $items | Where-Object {
                $relative = Get-RelativePath -BasePath $rootPath -Path $_.FullName
                [string]::Equals(
                    $relative,
                    '.venv-release',
                    [System.StringComparison]::Ordinal
                ) -and $_.PSIsContainer -and
                (($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)
            }
        )
        if ($repositoryVenv.Count -eq 1) {
            try {
                $trackedVenvFiles = @(
                    & git -C $rootPath ls-files -- .venv-release 2>$null |
                        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
                )
                if ($LASTEXITCODE -ne 0) {
                    Add-Violation 'Cannot prove .venv-release contains no tracked files'
                }
                elseif ($trackedVenvFiles.Count -gt 0) {
                    foreach ($trackedVenvFile in $trackedVenvFiles) {
                        Add-Violation "Tracked file beneath .venv-release is not allowed: $trackedVenvFile"
                    }
                }
                else {
                    $repositoryVenvPath = $repositoryVenv[0].FullName
                }
            }
            catch {
                Add-Violation 'Cannot prove .venv-release contains no tracked files'
            }
        }
    }
    $publicItems = @(
        $items | Where-Object {
            $relative = Get-RelativePath -BasePath $rootPath -Path $_.FullName
            $isRepositoryVenvItem = (
                $null -ne $repositoryVenvPath -and
                (
                    [string]::Equals(
                        $_.FullName,
                        $repositoryVenvPath,
                        [System.StringComparison]::Ordinal
                    ) -or $_.FullName.StartsWith(
                        ($repositoryVenvPath + [System.IO.Path]::DirectorySeparatorChar),
                        [System.StringComparison]::Ordinal
                    )
                )
            )
            -not (
                $isRepositoryVenvItem -or
                ($Profile -eq 'Repository' -and ($relative -match '^(?:\.git|dist)(?:/|$)'))
            )
        }
    )

    foreach ($item in $publicItems) {
        $relative = Get-RelativePath -BasePath $rootPath -Path $item.FullName
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Add-Violation "Reparse point is not allowed: $relative"
        }
        foreach ($pattern in $forbiddenPathPatterns) {
            if ($relative -match $pattern) {
                Add-Violation "Forbidden path: $relative (matches $pattern)"
            }
        }
    }

    $files = @($publicItems | Where-Object { -not $_.PSIsContainer })
    $directories = @($publicItems | Where-Object { $_.PSIsContainer })
    $actualPaths = @($files | ForEach-Object { Get-RelativePath -BasePath $rootPath -Path $_.FullName })
    $expectedPaths = if ($Profile -eq 'Package') { @($expectedPackage) } else { @($expectedRepository) }
    $requiredPaths = @($expectedPaths)
    if ($Profile -eq 'Repository' -and -not $RequireScreenshot) {
        $requiredPaths = @($requiredPaths | Where-Object { $_ -ne 'docs/images/skinbench-overview.png' })
    }

    $actualFileSet = New-OrdinalSet -Values $actualPaths
    $expectedFileSet = New-OrdinalSet -Values $expectedPaths
    $requiredFileSet = New-OrdinalSet -Values $requiredPaths
    $expectedDirectorySet = Get-ExpectedDirectories -FilePaths $expectedPaths

    foreach ($expected in $requiredFileSet) {
        if (-not $actualFileSet.Contains($expected)) {
            Add-Violation "Missing file: $expected"
        }
    }

    foreach ($actual in $actualPaths) {
        if (-not $expectedFileSet.Contains($actual)) {
            Add-Violation "Unexpected file: $actual"
        }
    }
    foreach ($directory in $directories) {
        $relative = Get-RelativePath -BasePath $rootPath -Path $directory.FullName
        if (-not $expectedDirectorySet.Contains($relative)) {
            Add-Violation "Unexpected directory: $relative"
        }
    }

    foreach ($file in $files) {
        $relative = Get-RelativePath -BasePath $rootPath -Path $file.FullName
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            continue
        }
        if ($file.Length -eq 0) {
            Add-Violation "Zero-byte file is not allowed: $relative"
        }

        $text = Get-Utf8Text -Path $file.FullName
        if ($null -eq $text) {
            continue
        }

        foreach ($record in $privateSignatureRecords) {
            if (Test-PrivateSignatureRecord -Text $text -Record $record) {
                Add-Violation "Private signature $($record.Id) found in: $relative"
            }
        }
    }

    $shippingPython = @(
        $files | Where-Object {
            $relative = Get-RelativePath -BasePath $rootPath -Path $_.FullName
            (($relative -eq 'install.py' -or
            $relative -match '^plug-ins/[^/]+\.py$' -or
            $relative -match '^scripts/.+\.py$') -and
            (($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0))
        }
    )
    $pythonInterpreter = Resolve-PythonInterpreter

    foreach ($file in $shippingPython) {
        $relative = Get-RelativePath -BasePath $rootPath -Path $file.FullName
        $text = Get-Utf8Text -Path $file.FullName
        if ($null -eq $text) {
            Add-Violation "Shipping Python is not UTF-8-readable: $relative"
            continue
        }

        Invoke-ProcessImportScan -Path $file.FullName -Relative $relative -Interpreter $pythonInterpreter

        $lineNumber = 0
        foreach ($line in ($text -split "`r?`n")) {
            $lineNumber++
            if (Test-NetworkImport -Line $line) {
                Add-Violation "Network import in $relative`:$lineNumber"
            }
            if ($line -match '(?<![\w.])(?:eval|exec)\s*\(') {
                Add-Violation "Dynamic execution in $relative`:$lineNumber"
            }
            if ($line -match 'os\.system\s*\(') {
                Add-Violation "os.system in $relative`:$lineNumber"
            }
            if ($line -match 'shell\s*=\s*True') {
                Add-Violation "shell=True in $relative`:$lineNumber"
            }
            if (Test-ProcessModuleAliasImport -Line $line) {
                Add-Violation "Process-capable alias import in $relative`:$lineNumber"
            }
            foreach ($call in (Get-ProcessCalls -Line $line)) {
                if (-not (Test-AllowedProcessCall -Call $call)) {
                    Add-Violation "Disallowed process call in $relative`:$lineNumber ($call)"
                }
            }
        }
    }

    $requiredSourceLines = @{
        'install.py' = '^\s*VERSION\s*=\s*"1\.0\.0-beta\.1"\s*(?:#.*)?$'
        'scripts/skinbench/__init__.py' = '^\s*__version__\s*=\s*"1\.0\.0-beta\.1"\s*(?:#.*)?$'
        'plug-ins/skinBench.py' = '^\s*VERSION\s*=\s*skinbench\.__version__\s*(?:#.*)?$'
    }
    foreach ($relative in $requiredSourceLines.Keys) {
        $path = Join-Path $rootPath ($relative -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Add-Violation "Version source is missing: $relative"
            continue
        }
        $text = Get-Utf8Text -Path $path
        $matches = @([regex]::Matches($text, $requiredSourceLines[$relative], [System.Text.RegularExpressions.RegexOptions]::Multiline))
        if ($matches.Count -ne 1) {
            Add-Violation "Expected exactly one version declaration in: $relative"
        }
    }

    $sourceVersionDeclarations = 0
    $pluginVersionReferences = 0
    foreach ($file in $shippingPython) {
        $text = Get-Utf8Text -Path $file.FullName
        if ($null -eq $text) {
            continue
        }
        foreach ($line in ($text -split "`r?`n")) {
            if ($line -match '^\s*(?:VERSION|__version__)\s*=\s*skinbench\.__version__\s*(?:#.*)?$') {
                $pluginVersionReferences++
            }
            elseif ($line -match '^\s*(?:VERSION|__version__)\s*=') {
                $sourceVersionDeclarations++
            }
        }
    }
    if ($sourceVersionDeclarations -ne 2) {
        Add-Violation "Expected exactly two source version declarations, found: $sourceVersionDeclarations"
    }
    if ($pluginVersionReferences -ne 1) {
        Add-Violation "Expected exactly one plugin version reference, found: $pluginVersionReferences"
    }

    if ($Profile -eq 'Package') {
        $manifestPath = Join-Path $rootPath 'SHA256SUMS.txt'
        if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
            $manifestText = Get-Utf8Text -Path $manifestPath
            if ($null -eq $manifestText) {
                Add-Violation 'SHA256SUMS.txt is not UTF-8-readable'
            }
            else {
                $manifestEntries = @()
                $manifestLineNumber = 0
                $manifestLines = @($manifestText -split "`r?`n")
                if ($manifestLines.Count -gt 0 -and $manifestLines[-1] -eq '') {
                    $manifestLines = if ($manifestLines.Count -eq 1) {
                        @()
                    }
                    else {
                        @($manifestLines[0..($manifestLines.Count - 2)])
                    }
                }
                foreach ($line in $manifestLines) {
                    $manifestLineNumber++
                    if ([string]::IsNullOrWhiteSpace($line)) {
                        Add-Violation "Invalid SHA256SUMS.txt line: $manifestLineNumber"
                        continue
                    }
                    $match = [regex]::Match($line, '^([a-fA-F0-9]{64})  ([^\\/].*)$')
                    if (-not $match.Success) {
                        Add-Violation "Invalid SHA256SUMS.txt line: $manifestLineNumber"
                        continue
                    }
                    $manifestEntries += [pscustomobject]@{
                        Hash = $match.Groups[1].Value.ToLowerInvariant()
                        Path = $match.Groups[2].Value.Replace('\\', '/')
                    }
                }

                $manifestPaths = @($manifestEntries | ForEach-Object { $_.Path })
                $sortedManifestPaths = [string[]]$manifestPaths.Clone()
                [Array]::Sort($sortedManifestPaths, [System.StringComparer]::Ordinal)
                if (($manifestPaths -join "`n") -ne ($sortedManifestPaths -join "`n")) {
                    Add-Violation 'SHA256SUMS.txt paths are not sorted'
                }
                $manifestPathSet = New-OrdinalSet
                foreach ($manifestPath in $manifestPaths) {
                    if (-not $manifestPathSet.Add($manifestPath)) {
                        Add-Violation "SHA256SUMS.txt contains duplicate path: $manifestPath"
                    }
                }

                $hashedPaths = @($expectedPackage | Where-Object { $_ -ne 'SHA256SUMS.txt' })
                $hashedPathSet = New-OrdinalSet -Values $hashedPaths
                foreach ($expected in $hashedPaths) {
                    if (-not $manifestPathSet.Contains($expected)) {
                        Add-Violation "SHA256SUMS.txt is missing: $expected"
                    }
                }
                foreach ($actual in $manifestPaths) {
                    if (-not $hashedPathSet.Contains($actual)) {
                        Add-Violation "SHA256SUMS.txt has unexpected path: $actual"
                    }
                }

                foreach ($entry in $manifestEntries) {
                    if (-not $hashedPathSet.Contains($entry.Path)) {
                        continue
                    }
                    $filePath = Join-Path $rootPath ($entry.Path -replace '/', [System.IO.Path]::DirectorySeparatorChar)
                    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
                        continue
                    }
                    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash.ToLowerInvariant()
                    if ($actualHash -ne $entry.Hash) {
                        Add-Violation "SHA256SUMS.txt hash mismatch: $($entry.Path)"
                    }
                }
            }
        }
    }
}

if ($violations.Count -gt 0) {
    $violations | ForEach-Object { Write-Host "[FAIL] $_" }
    exit 1
}

if (-not [string]::IsNullOrWhiteSpace($PrivacySelfTestValuesPath)) {
    try {
        Invoke-PrivacySelfTest -ValuesPath $PrivacySelfTestValuesPath
        Write-Host '[PASS] SkinBench privacy self-test'
    }
    catch {
        Write-Host "[FAIL] SkinBench privacy self-test: $($_.Exception.Message)"
        exit 1
    }
}

Write-Host "[PASS] SkinBench $Profile release policy"
