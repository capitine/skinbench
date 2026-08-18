# SkinBench User Guide

SkinBench is a five-tab Maya panel for repairing skinned polygon meshes. It
keeps common repair steps together, writes a daily local log, and creates JSON
weight snapshots before guarded API writes.

## Safety first

Work on a disposable copy until a repair has been saved, reopened, and checked.
SkinBench can change joints, bind data, skinClusters, and skin weights. API
weight writes are not restored by Maya Ctrl+Z. Read [Safety](safety.md) before
using an amber write button.

## Install and open

1. Extract the release to a permanent folder.
2. Drag `install.py` into a Maya viewport.
3. Click the SkinBench shelf button.

You can also open the panel with the MEL command:

```mel
skinBench;
```

The **Refresh from selection** button is above every tab. Use it after changing
the selected mesh, skinCluster, or joint.

## Choose a tab

| Need | Tab |
|---|---|
| Move or mirror joints without detaching the skin | **Move Joints** |
| Mirror, heal, solve, isolate, or clean weights | **Weights** |
| Save, apply, or clear joint rotations | **Poses** |
| Change the joint list or hierarchy, then rebuild the skin | **Detach / Reattach** |
| Fix the bind data of a newly added influence | **Bind Photo** |

Use **Move Joints** for ordinary joint placement. Use **Detach / Reattach**
when the skeleton structure itself must change.

## Move Joints tab

### Move a skinned joint

Select a skinned mesh or one of its influence joints.

1. Click **Unlock joints (mjm ON)**.
2. Move or rotate the joint with Maya's normal tools. The mesh stays in place.
3. Click **Lock in (mjm OFF)**.
4. Read the log and inspect the mesh.

`Move Skinned Joints`, shortened to `mjm`, is a skinCluster setting. If one
joint drives several meshes, tick **Apply to every skinCluster in the scene**
or refresh from a selection that reaches every affected cluster.

Do not use Maya's `D` / Edit Pivot mode to reposition a skinned joint. That
creates a pivot offset instead of moving the joint itself, and exported motion
may rotate around a different point.

When SkinBench locks the skin again, it checks bind-pose members and refreshes
stale entries. **Freshen bind poses** runs that check on demand. Both operations
refuse while the skeleton is posed because storing a posed matrix as the new
rest state would corrupt the bind reference. Return to rest from the **Poses**
tab first.

### Mirror a joint

The mirror section finds the opposite-side joint by its name pattern and can
mirror the selected joint across the X axis.

- **Mirror position only** keeps the target orientation when only placement
  needs correction.
- **Include the whole joint hierarchy** mirrors the selected chain.
- **Preserve children in world space** keeps child joints from being dragged
  by a parent-only adjustment.

Run this at rest. Check the result from several camera angles, then undo with
Maya if the joint operation is not correct. Weight mirroring is a separate
operation in the **Weights** tab.

### Bake rotate into jointOrient

After reorienting a joint, its new rest angle may still be in the `rotate`
channels. **Bake rotate → jointOrient** moves that angle into `jointOrient` and
returns `rotate` to zero while preserving the joint's world transform.

Tick **Include the whole joint hierarchy** for a chain. Locked or driven
channels are skipped and named in the log. Bake only after the placement and
bind-pose checks are correct.

## Weights tab

The Weights tab contains checks and writes. Controls labelled **Check** or
**Preview** do not write weights. Amber controls labelled **writes** do.

### Mirror painted weights

1. Return the rig to rest.
2. Pick the skinCluster and direction.
3. Choose **exact twin vertex** for a symmetric mesh, or **nearest vertex** for
   a deliberately asymmetric mesh.
4. Click **Check symmetry (changes nothing)**.
5. Review the pair count and maximum mismatch.
6. Mirror the whole cluster, selected vertices only, or every cluster in the
   current selection.

The far side is overwritten; it is not blended with the source. A snapshot is
created first. Whole-cluster and selected-vertex mirror writes refuse when the
rig is posed because vertex pairing would be based on the posed shape. If
nearest matching would reuse one source or destination for different pairs,
SkinBench refuses that mesh before creating a snapshot or changing weights.

### Heal selected vertices

**Heal selected vertices (writes)** runs Maya's Weight Hammer on the selected
polygon vertices. Each selected vertex replaces its weights with the average
of its neighbours.

Use it for a small spike or isolated bad vertex, not a whole smeared region.
SkinBench unlocks locked influences first and leaves them unlocked so Maya's
paint tools can edit them. The log lists changed influences. A snapshot is
created first, but Ctrl+Z does not restore the API weight write.

### Solve weights from a drag

Drag solve works backwards from the result you want:

1. Pose the rig so the defect is visible.
2. Choose **slide along the skin**, **slide along the edges**, or **drag freely
   in space**.
3. Click **Get me ready to drag**.
4. Move one or more vertices to the desired visible position.
5. Set the maximum joints per vertex.
6. Click **Preview the solve (changes nothing)**.
7. Read the delivered percentage and proposed influence shares.
8. If the anatomy and result make sense, click **Paint the solved weights
   (writes)**.

The write paints the solution and clears the vertex drag so the weights, not a
point offset, hold the vertex in place. It creates a snapshot first.

The solver uses only influences already present on the vertex or its immediate
neighbours. It refuses:

- at rest, where changing weights cannot move the vertex;
- when the requested point lies mostly outside what the joints can reach;
- when plain smooth-skinning math cannot reproduce unaffected vertices on the
  same mesh.

A refused drag remains on the mesh so it can be shortened and tried again.
That point offset is real and can be saved or exported. Use **Undo any drag
still on the mesh** to clear it without changing weights. With nothing
selected, the action checks every skinned polygon mesh in the scene; select
vertices or a skinned object to limit it. One Maya undo restores the whole
cleanup.

When the SkinBench window opens, it checks every skinned mesh for these stored
point offsets. If it finds any, the log opens with the vertex count and mesh
name. Unsupported skinned geometry is named as unverified; it does not hide
results from polygon meshes. SkinBench does not intercept Maya's Save or Export
commands, so run **Undo any drag still on the mesh** before saving or exporting
after a refused drag.

Several valid weight mixes may place a vertex at the same point. Always inspect
the named influences. A mathematically valid mix can still be anatomically
wrong. Drag solve does not smooth the result into neighbouring vertices; use
**Heal** on nearby vertices if the solved point leaves a visible spike.

### Select and isolate a driven region

Choose one or more joints, optionally include their hierarchy, then use:

- **Select region** to select vertices carrying at least the region threshold;
- **Select + isolate** to hide the rest of the mesh;
- **Show all** to leave isolate mode;
- **Invert** to select the opposite region;
- **Grow** and **Shrink** to add or remove topology rings;
- the direction control to keep growth on one side of the starting selection.

These are ordinary Maya selections. Shift adds and Ctrl removes. Selection and
isolation do not write weights.

### Anti-bleed distance preview

Bleed means a joint influences a vertex far outside its plausible reach.

1. Return the rig to rest.
2. Pick a skinCluster and one or more joints. With no joint chosen, every
   influence is audited.
3. Set `N`, the number of bone lengths used as the radius.
4. Click **Preview**.
5. Inspect the selected vertices in the viewport and all four log buckets.
6. Adjust `N` and preview again if needed.
7. Click **Strip bleeding weights (writes)** only after the selected vertices
   are clearly unwanted.

The radius is `N × the joint's longest attached bone`, using incoming or
outgoing segments. This scales the test to the skeleton. A joint with no usable
bone length is skipped.

Preview assigns every influence/vertex pair to one bucket:

| Bucket | Meaning | Action |
|---|---|---|
| **strippable** | Far from the joint and the joint is not the main owner | Preview selects it; Strip can remove it |
| **kept** | Far away, but this joint is the vertex's main owner | Reassign or repaint; do not strip automatically |
| **ignored** | Weight below `0.005` | Use Maya's Prune Small Weights if desired |
| **fine** | Inside the radius | No action |

Strip creates a snapshot, removes only the strippable weights, and
renormalizes survivors. It refuses on a posed rig. The distance is a straight
line, so a thin gap can make two distant surface regions appear close. Preview
and artist judgment remain required.

### Home-patch anti-bleed

Home patch asks whether a painted area is connected to the joint's main area of
work, rather than how far away it is.

1. Choose the skinCluster and joint.
2. Set **stickiness**, the minimum weight that may connect two neighbouring
   vertices.
3. Set **home reach**, the allowed distance from an island to the joint in bone
   lengths.
4. Click **Preview islands**.
5. Adjust the selected vertices with Maya selection tools or Grow/Shrink.
6. Click **Zero everything OUTSIDE the joint's patch (writes)** to zero exactly
   the current selection.

Home is the heaviest connected island that also reaches the joint. Dust below
the stickiness value cannot form a bridge. The write refuses if no island
reaches the joint, and it holds back vertices that would be left with almost no
other influence. Those require repainting by hand. A snapshot is created before
the accepted write.

### Zero selected weights

**Zero these joints on the SELECTION (writes)** removes the chosen joints from
exactly the currently selected vertices, then renormalizes the remaining
weights. It creates a snapshot first. Use a preview or deliberate Maya
selection to define the target before pressing it.

## Poses tab

Pose files store joint rotations as JSON under Maya's per-user SkinBench pose
folder. Saving a pose does not edit the scene.

### Save a pose

Enter a useful name and click **Save pose (whole skeleton)**. The pose records
the current skeleton rotations and can be used in another scene.

### Apply a pose

- **Apply — same character (exact)** applies stored rotations to matching
  joints.
- **…other biped (world-space)** maps same-named joints approximately in world
  space. Treat this as a starting point and inspect every result.
- **Open pose folder** opens the fixed local pose directory.

Pose application changes joint state. It does not write keyframes.

### Return to rest

**Zero every joint (rest pose)** clears the skeleton's pose rotations. Use this
before bind-pose refresh, joint/weight mirror, anti-bleed, detach, or another
operation that requires rest.

## Detach / Reattach tab

Use this workflow when adding, removing, reparenting, or broadly reorienting
joints.

1. Return the rig to its bind/rest state.
2. Select the skinned mesh.
3. Click **Detach (snapshot + unbind)**.
4. Change the skeleton.
5. Select the snapshot from the newest-first list.
6. Leave **Restore original bind matrices** off when the joints intentionally
   moved.
7. Click **Reattach (rebind + restore weights)**.
8. Inspect missing-influence and orphaned-vertex warnings.

Detach first saves a JSON snapshot, then unbinds. Reattach creates a new
skinCluster at the joints' current positions and applies the saved weights.

Tick **Restore original bind matrices** only to reverse a detach where the
skeleton was not intentionally changed. Restoring the old matrices after moving
joints defeats the rebind.

The **More** section provides:

- **Snapshot only** for insurance without unbinding;
- **Apply to existing skin** for a skinCluster already rebuilt by hand;
- **Open folder** to inspect the selected scene's snapshots.

### Snapshot matching

A snapshot records the influence list, node identifiers, bind pre-matrices,
sparse vertex weights, blend weights, and important skinCluster settings.
Influences are resolved by stable node identifier, full path, unique leaf name,
then namespace-tolerant leaf name.

Renamed or reparented influences may be remapped and logged. Missing influences
are dropped and survivors are renormalized. A vertex weighted only to missing
influences can reach zero total weight; SkinBench selects those vertices for
manual repainting.

Snapshot restore refuses when the vertex count differs. It also checks the
stored mesh identifier so two same-sized meshes are not silently confused.

## Bind Photo tab

Use **Bind Photo** after Maya's **Add Influence** when painting the new joint
immediately drags vertices.

1. Pick the skinCluster.
2. Pick the newly added joint.
3. Pick a working reference influence on the same skinCluster.
4. Click **Sync bind photo**.
5. Paint and test the new influence.

The reference influence supplies a reliable bind-space anchor, so this repair
can work even when the current frame is not a clean bind pose.

When using Maya's Add Influence options, keep **Use geometry** off for a joint
influence and keep the new influence unlocked for painting. Maya's **Remove
Unused Influences** can remove every zero-weight influence, so refresh and
inspect the list after using it.

## ROM sweep

ROM means range of motion. It is a command workflow, not a panel tab.

Select joints and run `skinBenchROM`. Each call advances one joint through
positive and negative rotation on its three local axes. The default is six
poses per joint at 45 degrees. Use `skinBenchROM -r` to end the sweep and
restore the exact rotations present when it began.

The sweep writes no keyframes. Locked or driven joints are skipped and logged.
It moves one joint at a time, so it can reveal local weight breaks without a
rig-specific angle table. It cannot reveal a problem that requires several
joints to bend together, and it does not judge whether a deformation looks
good.

## Curl chain

Curl bends the selected joint and its descendants together, 15 degrees per
call by default. Choose X, Y, or Z. Repeated calls accumulate; **Straighten**
restores the rotations present when curl began.

Enabled rotation limits are respected. A skeleton with no enabled limits can
continue bending as long as commands are issued. Roots, locked joints, and
driven joints are skipped. Curl writes no keyframes.

ROM and curl answer different questions: ROM isolates one joint; curl tests a
combined chain shape.

## Commands

These commands can be used in shelf buttons, hotkeys, or Maya scripts:

```mel
skinBench;                    // open the window
skinBenchMoveJoints -on;      // mjm ON for reachable skinClusters
skinBenchMoveJoints -off;     // mjm OFF and refresh bind poses
skinBenchMoveJoints -all -on; // every skinCluster in the scene
skinBenchBakeOrient;          // move rotate into jointOrient
skinBenchBakeOrient -hi;      // include the selected hierarchy

skinBenchDetach;              // snapshot + unbind selected meshes
skinBenchReattach;            // rebind from the newest snapshot
skinBenchReattach -rbm;       // also restore original bind matrices

skinBenchROM;                 // next ROM pose
skinBenchROM -hi;             // include joints under the selection
skinBenchROM -a 60;           // use 60 degrees
skinBenchROM -p;              // previous pose
skinBenchROM -r;              // end, restore, and verify

skinBenchCurl -ax X;          // bend the chain +15 degrees on X
skinBenchCurl -d -15;         // uncurl 15 on the last axis
skinBenchCurl -d 30 -ax Z;    // any angle and axis
skinBenchCurl -s;             // straighten, restore, and verify

skinBenchBleed;               // preview selected joints' over-reach
skinBenchBleed -n 4;          // use a tighter radius
skinBenchBleed -s;            // strip after snapshot

skinBenchIsolate;             // select the driven region
skinBenchIsolate -hi -iso;    // include hierarchy and isolate the view
skinBenchIsolate -off;        // show all
```

With no `-on` or `-off`, `skinBenchMoveJoints` toggles the first reachable
skinCluster. With no joint selection, `skinBenchBleed` audits every influence
on the reachable skinCluster.

Python can call the core directly:

```python
from skinbench import core

core.detach("body_mesh")
# Change the skeleton.
core.reattach("body_mesh", core.newest_snapshot_for("body_mesh"))
```

## Snapshots and recovery

Snapshots for a saved scene are stored under
`<scene folder>/skinbench/<scene name>/`. Unsaved scenes use a SkinBench folder
under Maya's per-user application directory. Snapshots are not auto-deleted.

Every detach creates a snapshot. Guarded weight-write controls also create one
before writing. If an operation is wrong:

1. Stop writing further changes.
2. Open **Detach / Reattach**.
3. Select the snapshot created immediately before the bad write.
4. Use **Apply to existing skin** when the current skinCluster still exists.
5. Use **Reattach** when the mesh is detached or must be rebound.
6. Inspect missing influences and selected zero-weight vertices.
7. Save to a new file, reopen, and verify.

If a guarded action was run more than once, the older snapshot may be the last
pristine copy. Keep all candidates until recovery is proven.

## Logs and local files

Every panel log line is also appended to a daily text file under Maya's
per-user `skinbench/logs/` folder by default. The line includes a time and the
current scene filename. `SKINBENCH_LOG_DIR` can override the log directory.

The log pane is closed by default. Refusals open and highlight it. Clearing the
pane does not clear the daily file. **Open log folder** opens the fixed local
folder using the operating system's folder opener.

Named poses are JSON files. UI appearance and anti-bleed dial preferences are
stored in `skinbench/settings.ini`. SkinBench does not write preferences to the
Windows registry.

## Known limits

- Polygon meshes only. NURBS and subdivision surfaces are not supported.
- Topology must match a snapshot. A changed vertex count is refused.
- API weight writes are not restored by Maya Ctrl+Z. Recover from snapshots.
- Reattach may receive a Maya-numbered skinCluster name when the old name is
  already occupied.
- ROM moves one joint at a time and cannot reveal every combined-pose defect.
- ROM, curl, preview, and solve tools present evidence; an artist still judges
  the deformation.
- Anti-bleed distance uses a straight line, not a route across the surface.
- Home patch depends on connected mesh edges and the chosen stickiness value.
- Drag solve handles selected vertices in one pose and does not smooth its
  answer into neighbouring vertices.
- Cross-character world-space pose application is approximate.
- Headless UI rendering is not part of the mayapy smoke test. The real panel
  still requires an interactive Maya check.
- Compatibility is limited to the claims in [compatibility.md](compatibility.md).
