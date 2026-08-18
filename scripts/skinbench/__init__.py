# -*- coding: utf-8 -*-
"""SkinBench — one-click skin detach/reattach and joint-move tools for Maya.

    import skinbench
    skinbench.show()
"""

__version__ = "1.0.0-beta.1"


def show():
    """Open the SkinBench window."""
    from . import ui

    return ui.show()


def reload_all():
    """Re-import every SkinBench module — for editing the tool while Maya runs."""
    import importlib

    from . import compat, core, ui

    for module in (compat, core, ui):
        importlib.reload(module)
    return show()
