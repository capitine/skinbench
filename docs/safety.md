# Safety

SkinBench is source-only Maya tooling. Read this page before using any control
that changes a scene.

## Install and uninstall

The installer writes `Documents/maya/modules/skinbench.mod`. It points Maya at
the extracted folder rather than copying the runtime. It loads the plugin,
enables Maya auto-load, and adds one shelf button.

Uninstall means removing the module file, shelf button, and extracted folder
after Maya is closed.

For optional full cleanup, you may also remove SkinBench's local preferences,
logs, pose files, and snapshots after saving anything you want to keep. These
files are not required for the basic uninstall.

## What the runtime does not do

SkinBench has no telemetry, network client, account, background service,
remote download, admin request, or registry write. The runtime does not install
Python packages or copy itself into Maya.

`Open pose folder` and `Open log folder` invoke only a fixed
operating-system folder opener. They do not accept a command or web address
from the scene.

## Local file writes

JSON snapshots and daily text logs are local file writes. SkinBench also saves
named poses as JSON and stores UI preferences in a local `settings.ini` under
Maya's per-user application folder.

- A saved scene's weight snapshots go under
  `<scene folder>/skinbench/<scene name>/`.
- An unsaved scene's snapshots go under Maya's per-user application folder.
- Daily logs go under Maya's per-user `skinbench/logs/` folder by default.
- Named poses and `settings.ini` stay under Maya's per-user `skinbench/`
  folder.

Snapshot, pose, log, and preference files can contain node names, scene names,
or local paths. Review them before sharing. Do not publish files from a client
or proprietary production.

## Scene changes

Explicit controls can change joint state, bind data, and skin weights in the
open scene. Examples include:

- moving, mirroring, reorienting, or posing joints;
- refreshing bind poses and bind pre-matrices;
- detaching and rebuilding a skinCluster;
- applying, mirroring, healing, solving, stripping, or zeroing skin weights.

Selection, isolate, symmetry-check, and preview controls are intended to avoid
a write, but they can still change the current selection, view, or temporary
pose. Read the log after a refusal or unexpected result.

## Leftover vertex drags

A refused drag is a real point offset. It can remain on a skinned mesh and be
saved or exported even though no weights were written. SkinBench scans skinned
meshes when its window opens. If it finds stored offsets, it opens the log and
names the vertex count and mesh. This check reads polygon meshes only. If a
skinCluster drives unsupported geometry, such as a NURBS surface, the log names
that shape as unverified while preserving results from supported meshes.

SkinBench does not intercept Maya's Save or Export commands. Use **Undo any
drag still on the mesh** before saving or exporting after a refused drag. That
control removes point offsets from supported polygon meshes without changing
skin weights. With nothing selected, it checks every skinned polygon mesh in
the scene; select vertices or a skinned object to limit the scope. The whole
cleanup is one Maya undo step. It continues past unsupported geometry and names
every skipped shape as unverified.

## Weight recovery

API weight writes are not restored by Maya Ctrl+Z; guarded writes create a
snapshot first, and artists still test on a disposable copy.

Use this routine:

1. Save a disposable copy of the scene.
2. Return the rig to rest unless the guide explicitly says posing is allowed.
3. Run the preview or check control first when one exists.
4. Inspect the selected vertices and the SkinBench log.
5. Run the write only when the preview matches your intent.
6. Save, reopen, and check the repaired scene before replacing any working
   file.

If a weight write is wrong, use the **Detach / Reattach** tab to apply the
snapshot created before the write. Keep older snapshots until recovery is
confirmed.

## Reporting safely

Follow [SECURITY.md](../SECURITY.md) for vulnerabilities and
[SUPPORT.md](../SUPPORT.md) for normal problems. Never attach proprietary rigs,
scenes, client data, credentials, logs containing private paths, or skin-weight
snapshots to a public report.
