# -*- coding: utf-8 -*-
"""Headless smoke test for an extracted SkinBench share package."""

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import types

sys.dont_write_bytecode = True

EXPECTED_VERSION = "1.0.0-beta.1"
INSTALL_SUCCESS_MESSAGE = (
    "<hl>SkinBench</hl> installed — plug-in loaded from this folder; "
    "auto-load ON; shelf button ready."
)
INSTALL_RESTART_MESSAGE = (
    "<hl>SkinBench</hl> updated — restart Maya before using this version."
)


class FakeInstallerCmds(object):
    """Small Maya boundary fake for installer contract checks."""

    def __init__(self, app_dir, loaded=False, plugin_path=None, version=None,
                 load_result_path=None, load_result_version=None):
        self.app_dir = app_dir
        self.loaded = loaded
        self.plugin_path = plugin_path
        self.version = version
        self.load_result_path = load_result_path
        self.load_result_version = load_result_version
        self.load_calls = []
        self.autoload = False
        self.messages = []
        self.shelf_buttons = []

    def internalVar(self, userAppDir=False):
        assert userAppDir
        return self.app_dir + os.sep

    def pluginInfo(self, name, query=False, loaded=False, path=False,
                   version=False, edit=False, autoload=False):
        assert name == "skinBench.py"
        if query and loaded:
            return self.loaded
        if query and path:
            return self.plugin_path
        if query and version:
            return self.version
        if edit and autoload:
            self.autoload = True
            return None
        raise AssertionError("Unexpected pluginInfo arguments")

    def loadPlugin(self, plugin_path):
        self.load_calls.append(plugin_path)
        self.loaded = True
        self.plugin_path = self.load_result_path or plugin_path
        self.version = self.load_result_version or EXPECTED_VERSION

    def inViewMessage(self, **kwargs):
        self.messages.append(kwargs["amg"])

    def tabLayout(self, shelf_top, query=False, selectTab=False):
        assert shelf_top == "ShelfTop" and query and selectTab
        return "Shelf"

    def shelfLayout(self, shelf, query=False, childArray=False):
        assert shelf == "Shelf" and query and childArray
        return list(self.shelf_buttons)

    def objectTypeUI(self, child):
        return "shelfButton"

    def shelfButton(self, child=None, query=False, annotation=False, **kwargs):
        if query and annotation:
            return child["annotation"]
        button = {"annotation": annotation}
        self.shelf_buttons.append(button)
        return button


class FakeInstallerMel(object):
    @staticmethod
    def eval(expression):
        assert expression == "$tmp = $gShelfTopLevel"
        return "ShelfTop"


def _load_installer_with_fake_maya(root, fake_cmds):
    """Import the real installer with only Maya's external boundary faked."""
    previous_modules = {
        name: sys.modules.get(name) for name in ("maya", "maya.cmds", "maya.mel")
    }
    maya_module = types.ModuleType("maya")
    cmds_module = types.ModuleType("maya.cmds")
    mel_module = types.ModuleType("maya.mel")
    for name in dir(fake_cmds):
        if not name.startswith("_"):
            setattr(cmds_module, name, getattr(fake_cmds, name))
    mel_module.eval = FakeInstallerMel.eval
    maya_module.cmds = cmds_module
    maya_module.mel = mel_module
    sys.modules["maya"] = maya_module
    sys.modules["maya.cmds"] = cmds_module
    sys.modules["maya.mel"] = mel_module
    try:
        spec = importlib.util.spec_from_file_location(
            "skinbench_installer_contract", os.path.join(root, "install.py")
        )
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        return installer
    finally:
        for name, module in previous_modules.items():
            if module is None:
                del sys.modules[name]
            else:
                sys.modules[name] = module


def _assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError("%s: expected %r, found %r" % (label, expected, actual))


def _install_with_fake_maya(root, fake_cmds):
    installer = _load_installer_with_fake_maya(root, fake_cmds)
    installer.install()
    return installer


def run_installer_contract_tests(root):
    """Exercise clean and upgrade installation without a Maya GUI process."""
    temp_app_dir = tempfile.mkdtemp(prefix="skinbench-installer-contract-")
    try:
        expected_plugin = os.path.join(root, "plug-ins", "skinBench.py")

        old_root = os.path.join(os.path.dirname(root), "SkinBench-old")
        path_mismatch = FakeInstallerCmds(
            temp_app_dir,
            True,
            os.path.join(old_root, "plug-ins", "skinBench.py"),
            EXPECTED_VERSION,
        )
        _install_with_fake_maya(root, path_mismatch)
        _assert_equal(path_mismatch.load_calls, [], "path mismatch must not hot reload")
        _assert_equal(path_mismatch.autoload, True, "path mismatch autoload")
        _assert_equal(len(path_mismatch.shelf_buttons), 1, "path mismatch one shelf")
        _assert_equal(
            path_mismatch.messages, [INSTALL_RESTART_MESSAGE], "path mismatch message"
        )

        version_mismatch = FakeInstallerCmds(
            temp_app_dir, True, expected_plugin, "1.0.0"
        )
        _install_with_fake_maya(root, version_mismatch)
        _assert_equal(version_mismatch.load_calls, [], "version mismatch must not hot reload")
        _assert_equal(version_mismatch.autoload, True, "version mismatch autoload")
        _assert_equal(len(version_mismatch.shelf_buttons), 1, "version mismatch one shelf")
        _assert_equal(
            version_mismatch.messages,
            [INSTALL_RESTART_MESSAGE],
            "version mismatch message",
        )

        clean_path_mismatch = FakeInstallerCmds(
            temp_app_dir,
            load_result_path=os.path.join(old_root, "plug-ins", "skinBench.py"),
        )
        _install_with_fake_maya(root, clean_path_mismatch)
        _assert_equal(
            clean_path_mismatch.load_calls,
            [expected_plugin],
            "clean path mismatch exact-path load",
        )
        _assert_equal(clean_path_mismatch.autoload, True, "clean path mismatch autoload")
        _assert_equal(
            len(clean_path_mismatch.shelf_buttons),
            1,
            "clean path mismatch one shelf",
        )
        _assert_equal(
            clean_path_mismatch.messages,
            [INSTALL_RESTART_MESSAGE],
            "clean path mismatch message",
        )

        clean_version_mismatch = FakeInstallerCmds(
            temp_app_dir, load_result_version="1.0.0"
        )
        _install_with_fake_maya(root, clean_version_mismatch)
        _assert_equal(
            clean_version_mismatch.load_calls,
            [expected_plugin],
            "clean version mismatch exact-path load",
        )
        _assert_equal(
            clean_version_mismatch.autoload,
            True,
            "clean version mismatch autoload",
        )
        _assert_equal(
            len(clean_version_mismatch.shelf_buttons),
            1,
            "clean version mismatch one shelf",
        )
        _assert_equal(
            clean_version_mismatch.messages,
            [INSTALL_RESTART_MESSAGE],
            "clean version mismatch message",
        )

        clean = FakeInstallerCmds(temp_app_dir)
        _install_with_fake_maya(root, clean)
        _assert_equal(clean.load_calls, [expected_plugin], "clean exact-path load")
        _assert_equal(clean.autoload, True, "clean autoload")
        _assert_equal(len(clean.shelf_buttons), 1, "clean one shelf setup")
        _assert_equal(clean.messages, [INSTALL_SUCCESS_MESSAGE], "clean success message")
        _install_with_fake_maya(root, clean)
        _assert_equal(clean.load_calls, [expected_plugin], "repeat same-root load")
        _assert_equal(len(clean.shelf_buttons), 1, "repeat one shelf setup")

        current = FakeInstallerCmds(
            temp_app_dir, True, expected_plugin, EXPECTED_VERSION
        )
        _install_with_fake_maya(root, current)
        _assert_equal(current.load_calls, [], "same-root loaded state")
        _assert_equal(current.autoload, True, "same-root autoload")
        _assert_equal(len(current.shelf_buttons), 1, "same-root one shelf setup")
        _assert_equal(current.messages, [INSTALL_SUCCESS_MESSAGE], "same-root message")

        normalized_path = os.path.join(
            root, "plug-ins", "placeholder", "..", "skinBench.py"
        )
        normalized = FakeInstallerCmds(
            temp_app_dir, True, normalized_path, EXPECTED_VERSION
        )
        _install_with_fake_maya(root, normalized)
        _assert_equal(normalized.load_calls, [], "normalized exact-path state")
        _assert_equal(normalized.messages, [INSTALL_SUCCESS_MESSAGE], "normalized message")
        print("PASS installer_upgrade_contract")
    finally:
        shutil.rmtree(temp_app_dir, ignore_errors=True)


def run_nearest_selection_mirror_regression(core, cmds):
    """Nearest matching must grade the real pair and prove the write."""
    made = []
    try:
        cmds.select(clear=True)
        source = cmds.joint(name="sbMirror_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        target = cmds.joint(name="sbMirror_R", position=(-1.0, 0.0, 0.0))
        made.extend([source, target])

        mesh = cmds.polyPlane(
            name="sbMirror_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.append(mesh)
        vertex_count = cmds.polyEvaluate(mesh, vertex=True)
        positive = []
        negative = []
        for index in range(vertex_count):
            component = "%s.vtx[%d]" % (mesh, index)
            x_value = cmds.pointPosition(component, world=True)[0]
            (positive if x_value > 0.0 else negative).append(index)

        # Move only the negative side by 0.01 cm. There are deliberately no
        # exact reflected coordinates, but every intended nearest pair remains
        # well inside SkinBench's match budget.
        for index in negative:
            cmds.move(
                0.0,
                0.01,
                0.0,
                "%s.vtx[%d]" % (mesh, index),
                relative=True,
                worldSpace=True,
            )

        cluster = cmds.skinCluster(
            [source, target], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        for index in positive + negative:
            cmds.skinPercent(
                cluster,
                "%s.vtx[%d]" % (mesh, index),
                transformValue=[(source, 1.0), (target, 0.0)],
                normalize=True,
            )

        rounded = set()
        for index in range(vertex_count):
            point = cmds.pointPosition("%s.vtx[%d]" % (mesh, index), world=True)
            rounded.add(tuple(round(value, 3) for value in point))
        for index in positive:
            point = cmds.pointPosition("%s.vtx[%d]" % (mesh, index), world=True)
            reflected = (round(-point[0], 3), round(point[1], 3), round(point[2], 3))
            if reflected in rounded:
                raise AssertionError("nearest-mirror fixture unexpectedly has an exact twin")

        cmds.select(
            ["%s.vtx[%d]" % (mesh, index) for index in positive],
            replace=True,
        )
        exact_report = core.mirror_weights_on_selection(
            from_positive_x=True, match="exact"
        )[0]
        if (
            exact_report.get("checked") != 0
            or exact_report.get("worst", "missing") is not None
            or exact_report.get("attempted") != 0
        ):
            raise AssertionError(
                "an ungradeable exact mirror reported a fake score: %r"
                % exact_report
            )

        report = core.mirror_weights_on_selection(
            from_positive_x=True, match="nearest"
        )[0]

        if report.get("checked", 0) != len(negative):
            raise AssertionError(
                "nearest mirror did not grade its actual pairs: %r" % report
            )
        if report.get("worst") is None or report["worst"] > 1e-8:
            raise AssertionError("nearest mirror write did not match its source: %r" % report)
        if report.get("changed", 0) != len(negative):
            raise AssertionError(
                "nearest mirror did not prove changed rows against the backup: %r"
                % report
            )
        if report.get("written") != len(negative) or not report.get("snapshot"):
            raise AssertionError("nearest mirror write was not fully verified: %r" % report)
        print("PASS nearest_selection_mirror_regression -> %s" % report)
    finally:
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)


def run_nearest_collision_regression(core, cmds):
    """Ambiguous nearest claims must refuse before any weight changes."""
    cases = (
        (True, True, "positive_source_selection"),
        (True, False, "positive_destination_selection"),
        (False, True, "negative_source_selection"),
        (False, False, "negative_destination_selection"),
    )
    completed = []
    for from_positive_x, select_source, label in cases:
        made = []
        try:
            cmds.select(clear=True)
            left = cmds.joint(
                name="sbCollision_%s_L" % label,
                position=(1.0, 0.0, 0.0),
            )
            cmds.select(clear=True)
            right = cmds.joint(
                name="sbCollision_%s_R" % label,
                position=(-1.0, 0.0, 0.0),
            )
            made.extend([left, right])

            source_x = 1.0 if from_positive_x else -1.0
            if select_source:
                points = [
                    (source_x, 0.0, 0.0),
                    (source_x, 0.01, 0.0),
                    (-source_x, 0.005, 0.0),
                ]
                selected = [0, 1]
            else:
                points = [
                    (source_x, 0.005, 0.0),
                    (-source_x, 0.0, 0.0),
                    (-source_x, 0.01, 0.0),
                ]
                selected = [1, 2]

            mesh = cmds.polyCreateFacet(
                name="sbCollision_%s_mesh" % label,
                point=points,
            )[0]
            made.append(mesh)
            cluster = cmds.skinCluster(
                [left, right], mesh, toSelectedBones=True, normalizeWeights=1
            )[0]
            rows = ((1.0, 0.0), (0.25, 0.75), (0.6, 0.4))
            for index, values in enumerate(rows):
                cmds.skinPercent(
                    cluster,
                    "%s.vtx[%d]" % (mesh, index),
                    transformValue=[(left, values[0]), (right, values[1])],
                    normalize=True,
                )

            shape = core.deformed_shape(mesh)
            before, _ = core._weight_rows(cluster, shape)
            cmds.select(
                ["%s.vtx[%d]" % (mesh, index) for index in selected],
                replace=True,
            )
            report = core.mirror_weights_on_selection(
                from_positive_x=from_positive_x,
                match="nearest",
            )[0]
            after, _ = core._weight_rows(cluster, shape)

            if report.get("collisions") != 2:
                raise AssertionError(
                    "%s did not report both ambiguous pair claims: %r"
                    % (label, report)
                )
            if "ambiguous" not in (report.get("skipped") or "").lower():
                raise AssertionError("%s did not refuse the collision: %r" % (label, report))
            if report.get("snapshot") or before != after:
                raise AssertionError(
                    "%s changed weights or created a snapshot before refusing"
                    % label
                )
            completed.append(label)
        finally:
            cmds.select(clear=True)
            for node in made:
                if cmds.objExists(node):
                    cmds.delete(node)
    print("PASS nearest_collision_regression -> %s" % ", ".join(completed))


def run_nearest_duplicate_pair_regression(core, cmds):
    """Selecting both members of one pair must deduplicate, not refuse."""
    made = []
    try:
        cmds.select(clear=True)
        source = cmds.joint(name="sbDuplicate_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        target = cmds.joint(name="sbDuplicate_R", position=(-1.0, 0.0, 0.0))
        made.extend([source, target])
        mesh = cmds.polyCreateFacet(
            name="sbDuplicate_mesh",
            point=[(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        )[0]
        made.append(mesh)
        cluster = cmds.skinCluster(
            [source, target], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        for index, values in enumerate(((1.0, 0.0), (0.0, 1.0), (0.5, 0.5))):
            cmds.skinPercent(
                cluster,
                "%s.vtx[%d]" % (mesh, index),
                transformValue=[(source, values[0]), (target, values[1])],
                normalize=True,
            )
        cmds.select(["%s.vtx[0]" % mesh, "%s.vtx[1]" % mesh], replace=True)
        report = core.mirror_weights_on_selection(
            from_positive_x=True,
            match="nearest",
        )[0]
        expected = {
            "collisions": 0,
            "attempted": 1,
            "written": 1,
            "checked": 1,
        }
        wrong = {key: report.get(key) for key, value in expected.items()
                 if report.get(key) != value}
        if wrong or report.get("skipped"):
            raise AssertionError(
                "one legitimate pair was falsely refused instead of deduplicated: %r"
                % report
            )
        print("PASS nearest_duplicate_pair_regression -> %s" % report)
    finally:
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)


def run_leftover_drag_warning_regression(core, ui, cmds):
    """Opening-time scan must find a saved dent without changing it."""
    made = []
    try:
        cmds.select(clear=True)
        joint = cmds.joint(name="sbDrag_joint", position=(0.0, 0.0, 0.0))
        cmds.select(clear=True)
        mesh = cmds.polyPlane(
            name="sbDrag_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([joint, mesh])
        cluster = cmds.skinCluster(
            joint,
            mesh,
            toSelectedBones=True,
            normalizeWeights=1,
            name="zDragPolySkin",
        )[0]
        shape = cmds.listRelatives(mesh, shapes=True, noIntermediate=True, fullPath=True)[0]

        cmds.select(clear=True)
        failed_joint = cmds.joint(
            name="sbDrag_readFailJoint", position=(0.0, 0.0, 0.0)
        )
        cmds.select(clear=True)
        failed_mesh = cmds.polyPlane(
            name="sbDrag_readFailMesh",
            width=1.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([failed_joint, failed_mesh])
        cmds.skinCluster(
            failed_joint,
            failed_mesh,
            toSelectedBones=True,
            normalizeWeights=1,
            name="mDragReadFailPolySkin",
        )
        failed_shape = cmds.listRelatives(
            failed_mesh, shapes=True, noIntermediate=True, fullPath=True
        )[0]

        cmds.select(clear=True)
        surface_joint = cmds.joint(
            name="sbDrag_surfaceJoint", position=(0.0, 0.0, 0.0)
        )
        cmds.select(clear=True)
        surface = cmds.nurbsPlane(
            name="sbDrag_unsupportedSurface",
            axis=(0.0, 1.0, 0.0),
            width=1.0,
            lengthRatio=1.0,
            degree=1,
            patchesU=1,
            patchesV=1,
            constructionHistory=False,
        )[0]
        made.extend([surface_joint, surface])
        cmds.skinCluster(
            surface_joint,
            surface,
            toSelectedBones=True,
            normalizeWeights=1,
            name="aDragNurbsSkin",
        )
        surface_shape = cmds.listRelatives(
            surface, shapes=True, noIntermediate=True, fullPath=True
        )[0]

        cmds.setAttr("%s.pnts[0]" % shape, 0.125, 0.0, 0.0, type="double3")
        cmds.setAttr("%s.pnts[1]" % shape, 1e-9, 0.0, 0.0, type="double3")
        cmds.setAttr("%s.pnts[2]" % shape, 0.25, 0.0, 0.0, type="double3")
        before = [
            cmds.getAttr("%s.pnts[%d]" % (shape, index))[0]
            for index in (0, 1, 2)
        ]

        report = core.leftover_drag_report()
        after = [
            cmds.getAttr("%s.pnts[%d]" % (shape, index))[0]
            for index in (0, 1, 2)
        ]
        if before != after:
            raise AssertionError("leftover-drag scan changed the mesh")
        expected = {
            "vertices": 2,
            "shapes": [{"shape": shape.split("|")[-1], "vertices": 2}],
            "skipped": [{
                "shape": surface_shape.split("|")[-1],
                "reason": "not a polygon mesh",
            }],
        }
        if report != expected:
            raise AssertionError(
                "leftover-drag scan missed or miscounted the saved dent: %r" % report
            )

        original_get_attr = core.cmds.getAttr

        def fail_one_polygon(attribute, *args, **kwargs):
            if attribute == failed_shape + ".pnts":
                raise ValueError("forced polygon point-read failure")
            return original_get_attr(attribute, *args, **kwargs)

        try:
            core.cmds.getAttr = fail_one_polygon
            forced_report = core.leftover_drag_report()
        finally:
            core.cmds.getAttr = original_get_attr
        failed_rows = [
            item for item in forced_report.get("skipped", [])
            if item.get("shape") == failed_shape.split("|")[-1]
        ]
        if (
            forced_report.get("vertices") != 2
            or forced_report.get("shapes") != expected["shapes"]
            or failed_rows != [{
                "shape": failed_shape.split("|")[-1],
                "reason": "point offsets unavailable",
            }]
        ):
            raise AssertionError(
                "one polygon read failure erased or corrupted the good result: %r"
                % forced_report
            )

        message = ui.leftover_drag_warning(report)
        required = (
            "2 leftover vertex drags",
            shape.split("|")[-1],
            "Undo any drag still on the mesh",
            "before saving or exporting",
            surface_shape.split("|")[-1],
            "could not be inspected",
        )
        missing = [text for text in required if text not in message]
        if missing:
            raise AssertionError(
                "leftover-drag warning is not actionable; missing %r in %r"
                % (missing, message)
            )

        class WarningProbe(object):
            _warn_leftover_drags = ui.SkinBenchWindow._warn_leftover_drags

            def __init__(self):
                self.lines = []

            def _log(self, text, alert=False):
                self.lines.append((text, alert))

        probe = WarningProbe()
        probe._warn_leftover_drags()
        if probe.lines != [("! %s" % message, True)]:
            raise AssertionError(
                "window-open warning was not raised through the real UI path: %r"
                % (probe.lines,)
            )

        class ClearProbe(object):
            _clear_drags = ui.SkinBenchWindow._clear_drags

            def __init__(self):
                self.lines = []

            def _log(self, text, alert=False):
                self.lines.append((text, alert))

        before_weights, _ = core._weight_rows(cluster, shape)
        cmds.select(clear=True)
        clear_probe = ClearProbe()
        clear_probe._clear_drags()
        after_weights, _ = core._weight_rows(cluster, shape)
        if before_weights != after_weights:
            raise AssertionError("clear-drag action changed skin weights")
        remaining = cmds.getAttr("%s.pnts[0]" % shape)[0]
        if max(abs(value) for value in remaining) > core.WEIGHT_EPSILON:
            raise AssertionError(
                "mixed-scene clear action left the reported polygon dent: %r; log=%r"
                % (remaining, clear_probe.lines)
            )
        second_remaining = cmds.getAttr("%s.pnts[2]" % shape)[0]
        if max(abs(value) for value in second_remaining) > core.WEIGHT_EPSILON:
            raise AssertionError(
                "clear action left the second reported polygon dent: %r"
                % (second_remaining,)
            )
        cmds.undo()
        restored = [
            cmds.getAttr("%s.pnts[%d]" % (shape, index))[0]
            for index in (0, 2)
        ]
        expected_restored = [before[0], before[2]]
        if restored != expected_restored:
            raise AssertionError(
                "one Ctrl+Z did not restore every cleared drag: got %r, expected %r"
                % (restored, expected_restored)
            )
        cmds.redo()
        redone = [
            cmds.getAttr("%s.pnts[%d]" % (shape, index))[0]
            for index in (0, 2)
        ]
        if any(
            max(abs(value) for value in point) > core.WEIGHT_EPSILON
            for point in redone
        ):
            raise AssertionError(
                "one redo did not reapply the grouped drag cleanup: %r" % redone
            )
        clear_text = "\n".join(text for text, _alert in clear_probe.lines)
        clear_required = (
            "Put 2 dragged vertex(es)",
            "every skinned polygon mesh in the scene",
            shape.split("|")[-1],
            surface_shape.split("|")[-1],
            "could not be inspected",
        )
        missing = [text for text in clear_required if text not in clear_text]
        if missing:
            raise AssertionError(
                "mixed-scene clear action did not name its result; missing %r in %r"
                % (missing, clear_probe.lines)
            )

        uncertain_probe = ClearProbe()
        uncertain_probe._clear_drags()
        uncertain_text = "\n".join(text for text, _alert in uncertain_probe.lines)
        if (
            "No drags left" in uncertain_text
            or surface_shape.split("|")[-1] not in uncertain_text
        ):
            raise AssertionError(
                "an unverified shape was incorrectly declared clean: %r"
                % (uncertain_probe.lines,)
            )

        cmds.delete(surface)
        cmds.delete(surface_joint)
        empty = core.leftover_drag_report()
        if empty != {"vertices": 0, "shapes": [], "skipped": []}:
            raise AssertionError("cleared point offsets still raised a warning: %r" % empty)
        silent_probe = WarningProbe()
        silent_probe._warn_leftover_drags()
        if silent_probe.lines:
            raise AssertionError("clean mesh raised a window-open warning: %r" % silent_probe.lines)
        print("PASS leftover_drag_warning_regression -> %s" % message)
    finally:
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)


def run_clear_log_pane_regression(ui):
    """The screen-only clear action must target the visible log widget."""

    class LogView(object):
        def __init__(self):
            self.clear_count = 0

        def clear(self):
            self.clear_count += 1

    class ClearLogProbe(object):
        _clear_log_pane = ui.SkinBenchWindow._clear_log_pane

        def __init__(self):
            self.log_view = LogView()
            self.lines = []

        def _log(self, text, alert=False):
            self.lines.append((text, alert))

    probe = ClearLogProbe()
    try:
        probe._clear_log_pane()
    except AttributeError as error:
        raise AssertionError(
            "clear-screen action targeted a missing widget: %s" % error
        )
    if probe.log_view.clear_count != 1:
        raise AssertionError(
            "clear-screen action did not clear the visible log exactly once"
        )
    if len(probe.lines) != 1 or "Screen cleared" not in probe.lines[0][0]:
        raise AssertionError(
            "clear-screen action did not preserve the on-disk-log notice: %r"
            % (probe.lines,)
        )
    print("PASS clear_log_pane_regression")


def run_selection_mirror_message_regression(ui):
    """An empty nearest result must never look like a perfect pairing."""
    empty_nearest = {
        "checked": 0,
        "worst": None,
        "match": "nearest",
        "worstGap": 0.0,
        "unmatched": 2,
    }
    empty_text = "\n".join(ui.selection_mirror_pair_messages(empty_nearest))
    if "CANNOT CHECK" not in empty_text:
        raise AssertionError("empty nearest result lost its NOT-PASS warning: %r" % empty_text)
    if "no safe partner" not in empty_text.lower():
        raise AssertionError("empty nearest result has no useful recovery text: %r" % empty_text)
    if "0.0000" in empty_text or "switch 'Pair by' to NEAREST" in empty_text:
        raise AssertionError("empty nearest result printed a fake score or wrong advice: %r" % empty_text)

    checked_nearest = dict(empty_nearest, checked=2, worst=0.0, worstGap=0.01234, unmatched=0)
    checked_text = "\n".join(ui.selection_mirror_pair_messages(checked_nearest))
    if "0.0123" not in checked_text or "actual pair" not in checked_text:
        raise AssertionError("checked nearest result lost its real score: %r" % checked_text)

    empty_exact = dict(empty_nearest, match="exact")
    exact_text = "\n".join(ui.selection_mirror_pair_messages(empty_exact))
    if "switch 'Pair by' to NEAREST" not in exact_text:
        raise AssertionError("exact unmatched result lost its valid recovery advice: %r" % exact_text)
    print("PASS selection_mirror_message_regression")


def run_apply_weights_normalization_cleanup_regression(core, cmds):
    """A failed pour must put the skinCluster normalization mode back."""
    made = []
    temp_dir = tempfile.mkdtemp(prefix="skinbench-apply-weights-cleanup-")
    try:
        cmds.select(clear=True)
        left = cmds.joint(name="sbApplyCleanup_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        right = cmds.joint(name="sbApplyCleanup_R", position=(-1.0, 0.0, 0.0))
        mesh = cmds.polyPlane(
            name="sbApplyCleanup_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([left, right, mesh])
        cluster = cmds.skinCluster(
            [left, right], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        cmds.setAttr(cluster + ".normalizeWeights", 2)
        expected_normalize = cmds.getAttr(cluster + ".normalizeWeights")
        _, snapshot = core.save_snapshot(
            mesh, os.path.join(temp_dir, "forced-pour-failure.json")
        )

        # This corrupts only the in-memory fixture after all apply-time target
        # checks. _pour_weights reaches it after normalization has been disabled.
        snapshot["weights"]["influence"][0] = "forced failure"
        try:
            core.apply_weights(mesh, snapshot)
        except TypeError:
            pass
        else:
            raise AssertionError("forced snapshot corruption did not reach _pour_weights")

        actual_normalize = cmds.getAttr(cluster + ".normalizeWeights")
        if actual_normalize != expected_normalize:
            raise AssertionError(
                "failed apply_weights left normalizeWeights at %r, expected %r"
                % (actual_normalize, expected_normalize)
            )
        print("PASS apply_weights_normalization_cleanup_regression")
    finally:
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_apply_weights_success_restore_order_regression(core, cmds):
    """A successful missing-influence apply restores once before normalizing."""
    made = []
    temp_dir = tempfile.mkdtemp(prefix="skinbench-apply-weights-order-")
    try:
        cmds.select(clear=True)
        left = cmds.joint(name="sbApplyOrder_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        right = cmds.joint(name="sbApplyOrder_R", position=(-1.0, 0.0, 0.0))
        mesh = cmds.polyPlane(
            name="sbApplyOrder_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([left, right, mesh])
        cluster = cmds.skinCluster(
            [left, right], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        cmds.setAttr(cluster + ".normalizeWeights", 2)
        expected_normalize = cmds.getAttr(cluster + ".normalizeWeights")
        _, snapshot = core.save_snapshot(
            mesh, os.path.join(temp_dir, "missing-influence.json")
        )
        snapshot["influences"].append("sbApplyOrder_missing")
        snapshot["influenceUuids"].append("")

        events = []
        real_set_if_settable = core._set_if_settable
        real_skin_percent = core.cmds.skinPercent

        def recording_set_if_settable(plug, value):
            if plug == cluster + ".normalizeWeights":
                events.append(("normalizeWeights", value))
            return real_set_if_settable(plug, value)

        def recording_skin_percent(*args, **kwargs):
            if kwargs.get("normalize"):
                events.append(("skinPercent", True))
            return real_skin_percent(*args, **kwargs)

        core._set_if_settable = recording_set_if_settable
        core.cmds.skinPercent = recording_skin_percent
        try:
            core.apply_weights(mesh, snapshot)
        finally:
            core._set_if_settable = real_set_if_settable
            core.cmds.skinPercent = real_skin_percent

        expected_events = [
            ("normalizeWeights", 0),
            ("normalizeWeights", expected_normalize),
            ("skinPercent", True),
        ]
        if events != expected_events:
            raise AssertionError(
                "successful apply changed normalization write ordering: %r" % (events,)
            )
        print("PASS apply_weights_success_restore_order_regression")
    finally:
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_apply_weights_cleanup_failure_regression(core, cmds):
    """A failed pour keeps its error when restoration and closeChunk fail too."""
    made = []
    temp_dir = tempfile.mkdtemp(prefix="skinbench-apply-weights-cleanup-failure-")
    chunk_open = False
    try:
        cmds.select(clear=True)
        left = cmds.joint(name="sbApplyFailure_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        right = cmds.joint(name="sbApplyFailure_R", position=(-1.0, 0.0, 0.0))
        mesh = cmds.polyPlane(
            name="sbApplyFailure_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([left, right, mesh])
        cluster = cmds.skinCluster(
            [left, right], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        cmds.setAttr(cluster + ".normalizeWeights", 2)
        expected_normalize = cmds.getAttr(cluster + ".normalizeWeights")
        _, snapshot = core.save_snapshot(
            mesh, os.path.join(temp_dir, "forced-pour-failure.json")
        )
        snapshot["weights"]["influence"][0] = "forced failure"

        close_attempts = []
        real_set_if_settable = core._set_if_settable
        real_undo_info = core.cmds.undoInfo

        def failing_restore(plug, value):
            if plug == cluster + ".normalizeWeights" and value == expected_normalize:
                raise RuntimeError("forced normalization restore failure")
            return real_set_if_settable(plug, value)

        def failing_close_chunk(**kwargs):
            if kwargs.get("closeChunk"):
                close_attempts.append(True)
                raise RuntimeError("forced apply closeChunk failure")
            return real_undo_info(**kwargs)

        core._set_if_settable = failing_restore
        core.cmds.undoInfo = failing_close_chunk
        chunk_open = True
        try:
            core.apply_weights(mesh, snapshot)
        except TypeError:
            pass
        except Exception as error:
            raise AssertionError("apply cleanup masked the pour failure: %s" % error)
        else:
            raise AssertionError("forced snapshot corruption did not reach _pour_weights")
        finally:
            core._set_if_settable = real_set_if_settable
            core.cmds.undoInfo = real_undo_info

        if close_attempts != [True]:
            raise AssertionError("apply cleanup did not attempt closeChunk")
        print("PASS apply_weights_cleanup_failure_regression")
    finally:
        if chunk_open:
            cmds.undoInfo(closeChunk=True)
            chunk_open = False
        if cmds.objExists(cluster + ".normalizeWeights"):
            cmds.setAttr(cluster + ".normalizeWeights", expected_normalize)
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_apply_weights_close_failure_regression(core, cmds):
    """A close-only failure surfaces after a successful weight application."""
    made = []
    cluster = None
    expected_normalize = None
    chunk_open = False
    temp_dir = tempfile.mkdtemp(prefix="skinbench-apply-weights-close-failure-")
    try:
        cmds.select(clear=True)
        left = cmds.joint(name="sbApplyClose_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        right = cmds.joint(name="sbApplyClose_R", position=(-1.0, 0.0, 0.0))
        mesh = cmds.polyPlane(
            name="sbApplyClose_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([left, right, mesh])
        cluster = cmds.skinCluster(
            [left, right], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        cmds.setAttr(cluster + ".normalizeWeights", 2)
        expected_normalize = cmds.getAttr(cluster + ".normalizeWeights")
        _, snapshot = core.save_snapshot(
            mesh, os.path.join(temp_dir, "close-only-failure.json")
        )

        close_attempts = []
        real_undo_info = core.cmds.undoInfo

        def failing_close_chunk(**kwargs):
            if kwargs.get("closeChunk"):
                close_attempts.append(True)
                raise RuntimeError("forced apply close-only failure")
            return real_undo_info(**kwargs)

        core.cmds.undoInfo = failing_close_chunk
        chunk_open = True
        try:
            core.apply_weights(mesh, snapshot)
        except RuntimeError as error:
            if str(error) != "forced apply close-only failure":
                raise AssertionError("apply close failure changed unexpectedly: %s" % error)
        else:
            raise AssertionError("apply close-only failure did not reach the caller")
        finally:
            core.cmds.undoInfo = real_undo_info

        if close_attempts != [True]:
            raise AssertionError("apply close-only fixture did not attempt closeChunk")
        if cmds.getAttr(cluster + ".normalizeWeights") != expected_normalize:
            raise AssertionError("apply close-only fixture did not restore normalization")
        print("PASS apply_weights_close_failure_regression")
    finally:
        if chunk_open:
            cmds.undoInfo(closeChunk=True)
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_mirror_joints_mode_cleanup_regression(core, cmds):
    """A failed mirror must turn Move Skinned Joints mode back off."""
    made = []
    target = None
    try:
        cmds.select(clear=True)
        source = cmds.joint(name="sbMirrorCleanup_L", position=(2.0, 0.0, 0.0))
        cmds.select(clear=True)
        target = cmds.joint(name="sbMirrorCleanup_R", position=(-1.0, 0.0, 0.0))
        target = cmds.ls(target, long=True)[0]
        mesh = cmds.polyPlane(
            name="sbMirrorCleanup_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([source, target, mesh])
        cluster = cmds.skinCluster(
            [source, target], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]
        affected = set(cmds.listConnections(target, type="skinCluster") or [])
        if cluster not in affected:
            raise AssertionError("mirror cleanup fixture did not find its affected skinCluster")
        real_xform = core.cmds.xform
        real_set_move_joints_mode = core.set_move_joints_mode
        mode_calls = []

        def recording_set_move_joints_mode(clusters, enabled, freshen=True):
            cluster_list = list(clusters)
            mode_calls.append((set(cluster_list), enabled, freshen))
            return real_set_move_joints_mode(cluster_list, enabled, freshen)

        def failing_target_xform(*args, **kwargs):
            if args and args[0] == target and "matrix" in kwargs:
                raise RuntimeError("forced target transform failure")
            return real_xform(*args, **kwargs)

        # Maya's xform bypasses both locked and connected destination plugs.
        # Keep the actual Maya mode transition real and force only its next
        # transform boundary to fail.
        core.cmds.xform = failing_target_xform
        core.set_move_joints_mode = recording_set_move_joints_mode
        try:
            core.mirror_joints([source])
        except RuntimeError as error:
            if str(error) != "forced target transform failure":
                raise AssertionError("mirror cleanup masked the original failure: %s" % error)
        else:
            raise AssertionError("forced target transform did not fail after mirror mode enabled")
        finally:
            core.cmds.xform = real_xform
            core.set_move_joints_mode = real_set_move_joints_mode

        if not any(enabled and clusters == affected
                   for clusters, enabled, _ in mode_calls):
            raise AssertionError(
                "mirror failure did not occur while Move Skinned Joints mode was enabled"
            )
        cleanup_calls = [
            (clusters, freshen) for clusters, enabled, freshen in mode_calls if not enabled
        ]
        cleaned = set()
        for clusters, freshen in cleanup_calls:
            if freshen:
                raise AssertionError("mirror cleanup freshened bind poses after a failed mirror")
            cleaned.update(clusters)
        missing_cleanup = sorted(affected - cleaned)
        if missing_cleanup:
            raise AssertionError(
                "failed mirror left Move Skinned Joints mode enabled on %s"
                % ", ".join(missing_cleanup)
            )
        print("PASS mirror_joints_mode_cleanup_regression")
    finally:
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)


def run_mirror_joints_cleanup_failure_regression(core, cmds):
    """A failed mirror keeps its error when mode cleanup and closeChunk fail."""
    made = []
    target = None
    cluster = None
    chunk_open = False
    try:
        cmds.select(clear=True)
        source = cmds.joint(name="sbMirrorFailure_L", position=(2.0, 0.0, 0.0))
        cmds.select(clear=True)
        target = cmds.joint(name="sbMirrorFailure_R", position=(-1.0, 0.0, 0.0))
        target = cmds.ls(target, long=True)[0]
        mesh = cmds.polyPlane(
            name="sbMirrorFailure_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([source, target, mesh])
        cluster = cmds.skinCluster(
            [source, target], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]

        close_attempts = []
        real_xform = core.cmds.xform
        real_set_move_joints_mode = core.set_move_joints_mode
        real_undo_info = core.cmds.undoInfo

        def failing_target_xform(*args, **kwargs):
            if args and args[0] == target and "matrix" in kwargs:
                raise RuntimeError("forced target transform failure")
            return real_xform(*args, **kwargs)

        def failing_mode_cleanup(clusters, enabled, freshen=True):
            if not enabled:
                raise RuntimeError("forced Move Skinned Joints cleanup failure")
            return real_set_move_joints_mode(clusters, enabled, freshen)

        def failing_close_chunk(**kwargs):
            if kwargs.get("closeChunk"):
                close_attempts.append(True)
                raise RuntimeError("forced mirror closeChunk failure")
            return real_undo_info(**kwargs)

        core.cmds.xform = failing_target_xform
        core.set_move_joints_mode = failing_mode_cleanup
        core.cmds.undoInfo = failing_close_chunk
        chunk_open = True
        try:
            core.mirror_joints([source])
        except RuntimeError as error:
            if str(error) != "forced target transform failure":
                raise AssertionError("mirror cleanup masked the transform failure: %s" % error)
        else:
            raise AssertionError("forced target transform did not fail after mirror mode enabled")
        finally:
            core.cmds.xform = real_xform
            core.set_move_joints_mode = real_set_move_joints_mode
            core.cmds.undoInfo = real_undo_info

        if close_attempts != [True]:
            raise AssertionError("mirror cleanup did not attempt closeChunk")
        print("PASS mirror_joints_cleanup_failure_regression")
    finally:
        if cluster and cmds.objExists(cluster):
            real_set_move_joints_mode([cluster], False, freshen=False)
        if chunk_open:
            real_undo_info(closeChunk=True)
            chunk_open = False
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)


def run_mirror_joints_close_failure_regression(core, cmds):
    """A close-only failure surfaces after a successful joint mirror."""
    made = []
    cluster = None
    chunk_open = False
    try:
        cmds.select(clear=True)
        source = cmds.joint(name="sbMirrorClose_L", position=(1.0, 0.0, 0.0))
        cmds.select(clear=True)
        target = cmds.joint(name="sbMirrorClose_R", position=(-1.0, 0.0, 0.0))
        target = cmds.ls(target, long=True)[0]
        mesh = cmds.polyPlane(
            name="sbMirrorClose_mesh",
            width=2.0,
            height=1.0,
            subdivisionsX=1,
            subdivisionsY=1,
        )[0]
        made.extend([source, target, mesh])
        cluster = cmds.skinCluster(
            [source, target], mesh, toSelectedBones=True, normalizeWeights=1
        )[0]

        close_attempts = []
        mode_calls = []
        undo_state = {"depth": 0, "mirror_depth": None}
        real_set_move_joints_mode = core.set_move_joints_mode
        real_undo_info = core.cmds.undoInfo

        def recording_set_move_joints_mode(clusters, enabled, freshen=True):
            mode_calls.append((set(clusters), enabled, freshen))
            return real_set_move_joints_mode(clusters, enabled, freshen)

        def failing_close_chunk(**kwargs):
            if kwargs.get("openChunk"):
                result = real_undo_info(**kwargs)
                undo_state["depth"] += 1
                if kwargs.get("chunkName") == "SkinBench mirror joints":
                    undo_state["mirror_depth"] = undo_state["depth"]
                return result
            if kwargs.get("closeChunk") and (
                    undo_state["depth"] == undo_state["mirror_depth"]):
                close_attempts.append(True)
                raise RuntimeError("forced mirror close-only failure")
            result = real_undo_info(**kwargs)
            if kwargs.get("closeChunk"):
                undo_state["depth"] -= 1
            return result

        core.set_move_joints_mode = recording_set_move_joints_mode
        core.cmds.undoInfo = failing_close_chunk
        chunk_open = True
        try:
            core.mirror_joints([source])
        except RuntimeError as error:
            if str(error) != "forced mirror close-only failure":
                raise AssertionError("mirror close failure changed unexpectedly: %s" % error)
        else:
            raise AssertionError("mirror close-only failure did not reach the caller")
        finally:
            core.set_move_joints_mode = real_set_move_joints_mode
            core.cmds.undoInfo = real_undo_info

        expected_calls = [
            ({cluster}, True, True),
            ({cluster}, False, False),
        ]
        if mode_calls != expected_calls:
            raise AssertionError("mirror close-only fixture changed normal mode cleanup: %r" % mode_calls)
        if close_attempts != [True]:
            raise AssertionError("mirror close-only fixture did not attempt closeChunk")
        print("PASS mirror_joints_close_failure_regression")
    finally:
        if chunk_open:
            real_undo_info(closeChunk=True)
        cmds.select(clear=True)
        for node in made:
            if cmds.objExists(node):
                cmds.delete(node)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package_root")
    parser.add_argument("--installer-contract-only", action="store_true")
    parser.add_argument(
        "--cleanup-regression",
        choices=(
            "apply-weights",
            "apply-weights-cleanup",
            "apply-weights-close",
            "mirror-joints",
            "mirror-joints-cleanup",
            "mirror-joints-close",
        ),
    )
    args = parser.parse_args()

    root = os.path.abspath(args.package_root)
    run_installer_contract_tests(root)
    if args.installer_contract_only:
        return 0
    scripts = os.path.join(root, "scripts")
    plugin = os.path.join(root, "plug-ins", "skinBench.py")
    required = [
        os.path.join(root, "install.py"),
        plugin,
        os.path.join(scripts, "skinbench", "__init__.py"),
        os.path.join(scripts, "skinbench", "compat.py"),
        os.path.join(scripts, "skinbench", "core.py"),
        os.path.join(scripts, "skinbench", "ui.py"),
    ]
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise RuntimeError("Missing package files: %s" % missing)

    temp_maya_root = tempfile.mkdtemp(prefix="skinbench-smoke-maya-")
    os.environ["MAYA_APP_DIR"] = temp_maya_root
    os.environ["SKINBENCH_LOG_DIR"] = os.path.join(temp_maya_root, "logs")
    sys.path.insert(0, scripts)

    import maya.standalone

    initialized = False
    try:
        maya.standalone.initialize(name="python")
        initialized = True

        import maya.cmds as cmds
        import skinbench
        from skinbench import core, ui  # noqa: F401

        installer_spec = importlib.util.spec_from_file_location(
            "skinbench_smoke_installer", os.path.join(root, "install.py")
        )
        installer = importlib.util.module_from_spec(installer_spec)
        installer_spec.loader.exec_module(installer)
        if installer.VERSION != EXPECTED_VERSION:
            raise RuntimeError(
                "Installer version mismatch: expected %s, found %s"
                % (EXPECTED_VERSION, installer.VERSION)
            )
        if skinbench.__version__ != EXPECTED_VERSION:
            raise RuntimeError(
                "Package version mismatch: expected %s, found %s"
                % (EXPECTED_VERSION, skinbench.__version__)
            )

        module_file = os.path.normcase(os.path.abspath(
            installer.write_module_file(root)
        ))
        temp_root_path = os.path.normcase(os.path.abspath(temp_maya_root))
        if os.path.commonpath([temp_root_path, module_file]) != temp_root_path:
            raise RuntimeError("Installer module file escaped the temporary Maya profile")
        expected_module = "+ skinbench %s %s\n" % (
            EXPECTED_VERSION, root.replace("\\", "/")
        )
        with open(module_file, "r") as module_handle:
            actual_module = module_handle.read()
        if actual_module != expected_module:
            raise RuntimeError("Installer module file content mismatch")
        print("PASS installer_module -> %s" % module_file)

        class SettingsProbe(object):
            _settings = ui.SkinBenchWindow._settings
            _restore_ui_scale = ui.SkinBenchWindow._restore_ui_scale
            _restore_log_look = ui.SkinBenchWindow._restore_log_look
            _remember_log_look = ui.SkinBenchWindow._remember_log_look
            _restore_patch_dials = ui.SkinBenchWindow._restore_patch_dials
            _remember_patch_dials = ui.SkinBenchWindow._remember_patch_dials

            def __init__(self):
                self._log_font = "Smoke default font"
                self._log_size = 13
                self._log_colour = ("#d6d6d6", "#232323")
                self.log_look_applied = False

            def _apply_log_look(self):
                self.log_look_applied = True

        settings_probe = SettingsProbe()
        settings = settings_probe._settings()
        expected_settings_dir = os.path.normcase(os.path.normpath(
            os.path.join(cmds.internalVar(userAppDir=True), "skinbench")
        ))
        settings_path = os.path.normcase(os.path.normpath(settings.fileName()))
        print("CHECK settings_format -> %s" % settings.format())
        print("CHECK settings_path -> %s" % settings_path)
        if settings.format() != ui.QtCore.QSettings.IniFormat:
            raise RuntimeError("SkinBench settings are not using IniFormat")
        if os.path.basename(settings_path) != "settings.ini":
            raise RuntimeError("SkinBench settings filename is not settings.ini")
        if os.path.commonpath([expected_settings_dir, settings_path]) != expected_settings_dir:
            raise RuntimeError("SkinBench settings escaped the temporary Maya profile")
        if os.path.commonpath([temp_root_path, settings_path]) != temp_root_path:
            raise RuntimeError("SkinBench settings escaped the actual temporary root")
        persisted_literal = "skinbench-fresh-settings-read"
        ui_scale_literal = 1.734567
        settings.setValue("smoke/localIni", persisted_literal)
        settings.setValue("ui/scale", ui_scale_literal)
        settings.sync()
        if not os.path.isfile(settings_path):
            raise RuntimeError("SkinBench settings did not sync to the local INI file")
        reopened = ui.QtCore.QSettings(settings_path, ui.QtCore.QSettings.IniFormat)
        if reopened.value("smoke/localIni") != persisted_literal:
            raise RuntimeError("SkinBench settings value did not survive a fresh read")
        ui._SCALE = 0.812345
        settings_probe._restore_ui_scale()
        print("CHECK restored_ui_scale -> %s" % ui._SCALE)
        if abs(ui._SCALE - ui_scale_literal) > 1e-9:
            raise RuntimeError("SkinBench UI scale did not restore from the local INI")
        print("PASS local_ini_settings -> %s" % settings_path)

        del reopened
        del settings
        shutil.rmtree(expected_settings_dir)
        with open(expected_settings_dir, "w") as collision_file:
            collision_file.write("blocks the SkinBench settings directory")
        ui._SCALE = 1.15
        settings_probe._restore_ui_scale()
        settings_probe._restore_log_look()
        settings_probe._restore_patch_dials()
        settings_probe._remember_log_look()
        settings_probe._remember_patch_dials()
        if abs(ui._SCALE - 1.15) > 1e-9:
            raise RuntimeError("Failed settings path changed the UI scale default")
        if settings_probe._log_size != 13 or not settings_probe.log_look_applied:
            raise RuntimeError("Failed settings path did not preserve log defaults")
        print("PASS settings_path_failure -> defaults preserved")
        os.remove(expected_settings_dir)
        os.makedirs(expected_settings_dir)

        run_nearest_selection_mirror_regression(core, cmds)
        run_nearest_collision_regression(core, cmds)
        run_nearest_duplicate_pair_regression(core, cmds)
        run_leftover_drag_warning_regression(core, ui, cmds)
        run_clear_log_pane_regression(ui)
        run_selection_mirror_message_regression(ui)
        if args.cleanup_regression == "apply-weights":
            run_apply_weights_normalization_cleanup_regression(core, cmds)
            run_apply_weights_success_restore_order_regression(core, cmds)
            return 0
        if args.cleanup_regression == "apply-weights-cleanup":
            run_apply_weights_cleanup_failure_regression(core, cmds)
            return 0
        if args.cleanup_regression == "apply-weights-close":
            run_apply_weights_close_failure_regression(core, cmds)
            return 0
        if args.cleanup_regression == "mirror-joints":
            run_mirror_joints_mode_cleanup_regression(core, cmds)
            return 0
        if args.cleanup_regression == "mirror-joints-cleanup":
            run_mirror_joints_cleanup_failure_regression(core, cmds)
            return 0
        if args.cleanup_regression == "mirror-joints-close":
            run_mirror_joints_close_failure_regression(core, cmds)
            return 0
        run_apply_weights_normalization_cleanup_regression(core, cmds)
        run_apply_weights_success_restore_order_regression(core, cmds)
        run_apply_weights_cleanup_failure_regression(core, cmds)
        run_apply_weights_close_failure_regression(core, cmds)
        run_mirror_joints_mode_cleanup_regression(core, cmds)
        run_mirror_joints_cleanup_failure_regression(core, cmds)
        run_mirror_joints_close_failure_regression(core, cmds)

        checks = [
            core.bind_pose_selftest,
            core.mirror_selftest,
            core.rom_selftest,
            core.heal_selftest,
            core.drag_selftest,
            core.patch_selftest,
            core.bleed_selftest,
            core.grow_selftest,
            core.pose_selftest,
        ]
        for check in checks:
            result = check()
            print("PASS %s%s" % (
                check.__name__, " -> %s" % result if result is not None else ""
            ))

        print("PASS weight_write_selftest -> %s" % core.weight_write_selftest(True))
        cmds.loadPlugin(plugin)
        if not cmds.pluginInfo(plugin, query=True, loaded=True):
            raise RuntimeError("SkinBench plugin did not load")
        plugin_version = cmds.pluginInfo(plugin, query=True, version=True)
        if plugin_version != EXPECTED_VERSION:
            raise RuntimeError(
                "Loaded plugin version mismatch: expected %s, found %s"
                % (EXPECTED_VERSION, plugin_version)
            )
        print("PASS plugin_load -> SkinBench %s" % plugin_version)

        log_dir = os.path.normcase(os.path.abspath(core.log_dir()))
        if os.path.commonpath([temp_root_path, log_dir]) != temp_root_path:
            raise RuntimeError("SkinBench log directory escaped the temporary Maya profile")
        print("PASS log_dir -> %s" % log_dir)
        print("SKINBENCH_SMOKE_PASS")
        return 0
    finally:
        try:
            if initialized:
                maya.standalone.uninitialize()
        finally:
            shutil.rmtree(temp_maya_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
