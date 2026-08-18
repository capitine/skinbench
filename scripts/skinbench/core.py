# -*- coding: utf-8 -*-
"""SkinBench core — skin weight snapshots, detach/reattach, joint-move helpers.

Pure logic, no UI. Every function here is safe to call from the Script Editor,
a shelf button, or the SkinBench window.

Design notes
------------
* Weights are stored SPARSE (only non-zero entries) as JSON, so a 100k-vert
  mesh with 30 influences is a few MB instead of a few hundred.
* Influences are matched back by full DAG path first, then by leaf name. A
  rename between detach and reattach is therefore survivable, and reported.
* Reattach rebinds at the joints' CURRENT positions by default -- that is the
  whole point of the workflow (move the joint, keep the weights). Pass
  ``restore_bind_matrices=True`` for an exact "put it back how it was" restore.
* ``skinCluster -e -mjm 1`` (Move Skinned Joints) is the lighter tool and does
  not need a detach at all. See ``set_move_joints_mode``, and
  ``mirror_joint`` which drives it for you. Reach for detach/reattach when the
  SKELETON changes shape: reparenting, adding or removing joints, or a
  freeze pass.
"""

import datetime
import io
import json
import math
import os
import re
import sys

import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma

SNAPSHOT_VERSION = 1
WEIGHT_EPSILON = 1e-7

# Attributes copied verbatim from the old skinCluster onto the new one.
_SETTINGS_ATTRS = (
    "skinningMethod",
    "useComponents",
    "deformUserNormals",
    "normalizeWeights",
    "weightDistribution",
    "maxInfluences",
    "maintainMaxInfluences",
    "envelope",
)


class SkinBenchError(RuntimeError):
    """Anything SkinBench refuses to do, with a reason a human can act on."""


# ---------------------------------------------------------------------------
# session log -- every line the window prints, also written to a dated file
# ---------------------------------------------------------------------------
#
# The window's log pane is wiped the moment Maya closes, so a dated disk log is
# the durable record. Keep the default inside Maya's own user folder so an
# extracted SkinBench package works on any machine. Studios can still redirect
# it with ``SKINBENCH_LOG_DIR``.


def _default_log_dir():
    """Portable per-user log folder under Maya's application directory."""
    return os.path.join(cmds.internalVar(userAppDir=True), "skinbench", "logs")


LOG_DIR = os.path.normpath(
    os.environ.get("SKINBENCH_LOG_DIR") or _default_log_dir()
)

# Paths this python session has already written a header into. Reset by
# ``reload_all``, which is exactly right -- a reload deserves a fresh banner.
_LOG_STARTED = set()


def log_dir():
    """The folder dated log files land in. Created on first use."""
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR)
    return LOG_DIR


def log_path(when=None):
    """Today's log file: ``<log_dir>/YYYY-MM-DD.log``."""
    when = when or datetime.datetime.now()
    return os.path.join(log_dir(), when.strftime("%Y-%m-%d") + ".log")


def _scene_name():
    """Just the scene's filename, so a log line says WHICH rig it happened on."""
    try:
        scene = cmds.file(query=True, sceneName=True)
    except Exception:
        scene = None
    return os.path.basename(scene) if scene else "unsaved-scene"


def format_log_line(message, scene, when):
    """One log line: time, scene file, message. Pure -- testable without Maya.

    A multi-line message (a traceback) gets the prefix on EVERY line, so the
    file stays greppable line by line.
    """
    stamp = when.strftime("%H:%M:%S")
    lines = str(message).splitlines() or [""]
    return "\n".join("%s  [%s]  %s" % (stamp, scene, line) for line in lines)


def write_log(message):
    """Append ``message`` to today's log file. Returns the path, or None.

    **Never raises.** Logging is a convenience, not a feature: a full disk, a
    locked file or a missing drive must not take the tool down in the middle of
    a weight edit. Every failure is swallowed on purpose, and the window's own
    log pane carries on regardless.
    """
    try:
        when = datetime.datetime.now()
        path = log_path(when)
        banner = None
        if path not in _LOG_STARTED:
            banner = u"=== SkinBench session %s ===" % when.strftime("%Y-%m-%d %H:%M:%S")
        text = format_log_line(message, _scene_name(), when)
        with io.open(path, "a", encoding="utf-8") as handle:
            if banner:
                handle.write(banner + u"\n")
            handle.write(text + u"\n")
        _LOG_STARTED.add(path)
        return path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# small maya/api helpers
# ---------------------------------------------------------------------------


def _mobject(name):
    sel = om.MSelectionList()
    sel.add(name)
    return sel.getDependNode(0)


def _dag_path(name):
    sel = om.MSelectionList()
    sel.add(name)
    return sel.getDagPath(0)


def deformed_shape(mesh):
    """Return the full path of the non-intermediate mesh shape under ``mesh``."""
    if cmds.objectType(mesh, isAType="shape"):
        return cmds.ls(mesh, long=True)[0]
    shapes = cmds.listRelatives(mesh, shapes=True, noIntermediate=True, fullPath=True)
    if not shapes:
        raise SkinBenchError("'%s' has no visible shape node." % mesh)
    meshes = cmds.ls(shapes, type="mesh", long=True)
    if not meshes:
        raise SkinBenchError(
            "'%s' is not a polygon mesh. SkinBench handles polygon meshes only." % mesh
        )
    return meshes[0]


def skin_cluster_for(node):
    """Return the skinCluster deforming ``node``, or None."""
    try:
        shape = deformed_shape(node)
    except SkinBenchError:
        return None
    history = cmds.listHistory(shape, pruneDagObjects=True, interestLevel=2) or []
    clusters = cmds.ls(history, type="skinCluster")
    return clusters[0] if clusters else None


def skin_clusters_from_selection(selection=None):
    """Every skinCluster reachable from the selection.

    Accepts meshes (walks their history) *and* joints (walks the
    ``worldMatrix`` connections), so you can select the joint you are about to
    move and still get the right skinClusters.
    """
    nodes = selection if selection is not None else (cmds.ls(selection=True, long=True) or [])
    found = []
    for node in nodes:
        if cmds.nodeType(node) == "skinCluster":
            found.append(node)
            continue
        cluster = skin_cluster_for(node)
        if cluster:
            found.append(cluster)
        if cmds.objectType(node, isAType="joint") or cmds.objectType(node, isAType="transform"):
            plugs = cmds.listConnections(
                node + ".worldMatrix[0]", type="skinCluster", source=False, destination=True
            ) or []
            found.extend(plugs)
    ordered = []
    for cluster in found:
        if cluster not in ordered:
            ordered.append(cluster)
    return ordered


def meshes_for_skin_cluster(cluster):
    """Full paths of the mesh shapes driven by ``cluster``."""
    shapes = cmds.skinCluster(cluster, query=True, geometry=True) or []
    return cmds.ls(shapes, long=True)


def _skin_fn(cluster):
    return oma.MFnSkinCluster(_mobject(cluster))


def _complete_vertex_component(count):
    comp_fn = om.MFnSingleIndexedComponent()
    component = comp_fn.create(om.MFn.kMeshVertComponent)
    comp_fn.setCompleteData(count)
    return component


def _influence_table(fn):
    """``(full paths, LOGICAL .matrix[] indices)`` in physical influence order.

    The two lists are indexed the same way -- position ``i`` in both is the i'th
    influence as ``influenceObjects()`` orders them -- but they hold different
    NUMBERS, and mixing them up is the bug that crashed Strip on a production rig.

    * position ``i`` (0..n-1) is the **physical** index. It is what
      ``getWeights`` lays its values out by, and what ``setWeights`` wants.
      Use ``_set_weights``.
    * ``logical[i]`` is the sparse plug index on ``.matrix[]`` /
      ``.bindPreMatrix[]``. Use it ONLY to address those attributes.

    On a cluster that has never had an influence removed the two are identical,
    which is exactly why the confusion survived until a rig with history and
    69 influences ran the write path for the first time.
    """
    paths = fn.influenceObjects()
    names = [path.fullPathName() for path in paths]
    logical = [fn.indexForInfluenceObject(path) for path in paths]
    return names, logical


def _physical_columns(count):
    """``[0, 1, ... count-1]`` -- the influence indices ``setWeights`` wants.

    Trivial on purpose: split out so the index-space rule can be asserted
    without a scene.
    """
    return list(range(count))


def _set_weights(fn, dag, component, values, vertex_count):
    """The ONE way SkinBench writes skin weights. Read this before adding a fifth caller.

    Two contracts, both of which Maya lets you break quietly:

    1. **Influence indices are PHYSICAL** -- ``0..n-1`` in ``influenceObjects()``
       order, the same layout ``getWeights`` hands back. NOT the logical
       ``.matrix[]`` indices from ``indexForInfluenceObject``. Those two agree
       only on a cluster with no influence add/remove history; on one with gaps
       the logical indices run past the end and Maya answers
       ``(kInvalidParameter): Object is incompatible with this method``.
       (During live testing, logical indices ran to 74 across 69 influences.
       The READ side worked all along, which is why the report printed fine
       and only the write blew up.)

    2. **Length is exact**: ``len(values) == vertex_count * influences``, and
       ``values`` must be an ``MDoubleArray``. Maya does **not** check this --
       a short array was accepted silently in testing, which writes garbage
       weights and reports success. So it is checked here, where it cannot be
       forgotten.
    """
    columns = len(fn.influenceObjects())
    expected = vertex_count * columns
    if len(values) != expected:
        raise SkinBenchError(
            "refusing to write weights: got %d values for %d vertices x %d "
            "influences (expected %d). Maya would accept this and write "
            "garbage." % (len(values), vertex_count, columns, expected)
        )
    if not isinstance(values, om.MDoubleArray):
        values = om.MDoubleArray(values)
    fn.setWeights(dag, component, om.MIntArray(_physical_columns(columns)),
                  values, False)
    return expected


# ---------------------------------------------------------------------------
# move skinned joints mode
# ---------------------------------------------------------------------------


def move_joints_mode(cluster):
    """Current state of ``skinCluster -q -mjm``."""
    return bool(cmds.skinCluster(cluster, query=True, moveJointsMode=True))


def set_move_joints_mode(clusters, enabled, freshen=True):
    """Toggle Move Skinned Joints mode on each skinCluster.

    While ON, moving a joint leaves the mesh frozen and re-photographs the bind
    pose on the way out. This is the no-detach path for repositioning a joint
    that is already painted.

    Turning it OFF is the "lock it in" moment, so that is where the bind-pose
    sweep runs. Pass ``freshen=False`` when the caller is mid-operation and the
    rig is not back at rest yet -- ``mirror_joints`` does exactly that, and runs
    the sweep itself once rotate has been folded into jointOrient.

    Returns ``{"clusters": [...], "bindPoses": <freshen report or None>}``.
    """
    touched = []
    for cluster in clusters:
        cmds.skinCluster(cluster, edit=True, moveJointsMode=bool(enabled))
        touched.append(cluster)
    poses = None
    if touched and not enabled and freshen:
        poses = freshen_bind_poses_quietly()
    return {"clusters": touched, "bindPoses": poses}


def all_skin_clusters():
    return cmds.ls(type="skinCluster") or []


# ---------------------------------------------------------------------------
# rest-pose guard -- shared by everything that must not run on a posed rig
# ---------------------------------------------------------------------------

# On these rigs a pose lives in ``.rotate``: rest is zeroed by convention and
# the FBX gates enforce it. So ``.rotate`` is what gets checked, and a baked
# offset in jointOrient is correctly ignored -- that is not a pose.
REST_TOLERANCE = 1e-4


def posed_joints(joints, tolerance=REST_TOLERANCE):
    """Leaf names of the joints carrying non-zero rotate. Empty = rig at rest."""
    posed = []
    for joint in joints:
        try:
            rotate = cmds.getAttr(joint + ".rotate")[0]
        except (ValueError, RuntimeError):
            continue
        if any(abs(value) > tolerance for value in rotate):
            posed.append(joint.split("|")[-1])
    return posed


def describe_posed(posed):
    """``"Head, Neck carry non-zero rotate"`` -- one phrase, several messages."""
    return "%s%s carr%s non-zero rotate" % (
        ", ".join(posed[:4]),
        " and %d more" % (len(posed) - 4) if len(posed) > 4 else "",
        "ies" if len(posed) == 1 else "y",
    )


# ---------------------------------------------------------------------------
# bind pose (dagPose) auto-heal -- move a joint, keep the photo honest
# ---------------------------------------------------------------------------

# How far a stored bind-pose matrix may drift from the joint's real world matrix
# before it counts as stale. Matrices are exact when nothing moved, so anything
# above float noise is a genuine edit.
BIND_POSE_TOLERANCE = 1e-4


def _pose_members(pose):
    """``{slot: joint full path}`` for one dagPose node.

    Read off the ``.members`` message connections rather than
    ``dagPose -q -members``, because the SLOT is what indexes the stored
    matrices and the query only returns names.
    """
    pairs = cmds.listConnections(
        pose + ".members", plugs=True, connections=True,
        source=True, destination=False,
    ) or []
    members = {}
    for index in range(0, len(pairs), 2):
        try:
            slot = int(pairs[index].rsplit("[", 1)[1].rstrip("]"))
        except (IndexError, ValueError):
            continue
        found = cmds.ls(pairs[index + 1].rsplit(".", 1)[0], long=True) or []
        if found:
            members[slot] = found[0]
    return members


def _bind_pose_drift(stored, current):
    """Biggest element-wise disagreement between two 16-float matrices."""
    return max([abs(a - b) for a, b in zip(stored, current)] or [0.0])


def needs_refresh(stored, current, tolerance=BIND_POSE_TOLERANCE):
    """Is this stored bind-pose matrix stale? Pure -- testable without a scene."""
    return _bind_pose_drift(stored, current) > tolerance


def freshen_bind_poses(poses=None, tolerance=BIND_POSE_TOLERANCE):
    """Re-photograph every bind-pose entry that no longer matches its joint.

    Moving a joint can leave the stored ``dagPose`` holding the joint's OLD
    world matrix. Nothing in Maya complains: the rig looks right, the skin
    deforms right, and then MotionBuilder (or any retarget that trusts the bind
    pose) puts the character together out of stale numbers. That is the
    recurring blocker this heals.

    Only runs on a rig at REST. Re-photographing a posed rig would bake the pose
    in as the new rest pose, which is a far worse issue than the one being
    fixed -- so a posed rig is refused, loudly, rather than "helped".

    Idempotent: a joint whose stored matrix already agrees is not touched, so
    running it twice resets nothing the second time.
    """
    poses = list(poses) if poses else (cmds.ls(type="dagPose") or [])
    report = {"poses": [], "reset": 0, "checked": 0, "stale": [], "skipped": None}
    members_by_pose = {pose: _pose_members(pose) for pose in poses}
    everyone = sorted({joint for members in members_by_pose.values()
                       for joint in members.values()})
    if not everyone:
        return report

    posed = posed_joints(everyone)
    if posed:
        raise SkinBenchError(
            "The rig is POSED -- %s. Re-photographing the bind pose now would "
            "BAKE THE POSE IN as the new rest pose. Zero the pose (Poses tab), "
            "end the ROM sweep (skinBenchROM -r), or Ctrl+Z back to rest first."
            % describe_posed(posed)
        )

    cmds.undoInfo(openChunk=True, chunkName="SkinBench freshen bind poses")
    try:
        for pose in poses:
            members = members_by_pose[pose]
            refreshed = []
            for slot, joint in sorted(members.items()):
                report["checked"] += 1
                try:
                    stored = cmds.getAttr("%s.worldMatrix[%d]" % (pose, slot))
                    current = cmds.getAttr(joint + ".worldMatrix[0]")
                except (ValueError, RuntimeError):
                    continue
                if not needs_refresh(stored, current, tolerance):
                    continue
                cmds.dagPose(joint, name=pose, reset=True)
                leaf = joint.split("|")[-1]
                refreshed.append(leaf)
                report["stale"].append((pose, leaf, _bind_pose_drift(stored, current)))
            report["reset"] += len(refreshed)
            if members:
                report["poses"].append(
                    {"pose": pose, "members": len(members), "refreshed": refreshed}
                )
    finally:
        cmds.undoInfo(closeChunk=True)
    return report


def freshen_bind_poses_quietly():
    """``freshen_bind_poses`` for the automatic moments. Never raises.

    A posed rig must not turn "Lock in" into an error -- the lock-in still
    worked. The reason lands in ``report["skipped"]`` for the caller to print.
    """
    try:
        return freshen_bind_poses()
    except SkinBenchError as error:
        return {"poses": [], "reset": 0, "checked": 0, "stale": [],
                "skipped": str(error)}


def bind_pose_selftest():
    """The staleness rule, with no scene. Script Editor or mayapy."""
    rest = [1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 5.0, 0.0, 1.0]

    # The no-op case: identical matrices must NOT be reset. This is what makes
    # the sweep safe to fire automatically after every lock-in.
    assert not needs_refresh(rest, list(rest))
    assert _bind_pose_drift(rest, list(rest)) == 0.0

    moved = list(rest)
    moved[12] = 2.0                                   # joint dragged 2 units in x
    assert needs_refresh(rest, moved)
    assert _bind_pose_drift(rest, moved) == 2.0

    # Float noise is not an edit; a real nudge is.
    noise = list(rest)
    noise[13] += 1e-9
    assert not needs_refresh(noise, rest)
    nudge = list(rest)
    nudge[13] += 1e-3
    assert needs_refresh(nudge, rest)

    # The tolerance is a boundary, not a suggestion.
    edge = list(rest)
    edge[13] += BIND_POSE_TOLERANCE
    assert not needs_refresh(edge, rest), "exactly at tolerance is not stale"

    assert posed_joints([]) == []
    assert describe_posed(["Head"]) == "Head carries non-zero rotate"
    assert describe_posed(["Head", "Neck"]) == "Head, Neck carry non-zero rotate"
    assert "and 1 more" in describe_posed(["a", "b", "c", "d", "e"])
    print("SkinBench bind pose selftest OK")
    return True


# ---------------------------------------------------------------------------
# Maya tool settings SkinBench owns, so nobody has to dig through Tool Settings
# ---------------------------------------------------------------------------

# Maya's three default manipulator contexts. Same flag on all three, and it is
# stored per-tool -- which is exactly why it keeps getting forgotten.
_MANIP_CONTEXTS = (
    ("Move", "manipMoveContext"),
    ("Rotate", "manipRotateContext"),
    ("Scale", "manipScaleContext"),
)


def preserve_child_position(enabled=None):
    """Query (``None``) or set Maya's "Preserve Children" on all three tools.

    ON means rotating a joint changes THAT joint's axis and leaves its child
    chain where it is in world space. Maya pays for that with counter-values in
    the children's rotate channels, so finish with
    ``bake_rotation_to_orient(include_hierarchy=True)`` or the rig hands a
    retarget a skeleton that reads as already posed.

    Returns True only when every available tool has it on.
    """
    states = []
    for name, command in _MANIP_CONTEXTS:
        if not cmds.contextInfo(name, exists=True):
            continue
        context = getattr(cmds, command)
        if enabled is not None:
            context(name, edit=True, preserveChildPosition=bool(enabled))
        states.append(bool(context(name, query=True, preserveChildPosition=True)))
    if not states:
        raise SkinBenchError(
            "Maya's Move/Rotate/Scale tool contexts are not available in this session."
        )
    return all(states)


# ---------------------------------------------------------------------------
# bake rotate -> jointOrient
# ---------------------------------------------------------------------------

# Maya rotateOrder enum order matches MEulerRotation's constants exactly.
_ROTATE_ORDERS = (
    om.MEulerRotation.kXYZ,
    om.MEulerRotation.kYZX,
    om.MEulerRotation.kZXY,
    om.MEulerRotation.kXZY,
    om.MEulerRotation.kYXZ,
    om.MEulerRotation.kZYX,
)


def _euler(degrees, order=om.MEulerRotation.kXYZ):
    return om.MEulerRotation([math.radians(v) for v in degrees], order)


def _fold_rotate_into_orient(joint):
    """rotate * jointOrient -> jointOrient, rotate 0. No undo chunk of its own."""
    order = _ROTATE_ORDERS[cmds.getAttr(joint + ".rotateOrder")]
    combined = (
        _euler(cmds.getAttr(joint + ".rotate")[0], order).asMatrix()
        * _euler(cmds.getAttr(joint + ".jointOrient")[0]).asMatrix()
    )
    result = om.MTransformationMatrix(combined).rotation(asQuaternion=False)
    result.reorderIt(om.MEulerRotation.kXYZ)
    cmds.setAttr(
        joint + ".jointOrient",
        math.degrees(result.x),
        math.degrees(result.y),
        math.degrees(result.z),
    )
    cmds.setAttr(joint + ".rotate", 0.0, 0.0, 0.0)


def bake_rotation_to_orient(joints=None, include_hierarchy=False):
    """Fold each joint's rotate channels into jointOrient, leaving rotate at 0.

    The joint's world matrix does not change. A joint's local matrix multiplies
    ``rotateAxis * rotate * jointOrient``, so moving the rotation from the
    middle factor into the last one leaves the product identical -- children
    stay put, bindPreMatrices stay valid, weights are untouched.

    Why you need it: a Move Skinned Joints pass re-photographs the bind pose to
    match the joint's new transform, but that rotation is still sitting in
    ``rotate``. Zeroing that channel by hand therefore throws the reorientation
    away and the joint snaps back to its old axis. Baking makes zero mean zero,
    which is also what a clean rig wants before export.

    Returns ``(baked, skipped)`` where skipped entries are ``(joint, reason)``.
    """
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if include_hierarchy and joints:
        children = cmds.listRelatives(
            joints, allDescendents=True, type="joint", fullPath=True
        ) or []
        joints.extend(child for child in children if child not in joints)
    if not joints:
        raise SkinBenchError("Select the joint (or joints) whose rotate should be zeroed.")

    baked, skipped = [], []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench bake rotate to orient")
    try:
        for joint in joints:
            if not cmds.objExists(joint + ".jointOrient"):
                skipped.append((joint, "not a joint"))
                continue

            locked = [
                attr
                for attr in ("rotateX", "rotateY", "rotateZ",
                             "jointOrientX", "jointOrientY", "jointOrientZ")
                if cmds.getAttr("%s.%s" % (joint, attr), lock=True)
            ]
            if locked:
                skipped.append((joint, "locked: " + ", ".join(locked)))
                continue

            driver = cmds.listConnections(
                joint + ".rotate", source=True, destination=False, plugs=True
            )
            if driver:
                skipped.append((joint, "rotate is driven by %s" % driver[0]))
                continue

            rotate = cmds.getAttr(joint + ".rotate")[0]
            if all(abs(value) < 1e-9 for value in rotate):
                continue  # already zero, nothing to fold

            _fold_rotate_into_orient(joint)
            baked.append(joint)
    finally:
        cmds.undoInfo(closeChunk=True)

    return baked, skipped


# ---------------------------------------------------------------------------
# mirror a joint onto its opposite number
# ---------------------------------------------------------------------------

# Reflection across the world YZ plane (x = 0). Every character SkinBench has
# run on is built symmetric about world x.
# ponytail: hard-coded plane. Take a matrix argument if a rig ever mirrors
# about something else -- the maths below already works for any reflection.
_MIRROR_X = om.MMatrix([-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])

# Tried in order, both directions. First swap that names exactly one existing
# joint wins, so a bad guess resolves to nothing rather than to the wrong bone.
_SIDE_PAIRS = (("_L", "_R"), ("Left", "Right"), ("_l_", "_r_"), ("left", "right"))


def opposite_joint(joint):
    """The other side's joint, found by name swap, or None."""
    full = cmds.ls(joint, long=True)
    if not full:
        return None
    leaf = full[0].split("|")[-1].split(":")[-1]
    for left, right in _SIDE_PAIRS:
        for old, new in ((left, right), (right, left)):
            if old not in leaf:
                continue
            found = cmds.ls(leaf.replace(old, new), type="joint", long=True) or []
            if len(found) == 1 and found[0] != full[0]:
                return found[0]
    return None


def _depth(joint):
    return joint.count("|")


def mirror_joints(sources, mirror_position=True):
    """Give each source joint's opposite number the mirror of its world transform.

    The world matrix is reflected across x = 0 and the rotation conjugated by
    the same reflection (``M' = S M S``), so the target's local axes come out
    as the geometric mirror of the source's and its rotate channels behave
    symmetrically. Working in WORLD space is the point: the two sides' parents
    are often not mirrors of each other, and then copying local jointOrient
    with sign flips is simply wrong.

    Runs inside Move Skinned Joints so every skinCluster the targets drive
    re-photographs its bind pose and the mesh does not follow, then folds the
    new angle into jointOrient so the rest pose stays neutral.

    A joint carries its children. Mirroring a wrist alone therefore SWINGS the
    fingers instead of mirroring them -- pass the whole chain, and this walks
    it parent-first so each joint's own world matrix is the last word.

    Returns one report dict for the batch. ``mesh_moved`` is measured per
    vertex, before against after -- anything but ~0 means undo.
    """
    sources = [j for j in (cmds.ls(sources, long=True, type="joint") or [])]
    if not sources:
        raise SkinBenchError("Select the SOURCE joint — the side that is already correct.")

    pairs, unpaired = [], []
    for source in sorted(set(sources), key=_depth):
        target = opposite_joint(source)
        if target:
            pairs.append((source, target))
        else:
            unpaired.append(source)
    if not pairs:
        raise SkinBenchError(
            "No opposite joint found for %s. Expected a name differing by one "
            "of %s." % (", ".join(j.split("|")[-1] for j in unpaired),
                        ", ".join("/".join(pair) for pair in _SIDE_PAIRS))
        )

    source_set = {s for s, _ in pairs}
    clash = [t for _, t in pairs if t in source_set]
    if clash:
        raise SkinBenchError(
            "Both sides are selected (%s). Select only the side you want to "
            "COPY FROM." % ", ".join(j.split("|")[-1] for j in clash)
        )

    # Read every source BEFORE touching anything: mirroring a parent moves its
    # children, and a source read late would be read post-move.
    plan = []
    for source, target in pairs:
        matrix = _MIRROR_X * om.MMatrix(
            cmds.xform(source, query=True, worldSpace=True, matrix=True)
        ) * _MIRROR_X
        if not mirror_position:
            keep = cmds.xform(target, query=True, worldSpace=True, translation=True)
            matrix = om.MMatrix(list(matrix)[:12] + keep + [1.0])
        plan.append((source, target, matrix))

    clusters = sorted({c for _, target, _ in plan
                       for c in (cmds.listConnections(target, type="skinCluster") or [])})
    meshes = sorted({m for c in clusters for m in meshes_for_skin_cluster(c)})
    before = {m: cmds.xform(m + ".vtx[*]", query=True, worldSpace=True, translation=True)
              for m in meshes}

    cmds.undoInfo(openChunk=True, chunkName="SkinBench mirror joints")
    move_joints_enabled = False
    operation_error = None
    mode_cleanup_error = None
    close_error = None
    try:
        move_joints_enabled = bool(clusters)
        set_move_joints_mode(clusters, True)
        for _, target, matrix in plan:          # parent-first, see _depth sort
            cmds.xform(target, worldSpace=True, matrix=list(matrix))
        # freshen=False: rotate is still carrying the mirror at this point, so
        # the sweep would (rightly) refuse. It runs below, after the fold.
        set_move_joints_mode(clusters, False, freshen=False)
        move_joints_enabled = False
        for _, target, _ in plan:
            _fold_rotate_into_orient(target)
            # mjm does not touch the stored bind/default poses; without this,
            # going to bind pose later snaps the joint back to its old axis.
            for pose in set(cmds.listConnections(target + ".message", type="dagPose") or []):
                cmds.dagPose(target, reset=True, name=pose)
        # The targeted resets above cover the joints that were mirrored. This
        # catches anything else the move dragged out of step -- children that
        # rode along, joints shared with another cluster.
        bind_poses = freshen_bind_poses_quietly()
    except BaseException:
        operation_error = sys.exc_info()

    if move_joints_enabled:
        try:
            set_move_joints_mode(clusters, False, freshen=False)
        except BaseException:
            mode_cleanup_error = sys.exc_info()

    try:
        cmds.undoInfo(closeChunk=True)
    except BaseException:
        close_error = sys.exc_info()

    if operation_error:
        raise operation_error[1].with_traceback(operation_error[2])
    if mode_cleanup_error:
        raise mode_cleanup_error[1].with_traceback(mode_cleanup_error[2])
    if close_error:
        raise close_error[1].with_traceback(close_error[2])

    moved = 0.0
    for mesh in meshes:
        after = cmds.xform(mesh + ".vtx[*]", query=True, worldSpace=True, translation=True)
        moved = max([moved] + [abs(a - b) for a, b in zip(before[mesh], after)])

    orient_error = 0.0
    dragged = []
    done = {t for _, t, _ in plan}
    for _, target, matrix in plan:
        got = om.MMatrix(cmds.xform(target, query=True, worldSpace=True, matrix=True))
        orient_error = max([orient_error] + [abs(got[i] - matrix[i]) for i in range(16)])
        # a child that was carried along but never mirrored in its own right
        for child in cmds.listRelatives(target, type="joint", fullPath=True) or []:
            if child not in done:
                dragged.append(child)

    return {
        "pairs": [(s, t) for s, t, _ in plan],
        "unpaired": unpaired,
        "clusters": clusters,
        "meshes": meshes,
        "orient_error": orient_error,
        "mesh_moved": moved,
        "dragged": dragged,
        "bindPoses": bind_poses,
    }


def mirror_joint(source, mirror_position=True):
    """One joint, no hierarchy. Kept for the Script Editor."""
    return mirror_joints([source], mirror_position=mirror_position)


def mirror_selected_joints(joints=None, mirror_position=True, include_hierarchy=False):
    """Mirror the selected joints -- optionally their whole chain -- onto the far side."""
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if include_hierarchy and joints:
        children = cmds.listRelatives(
            joints, allDescendents=True, type="joint", fullPath=True
        ) or []
        joints.extend(child for child in children if child not in joints)
    return mirror_joints(joints, mirror_position=mirror_position)


# ---------------------------------------------------------------------------
# mirror the WEIGHTS, and prove the two sides agree afterwards
# ---------------------------------------------------------------------------

# World cm, rounded to this many places, is how a vertex finds its twin across
# x = 0. 3 places is 0.001 cm on a 200 cm character -- tight enough that two
# different vertices never collide, loose enough to survive modelling noise.
_MATCH_PLACES = 3


def _weight_rows(cluster, shape):
    """``(rows, influence names)`` -- one weight row per vertex, physical order."""
    fn = _skin_fn(cluster)
    path = _dag_path(shape)
    count = om.MFnMesh(path).numVertices
    flat, stride = fn.getWeights(path, _complete_vertex_component(count))
    rows = [list(flat[i * stride:(i + 1) * stride]) for i in range(count)]
    return rows, _influence_table(fn)[0]


def _mirrored_columns(names):
    """For each influence column, the column holding its opposite number.

    A midline joint (no twin) maps to itself, which is what makes a spine
    vertex compare equal to its own reflection instead of failing.
    """
    index = {name: i for i, name in enumerate(names)}
    columns = []
    for i, name in enumerate(names):
        twin = opposite_joint(name)
        columns.append(index.get(twin, i))
    return columns


def weight_symmetry(cluster):
    """Worst weight difference between a vertex and its mirror twin.

    ``worst`` near 0 means the two sides genuinely carry the same weights --
    the thing "it ran without error" does not tell you. ``unmatched`` counts
    vertices with no partner across x = 0; that is the MESH being asymmetric
    there, not the weights, and it is why the number is reported rather than
    swallowed. ``checked == 0`` means CANNOT CHECK, not symmetric.

    Midline vertices are their own twin, so their "asymmetry" is one-sided
    paint (chest_joint_left != chest_joint_right on the same vertex). A mirror can
    never change them, so they are reported SEPARATELY -- folding them into
    ``worst`` made characterA's clean body mirror read as 0.433-broken.

    Returns ``(worst, unmatched, checked, midline_worst, midline)``.
    """
    _refuse_when_posed(
        [cluster],
        "Twins are found by where the vertices SIT, so a posed rig compares "
        "the wrong pairs and the number means nothing. Zero the pose (Poses "
        "tab) first.")

    worst, unmatched, checked, midline_worst, midline = 0.0, 0, 0, 0.0, 0
    for shape in meshes_for_skin_cluster(cluster):
        rows, names = _weight_rows(cluster, shape)
        columns = _mirrored_columns(names)
        points = om.MFnMesh(_dag_path(shape)).getPoints(om.MSpace.kWorld)
        lookup = {
            (round(p.x, _MATCH_PLACES), round(p.y, _MATCH_PLACES), round(p.z, _MATCH_PLACES)): i
            for i, p in enumerate(points)
        }
        for i, point in enumerate(points):
            if point.x <= 0:
                continue  # walk one side only; every pair is seen once
            key = (round(-point.x, _MATCH_PLACES),
                   round(point.y, _MATCH_PLACES),
                   round(point.z, _MATCH_PLACES))
            twin = lookup.get(key)
            if twin is None:
                unmatched += 1
                continue
            here, there = rows[i], rows[twin]
            delta = max(abs(here[k] - there[columns[k]]) for k in range(len(here)))
            if twin == i:
                midline += 1
                midline_worst = max(midline_worst, delta)
            else:
                checked += 1
                worst = max(worst, delta)
    return worst, unmatched, checked, midline_worst, midline


def _changed_vs_snapshot(cluster, shape, data):
    """``(changed verts, max delta)`` of the cluster's current weights vs a
    snapshot. Matched by influence NAME so it survives influence reorder."""
    rows, names = _weight_rows(cluster, shape)
    old = {}
    old_names = data["influences"]
    weights = data["weights"]
    for v, i, value in zip(weights["vertex"], weights["influence"], weights["value"]):
        old[(v, old_names[i])] = value
    changed, max_delta = 0, 0.0
    for v, row in enumerate(rows):
        delta = max(abs(row[k] - old.get((v, names[k]), 0.0))
                    for k in range(len(names)))
        if delta > 1e-4:
            changed += 1
            max_delta = max(max_delta, delta)
    return changed, max_delta


def mirror_skin_weights(clusters=None, from_positive_x=True,
                        surface_association="closestPoint"):
    """Maya's Mirror Skin Weights across YZ, then PROVE what it did.

    OVERWRITES the far side. Anything hand-painted over there is gone, so this
    is opt-in per cluster and never part of the joint mirror.

    ``copySkinWeights`` samples by closest point, so it rewrites the far side
    whether or not exact vertex twins exist -- on characterA's bracers it
    rewrote all 247 verts while the twin-check found 0 pairs. The proof of the
    WRITE is therefore a diff against the snapshot just taken (``changed`` /
    ``max_delta``); the twin-check numbers only say whether the two sides now
    AGREE, and only where exact twins exist.

    Influences are matched by ``closestJoint`` in mirrored space, which is
    correct only once the SKELETON is already mirrored -- do the joints first.
    """
    _refuse_when_posed(
        clusters if clusters else skin_clusters_from_selection(),
        "Maya samples the far side by closest point in world space, so a posed "
        "rig copies from the wrong place. Zero the pose (Poses tab) first.")

    if clusters is None:
        clusters = skin_clusters_from_selection()
    clusters = list(clusters)
    if not clusters:
        raise SkinBenchError("Select a skinned mesh (or one of its influences) first.")

    reports = []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench mirror skin weights")
    try:
        for cluster in clusters:
            # Same gate as Strip: this overwrites painted work, so it goes to
            # disk first. Ctrl+Z is not a backup.
            snapshots, snapshot_data = [], []
            for shape in meshes_for_skin_cluster(cluster):
                path, data = save_snapshot(shape)
                snapshots.append(path)
                snapshot_data.append((shape, data))
            cmds.copySkinWeights(
                sourceSkin=cluster,
                destinationSkin=cluster,
                mirrorMode="YZ",
                mirrorInverse=not from_positive_x,
                surfaceAssociation=surface_association,
                influenceAssociation=["closestJoint", "oneToOne"],
            )
            changed, max_delta = 0, 0.0
            for shape, data in snapshot_data:
                shape_changed, shape_delta = _changed_vs_snapshot(cluster, shape, data)
                changed += shape_changed
                max_delta = max(max_delta, shape_delta)
            worst, unmatched, checked, midline_worst, midline = weight_symmetry(cluster)
            reports.append({
                "cluster": cluster,
                "changed": changed,
                "max_delta": max_delta,
                "worst": worst,
                "unmatched": unmatched,
                "checked": checked,
                "midline_worst": midline_worst,
                "midline": midline,
                "snapshots": snapshots,
            })
    finally:
        cmds.undoInfo(closeChunk=True)
    return reports


def _refuse_when_posed(clusters, because):
    """Stop a position-based operation dead if the rig is not at rest."""
    influences = []
    for cluster in clusters:
        if cluster:
            influences.extend(
                cmds.skinCluster(cluster, query=True, influence=True) or [])
    posed = posed_joints(influences)
    if posed:
        raise SkinBenchError(
            "The rig is POSED -- %s. %s" % (describe_posed(posed), because))


def _twin_lookup(points):
    """Vertex index by rounded world position, for finding a vertex's reflection."""
    return {
        (round(p.x, _MATCH_PLACES), round(p.y, _MATCH_PLACES), round(p.z, _MATCH_PLACES)): i
        for i, p in enumerate(points)
    }


def _selected_vertices_by_shape():
    """``{mesh shape: [vertex ids]}`` from whatever polygon verts are selected."""
    grouped = {}
    for item in cmds.filterExpand(selectionMask=31, fullPath=True) or []:
        node, _, index = item.partition(".vtx[")
        grouped.setdefault(node, []).append(int(index.rstrip("]")))
    return grouped


def nearest_across_x(points, ids, budget):
    """For each id, the vertex NEAREST to its reflection. ``{id: (twin, gap)}``.

    Exact-twin matching needs a mesh that is symmetric to the decimal place,
    and plenty are not: characterB's body sits about 0.18 off x=0, so **zero** of
    its 3748 right-side vertices have an exact partner. This is the fallback --
    same question, answered by "closest one" instead of "identical one", with
    the distance reported so a bad pairing cannot hide.

    ``budget`` is how far a match may be before it is thrown away. Brute force
    on purpose: the selection is small, the mesh is one pass, and a spatial
    index here would be code to maintain for no measured gain.
    """
    found = {}
    for i in ids:
        target = (-points[i].x, points[i].y, points[i].z)
        best, best_gap = None, budget
        for j in range(len(points)):
            if j == i:
                continue
            gap = _distance((points[j].x, points[j].y, points[j].z), target)
            if gap < best_gap:
                best, best_gap = j, gap
        if best is not None:
            found[i] = (best, best_gap)
    return found


def _match_budget(points, share=0.75):
    """How far a nearest-match may be: a fraction of the model's own size.

    Relative, so a fingertip and a tail do not need separate numbers.
    """
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    zs = [p.z for p in points]
    size = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    return max(size, 1.0) * 0.02 * share


def mirror_weights_on_selection(from_positive_x=True, match="exact"):
    """Mirror weights for the SELECTED vertices only, twin by twin.

    The selection says WHERE, the direction says WHICH WAY. A selected vertex
    on the source side pushes its weights onto its reflection; a selected
    vertex on the far side is filled from its reflection. So it does not matter
    which side of the model you rubber-banded -- the region is the same region
    either way, and both intuitions land on the same result.

    Exact vertex twins, not Maya's closest-point sampling: on a symmetric mesh
    that is the tighter answer, and a vertex with no twin is reported instead of
    being quietly approximated.
    """
    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "Select some vertices first — the region you want mirrored. "
            "(Right-click the mesh > Vertex, or use Select region above.)"
        )

    # A twin is found by world position, so on a posed rig the two sides are
    # simply not in mirrored places -- one arm up and one arm down means almost
    # nothing pairs, and whatever DOES pair is the wrong vertex. The README has
    # said "needs the rest pose" since this was written; nothing enforced it
    # until live testing.
    _refuse_when_posed(
        {skin_cluster_for(deformed_shape(node)) for node in grouped},
        "Twins are found by where the vertices SIT, so the two sides have to be "
        "in mirrored places. Zero the pose (Poses tab), mirror, then re-apply "
        "the pose to look at it.")

    sign = 1.0 if from_positive_x else -1.0
    reports = []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench mirror weights on selection")
    try:
        for node, ids in sorted(grouped.items()):
            shape = deformed_shape(node)
            cluster = skin_cluster_for(shape)
            if not cluster:
                reports.append({"cluster": None, "mesh": node, "skipped": "not skinned"})
                continue

            fn = _skin_fn(cluster)
            path = _dag_path(shape)
            names, _ = _influence_table(fn)
            columns = _mirrored_columns(names)
            # an influence whose twin joint exists but is not in THIS cluster
            unpaired = sum(1 for i, mapped in enumerate(columns)
                           if mapped == i and opposite_joint(names[i]))
            rows, _ = _weight_rows(cluster, shape)
            points = om.MFnMesh(path).getPoints(om.MSpace.kWorld)
            lookup = _twin_lookup(points)
            near = ({} if match == "exact"
                    else nearest_across_x(points, ids, _match_budget(points)))

            pair_claims = {}
            unmatched, midline = 0, 0
            for i in ids:
                point = points[i]
                if abs(point.x) < 10.0 ** -_MATCH_PLACES:
                    midline += 1  # sits ON the mirror plane; its twin is itself
                    continue
                if match == "exact":
                    twin = lookup.get((round(-point.x, _MATCH_PLACES),
                                       round(point.y, _MATCH_PLACES),
                                       round(point.z, _MATCH_PLACES)))
                else:
                    pair = near.get(i)
                    twin = pair[0] if pair else None
                if twin is None:
                    unmatched += 1
                    continue
                # source side pushes out, far side pulls in -- same pair either way
                source, destination = (i, twin) if sign * point.x > 0 else (twin, i)
                gap = pair[1] if match != "exact" else 0.0
                claim = (source, destination)
                pair_claims[claim] = max(pair_claims.get(claim, 0.0), gap)

            source_counts = {}
            destination_counts = {}
            for source, destination in pair_claims:
                source_counts[source] = source_counts.get(source, 0) + 1
                destination_counts[destination] = destination_counts.get(destination, 0) + 1
            ambiguous = [
                claim for claim in pair_claims
                if source_counts[claim[0]] > 1 or destination_counts[claim[1]] > 1
            ]
            if ambiguous:
                reports.append({
                    "cluster": cluster,
                    "mesh": node,
                    "selected": len(ids),
                    "unmatched": unmatched,
                    "midline": midline,
                    "match": match,
                    "collisions": len(ambiguous),
                    "snapshot": None,
                    "skipped": (
                        "ambiguous vertex pairing: %d pair claims share a source or "
                        "destination; no weights changed" % len(ambiguous)
                    ),
                })
                continue

            writes, partners = {}, {}
            worst_gap = 0.0
            for (source, destination), gap in pair_claims.items():
                writes[destination] = [
                    rows[source][columns[k]] for k in range(len(names))
                ]
                partners[destination] = source
                worst_gap = max(worst_gap, gap)

            snapshot, snapshot_data = None, None
            if writes:
                snapshot, snapshot_data = save_snapshot(shape)   # same gate as Strip
                comp_fn = om.MFnSingleIndexedComponent()
                component = comp_fn.create(om.MFn.kMeshVertComponent)
                comp_fn.addElements(sorted(writes))
                values = om.MDoubleArray(
                    [value for vertex in sorted(writes) for value in writes[vertex]]
                )
                _set_weights(fn, path, component, values, len(writes))

            after, _ = _weight_rows(cluster, shape)
            checked = 0
            worst = None
            for destination, source in partners.items():
                delta = max(
                    abs(after[destination][k] - after[source][columns[k]])
                    for k in range(len(names))
                )
                worst = delta if worst is None else max(worst, delta)
                checked += 1

            written, write_worst = 0, 0.0
            for destination, intended in writes.items():
                delta = max(
                    abs(after[destination][k] - intended[k])
                    for k in range(len(names))
                )
                write_worst = max(write_worst, delta)
                if delta <= WEIGHT_EPSILON:
                    written += 1

            changed, max_delta = 0, 0.0
            if snapshot_data is not None:
                changed, max_delta = _changed_vs_snapshot(
                    cluster, shape, snapshot_data
                )

            reports.append({
                "cluster": cluster,
                "mesh": node,
                "attempted": len(writes),
                "written": written,
                "write_worst": write_worst,
                "changed": changed,
                "max_delta": max_delta,
                "selected": len(ids),
                "unmatched": unmatched,
                "midline": midline,
                "unpaired_influences": unpaired,
                "worst": worst,
                "checked": checked,
                "match": match,
                "worstGap": worst_gap,
                "collisions": 0,
                "snapshot": snapshot,
                "skipped": None,
            })
    finally:
        cmds.undoInfo(closeChunk=True)
    return reports


def mirror_selftest():
    """Assert the mirror maths. Run from mayapy or the Script Editor."""
    src = om.MMatrix([0.0, 1.0, 0.0, 0.0,
                      0.0, 0.0, 1.0, 0.0,
                      1.0, 0.0, 0.0, 0.0,
                      3.0, 4.0, 5.0, 1.0])
    out = _MIRROR_X * src * _MIRROR_X
    assert [round(v, 9) for v in list(out)[12:15]] == [-3.0, 4.0, 5.0], "position"
    # a joint cannot hold a left-handed basis; the reflection must cancel out
    assert round(out.det3x3(), 9) == 1.0, "handedness"
    # x-axis survives the reflection, y and z flip -- the whole point
    assert [round(v, 9) + 0.0 for v in list(out)[0:3]] == [0.0, -1.0, 0.0], "x axis"
    back = _MIRROR_X * out * _MIRROR_X
    assert max(abs(a - b) for a, b in zip(list(back), list(src))) < 1e-9, "involution"
    return "mirror_selftest OK"


# ---------------------------------------------------------------------------
# range-of-motion sweep -- pose, look, put it back. Never keys.
# ---------------------------------------------------------------------------

ROM_ANGLE = 45.0
_AXES = ("X", "Y", "Z")
_POSE_TOLERANCE = 1e-6

# The sweep in progress, so one hotkey can step it across separate calls.
# ponytail: one sweep at a time; key it by rig if two ever need to run at once.
_ROM = None


def _rotate_drivers(joint):
    """Connections feeding a joint's rotate -- the compound plug and its children.

    Checking only ``.rotate`` misses a curve wired straight to ``.rotateX``,
    which is exactly how a keyed pose arrives.
    """
    plugs = []
    for attr in ("rotate", "rotateX", "rotateY", "rotateZ"):
        plugs.extend(
            cmds.listConnections(
                "%s.%s" % (joint, attr), source=True, destination=False, plugs=True
            ) or []
        )
    return plugs


def _poseable(joint):
    """``(ok, reason)`` -- usable means we can pose it AND put it back exactly."""
    if not cmds.objExists(joint + ".rotate"):
        return False, "no rotate channel"
    if not cmds.listRelatives(joint, parent=True, type="joint"):
        # A joint with nothing above it moves everything below it rigidly and
        # deforms nothing, so the pose cannot fail -- it is not a test. This is
        # what makes "select the root, sweep the whole rig" safe to ask for:
        # the root drops out on its own, no joint names involved.
        return False, "root joint (nothing above it to twist against)"
    locked = [
        attr
        for attr in ("rotateX", "rotateY", "rotateZ")
        if cmds.getAttr("%s.%s" % (joint, attr), lock=True)
    ]
    if locked:
        return False, "locked: " + ", ".join(locked)
    drivers = _rotate_drivers(joint)
    if drivers:
        # A constraint would fight the pose; an anim curve would swallow it the
        # moment time changed, and overwriting it would destroy real animation.
        return False, "rotate is driven by %s" % drivers[0]
    return True, ""


def _rom_plan(joints, angle):
    """Pure: the ordered pose list, ``(joint, axis, degrees)`` per step.

    Six steps per joint -- both directions on each of its own local axes. Local
    is the whole trick: it needs no joint names and no per-rig angle table, so a
    rig whose joints carry jointOrient sweeps correctly without the sweep
    knowing anything about it.
    """
    steps = []
    for joint in joints:
        for axis in _AXES:
            steps.append((joint, axis, angle))
            steps.append((joint, axis, -angle))
    return steps


def rom_start(joints=None, angle=ROM_ANGLE, include_hierarchy=False):
    """Begin a range-of-motion sweep on the selected joints.

    Records each joint's CURRENT rotate, so poses are added on top of the rig's
    rest shape and ``rom_reset`` restores exactly that -- not zero. A joint that
    legitimately carries rotate (any joint that went through a Move Joints pass)
    would otherwise be snapped to a different axis by the reset.

    Returns ``{"joints": [...], "steps": n, "skipped": [(joint, reason)]}``.
    """
    global _ROM
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if include_hierarchy and joints:
        children = cmds.listRelatives(
            joints, allDescendents=True, type="joint", fullPath=True
        ) or []
        joints.extend(child for child in children if child not in joints)
        # allDescendents comes back deepest-first, so an arm would sweep
        # fingertips before the shoulder. Sort by DAG depth (stable, so
        # same-depth joints keep their order) and the sequence reads the way
        # the limb is built: shoulder, elbow, wrist, fingers.
        joints.sort(key=lambda path: path.count("|"))
    if not joints:
        raise SkinBenchError("Select the joint (or joints) to sweep.")

    usable, skipped = [], []
    for joint in joints:
        ok, reason = _poseable(joint)
        if ok:
            usable.append(joint)
        else:
            skipped.append((joint, reason))
    if not usable:
        raise SkinBenchError(
            "Nothing to sweep -- every joint is locked or driven:\n  "
            + "\n  ".join("%s (%s)" % pair for pair in skipped)
        )

    _ROM = {
        "steps": _rom_plan(usable, float(angle)),
        "index": -1,
        "originals": dict(
            (joint, tuple(cmds.getAttr(joint + ".rotate")[0])) for joint in usable
        ),
        "posed": None,
        "angle": float(angle),
        "hierarchy": bool(include_hierarchy),
        "selection": tuple(cmds.ls(selection=True, long=True, type="joint") or ()),
    }
    return {"joints": usable, "steps": len(_ROM["steps"]), "skipped": skipped}


def _rom_stale(angle, include_hierarchy):
    """True when the request differs from the sweep in progress."""
    if _ROM is None:
        return True
    if _ROM["hierarchy"] != bool(include_hierarchy):
        return True
    if abs(_ROM["angle"] - float(angle)) > 1e-9:
        return True
    current = tuple(cmds.ls(selection=True, long=True, type="joint") or ())
    # An empty selection is not a change -- clicking into empty space mid-sweep
    # must not throw the sweep away.
    return bool(current) and current != _ROM["selection"]


def rom_next(delta=1, angle=ROM_ANGLE, include_hierarchy=False):
    """Step the sweep, starting or restarting it when the request has changed.

    A menu item passes the SAME flags on every press, so "restart" has to mean
    "this request differs from the sweep in progress" -- a different selection,
    a different hierarchy setting, or a different angle. Testing the flag alone
    would restart the hierarchy item on every click and never reach pose 2;
    ignoring it entirely (the live-testing bug) silently drops the flag, so
    picking the hierarchy item after starting a single-joint sweep did nothing.

    Returns ``(label, index, total, report)``; ``report`` is the start report
    when a sweep began, and ``None`` when an existing one simply stepped.
    """
    report = None
    if _rom_stale(angle, include_hierarchy):
        rom_reset()  # put the outgoing sweep's joints back before starting over
        report = rom_start(angle=angle, include_hierarchy=include_hierarchy)
    return rom_step(delta) + (report,)


def rom_step(delta=1):
    """Apply the next pose. Returns ``(label, index, total)``, 1-based index.

    One joint is posed at a time, against a parent that stays put -- rotating a
    whole chain together moves it rigidly and deforms nothing, so it cannot
    fail. The previously posed joint is restored before the next one moves, so
    what you are looking at is one bend, never a pile of them.
    """
    if _ROM is None:
        raise SkinBenchError("No sweep running -- start one first.")

    total = len(_ROM["steps"])
    index = (_ROM["index"] + delta) % total
    joint, axis, degrees = _ROM["steps"][index]

    cmds.undoInfo(openChunk=True, chunkName="SkinBench ROM step")
    try:
        _rom_unpose()
        base = _ROM["originals"][joint]
        offset = [0.0, 0.0, 0.0]
        offset[_AXES.index(axis)] = degrees
        # setAttr, never setKeyframe. A production character exported a clean
        # FBX out of a scene that still held 700 pose curves on 70 joints.
        cmds.setAttr(joint + ".rotate", *[base[i] + offset[i] for i in range(3)])
        _ROM["posed"] = joint
        _ROM["index"] = index
    finally:
        cmds.undoInfo(closeChunk=True)

    return _rom_label(index), index + 1, total


def _rom_unpose():
    """Put the currently posed joint back where the sweep found it."""
    joint = _ROM["posed"]
    if joint and cmds.objExists(joint):
        cmds.setAttr(joint + ".rotate", *_ROM["originals"][joint])
    _ROM["posed"] = None


def _rom_label(index):
    joint, axis, degrees = _ROM["steps"][index]
    return "%s  rotate%s %+g" % (joint.split("|")[-1], axis, degrees)


def rom_reset():
    """End the sweep, put every joint back, and PROVE it came back.

    Returns the joints that did not return to their starting rotate -- an empty
    list is the pass. "It moved" is not proof of anything; "everything is back
    and here is the check" is.
    """
    global _ROM
    if _ROM is None:
        return []

    stragglers = []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench ROM reset")
    try:
        for joint, original in _ROM["originals"].items():
            if not cmds.objExists(joint):
                stragglers.append((joint, "no longer exists"))
                continue
            cmds.setAttr(joint + ".rotate", *original)
            now = cmds.getAttr(joint + ".rotate")[0]
            if any(abs(now[i] - original[i]) > _POSE_TOLERANCE for i in range(3)):
                stragglers.append(
                    (joint, "sits at %s, expected %s" % (tuple(now), original))
                )
    finally:
        cmds.undoInfo(closeChunk=True)
        _ROM = None
    return stragglers


def rom_state():
    """``(label, index, total)`` for the sweep in progress, or ``None``."""
    if _ROM is None:
        return None
    total = len(_ROM["steps"])
    if _ROM["index"] < 0:
        return "not started", 0, total
    return _rom_label(_ROM["index"]), _ROM["index"] + 1, total


# ---------------------------------------------------------------------------
# chain curl -- bend a whole limb at once, cumulatively
# ---------------------------------------------------------------------------

CURL_STEP = 15.0
_LIMIT_FLAGS = {"X": ("rx", "erx"), "Y": ("ry", "ery"), "Z": ("rz", "erz")}
_CURL = None


def _clamp_to_limit(joint, axis, value):
    """Clamp a rotate value to this joint's own enabled rotation limit.

    Maya's per-joint limits live ON the joint, in ITS axes, so they are already
    correct for whatever rig they are on -- and the rigger, not this tool,
    decides what is anatomical. Nothing enabled means no clamp.

    During live testing, an imported rig had none set. All 107 joints carried
    ``-360/360`` with no enable flags, which is the FBX-import passthrough. So
    this is the knob for when limits get authored, not a live constraint today.
    """
    value_flag, enable_flag = _LIMIT_FLAGS[axis]
    low_on, high_on = cmds.transformLimits(joint, query=True, **{enable_flag: True})
    if not (low_on or high_on):
        return value
    low, high = cmds.transformLimits(joint, query=True, **{value_flag: True})
    if low_on:
        value = max(value, low)
    if high_on:
        value = min(value, high)
    return value


def curl_chain(degrees=CURL_STEP, axis=None, joints=None):
    """Bend every joint from the selection down by ``degrees`` on one local axis.

    Cumulative: call it again and the chain bends further. This is the opposite
    end of the telescope from the ROM sweep. The sweep isolates ONE joint to
    ask *is this joint's paint broken*; the curl bends the WHOLE chain to ask
    *does the limb read right when it actually bends*. Neither replaces the
    other -- a chain moving together is closer to rigid motion, so a curl can
    hide the single-joint break the sweep is built to expose.

    ``axis`` defaults to the last one curled, then to X. Rotations are clamped
    to each joint's own enabled limits, so an over-eager tenth click cannot
    fold a finger through the palm on a rig where the limits were authored.

    Returns ``{"joints": [...], "skipped": [...], "clamped": [...]}``.
    """
    global _CURL
    if axis is None:
        axis = _CURL["axis"] if _CURL else "X"
    axis = axis.upper()
    if axis not in _LIMIT_FLAGS:
        raise SkinBenchError("Axis must be X, Y or Z -- got %r." % axis)

    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    if not joints:
        raise SkinBenchError("Select the joint to curl from.")
    children = cmds.listRelatives(
        joints, allDescendents=True, type="joint", fullPath=True
    ) or []
    chain = list(joints) + [child for child in children if child not in joints]

    usable, skipped = [], []
    for joint in chain:
        ok, reason = _poseable(joint)
        if ok:
            usable.append(joint)
        else:
            skipped.append((joint, reason))
    if not usable:
        raise SkinBenchError(
            "Nothing to curl -- every joint is a root, locked, or driven:\n  "
            + "\n  ".join("%s (%s)" % pair for pair in skipped)
        )

    if _CURL is None:
        _CURL = {"originals": {}, "axis": axis}
    _CURL["axis"] = axis
    index = _AXES.index(axis)

    clamped = []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench curl chain")
    try:
        for joint in usable:
            values = list(cmds.getAttr(joint + ".rotate")[0])
            # Recorded once per joint, on its first curl, so Straighten returns
            # it to where the rig actually was -- not to zero.
            _CURL["originals"].setdefault(joint, tuple(values))
            wanted = values[index] + degrees
            values[index] = _clamp_to_limit(joint, axis, wanted)
            if abs(values[index] - wanted) > 1e-9:
                clamped.append(joint)
            # setAttr, never setKeyframe -- same law as the sweep.
            cmds.setAttr(joint + ".rotate", *values)
    finally:
        cmds.undoInfo(closeChunk=True)

    return {"joints": usable, "skipped": skipped, "clamped": clamped}


def straighten_chain():
    """Undo every curl since the last straighten, and PROVE it came back.

    Same contract as ``rom_reset``: back to the rotate each joint HAD when the
    curl first touched it, not to zero, then re-read and name anything that did
    not return. Returns that list -- empty is the pass.
    """
    global _CURL
    if _CURL is None:
        return []

    stragglers = []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench straighten chain")
    try:
        for joint, original in _CURL["originals"].items():
            if not cmds.objExists(joint):
                stragglers.append((joint, "no longer exists"))
                continue
            cmds.setAttr(joint + ".rotate", *original)
            now = cmds.getAttr(joint + ".rotate")[0]
            if any(abs(now[i] - original[i]) > _POSE_TOLERANCE for i in range(3)):
                stragglers.append(
                    (joint, "sits at %s, expected %s" % (tuple(now), original))
                )
    finally:
        cmds.undoInfo(closeChunk=True)
        _CURL = None
    return stragglers


def rom_selftest():
    """Assert the pose plan. Run from mayapy or the Script Editor."""
    steps = _rom_plan(["a", "b"], 45.0)
    assert len(steps) == 12, steps
    assert steps[0] == ("a", "X", 45.0), steps[0]
    assert steps[1] == ("a", "X", -45.0), steps[1]
    assert steps[6][0] == "b", "joints must not interleave"
    assert len(set(steps)) == len(steps), "no duplicate poses"
    assert abs(sum(degrees for _, _, degrees in steps)) < 1e-12, "every bend needs its opposite"
    axes = [axis for joint, axis, _ in steps if joint == "a"]
    assert axes == ["X", "X", "Y", "Y", "Z", "Z"], axes
    print("SkinBench ROM selftest OK -- %d steps for 2 joints" % len(steps))
    return True


# ---------------------------------------------------------------------------
# bind photo sync (port of the proven v3 snippet)
# ---------------------------------------------------------------------------


def _bind_pre_matrix_index(cluster, influence):
    plugs = cmds.listConnections(influence + ".worldMatrix[0]", plugs=True, type="skinCluster") or []
    for plug in plugs:
        if plug.startswith(cluster + ".matrix["):
            return int(plug.split("[")[1].rstrip("]"))
    raise SkinBenchError("'%s' is not an influence on '%s'." % (influence, cluster))


def sync_bind_photo(cluster, influence, reference):
    """Rewrite one influence's bind photo so it agrees with the current pose.

    Use on a freshly Add Influence-d joint, before painting, so the first brush
    stroke does not drag verts. ``reference`` must already be an influence on
    the SAME skinCluster; it supplies the "where the body actually is" anchor,
    which is why this works at any frame and does not need a true bind pose.
    """
    if influence == reference:
        raise SkinBenchError("Reference influence must be a different joint.")
    joint_index = _bind_pre_matrix_index(cluster, influence)
    ref_index = _bind_pre_matrix_index(cluster, reference)

    world_joint = om.MMatrix(cmds.getAttr(influence + ".worldMatrix[0]"))
    world_ref = om.MMatrix(cmds.getAttr(reference + ".worldMatrix[0]"))
    bind_ref = om.MMatrix(cmds.getAttr("%s.bindPreMatrix[%d]" % (cluster, ref_index)))

    synced = bind_ref * world_ref * world_joint.inverse()
    cmds.setAttr(
        "%s.bindPreMatrix[%d]" % (cluster, joint_index), *list(synced), type="matrix"
    )
    return joint_index


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


def snapshot_dir():
    """Where snapshots land: beside the scene file, or in the Maya app dir."""
    scene = cmds.file(query=True, sceneName=True)
    if scene:
        base = os.path.join(os.path.dirname(scene), "skinbench")
        scene_name = os.path.splitext(os.path.basename(scene))[0]
        path = os.path.join(base, scene_name)
    else:
        path = os.path.join(cmds.internalVar(userAppDir=True), "skinbench", "_unsaved")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _timestamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _file_stem(mesh):
    """Filename-safe identity for a mesh, NAMESPACE INCLUDED.

    Two referenced rigs can both own a mesh called ``body``. Stripping the
    namespace would give them the same snapshot prefix, so ``charA:body`` could
    be reattached from ``charB``'s snapshot -- and a same-second save would
    overwrite the only recovery copy. Keeping the namespace keeps them apart.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", mesh.split("|")[-1])


def uuid_of(node):
    """Maya's stable UUID for a node, or None. Survives renames."""
    if not node or not cmds.objExists(node):
        return None
    found = cmds.ls(node, uuid=True) or []
    return found[0] if found else None


def _node_from_uuid(uuid):
    if not uuid:
        return None
    found = cmds.ls(uuid, long=True) or []
    return found[0] if len(found) == 1 else None


def snapshot_path(mesh, directory=None):
    directory = directory or snapshot_dir()
    stem = "%s__%s" % (_file_stem(mesh), _timestamp())
    path = os.path.join(directory, stem + ".json")
    # Timestamps are per-second; two saves inside the same second must not
    # clobber each other, because the older file may be the only pristine copy.
    counter = 1
    while os.path.exists(path):
        path = os.path.join(directory, "%s-%d.json" % (stem, counter))
        counter += 1
    return path


def list_snapshots(directory=None):
    """Snapshot files newest first."""
    directory = directory or snapshot_dir()
    if not os.path.isdir(directory):
        return []
    files = [
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.lower().endswith(".json")
    ]
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def read_snapshot(path):
    with open(path, "r") as handle:
        data = json.load(handle)
    if data.get("version") != SNAPSHOT_VERSION:
        raise SkinBenchError(
            "Snapshot '%s' is version %s; this SkinBench reads version %d."
            % (os.path.basename(path), data.get("version"), SNAPSHOT_VERSION)
        )
    return data


def save_snapshot(mesh, path=None):
    """Capture everything needed to rebuild this mesh's skinning.

    Does not modify the scene. Returns ``(path, data)``.
    """
    shape = deformed_shape(mesh)
    cluster = skin_cluster_for(shape)
    if not cluster:
        raise SkinBenchError("'%s' has no skinCluster to snapshot." % mesh)

    fn = _skin_fn(cluster)
    dag = _dag_path(shape)
    vertex_count = cmds.polyEvaluate(shape, vertex=True)
    component = _complete_vertex_component(vertex_count)

    names, logical = _influence_table(fn)
    flat, influence_count = fn.getWeights(dag, component)

    vertex_ids, influence_ids, values = [], [], []
    for vertex in range(vertex_count):
        row = vertex * influence_count
        for slot in range(influence_count):
            weight = flat[row + slot]
            if weight > WEIGHT_EPSILON:
                vertex_ids.append(vertex)
                influence_ids.append(slot)
                values.append(round(weight, 8))

    try:
        blend = list(fn.getBlendWeights(dag, component))
    except RuntimeError:
        blend = []
    if blend and not any(value > WEIGHT_EPSILON for value in blend):
        blend = []

    settings = {}
    for attr in _SETTINGS_ATTRS:
        plug = "%s.%s" % (cluster, attr)
        if cmds.objExists(plug):
            settings[attr] = cmds.getAttr(plug)

    transform = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
    data = {
        "version": SNAPSHOT_VERSION,
        "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scene": cmds.file(query=True, sceneName=True) or "<unsaved>",
        "mesh": transform,
        "meshUuid": uuid_of(transform),
        "shape": shape,
        "skinCluster": cluster,
        "vertexCount": vertex_count,
        "influences": names,
        # UUIDs are the only identity that survives a rename -- names are kept
        # as the human-readable fallback for hand-edited or cross-scene use.
        "influenceUuids": [uuid_of(name) for name in names],
        "influenceIndices": logical,
        "bindPreMatrices": [
            cmds.getAttr("%s.bindPreMatrix[%d]" % (cluster, index)) for index in logical
        ],
        "settings": settings,
        "weights": {"vertex": vertex_ids, "influence": influence_ids, "value": values},
        "blendWeights": blend,
    }

    # Named after the TRANSFORM, so newest_snapshot_for() can find it again.
    path = path or snapshot_path(transform)
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path, data


# ---------------------------------------------------------------------------
# detach / reattach
# ---------------------------------------------------------------------------


def detach(mesh, path=None):
    """Snapshot the skinning, then unbind. Returns ``(snapshot_path, cluster)``.

    The mesh keeps its current shape; the joints are free to be moved,
    reparented, reoriented, deleted or added before you reattach.
    """
    path, data = save_snapshot(mesh, path=path)
    cluster = data["skinCluster"]
    cmds.undoInfo(openChunk=True, chunkName="SkinBench detach")
    try:
        cmds.skinCluster(cluster, edit=True, unbind=True)
    finally:
        cmds.undoInfo(closeChunk=True)
    return path, cluster


def _set_if_settable(plug, value):
    """setAttr that quietly skips locked, connected or absent plugs."""
    if not cmds.objExists(plug):
        return False
    if cmds.getAttr(plug, lock=True):
        return False
    if cmds.listConnections(plug, source=True, destination=False, plugs=True):
        return False
    cmds.setAttr(plug, value)
    return True


def _resolve_influences(saved_names, saved_uuids=None):
    """Map saved influence names onto nodes that exist now.

    Resolution order, most reliable first:

    1. **UUID** -- Maya's stable node id. This is what survives an actual
       rename; names alone do not, because a renamed joint is findable under
       neither its old path nor its old leaf name.
    2. Exact full DAG path.
    3. Unique leaf name -- catches reparenting.
    4. Unique ``*:leaf`` -- catches a namespace change.

    Returns ``(resolved, remapped, missing)``.
    """
    saved_uuids = saved_uuids or [None] * len(saved_names)
    resolved, remapped, missing = [], [], []

    for index, name in enumerate(saved_names):
        uuid = saved_uuids[index] if index < len(saved_uuids) else None

        target = _node_from_uuid(uuid)
        if target:
            resolved.append((name, target))
            if target != name:
                remapped.append((name, target))
            continue

        if cmds.objExists(name):
            resolved.append((name, cmds.ls(name, long=True)[0]))
            continue

        leaf = name.split("|")[-1].split(":")[-1]
        matches = (
            cmds.ls(leaf, long=True, type="joint")
            or cmds.ls("*:" + leaf, long=True, type="joint")
            or cmds.ls(leaf, long=True)
            or cmds.ls("*:" + leaf, long=True)
            or []
        )
        if len(matches) == 1:
            resolved.append((name, matches[0]))
            remapped.append((name, matches[0]))
        else:
            resolved.append((name, None))
            missing.append(name)

    return resolved, remapped, missing


def _verify_snapshot_target(mesh, data, allow_mismatch=False):
    """Refuse to pour one mesh's weights into a different mesh.

    The vertex-count guard does not catch this on its own: two referenced rigs
    of the same character have identical topology, so ``charB``'s snapshot
    would load onto ``charA`` without complaint.
    """
    if allow_mismatch:
        return
    saved_uuid = data.get("meshUuid")
    if not saved_uuid:
        return  # snapshot predates UUID tracking; names are all we have
    current_uuid = uuid_of(mesh)
    if current_uuid and current_uuid != saved_uuid:
        raise SkinBenchError(
            "That snapshot belongs to '%s', not '%s' (different node, same "
            "topology). Pick the snapshot for this mesh, or call with "
            "allow_mismatch=True if you really mean to transfer weights "
            "between them." % (data.get("mesh", "?").split("|")[-1], mesh.split("|")[-1])
        )


def reattach(mesh, snapshot, restore_bind_matrices=False, cluster_name=None,
             allow_mismatch=False):
    """Rebind ``mesh`` and pour the snapshot's weights back in.

    By default the new skinCluster photographs the joints where they sit NOW,
    which is what makes "detach -> move joints -> reattach" behave: the mesh
    does not move, the weights are the ones you painted, and the joints drive
    from their new positions.

    Set ``restore_bind_matrices=True`` to also write the old bindPreMatrix
    values back, i.e. an exact undo of the detach.

    Returns a report dict: ``skinCluster``, ``remapped``, ``missing``,
    ``dropped`` (influences that no longer exist and whose weights were
    redistributed by normalization).
    """
    data = read_snapshot(snapshot) if isinstance(snapshot, str) else snapshot

    shape = deformed_shape(mesh)
    existing = skin_cluster_for(shape)
    if existing:
        raise SkinBenchError(
            "'%s' is already skinned by '%s'. Detach it first, or use "
            "apply_weights() to load the snapshot onto the existing skin."
            % (mesh, existing)
        )

    _verify_snapshot_target(mesh, data, allow_mismatch)

    vertex_count = cmds.polyEvaluate(shape, vertex=True)
    if vertex_count != data["vertexCount"]:
        raise SkinBenchError(
            "Vertex count changed since the snapshot (%d now, %d then). The "
            "weights no longer describe this mesh -- reattach refused."
            % (vertex_count, data["vertexCount"])
        )

    resolved, remapped, missing = _resolve_influences(
        data["influences"], data.get("influenceUuids")
    )
    live = []
    for _, target in resolved:
        if target and target not in live:
            live.append(target)
    if not live:
        raise SkinBenchError("None of the snapshot's influences exist in the scene.")

    settings = data.get("settings", {})
    cmds.undoInfo(openChunk=True, chunkName="SkinBench reattach")
    try:
        cluster = cmds.skinCluster(
            live + [shape],
            toSelectedBones=True,
            bindMethod=0,
            skinMethod=int(settings.get("skinningMethod", 0)),
            weightDistribution=int(settings.get("weightDistribution", 0)),
            maximumInfluences=int(settings.get("maxInfluences", 5)),
            obeyMaxInfluences=False,
            removeUnusedInfluence=False,
            normalizeWeights=0,
            name=cluster_name or data.get("skinCluster") or "skinCluster1",
        )[0]

        orphans = _pour_weights(cluster, shape, data, resolved)

        if restore_bind_matrices:
            _restore_bind_matrices(cluster, data, resolved)

        for attr, value in settings.items():
            _set_if_settable("%s.%s" % (cluster, attr), value)

        if missing:
            # Weights that belonged to vanished joints are gone; renormalize so
            # the surviving influences still sum to 1.
            cmds.skinPercent(cluster, shape, normalize=True)
    finally:
        cmds.undoInfo(closeChunk=True)

    return {
        "skinCluster": cluster,
        "remapped": remapped,
        "missing": missing,
        "dropped": len(missing),
        "influences": len(live),
        "orphanedVertices": orphans,
        "snapshot": snapshot if isinstance(snapshot, str) else "<in-memory>",
    }


def apply_weights(mesh, snapshot, allow_mismatch=False):
    """Pour a snapshot's weights onto the mesh's EXISTING skinCluster.

    Useful when you rebound by hand, or copied a rig, and only want the
    painted values back. Missing influences are reported, not fatal.
    """
    data = read_snapshot(snapshot) if isinstance(snapshot, str) else snapshot
    shape = deformed_shape(mesh)
    cluster = skin_cluster_for(shape)
    if not cluster:
        raise SkinBenchError("'%s' has no skinCluster. Use Reattach instead." % mesh)

    _verify_snapshot_target(mesh, data, allow_mismatch)

    vertex_count = cmds.polyEvaluate(shape, vertex=True)
    if vertex_count != data["vertexCount"]:
        raise SkinBenchError(
            "Vertex count changed since the snapshot (%d now, %d then)."
            % (vertex_count, data["vertexCount"])
        )

    resolved, remapped, missing = _resolve_influences(
        data["influences"], data.get("influenceUuids")
    )
    fn = _skin_fn(cluster)
    live_names = set(_influence_table(fn)[0])
    absent = [
        saved for saved, target in resolved if target and target not in live_names
    ]
    if absent:
        raise SkinBenchError(
            "These joints exist but are not influences on '%s': %s. Add them "
            "first (Skin > Edit Influences > Add Influence)."
            % (cluster, ", ".join(name.split("|")[-1] for name in absent[:6]))
        )

    normalize = cmds.getAttr(cluster + ".normalizeWeights")
    cmds.undoInfo(openChunk=True, chunkName="SkinBench apply weights")
    normalization_restored = False
    operation_error = None
    restore_error = None
    close_error = None
    try:
        _set_if_settable(cluster + ".normalizeWeights", 0)
        orphans = _pour_weights(cluster, shape, data, resolved)
        _set_if_settable(cluster + ".normalizeWeights", normalize)
        normalization_restored = True
        if missing:
            cmds.skinPercent(cluster, shape, normalize=True)
    except BaseException:
        operation_error = sys.exc_info()

    if not normalization_restored:
        try:
            _set_if_settable(cluster + ".normalizeWeights", normalize)
        except BaseException:
            restore_error = sys.exc_info()

    try:
        cmds.undoInfo(closeChunk=True)
    except BaseException:
        close_error = sys.exc_info()

    if operation_error:
        raise operation_error[1].with_traceback(operation_error[2])
    if restore_error:
        raise restore_error[1].with_traceback(restore_error[2])
    if close_error:
        raise close_error[1].with_traceback(close_error[2])

    return {
        "skinCluster": cluster,
        "remapped": remapped,
        "missing": missing,
        "dropped": len(missing),
        "influences": len(live_names),
        "orphanedVertices": orphans,
        "snapshot": snapshot if isinstance(snapshot, str) else "<in-memory>",
    }


def _pour_weights(cluster, shape, data, resolved):
    """Write the snapshot's sparse weights onto ``cluster``."""
    fn = _skin_fn(cluster)
    dag = _dag_path(shape)
    vertex_count = data["vertexCount"]
    component = _complete_vertex_component(vertex_count)

    names, _ = _influence_table(fn)
    slot_of = {name: index for index, name in enumerate(names)}

    # saved influence slot -> slot in the new skinCluster (or None if gone)
    lane = []
    for saved_name, target in resolved:
        lane.append(slot_of.get(target) if target else None)

    influence_count = len(names)
    flat = om.MDoubleArray([0.0] * (vertex_count * influence_count))

    # Vertices that had weight in the snapshot but lost all of it because their
    # influences are gone. skinPercent -normalize cannot rescue those (there is
    # nothing left to scale up), so they must be reported, not swallowed.
    had_weight = set()
    kept_weight = set()

    weights = data["weights"]
    for vertex, saved_slot, value in zip(
        weights["vertex"], weights["influence"], weights["value"]
    ):
        had_weight.add(vertex)
        slot = lane[saved_slot] if saved_slot < len(lane) else None
        if slot is None:
            continue
        flat[vertex * influence_count + slot] += value
        kept_weight.add(vertex)

    _set_weights(fn, dag, component, flat, vertex_count)

    blend = data.get("blendWeights") or []
    if blend and len(blend) == vertex_count:
        fn.setBlendWeights(dag, component, om.MDoubleArray(blend))

    return sorted(had_weight - kept_weight)


def _restore_bind_matrices(cluster, data, resolved):
    fn = _skin_fn(cluster)
    names, logical = _influence_table(fn)
    slot_of = {name: index for index, name in enumerate(names)}
    saved_matrices = data.get("bindPreMatrices") or []

    for saved_slot, (saved_name, target) in enumerate(resolved):
        if not target or saved_slot >= len(saved_matrices):
            continue
        slot = slot_of.get(target)
        if slot is None:
            continue
        cmds.setAttr(
            "%s.bindPreMatrix[%d]" % (cluster, logical[slot]),
            *saved_matrices[saved_slot],
            type="matrix"
        )


# ---------------------------------------------------------------------------
# anti-bleed: weights reaching further than the joint physically can
# ---------------------------------------------------------------------------

# Radius = N x the OWNING JOINT'S OWN bone length. Not a fixed distance, and not
# a fraction of the character. A pinky bone is ~10 units and a spine bone ~40, so
# the pinky gets a tight radius and the spine a wide one, from the same N, with
# no per-part table to maintain and nothing to re-tune when the rig is scaled.
#
# Proven against saved weight data during offline testing: cleanup reduced a
# set of 99 strippable pairs to 0. This is the live half of that tool.
#
# The question it asks -- "is this joint physically anywhere near the vertex it
# is pulling?" -- is the one that has no body-part vocabulary, so it has no
# group table to be wrong about. bleed.py and region_drift.py both classify a
# vertex by its DOMINANT influence, so a vertex owned by the wrong body part got
# filed under the wrong body part and was never examined; both scored four
# toe-owned hand verts clean.
BLEED_N = 8.0
BLEED_FLOOR = 0.005     # below this a weight moves nothing visible


def _world_point(node):
    return tuple(cmds.xform(node, query=True, worldSpace=True, translation=True))


def _distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _reach_from_segments(segments):
    """The reach a joint earns from the lengths of the bones attached to it.

    Split out from ``bone_length`` so the RULE can be asserted without a scene.
    """
    longest = max(segments or [0.0])
    return longest if longest > 1e-6 else None


def bone_length(joint):
    """How far this joint can plausibly reach: its LONGEST attached segment.

    Incoming (parent -> joint) or outgoing (joint -> joint child), whichever is
    longer.

    The old rule was "the segment that ENDS at the joint". That is the textbook
    definition of a bone length, and it is wrong for any joint whose real body
    runs FORWARD from it. During live testing, ``jaw_joint`` hung off its parent
    on a 5.43-unit stub, while the long-jaw rig segment it actually drives ran
    ~50 units forward to ``end_joint``. Measured by the stub, its own snout tip
    sits 8.7 "bone lengths" out -- so at the default N=8 the tool
    flagged 47 vertices the upper jaw owns outright at w=1.000, and at N=2 the
    radius pulled in the face and the eyes. The joint was being judged by the
    wrong bone entirely.

    Taking the longest attached segment fixes it with no per-rig dial: a pinky
    still gets a tight radius, because every segment near a pinky is short, and
    the upper jaw gets the long one it earns. It can only ever make the radius
    BIGGER, so the change is one-way safe -- it removes false accusations and
    cannot invent new ones.

    A joint with neither parent joint nor joint children returns None and is
    never flagged. Silence is the safe answer when the rig gives no evidence for
    a scale; a joint measured as 0 would make every ratio infinite and flag the
    whole rig.

    Measured in WORLD space, the same space as the vertex distances, so the
    ratio is immune to rig scale. (A reference rig carried a baked 1.5 scale.
    A distance test that mixes object-space verts with world-space joints is
    wrong by exactly that factor and looks entirely plausible.)
    """
    here = _world_point(joint)
    segments = []
    parent = (cmds.listRelatives(joint, parent=True, fullPath=True, type="joint") or [None])[0]
    if parent:
        segments.append(_distance(here, _world_point(parent)))
    for child in cmds.listRelatives(joint, children=True, fullPath=True, type="joint") or []:
        segments.append(_distance(here, _world_point(child)))
    return _reach_from_segments(segments)


def _one_cluster_from_selection():
    clusters = skin_clusters_from_selection()
    if not clusters:
        raise SkinBenchError(
            "No skinCluster in the selection -- select the skinned mesh, or one "
            "of its influence joints."
        )
    return clusters[0]


def bleed_report(cluster=None, joints=None, n=BLEED_N, floor=BLEED_FLOOR):
    """Which weights pull a vertex from further away than the joint can reach.

    ``joints`` restricts the audit to those influences -- the "clean up THIS toe
    joint" case, which is the one a human can actually judge. ``None`` audits
    every influence on the cluster.

    Reports only; nothing is written. Removal is ``strip_bleed``, which carries
    its own gates.
    """
    cluster = cluster or _one_cluster_from_selection()
    shapes = meshes_for_skin_cluster(cluster)
    if not shapes:
        raise SkinBenchError("'%s' drives no mesh." % cluster)
    shape = shapes[0]
    dag = _dag_path(shape)

    fn = _skin_fn(cluster)
    names, _ = _influence_table(fn)
    influence_count = len(names)

    wanted = set(range(influence_count))
    if joints:
        asked = set(joints) | {name.split("|")[-1] for name in joints}
        wanted = {
            slot
            for slot, name in enumerate(names)
            if name in asked or name.split("|")[-1] in asked
        }
        if not wanted:
            raise SkinBenchError(
                "None of those joints influence '%s'." % cluster
            )

    # Refuse on a posed rig rather than answer the wrong question quietly. The
    # ROM sweep and the curl both leave the rig posed, and they live in this
    # same tool -- so this WILL happen. A pose on these rigs lives in .rotate
    # (rest is zeroed by convention and the FBX gates enforce it), so .rotate
    # is what is checked -- on EVERY influence, because a bent Spine corrupts
    # the throat's distances just as surely as a bent throat.
    #
    # Bind-photo drift was the old check and it can never pass on characterA:
    # all 65 body influences carry a baked ~9 offset from their bind photos
    # at rest, identical in the delivered v94 (a 150%-scale-era leftover,
    # compensated inside the cluster). A baked offset is not a pose.
    # ponytail: misses a joint TRANSLATED without mjm -- that is a structural
    # accident, not a pose; per-joint bind repair lives in the Bind Photo tab.
    posed = posed_joints(names)
    if posed:
        raise SkinBenchError(
            "The rig is POSED -- %s, so distances would be measured against the "
            "pose, not the rig. End the ROM sweep (skinBenchROM -r), zero the "
            "pose (Poses tab), or Ctrl+Z back to rest first." % describe_posed(posed)
        )

    lengths, skipped = {}, []
    for slot in sorted(wanted):
        length = bone_length(names[slot])
        if length is None:
            skipped.append((names[slot].split("|")[-1], "no measurable bone"))
            continue
        lengths[slot] = length
    positions = {slot: _world_point(names[slot]) for slot in lengths}

    vertex_count = cmds.polyEvaluate(shape, vertex=True)
    component = _complete_vertex_component(vertex_count)
    flat, stride = fn.getWeights(dag, component)
    points = om.MFnMesh(dag).getPoints(om.MSpace.kWorld)

    pairs, strip_ids, total_weight, dominant_hits = [], set(), 0.0, 0
    kept_ids, near_ids, dust_ids = set(), set(), set()
    dust_weight = 0.0
    for vertex in range(vertex_count):
        row = vertex * stride
        best_slot, best_weight = None, 0.0
        for slot in range(stride):
            if flat[row + slot] > best_weight:
                best_slot, best_weight = slot, flat[row + slot]
        if best_slot is None:
            continue
        point = points[vertex]
        far_strip = far_kept = near = False
        vertex_dust = 0.0
        for slot, length in lengths.items():
            weight = flat[row + slot]
            if weight <= WEIGHT_EPSILON:
                continue                    # this joint does not touch the vertex
            if weight < floor:
                vertex_dust += weight       # bucket 3: invisible dust
                continue
            ratio = _distance((point.x, point.y, point.z), positions[slot]) / length
            if ratio < n:
                near = True                 # bucket 4: close enough to be real
                continue
            dominant = slot == best_slot
            pairs.append(
                {
                    "vertex": vertex,
                    "joint": names[slot].split("|")[-1],
                    "jointPath": names[slot],
                    "weight": weight,
                    "ratio": ratio,
                    "dominant": dominant,
                }
            )
            if dominant:
                dominant_hits += 1
                far_kept = True             # bucket 2: far, but the main owner
            else:
                strip_ids.add(vertex)       # bucket 1: far and not the owner
                total_weight += weight
                far_strip = True

        # Exactly ONE bucket per vertex, worst first, so the four counts add up
        # to "vertices these joints touch at all". Overlapping buckets are what
        # made the old line unreadable: 45 + 47 + 30 did not equal anything, so
        # blue-but-unselected paint read as the tool being broken.
        if far_strip:
            pass                            # already counted in strip_ids
        elif far_kept:
            kept_ids.add(vertex)
        elif near:
            near_ids.add(vertex)
        elif vertex_dust > 0.0:
            dust_ids.add(vertex)
            dust_weight += vertex_dust

    pairs.sort(key=lambda pair: -pair["ratio"])
    return {
        "skinCluster": cluster,
        "shape": shape,
        "n": n,
        "floor": floor,
        "audited": [names[slot].split("|")[-1] for slot in sorted(lengths)],
        "pairs": pairs,
        "vertices": sorted(strip_ids),
        "strippable": len(pairs) - dominant_hits,
        "dominant": dominant_hits,
        "totalWeight": total_weight,
        "skipped": skipped,
        # -- the four buckets, one vertex in exactly one of them --------------
        "kept": sorted(kept_ids),           # far, but this joint is the owner
        "near": len(near_ids),              # close enough to be real weight
        "dust": len(dust_ids),              # only sub-floor weight here
        "dustWeight": dust_weight,
        "carrying": len(strip_ids) + len(kept_ids) + len(near_ids) + len(dust_ids),
    }


def _renormalized(row, doomed):
    """Zero the doomed slots and scale the survivors back up to 1.0.

    Refuses to return an all-zero row. ``skinPercent -normalize`` cannot rescue
    a vertex with nothing left to scale up, and skipping the renormalize
    silently shrinks the mesh wherever a weight was removed. Gate 2 (never
    strip a vertex's dominant influence) is what makes the empty case
    unreachable -- this is the proof that it held.
    """
    out = list(row)
    for slot in doomed:
        out[slot] = 0.0
    total = sum(out)
    if total <= WEIGHT_EPSILON:
        raise SkinBenchError(
            "stripping would leave a vertex with no weight at all -- refused."
        )
    return [value / total for value in out]


def strip_bleed(cluster=None, joints=None, n=BLEED_N, floor=BLEED_FLOOR, dry_run=True):
    """Delete the bleeding weights ``bleed_report`` found, then renormalize.

    Four gates, each one paid for:

    1. **Snapshot first.** Weight writes go through the API and are not on
       Maya's undo queue. Ctrl+Z will not bring these back.
    2. **Never strip a vertex's DOMINANT influence.** ``end_joint`` reaching
       onto characterB's hips looks exactly like bleed and is correct anatomy. A
       distance test cannot tell those apart; the dominance rule can.
    3. **Renormalize the survivors to 1.0**, or the mesh shrinks where a weight
       was removed.
    4. **Dry run by default.** ``dry_run=False`` is the only thing that writes.
    """
    report = bleed_report(cluster=cluster, joints=joints, n=n, floor=floor)
    report["dryRun"] = bool(dry_run)
    report["snapshot"] = None
    report["strippedVertices"] = 0
    if dry_run or not report["vertices"]:
        return report

    shape = report["shape"]
    mesh = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
    report["snapshot"] = save_snapshot(mesh)[0]                     # gate 1

    fn = _skin_fn(report["skinCluster"])
    dag = _dag_path(shape)
    names, _ = _influence_table(fn)
    slot_of = {name: index for index, name in enumerate(names)}

    doomed = {}
    for pair in report["pairs"]:
        if pair["dominant"]:                                       # gate 2
            continue
        doomed.setdefault(pair["vertex"], []).append(slot_of[pair["jointPath"]])

    vertices = report["vertices"]
    comp_fn = om.MFnSingleIndexedComponent()
    component = comp_fn.create(om.MFn.kMeshVertComponent)
    comp_fn.addElements(vertices)

    flat, stride = fn.getWeights(dag, component)
    updated = om.MDoubleArray(flat)
    for index, vertex in enumerate(vertices):
        row = index * stride
        kept = _renormalized(flat[row:row + stride], doomed.get(vertex, ()))  # gate 3
        for slot, value in enumerate(kept):
            updated[row + slot] = value

    _set_weights(fn, dag, component, updated, len(vertices))
    report["strippedVertices"] = len(vertices)
    return report


# Verdict -> the report counter it increments. One place, so a new verdict
# cannot silently land in the wrong bucket.
_VERDICT_KEY = {"untouched": "untouched", "skip": "skipped", "dominant": "dominant"}


def _zero_verdict(row, doomed, keep_dominant=True):
    """``'untouched'`` / ``'skip'`` / ``'dominant'`` / ``'clear'`` for one vertex.

    Separate from the loop so all three guards can be asserted without a scene.

    ``dominant`` is the gate that stops a region clean from eating the thing it
    was aimed past. ``Select region`` on ``chest_joint`` returns every vertex the
    joint touches at all -- on characterA that is 228 verts, of which 65 are the
    REAL chest area that ``chest_joint`` rightly owns. Skipping is not a wrong
    answer needing a flag; it is what "clean the bleed" meant. ``strip_bleed``
    already keeps dominant weights and says so per pair; this is the same rule.
    """
    removed = sum(row[slot] for slot in doomed)
    if removed <= WEIGHT_EPSILON:
        return "untouched", removed
    if sum(row) - removed <= WEIGHT_EPSILON:
        return "skip", removed
    if keep_dominant and max(range(len(row)), key=lambda slot: row[slot]) in doomed:
        return "dominant", removed
    return "clear", removed


def zero_influence_on_selection(joints=None, cluster=None, keep_dominant=True):
    """Take these joints to ZERO on the selected vertices, renormalize the rest.

    The region-shaped answer to bleed, and it works exactly where the distance
    test cannot. ``bleed_report`` asks "is this weight further than N bone
    lengths from the joint" -- which catches a toe joint reaching across the
    body, and is BLIND to ``jaw_joint`` painting the whole neck, because the
    neck sits well inside two skull-bone-lengths. Preview honestly reports
    "nothing to strip" and the bleed is right there on screen. The artist proved
    the fix by hand during live testing: Select region -> Invert -> flood 0.
    This is that, as one button, with the gates.

    The SELECTION is the preview -- it is on screen before the click -- so
    there is no dry run. A snapshot is still taken first; weight writes go
    through the API and Ctrl+Z will not bring them back.

    A vertex driven ONLY by these joints is SKIPPED, not emptied, and counted
    in ``skipped``. Zeroing it would leave nothing to renormalize and collapse
    the vertex onto the origin.

    A vertex these joints DOMINATE is skipped too, and counted in ``dominant``
    -- see ``_zero_verdict``. Pass ``keep_dominant=False`` only to hand a
    region to a different joint outright, and select that region by hand.
    """
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if not joints:
        raise SkinBenchError(
            "Select the joint to zero -- the one whose bleed you are cleaning."
        )

    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "Select the vertices to clean first -- Select region, then Invert."
        )

    cluster = cluster or _one_cluster_from_selection()
    driven = set(meshes_for_skin_cluster(cluster))
    fn = _skin_fn(cluster)
    names, _ = _influence_table(fn)
    asked = set(joints) | {name.split("|")[-1] for name in joints}
    doomed = [
        slot
        for slot, name in enumerate(names)
        if name in asked or name.split("|")[-1] in asked
    ]
    if not doomed:
        raise SkinBenchError(
            "None of those joints influence '%s' -- right joint, wrong mesh?" % cluster
        )

    report = {
        "skinCluster": cluster,
        "joints": [names[slot].split("|")[-1] for slot in doomed],
        "shapes": [], "snapshots": [],
        "cleared": 0, "skipped": 0, "untouched": 0, "dominant": 0,
        "weightRemoved": 0.0,
        "ignoredShapes": sorted(s.split("|")[-1] for s in grouped if s not in driven),
    }

    for shape in sorted(grouped):
        if shape not in driven:
            continue                       # selection spilled onto another mesh
        vertices = sorted(set(grouped[shape]))
        mesh = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
        report["snapshots"].append(save_snapshot(mesh)[0])          # gate 1
        report["shapes"].append(shape.split("|")[-1])

        dag = _dag_path(shape)
        comp_fn = om.MFnSingleIndexedComponent()
        component = comp_fn.create(om.MFn.kMeshVertComponent)
        comp_fn.addElements(vertices)
        flat, stride = fn.getWeights(dag, component)
        updated = om.MDoubleArray(flat)

        for index in range(len(vertices)):
            row = index * stride
            here = flat[row:row + stride]
            verdict, removed = _zero_verdict(here, doomed, keep_dominant)  # gate 2
            if verdict != "clear":
                report[_VERDICT_KEY[verdict]] += 1
                continue
            for slot, value in enumerate(_renormalized(here, doomed)):  # gate 3
                updated[row + slot] = value
            report["cleared"] += 1
            report["weightRemoved"] += removed

        _set_weights(fn, dag, component, updated, len(vertices))

    if not report["shapes"]:
        raise SkinBenchError(
            "None of the selected vertices belong to '%s'." % cluster
        )
    return report


# ---------------------------------------------------------------------------
# heal: Maya's Weight Hammer on a selection, locks off, with a receipt
# ---------------------------------------------------------------------------

# Below this a weight change is rounding noise, not a fix worth naming.
HEAL_REPORT_FLOOR = 0.001

# Past this many vertices, say out loud that Hammer re-averages ALL of them.
# Measured during live testing on a throwaway two-joint plane: a vertex whose neighbours
# already agreed with it still moved ~0.02. Hammer has no "leave this one
# alone" -- it is a broom for a handful of bad vertices, not for a region.
HEAL_CROWD = 40

# How many joints one log line may name before the tail is summed up instead.
# On a production vertex, dust of 0.0029 sat on ALL 69 influences, so the honest
# full line named 69 joints and was unreadable. The first few movers ARE the
# answer; the tail is dust and only its size matters.
HEAL_LINE_JOINTS = 6


def _heal_line(shape, vertex, moves, limit=HEAL_LINE_JOINTS):
    """One readable log line for a healed vertex. Pure -- testable anywhere."""
    shown = ",  ".join("%s %.4f -> %.4f" % move for move in moves[:limit])
    rest = moves[limit:]
    if rest:
        biggest = max(abs(move[2] - move[1]) for move in rest)
        ceiling = "%.4f" % biggest
        if ceiling == "0.0000":         # never print a size that reads as zero
            ceiling = "0.0001"
        shown += ",  +%d more, none above %s" % (len(rest), ceiling)
    return "  %s.vtx[%d]  %s" % (shape, vertex, shown)


def _weight_moves(before, after, names, floor=HEAL_REPORT_FLOOR):
    """Which influences moved on one vertex: ``[(joint, was, now)]``.

    Biggest move first, so the guilty joint is the first thing printed.
    """
    moves = [
        (names[slot].split("|")[-1], before[slot], after[slot])
        for slot in range(len(names))
        if abs(after[slot] - before[slot]) >= floor
    ]
    moves.sort(key=lambda move: abs(move[2] - move[1]), reverse=True)
    return moves


def unlock_all_influences(cluster):
    """Take the lock off every influence. Returns the names that were locked.

    Maya's *Lock Weights* is the quietest trap in skinning: ``skinPercent``,
    Weight Hammer and the brush all SKIP a locked influence and report
    success. An hour of painting with no effect is the usual first symptom
    (seen during live testing). Healing unlocks first, every time, and LEAVES them
    unlocked -- silently re-arming the trap would be worse than the trap.
    """
    was_locked = []
    for influence in cmds.skinCluster(cluster, query=True, influence=True) or []:
        plug = "%s.liw" % influence
        try:
            if cmds.getAttr(plug):
                cmds.setAttr(plug, 0)
                was_locked.append(influence.split("|")[-1])
        except Exception:
            continue                    # no .liw, or the attr itself is locked
    return was_locked


def heal_selection(cluster=None, examples=6):
    """Weight Hammer the selected vertices, and name the joint it took from.

    Weight Hammer re-computes a vertex's weights from its neighbours. It is
    the right broom for the odd bad vertex -- the one that spikes only when
    the rig is posed -- and the artist proved this click path by hand during
    live testing: ``toe_joint`` weights on the hand and chest were gone, with
    clean neighbours each time.

    Two things this adds over pressing Weight Hammer yourself. The locks come
    off first (see ``unlock_all_influences``). And every influence that moved
    is written to the disk log with its before and after weight, per vertex --
    Hammer on its own says nothing about WHICH joint was wrong, so the same
    bad joint gets to bleed somewhere else next week.

    Weight edits are pose-independent, so healing a POSED rig is fine and is
    usually the point: the pose is what made the bad vertex visible.
    """
    from maya import mel

    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "Select the vertices to heal first -- the ones deforming badly."
        )

    # The selected VERTICES name their own mesh, so the cluster comes from
    # them -- ``cmds.ls`` on a component selection is not a reliable route to
    # the shape, and the artist should not have to also select the mesh.
    if not cluster:
        for shape in sorted(grouped):
            cluster = skin_cluster_for(shape)
            if cluster:
                break
    if not cluster:
        raise SkinBenchError(
            "Those vertices belong to a mesh with no skinCluster -- nothing to heal."
        )

    driven = set(meshes_for_skin_cluster(cluster))
    fn = _skin_fn(cluster)
    names, _ = _influence_table(fn)

    report = {
        "skinCluster": cluster,
        "shapes": [], "snapshots": [],
        "unlocked": unlock_all_influences(cluster),      # gate 1: no silent skip
        "healed": 0, "unchanged": 0,
        "joints": {},                                    # leaf name -> vert count
        "examples": [],
        "ignoredShapes": sorted(s.split("|")[-1] for s in grouped if s not in driven),
    }

    # Read every BEFORE row first, then hammer once, then read the AFTERs.
    # Hammer works on the live selection, so it must not be interleaved with
    # anything that could disturb it.
    work = []
    for shape in sorted(grouped):
        if shape not in driven:
            continue                     # selection spilled onto another mesh
        vertices = sorted(set(grouped[shape]))
        mesh = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
        report["snapshots"].append(save_snapshot(mesh)[0])           # gate 2
        report["shapes"].append(shape.split("|")[-1])

        dag = _dag_path(shape)
        comp_fn = om.MFnSingleIndexedComponent()
        component = comp_fn.create(om.MFn.kMeshVertComponent)
        comp_fn.addElements(vertices)
        before, stride = fn.getWeights(dag, component)
        work.append((shape, vertices, dag, component, list(before), stride))

    if not work:
        raise SkinBenchError(
            "None of the selected vertices belong to '%s'." % cluster
        )

    selection = cmds.ls(selection=True, long=True)
    try:
        mel.eval("WeightHammer;")
    except RuntimeError:
        mel.eval("weightHammerVerts;")   # older Maya names the proc directly
    finally:
        cmds.select(selection, replace=True)

    for shape, vertices, dag, component, before, stride in work:
        after, _ = fn.getWeights(dag, component)
        leaf = shape.split("|")[-1]
        for index, vertex in enumerate(vertices):
            row = index * stride
            moves = _weight_moves(
                before[row:row + stride], after[row:row + stride], names)
            if not moves:
                report["unchanged"] += 1
                continue
            report["healed"] += 1
            # Only the movers that made the line. Counting the dust tail too
            # would list all 69 influences as suspects on a single vertex.
            for joint, _was, _now in moves[:HEAL_LINE_JOINTS]:
                report["joints"][joint] = report["joints"].get(joint, 0) + 1
            line = _heal_line(leaf, vertex, moves)
            write_log(line)                                          # gate 3
            if len(report["examples"]) < examples:
                report["examples"].append(line.strip())

    return report


def heal_selftest():
    """Assert the receipt names the right joint. Script Editor or mayapy."""
    names = ["|root|Hips", "|root|Hips|jnt_LeftToe04", "|root|Hips|Spine"]

    # Example: a toe joint holding paint on the chest, hammered away.
    # The toe moved most, so it must be reported FIRST -- that ordering is the
    # whole point of the receipt.
    moves = _weight_moves([0.30, 0.55, 0.15], [0.62, 0.00, 0.38], names)
    assert [move[0] for move in moves] == ["jnt_LeftToe04", "Hips", "Spine"], moves
    assert moves[0][1] == 0.55 and moves[0][2] == 0.0, moves[0]
    assert all("|" not in move[0] for move in moves), "log the leaf name only"

    # Rounding noise is not a fix. A vertex Hammer left alone must report
    # NOTHING, or every clean vertex would name an innocent joint.
    assert _weight_moves([0.5, 0.5, 0.0], [0.5, 0.5, 0.0], names) == []
    assert _weight_moves(
        [0.5, 0.5, 0.0], [0.50005, 0.49995, 0.0], names) == []
    assert len(_weight_moves(
        [0.5, 0.5, 0.0], [0.499, 0.501, 0.0], names)) == 2, "0.001 must count"

    # The log line stays readable. A production vertex had dust of 0.0029 on all
    # 69 influences; the honest full line named every one of them and could not
    # be read. The big movers are the answer, the tail is one summed phrase.
    short = [("A", 0.5, 0.0), ("B", 0.0, 0.4)]
    assert _heal_line("m", 7, short).count("->") == 2
    assert "more" not in _heal_line("m", 7, short)

    dust = short + [("J%d" % i, 0.0029, 0.0) for i in range(67)]
    line = _heal_line("m", 988, dust)
    assert line.count("->") == HEAL_LINE_JOINTS, line
    assert "+63 more, none above 0.0029" in line, line
    tiny = short + [("J%d" % i, 0.00004, 0.0) for i in range(3)]
    assert "none above 0.0001" in _heal_line("m", 9, tiny, limit=2)
    assert len(line) < 220, "a log line has to fit on a screen: %d" % len(line)

    print("SkinBench heal selftest OK")
    return True


def drag_selftest():
    """Assert the solver's answers and its three refusals. No scene needed."""
    # Two joints, one axis. The vertex can sit anywhere between where each
    # would put it, and the weight IS how far along that line it sits.
    line = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    for wanted in (0.0, 0.25, 0.5, 0.9, 1.0):
        weights, residual = solve_weights_for_point(
            line, (10.0 * wanted, 0.0, 0.0), [0.5, 0.5])
        assert residual < 1e-4, (wanted, residual)
        assert abs(weights[1] - wanted) < 1e-3, (wanted, weights)
        assert abs(sum(weights) - 1.0) < 1e-9, weights
        assert min(weights) >= 0.0, weights

    # Outside the reachable zone: the answer must be the NEAREST legal point,
    # and it must report a residual big enough for the caller to refuse on.
    weights, residual = solve_weights_for_point(
        line, (25.0, 0.0, 0.0), [0.5, 0.5])
    assert abs(residual - 15.0) < 1e-3, residual
    assert abs(_blend(line, weights)[0] - 10.0) < 1e-3, "clamp to the far end"

    # Off the line entirely -- it can only get as close as the line allows.
    weights, residual = solve_weights_for_point(
        line, (5.0, 4.0, 0.0), [0.5, 0.5])
    assert abs(residual - 4.0) < 1e-3, residual

    # Rest pose: every candidate in the same place. Any weights land in the
    # same spot, so the caller refuses on SPREAD, not on the residual -- the
    # residual here is zero and looks like success.
    rest = [(1.0, 2.0, 3.0)] * 4
    assert _spread(rest) == 0.0
    _, residual = solve_weights_for_point(rest, (1.0, 2.0, 3.0), [0.25] * 4)
    assert residual < 1e-9, "a rest pose fits perfectly and means nothing"

    # Ties are broken toward the paint already there, not toward joint 0.
    square = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    weights, _ = solve_weights_for_point(
        square, (5.0, 0.0, 0.0), [0.0, 0.0, 0.5, 0.5])
    assert weights[2] + weights[3] > weights[0] + weights[1], weights

    # Weights never negative, never sum past one -- the two rules a rig relies
    # on. Projection is what guarantees it, so test the projection directly.
    even = _project_to_simplex([0.9, 0.9, 0.9])
    assert all(abs(value - 1.0 / 3) < 1e-12 for value in even), even
    projected = _project_to_simplex([2.0, -5.0, 0.1])
    assert min(projected) >= 0.0 and abs(sum(projected) - 1.0) < 1e-12, projected

    # The joint cap is enforced by re-solving inside the survivors, so the
    # capped answer must still be a legal set of weights AND the best one
    # available from those joints -- not a trimmed copy of a wider answer.
    three = line + [(5.0, 2.0, 0.0)]
    capped, residual = solve_within(three, (5.0, 1.0, 0.0), [0.34, 0.33, 0.33],
                                    slots=[0, 2])
    assert capped[1] == 0.0, "a slot outside the set must stay at zero"
    assert abs(sum(capped) - 1.0) < 1e-9, capped
    # Hand-checked: nearest point on the segment (0,0,0)->(5,2,0) to (5,1,0)
    # is (4.655, 1.862), which is 0.9285 away. The solver must find exactly
    # that -- a looser bound here would pass a solver that stopped early.
    assert abs(residual - 0.9285) < 0.002, residual
    try:
        solve_within(three, (1.0, 0.0, 0.0), [0.34, 0.33, 0.33], slots=[])
    except SkinBenchError:
        pass
    else:
        raise AssertionError("solving with no influences must refuse")

    print("SkinBench drag selftest OK")
    return True


# ---------------------------------------------------------------------------
# drag-solve: move the vertex where it belongs, work out the weights that hold
# it there
# ---------------------------------------------------------------------------
#
# Prior art, read before this was written: SSDR -- Smooth Skinning Decomposition
# with Rigid Bones, Le & Deng, SIGGRAPH Asia 2012 -- already solves "fit weights
# so the skin matches example shapes", with the same three constraints (weights
# never negative, always sum to one, capped influence count). SSDR solves for
# the BONES as well and fits a whole animation; this solves one vertex in one
# pose with the skeleton fixed, which is the small corner of the same issue.
# Related: Pose Space Deformation, Lewis et al. 2000.
#
# The geometry, in one paragraph. In a pose, each joint would put this vertex
# somewhere -- call those the candidate points, one per influence. Skinning
# places the vertex at a weighted average of them. So every position the vertex
# can possibly reach is inside the shape those candidate points span, and
# "which weights put it here" is "which mix of the candidates makes this
# point". Drag outside that shape and no weighting on earth reaches it.

# How much of the drag the answer has to actually deliver, 0 to 1.
#
# Measured during live testing, and it corrected the first design: the set of places a
# vertex can reach is SMALL. With two influences it is a line segment; with
# four, a tetrahedron. A hand drag in 3D will nearly always land a little off
# it, so scoring the fit against the size of the rig refused almost everything.
# What matters is how much of the move the weights can deliver: land 80% of the
# way and that is a good fix, land 0% and no weighting goes that way at all.
DRAG_MIN_GAIN = 0.5

# Below this the candidate points are all in the same place -- a rest pose --
# and no weighting can move the vertex anywhere. Also relative.
DRAG_REST_SPREAD = 1e-4

# How hard the answer is pulled back toward the paint already there. Many
# weight mixes put the vertex in the same spot; this picks the one that changes
# the least, so a solve does not quietly re-author a vertex it happened to fix.
DRAG_STAY_NEAR = 1e-3


def _project_to_simplex(values):
    """Nearest point with no negatives that still sums to one."""
    ordered = sorted(values, reverse=True)
    total, threshold, count = 0.0, 0.0, 0
    for index, value in enumerate(ordered):
        total += value
        candidate = (total - 1.0) / (index + 1)
        if value - candidate > 0:
            threshold, count = candidate, index + 1
    if not count:                       # degenerate input: spread it evenly
        return [1.0 / len(values)] * len(values)
    return [max(0.0, value - threshold) for value in values]


def solve_weights_for_point(candidates, target, start, stay_near=DRAG_STAY_NEAR,
                            iterations=400):
    """Weights that put the vertex at ``target``. Pure maths -- no scene.

    ``candidates`` is where each influence would put this vertex on its own;
    ``start`` is the paint already on it. Returns ``(weights, residual)``,
    where residual is how far the answer actually lands from the target.

    Frank-Wolfe: every step stays a legal set of weights (no negatives, sums to
    one) so there is no way to end up with an illegal answer, and it favours
    few influences on its own because each step pulls toward a single joint.
    """
    count = len(candidates)
    if count != len(start):
        raise SkinBenchError("candidate and weight counts disagree.")
    weights = _project_to_simplex(list(start))

    def at(current):
        return [sum(current[j] * candidates[j][axis] for j in range(count))
                for axis in range(3)]

    for _ in range(iterations):
        here = at(weights)
        error = [here[axis] - target[axis] for axis in range(3)]
        gradient = [
            2.0 * sum(error[axis] * candidates[j][axis] for axis in range(3))
            + 2.0 * stay_near * (weights[j] - start[j])
            for j in range(count)
        ]
        best = min(range(count), key=lambda j: gradient[j])
        step_dir = [-w for w in weights]
        step_dir[best] += 1.0

        slope = sum(gradient[j] * step_dir[j] for j in range(count))
        if slope >= -1e-14:
            break                        # already the best legal answer
        moved = [sum(step_dir[j] * candidates[j][axis] for j in range(count))
                 for axis in range(3)]
        curve = (sum(value * value for value in moved)
                 + stay_near * sum(value * value for value in step_dir))
        gamma = 1.0 if curve <= 1e-18 else min(1.0, max(0.0, -slope / (2.0 * curve)))
        if gamma <= 1e-12:
            break
        weights = [weights[j] + gamma * step_dir[j] for j in range(count)]

    landed = at(weights)
    residual = _distance(landed, target)
    return weights, residual


def solve_within(candidates, target, start, slots, **kwargs):
    """Solve using ONLY these influence slots. Returns full-length weights.

    The cap on how many joints may hold a vertex is a hard rule, not a
    preference: rigs declare it (one production rig says four) and game engines enforce
    it. Solving freely and trimming afterwards breaks the fit it just found, so
    the trimming happens by re-solving inside the smaller set instead.
    """
    slots = sorted(slots)
    if not slots:
        raise SkinBenchError("No influences left to solve with.")
    sub_start = [start[slot] for slot in slots]
    total = sum(sub_start)
    sub_start = ([value / total for value in sub_start] if total > 1e-9
                 else [1.0 / len(slots)] * len(slots))
    sub, residual = solve_weights_for_point(
        [candidates[slot] for slot in slots], target, sub_start, **kwargs)
    weights = [0.0] * len(candidates)
    for position, slot in enumerate(slots):
        weights[slot] = sub[position]
    return weights, residual


def _spread(points):
    """How far apart the candidate points are: the scale of what is possible."""
    widest = 0.0
    for index, point in enumerate(points):
        for other in points[index + 1:]:
            widest = max(widest, _distance(point, other))
    return widest


def _geom_matrix(cluster):
    """Where the geometry stood when it was bound. Identity if unrecorded."""
    for plug in ("%s.geomMatrix" % cluster, "%s.geomMatrix[0]" % cluster):
        try:
            return om.MMatrix(cmds.getAttr(plug))
        except (ValueError, RuntimeError):
            continue
    return om.MMatrix()


def _influence_matrices(cluster, shape, names):
    """One matrix per influence: local rest point in, world position out.

    ``geomMatrix`` is where the mesh stood when it was bound, and leaving it
    out is a silent, invisible bug. On a mesh sitting at the origin at scale 1
    it is the identity and changes nothing -- which is why a throwaway test
    plane could never catch it. An asymmetrical rig was bound at 150% scale,
    and without this the predicted position of every vertex was 12 to 120
    units wrong during live testing. Measured with it: worst error 0.000000.
    """
    geom = _geom_matrix(cluster)
    # geomMatrix in, geomMatrix out, then wherever the transform is NOW -- so a
    # mesh whose transform has moved since bind is still handled.
    tail = geom.inverse() * om.MMatrix(cmds.getAttr(shape + ".worldMatrix[0]"))
    out = []
    for name in names:
        index = _bind_pre_matrix_index(cluster, name)
        pre = om.MMatrix(cmds.getAttr("%s.bindPreMatrix[%d]" % (cluster, index)))
        joint = om.MMatrix(cmds.getAttr(name + ".worldMatrix[0]"))
        out.append(geom * pre * joint * tail)
    return out


def _candidate_points(matrices, rest_point):
    """Where each influence, alone, would put this vertex right now (world)."""
    point = om.MPoint(rest_point[0], rest_point[1], rest_point[2])
    placed = [point * matrix for matrix in matrices]
    return [(p.x, p.y, p.z) for p in placed]


def _blend(candidates, weights):
    """Where skinning actually puts the vertex for these weights."""
    return tuple(
        sum(weights[j] * candidates[j][axis] for j in range(len(weights)))
        for axis in range(3)
    )


# What "drag along the skin" is called inside Maya. The plain words are the
# label; these are the values cmds.xformConstraint understands.
DRAG_CONSTRAINTS = {
    "free": "none",
    "surface": "surface",
    "edge": "edge",
}


def ready_to_drag(constraint="surface", cluster=None):
    """Put Maya in the state where dragging a vertex is possible, in one press.

    The Move tool's *Transform Constraint* is the setting that makes a vertex
    slide along the skin instead of flying off into space -- and it lives in
    the Tool Settings panel, which shows whichever tool is CURRENTLY active.
    So from inside the paint tool, where a vertex cannot be moved at all, the
    setting the artist wants is not merely buried: it is not on screen.

    Sets the constraint, switches to the Move tool, and drops the mesh into
    vertex mode. Reads the constraint back afterwards, because Maya accepts
    values it then ignores -- ``live`` sets silently and stays ``none``.
    """
    from maya import mel

    wanted = DRAG_CONSTRAINTS.get(constraint)
    if wanted is None:
        raise SkinBenchError("'%s' is not a drag mode." % constraint)

    cluster = cluster or _one_cluster_from_selection()
    shapes = meshes_for_skin_cluster(cluster)
    if not shapes:
        raise SkinBenchError("'%s' drives no mesh." % cluster)
    mesh = cmds.listRelatives(shapes[0], parent=True, fullPath=True)[0]

    # Untestable headless: Maya's moveJointsMode query returns None in batch,
    # so this guard reads False there and never fires. It is live in the GUI,
    # which is the only place a vertex can be dragged anyway.
    if move_joints_mode(cluster):
        raise SkinBenchError(
            "'%s' is UNLOCKED for moving joints — the mesh is frozen, so "
            "dragging a vertex would not mean anything. Press 'Lock in' on the "
            "Move Joints tab first." % cluster
        )

    cmds.xformConstraint(type=wanted)
    cmds.select(mesh, replace=True)
    cmds.selectMode(component=True)
    cmds.selectType(vertex=True)
    cmds.hilite(mesh)
    mel.eval("setToolTo moveSuperContext;")

    landed = cmds.xformConstraint(query=True, type=True)
    return {"mesh": mesh.split("|")[-1], "asked": wanted, "constraint": landed,
            "honoured": landed == wanted}


def _nearby_influences(shape, vertex, flat, stride):
    """Which joints are allowed to hold this vertex: its own, and its neighbours'.

    Anatomy is not in the arithmetic. To the solver a toe joint is just another
    direction to push in, so left free it will happily hold an arm vertex with
    a toe if the numbers work out. The neighbouring vertices already encode
    which joints belong on this part of the body, so they are the fence.
    """
    ids = _one_ring(shape, [vertex]) | {vertex}
    allowed = set()
    for other in ids:
        row = other * stride
        for slot in range(stride):
            if flat[row + slot] > WEIGHT_EPSILON:
                allowed.add(slot)
    if not allowed:                       # unweighted island: nothing to fence
        allowed = set(range(stride))
    return allowed


def _orig_points(shape):
    """The mesh BEFORE any deformer -- the rest shape the skinning starts from."""
    plugs = cmds.deformableShape(shape, originalGeometry=True) or []
    if not plugs or not plugs[0]:
        raise SkinBenchError("'%s' has no original geometry to read." % shape)
    orig = plugs[0].split(".")[0]
    return om.MFnMesh(_dag_path(orig)).getPoints(om.MSpace.kObject)


def _model_size(shape):
    """The largest dimension of the mesh, for turning ratios into distances."""
    box = cmds.exactWorldBoundingBox(shape)
    return max(box[3] - box[0], box[4] - box[1], box[5] - box[2], 1.0)


# The smallest move that counts as a drag, as a fraction of the model's size.
# During live testing, the drag test used WEIGHT_EPSILON (0.0000001) -- a
# tolerance for weight VALUES, borrowed for a DISTANCE. On a scaled rig that let leftover
# rounding register as a drag, and the "how much of your move did we deliver"
# sum then divided by almost nothing and printed the nonsense
# "0% of that move, stopping 0.000 short". Below this, a vertex has not been
# moved in any sense a person could see.
DRAG_MIN_MOVE = 0.0002


def _dragged_vertices(shape):
    """``{vertex: offset}`` for every vertex carrying a hand drag on this shape.

    Measured during live testing: on a skinned mesh with no tweak node, dragging a
    vertex writes to the shape's own ``pnts``, and that offset is applied
    AFTER the skinning -- rotate the joint that owns the vertex and the offset
    does not follow it. So ``pnts`` is a clean read of "where the artist moved
    this, in the pose", and clearing it is how the fix moves into the weights.
    """
    floor = _model_size(shape) * DRAG_MIN_MOVE
    found = {}
    for index in cmds.getAttr(shape + ".pnts", multiIndices=True) or []:
        offset = cmds.getAttr("%s.pnts[%d]" % (shape, index))[0]
        if max(abs(value) for value in offset) > floor:
            found[index] = tuple(offset)
    return found


def leftover_drag_report():
    """Count real point offsets that can survive a save or export.

    This is deliberately read-only and walks only skinned output shapes. Maya's
    ``pnts`` array is sparse, so the cost follows the number of stored tweaks,
    not the number of vertices on the character.
    """
    report = {"vertices": 0, "shapes": [], "skipped": []}
    seen = set()
    for cluster in sorted(all_skin_clusters()):
        for shape in sorted(meshes_for_skin_cluster(cluster)):
            if shape in seen:
                continue
            seen.add(shape)
            short_name = shape.split("|")[-1]
            try:
                shape_type = cmds.nodeType(shape)
            except (RuntimeError, TypeError, ValueError):
                report["skipped"].append({
                    "shape": short_name,
                    "reason": "shape type unavailable",
                })
                continue
            if shape_type != "mesh":
                report["skipped"].append({
                    "shape": short_name,
                    "reason": "not a polygon mesh",
                })
                continue
            count = 0
            try:
                for index in cmds.getAttr(shape + ".pnts", multiIndices=True) or []:
                    offset = cmds.getAttr("%s.pnts[%d]" % (shape, index))[0]
                    if max(abs(value) for value in offset) > WEIGHT_EPSILON:
                        count += 1
            except (RuntimeError, TypeError, ValueError):
                report["skipped"].append({
                    "shape": short_name,
                    "reason": "point offsets unavailable",
                })
                continue
            if count:
                report["vertices"] += count
                report["shapes"].append({
                    "shape": short_name,
                    "vertices": count,
                })
    return report


def clear_drags(cluster=None):
    """Undo every hand drag still sitting on the mesh, solved or not.

    A refused drag STAYS -- deliberately, so the vertex is still where you put
    it and you can try a shorter move. The hazard is that it is a real dent in
    the model, not a preview: it survives a save and it exports. This is the
    broom for the ones you gave up on.

    Only the drag is removed. Weights are untouched.
    """
    clusters = [cluster] if cluster else skin_clusters_from_selection()
    scene_wide = not clusters
    if scene_wide:
        clusters = all_skin_clusters()
    scoped = _selected_vertices_by_shape()

    report = {
        "shapes": [],
        "cleared": 0,
        "skipped": [],
        "scope": "scene" if scene_wide else "selection",
    }
    seen = set()
    planned = []
    for cluster in clusters:
        for shape in meshes_for_skin_cluster(cluster):
            if shape in seen:
                continue
            seen.add(shape)
            short_name = shape.split("|")[-1]
            try:
                shape_type = cmds.nodeType(shape)
            except (RuntimeError, TypeError, ValueError):
                report["skipped"].append({
                    "shape": short_name,
                    "reason": "shape type unavailable",
                })
                continue
            if shape_type != "mesh":
                report["skipped"].append({
                    "shape": short_name,
                    "reason": "not a polygon mesh",
                })
                continue
            # Every offset, including the ones too small for the solver to
            # bother with -- a crumb still exports.
            try:
                dragged = {}
                for index in cmds.getAttr(shape + ".pnts", multiIndices=True) or []:
                    offset = tuple(cmds.getAttr(
                        "%s.pnts[%d]" % (shape, index)
                    )[0])
                    if max(abs(value) for value in offset) > WEIGHT_EPSILON:
                        dragged[index] = offset
            except (RuntimeError, TypeError, ValueError):
                report["skipped"].append({
                    "shape": short_name,
                    "reason": "point offsets unavailable",
                })
                continue
            if scoped.get(shape):
                wanted = set(scoped[shape])
                dragged = {v: o for v, o in dragged.items() if v in wanted}
            if not dragged:
                continue
            planned.append((shape, short_name, sorted(dragged)))

    if planned:
        cmds.undoInfo(openChunk=True, chunkName="SkinBench clear drags")
        try:
            for shape, short_name, vertices in planned:
                for vertex in vertices:
                    cmds.setAttr("%s.pnts[%d]" % (shape, vertex), 0, 0, 0,
                                 type="double3")
                report["shapes"].append(short_name)
                report["cleared"] += len(vertices)
        finally:
            cmds.undoInfo(closeChunk=True)
    return report


def solve_dragged_vertices(cluster=None, max_influences=4, dry_run=True):
    """Read where you dragged each vertex, and paint the weights that hold it.

    The loop this is for: pose the rig (Poses tab), find the vertex sitting in
    the wrong place, drag it with the Move tool to where it belongs, press
    this. The drag is the question; the weights are the answer.

    Three honest refusals, because each one is a case where a solver could
    invent a plausible answer instead of admitting it has none:

    * **At rest pose** every joint puts the vertex in the same spot, so no
      weighting can move it anywhere. Refused, by name, rather than appearing
      to run and doing nothing.
    * **Outside the reachable zone.** A vertex can only sit inside the shape
      the candidate points span. Dragged past that, the nearest reachable
      point is reported and NOTHING is written -- a "close enough" fit written
      silently is the failure mode worth designing out.
    * **The model has to check out first.** Before solving anything, the same
      arithmetic is run on vertices you did not drag; if it cannot reproduce
      where they already sit, the assumption is wrong on this rig and the
      whole run refuses.

    ``dry_run`` reports without writing. The per-joint share is always logged,
    so the answer can be sanity-checked before it is trusted.
    """
    clusters = [cluster] if cluster else skin_clusters_from_selection()
    if not clusters:
        clusters = all_skin_clusters()
    scoped = _selected_vertices_by_shape()

    report = {"dry_run": dry_run, "shapes": [], "snapshots": [],
              "solved": [], "refused": [], "written": 0}

    for cluster in clusters:
        fn = _skin_fn(cluster)
        names, _ = _influence_table(fn)

        for shape in meshes_for_skin_cluster(cluster):
            # Per shape, not per cluster: the matrices carry this mesh's own
            # bind-time and current transforms.
            matrices = _influence_matrices(cluster, shape, names)
            dragged = _dragged_vertices(shape)
            if scoped.get(shape):
                wanted = set(scoped[shape])
                dragged = {v: o for v, o in dragged.items() if v in wanted}
            if not dragged:
                continue

            rest = _orig_points(shape)
            live = om.MFnMesh(_dag_path(shape)).getPoints(om.MSpace.kWorld)
            dag = _dag_path(shape)
            count = cmds.polyEvaluate(shape, vertex=True)
            flat, stride = fn.getWeights(dag, _complete_vertex_component(count))

            _check_skin_model(shape, matrices, rest, live, flat, stride,
                              skip=set(dragged))

            leaf = shape.split("|")[-1]
            report["shapes"].append(leaf)
            solved_here = {}

            for vertex in sorted(dragged):
                target = (live[vertex].x, live[vertex].y, live[vertex].z)
                candidates = _candidate_points(
                    matrices, (rest[vertex].x, rest[vertex].y, rest[vertex].z))
                spread = _spread(candidates)
                row = vertex * stride
                start = list(flat[row:row + stride])

                # How far the drag asked the vertex to travel, from where the
                # weights currently put it. This is the yardstick for the fit.
                asked = _distance(target, _blend(candidates, start))
                if spread <= DRAG_REST_SPREAD * max(asked, 1.0):
                    report["refused"].append({
                        "shape": leaf, "vertex": vertex, "why": "rest",
                        "message": "The rig is at REST here — every joint puts "
                                   "this vertex in the same place, so no "
                                   "weighting can move it. Pose the rig first.",
                    })
                    continue

                # Only joints that already hold this vertex or one of its
                # neighbours may be used. Without this the solver finds exact
                # fits made of nonsense -- during live testing it put a toe joint on an
                # arm vertex, because a toe joint is a direction like any other
                # once anatomy is not in the maths.
                allowed = _nearby_influences(shape, vertex, flat, stride)
                weights, residual = solve_within(
                    candidates, target, start, allowed)

                # The joint cap is hard, so it is applied by RE-SOLVING inside
                # the surviving joints, not by trimming a finished answer.
                keep = sorted(range(stride), key=lambda j: -weights[j])
                keep = [j for j in keep if weights[j] > WEIGHT_EPSILON]
                if len(keep) > max_influences:
                    weights, residual = solve_within(
                        candidates, target, start, keep[:max_influences])

                gain = 0.0 if asked <= 1e-9 else max(0.0, (asked - residual) / asked)
                if gain < DRAG_MIN_GAIN:
                    nearest = _blend(candidates, weights)
                    report["refused"].append({
                        "shape": leaf, "vertex": vertex, "why": "unreachable",
                        "short": residual, "gain": gain, "nearest": nearest,
                        "message": "No weighting takes it there — the best any "
                                   "mix of these joints can do is %.0f%% of "
                                   "that move, stopping %.3f short at (%.3f, "
                                   "%.3f, %.3f). Nothing written."
                                   % ((gain * 100, residual) + nearest),
                    })
                    continue

                share = sorted(
                    ((names[j].split("|")[-1], weights[j])
                     for j in range(stride) if weights[j] > WEIGHT_EPSILON),
                    key=lambda pair: -pair[1])
                report["solved"].append({
                    "shape": leaf, "vertex": vertex, "residual": residual,
                    "asked": asked, "gain": gain, "spread": spread,
                    "share": share,
                })
                solved_here[vertex] = weights

            if not solved_here or dry_run:
                continue

            mesh = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
            report["snapshots"].append(save_snapshot(mesh)[0])

            vertices = sorted(solved_here)
            comp_fn = om.MFnSingleIndexedComponent()
            component = comp_fn.create(om.MFn.kMeshVertComponent)
            comp_fn.addElements(vertices)
            values = om.MDoubleArray()
            for vertex in vertices:
                for value in solved_here[vertex]:
                    values.append(value)
            _set_weights(fn, dag, component, values, len(vertices))

            for vertex in vertices:                    # the drag moves into the
                cmds.setAttr("%s.pnts[%d]" % (shape, vertex), 0, 0, 0,  # weights
                             type="double3")
            report["written"] += len(vertices)

            # Proof, measured after the write: is the vertex still where it was
            # put, now that only the weights are holding it there?
            moved = om.MFnMesh(_dag_path(shape)).getPoints(om.MSpace.kWorld)
            for row in report["solved"]:
                if row["shape"] == leaf and row["vertex"] in solved_here:
                    vertex = row["vertex"]
                    row["landed"] = _distance(
                        (moved[vertex].x, moved[vertex].y, moved[vertex].z),
                        (live[vertex].x, live[vertex].y, live[vertex].z))

    if not report["solved"] and not report["refused"]:
        raise SkinBenchError(
            "No dragged vertices found. Pose the rig, move a vertex with the "
            "Move tool to where it should sit, then press this."
        )
    return report


def _check_skin_model(shape, matrices, rest, live, flat, stride, skip,
                      samples=24, tolerance=1e-3):
    """Refuse the whole run if the arithmetic cannot explain UNDRAGGED verts.

    The solver assumes a vertex sits at the weighted average of where each
    joint would put it. That is true for plain smooth skinning and false the
    moment something else is in the chain -- dual quaternion skinning, a
    blendshape, a second deformer, a post-skin tweak. Rather than trusting the
    assumption, it is measured here against vertices nobody touched.
    """
    checked, worst, where = 0, 0.0, None
    total = len(rest)
    step = max(1, total // samples)
    for vertex in range(0, total, step):
        if vertex in skip:
            continue
        row = vertex * stride
        weights = flat[row:row + stride]
        if sum(weights) < 0.5:
            continue                     # unweighted vertex proves nothing
        predicted = _blend(
            _candidate_points(matrices,
                              (rest[vertex].x, rest[vertex].y, rest[vertex].z)),
            weights)
        gap = _distance(predicted,
                        (live[vertex].x, live[vertex].y, live[vertex].z))
        if gap > worst:
            worst, where = gap, vertex
        checked += 1
        if checked >= samples:
            break

    if not checked:
        raise SkinBenchError(
            "'%s' has no ordinary weighted vertices to check the maths against."
            % shape.split("|")[-1])
    scale = max(_spread([(p.x, p.y, p.z) for p in live[:200]]), 1.0)
    if worst > tolerance * scale:
        raise SkinBenchError(
            "The maths does not match this mesh -- predicted vertex %d is "
            "%.4f away from where it really is. Something other than plain "
            "smooth skinning is moving '%s' (dual quaternion skinning, a "
            "blendshape, or a second deformer). Refusing to solve weights on "
            "a mesh whose deformation is not understood."
            % (where, worst, shape.split("|")[-1]))


# ---------------------------------------------------------------------------
# patch isolate: keep the one island of paint that belongs, delete the rest
# ---------------------------------------------------------------------------

# How near a vertex has to be, in the joint's own bone lengths, for its island
# to count as the joint's real patch. 1.0 is deliberately tight: this asks "does
# this island TOUCH the joint", not "is this island roughly nearby".
PATCH_KEEP_RADIUS = 1.0

# A doomed vertex where the joint is the DOMINANT owner is only zeroed if the
# other influences already hold at least this much between them. Below that,
# renormalizing would blow one survivor up from a rounding error to near 1.0 and
# invent a deformation nobody painted. Those vertices are reported for a human
# instead of being written.
PATCH_RESCUE_FLOOR = 0.2


def _vertex_adjacency(dag):
    """``{vertex: set(neighbours)}`` from the mesh's edges."""
    adjacency = {}
    iterator = om.MItMeshEdge(dag)
    while not iterator.isDone():
        first, second = iterator.vertexId(0), iterator.vertexId(1)
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)
        iterator.next()
    return adjacency


def _islands(members, adjacency):
    """Connected groups within ``members``, walking only edges between members.

    Pure: hand it a dict and a set and it can be asserted with no scene. That
    matters, because the island rule is the whole feature -- distance says "far
    from the joint", connectivity says "not attached to the joint's patch", and
    only the second one can tell a long-jaw rig's snout from a shoulder smear.
    """
    remaining = set(members)
    found = []
    while remaining:
        island = {remaining.pop()}
        stack = list(island)
        while stack:
            current = stack.pop()
            for neighbour in adjacency.get(current, ()):
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    island.add(neighbour)
                    stack.append(neighbour)
        found.append(island)
    return sorted(found, key=len, reverse=True)


def patch_islands(joints=None, cluster=None, link_floor=BLEED_FLOOR,
                  keep_radius=PATCH_KEEP_RADIUS):
    """Find a joint's HOME MASS and call everything not joined to it a candidate.

    This is a *selection* service. It writes nothing and it selects nothing --
    ``select_outside_patch`` does the selecting, ``zero_patch_selection`` does
    the writing, and between those two the artist is free to edit the selection
    by hand. That order is the whole design: the selection is the product.

    The question it answers is not "how far away is this weight" (that is
    ``strip_bleed``) but **"is this weight even joined to the lump of paint
    where the joint genuinely works"**. A smear of ``jaw_joint`` on an
    asymmetrical rig's shoulder is not far enough to trip even a tight N, yet there is no
    path across the mesh from it back to the snout. Light or heavy stops
    mattering; attachment is what counts.

    How the home mass is chosen:

    * every vertex carrying any of the joint's weight is in scope, **dust
      included** -- clearing the dust is half the point;
    * two vertices share an island only if they share a mesh EDGE and both
      carry at least ``link_floor`` ("stickiness"). Dust is never a link, so a
      trail of 0.001 weights cannot bridge a smear back to the home mass;
    * each island is scored by its total weight MASS, and asked whether any of
      its vertices sits within ``keep_radius`` ("home reach") of the joint, in
      that joint's own bone lengths;
    * **home = the heaviest island that also reaches the joint.**

    When the heaviest island of all is NOT the one at the joint, that is
    reported as a disagreement rather than silently resolved. It means the
    joint's main body of paint has ended up somewhere it does not belong, which
    is a rig issue worth a human's eyes, not something to quietly delete.
    """
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if not joints:
        raise SkinBenchError("Pick the joint whose home patch you want to keep.")

    cluster = cluster or _one_cluster_from_selection()
    shapes = meshes_for_skin_cluster(cluster)
    if not shapes:
        raise SkinBenchError("'%s' drives no mesh." % cluster)
    shape = shapes[0]
    dag = _dag_path(shape)

    fn = _skin_fn(cluster)
    names, _ = _influence_table(fn)
    asked = set(joints) | {name.split("|")[-1] for name in joints}
    wanted = [
        slot for slot, name in enumerate(names)
        if name in asked or name.split("|")[-1] in asked
    ]
    if not wanted:
        raise SkinBenchError(
            "None of those joints influence '%s' -- right joint, wrong mesh?" % cluster
        )
    who = ", ".join(names[slot].split("|")[-1] for slot in wanted)

    posed = posed_joints(names)
    if posed:
        raise SkinBenchError(
            "The rig is POSED -- %s, so the home patch would be measured "
            "against the pose, not the rig. Zero the pose (Poses tab), end the "
            "ROM sweep (skinBenchROM -r), or Ctrl+Z back to rest first."
            % describe_posed(posed)
        )

    # "Near the joint" needs a ruler, and guessing one is the mistake this whole
    # tool exists to stop.
    rulers = {}
    for slot in wanted:
        length = bone_length(names[slot])
        if length is not None:
            rulers[slot] = (length, _world_point(names[slot]))
    if not rulers:
        raise SkinBenchError(
            "No measurable bone on %s, so 'near the joint' has no meaning here. "
            "This joint has no parent joint and no joint children." % who
        )

    vertex_count = cmds.polyEvaluate(shape, vertex=True)
    flat, stride = fn.getWeights(dag, _complete_vertex_component(vertex_count))
    points = om.MFnMesh(dag).getPoints(om.MSpace.kWorld)

    carried, linkable = {}, set()
    for vertex in range(vertex_count):
        row = vertex * stride
        held = sum(flat[row + slot] for slot in wanted)
        if held <= WEIGHT_EPSILON:
            continue
        carried[vertex] = held
        if held >= link_floor:
            linkable.add(vertex)

    if not carried:
        raise SkinBenchError(
            "%s holds no weight on '%s' -- nothing to clean."
            % (who, shape.split("|")[-1])
        )

    adjacency = _vertex_adjacency(dag)
    islands = _islands(linkable, adjacency)

    scored = []
    for island in islands:
        near = False
        for vertex in island:
            point = points[vertex]
            for length, origin in rulers.values():
                if _distance((point.x, point.y, point.z), origin) <= keep_radius * length:
                    near = True
                    break
            if near:
                break
        scored.append({
            "vertices": island,
            "mass": sum(carried[vertex] for vertex in island),
            "near": near,
        })

    at_joint = [entry for entry in scored if entry["near"]]
    if not at_joint:
        raise SkinBenchError(
            "No island of %s's paint reaches within %g bone length(s) of the "
            "joint, so there is no home patch to keep and this would erase the "
            "joint entirely. That is a structural issue -- wrong joint, wrong "
            "mesh, or the patch was never painted -- not something to clean."
            % (who, keep_radius)
        )

    home = max(at_joint, key=lambda entry: entry["mass"])
    heaviest = max(scored, key=lambda entry: entry["mass"])
    disagreement = heaviest is not home and heaviest["mass"] > home["mass"]

    keep = home["vertices"]
    other_near = sum(len(entry["vertices"]) for entry in at_joint
                     if entry is not home)

    doomed, needs_repaint, dust_hit, removed = [], [], 0, 0.0
    for vertex, held in carried.items():
        if vertex in keep:
            continue
        row = vertex * stride
        best_slot, best_weight = None, 0.0
        for slot in range(stride):
            if flat[row + slot] > best_weight:
                best_slot, best_weight = slot, flat[row + slot]
        others = sum(flat[row + slot] for slot in range(stride) if slot not in wanted)
        if best_slot in wanted and others < PATCH_RESCUE_FLOOR:
            needs_repaint.append(vertex)
            continue
        doomed.append(vertex)
        removed += held
        if held < link_floor:
            dust_hit += 1

    return {
        "skinCluster": cluster,
        "shape": shape,
        "joints": [names[slot].split("|")[-1] for slot in wanted],
        "linkFloor": link_floor,
        "keepRadius": keep_radius,
        "carrying": len(carried),
        "islands": len(islands),
        "homeVertices": sorted(keep),
        "homeMass": home["mass"],
        "heaviestMass": heaviest["mass"],
        "disagreement": disagreement,
        "otherNearVertices": other_near,
        "candidates": sorted(doomed),
        "candidateIslands": len(islands) - 1,
        "dust": dust_hit,
        "weightRemoved": removed,
        "needsRepaint": sorted(needs_repaint),
    }


def select_outside_patch(joints=None, cluster=None, link_floor=BLEED_FLOOR,
                         keep_radius=PATCH_KEEP_RADIUS):
    """``patch_islands`` plus: select the candidates so they can be eyeballed.

    Selection is the product. Everything Maya can do to a vertex selection --
    Shift+drag to add, Ctrl+drag to remove, SkinBench's own Grow and Shrink --
    applies before any write, and the write consumes whatever survives that.
    """
    report = patch_islands(joints=joints, cluster=cluster,
                           link_floor=link_floor, keep_radius=keep_radius)
    if report["candidates"]:
        select_vertices(report["skinCluster"], report["candidates"])
    return report


def zero_patch_selection(joints=None, cluster=None):
    """Zero these joints on the vertices SELECTED RIGHT NOW, and renormalize.

    It deliberately does NOT recompute the islands. What is on screen is what
    gets written -- including every hand edit made since Preview -- because a
    write that silently re-derives its own target makes the preview a lie.

    Same gates as Strip: snapshot first, renormalize the survivors, and refuse
    to empty a vertex. Plus the home-patch rescue rule: a vertex this joint
    OWNS with less than ``PATCH_RESCUE_FLOOR`` held by everything else is left
    alone and reported, because renormalizing there would promote a rounding
    error to near 1.0 and invent a deformation nobody painted.
    """
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if not joints:
        raise SkinBenchError("Pick the joint whose paint you are deleting.")

    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "No vertices are selected. Run Preview islands first, edit the "
            "selection if you want to, then press this."
        )

    cluster = cluster or _one_cluster_from_selection()
    driven = set(meshes_for_skin_cluster(cluster))
    fn = _skin_fn(cluster)
    names, _ = _influence_table(fn)
    asked = set(joints) | {name.split("|")[-1] for name in joints}
    wanted = [
        slot for slot, name in enumerate(names)
        if name in asked or name.split("|")[-1] in asked
    ]
    if not wanted:
        raise SkinBenchError(
            "None of those joints influence '%s' -- right joint, wrong mesh?" % cluster
        )

    posed = posed_joints(names)
    if posed:
        raise SkinBenchError(
            "The rig is POSED -- %s. Zero the pose (Poses tab), end the ROM "
            "sweep (skinBenchROM -r), or Ctrl+Z back to rest first."
            % describe_posed(posed)
        )

    report = {
        "skinCluster": cluster,
        "joints": [names[slot].split("|")[-1] for slot in wanted],
        "shapes": [], "snapshots": [],
        "selected": sum(len(ids) for ids in grouped.values()),
        "zeroed": 0, "untouched": 0, "needsRepaint": [],
        "weightRemoved": 0.0,
        "ignoredShapes": sorted(s.split("|")[-1] for s in grouped if s not in driven),
    }

    for shape in sorted(grouped):
        if shape not in driven:
            continue
        vertices = sorted(set(grouped[shape]))
        mesh = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
        report["snapshots"].append(save_snapshot(mesh)[0])          # gate 1
        report["shapes"].append(shape.split("|")[-1])

        dag = _dag_path(shape)
        comp_fn = om.MFnSingleIndexedComponent()
        component = comp_fn.create(om.MFn.kMeshVertComponent)
        comp_fn.addElements(vertices)
        flat, stride = fn.getWeights(dag, component)
        updated = om.MDoubleArray(flat)

        writable = []
        for index, vertex in enumerate(vertices):
            row = index * stride
            here = flat[row:row + stride]
            held = sum(here[slot] for slot in wanted)
            if held <= WEIGHT_EPSILON:
                report["untouched"] += 1
                continue
            others = sum(here[slot] for slot in range(stride) if slot not in wanted)
            dominant = max(range(stride), key=lambda slot: here[slot]) in wanted
            if dominant and others < PATCH_RESCUE_FLOOR:            # gate 2
                report["needsRepaint"].append(vertex)
                continue
            for slot, value in enumerate(_renormalized(here, wanted)):  # gate 3
                updated[row + slot] = value
            report["weightRemoved"] += held
            writable.append(vertex)

        if writable:
            _set_weights(fn, dag, component, updated, len(vertices))
            report["zeroed"] += len(writable)

    if not report["shapes"]:
        raise SkinBenchError(
            "None of the selected vertices belong to '%s'." % cluster
        )
    return report


def patch_selftest():
    """The island rule, with no scene. Script Editor or mayapy.

    A straight strip of vertices: 0-2 painted near the joint, 6-8 painted far
    away, and 3-5 carrying dust that is BELOW the link floor. The dust must not
    join the two ends into one island, or the far smear rides home on it.
    """
    line = {index: {index - 1, index + 1} for index in range(9)}
    line[0].discard(-1)
    line[8].discard(9)

    weights = {0: .9, 1: .8, 2: .7,      # the joint's real patch
               3: .002, 4: .001, 5: .002,  # dust bridge, under the floor
               6: .6, 7: .7, 8: .5}      # a remote smear

    floor = BLEED_FLOOR                  # 0.005
    linkable = {v for v, w in weights.items() if w >= floor}
    assert linkable == {0, 1, 2, 6, 7, 8}, linkable
    islands = _islands(linkable, line)
    assert len(islands) == 2, islands
    assert {0, 1, 2} in islands and {6, 7, 8} in islands, islands

    # Negative control: drop the link floor under the dust and the bridge forms,
    # which is exactly the failure the floor exists to prevent.
    loose = {v for v, w in weights.items() if w >= 0.0005}
    merged = _islands(loose, line)
    assert len(merged) == 1, merged
    assert len(merged[0]) == 9, merged

    # Sorted biggest-first, so a report can name the main patch without hunting.
    sizes = [len(island) for island in _islands({0, 1, 2, 6, 7}, line)]
    assert sizes == sorted(sizes, reverse=True), sizes

    # A lone vertex is its own island; nothing is an empty island.
    assert _islands(set(), line) == []
    assert _islands({4}, line) == [{4}]

    # The rescue rule: only the DOMINANT-and-nothing-left case is refused.
    assert PATCH_RESCUE_FLOOR == 0.2
    assert _renormalized([0.7, 0.3], [0])[1] == 1.0

    # -- home mass: heaviest island that ALSO reaches the joint ---------------
    # Scored the way patch_islands scores them, so the ranking can be asserted
    # without a mesh.
    def home_of(entries):
        at_joint = [entry for entry in entries if entry["near"]]
        if not at_joint:
            return None, False
        home = max(at_joint, key=lambda entry: entry["mass"])
        heaviest = max(entries, key=lambda entry: entry["mass"])
        return home, heaviest is not home and heaviest["mass"] > home["mass"]

    # Agreement: the big island is the one at the joint.
    home, clash = home_of([{"name": "snout", "mass": 40.0, "near": True},
                           {"name": "shoulder", "mass": 3.0, "near": False}])
    assert home["name"] == "snout" and not clash

    # Mass ranking picks the right island when TWO reach the joint.
    home, clash = home_of([{"name": "jaw-tip", "mass": 2.0, "near": True},
                           {"name": "jaw-main", "mass": 30.0, "near": True},
                           {"name": "back", "mass": 1.0, "near": False}])
    assert home["name"] == "jaw-main" and not clash, home

    # Disagreement: the heaviest island is NOT at the joint. Keep the near one
    # and say so -- the joint's main mass being elsewhere is a rig issue.
    home, clash = home_of([{"name": "snout", "mass": 4.0, "near": True},
                           {"name": "shoulder", "mass": 50.0, "near": False}])
    assert home["name"] == "snout", home
    assert clash, "a heavier island away from the joint must be reported"

    # A tie does not count as a disagreement; the near island wins outright.
    home, clash = home_of([{"name": "snout", "mass": 9.0, "near": True},
                           {"name": "hip", "mass": 9.0, "near": False}])
    assert home["name"] == "snout" and not clash

    # Nothing near the joint at all -> no home, which the caller must refuse.
    home, clash = home_of([{"name": "back", "mass": 5.0, "near": False}])
    assert home is None

    print("SkinBench patch selftest OK")
    return True


def bleed_selftest():
    """Assert the two rules that keep a strip safe. Script Editor or mayapy."""
    kept = _renormalized([0.60, 0.30, 0.10], [2])
    assert kept[2] == 0.0, kept
    assert abs(sum(kept) - 1.0) < 1e-12, kept
    assert abs(kept[0] - 0.60 / 0.90) < 1e-12, kept

    unchanged = _renormalized([0.5, 0.5], [])
    assert abs(sum(unchanged) - 1.0) < 1e-12, unchanged

    # Gate 2 makes this unreachable; prove the guard refuses rather than
    # writing a row of zeros if it ever were reached.
    try:
        _renormalized([0.0, 0.0, 1.0], [2])
    except SkinBenchError:
        pass
    else:
        raise AssertionError("emptying a vertex must refuse, not write zeros")

    # Zero-on-selection's three verdicts. The 'skip' case is the one that
    # matters: that vertex is genuinely OWNED by the joint, so clearing it
    # would empty the row -- skip it and say so, never write zeros.
    assert _zero_verdict([0.7, 0.3], [1])[0] == "clear"
    assert _zero_verdict([1.0, 0.0], [1])[0] == "untouched"
    assert _zero_verdict([0.0, 1.0], [1])[0] == "skip"

    # The dominant gate. 0.6 vs 0.4 is bleed's mirror image -- same two joints,
    # and only which one leads decides whether this is a clean or a wipe.
    assert _zero_verdict([0.4, 0.6], [1])[0] == "dominant"
    assert _zero_verdict([0.4, 0.6], [1], keep_dominant=False)[0] == "clear"
    assert _zero_verdict([0.6, 0.4], [1])[0] == "clear"
    assert set(_VERDICT_KEY) == {"untouched", "skip", "dominant"}, _VERDICT_KEY

    # -- the ruler (bone_length). During live testing, jaw_joint hung off its
    # parent on a 5.43 stub, and its real bone ran ~50 units forward to
    # end_joint. The snout TIP -- which the upper jaw owns at w=1.000 --
    # sits about 47 units out. Judged by the stub it is 8.7 bone lengths away
    # and gets flagged at the default N=8; judged by the bone it actually
    # drives, it is under one. 47 verts were being accused of bleed for this.
    stub, real_bone, snout_tip = 5.43, 50.0, 47.0
    assert _reach_from_segments([stub]) == stub, "incoming-only still counts"
    reach = _reach_from_segments([stub, real_bone])
    assert reach == real_bone, reach
    assert snout_tip / stub > 8.0, "the old ruler flagged the rig's own snout"
    assert snout_tip / reach < 8.0, "the new ruler must not flag it at N=8"
    assert snout_tip / reach < 1.0, "it is not even one bone length out"

    # A short joint stays short -- the fix must not blunt the test everywhere.
    pinky = _reach_from_segments([0.8, 0.7])
    assert pinky == 0.8, pinky
    assert 9.0 / pinky > 8.0, "a vert 9 units from a pinky is still bleed"

    # No evidence for a scale -> no opinion, never a zero-length ruler.
    assert _reach_from_segments([]) is None
    assert _reach_from_segments([0.0]) is None
    assert _reach_from_segments([1e-9]) is None

    print("SkinBench bleed selftest OK")
    return True


def weight_write_selftest(allow_scene_changes=False):
    """Prove SkinBench writes weights through the right index space.

    The pure half always runs. The live half builds a throwaway rig with SPARSE
    influence indices -- the shape that crashed Strip during live testing -- so it
    CHANGES THE SCENE and is opt-in: ``weight_write_selftest(True)``, in mayapy
    or an empty scene. It deletes everything it made on the way out.
    """
    # -- pure: the index-space rule ------------------------------------------
    assert _physical_columns(4) == [0, 1, 2, 3]
    assert _physical_columns(0) == []
    assert _physical_columns(1) == [0]
    # A cluster with history: 69 influences whose logical indices run past 68.
    # Physical is what setWeights wants, and it never has gaps by definition.
    sparse_logical = [0, 3, 4, 5]
    assert _physical_columns(len(sparse_logical)) != sparse_logical
    assert max(sparse_logical) >= len(sparse_logical), "this is the trap"

    if not allow_scene_changes:
        print("SkinBench weight write selftest OK (pure half only -- pass True "
              "in mayapy or an empty scene for the live cluster half)")
        return True

    # -- live: a real cluster with gaps in its logical indices -----------------
    made = []
    try:
        # A CHAIN, not six loose joints: every joint needs a measurable bone or
        # bleed_report skips it and the Strip half of this test proves nothing.
        cmds.select(clear=True)
        joints = [cmds.joint(name="sbTest_j%d" % index, position=(index * 2.0, 0, 0))
                  for index in range(6)]
        cmds.select(clear=True)
        made.append(joints[0])                  # deleting the root takes the chain
        mesh = cmds.polyPlane(name="sbTest_mesh", width=10, height=2,
                              subdivisionsX=5, subdivisionsY=1)[0]
        made.append(mesh)
        cluster = cmds.skinCluster(joints[:4], mesh, toSelectedBones=True,
                                   maximumInfluences=4)[0]

        # Punch holes in .matrix[] the way years of rig edits do.
        cmds.skinCluster(cluster, edit=True, removeInfluence=joints[1])
        cmds.skinCluster(cluster, edit=True, removeInfluence=joints[2])
        cmds.skinCluster(cluster, edit=True, addInfluence=joints[4], weight=0.0)
        cmds.skinCluster(cluster, edit=True, addInfluence=joints[5], weight=0.0)

        fn = _skin_fn(cluster)
        names, logical = _influence_table(fn)
        physical = _physical_columns(len(names))
        assert logical != physical, (
            "this rig is not sparse, so it cannot prove anything: %s" % logical)

        shape = deformed_shape(mesh)
        dag = _dag_path(shape)
        subset = [0, 1, 2]                      # a PARTIAL component, as Strip uses

        def component():
            comp_fn = om.MFnSingleIndexedComponent()
            made_component = comp_fn.create(om.MFn.kMeshVertComponent)
            comp_fn.addElements(subset)
            return made_component

        flat, stride = fn.getWeights(dag, component())
        assert stride == len(names), (stride, len(names))
        assert len(flat) == len(subset) * stride

        values = om.MDoubleArray(flat)
        for vertex in range(len(subset)):
            for slot in range(stride):
                values[vertex * stride + slot] = 1.0 if slot == 0 else 0.0

        # The old call shape must still blow up, or this test proves nothing.
        try:
            fn.setWeights(dag, component(), om.MIntArray(logical),
                          om.MDoubleArray(values), False)
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "logical indices were accepted -- the crash cannot be reproduced, "
                "so this test would not catch a regression")

        # The fixed path writes, and the weights read back as asked.
        _set_weights(fn, dag, component(), om.MDoubleArray(values), len(subset))
        back, back_stride = fn.getWeights(dag, component())
        for vertex in range(len(subset)):
            row = vertex * back_stride
            assert abs(back[row] - 1.0) < 1e-9, back[row:row + back_stride]
            for slot in range(1, back_stride):
                assert abs(back[row + slot]) < 1e-9, back[row:row + back_stride]

        # Maya accepts a wrong-length array and writes garbage; we must not.
        try:
            _set_weights(fn, dag, component(),
                         om.MDoubleArray(list(values)[:-1]), len(subset))
        except SkinBenchError:
            pass
        else:
            raise AssertionError("a short value array must be refused")

        # And the whole Strip path, end to end, on that same sparse cluster.
        report = strip_bleed(cluster=cluster, n=0.5, dry_run=False)
        assert report["snapshot"], "Strip must snapshot before it writes"
        print("SkinBench weight write selftest OK -- sparse logical %s vs "
              "physical %s, Strip wrote %d vertex(es)"
              % (logical, physical, report["strippedVertices"]))
    finally:
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)
    return True


# ---------------------------------------------------------------------------
# region isolate: show only what these joints drive
# ---------------------------------------------------------------------------

# A vertex belongs to the region if any of the chosen joints holds at least this
# much of it. Deliberately low: painting a region needs the falloff zone at the
# shoulder VISIBLE, or you paint a seam you cannot see.
#
# ponytail: one constant, no per-call dial. If a rig ever needs a different
# floor, make it a spinbox on the Cleanup tab -- not before.
REGION_FLOOR = 0.05


def _model_panel():
    """The viewport to isolate in: the focused one, else any visible one."""
    focused = cmds.getPanel(withFocus=True)
    if focused and cmds.getPanel(typeOf=focused) == "modelPanel":
        return focused
    panels = cmds.getPanel(type="modelPanel") or []
    if not panels:
        raise SkinBenchError("No 3D viewport open to isolate in.")
    visible = set(cmds.getPanel(visiblePanels=True) or [])
    return next((panel for panel in panels if panel in visible), panels[0])


def isolate_selection(enable=True, panel=None):
    """Maya's View Selected, driven from code. Returns the panel it acted on."""
    panel = panel or _model_panel()
    cmds.isolateSelect(panel, state=int(bool(enable)))
    if enable:
        cmds.isolateSelect(panel, addSelected=True)
    return panel


def influenced_vertices(joints=None, cluster=None, include_hierarchy=False,
                        floor=REGION_FLOOR):
    """Every vertex the given joints actually drive.

    This is the "isolate the arm" region, DERIVED from the rig rather than saved
    on it. Maya's Quick Select Sets already store a named component selection --
    the reason not to use one here is that a saved set goes stale silently. Add
    a joint, change topology, repaint, and the set still selects *something*,
    just the wrong something. Asking the weights cannot drift from the weights.

    ``include_hierarchy`` takes everything below the selection, so picking
    ``LeftArm`` gets the whole arm and hand.
    """
    if joints is None:
        joints = cmds.ls(selection=True, long=True, type="joint") or []
    joints = list(joints)
    if include_hierarchy and joints:
        children = cmds.listRelatives(
            joints, allDescendents=True, type="joint", fullPath=True
        ) or []
        joints.extend(child for child in children if child not in joints)
    if not joints:
        raise SkinBenchError("Select the joint (or joints) whose region you want.")

    cluster = cluster or _one_cluster_from_selection()
    shapes = meshes_for_skin_cluster(cluster)
    if not shapes:
        raise SkinBenchError("'%s' drives no mesh." % cluster)
    shape = shapes[0]

    fn = _skin_fn(cluster)
    names, _ = _influence_table(fn)
    asked = set(joints) | {name.split("|")[-1] for name in joints}
    wanted = [
        slot
        for slot, name in enumerate(names)
        if name in asked or name.split("|")[-1] in asked
    ]
    if not wanted:
        raise SkinBenchError(
            "None of those joints influence '%s' -- right joints, wrong mesh?" % cluster
        )

    vertex_count = cmds.polyEvaluate(shape, vertex=True)
    flat, stride = fn.getWeights(_dag_path(shape), _complete_vertex_component(vertex_count))

    vertices = [
        vertex
        for vertex in range(vertex_count)
        if any(flat[vertex * stride + slot] >= floor for slot in wanted)
    ]
    return {
        "skinCluster": cluster,
        "shape": shape,
        "joints": [names[slot].split("|")[-1] for slot in wanted],
        "vertices": vertices,
        "floor": floor,
    }


def select_influenced(joints=None, cluster=None, include_hierarchy=False,
                      isolate=False, floor=REGION_FLOOR):
    """Select the region these joints drive, and optionally isolate the view."""
    report = influenced_vertices(
        joints=joints, cluster=cluster,
        include_hierarchy=include_hierarchy, floor=floor,
    )
    if not report["vertices"]:
        raise SkinBenchError(
            "Those joints hold no weight above %.2f -- nothing to isolate." % floor
        )
    select_vertices(report["skinCluster"], report["vertices"])
    report["panel"] = isolate_selection(True) if isolate else None
    return report


# ---------------------------------------------------------------------------
# grow: the region the weights give you is often smaller than the one you want
# ---------------------------------------------------------------------------

# ``(label, axis)``. The axis is the half-space a NEWLY reached vertex must sit
# in, measured from the middle of the selection the grow started from. ``None``
# keeps everything the grow reached -- plain Grow Selection.
GROW_AXES = (
    ("All directions", None),
    ("Up  (+Y)", (0.0, 1.0, 0.0)),
    ("Down  (-Y)", (0.0, -1.0, 0.0)),
    ("Forward  (+Z)", (0.0, 0.0, 1.0)),
    ("Back  (-Z)", (0.0, 0.0, -1.0)),
    ("Right  (+X)", (1.0, 0.0, 0.0)),
    ("Left  (-X)", (-1.0, 0.0, 0.0)),
)


def _centroid(points, ids):
    """Average position of ``ids``. ``points`` is anything indexable with .x/.y/.z."""
    total = [0.0, 0.0, 0.0]
    for index in ids:
        point = points[index]
        total[0] += point.x
        total[1] += point.y
        total[2] += point.z
    count = float(len(ids))
    return (total[0] / count, total[1] / count, total[2] / count)


def _in_half_space(point, centre, axis):
    """Is ``point`` on the ``axis`` side of ``centre``? ``axis=None`` = always."""
    if axis is None:
        return True
    return sum((point[i] - centre[i]) * axis[i] for i in range(3)) > 0.0


def _one_ring(shape, ids):
    """Every vertex of ``shape`` one edge away from ``ids``, plus ``ids`` itself.

    vertices -> edges -> vertices is Maya's own Grow Selection, spelled out.
    ``polyListComponentConversion`` takes any edge that touches a chosen vertex
    unless ``internal`` is set, so the round trip lands exactly one ring out.
    """
    components = ["%s.vtx[%d]" % (shape, index) for index in ids]
    edges = cmds.polyListComponentConversion(
        components, fromVertex=True, toEdge=True) or []
    if not edges:
        return set(ids)
    grown = cmds.polyListComponentConversion(edges, fromEdge=True, toVertex=True) or []
    found = set()
    for name in cmds.filterExpand(grown, selectionMask=31, fullPath=True) or []:
        found.add(int(name.partition(".vtx[")[2].rstrip("]")))
    return found or set(ids)


def grow_selection(steps=1, axis=None):
    """Push the selected vertices out by ``steps`` rings of neighbours.

    The region read off the weights is often smaller than the one an artist
    wants to paint -- this is the "a bit more, please" button, per mesh, and it
    REPORTS what it did rather than leaving you counting dots.

    With an ``axis`` a newly reached vertex is kept only if it sits on that side
    of where the selection started, so a region can creep up the neck without
    spilling down the chest. The centre is measured ONCE, before the first step:
    re-measuring it each step would let the selection chase its own centroid and
    curve away from the direction that was asked for.
    """
    steps = int(steps)
    if steps < 1:
        raise SkinBenchError("Grow needs at least 1 step.")
    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "Select some vertices first -- e.g. with Select region.")

    report = {
        "steps": steps, "axis": axis, "shapes": [],
        "before": 0, "after": 0, "added": 0, "wrongSide": 0,
    }
    components = []
    for shape in sorted(grouped):
        ids = set(grouped[shape])
        report["before"] += len(ids)
        report["shapes"].append(shape.split("|")[-1])
        points = om.MFnMesh(_dag_path(shape)).getPoints(om.MSpace.kWorld)
        centre = _centroid(points, ids)
        wrong_side = set()
        for _ in range(steps):
            fresh = _one_ring(shape, ids) - ids
            if not fresh:
                break                      # already at the edge of the mesh
            kept = set()
            for index in fresh:
                point = points[index]
                if _in_half_space((point.x, point.y, point.z), centre, axis):
                    kept.add(index)
            wrong_side |= fresh - kept
            if not kept:
                break                      # everything next door is the wrong way
            ids |= kept
        report["after"] += len(ids)
        report["wrongSide"] += len(wrong_side)
        components.extend("%s.vtx[%d]" % (shape, index) for index in sorted(ids))

    report["added"] = report["after"] - report["before"]
    if components:
        cmds.select(components, replace=True)
    return report


def shrink_selection(steps=1):
    """Pull the selected vertices IN by ``steps`` rings -- the exact inverse of Grow.

    A vertex survives a step only if every neighbour it shares an edge with is
    also selected, so the selection erodes from its own border inward. Grow N
    then Shrink N returns the set you started with (proven on a sphere in the
    selftest), which is what makes the pair usable as a nudge in both
    directions rather than a one-way trip.

    A selection covering a whole shell has no border to erode from, so it is
    reported as unchanged rather than being quietly emptied.
    """
    steps = int(steps)
    if steps < 1:
        raise SkinBenchError("Shrink needs at least 1 step.")
    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "Select some vertices first -- e.g. with Select region.")

    report = {"steps": steps, "shapes": [], "before": 0, "after": 0,
              "removed": 0, "sealed": 0}
    components = []
    for shape in sorted(grouped):
        ids = set(grouped[shape])
        report["before"] += len(ids)
        report["shapes"].append(shape.split("|")[-1])
        for _ in range(steps):
            outside = _one_ring(shape, ids) - ids
            if not outside:
                report["sealed"] += 1     # nothing left to erode from
                break
            border = _one_ring(shape, outside) & ids
            if not border or border == ids:
                break                     # eroding would empty it; stop instead
            ids -= border
        report["after"] += len(ids)
        components.extend("%s.vtx[%d]" % (shape, index) for index in sorted(ids))

    report["removed"] = report["before"] - report["after"]
    if components:
        cmds.select(components, replace=True)
    return report


def grow_selftest():
    """The direction filter's maths, with no scene. Script Editor or mayapy."""

    class _P(object):
        def __init__(self, x, y, z):
            self.x = x
            self.y = y
            self.z = z

    assert _centroid([_P(0.0, 0.0, 0.0), _P(2.0, 4.0, 6.0)], [0, 1]) == (1.0, 2.0, 3.0)
    assert _centroid([_P(1.0, 1.0, 1.0)], [0]) == (1.0, 1.0, 1.0)

    centre, up = (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    assert _in_half_space((0.0, 5.0, 0.0), centre, up)
    assert not _in_half_space((0.0, -5.0, 0.0), centre, up)
    # Dead level with the centre is NOT "up" -- otherwise a directional grow
    # creeps sideways around the whole model and looks like plain Grow.
    assert not _in_half_space((9.0, 0.0, 0.0), centre, up)
    # No axis keeps everything, which is what "All directions" means.
    assert _in_half_space((0.0, -5.0, 0.0), centre, None)
    # The centre moves with the selection, not with the world origin.
    assert _in_half_space((0.0, 3.0, 0.0), (0.0, 2.0, 0.0), up)
    assert not _in_half_space((0.0, 1.0, 0.0), (0.0, 2.0, 0.0), up)

    labels = [label for label, _ in GROW_AXES]
    assert len(set(labels)) == len(labels), labels
    assert GROW_AXES[0][1] is None, "the first entry must be the plain grow"
    print("SkinBench grow selftest OK")
    return True


def paint_influence(joint=None, cluster=None):
    """Open Paint Skin Weights on this cluster's mesh with THIS joint active.

    Picking a joint in SkinBench's list answered "which joint" and then left
    the artist switching tool by hand and hunting the SAME name again in
    Maya's own influence list -- the picture was two menus away from the
    question that asked for it. This is that click path, as one button.
    """
    from maya import mel

    if joint is None:
        chosen = cmds.ls(selection=True, long=True, type="joint") or []
        joint = chosen[0] if chosen else None
    if not joint:
        raise SkinBenchError("Pick the joint you want to see painted.")
    leaf = joint.split("|")[-1]

    cluster = cluster or _one_cluster_from_selection()
    shapes = meshes_for_skin_cluster(cluster)
    if not shapes:
        raise SkinBenchError("'%s' drives no mesh." % cluster)
    mesh = cmds.listRelatives(shapes[0], parent=True, fullPath=True)[0]

    influences = cmds.skinCluster(cluster, query=True, influence=True) or []
    if leaf not in [name.split("|")[-1] for name in influences]:
        raise SkinBenchError(
            "'%s' does not influence '%s' -- right joint, wrong mesh?"
            % (leaf, cluster)
        )

    cmds.select(mesh, replace=True)
    mel.eval("ArtPaintSkinWeightsTool;")
    try:
        mel.eval('artSkinSelectInfluence("%s", "%s");' % (cmds.currentCtx(), leaf))
    except RuntimeError:
        # The tool IS open on the right mesh, which is most of the job. Say
        # exactly what is left rather than pretending the button failed.
        raise SkinBenchError(
            "Paint tool open on '%s', but Maya would not switch the influence "
            "to '%s' -- pick it in the tool's own list."
            % (mesh.split("|")[-1], leaf)
        )
    return {"mesh": mesh.split("|")[-1], "joint": leaf, "skinCluster": cluster}


# ---------------------------------------------------------------------------
# convenience wrappers for shelf buttons / the plugin commands
# ---------------------------------------------------------------------------


def invert_region():
    """Select every vertex of the same meshes that is NOT currently selected.

    ``Select region`` answers "what does this joint drive"; this answers
    "what does it not" -- per mesh, never across meshes.
    """
    grouped = _selected_vertices_by_shape()
    if not grouped:
        raise SkinBenchError(
            "Select some vertices first — e.g. with Select region.")
    components = []
    for shape, ids in sorted(grouped.items()):
        chosen = set(ids)
        count = cmds.polyEvaluate(shape, vertex=True)
        components.extend("%s.vtx[%d]" % (shape, index)
                          for index in range(count) if index not in chosen)
    if components:
        cmds.select(components, replace=True)
    else:
        cmds.select(clear=True)
    return len(components)


def detach_selection():
    """Detach every skinned mesh in the selection. Returns a list of reports."""
    meshes = _selected_skinned_meshes()
    reports = []
    for mesh in meshes:
        path, cluster = detach(mesh)
        reports.append({"mesh": mesh, "snapshot": path, "skinCluster": cluster})
    return reports


def reattach_selection(restore_bind_matrices=False):
    """Reattach every selected mesh from its newest snapshot."""
    meshes = cmds.ls(selection=True, long=True, type="transform") or []
    meshes = [mesh for mesh in meshes if cmds.listRelatives(mesh, shapes=True, type="mesh")]
    if not meshes:
        raise SkinBenchError("Select the mesh (or meshes) you want to reattach.")
    reports = []
    for mesh in meshes:
        snapshot = newest_snapshot_for(mesh)
        if not snapshot:
            raise SkinBenchError("No snapshot found for '%s'." % mesh.split("|")[-1])
        reports.append(reattach(mesh, snapshot, restore_bind_matrices=restore_bind_matrices))
    return reports


def newest_snapshot_for(mesh, directory=None):
    """Most recent snapshot belonging to ``mesh``.

    Matched on the namespace-qualified name, and confirmed by UUID when the
    snapshot carries one -- so a scene holding ``charA:body`` and ``charB:body``
    never hands one rig the other's weights.
    """
    prefix = _file_stem(mesh) + "__"
    current_uuid = uuid_of(mesh)
    fallback = None
    for path in list_snapshots(directory):
        if not os.path.basename(path).startswith(prefix):
            continue
        if not current_uuid:
            return path
        try:
            saved_uuid = read_snapshot(path).get("meshUuid")
        except (SkinBenchError, ValueError, IOError):
            continue
        if saved_uuid == current_uuid:
            return path
        if saved_uuid is None and fallback is None:
            fallback = path  # pre-UUID snapshot; usable, just less certain
    return fallback


def select_vertices(cluster_or_mesh, vertex_ids):
    """Select the given vertex ids on the mesh, so damage is visible not buried."""
    if not vertex_ids:
        return []
    if cmds.nodeType(cluster_or_mesh) == "skinCluster":
        shapes = meshes_for_skin_cluster(cluster_or_mesh)
        if not shapes:
            return []
        shape = shapes[0]
    else:
        shape = deformed_shape(cluster_or_mesh)
    components = ["%s.vtx[%d]" % (shape, index) for index in vertex_ids]
    cmds.select(components, replace=True)
    return components


def _selected_skinned_meshes():
    selection = cmds.ls(selection=True, long=True) or []
    if not selection:
        raise SkinBenchError("Nothing selected. Select the skinned mesh first.")
    meshes = []
    for node in selection:
        cluster = skin_cluster_for(node)
        if not cluster:
            continue
        transform = cmds.listRelatives(deformed_shape(node), parent=True, fullPath=True)[0]
        if transform not in meshes:
            meshes.append(transform)
    if not meshes:
        raise SkinBenchError("Nothing in the selection is skinned.")
    return meshes


# ---------------------------------------------------------------------------
# pose bank -- save a pose once, replay it on this rig or roughly on another
# ---------------------------------------------------------------------------

POSE_VERSION = 1


def pose_dir():
    """One shared library for every character, so poses cross scene folders."""
    path = os.path.join(cmds.internalVar(userAppDir=True), "skinbench", "poses")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def _quat_deg(xyz):
    return om.MEulerRotation([math.radians(v) for v in xyz]).asQuaternion()


def _world_rotation(node):
    matrix = om.MMatrix(cmds.xform(node, query=True, worldSpace=True, matrix=True))
    return om.MTransformationMatrix(matrix).rotation(asQuaternion=True)


def _pose_leaf(node):
    return node.split("|")[-1].split(":")[-1]


def _rest_world_rotation(joint, cache):
    """World rotation this joint WOULD have with every rotate at zero.

    Computed from jointOrient/rotateAxis alone, walking parent-first -- the
    scene is never touched, so a pose can be SAVED while the rig is posed.
    Non-joint ancestors (groups) are read as-is; they are never posed.
    """
    if joint in cache:
        return cache[joint]
    parents = cmds.listRelatives(joint, parent=True, fullPath=True) or []
    if not parents:
        parent_q = om.MQuaternion()
    elif cmds.nodeType(parents[0]) == "joint":
        parent_q = _rest_world_rotation(parents[0], cache)
    else:
        parent_q = _world_rotation(parents[0])
    ra = cmds.getAttr(joint + ".rotateAxis")[0]
    jo = cmds.getAttr(joint + ".jointOrient")[0]
    # local rotation = rotateAxis * rotate * jointOrient, with rotate = zero
    q = _quat_deg(ra) * _quat_deg(jo) * parent_q
    cache[joint] = q
    return q


def save_pose(name, joints=None):
    """Write the current pose to the shared library. Returns the file path.

    Stores, per joint: the local rotate values (exact same-rig replay), and
    the world rotation + computed REST world rotation (so another biped can
    replay the world-space offset-from-rest by joint name).
    """
    if joints is None:
        joints = cmds.ls(type="joint", long=True) or []
    if not joints:
        raise SkinBenchError("No joints to save.")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", (name or "").strip())
    if not safe:
        raise SkinBenchError("Give the pose a name first.")

    cache, entries = {}, {}
    for joint in sorted(joints, key=_depth):
        leaf = _pose_leaf(joint)
        if leaf in entries:
            continue  # duplicate leaf names cannot round-trip by name
        world = _world_rotation(joint)
        rest = _rest_world_rotation(joint, cache)
        entries[leaf] = {
            "rotate": list(cmds.getAttr(joint + ".rotate")[0]),
            "world_q": [world.x, world.y, world.z, world.w],
            "rest_q": [rest.x, rest.y, rest.z, rest.w],
        }

    data = {
        "version": POSE_VERSION,
        "name": name,
        "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scene": cmds.file(query=True, sceneName=True) or "<unsaved>",
        "joints": entries,
    }
    path = os.path.join(pose_dir(), safe + ".json")
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path


def list_pose_files():
    """Pose files, newest first."""
    directory = pose_dir()
    return sorted(
        (os.path.join(directory, name) for name in os.listdir(directory)
         if name.lower().endswith(".json")),
        key=os.path.getmtime, reverse=True)


def read_pose(path):
    with open(path) as handle:
        data = json.load(handle)
    if data.get("version") != POSE_VERSION:
        raise SkinBenchError(
            "Pose '%s' is version %s; this SkinBench reads version %d."
            % (os.path.basename(path), data.get("version"), POSE_VERSION))
    return data


def apply_pose(path, world_space=False):
    """Put the rig into a saved pose. Writes ``.rotate`` only; Ctrl+Z undoes.

    Same rig: exact -- the stored rotate values go straight back on.

    ``world_space``: for a DIFFERENT biped. Each stored joint's world-space
    offset-from-rest is replayed onto the same-named joint here, parent-first,
    and Maya solves the local values -- the same world-not-local trick as the
    joint mirror, and why a characterA pose lands on characterB despite
    completely different jointOrients (94 shared names, 13 matching orients,
    measured during live testing). Roughly right is the promise; nudge from there. Rotation
    only -- translations never move, so different body sizes are fine.
    """
    data = path if isinstance(path, dict) else read_pose(path)

    targets = {}
    for leaf in data["joints"]:
        found = cmds.ls(leaf, type="joint", long=True) or []
        if len(found) == 1:
            targets[leaf] = found[0]
    missing = sorted(set(data["joints"]) - set(targets))

    applied, locked = 0, []
    cache = {}
    cmds.undoInfo(openChunk=True, chunkName="SkinBench apply pose")
    try:
        for leaf, joint in sorted(targets.items(), key=lambda kv: _depth(kv[1])):
            entry = data["joints"][leaf]
            try:
                if world_space:
                    src_rest = om.MQuaternion(*entry["rest_q"])
                    src_pose = om.MQuaternion(*entry["world_q"])
                    delta = src_rest.inverse() * src_pose  # world-frame offset
                    goal = _rest_world_rotation(joint, cache) * delta
                    euler = goal.asEulerRotation().reorder(
                        cmds.getAttr(joint + ".rotateOrder"))
                    cmds.xform(joint, worldSpace=True, rotation=[
                        math.degrees(euler.x), math.degrees(euler.y),
                        math.degrees(euler.z)])
                else:
                    cmds.setAttr(joint + ".rotate", *entry["rotate"])
                applied += 1
            except RuntimeError:
                locked.append(leaf)
    finally:
        cmds.undoInfo(closeChunk=True)
    return {"name": data.get("name"), "applied": applied, "missing": missing,
            "locked": locked, "world_space": world_space}


def zero_pose(joints=None):
    """Every joint back to ``rotate 0`` -- the rest pose on a zeroed rig."""
    if joints is None:
        joints = cmds.ls(type="joint", long=True) or []
    cleared, locked = 0, []
    cmds.undoInfo(openChunk=True, chunkName="SkinBench zero pose")
    try:
        for joint in joints:
            rotate = cmds.getAttr(joint + ".rotate")[0]
            if all(abs(v) < 1e-9 for v in rotate):
                continue
            try:
                cmds.setAttr(joint + ".rotate", 0.0, 0.0, 0.0)
                cleared += 1
            except RuntimeError:
                locked.append(_pose_leaf(joint))
    finally:
        cmds.undoInfo(closeChunk=True)
    return {"cleared": cleared, "locked": locked}


def pose_selftest():
    """Assert the pose maths (quaternion conventions). Scene-free."""
    a, b = _quat_deg((30, 0, 0)), _quat_deg((0, 45, 0))
    left, right = (a * b).asMatrix(), a.asMatrix() * b.asMatrix()
    assert max(abs(left[i] - right[i]) for i in range(16)) < 1e-9, "homomorphism"
    # rest * (rest^-1 * pose) must land exactly back on pose
    rest, pose = _quat_deg((10, 20, 30)), _quat_deg((-40, 25, 5))
    got = (rest * (rest.inverse() * pose)).asMatrix()
    want = pose.asMatrix()
    assert max(abs(got[i] - want[i]) for i in range(16)) < 1e-9, "delta"
    return "pose_selftest OK"
