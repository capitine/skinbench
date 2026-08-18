# -*- coding: utf-8 -*-
"""Qt binding shim — PySide6 (Maya 2025+) or PySide2 (Maya 2020-2024)."""

try:  # Maya 2025+
    from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    from shiboken6 import wrapInstance  # noqa: F401

    QT_BINDING = "PySide6"
except ImportError:  # Maya 2020-2024
    from PySide2 import QtCore, QtGui, QtWidgets  # noqa: F401
    from shiboken2 import wrapInstance  # noqa: F401

    QT_BINDING = "PySide2"

import maya.OpenMayaUI as omui


def maya_main_window():
    """The Maya main window as a QWidget, for parenting."""
    pointer = omui.MQtUtil.mainWindow()
    if pointer is None:
        return None
    return wrapInstance(int(pointer), QtWidgets.QWidget)
