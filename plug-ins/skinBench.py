# -*- coding: utf-8 -*-
"""SkinBench Maya plugin — registers commands and the SkinBench menu.

Load from Windows > Settings/Preferences > Plug-in Manager (tick Auto load),
or run ``cmds.loadPlugin("skinBench.py")``.

Commands registered
-------------------
``skinBench``            open the SkinBench window
``skinBenchDetach``      snapshot + unbind every skinned mesh in the selection
``skinBenchReattach``    rebind from the newest snapshot  (-rbm to restore bind matrices)
``skinBenchMoveJoints``  toggle Move Skinned Joints  (-on / -off, -all for the whole scene)
``skinBenchBakeOrient``  fold rotate into jointOrient, zeroing rotate  (-hi for the hierarchy)
``skinBenchROM``         step the selected joints through their range of motion  (-r to end)
``skinBenchCurl``        bend the whole chain below the selection  (-ax X|Y|Z, -s to straighten)
``skinBenchBleed``       find weights reaching too far from the selected joints  (-n, -s to strip)
``skinBenchIsolate``     select the region the selected joints drive  (-hi, -iso, -off)
"""

import maya.api.OpenMaya as om
import maya.cmds as cmds
import maya.mel as mel

import skinbench
from skinbench import core

VENDOR = "SkinBench"
VERSION = skinbench.__version__
MENU_NAME = "skinBenchMenu"


def maya_useNewAPI():
    """Tell Maya this plugin speaks the Python API 2.0."""


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


class _Command(om.MPxCommand):
    kName = ""

    def isUndoable(self):
        # Weight writes go through the API and are not on Maya's undo queue.
        # Recovery is by snapshot, not by Ctrl+Z -- see the README.
        return False

    @classmethod
    def creator(cls):
        return cls()


class SkinBenchCommand(_Command):
    kName = "skinBench"

    def doIt(self, args):
        skinbench.show()


class DetachCommand(_Command):
    kName = "skinBenchDetach"

    def doIt(self, args):
        for report in core.detach_selection():
            om.MGlobal.displayInfo(
                "SkinBench: detached %s -> %s"
                % (report["mesh"].split("|")[-1], report["snapshot"])
            )


class ReattachCommand(_Command):
    kName = "skinBenchReattach"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-rbm", "-restoreBindMatrices", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)
        restore = parser.isFlagSet("-rbm")
        for report in core.reattach_selection(restore_bind_matrices=restore):
            om.MGlobal.displayInfo(
                "SkinBench: reattached %s (%d influences, %d dropped)"
                % (report["skinCluster"], report["influences"], report["dropped"])
            )
            for name in report["missing"]:
                om.MGlobal.displayWarning(
                    "SkinBench: influence '%s' no longer exists; weights renormalized." % name
                )
            orphans = report.get("orphanedVertices") or []
            if orphans:
                om.MGlobal.displayWarning(
                    "SkinBench: %d vertices lost all weight and have been selected."
                    % len(orphans)
                )
                core.select_vertices(report["skinCluster"], orphans)


class MoveJointsCommand(_Command):
    kName = "skinBenchMoveJoints"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-on", "-enable", om.MSyntax.kNoArg)
        syntax.addFlag("-off", "-disable", om.MSyntax.kNoArg)
        syntax.addFlag("-all", "-allClusters", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)
        clusters = (
            core.all_skin_clusters()
            if parser.isFlagSet("-all")
            else core.skin_clusters_from_selection()
        )
        if not clusters:
            om.MGlobal.displayWarning(
                "SkinBench: no skinCluster found — select a skinned mesh or an influence joint."
            )
            return

        if parser.isFlagSet("-on"):
            enabled = True
        elif parser.isFlagSet("-off"):
            enabled = False
        else:  # no flag: flip whatever the first cluster currently is
            enabled = not core.move_joints_mode(clusters[0])

        result = core.set_move_joints_mode(clusters, enabled)
        om.MGlobal.displayInfo(
            "SkinBench: Move Skinned Joints %s on %s"
            % ("ON" if enabled else "OFF", ", ".join(clusters))
        )
        poses = result["bindPoses"]
        if poses and poses.get("skipped"):
            om.MGlobal.displayWarning(
                "SkinBench: bind poses NOT refreshed — %s" % poses["skipped"])
        elif poses and poses["checked"]:
            om.MGlobal.displayInfo(
                "SkinBench: bind poses — %d joint(s) checked, %d refreshed."
                % (poses["checked"], poses["reset"]))
        self.setResult(enabled)


class BakeOrientCommand(_Command):
    kName = "skinBenchBakeOrient"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-hi", "-hierarchy", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)
        baked, skipped = core.bake_rotation_to_orient(
            include_hierarchy=parser.isFlagSet("-hi")
        )
        om.MGlobal.displayInfo(
            "SkinBench: baked rotate -> jointOrient on %d joint(s)." % len(baked)
        )
        for joint, reason in skipped:
            om.MGlobal.displayWarning("SkinBench: skipped %s (%s)" % (joint, reason))
        self.setResult([j for j in baked])


class RomCommand(_Command):
    kName = "skinBenchROM"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-r", "-reset", om.MSyntax.kNoArg)
        syntax.addFlag("-p", "-previous", om.MSyntax.kNoArg)
        syntax.addFlag("-hi", "-hierarchy", om.MSyntax.kNoArg)
        syntax.addFlag("-a", "-angle", om.MSyntax.kDouble)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)

        if parser.isFlagSet("-r"):
            stragglers = core.rom_reset()
            for joint, reason in stragglers:
                om.MGlobal.displayWarning(
                    "SkinBench: %s did NOT reset (%s)" % (joint, reason)
                )
            if not stragglers:
                om.MGlobal.displayInfo(
                    "SkinBench: sweep ended, every joint back at its starting rotate."
                )
            return

        angle = (
            parser.flagArgumentDouble("-a", 0)
            if parser.isFlagSet("-a")
            else core.ROM_ANGLE
        )
        label, index, total, report = core.rom_next(
            delta=-1 if parser.isFlagSet("-p") else 1,
            angle=angle,
            include_hierarchy=parser.isFlagSet("-hi"),
        )
        if report is not None:
            om.MGlobal.displayInfo(
                "SkinBench: sweeping %d joint(s), %d poses -- one joint at a time, "
                "6 poses each. Run again to step, -r to end."
                % (len(report["joints"]), report["steps"])
            )
            for joint, reason in report["skipped"]:
                om.MGlobal.displayWarning("SkinBench: skipped %s (%s)" % (joint, reason))

        om.MGlobal.displayInfo("SkinBench ROM %d/%d : %s" % (index, total, label))
        self.setResult(label)


class CurlCommand(_Command):
    kName = "skinBenchCurl"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-d", "-degrees", om.MSyntax.kDouble)
        syntax.addFlag("-ax", "-axis", om.MSyntax.kString)
        syntax.addFlag("-s", "-straighten", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)

        if parser.isFlagSet("-s"):
            stragglers = core.straighten_chain()
            for joint, reason in stragglers:
                om.MGlobal.displayWarning(
                    "SkinBench: %s did NOT straighten (%s)" % (joint, reason)
                )
            if not stragglers:
                om.MGlobal.displayInfo(
                    "SkinBench: chain straightened, every joint back where the curl found it."
                )
            return

        report = core.curl_chain(
            degrees=(
                parser.flagArgumentDouble("-d", 0)
                if parser.isFlagSet("-d")
                else core.CURL_STEP
            ),
            axis=parser.flagArgumentString("-ax", 0) if parser.isFlagSet("-ax") else None,
        )
        om.MGlobal.displayInfo(
            "SkinBench: curled %d joint(s). Click again to bend further, straighten to undo."
            % len(report["joints"])
        )
        if report["clamped"]:
            om.MGlobal.displayInfo(
                "SkinBench: %d joint(s) hit their rotation limit and stopped."
                % len(report["clamped"])
            )
        for joint, reason in report["skipped"]:
            om.MGlobal.displayWarning("SkinBench: skipped %s (%s)" % (joint, reason))
        self.setResult(len(report["joints"]))


class BleedCommand(_Command):
    kName = "skinBenchBleed"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-n", "-multiple", om.MSyntax.kDouble)
        syntax.addFlag("-s", "-strip", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)
        n = (
            parser.flagArgumentDouble("-n", 0)
            if parser.isFlagSet("-n")
            else core.BLEED_N
        )
        joints = cmds.ls(selection=True, long=True, type="joint") or None
        report = core.strip_bleed(joints=joints, n=n, dry_run=not parser.isFlagSet("-s"))

        who = ", ".join(report["audited"][:4]) or "every influence"
        if len(report["audited"]) > 4:
            who += " +%d more" % (len(report["audited"]) - 4)
        # Same four named buckets as the window's log. One vocabulary, so a
        # shelf button and the UI never look like they disagree.
        om.MGlobal.displayInfo(
            "SkinBench bleed: %s at N=%g — %d vertex(es) carry these joints: "
            "%d strippable (selected, %.3f weight) · %d kept (this joint is the "
            "main owner — reassign or repaint, never strip) · %d ignored "
            "(invisible dust under %.3f, %.3f weight — Skin > Prune Small "
            "Weights) · %d fine."
            % (who, n, report["carrying"], len(report["vertices"]),
               report["totalWeight"], len(report["kept"]), report["dust"],
               report["floor"], report["dustWeight"], report["near"])
        )
        for joint, reason in report["skipped"]:
            om.MGlobal.displayWarning("SkinBench: skipped %s (%s)" % (joint, reason))

        if report["dryRun"]:
            core.select_vertices(report["skinCluster"], report["vertices"])
            om.MGlobal.displayInfo(
                "SkinBench: preview only — those vertices are now SELECTED. "
                "Look, then run again with -s to strip."
            )
        else:
            om.MGlobal.displayInfo(
                "SkinBench: stripped %d vertex(es). Snapshot: %s"
                % (report["strippedVertices"], report["snapshot"])
            )
        self.setResult(len(report["vertices"]))


class IsolateCommand(_Command):
    kName = "skinBenchIsolate"

    @staticmethod
    def syntax():
        syntax = om.MSyntax()
        syntax.addFlag("-hi", "-hierarchy", om.MSyntax.kNoArg)
        syntax.addFlag("-iso", "-isolate", om.MSyntax.kNoArg)
        syntax.addFlag("-off", "-showAll", om.MSyntax.kNoArg)
        return syntax

    def doIt(self, args):
        parser = om.MArgParser(self.syntax(), args)

        if parser.isFlagSet("-off"):
            panel = core.isolate_selection(False)
            om.MGlobal.displayInfo("SkinBench: isolate OFF on %s." % panel)
            return

        report = core.select_influenced(
            include_hierarchy=parser.isFlagSet("-hi"),
            isolate=parser.isFlagSet("-iso"),
        )
        who = ", ".join(report["joints"][:4])
        if len(report["joints"]) > 4:
            who += " +%d more" % (len(report["joints"]) - 4)
        om.MGlobal.displayInfo(
            "SkinBench: %d vertex(es) driven by %s%s"
            % (len(report["vertices"]), who,
               " — isolated in %s" % report["panel"] if report["panel"] else "")
        )
        self.setResult(len(report["vertices"]))


_COMMANDS = (
    SkinBenchCommand,
    DetachCommand,
    ReattachCommand,
    MoveJointsCommand,
    BakeOrientCommand,
    RomCommand,
    CurlCommand,
    BleedCommand,
    IsolateCommand,
)


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------


def _menu_item(label, command, source="mel"):
    """Add one menu item, saying out loud which language its command is in.

    Left implicit, Maya ran these MEL strings as PYTHON and every one of them
    died with ``NameError: name 'skinBenchDetach' is not defined``. Nobody
    noticed for a week because the SkinBench window has a button for each and
    the window was what everyone used. Found during live testing on the ROM sweep.
    """
    cmds.menuItem(label=label, parent=MENU_NAME, command=command, sourceType=source)


def _build_menu():
    _remove_menu()
    parent = mel.eval("$tmp = $gMainWindow")
    cmds.menu(MENU_NAME, label="SkinBench", parent=parent, tearOff=True)

    _menu_item("Open SkinBench...", "import skinbench; skinbench.show()", "python")
    cmds.menuItem(divider=True, parent=MENU_NAME)

    _menu_item("Unlock joints (mjm ON)", "skinBenchMoveJoints -on")
    _menu_item("Lock in (mjm OFF)", "skinBenchMoveJoints -off")
    _menu_item("Bake rotate to jointOrient", "skinBenchBakeOrient")
    _menu_item("Bake rotate to jointOrient (hierarchy)", "skinBenchBakeOrient -hi")
    cmds.menuItem(divider=True, parent=MENU_NAME)

    _menu_item("ROM sweep: next pose", "skinBenchROM")
    _menu_item("ROM sweep: next pose (hierarchy)", "skinBenchROM -hi")
    _menu_item("ROM sweep: previous pose", "skinBenchROM -p")
    _menu_item("ROM sweep: end and reset", "skinBenchROM -r")
    cmds.menuItem(divider=True, parent=MENU_NAME)

    _menu_item("Curl chain +15 (X)", "skinBenchCurl -ax X")
    _menu_item("Curl chain +15 (Y)", "skinBenchCurl -ax Y")
    _menu_item("Curl chain +15 (Z)", "skinBenchCurl -ax Z")
    _menu_item("Uncurl 15 (same axis)", "skinBenchCurl -d -15")
    _menu_item("Straighten chain", "skinBenchCurl -s")
    cmds.menuItem(divider=True, parent=MENU_NAME)

    _menu_item("Select what this joint drives", "skinBenchIsolate")
    _menu_item("Select + isolate (hierarchy)", "skinBenchIsolate -hi -iso")
    _menu_item("Isolate off (show all)", "skinBenchIsolate -off")
    cmds.menuItem(divider=True, parent=MENU_NAME)

    _menu_item("Anti-bleed: preview on selected joint(s)", "skinBenchBleed")
    _menu_item("Anti-bleed: STRIP on selected joint(s)", "skinBenchBleed -s")
    cmds.menuItem(divider=True, parent=MENU_NAME)

    _menu_item("Detach (snapshot + unbind)", "skinBenchDetach")
    _menu_item("Reattach (newest snapshot)", "skinBenchReattach")
    _menu_item("Reattach, restore bind matrices", "skinBenchReattach -rbm")


def _remove_menu():
    if cmds.menu(MENU_NAME, query=True, exists=True):
        cmds.deleteUI(MENU_NAME, menu=True)


# ---------------------------------------------------------------------------
# plugin lifecycle
# ---------------------------------------------------------------------------


def initializePlugin(mobject):
    plugin = om.MFnPlugin(mobject, VENDOR, VERSION, "Any")
    for command in _COMMANDS:
        try:
            if "syntax" in command.__dict__:
                plugin.registerCommand(command.kName, command.creator, command.syntax)
            else:
                plugin.registerCommand(command.kName, command.creator)
        except Exception:
            om.MGlobal.displayError("SkinBench: failed to register '%s'." % command.kName)
            raise
    if not cmds.about(batch=True):
        _build_menu()


def uninitializePlugin(mobject):
    plugin = om.MFnPlugin(mobject)
    if not cmds.about(batch=True):
        _remove_menu()
    for command in reversed(_COMMANDS):
        try:
            plugin.deregisterCommand(command.kName)
        except Exception:
            om.MGlobal.displayError("SkinBench: failed to deregister '%s'." % command.kName)
            raise
