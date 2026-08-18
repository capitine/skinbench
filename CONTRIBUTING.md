# Contributing to SkinBench

Focused bug fixes, tests, documentation corrections, and compatibility reports
are welcome. Read [Safety](docs/safety.md) before changing scene or weight-write
code.

## Privacy and reproduction rule

Every report and test fixture must use a disposable reproduction. Do not commit
or attach proprietary rigs, Maya scenes, client data, credentials, private
paths, logs, snapshots, or production names. Reduce the problem to the smallest
neutral polygon mesh and joint setup that still demonstrates it.

## Compatibility boundary

Shipping Python must remain valid Python 3.7 syntax. CI compiles it on Python
3.7, 3.9, 3.10, and 3.11. That syntax boundary does not expand the live-tested
compatibility claims in [docs/compatibility.md](docs/compatibility.md).

Do not silently add a runtime dependency, network access, telemetry, account,
background service, download, admin requirement, or registry write. Any
proposed change to the runtime access boundary must be explicit in the pull
request and requires maintainer approval, safety documentation, and tests.

## Local checks

Run these commands in PowerShell from the standalone repository root.

```powershell
py -3.11 -m venv .\.venv-release
& .\.venv-release\Scripts\python.exe -m pip install `
  --disable-pip-version-check `
  -r .\requirements-dev.txt

$python = '.\.venv-release\Scripts\python.exe'
& $python -m ruff check --select E9,F63,F7,F82 `
  .\install.py .\plug-ins .\scripts .\tests
& $python -m bandit -r .\install.py .\plug-ins .\scripts -ll
pwsh -NoProfile -File .\tests\release_policy.ps1 `
  -Profile Repository -Root .
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'C:\Program Files\Autodesk\Maya2025\bin\mayapy.exe' -B `
  .\tests\smoke_mayapy.py .
```

CI also runs this exact syntax command on Python 3.7, 3.9, 3.10, and 3.11:

```powershell
python -m compileall -q install.py plug-ins scripts
```

The ordinary contributor checks do not require the approved public screenshot.
After the screenshot exists, release maintainers also run:

```powershell
pwsh -NoProfile -File .\tests\release_policy.ps1 `
  -Profile Repository -Root . -RequireScreenshot
pwsh -NoProfile -File .\tools\build_release.ps1 `
  -SourceRoot . -OutputDirectory .\dist
```

## Pull requests

After publication, maintenance is pull-request-only. Do not push a change
directly to the protected default branch.

A pull request should:

- explain the user-visible problem and the smallest fix;
- include a focused test that failed before the fix when behavior changed;
- list the exact checks run and their results;
- keep compatibility claims tied to evidence;
- confirm that no private or generated asset entered the diff;
- update the user, safety, or development docs when their contract changes.

Builders do not approve their own work. A maintainer and an independent review
gate decide whether a change can merge or release.

## Conduct and security

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report
vulnerabilities through the private path in [SECURITY.md](SECURITY.md), not a
public issue.
