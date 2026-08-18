# Development

SkinBench is a source-only Maya module. Runtime code has no third-party
dependencies. It uses Python, Maya commands, OpenMaya, and the PySide binding
bundled with Maya. Development-only checks are listed in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Module layout

```text
install.py
plug-ins/
  skinBench.py
scripts/
  skinbench/
    __init__.py
    compat.py
    core.py
    ui.py
tests/
  release_policy.ps1
  smoke_mayapy.py
tools/
  build_release.ps1
```

- `install.py` writes the Maya module pointer, primes the current process,
  loads the plugin, enables auto-load, and adds the shelf button.
- `plug-ins/skinBench.py` registers the Maya commands and menu.
- `scripts/skinbench/core.py` owns scene and weight logic without importing
  the UI.
- `scripts/skinbench/compat.py` selects PySide6 or PySide2.
- `scripts/skinbench/ui.py` builds the dockable five-tab panel.

## Import flow

The module file points Maya at the extracted repository or release folder.
Maya adds its `scripts/` and `plug-ins/` directories to the relevant paths.
The plugin imports `skinbench`, then `skinbench.core`; the shelf command calls
`skinbench.show()` to import and show the UI.

During development, reload the package from Maya's Python Script Editor:

```python
import skinbench
skinbench.reload_all()
```

Reloading source does not replace the need to reopen the panel or restart Maya
when Maya itself keeps an old UI or plugin object alive.

## Self-tests

The mayapy smoke test runs the real package and these core checks:

- `core.bind_pose_selftest()`
- `core.mirror_selftest()`
- `core.rom_selftest()`
- `core.heal_selftest()`
- `core.drag_selftest()`
- `core.patch_selftest()`
- `core.bleed_selftest()`
- `core.grow_selftest()`
- `core.pose_selftest()`
- `core.weight_write_selftest(True)`

It also checks local INI preference storage and loads the real plugin. The
weight-write check creates and removes a disposable scene fixture; run it only
in mayapy or an empty disposable Maya scene.

## CI scopes

Repository automation covers:

- Python 3.7, 3.9, 3.10, and 3.11 syntax compilation;
- fatal Ruff rules for syntax errors, undefined names, and invalid control
  flow;
- Bandit medium-and-higher security findings;
- the repository release allowlist and privacy/process scanner;
- release packaging and checksum verification in CI and the manually dispatched
  draft-release workflow.

Maya 2025 mayapy smoke is a required local acceptance check. GitHub-hosted
runners do not include Maya, so the public workflow does not claim to run that
test.

The Python 3.7 job is a syntax boundary. It is not a claim that every Maya and
operating-system combination was live tested.

## Release policy

Public releases contain only the documented source, community files, approved
screenshot, tests, build script, and checksum manifest. The release policy
rejects unexpected files, private path or project text, scene and rig assets,
archives, logs, snapshots, bytecode, unsafe process calls, dynamic execution,
and runtime network imports.

Version declarations must agree on `1.0.0-beta.1` for this release. Public
support covers only the latest beta. Compatibility wording must stay bounded
by [the evidence table](compatibility.md).

## Release build

After an approved screenshot exists, release maintainers run the repository
policy with the screenshot gate, then build a clean archive:

```powershell
pwsh -NoProfile -File .\tests\release_policy.ps1 `
  -Profile Repository -Root . -RequireScreenshot
pwsh -NoProfile -File .\tools\build_release.ps1 `
  -SourceRoot . -OutputDirectory .\dist
```

The build produces a versioned source package and SHA-256 checksums. It does
not compile, minify, or download the runtime.

## Review gates

A release must pass, in order:

1. repository policy, syntax, Ruff, Bandit, and mayapy smoke checks;
2. a diff review that confirms no private files, runtime dependency, network,
   or telemetry change was introduced silently;
3. an independent source review;
4. a maintainer check in Maya using a disposable scene;
5. the screenshot-required release policy and clean package verification.

Builders do not approve their own release. Publication and release tags remain
maintainer actions.
