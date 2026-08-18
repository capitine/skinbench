# -*- coding: utf-8 -*-
"""SkinBench installer.

Drag this file from Explorer into a Maya viewport. That's the whole install.

It writes a Maya module file pointing at this folder (so ``scripts/`` and
``plug-ins/`` are on Maya's paths every launch), loads the plugin, sets it to
auto-load, and drops a SkinBench button on your current shelf.

Nothing is copied — the module points at this folder. Keep the extracted
SkinBench folder in a permanent location; if you move it, run this installer
again.

Uninstall: delete ``<maya app dir>/modules/skinbench.mod`` and the shelf button.
"""

import inspect
import os
import sys

import maya.cmds as cmds
import maya.mel as mel

MODULE_NAME = "skinbench"
VERSION = "1.0.0-beta.1"
PLUGIN_NAME = "skinBench.py"
SHELF_ANNOTATION = "SkinBench — skin detach/reattach + move skinned joints"
SHELF_COMMAND = "import skinbench\nskinbench.show()"


def _module_root():
    this_file = inspect.getsourcefile(lambda: 0)
    return os.path.dirname(os.path.abspath(this_file))


def _modules_dir():
    path = os.path.join(cmds.internalVar(userAppDir=True), "modules")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def write_module_file(root=None):
    """Write ``skinbench.mod`` so Maya finds the tool on every launch."""
    root = root or _module_root()
    target = os.path.join(_modules_dir(), MODULE_NAME + ".mod")
    # Maya appends scripts/, plug-ins/, icons/ under the module root automatically.
    with open(target, "w") as handle:
        handle.write("+ %s %s %s\n" % (MODULE_NAME, VERSION, root.replace("\\", "/")))
    return target


def prime_current_session(root=None):
    """Make the paths live right now, without a Maya restart."""
    root = root or _module_root()
    scripts = os.path.join(root, "scripts")
    plugins = os.path.join(root, "plug-ins")

    if scripts not in sys.path:
        sys.path.insert(0, scripts)

    existing = os.environ.get("MAYA_PLUG_IN_PATH", "")
    if plugins not in existing.split(os.pathsep):
        os.environ["MAYA_PLUG_IN_PATH"] = (
            plugins + os.pathsep + existing if existing else plugins
        )
    return scripts, plugins


def _normalized_path(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_current_plugin(root):
    expected_path = _normalized_path(os.path.join(root, "plug-ins", PLUGIN_NAME))
    loaded_path = cmds.pluginInfo(PLUGIN_NAME, query=True, path=True)
    loaded_version = cmds.pluginInfo(PLUGIN_NAME, query=True, version=True)
    return _normalized_path(loaded_path) == expected_path and loaded_version == VERSION


def load_plugin(root=None):
    """Load this root once, or report that Maya needs a restart for an upgrade."""
    root = root or _module_root()
    plugin_path = os.path.join(root, "plug-ins", PLUGIN_NAME)
    if cmds.pluginInfo(PLUGIN_NAME, query=True, loaded=True):
        current = _is_current_plugin(root)
    else:
        cmds.loadPlugin(plugin_path)
        current = _is_current_plugin(root)
    cmds.pluginInfo(PLUGIN_NAME, edit=True, autoload=True)
    return current


def make_shelf_button():
    """Add a SkinBench button to the active shelf, once."""
    shelf_top = mel.eval("$tmp = $gShelfTopLevel")
    shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    for child in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        if cmds.objectTypeUI(child) != "shelfButton":
            continue  # separators and shelf spacers have no annotation
        if cmds.shelfButton(child, query=True, annotation=True) == SHELF_ANNOTATION:
            return child
    return cmds.shelfButton(
        parent=shelf,
        label="SkinBench",
        imageOverlayLabel="Skin",
        annotation=SHELF_ANNOTATION,
        sourceType="python",
        command=SHELF_COMMAND,
        image="smoothSkin.png",
        style="iconOnly",
    )


def install():
    root = _module_root()
    module_file = write_module_file(root)
    prime_current_session(root)
    plugin_current = load_plugin(root)
    make_shelf_button()

    if plugin_current:
        message = (
            "<hl>SkinBench</hl> installed — plug-in loaded from this folder; "
            "auto-load ON; shelf button ready."
        )
        print("[SkinBench] plug-in loaded from this folder; auto-load ON, shelf button ready.")
    else:
        message = "<hl>SkinBench</hl> updated — restart Maya before using this version."
        print(
            "[SkinBench] restart required: a different SkinBench plug-in is "
            "still loaded; the updated files will load next launch."
        )
    cmds.inViewMessage(amg=message, pos="midCenter", fade=True)
    print("[SkinBench] module root : %s" % root)
    print("[SkinBench] module file : %s" % module_file)
    return module_file


def onMayaDroppedPythonFile(*args):
    """Maya calls this when the file is dropped into a viewport."""
    install()


if __name__ == "__main__":
    install()
