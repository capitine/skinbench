# -*- coding: utf-8 -*-
"""SkinBench window — dockable, tabbed, one click per operation.

Tabs, in the order you actually reach for them:
  1. Move Joints   — the no-detach path (skinCluster -mjm). Try this first.
  2. Weights       — mirror the painted weights, then strip bleed a joint
                     cannot physically reach.
  3. Poses         — save, apply, and clear reusable joint rotations.
  4. Detach / Reattach — the heavy hammer, for skeleton changes.
  5. Bind Photo    — sync a freshly added influence before you paint it.

Every section is a colour-edged box with a red "?" that pops its own help
card. Prose does not live on the page; only the two warnings that stop damage
do. Colours mean the same thing on every tab: blue moves joints, green is
symmetry, amber writes weights you cannot Ctrl+Z, purple finishes.
"""

import os
import traceback

import maya.cmds as cmds
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin

from .compat import QtCore, QtGui, QtWidgets
from . import core

WINDOW_OBJECT_NAME = "skinBenchWindow"
WINDOW_TITLE = "SkinBench"

_window = None


def leftover_drag_warning(report):
    """One actionable warning for point offsets found when the panel opens."""
    if not report:
        return None
    total = report.get("vertices", 0)
    skipped = report.get("skipped") or []
    messages = []
    if total:
        details = ", ".join(
            "%s (%d)" % (item["shape"], item["vertices"])
            for item in report["shapes"]
        )
        messages.append(
            "SAFETY: %d leftover vertex drag%s remain%s on %s. This is a real "
            "point offset that can be saved or exported. Use 'Undo any drag still "
            "on the mesh' before saving or exporting."
            % (total, "" if total == 1 else "s", "s" if total == 1 else "", details)
        )
    if skipped:
        names = ", ".join(item["shape"] for item in skipped)
        messages.append(
            "%d skinned shape%s could not be inspected (%s). Treat %s as "
            "unverified before saving or exporting."
            % (
                len(skipped),
                "" if len(skipped) == 1 else "s",
                names,
                "it" if len(skipped) == 1 else "them",
            )
        )
    return " ".join(messages) or None


def selection_mirror_pair_messages(report):
    """Explain actual mirror-pair evidence without inventing an empty score."""
    messages = []
    if report.get("checked"):
        messages.append(
            "   · paired-weight check: worst %.6f over %d actual pair(s)"
            % (report["worst"], report["checked"])
        )
        if report.get("match") == "nearest":
            messages.append(
                "   · paired by NEAREST vertex, worst gap %.4f. That gap is how "
                "far apart a pair sat — small is a good pairing, large means it "
                "grabbed the wrong vertex."
                % report["worstGap"]
            )
    else:
        messages.append(
            "   ! paired-weight check CANNOT CHECK: no mirrored pair was written. "
            "This is not a PASS."
        )

    unmatched = report.get("unmatched", 0)
    if unmatched and report.get("match") == "exact":
        messages.append(
            "   ! %d found no exact twin across x=0. The mesh does not mirror to "
            "the decimal place — switch 'Pair by' to NEAREST. They kept their old "
            "weights." % unmatched
        )
    elif unmatched:
        messages.append(
            "   ! %d found no safe partner across x=0 inside the allowed nearest "
            "gap. They kept their old weights." % unmatched
        )
    return messages

# One hue per job, so the eye finds the right block before it reads a word.
# Green = symmetry, amber = writes weights, blue = moves joints, purple = finish.
_SECTION_COLOURS = {
    "move": "#5a9bd8",
    "mirror": "#6fb95f",
    "bake": "#a883cc",
    "bleed": "#d08a3a",
}


class _HelpPopup(QtWidgets.QDialog):
    """A floating help card, animBot-style. Click anywhere else or Esc to close.

    The body is rich text, so an ``<img src="file:///...gif">`` drops straight
    in the day we have pictures worth showing. Nothing to rebuild for it.
    """

    def __init__(self, title, body, parent=None):
        super(_HelpPopup, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Popup)
        self.setMaximumWidth(460)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)

        heading = QtWidgets.QLabel(title)
        heading.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(heading)

        text = QtWidgets.QLabel(body)
        text.setWordWrap(True)
        text.setTextFormat(QtCore.Qt.RichText)
        text.setOpenExternalLinks(True)
        layout.addWidget(text)

        self.setStyleSheet(
            "QDialog { background: #2b2b2b; border: 1px solid #6a6a6a;"
            " border-radius: 5px; }"
        )

    def show_near(self, widget):
        corner = widget.mapToGlobal(widget.rect().bottomRight())
        self.adjustSize()
        self.move(corner.x() - self.width(), corner.y() + 4)
        self.show()


def _filter_field(list_widget, placeholder="filter…"):
    """A search box that hides non-matching rows in ``list_widget``.

    Re-applies itself whenever the list is refilled, by listening to the
    model rather than asking every reload site to remember.
    """
    field = QtWidgets.QLineEdit()
    field.setPlaceholderText(placeholder)
    field.setClearButtonEnabled(True)

    def apply(text=None):
        needle = (text if text is not None else field.text()).lower()
        for row in range(list_widget.count()):
            item = list_widget.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    field.textChanged.connect(apply)
    list_widget.model().rowsInserted.connect(lambda *_: apply())
    return field


def _make_searchable(combo):
    """Let a combo be typed into, while refusing anything not in the list.

    Qt's completer does the matching. The guard matters: an editable combo
    otherwise hands ``currentText()`` back whatever was typed, and a typo
    would travel to Maya as a skinCluster name.
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
    completer = combo.completer()
    completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
    completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
    try:
        completer.setFilterMode(QtCore.Qt.MatchContains)   # Qt 5.2+
    except AttributeError:
        pass

    def snap_back():
        index = combo.findText(combo.currentText())
        combo.setCurrentIndex(index if index >= 0 else max(0, combo.currentIndex()))

    combo.lineEdit().editingFinished.connect(snap_back)
    combo.setToolTip("Type to search — anything not in the list snaps back.")
    return combo


def _warn(text):
    """One short amber line. The long version lives behind the section's '?'."""
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #d08a3a;")
    return label


# Panel size. One number, set once before the window builds, read by every
# helper below. A global because the window is a singleton and changing size
# rebuilds the whole thing from scratch — there is no second panel to confuse.
_SCALE = 1.15


def _px(size):
    """A pixel size at the current panel scale."""
    return int(round(size * _SCALE))


# The filled version of the same four hues, for the ONE button a section is
# really about. Ranking is done with size and fill, not with new colours:
# blue still means "safe to press", amber still means "this writes weights".
# Refresh's own red. Not in _PRIMARY_COLOURS on purpose: those are section
# colours that carry a MEANING (blue safe, green symmetry, purple finish, amber
# writes), and Refresh belongs to no section -- it sits above the tabs and is
# the safest button in the tool. Its red says "start here", not "danger".
#
# Both values are measured, not eyeballed. White bold text reads 7.7:1 against
# the base (comfortably past the 4.5:1 bar) and 5.9:1 on hover. A first try at
# #c22e49 passed the text test but sat only 1.2:1 away from the "?" badge red,
# which is not enough to tell two reds apart at a glance; this one is 1.7:1.
_REFRESH_RED = "#8a3549"
_REFRESH_RED_HOVER = "#a34157"

_PRIMARY_COLOURS = {
    "move": ("#3d78ad", "#4d8fc9"),
    "mirror": ("#4f8f42", "#61a854"),
    "bake": ("#7f5fa5", "#9673bd"),
    "bleed": ("#a86a24", "#c47f31"),
}


def _saturate(colour, factor):
    """More colourful (factor > 1) or greyer (factor < 1). Returns #rrggbb.

    Pushes each channel away from the average of the three, which is the plain
    version of "turn the colour up". Hue is untouched, so the colour LANGUAGE
    survives -- green still means symmetry, amber still means this writes.
    """
    parts = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    middle = sum(parts) / 3.0
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(middle + (part - middle) * factor))))
        for part in parts
    )


# The two knobs behind "which of these is a title and which is a button".
# Titles are turned UP and buttons turned DOWN, from the same base colour, so
# a section reads as one loud bar with quieter controls under it rather than
# as a block of one colour. Raise the gap if they still compete.
# Buttons were turned right down during live testing: the colour should be just
# visible in the background and no more. The title bar is the loud thing; a
# button is found by size and position. Raise _BUTTON_MUTED to bring the tint
# back if a section ever becomes hard to place.
_TITLE_VIVID, _TITLE_LIGHT = 1.6, 1.12
_BUTTON_MUTED, _BUTTON_DARK = 0.22, 0.72


def _vivid(colour):
    """The loud version, for a section title bar."""
    return _shade_hex(_saturate(colour, _TITLE_VIVID), _TITLE_LIGHT)


def _muted(colour):
    """The quiet version, for a button under a title."""
    return _shade_hex(_saturate(colour, _BUTTON_MUTED), _BUTTON_DARK)


def _shade_hex(colour, factor):
    """Like ``_shade`` but back in #rrggbb, so it can be fed to the others."""
    parts = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(part * factor)))) for part in parts)


def _shade(colour, factor):
    """Lighten (factor > 1) or darken (factor < 1) a #rrggbb colour."""
    parts = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    return "rgb(%d,%d,%d)" % tuple(
        max(0, min(255, int(round(part * factor)))) for part in parts
    )


def _raised(base, top=1.25, bottom=0.72):
    """The pressable look: lit from above, edged, so it reads as a real button.

    Titles stay FLAT on purpose. Flat = a label. Raised = you can press it.
    """
    return (
        "background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
        " stop:0 %s, stop:0.48 %s, stop:0.52 %s, stop:1 %s);"
        " border: 1px solid %s; border-top-color: %s;"
        % (
            _shade(base, top), _shade(base, 1.05),
            _shade(base, 0.95), _shade(base, bottom),
            _shade(base, 0.55), _shade(base, top),
        )
    )


def _primary(button, key):
    """Promote a button to the one thing this section is for: big and filled.

    Muted on purpose: the title bar above it is the loud thing. A button is
    found by its SIZE and its position, not by out-shouting the header.
    """
    base, hover = (_muted(colour) for colour in _PRIMARY_COLOURS[key])
    button.setMinimumHeight(_px(34))
    button.setStyleSheet(
        ("QPushButton { %s color: #ffffff; font-weight: bold;"
         " font-size: %dpx; padding: %dpx; border-radius: 5px; }"
         % (_raised(base), _px(13), _px(8))) +
        ("QPushButton:hover { %s }" % _raised(hover)) +
        # Pressed: the light comes from below and the text nudges down, so the
        # click is felt as well as seen.
        ("QPushButton:pressed { %s padding-top: %dpx; padding-bottom: %dpx; }"
         % (_raised(base, top=0.72, bottom=1.15), _px(10), _px(6)))
    )
    return button


def _minor(button):
    """Demote a rarely-used button: small, quiet, never the eye's first stop."""
    button.setStyleSheet(
        "padding: %dpx %dpx; font-size: %dpx;" % (_px(2), _px(6), _px(11)))
    return button


def _fold(label, parent_layout, start_open=False):
    """A skinny full-width bar that shows/hides a stack of widgets under it.

    Returns the layout the hidden widgets go into. Used for the rare buttons
    on each tab and for the log pane — one gesture, learned once.
    """
    bar = QtWidgets.QToolButton()
    bar.setCheckable(True)
    bar.setChecked(start_open)
    bar.setCursor(QtCore.Qt.PointingHandCursor)
    bar.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    bar.setStyleSheet(
        "QToolButton { background: #3a3a3a; color: #c8c8c8; border: none;"
        " border-radius: 3px; padding: %dpx; font-size: %dpx;"
        " text-align: center; }"
        "QToolButton:hover { background: #474747; }" % (_px(3), _px(11))
    )

    body = QtWidgets.QWidget()
    inner = QtWidgets.QVBoxLayout(body)
    inner.setContentsMargins(0, 2, 0, 0)
    inner.setSpacing(4)
    body.setVisible(start_open)

    def toggled(on):
        body.setVisible(on)
        bar.setText(("▲  " if on else "▼  ") + label)

    bar.toggled.connect(toggled)
    toggled(start_open)

    parent_layout.addWidget(bar)
    parent_layout.addWidget(body)
    return inner


def _scrollable(page):
    """Wrap a tab page so it scrolls instead of being squashed.

    Needed once the log became draggable: pulling the log up shrinks the tab
    area, and without this the buttons get crushed instead of scrolling.
    """
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.NoFrame)
    area.setWidget(page)
    return area


def _section(title, key, icon=""):
    """A titled, colour-edged group box. Returns ``(box, layout, header_row)``.

    The title is a filled gradient bar, not a coloured word: at a glance the
    eye should find the BLOCK first and only then read what it says.
    """
    box = QtWidgets.QGroupBox()
    colour = _vivid(_SECTION_COLOURS[key])
    box.setStyleSheet(
        "QGroupBox { border: 1px solid %s; border-radius: 4px;"
        " margin-top: 4px; padding: 6px; }" % colour
    )
    outer = QtWidgets.QVBoxLayout(box)
    outer.setSpacing(5)

    header = QtWidgets.QHBoxLayout()
    red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))

    arrow = QtWidgets.QLabel("▾")
    arrow.setCursor(QtCore.Qt.PointingHandCursor)
    arrow.setStyleSheet(
        "QLabel { color: #ffffff; font-weight: bold; font-size: %dpx;"
        " padding: %dpx %dpx; background: %s; border-radius: 3px; }"
        % (_px(13), _px(5), _px(7), colour)
    )
    arrow.setToolTip("Click the title to fold this section away.")
    header.addWidget(arrow)

    label = QtWidgets.QLabel(("%s  %s" % (icon, title)) if icon else title)
    label.setCursor(QtCore.Qt.PointingHandCursor)
    label.setToolTip("Click to fold this section away. Click again to open it.")
    label.setStyleSheet(
        ("QLabel { color: #ffffff; font-weight: bold; font-size: %dpx;"
         " padding: %dpx %dpx; border-radius: 3px;" % (_px(14), _px(5), _px(10))) +
        " background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
        " stop:0 rgba(%d,%d,%d,255), stop:0.7 rgba(%d,%d,%d,90),"
        " stop:1 rgba(%d,%d,%d,0)); }"
        % (red, green, blue, red, green, blue, red, green, blue)
    )
    header.addWidget(label)
    header.addStretch()
    outer.addLayout(header)

    # Everything below the header lives in its own widget, so clicking the
    # title can fold the whole block away and leave the coloured bar behind.
    body = QtWidgets.QWidget()
    body_layout = QtWidgets.QVBoxLayout(body)
    body_layout.setContentsMargins(0, 0, 0, 0)
    body_layout.setSpacing(5)
    outer.addWidget(body)

    def fold(_event=None):
        shown = not body.isVisible()
        body.setVisible(shown)
        arrow.setText("▾" if shown else "▸")

    label.mousePressEvent = fold
    arrow.mousePressEvent = fold

    box.title_label = label         # so a section can show live state in its title
    box.title_text = label.text()
    return box, body_layout, header


class SkinBenchWindow(MayaQWidgetDockableMixin, QtWidgets.QWidget):

    # How the log pane can look. The artist picks one of each from the "Aa" button;
    # the choice is remembered between sessions.
    _LOG_FONTS = [
        ("Consolas — even spacing, numbers line up", "Consolas, monospace"),
        ("Segoe UI — the normal Windows text font", "'Segoe UI', sans-serif"),
        ("Verdana — wide and round, easiest to skim", "Verdana, sans-serif"),
    ]
    _LOG_SIZES = [
        ("Small (11)", 11),
        ("Medium (13)", 13),
        ("Large (16)", 16),
        ("Huge (20)", 20),
    ]
    _LOG_COLOURS = [
        ("Default — soft grey on dark", ("#d6d6d6", "#232323")),
        ("High contrast — white on black", ("#ffffff", "#000000")),
        ("Warm — cream on brown", ("#f3e6cf", "#2a2119")),
        ("Cool — pale green on slate", ("#d2f0d8", "#1d2621")),
    ]

    # Whole-panel size. Changing it rebuilds the window — nothing is stored in
    # the panel that the scene cannot hand back, so a rebuild costs nothing.
    _UI_SIZES = [
        ("Small — the old size", 1.0),
        ("Normal", 1.15),
        ("Large", 1.35),
        ("Extra large", 1.6),
    ]

    def __init__(self, parent=None):
        super(SkinBenchWindow, self).__init__(parent=parent)
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle(WINDOW_TITLE)

        self._restore_ui_scale()        # sets the module scale before _build
        self.setMinimumWidth(_px(420))

        # Log look, before _build() needs it. Defaults are bigger than the old
        # 11px pane -- unreadable text is why nobody read the log.
        self._log_font = self._LOG_FONTS[0][1]
        self._log_size = 13
        self._log_colour = self._LOG_COLOURS[0][1]

        self._build()
        self._restore_log_look()
        self._restore_patch_dials()
        self.refresh()
        self._announce_log()
        self._warn_leftover_drags()

    def _warn_leftover_drags(self):
        try:
            message = leftover_drag_warning(core.leftover_drag_report())
        except Exception:
            self._log(
                "! SAFETY CHECK UNAVAILABLE: SkinBench could not inspect "
                "leftover vertex drags. Use 'Undo any drag still on the mesh' "
                "before saving or exporting.",
                alert=True,
            )
            return
        if message:
            self._log("! %s" % message, alert=True)

    # -- construction ------------------------------------------------------

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Everything that does not set its own font size inherits this one.
        font = QtWidgets.QApplication.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * _SCALE))
        self.setFont(font)

        # Every plain button gets the same pressable look. Buttons that set
        # their own style (the coloured primaries) override this — a style set
        # on a widget always beats one inherited from its parent.
        self.setStyleSheet(
            ("QPushButton { %s color: #dcdcdc; border-radius: 4px;"
             " padding: %dpx %dpx; }" % (_raised("#454545"), _px(5), _px(9))) +
            ("QPushButton:hover { %s color: #ffffff; }" % _raised("#535353")) +
            ("QPushButton:pressed { %s padding-top: %dpx; }"
             % (_raised("#454545", top=0.72, bottom=1.15), _px(7)))
        )

        # Refresh lives at the TOP, above the tabs, so it is in the same place
        # on every tab and never buried next to the log.
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(4)
        refresh = QtWidgets.QPushButton("⟳   Refresh from selection")
        refresh.setToolTip(
            "Re-read what you have selected in Maya and refill every list on "
            "every tab.\nPress it whenever you pick a different mesh or joint."
        )
        refresh.setCursor(QtCore.Qt.PointingHandCursor)
        refresh.setMinimumHeight(_px(38))
        # A red of its OWN. Deliberately pink-leaning (#c22e49) so it does not
        # read as a member of either family already in use: the brick-red "?"
        # badges, or the amber that means this button writes weights. White
        # bold on it measures 5.5:1 contrast -- comfortably readable.
        # Slightly taller than any other button, and the only one that spans
        # the full width, so it reads as the bar across the top rather than as
        # one more thing to choose between.
        # A double edge: a dark outer line, then a lighter line drawn just
        # inside it. Qt stylesheets have no inset shadow, so the inner line is
        # the first stop of the background gradient -- a thin band of a lighter
        # shade that stops immediately. It reads as a framed plate, which is
        # what tells this button apart from every flat-filled one below.
        inner = (
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            " stop:0 %s, stop:0.06 %s, stop:0.5 %s, stop:0.94 %s, stop:1 %s);"
            " border: 2px solid %s;"
        )

        def plate(base, lit=1.55, body=1.0, floor=0.7, edge=0.45):
            return inner % (_shade(base, lit), _shade(base, body),
                            _shade(base, body * 0.94), _shade(base, floor),
                            _shade(base, lit * 0.8), _shade(base, edge))

        refresh.setStyleSheet(
            ("QPushButton { %s color: #ffffff; font-weight: bold;"
             " font-size: %dpx; border-radius: 6px; padding: %dpx;"
             " letter-spacing: 1px; }" % (plate(_REFRESH_RED), _px(14), _px(7))) +
            ("QPushButton:hover { %s }" % plate(_REFRESH_RED_HOVER)) +
            ("QPushButton:pressed { %s padding-top: %dpx; padding-bottom: %dpx; }"
             % (plate(_REFRESH_RED, lit=0.7, body=0.82, floor=1.2, edge=0.4),
                _px(9), _px(5)))
        )
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh, 1)

        self.size_button = QtWidgets.QToolButton()
        self.size_button.setText("A⁺")
        self.size_button.setFixedSize(_px(30), _px(28))
        self.size_button.setCursor(QtCore.Qt.PointingHandCursor)
        self.size_button.setToolTip(
            "Make the whole panel bigger or smaller — buttons, text, icons, "
            "everything.\nThe window reopens at the new size; nothing is lost."
        )
        self.size_button.setStyleSheet(
            "QToolButton { background: #3a3a3a; color: #c8c8c8; border: none;"
            " border-radius: 3px; font-weight: bold; font-size: %dpx; }"
            "QToolButton:hover { background: #474747; }" % _px(12)
        )
        self.size_button.clicked.connect(self._show_size_menu)
        top.addWidget(self.size_button)
        layout.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        # Order = how often they get reached for, not how they were built.
        # Each page scrolls, so dragging the log up shrinks the tab area
        # instead of crushing the buttons inside it.
        self.tabs.addTab(_scrollable(self._build_move_tab()), "Move Joints")
        self.tabs.addTab(_scrollable(self._build_cleanup_tab()), "Weights")
        self.tabs.addTab(_scrollable(self._build_poses_tab()), "Poses")
        self.tabs.addTab(_scrollable(self._build_swap_tab()), "Detach / Reattach")
        self.tabs.addTab(_scrollable(self._build_photo_tab()), "Bind Photo")

        # -- log: hidden until asked for -------------------------------------
        log_bar = QtWidgets.QHBoxLayout()
        log_bar.setSpacing(4)

        self.log_toggle = QtWidgets.QToolButton()
        self.log_toggle.setCheckable(True)
        self.log_toggle.setCursor(QtCore.Qt.PointingHandCursor)
        self.log_toggle.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self.log_toggle.setStyleSheet(
            "QToolButton { background: #3a3a3a; color: #c8c8c8; border: none;"
            " border-radius: 3px; padding: 4px; text-align: center; }"
            "QToolButton:hover { background: #474747; }"
        )
        self.log_toggle.toggled.connect(self._toggle_log)
        log_bar.addWidget(self.log_toggle)

        self.log_options = QtWidgets.QToolButton()
        self.log_options.setText("Aa")
        self.log_options.setFixedSize(28, 24)
        self.log_options.setCursor(QtCore.Qt.PointingHandCursor)
        self.log_options.setToolTip(
            "Change how the log READS — text size, font, colours.\n"
            "Nothing here changes what any button does."
        )
        self.log_options.setStyleSheet(
            "QToolButton { background: #3a3a3a; color: #c8c8c8; border: none;"
            " border-radius: 3px; font-weight: bold; }"
            "QToolButton:hover { background: #474747; }"
            "QToolButton::menu-indicator { image: none; }"
        )
        self.log_options.clicked.connect(self._show_log_options)
        log_bar.addWidget(self.log_options)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(_px(80))

        # The bar and the pane travel together, under a draggable handle: grab
        # the ridge just above "Show log" and pull up to give the log more room.
        self._log_block = QtWidgets.QWidget()
        block = QtWidgets.QVBoxLayout(self._log_block)
        block.setContentsMargins(0, 0, 0, 0)
        block.setSpacing(4)
        block.addLayout(log_bar)
        block.addWidget(self.log_view, 1)

        self._split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self._split.setChildrenCollapsible(False)
        self._split.setHandleWidth(8)
        self._split.setStyleSheet(
            "QSplitter::handle { background: #3a3a3a; border-top: 1px solid #555;"
            " border-bottom: 1px solid #555; margin: 1px 0; }"
            "QSplitter::handle:hover { background: #5a9bd8; }"
        )
        self._split.addWidget(self.tabs)
        self._split.addWidget(self._log_block)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 0)
        layout.addWidget(self._split, 1)

        self._toggle_log(False)     # starts closed; the bar says how to open it

    # -- move-joints state -------------------------------------------------

    def _neutral(self, button):
        """A plain, still-clickable button. Not the current state, not disabled."""
        button.setMinimumHeight(_px(34))
        button.setStyleSheet(
            ("QPushButton { %s color: #d0d0d0; border-radius: 5px;"
             " font-size: %dpx; padding: %dpx; }" % (_raised("#4a4a4a"), _px(13), _px(8))) +
            ("QPushButton:hover { %s color: #ffffff; }" % _raised("#5a5a5a")) +
            ("QPushButton:pressed { %s padding-top: %dpx; padding-bottom: %dpx; }"
             % (_raised("#4a4a4a", top=0.72, bottom=1.15), _px(10), _px(6)))
        )
        return button

    def _paint_mjm_state(self, states):
        """Light the button that matches the state the skin is IN right now.

        Blue = where you are. Grey = the other one, still clickable. Nothing
        here changes the scene — it only reads.
        """
        if not states:
            word, colour = "", ""
        elif all(states):
            word, colour = "UNLOCKED — mesh is frozen", "#d08a3a"
        elif not any(states):
            word, colour = "locked", "#9a9a9a"
        else:
            word, colour = "MIXED — some unlocked", "#d08a3a"

        label = self._mjm_box.title_label
        base = self._mjm_box.title_text
        label.setText(
            "%s   <span style='color:%s'>· %s</span>" % (base, colour, word)
            if word else base
        )

        unlocked = bool(states) and all(states)
        if unlocked:
            _primary(self._unlock_button, "move")
            self._neutral(self._lock_button)
            self._unlock_button.setToolTip("The joints ARE unlocked right now.")
            self._lock_button.setToolTip(
                "Click to lock the skin back down and re-take the bind photo.")
        else:
            _primary(self._lock_button, "move")
            self._neutral(self._unlock_button)
            self._lock_button.setToolTip("The skin IS locked right now.")
            self._unlock_button.setToolTip(
                "Click to freeze the mesh so you can move joints without "
                "dragging the skin.")

    # -- panel size --------------------------------------------------------

    def _show_size_menu(self):
        """Pick how big the whole panel is. Rebuilds the window."""
        menu = QtWidgets.QMenu(self)
        try:
            menu.setToolTipsVisible(True)
        except AttributeError:
            pass
        for label, scale in self._UI_SIZES:
            action = menu.addAction(label)
            action.setToolTip(
                "Scale every button, label and icon in SkinBench. "
                "The window reopens at this size and remembers it."
            )
            action.setCheckable(True)
            action.setChecked(abs(scale - _SCALE) < 0.001)
            action.triggered.connect(
                lambda _=False, s=scale: self._set_ui_scale(s))
        corner = self.size_button.mapToGlobal(
            self.size_button.rect().bottomLeft())
        run = getattr(menu, "exec_", None) or menu.exec
        run(corner)

    def _set_ui_scale(self, scale):
        """Save the new size and rebuild the window at it.

        Deferred by a beat: the menu that triggered this is still closing, and
        ``show()`` deletes the widget it lives on.
        """
        global _SCALE
        _SCALE = scale
        try:
            self._settings().setValue("ui/scale", float(scale))
        except (ValueError, TypeError, RuntimeError, OSError):
            pass
        QtCore.QTimer.singleShot(0, show)

    def _restore_ui_scale(self):
        global _SCALE
        try:
            saved = self._settings().value("ui/scale")
            if saved is not None:
                _SCALE = max(0.8, min(2.0, float(saved)))
        except (ValueError, TypeError, RuntimeError, OSError):
            pass

    # -- log pane ----------------------------------------------------------

    def _toggle_log(self, shown):
        """Show or hide the log, and say on the bar what the next click does.

        Closed, the block is clamped to the bar's own height so the splitter
        cannot leave an empty gap under it.
        """
        self.log_view.setVisible(shown)
        self.log_toggle.setText("▲   Hide log" if shown else "▼   Show log")
        self.log_toggle.setToolTip(
            "Hide the running list of what SkinBench just did."
            if shown else
            "Show the running list of what SkinBench just did.\n"
            "Open it when a button seems to have done nothing.\n"
            "Open, drag the ridge above this bar to resize it."
        )
        if shown:
            self._log_block.setMaximumHeight(16777215)
            total = max(self._split.height(), 400)
            self._split.setSizes([total - _px(240), _px(240)])
        else:
            self._log_block.setMaximumHeight(
                self.log_toggle.sizeHint().height() + 8)

    def _log_style(self, alert=False):
        text, background = self._log_colour
        if alert:
            text, background = "#ffd9d2", "#5a1e18"
        return (
            "font-family: %s; font-size: %dpx; color: %s; background: %s;"
            % (self._log_font, self._log_size, text, background)
        )

    def _apply_log_look(self):
        self.log_view.setStyleSheet(self._log_style())

    def _show_log_options(self):
        """The small 'Aa' menu: how the log reads. Every line has a tooltip."""
        menu = QtWidgets.QMenu(self)
        try:
            menu.setToolTipsVisible(True)
        except AttributeError:
            pass

        def add(sub, label, tip, checked, on_pick):
            action = sub.addAction(label)
            action.setToolTip(tip)
            action.setCheckable(True)
            action.setChecked(checked)
            action.triggered.connect(on_pick)

        sizes = menu.addMenu("Text size")
        sizes.setToolTipsVisible(True)
        for label, size in self._LOG_SIZES:
            add(sizes, label, "How big the log text is. Bigger is easier to "
                "read from across the desk.", size == self._log_size,
                lambda _=False, s=size: self._set_log_look(size=s))

        fonts = menu.addMenu("Font")
        fonts.setToolTipsVisible(True)
        for label, family in self._LOG_FONTS:
            add(fonts, label, "The shape of the letters. Pick whichever one "
                "your eye slides over fastest.", family == self._log_font,
                lambda _=False, f=family: self._set_log_look(font=f))

        colours = menu.addMenu("Colours")
        colours.setToolTipsVisible(True)
        for label, pair in self._LOG_COLOURS:
            add(colours, label, "Text colour and background colour of the log "
                "pane. More contrast is easier on tired eyes.",
                pair == self._log_colour,
                lambda _=False, p=pair: self._set_log_look(colour=p))

        menu.addSeparator()
        # Clearing wipes the PANE, never the file. Every line is already on
        # disk under a dated name, so there is nothing here to lose and no
        # "save a copy" worth building -- the copy already exists.
        wipe = menu.addAction("Clear what is on screen")
        wipe.setToolTip(
            "Empty the pane so the next thing you press is the only thing in "
            "it.\nThe file on disk keeps every line — this cannot lose "
            "anything.")
        wipe.triggered.connect(self._clear_log_pane)

        folder = menu.addAction("Open log folder")
        folder.setToolTip(
            "Open the fixed Maya folder holding today's log and every earlier day.\n"
            "Everything you see in this pane is already saved there.")
        folder.triggered.connect(self._open_log_folder)

        corner = self.log_options.mapToGlobal(
            self.log_options.rect().bottomLeft())
        # PySide6 renamed exec_ to exec. Take whichever this build has.
        run = getattr(menu, "exec_", None) or menu.exec
        run(corner)

    def _set_log_look(self, size=None, font=None, colour=None):
        if size is not None:
            self._log_size = size
        if font is not None:
            self._log_font = font
        if colour is not None:
            self._log_colour = colour
        self._apply_log_look()
        self._remember_log_look()

    def _help(self, header, title, body):
        """Put a '?' on a section header that pops the explanation on demand."""
        button = QtWidgets.QToolButton()
        button.setText("?")
        button.setCursor(QtCore.Qt.PointingHandCursor)
        button.setToolTip("What is this for?  (click)")
        button.setFixedSize(_px(22), _px(22))
        # A filled red badge, not a faint glyph. It has to be findable at a
        # glance from across the screen or nobody clicks it.
        button.setStyleSheet(
            "QToolButton { background: #c94a3a; color: #ffffff; font-weight: bold;"
            " font-size: %dpx; border: none; border-radius: %dpx; }"
            % (_px(14), _px(11)) +
            "QToolButton:hover { background: #e8604d; }"
            "QToolButton:pressed { background: #a63a2c; }"
        )
        button.clicked.connect(
            lambda: _HelpPopup(title, body, self).show_near(button)
        )
        header.addWidget(button)
        return button

    def _build_move_tab(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setSpacing(8)

        # -- 1. unlock ------------------------------------------------------
        box, layout, header = _section("1 · Unlock the skin", "move", "🔓")
        outer.addWidget(box)
        self._help(
            header,
            "Move Skinned Joints",
            "<p>Freezes the mesh while you reposition a joint, then "
            "re-photographs the bind pose on the way out. <b>No detach "
            "needed.</b> Use it for pivot and placement fixes.</p>"
            "<p><b>Preserve Children</b> is Maya's own Rotate/Move/Scale Tool "
            "Settings option, set on all three at once. Without it, reorienting "
            "a wrist swings every finger with it. It pays for that with "
            "counter-values in the children's rotate channels — so run step 3 "
            "with the hierarchy box ticked afterwards.</p>"
            "<p><b>Bind poses are kept honest for you.</b> A rig stores a photo "
            "of where every joint stood at bind time (its <i>bindPose</i>). Move "
            "a joint and that photo can go out of date — Maya never complains, "
            "the skin still looks right, and then MotionBuilder builds the "
            "character out of the old numbers. <b>Lock in</b> re-takes the photo "
            "for any joint that has drifted, and says how many it fixed.</p>"
            "<p><b>Freshen bind poses</b> is the same sweep on demand, for a "
            "joint that got moved some other way — dragged with the skin locked, "
            "or moved by a Skeleton-menu tool. Safe to click twice: a joint that "
            "already matches is left alone.</p>"
            "<p style='color:#d08a3a'>Both refuse while the rig is <b>POSED</b>, "
            "and they are right to. Re-photographing a posed rig would bake the "
            "pose in as the new rest pose — a much worse issue than the one "
            "being fixed. Zero the pose (Poses tab) first.</p>"
            "<p style='color:#d08a3a'><b>Never use D / Edit Pivot on a skinned "
            "joint.</b> The offset is invisible to FBX and the piece swings "
            "around the wrong point in engine. Move the joint itself.</p>",
        )

        self.cluster_list = QtWidgets.QListWidget()
        self.cluster_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.cluster_list.setMaximumHeight(_px(110))
        layout.addWidget(_filter_field(self.cluster_list, "filter skinClusters…"))
        layout.addWidget(self.cluster_list)

        self.scene_wide = QtWidgets.QCheckBox("Apply to every skinCluster in the scene")
        layout.addWidget(self.scene_wide)

        # The pair this whole tab exists for. Everything else here is setup.
        # They are a STATE pair: the one you would press next is filled, the
        # one you are already in goes quiet and says so. See _paint_mjm_state.
        self._mjm_box = box
        buttons = QtWidgets.QHBoxLayout()
        self._unlock_button = QtWidgets.QPushButton("Unlock joints  (mjm ON)")
        self._unlock_button.clicked.connect(lambda: self._set_mjm(True))
        self._lock_button = QtWidgets.QPushButton("Lock in  (mjm OFF)")
        self._lock_button.clicked.connect(lambda: self._set_mjm(False))
        buttons.addWidget(self._unlock_button)
        buttons.addWidget(self._lock_button)
        layout.addLayout(buttons)

        self.preserve_children = QtWidgets.QCheckBox(
            "Preserve Children — child joints stay put while you rotate/move"
        )
        self.preserve_children.setToolTip(
            "ON: rotate a joint and its child joints DO NOT follow — the mesh "
            "bends but the bones underneath stay where they were.\n"
            "Turn it OFF when you want the whole chain to swing together."
        )
        self.preserve_children.toggled.connect(self._set_preserve_children)
        layout.addWidget(self.preserve_children)

        # Loud while it is on, invisible while it is off. The trap it guards
        # against is silent: the mesh moves, the child bones do not, and
        # nothing on screen says why.
        self._pc_warning = QtWidgets.QLabel(
            "⚠  Preserve Children is ON — rotating a joint will move the mesh "
            "but leave the child bones behind. Untick it to swing the whole chain."
        )
        self._pc_warning.setWordWrap(True)
        self._pc_warning.setVisible(False)
        layout.addWidget(self._pc_warning)

        layout.addWidget(_warn("Never use D / Edit Pivot on a skinned joint."))

        # Rare: Lock in already does this. Folded away so it stops competing.
        more = _fold("More — bind-pose catch-up", layout)
        freshen_button = _minor(QtWidgets.QPushButton("Freshen bind poses"))
        freshen_button.setToolTip(
            "Re-photograph the bind pose for any joint that has moved away from "
            "it.\n"
            "Lock in does this for you — this is the catch-up button for a joint "
            "moved some other way. Rig must be at rest. Safe to click twice."
        )
        freshen_button.clicked.connect(self._freshen_poses)
        more.addWidget(freshen_button)

        # -- 2. mirror ------------------------------------------------------
        box, layout, header = _section("2 · Mirror a joint to the other side", "mirror", "🔁")
        outer.addWidget(box)
        self._help(
            header,
            "Mirror joints",
            "<p>Select the side you already got <b>right</b>, then mirror it "
            "onto its twin (<code>_L↔_R</code>, <code>Left↔Right</code>). Works "
            "in world space across x=0, turns mjm on for you, and bakes the "
            "result into jointOrient.</p>"
            "<p>Use this instead of <b>Skeleton ▸ Mirror Joints</b> whenever the "
            "far joint already exists and is already bound — that tool builds a "
            "NEW chain from a name search/replace.</p>"
            "<p><b>Whole chain</b> matters because a joint carries its children. "
            "Mirror a wrist on its own and the fingers get SWUNG, not mirrored — "
            "they end up wherever the new wrist axis threw them.</p>"
            "<p>Afterwards it reports <b>orient err</b> and <b>mesh moved</b>. "
            "Both near 0 is the proof.</p>",
        )

        self.mirror_position = QtWidgets.QCheckBox(
            "Mirror position too (off = orientation only, joint stays put)"
        )
        self.mirror_position.setChecked(True)
        layout.addWidget(self.mirror_position)

        self.mirror_hierarchy = QtWidgets.QCheckBox(
            "Mirror the whole chain below it (wrists, ankles — leave ON)"
        )
        self.mirror_hierarchy.setChecked(True)
        layout.addWidget(self.mirror_hierarchy)

        mirror_button = _primary(
            QtWidgets.QPushButton("Mirror selected joint → opposite side"), "mirror")
        mirror_button.clicked.connect(self._mirror_joint)
        layout.addWidget(mirror_button)

        # -- 3. finish ------------------------------------------------------
        box, layout, header = _section("3 · Finish — zero the channels", "bake", "✅")
        outer.addWidget(box)
        self._help(
            header,
            "Bake rotate → jointOrient",
            "<p>After a reorient the new angle is sitting in the joint's "
            "<code>rotate</code> channel. Zero it by hand and the joint snaps "
            "back to its old axis.</p>"
            "<p>Baking moves it into <code>jointOrient</code> instead: same "
            "orientation, rotate reads 0,0,0, nothing moves, weights "
            "untouched.</p>"
            "<p>Do this before any handoff. A rig that still carries rotate "
            "reads to a retarget as a character already standing in a pose.</p>",
        )

        self.bake_hierarchy = QtWidgets.QCheckBox(
            "Include the whole joint hierarchy below the selection"
        )
        layout.addWidget(self.bake_hierarchy)

        bake_button = _primary(
            QtWidgets.QPushButton("Bake rotate → jointOrient"), "bake")
        bake_button.clicked.connect(self._bake_orient)
        layout.addWidget(bake_button)

        outer.addStretch()
        return page

    def _build_poses_tab(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setSpacing(8)

        # -- save -----------------------------------------------------------
        box, layout, header = _section("1 · Save the current pose", "move", "💾")
        outer.addWidget(box)
        self._help(
            header,
            "Save the current pose",
            "<p>Snapshots every joint's rotation to a named file in one "
            "shared library — the same library for every character, so a "
            "pose saved on characterA is offered on characterB too.</p>"
            "<p>Pose the rig however you like (rotate joints straight in the "
            "viewport — no keys needed), type a name, save. Rotations only: "
            "joint positions and bone lengths are never part of a pose.</p>",
        )
        form = QtWidgets.QFormLayout()
        self.pose_name = QtWidgets.QLineEdit()
        self.pose_name.setPlaceholderText("e.g. spear_throw")
        form.addRow("Name:", self.pose_name)
        layout.addLayout(form)
        save_button = QtWidgets.QPushButton("Save pose  (whole skeleton)")
        save_button.setStyleSheet("font-weight: bold; padding: 6px;")
        save_button.clicked.connect(self._save_pose)
        layout.addWidget(save_button)

        # -- apply ----------------------------------------------------------
        box, layout, header = _section("2 · Apply a saved pose", "mirror", "▶️")
        outer.addWidget(box)
        self._help(
            header,
            "Apply a saved pose",
            "<p><b>Same character (exact)</b> — puts the stored rotation "
            "values straight back on. Byte-exact replay, any scene version "
            "of the same rig.</p>"
            "<p><b>Other biped (world-space)</b> — replays each joint's "
            "world-space offset-from-rest onto the same-NAMED joint here, "
            "and Maya solves the local values. That is the same "
            "world-not-local trick as the joint mirror, and why it survives "
            "two rigs having completely different jointOrients. "
            "<i>Roughly right is the promise — a starting point you nudge, "
            "not an exact copy.</i> Joints whose names don't match are "
            "reported and skipped. For exact animation transfer between "
            "rigs, that's MotionBuilder's job, not SkinBench's.</p>"
            "<p>Applying writes only <code>.rotate</code> — Ctrl+Z or "
            "<b>Zero every joint</b> undoes it.</p>"
            "<p style='color:#d08a3a'>Lock in (mjm OFF) before posing — in "
            "Move Skinned Joints mode the mesh will not follow.</p>",
        )
        self.pose_list = QtWidgets.QListWidget()
        self.pose_list.setMaximumHeight(_px(140))
        layout.addWidget(_filter_field(self.pose_list, "filter poses…"))
        layout.addWidget(self.pose_list)
        # Apply was the most-pressed button on this tab during live testing.
        same_button = _primary(
            QtWidgets.QPushButton("Apply — same character (exact)"), "mirror")
        same_button.clicked.connect(lambda: self._apply_pose(False))
        layout.addWidget(same_button)
        world_button = QtWidgets.QPushButton("…other biped (world-space)")
        world_button.clicked.connect(lambda: self._apply_pose(True))
        layout.addWidget(world_button)

        more = _fold("More — the pose files on disk", layout)
        folder_button = _minor(QtWidgets.QPushButton("Open pose folder"))
        folder_button.setToolTip("Open the fixed Maya folder where poses are saved.")
        folder_button.clicked.connect(
            lambda: os.startfile(core.pose_dir()))
        more.addWidget(folder_button)

        # -- zero -----------------------------------------------------------
        box, layout, header = _section("3 · Back to rest", "bake", "🔄")
        outer.addWidget(box)
        self._help(
            header,
            "Back to rest",
            "<p>Sets every joint's rotate to 0 — the rest pose, because "
            "these rigs keep their rest in <code>jointOrient</code>.</p>"
            "<p style='color:#d08a3a'>Never save or export while posed — "
            "the FBX gates check for non-zero rotate, and a pose left in "
            "the scene is exactly the defect they exist to catch.</p>",
        )
        zero_button = _primary(
            QtWidgets.QPushButton("Zero every joint  (rest pose)"), "bake")
        zero_button.clicked.connect(self._zero_pose)
        layout.addWidget(zero_button)
        layout.addWidget(_warn("Poses are for LOOKING. Zero before you save the scene."))

        outer.addStretch()
        return page

    def _build_swap_tab(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setSpacing(8)

        # -- detach ---------------------------------------------------------
        box, layout, header = _section("1 · Detach — snapshot + unbind", "bleed", "✂️")
        outer.addWidget(box)
        self._help(
            header,
            "Detach / Reattach",
            "<p>The heavy hammer, for skeleton changes <b>mjm cannot cover</b>: "
            "reparenting, adding or removing joints, freeze passes.</p>"
            "<p>Detach snapshots the weights to disk and unbinds. Reattach "
            "rebinds at the joints' NEW positions and pours the weights back.</p>"
            "<p style='color:#d08a3a'>Detach reverts the mesh to its UNDEFORMED "
            "geometry, so it will visibly jump unless the scene is at bind pose. "
            "That is Maya's unbind, not damage.</p>"
            "<p>To undo exactly: Reattach with <b>Restore original bind "
            "matrices</b> ticked. To genuinely rebind at new joint positions: "
            "detach at bind pose — otherwise the current pose gets baked into "
            "the new bind.</p>",
        )

        form = QtWidgets.QFormLayout()
        self.mesh_field = QtWidgets.QLineEdit()
        self.mesh_field.setReadOnly(True)
        self.mesh_field.setPlaceholderText("select a skinned mesh, then Refresh")
        form.addRow("Mesh:", self.mesh_field)
        layout.addLayout(form)

        layout.addWidget(_warn("The mesh will jump unless the scene is at bind pose."))

        detach_button = _primary(
            QtWidgets.QPushButton("Detach  (snapshot + unbind)"), "bleed")
        detach_button.clicked.connect(self._detach)
        layout.addWidget(detach_button)

        # -- reattach -------------------------------------------------------
        box, layout, header = _section("2 · Reattach — rebind + restore", "move", "🔗")
        outer.addWidget(box)
        self._help(
            header,
            "Snapshots",
            "<p>Every snapshot on disk, newest first. They survive scene "
            "reloads — this is the backup Ctrl+Z is not.</p>"
            "<p><b>Snapshot only</b> saves the weights without unbinding.<br>"
            "<b>Apply weights to existing skin</b> pours a snapshot onto a "
            "skinCluster that already exists.<br>"
            "<b>Open folder</b> shows where they live.</p>"
            "<p>Mirror and Strip both write a snapshot before they change "
            "anything, so their backups land in this list too.</p>",
        )

        layout.addWidget(QtWidgets.QLabel("Snapshots (newest first):"))
        self.snapshot_list = QtWidgets.QListWidget()
        self.snapshot_list.setMaximumHeight(_px(120))
        layout.addWidget(self.snapshot_list)

        self.restore_matrices = QtWidgets.QCheckBox(
            "Restore original bind matrices (exact undo — do NOT tick if you moved joints)"
        )
        layout.addWidget(self.restore_matrices)

        reattach_button = _primary(
            QtWidgets.QPushButton("Reattach  (rebind + restore weights)"), "move")
        reattach_button.clicked.connect(self._reattach)
        layout.addWidget(reattach_button)

        more = _fold("More — snapshots without a rebind", layout)
        row = QtWidgets.QHBoxLayout()
        snapshot_only = _minor(QtWidgets.QPushButton("Snapshot only"))
        snapshot_only.setToolTip("Save weights without unbinding.")
        snapshot_only.clicked.connect(self._snapshot_only)
        apply_only = _minor(QtWidgets.QPushButton("Apply to existing skin"))
        apply_only.setToolTip("Pour the selected snapshot onto a skinCluster that already exists.")
        apply_only.clicked.connect(self._apply_weights)
        open_folder = _minor(QtWidgets.QPushButton("Open folder"))
        open_folder.setToolTip("Open the Windows folder where snapshots are saved.")
        open_folder.clicked.connect(self._open_snapshot_folder)
        row.addWidget(snapshot_only)
        row.addWidget(apply_only)
        row.addWidget(open_folder)
        more.addLayout(row)

        outer.addStretch()
        return page

    def _build_photo_tab(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setSpacing(8)

        box, layout, header = _section("Sync a new influence's bind photo", "bake", "📷")
        outer.addWidget(box)
        self._help(
            header,
            "Bind Photo",
            "<p>A joint added with <b>Add Influence</b> carries a wrong bind "
            "photo, so the first brush stroke drags verts across the mesh.</p>"
            "<p>Sync it to an influence that is already on the same skin, THEN "
            "paint. Works at any frame — it does not need a true bind pose.</p>"
            "<p style='color:#d08a3a'><b>Remove Unused Influences</b> drops "
            "EVERY zero-weight influence, not just the one you meant. Re-list "
            "influences after running it.</p>",
        )

        form = QtWidgets.QFormLayout()
        self.photo_cluster = _make_searchable(QtWidgets.QComboBox())
        self.photo_cluster.currentIndexChanged.connect(self._reload_photo_influences)
        self.photo_joint = _make_searchable(QtWidgets.QComboBox())
        self.photo_reference = _make_searchable(QtWidgets.QComboBox())
        form.addRow("skinCluster:", self.photo_cluster)
        form.addRow("Joint to fix:", self.photo_joint)
        form.addRow("Reference influence:", self.photo_reference)
        layout.addLayout(form)

        sync_button = _primary(QtWidgets.QPushButton("Sync bind photo"), "bake")
        sync_button.clicked.connect(self._sync_photo)
        layout.addWidget(sync_button)

        outer.addStretch()
        return page

    def _build_cleanup_tab(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setSpacing(8)

        # -- mirror ---------------------------------------------------------
        box, layout, header = _section("Mirror painted weights", "mirror", "🔁")
        outer.addWidget(box)
        self._help(
            header,
            "Mirror painted weights",
            "<p>Copies the painted weights across x=0. Do the <b>skeleton</b> "
            "first — influences are matched by closest joint in mirrored space, "
            "so crooked joints give crooked weights.</p>"
            "<p><b>This cluster</b> / <b>every cluster</b> use Maya's "
            "<code>copySkinWeights</code> on whole meshes.<br>"
            "<b>Selected vertices only</b> matches exact vertex twins instead, "
            "and touches nothing outside your selection. The selection says "
            "WHERE, the Copy dropdown says WHICH WAY — so it does not matter "
            "which side you rubber-banded.</p>"
            "<p><b>Reading the result:</b><br>"
            "• <b>rewrote N verts</b> — the proof the mirror WROTE, measured "
            "against the backup it just took. <i>'It ran without an error' is "
            "not proof.</i><br>"
            "• <b>twin-check worst</b> — biggest weight difference between a "
            "vertex and its exact twin. Near 0 means the sides now agree.<br>"
            "• <b>CANNOT CHECK</b> — the mesh has no exact twins across x=0. "
            "The whole-cluster mirror samples by closest point, so it "
            "<b>rewrites the far side anyway</b> — judge it by eye.<br>"
            "• <b>unmatched</b> — verts with no twin: the MESH is asymmetric "
            "there, or the rig is posed. A blind spot in the check, not a "
            "skip by the mirror.<br>"
            "• <b>midline one-sided paint</b> — a vert ON x=0 whose Left/Right "
            "weights disagree with each other. It is its own twin, so no "
            "mirror can fix it; repaint or symmetrize it by hand.</p>"
            "<p style='color:#d08a3a'>Vertex mirroring needs the rig at REST "
            "pose — twins are found in world space.</p>"
            "<p><b>Selected vertices only</b> writes through the same guarded "
            "path as Strip. If the numbers do not exactly fit the mesh and its "
            "influence list it <b>refuses</b>, logging "
            "<i>refusing to write weights</i> rather than writing something "
            "wrong. (It shared the influence-index bug fixed during live testing — "
            "see the Strip card.)</p>",
        )
        layout.addWidget(_warn("Overwrites the far side. No merge — but it snapshots first."))

        mirror_form = QtWidgets.QFormLayout()
        self.mirror_cluster = _make_searchable(QtWidgets.QComboBox())
        mirror_form.addRow("skinCluster:", self.mirror_cluster)
        self.weight_direction = QtWidgets.QComboBox()
        self.weight_direction.addItem("Left → Right   (+x onto −x)", True)
        self.weight_direction.addItem("Right → Left   (−x onto +x)", False)
        mirror_form.addRow("Copy:", self.weight_direction)
        self.mirror_match = QtWidgets.QComboBox()
        self.mirror_match.addItem("exact twin vertex (strict)", "exact")
        self.mirror_match.addItem("nearest vertex (asymmetric meshes)", "nearest")
        self.mirror_match.setToolTip(
            "How a vertex finds its partner on the other side.\n"
            "Exact needs a mesh that mirrors to the decimal place. CharacterB's "
            "body sits about 0.18 off centre, so exact finds NOTHING there — "
            "use nearest, and read the reported gap."
        )
        mirror_form.addRow("Pair by:", self.mirror_match)
        layout.addLayout(mirror_form)

        check_button = QtWidgets.QPushButton("Check symmetry  (changes nothing)")
        check_button.setToolTip(
            "How far apart the two sides already are, before you overwrite "
            "anything. Run this first."
        )
        check_button.clicked.connect(self._check_symmetry)
        layout.addWidget(check_button)

        one_button = _primary(
            QtWidgets.QPushButton("Mirror weights — this cluster"), "mirror")
        one_button.clicked.connect(lambda: self._mirror_weights(scene_selection=False))
        layout.addWidget(one_button)

        selection_button = _primary(
            QtWidgets.QPushButton("Mirror weights — SELECTED VERTICES only"), "mirror")
        selection_button.setToolTip(
            "Select a patch of verts anywhere on the model. Either side works — "
            "the selection says where, the Copy dropdown says which way. Matches "
            "exact vertex twins across x=0, so nothing outside the patch is "
            "touched."
        )
        selection_button.clicked.connect(self._mirror_weights_selection)
        layout.addWidget(selection_button)

        more = _fold("More — mirror several meshes at once", layout)
        many_button = _minor(QtWidgets.QPushButton("…every cluster in the selection"))
        many_button.setToolTip(
            "Mirror every skinned mesh you have selected, not just the one in "
            "the box above."
        )
        many_button.clicked.connect(lambda: self._mirror_weights(scene_selection=True))
        more.addWidget(many_button)

        # -- heal -----------------------------------------------------------
        box, layout, header = _section("Heal selected vertices", "bleed", "🔨")
        outer.addWidget(box)
        self._help(
            header,
            "Heal selected vertices",
            "<p>Maya's <b>Weight Hammer</b>, with the two things it is missing "
            "bolted on. Select the vertices that deform badly and press it: "
            "each one throws away its own weights and copies the average of "
            "its neighbours.</p>"
            "<p>It is the right broom for <b>a few bad vertices</b> — the ones "
            "that spike when you pose the rig. It is the wrong tool for a "
            "whole smeared region; that is Strip or Zero, below.</p>"
            "<p><b>1 · The locks come off first.</b> A locked influence is "
            "silently skipped by Hammer, by the brush and by every weight "
            "command — they all report success and change nothing. That cost "
            "an hour of painting on characterA. SkinBench unlocks every "
            "influence before hammering and <b>leaves them unlocked</b>, "
            "because quietly re-arming that trap would be worse than the "
            "trap. The log names anything it had to unlock.</p>"
            "<p><b>2 · It names the guilty joint.</b> For every vertex that "
            "changed, the log gets the before and after weight of each "
            "influence that moved, biggest mover first — e.g. "
            "<code>toe_joint 0.5500 -> 0.0000</code>. Hammer on its own "
            "tells you nothing about which joint was wrong, so the same bad "
            "joint gets to bleed somewhere else next week. Proved during live "
            "testing: toe_joint weights on the hand and chest.</p>"
            "<p><b>Posing is fine — that is the point.</b> Weight edits do not "
            "care what pose the rig is in, and a pose is usually what made the "
            "bad vertex visible. Pose it (Poses tab), spot the spike, select, "
            "heal, re-pose to check. Only <i>moving joints</i> and <i>saving</i> "
            "need rest pose.</p>"
            "<p style='color:#d08a3a'><b>Select only the bad vertices.</b> "
            "Measured during live testing: Hammer moves <b>every</b> vertex you hand it, "
            "not just the broken ones — a clean vertex on a smooth gradient "
            "still shifted about 0.02. So healing a whole region quietly "
            "re-averages good paint along with the bad. A handful of vertices "
            "at a time is the job.</p>"
            "<p style='color:#d08a3a'>A snapshot is taken first. Ctrl+Z will "
            "not undo this.</p>",
        )

        heal_button = _primary(
            QtWidgets.QPushButton("Heal selected vertices  (writes)"), "bleed")
        heal_button.setToolTip(
            "Weight Hammer the selected vertices: each one copies the average "
            "of its neighbours.\n"
            "Unlocks every influence first, and logs which joint it took the "
            "weight from. Works posed."
        )
        heal_button.clicked.connect(self._heal_selection)
        layout.addWidget(heal_button)

        # -- drag-solve -----------------------------------------------------
        box, layout, header = _section("Solve weights from a drag", "bake", "🖐️")
        outer.addWidget(box)
        self._help(
            header,
            "Solve weights from a drag",
            "<p><b>Pose the rig. Move the bad vertex to where it should sit. "
            "Press this.</b> SkinBench works out the weights that hold it "
            "there and paints them, then removes your drag — so the fix lives "
            "in the weights, not in a moved point.</p>"
            "<p>It is the opposite way round from a brush. A brush asks you to "
            "guess a strength and look at the result. This asks you for the "
            "result and works out the numbers.</p>"
            "<p><b>Preview first</b> — it prints the share it would paint "
            "(e.g. <i>Jaw 60%, Neck 30%, Head 10%</i>) and how much of your "
            "drag it can actually deliver, and writes nothing.</p>"
            "<p><b>Three things it refuses to do, on purpose:</b></p>"
            "<p>• <b>At rest pose.</b> With the rig at rest every joint puts "
            "the vertex in the same place, so no weighting can move it "
            "anywhere. It says so instead of looking broken. Pose it first "
            "(Poses tab).</p>"
            "<p>• <b>Past what the joints can do.</b> A vertex can only sit "
            "somewhere between where each joint would put it — usually a "
            "surprisingly small space. If your drag lands mostly outside it, "
            "you get the nearest reachable point as a number and "
            "<b>nothing is written</b>. It will not quietly fit something "
            "close and call it done.</p>"
            "<p>• <b>On a mesh it does not understand.</b> Before solving "
            "anything it re-does the sums on vertices you did not touch. If it "
            "cannot reproduce where those already sit, something other than "
            "ordinary smooth skinning is moving this mesh (a blendshape, a "
            "second deformer, dual quaternion skinning) and the whole run "
            "stops.</p>"
            "<p><b>Reading the result.</b> <i>delivered 78%</i> means the "
            "weights got the vertex 78% of the way to where you put it — the "
            "rest is simply not reachable by any weighting. The share is "
            "logged so you can sanity-check it: if a hand vertex comes back "
            "60% <i>Jaw</i>, the answer fits the maths and not the anatomy, "
            "and you should throw it away.</p>"
            "<p><b>max joints</b> caps how many influences the answer may use. "
            "4 matches what game engines allow.</p>"
            "<p style='color:#d08a3a'>Prior art, read before this was built: "
            "<b>SSDR</b> (Le &amp; Deng, 2012) already solves fitting weights "
            "to match a shape, with these same rules. This is the one-vertex, "
            "one-pose corner of it.</p>"
            "<p style='color:#d08a3a'>A snapshot is taken first. Ctrl+Z will "
            "not undo the write.</p>",
        )

        ready_row = QtWidgets.QHBoxLayout()
        self.drag_constraint = QtWidgets.QComboBox()
        self.drag_constraint.addItem("slide along the skin", "surface")
        self.drag_constraint.addItem("slide along the edges", "edge")
        self.drag_constraint.addItem("drag freely in space", "free")
        self.drag_constraint.setToolTip(
            "How the vertex is allowed to move.\n"
            "Sliding along the skin keeps it on the surface, which is usually "
            "what you mean. Free lets it go anywhere, including off the model."
        )
        ready_row.addWidget(self.drag_constraint, 1)
        ready_button = QtWidgets.QPushButton("Get me ready to drag")
        ready_button.setToolTip(
            "Switch to the Move tool, put the mesh in vertex mode, and set how "
            "the vertex may move — all in one press.\n"
            "That setting lives in Maya's Tool Settings, which only shows the "
            "tool you are already in, so from the paint tool it is not on "
            "screen at all."
        )
        ready_button.clicked.connect(self._ready_to_drag)
        ready_row.addWidget(ready_button)
        layout.addLayout(ready_row)

        drag_row = QtWidgets.QHBoxLayout()
        drag_row.addWidget(QtWidgets.QLabel("max joints per vertex:"))
        self.drag_max_influences = QtWidgets.QSpinBox()
        self.drag_max_influences.setRange(1, 12)
        self.drag_max_influences.setValue(4)
        self.drag_max_influences.setToolTip(
            "How many joints the answer is allowed to use.\n"
            "4 is what most game engines allow. Raise it only if the solve "
            "cannot reach far enough."
        )
        drag_row.addWidget(self.drag_max_influences)
        drag_row.addStretch()
        layout.addLayout(drag_row)

        drag_preview = _primary(
            QtWidgets.QPushButton("Preview the solve  (changes nothing)"), "move")
        drag_preview.setToolTip(
            "Work out the weights and print them, without painting anything.\n"
            "Run this first, every time."
        )
        drag_preview.clicked.connect(lambda: self._drag_solve(dry_run=True))
        layout.addWidget(drag_preview)

        drag_write = _primary(
            QtWidgets.QPushButton("Paint the solved weights  (writes)"), "bake")
        drag_write.setToolTip(
            "Paint the weights that hold each dragged vertex where you put it, "
            "then clear the drag so only the weights are holding it."
        )
        drag_write.clicked.connect(lambda: self._drag_solve(dry_run=False))
        layout.addWidget(drag_write)

        clear_drags = _minor(QtWidgets.QPushButton(
            "Undo any drag still on the mesh"))
        clear_drags.setToolTip(
            "A refused drag stays where you put it, on purpose — but it is a "
            "real dent in the model, and it saves and exports.\n"
            "This puts those vertices back. Weights are not touched.\n"
            "With no selection, it checks every skinned polygon mesh in the scene.\n"
            "One Ctrl+Z restores the whole cleanup."
        )
        clear_drags.clicked.connect(self._clear_drags)
        layout.addWidget(clear_drags)

        layout.addWidget(_warn(
            "Pose the rig first. At rest pose there is nothing to solve."))

        # -- bleed ----------------------------------------------------------
        box, layout, header = _section("Strip bleeding weights", "bleed", "🧹")
        outer.addWidget(box)
        self._help(
            header,
            "Strip bleeding weights",
            "<p>Bleed is a joint pulling a vertex it is nowhere near — a toe "
            "joint holding weight on the head.</p>"
            "<p>Pick the joint, <b>Preview</b> to see which vertices it reaches "
            "too far for, then <b>Strip</b>. The radius is <b>N × that joint's "
            "LONGEST bone</b> — the one coming in from its parent, or the one "
            "going out to its child, whichever is longer. So a pinky gets a "
            "tight radius and a long-jaw rig gets the long snout it "
            "actually drives, with nothing to re-tune per rig. Lower N catches "
            "more.</p>"
            "<p><b>Preview sorts every vertex into four buckets, and says all "
            "four.</b> They add up to the number of vertices the joint touches "
            "at all — so paint you can see but Preview did not select is "
            "answered, not missing:</p>"
            "<p>• <b>strippable</b> — far from the joint and NOT its main owner. "
            "The junk. These are the ones Preview selects, and the only ones "
            "Strip touches.</p>"
            "<p>• <b>kept</b> — far, but this joint is the vertex's main owner. "
            "That is the joint's real body, not bleed: on a long-jaw rig, the snout "
            "tip is a long way from the upper jaw and belongs to it completely. "
            "Fix these by reassigning or repainting, never by stripping.</p>"
            "<p>• <b>ignored</b> — weight under <b>0.005</b>, which moves nothing "
            "you can see. Faint blue paint that Preview skips is display dust, "
            "not deformation. Maya's <i>Skin &gt; Prune Small Weights</i> at "
            "0.005 is the broom for it.</p>"
            "<p>• <b>fine</b> — close enough to the joint to be real weight. "
            "Nothing to do.</p>"
            "<p><b>Home patch — the bleed finder.</b> This asks a different "
            "question from N, and often a better one. Instead of <i>how far "
            "away is this weight</i>, it asks <b>is this weight even joined to "
            "the lump of paint where the joint really works</b>. That lump is "
            "the joint's <b>home patch</b>. Anything not connected to home is a "
            "bleed candidate — <b>however light or heavy its paint is</b>.</p>"
            "<p>Use it when a joint has paint scattered across the model. A "
            "smear of jaw_joint on an asymmetrical rig's shoulder is not far enough away "
            "to trip even a tight N, but there is no path across the mesh from "
            "it back to the snout. <b>Dust is deleted too</b> — that is half "
            "the point.</p>"
            "<p><b>Preview islands</b> is the main event: it selects the "
            "bleeding areas and writes nothing. <b>The selection is the "
            "product.</b> Fix it however you like — <b>Shift+drag adds, "
            "Ctrl+drag removes</b> (that is plain Maya, and it works on every "
            "selection SkinBench makes), or use Grow and Shrink. Then the write "
            "button below deletes <b>exactly what is selected at that moment</b> "
            "— it does not go looking for patches again.</p>"
            "<p><b>stickiness</b> is how much paint a vertex needs before it "
            "can pass the patch on to its neighbour. Raise it to break faint "
            "bridges apart; lower it to let thin paint join patches together. "
            "The default stops a trail of dust from bridging a shoulder smear "
            "back to the snout and rescuing it.</p>"
            "<p><b>home reach</b> is how close a patch has to come to the joint "
            "to count as home, in that joint's own bone lengths. Raise it if "
            "the real patch starts a little way off the joint. Both dials are "
            "remembered between sessions.</p>"
            "<p>When two patches both reach the joint, the <b>heaviest</b> one "
            "is home. If the heaviest patch of all is NOT the one at the joint, "
            "SkinBench keeps the one at the joint and says so loudly — a "
            "joint's main paint living somewhere else is a rig issue worth "
            "your eyes, not something to quietly delete.</p>"
            "<p>Two things it refuses, on purpose: if <b>no</b> patch reaches "
            "the joint it stops rather than erasing the joint completely (that "
            "means wrong joint, wrong mesh, or the patch was never painted — a "
            "structural issue, not paint). And a vertex this joint OWNS with "
            "almost nothing else holding it is left alone and reported as "
            "<b>repaint by hand</b>, because deleting its only real influence "
            "would hand the vertex to a rounding error.</p>"
            "<p><b>Select region</b> reads the area straight off the weights — "
            "not saved, so it cannot go stale when you add a joint or repaint. "
            "Isolate the arm, then Preview to check only that arm. "
            "<b>Invert</b> flips it: everything on the same mesh the joint "
            "does NOT drive.</p>"
            "<p><b>Grow</b> makes that region bigger — one ring of neighbouring "
            "vertices per click. <b>Shrink</b> is the exact opposite: it peels "
            "the outer ring off, so Grow 2 then Shrink 2 puts you back where "
            "you started. <b>rings per click</b> is how many rings each press "
            "moves. Set <b>grow</b> to a direction and Grow keeps only the new "
            "vertices on that side of where the selection STARTED, so a region "
            "can creep up the neck without spilling down the chest. The middle "
            "is measured once, before the first ring — the direction does not "
            "wander as the selection spreads. Vertices turned away for facing "
            "the wrong way are counted in the log.</p>"
            "<p>Any selection SkinBench makes is a normal Maya selection: "
            "<b>Shift+drag adds, Ctrl+drag removes</b>. Nothing is written "
            "until you press a button that says <i>writes</i>, so edit freely "
            "first.</p>"
            "<p><b>If a button seems to do nothing, read the log.</b> Every "
            "refusal says <b>&lt;BUTTON&gt; BLOCKED (nothing happened)</b> and "
            "flashes the log pane red. The commonest one: <b>Preview and Strip "
            "refuse on a POSED rig</b>, because distances would be measured "
            "against the pose instead of the rig. Zero the pose (Poses tab), "
            "end the ROM sweep, or Ctrl+Z back to rest, then Preview again. "
            "If Preview ran but you cannot see the vertices, press <b>F</b> to "
            "frame them and <b>Show all</b> if the view is still isolated.</p>"
            "<p style='color:#d08a3a'><b>Preview first, every time.</b> A "
            "vertex's DOMINANT influence is never stripped — end_joint reaching "
            "onto characterB's hips looks exactly like bleed and is correct "
            "anatomy, and distance alone cannot tell them apart. Strip "
            "snapshots the weights first; Ctrl+Z will not.</p>"
            "<p><b>Writing is checked before it happens.</b> Every weight write "
            "in SkinBench goes through one guarded path: if the numbers about "
            "to be written do not exactly fit this mesh and its influence list, "
            "it <b>refuses</b> and says <i>refusing to write weights</i>. Maya "
            "on its own accepts a wrong-sized batch and writes garbage without "
            "a word, so the check lives here instead.</p>"
            "<p><i>Fixed during live testing:</i> Strip crashed with <i>Object is "
            "incompatible with this method</i> on any rig whose influences have "
            "been added and removed over the years — one rig had 69 influences. "
            "Reading was always correct, which is why Preview's report printed "
            "fine and only the write blew up. If you ever see that message "
            "again, stop and report it; nothing was written.</p>",
        )

        form = QtWidgets.QFormLayout()
        self.bleed_cluster = _make_searchable(QtWidgets.QComboBox())
        self.bleed_cluster.currentIndexChanged.connect(self._reload_bleed_joints)
        form.addRow("skinCluster:", self.bleed_cluster)
        layout.addLayout(form)

        layout.addWidget(QtWidgets.QLabel("Joints to clean (none selected = every influence):"))
        self.bleed_joints = QtWidgets.QListWidget()
        self.bleed_joints.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.bleed_joints.setMaximumHeight(_px(130))
        layout.addWidget(_filter_field(self.bleed_joints, "filter joints…  e.g. Thumb, Left, Toe"))
        layout.addWidget(self.bleed_joints)

        self.region_hierarchy = QtWidgets.QCheckBox(
            "Include everything below the selected joints"
        )
        self.region_hierarchy.setChecked(True)
        layout.addWidget(self.region_hierarchy)

        region = QtWidgets.QHBoxLayout()
        paint_button = QtWidgets.QPushButton("Paint this joint")
        paint_button.setToolTip(
            "Open Maya's Paint Skin Weights on this mesh with the joint above "
            "already active — white is 1.0, black is 0.\n"
            "One joint at a time; if several are picked it uses the first."
        )
        paint_button.clicked.connect(self._paint_joint)
        select_button = QtWidgets.QPushButton("Select region")
        select_button.setToolTip("Select every vertex these joints actually drive.")
        select_button.clicked.connect(lambda: self._region(isolate=False))
        invert_button = QtWidgets.QPushButton("Invert")
        invert_button.setToolTip(
            "Flip the selection: every vertex of the same mesh(es) that is "
            "NOT selected. Region first, then Invert = what the joint does "
            "NOT drive."
        )
        invert_button.clicked.connect(self._invert_region)
        isolate_button = QtWidgets.QPushButton("Select + isolate")
        isolate_button.setToolTip("...and show only that part of the mesh.")
        isolate_button.clicked.connect(lambda: self._region(isolate=True))
        show_all = QtWidgets.QPushButton("Show all")
        show_all.setToolTip("Bring the rest of the model back after Select + isolate.")
        show_all.clicked.connect(self._isolate_off)
        region.addWidget(paint_button)
        region.addWidget(select_button)
        layout.addLayout(region)

        # Invert / isolate / show-all are the once-in-a-while ones (log,
        # During live testing, Select region and Grow carried the session; these three
        # were never pressed). Folded, not deleted.
        region_more = _fold("More — invert, isolate, show all", layout)
        region_row2 = QtWidgets.QHBoxLayout()
        region_row2.addWidget(_minor(invert_button))
        region_row2.addWidget(_minor(isolate_button))
        region_row2.addWidget(_minor(show_all))
        region_more.addLayout(region_row2)

        # Grow sits on its own row under Select region: same job (deciding WHAT
        # is selected), but it needs two dials, and eight widgets on one row is
        # unreadable.
        grow_row = QtWidgets.QHBoxLayout()
        grow_button = QtWidgets.QPushButton("Grow")
        grow_button.setToolTip(
            "Add the next ring of vertices around what is already selected.\n"
            "Click again to keep going. Select region first."
        )
        grow_button.clicked.connect(self._grow)
        self.grow_steps = QtWidgets.QSpinBox()
        self.grow_steps.setRange(1, 50)
        self.grow_steps.setValue(1)
        self.grow_steps.setToolTip("How many rings one click adds.")
        self.grow_direction = QtWidgets.QComboBox()
        for label, axis in core.GROW_AXES:
            self.grow_direction.addItem(label, axis)
        self.grow_direction.setToolTip(
            "All directions grows evenly, like Maya's Grow Selection.\n"
            "Pick a direction and it keeps only the new vertices on that side "
            "of where the selection started."
        )
        shrink_button = QtWidgets.QPushButton("Shrink")
        shrink_button.setToolTip(
            "The opposite of Grow: peel the outer ring off the selection.\n"
            "Grow 2 then Shrink 2 gets you back where you started."
        )
        shrink_button.clicked.connect(self._shrink)
        grow_row.addWidget(grow_button)
        grow_row.addWidget(shrink_button)
        grow_row.addWidget(QtWidgets.QLabel("rings per click:"))
        grow_row.addWidget(self.grow_steps)
        grow_row.addWidget(QtWidgets.QLabel("grow:"))
        grow_row.addWidget(self.grow_direction)
        grow_row.addStretch()
        layout.addLayout(grow_row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("N (bone lengths):"))
        self.bleed_n = QtWidgets.QDoubleSpinBox()
        self.bleed_n.setRange(0.5, 100.0)
        self.bleed_n.setSingleStep(0.5)
        self.bleed_n.setValue(core.BLEED_N)
        self.bleed_n.setToolTip(
            "Lower catches more. Preview after every change — the number that is "
            "right for a finger is not the one that is right for a spine."
        )
        row.addWidget(self.bleed_n)
        row.addStretch()
        layout.addLayout(row)

        # Blue = looks only, amber = writes weights. Same code as every tab.
        preview_button = _primary(
            QtWidgets.QPushButton("Preview  (select the offending vertices)"), "move")
        preview_button.clicked.connect(lambda: self._bleed(dry_run=True))
        layout.addWidget(preview_button)

        strip_button = _primary(
            QtWidgets.QPushButton("Strip bleeding weights  (writes)"), "bleed")
        strip_button.clicked.connect(lambda: self._bleed(dry_run=False))
        layout.addWidget(strip_button)

        zero_button = QtWidgets.QPushButton("Zero these joints on the SELECTION  (writes)")
        zero_button.setStyleSheet("font-weight: bold; padding: 6px; color: #d08a3a;")
        zero_button.setToolTip(
            "The region-shaped clean, for bleed that N cannot see — a skull "
            "joint painting the neck sits too close to ever trip the distance "
            "test.\n"
            "Select region → Invert → this. The selection on screen IS the "
            "preview, so there is no dry run; a snapshot is taken first."
        )
        zero_button.clicked.connect(self._zero_on_selection)
        layout.addWidget(zero_button)

        # The two dials that decide what counts as "joined" and what counts as
        # "at the joint". Plain names: the maths words live in the help card.
        dials = QtWidgets.QHBoxLayout()
        dials.addWidget(QtWidgets.QLabel("stickiness:"))
        self.patch_stickiness = QtWidgets.QDoubleSpinBox()
        self.patch_stickiness.setDecimals(4)
        self.patch_stickiness.setRange(0.0001, 1.0)
        self.patch_stickiness.setSingleStep(0.001)
        self.patch_stickiness.setValue(core.BLEED_FLOOR)
        self.patch_stickiness.setToolTip(
            "How much paint a vertex needs before it can pass the patch along "
            "to its neighbour.\n"
            "Higher splits faint bridges apart. Lower lets thin paint join "
            "patches together."
        )
        dials.addWidget(self.patch_stickiness)
        dials.addWidget(QtWidgets.QLabel("home reach:"))
        self.patch_reach = QtWidgets.QDoubleSpinBox()
        self.patch_reach.setDecimals(2)
        self.patch_reach.setRange(0.1, 50.0)
        self.patch_reach.setSingleStep(0.25)
        self.patch_reach.setValue(core.PATCH_KEEP_RADIUS)
        self.patch_reach.setToolTip(
            "How close a patch has to come to the joint to count as its home, "
            "in that joint's own bone lengths.\n"
            "Raise it if the real patch starts a little way off the joint."
        )
        dials.addWidget(self.patch_reach)
        dials.addStretch()
        layout.addLayout(dials)

        patch_preview = _primary(
            QtWidgets.QPushButton("Preview islands  (select the bleeding areas)"),
            "move")
        patch_preview.setToolTip(
            "Find the joint's home patch — the big connected lump of paint "
            "where it really works — and select everything not joined to it.\n"
            "Changes nothing. Edit the selection by hand afterwards if you like."
        )
        patch_preview.clicked.connect(self._patch_preview)
        layout.addWidget(patch_preview)

        patch_button = _primary(
            QtWidgets.QPushButton("Zero everything OUTSIDE the joint's patch  (writes)"),
            "bleed")
        patch_button.setToolTip(
            "Delete this joint's paint on the vertices selected RIGHT NOW.\n"
            "It uses what is on screen — including your own Shift/Ctrl edits, "
            "Grow and Shrink — and does not re-find the patches."
        )
        patch_button.clicked.connect(self._patch_write)
        layout.addWidget(patch_button)

        layout.addWidget(_warn("Preview first, every time. Ctrl+Z will not undo either write."))

        outer.addStretch()
        return page

    # -- refresh -----------------------------------------------------------

    def refresh(self):
        try:
            clusters = core.skin_clusters_from_selection()
            self.cluster_list.clear()
            states = []
            for cluster in clusters:
                unlocked = core.move_joints_mode(cluster)
                states.append(unlocked)
                item = QtWidgets.QListWidgetItem(
                    "[%s]  %s" % ("ON " if unlocked else "off", cluster))
                item.setData(QtCore.Qt.UserRole, cluster)
                item.setSelected(True)
                self.cluster_list.addItem(item)
            if not clusters:
                self.cluster_list.addItem("— select a skinned mesh or an influence joint —")
            self._paint_mjm_state(states)

            meshes = [
                node
                for node in (cmds.ls(selection=True, long=True) or [])
                if core.skin_cluster_for(node)
            ]
            self.mesh_field.setText(meshes[0] if meshes else "")

            # Maya owns this one, and the user can flip it from Tool Settings
            # too — mirror the truth rather than remembering our own copy.
            self.preserve_children.blockSignals(True)
            preserving = core.preserve_child_position()
            self.preserve_children.setChecked(preserving)
            self.preserve_children.blockSignals(False)
            self._show_preserve_children_state(preserving)

            self._reload_snapshots()
            self._reload_photo_clusters(clusters)
            self._reload_bleed_clusters(clusters)
            self._reload_combo(self.mirror_cluster, clusters)
            self._reload_poses()
        except Exception:
            self._log(traceback.format_exc())

    def _reload_combo(self, combo, clusters):
        """Refill a skinCluster combo, keeping the current pick if it survives."""
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(clusters or core.all_skin_clusters())
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _reload_snapshots(self):
        self.snapshot_list.clear()
        for path in core.list_snapshots():
            item = QtWidgets.QListWidgetItem(os.path.basename(path))
            item.setData(QtCore.Qt.UserRole, path)
            self.snapshot_list.addItem(item)
        mesh = self.mesh_field.text()
        if mesh:
            preferred = core.newest_snapshot_for(mesh)
            for row in range(self.snapshot_list.count()):
                if self.snapshot_list.item(row).data(QtCore.Qt.UserRole) == preferred:
                    self.snapshot_list.setCurrentRow(row)
                    break
        elif self.snapshot_list.count():
            self.snapshot_list.setCurrentRow(0)

    def _reload_photo_clusters(self, clusters):
        current = self.photo_cluster.currentText()
        self.photo_cluster.blockSignals(True)
        self.photo_cluster.clear()
        self.photo_cluster.addItems(clusters or core.all_skin_clusters())
        index = self.photo_cluster.findText(current)
        if index >= 0:
            self.photo_cluster.setCurrentIndex(index)
        self.photo_cluster.blockSignals(False)
        self._reload_photo_influences()

    def _reload_photo_influences(self):
        cluster = self.photo_cluster.currentText()
        self.photo_joint.clear()
        self.photo_reference.clear()
        if not cluster or not cmds.objExists(cluster):
            return
        influences = cmds.skinCluster(cluster, query=True, influence=True) or []
        self.photo_joint.addItems(influences)
        self.photo_reference.addItems(influences)
        selected = [
            node.split("|")[-1] for node in (cmds.ls(selection=True, long=True) or [])
        ]
        for name in selected:
            index = self.photo_joint.findText(name)
            if index >= 0:
                self.photo_joint.setCurrentIndex(index)
                break

    def _reload_bleed_clusters(self, clusters):
        current = self.bleed_cluster.currentText()
        self.bleed_cluster.blockSignals(True)
        self.bleed_cluster.clear()
        self.bleed_cluster.addItems(clusters or core.all_skin_clusters())
        index = self.bleed_cluster.findText(current)
        if index >= 0:
            self.bleed_cluster.setCurrentIndex(index)
        self.bleed_cluster.blockSignals(False)
        self._reload_bleed_joints()

    def _reload_bleed_joints(self):
        """List the influences, preselecting whatever joints are selected in Maya.

        Selecting the joint in the viewport and hitting Preview is the whole
        workflow; the list is there to see what got picked, not to replace it.
        """
        cluster = self.bleed_cluster.currentText()
        self.bleed_joints.clear()
        if not cluster or not cmds.objExists(cluster):
            return
        influences = cmds.skinCluster(cluster, query=True, influence=True) or []
        selected = {
            node.split("|")[-1] for node in (cmds.ls(selection=True, long=True) or [])
        }
        for name in influences:
            item = QtWidgets.QListWidgetItem(name.split("|")[-1])
            item.setData(QtCore.Qt.UserRole, name)
            self.bleed_joints.addItem(item)
            if name.split("|")[-1] in selected:
                item.setSelected(True)

    # -- actions -----------------------------------------------------------

    def _selected_clusters(self):
        if self.scene_wide.isChecked():
            return core.all_skin_clusters()
        clusters = []
        for item in self.cluster_list.selectedItems():
            cluster = item.data(QtCore.Qt.UserRole)
            if cluster:
                clusters.append(cluster)
        return clusters

    def _set_mjm(self, enabled):
        try:
            clusters = self._selected_clusters()
            if not clusters:
                self._log("Nothing to toggle — select a skinned mesh or an "
                          "influence joint.", alert=True)
                return
            result = core.set_move_joints_mode(clusters, enabled)
            self._log(
                "Move Skinned Joints %s on %d skinCluster(s): %s"
                % ("ON" if enabled else "OFF", len(clusters), ", ".join(clusters))
            )
            self._report_bind_poses(result["bindPoses"])
            self.refresh()
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _report_bind_poses(self, report, verb="Bind poses"):
        """Print what the bind-pose sweep did. ``None`` = it did not run."""
        if not report:
            return
        if report.get("skipped"):
            self._log("   %s NOT refreshed: %s" % (verb, report["skipped"]))
            return
        if not report["checked"]:
            self._log("   %s: this rig has no bindPose node to refresh." % verb)
            return
        for entry in report["poses"]:
            if entry["refreshed"]:
                self._log(
                    "   %s: %d joint(s) refreshed — %s"
                    % (entry["pose"], len(entry["refreshed"]),
                       ", ".join(entry["refreshed"][:6])
                       + (" +%d more" % (len(entry["refreshed"]) - 6)
                          if len(entry["refreshed"]) > 6 else ""))
                )
        if not report["reset"]:
            self._log("   %s already match the rig — nothing needed refreshing "
                      "(%d joint(s) checked)." % (verb, report["checked"]))

    def _freshen_poses(self):
        try:
            report = core.freshen_bind_poses()
            if not report["checked"]:
                self._log("Freshen bind poses: this scene has no bindPose node "
                          "to refresh.", alert=True)
                return
            if report["reset"]:
                self._log("Freshened the bind pose — %d joint(s) were out of date."
                          % report["reset"])
            self._report_bind_poses(report)
        except core.SkinBenchError as error:
            self._log("FRESHEN BIND POSES BLOCKED (nothing happened): %s" % error,
                      alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _mirror_weights(self, scene_selection):
        try:
            if scene_selection:
                clusters = None          # core reads the current selection
            else:
                cluster = self.mirror_cluster.currentText()
                if not cluster:
                    self._log("Pick a skinCluster in the box above first.")
                    return
                clusters = [cluster]
            reports = core.mirror_skin_weights(
                clusters,
                from_positive_x=self.weight_direction.currentData(),
            )
            for report in reports:
                self._log(
                    "Mirrored weights on %s — rewrote %d vert(s), biggest change "
                    "%.4f (proof: diff vs the backup)"
                    % (report["cluster"], report["changed"], report["max_delta"])
                )
                for path in report.get("snapshots") or []:
                    self._log("   · backed up to %s" % os.path.basename(path))
                if not report["changed"]:
                    self._log("   · nothing changed — the far side already matched.")
                if report["checked"]:
                    self._log(
                        "   · twin-check: worst %.6f over %d exact pair(s)"
                        % (report["worst"], report["checked"])
                    )
                    if report["worst"] > 1e-4:
                        self._log(
                            "   !! the two sides still do NOT agree — check the "
                            "skeleton is mirrored first, then undo (Ctrl+Z)."
                        )
                else:
                    self._log(
                        "   ! twin-check CANNOT verify this mesh — no exact vertex "
                        "pairs across x=0. The rewrite above still happened; judge "
                        "it BY EYE (bend the far side)."
                    )
                if report["unmatched"]:
                    self._log(
                        "   · %d vert(s) have no exact twin (mesh asymmetric there, "
                        "or rig posed) — invisible to the twin-check, NOT skipped "
                        "by the mirror." % report["unmatched"]
                    )
                if report["midline"] and report["midline_worst"] > 1e-4:
                    self._log(
                        "   · %d midline vert(s) carry one-sided paint, worst %.3f "
                        "— a mirror cannot change a vertex that is its own twin."
                        % (report["midline"], report["midline_worst"])
                    )
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _check_symmetry(self):
        try:
            cluster = self.mirror_cluster.currentText()
            if not cluster:
                self._log("Pick a skinCluster first.")
                return
            worst, unmatched, checked, midline_worst, midline = \
                core.weight_symmetry(cluster)
            if not checked:
                self._log(
                    "%s — CANNOT CHECK by twins: no exact vertex pairs across "
                    "x=0 (mesh asymmetric, or rig posed). This is not a PASS. "
                    "A whole-cluster mirror will still rewrite the far side — "
                    "judge that by eye." % cluster
                )
            else:
                self._log(
                    "%s — sides differ by %.6f at worst, over %d exact pair(s)"
                    % (cluster, worst, checked)
                )
                if worst <= 1e-4:
                    self._log("   · already symmetric. Nothing to mirror.")
                else:
                    self._log("   · the two sides differ — mirroring will change things.")
            if midline and midline_worst > 1e-4:
                self._log(
                    "   · %d midline vert(s) carry one-sided paint (worst %.3f) — "
                    "mirroring will NOT fix these; they are their own twin."
                    % (midline, midline_worst)
                )
            if unmatched:
                self._log(
                    "   · %d vert(s) have no exact twin — invisible to this check."
                    % unmatched
                )
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _mirror_weights_selection(self):
        try:
            reports = core.mirror_weights_on_selection(
                from_positive_x=self.weight_direction.currentData(),
                match=self.mirror_match.currentData(),
            )
            for report in reports:
                name = report["mesh"].split("|")[-1]
                if report["skipped"]:
                    self._log("   ! %s skipped (%s)" % (name, report["skipped"]))
                    continue
                self._log(
                    "Mirror targeted %d of %d selected vert(s) on %s — "
                    "verified %d write(s), changed %d vs the backup"
                    % (
                        report["attempted"],
                        report["selected"],
                        name,
                        report["written"],
                        report["changed"],
                    )
                )
                if report["written"] != report["attempted"]:
                    self._log(
                        "   !! write-check FAILED — worst mismatch %.6f. "
                        "Restore the backup before continuing."
                        % report["write_worst"]
                    )
                for message in selection_mirror_pair_messages(report):
                    self._log(message)
                if report.get("snapshot"):
                    self._log("   · backed up to %s"
                              % os.path.basename(report["snapshot"]))
                if report["worst"] is not None and report["worst"] > 1e-4:
                    self._log("   !! the pairs do NOT agree — undo (Ctrl+Z).")
                if report["midline"]:
                    self._log(
                        "   · %d sat ON the midline — nothing to mirror."
                        % report["midline"]
                    )
                if report["unpaired_influences"]:
                    self._log(
                        "   ! %d influence(s) have a twin joint that is NOT in this "
                        "cluster — their weights mirror onto themselves."
                        % report["unpaired_influences"]
                    )
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _reload_poses(self):
        self.pose_list.clear()
        for path in core.list_pose_files():
            item = QtWidgets.QListWidgetItem(
                os.path.splitext(os.path.basename(path))[0])
            item.setData(QtCore.Qt.UserRole, path)
            self.pose_list.addItem(item)

    def _save_pose(self):
        try:
            name = self.pose_name.text().strip()
            path = core.save_pose(name)
            self._log("Saved pose '%s' → %s" % (name, os.path.basename(path)))
            self._reload_poses()
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _apply_pose(self, world_space):
        try:
            items = self.pose_list.selectedItems()
            if not items:
                self._log("Pick a pose in the list first.")
                return
            path = items[0].data(QtCore.Qt.UserRole)
            report = core.apply_pose(path, world_space=world_space)
            mode = ("world-space — roughly right, nudge from here"
                    if world_space else "exact")
            self._log("Applied pose '%s' to %d joint(s) — %s. Ctrl+Z or "
                      "Zero to undo." % (report["name"], report["applied"], mode))
            if report["missing"]:
                shown = ", ".join(report["missing"][:8])
                if len(report["missing"]) > 8:
                    shown += ", …"
                self._log("   ! %d pose joint(s) have no same-named joint "
                          "here (skipped): %s" % (len(report["missing"]), shown))
            if report["locked"]:
                self._log("   ! %d joint(s) refused the rotation (locked "
                          "channels): %s" % (len(report["locked"]),
                                             ", ".join(report["locked"][:8])))
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _zero_pose(self):
        try:
            report = core.zero_pose()
            self._log("Zeroed %d joint(s) back to rest." % report["cleared"])
            if report["locked"]:
                self._log("   ! %d joint(s) refused (locked): %s"
                          % (len(report["locked"]),
                             ", ".join(report["locked"][:8])))
        except Exception:
            self._log(traceback.format_exc())

    def _set_preserve_children(self, enabled):
        try:
            state = core.preserve_child_position(enabled)
            self._log(
                "Preserve Children %s on the Move / Rotate / Scale tools.%s"
                % (
                    "ON" if state else "OFF",
                    "  Bake the hierarchy when you are done." if state else "",
                )
            )
            self._show_preserve_children_state(state)
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    # -- Preserve Children watcher -----------------------------------------

    _PC_STYLE = (
        "QLabel { color: #ffd9a3; background: #4a3416; border: 1px solid #d08a3a;"
        " border-radius: 3px; padding: %dpx; font-weight: bold; }"
    )
    _PC_STYLE_LOUD = (
        "QLabel { color: #ffffff; background: #b5561f; border: 1px solid #ffb066;"
        " border-radius: 3px; padding: %dpx; font-weight: bold; }"
    )

    def _show_preserve_children_state(self, on):
        """Show the amber banner while the option is on, and watch for rotates.

        The timer only exists while the option is ON, so nothing polls Maya
        the rest of the time.
        """
        self._pc_warning.setStyleSheet(self._PC_STYLE % _px(6))
        self._pc_warning.setVisible(bool(on))
        if on:
            self._pc_seen = self._joint_rotations()
            if not hasattr(self, "_pc_timer"):
                self._pc_timer = QtCore.QTimer(self)
                self._pc_timer.setInterval(400)
                self._pc_timer.timeout.connect(self._watch_preserve_children)
            self._pc_timer.start()
        elif hasattr(self, "_pc_timer"):
            self._pc_timer.stop()

    def _joint_rotations(self):
        """Every selected joint's rotate, as plain numbers. Read-only."""
        try:
            joints = cmds.ls(selection=True, type="joint", long=True) or []
            return dict(
                (joint, tuple(cmds.getAttr(joint + ".rotate")[0]))
                for joint in joints
            )
        except Exception:
            return {}

    def _watch_preserve_children(self):
        """If a selected joint just rotated, make the banner shout once."""
        now = self._joint_rotations()
        if now and self._pc_seen and now != self._pc_seen:
            self._pc_warning.setStyleSheet(self._PC_STYLE_LOUD % _px(6))
            QtCore.QTimer.singleShot(1200, self._calm_preserve_children)
        self._pc_seen = now

    def _calm_preserve_children(self):
        try:
            self._pc_warning.setStyleSheet(self._PC_STYLE % _px(6))
        except RuntimeError:
            pass        # window closed mid-flash

    def _bake_orient(self):
        try:
            baked, skipped = core.bake_rotation_to_orient(
                include_hierarchy=self.bake_hierarchy.isChecked()
            )
            if not baked and not skipped:
                self._log("Nothing to bake — rotate is already 0,0,0 on the selection.")
                return
            for joint in baked:
                self._log("Baked rotate -> jointOrient: %s" % joint.split("|")[-1])
            for joint, reason in skipped:
                self._log("   ! skipped %s (%s)" % (joint.split("|")[-1], reason))
            self._log("%d joint(s) baked, %d skipped." % (len(baked), len(skipped)))
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _mirror_joint(self):
        try:
            report = core.mirror_selected_joints(
                mirror_position=self.mirror_position.isChecked(),
                include_hierarchy=self.mirror_hierarchy.isChecked(),
            )
            pairs = report["pairs"]
            self._log(
                "Mirrored %d joint(s) across %d skinCluster(s): %s -> %s"
                % (
                    len(pairs),
                    len(report["clusters"]),
                    pairs[0][0].split("|")[-1],
                    pairs[0][1].split("|")[-1],
                )
                + ("  (+%d more)" % (len(pairs) - 1) if len(pairs) > 1 else "")
            )
            self._log(
                "   orient err %.9f   mesh moved %.9f"
                % (report["orient_error"], report["mesh_moved"])
            )
            if report["mesh_moved"] > 1e-6:
                self._log("   !! THE MESH FOLLOWED THE JOINTS — undo (Ctrl+Z) and tell someone.")
            if report["dragged"]:
                self._log(
                    "   !! %d child joint(s) were SWUNG, not mirrored: %s"
                    % (
                        len(report["dragged"]),
                        ", ".join(c.split("|")[-1] for c in report["dragged"][:6]),
                    )
                )
                self._log("      Undo, tick 'Mirror the whole chain below it', run again.")
            if report["unpaired"]:
                self._log(
                    "   ! no twin found for: %s"
                    % ", ".join(j.split("|")[-1] for j in report["unpaired"][:6])
                )
            self._report_bind_poses(report.get("bindPoses"))
            self.refresh()
        except core.SkinBenchError as error:
            self._log("MIRROR JOINT BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _detach(self):
        try:
            reports = core.detach_selection()
            for report in reports:
                self._log(
                    "Detached %s (was %s) -> %s"
                    % (
                        report["mesh"].split("|")[-1],
                        report["skinCluster"],
                        os.path.basename(report["snapshot"]),
                    )
                )
            self.refresh()
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _current_snapshot(self):
        item = self.snapshot_list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item else None

    def _reattach(self):
        try:
            mesh = self._selected_mesh()
            snapshot = self._current_snapshot()
            if not snapshot:
                self._log("! Pick a snapshot from the list first.")
                return
            report = core.reattach(
                mesh, snapshot, restore_bind_matrices=self.restore_matrices.isChecked()
            )
            self._report(report, "Reattached")
            self.refresh()
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _apply_weights(self):
        try:
            mesh = self._selected_mesh()
            snapshot = self._current_snapshot()
            if not snapshot:
                self._log("! Pick a snapshot from the list first.")
                return
            report = core.apply_weights(mesh, snapshot)
            self._report(report, "Applied weights to")
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _snapshot_only(self):
        try:
            mesh = self._selected_mesh()
            path, _ = core.save_snapshot(mesh)
            self._log("Snapshot saved: %s" % os.path.basename(path))
            self._reload_snapshots()
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _paint_joint(self):
        try:
            cluster = self.bleed_cluster.currentText()
            joints = [
                item.data(QtCore.Qt.UserRole) for item in self.bleed_joints.selectedItems()
            ]
            if not joints:
                self._log("PAINT BLOCKED (nothing happened): pick the joint you "
                          "want to see painted.", alert=True)
                return
            if len(joints) > 1:
                self._log("Painting shows one joint at a time — using %s."
                          % joints[0].split("|")[-1])
            report = core.paint_influence(joint=joints[0], cluster=cluster or None)
            self._log("Painting %s on %s — white is 1.0, black is 0."
                      % (report["joint"], report["mesh"]))
        except core.SkinBenchError as error:
            self._log("PAINT: %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _invert_region(self):
        try:
            count = core.invert_region()
            self._log("Inverted: %d vertex(es) now selected — everything the "
                      "region was NOT." % count)
        except core.SkinBenchError as error:
            self._log("INVERT BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _grow(self):
        try:
            axis = self.grow_direction.currentData()
            report = core.grow_selection(steps=self.grow_steps.value(), axis=axis)
            where = " %s only" % self.grow_direction.currentText().strip() if axis else ""
            self._log(
                "Grew %d ring(s)%s — %d vertex(es) selected, %d new.%s"
                % (report["steps"], where, report["after"], report["added"],
                   "  %d skipped, wrong side." % report["wrongSide"]
                   if report["wrongSide"] else "")
            )
            if not report["added"]:
                self._log(
                    "Nothing new to add — the selection already reaches the edge "
                    "of the mesh, or everything next to it is the wrong way.",
                    alert=True,
                )
        except core.SkinBenchError as error:
            self._log("GROW BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _region(self, isolate):
        button = "SELECT + ISOLATE" if isolate else "SELECT REGION"
        try:
            cluster = self.bleed_cluster.currentText()
            joints = [
                item.data(QtCore.Qt.UserRole) for item in self.bleed_joints.selectedItems()
            ]
            if not joints:
                self._log(
                    "%s BLOCKED (nothing happened): pick the joint(s) in the list "
                    "— or select them in Maya, then Refresh." % button, alert=True)
                return
            report = core.select_influenced(
                joints=joints,
                cluster=cluster or None,
                include_hierarchy=self.region_hierarchy.isChecked(),
                isolate=isolate,
            )
            who = ", ".join(report["joints"][:4])
            if len(report["joints"]) > 4:
                who += " +%d more" % (len(report["joints"]) - 4)
            self._log(
                "Region: %d vertex(es) driven by %s%s"
                % (len(report["vertices"]), who,
                   " — isolated" if report["panel"] else "")
            )
        except core.SkinBenchError as error:
            self._log("%s BLOCKED (nothing happened): %s" % (button, error), alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _isolate_off(self):
        try:
            self._log("Isolate off on %s." % core.isolate_selection(False))
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _zero_on_selection(self):
        try:
            cluster = self.bleed_cluster.currentText()
            joints = [
                item.data(QtCore.Qt.UserRole) for item in self.bleed_joints.selectedItems()
            ]
            if not joints:
                self._log("ZERO BLOCKED (nothing happened): pick the joint(s) to "
                          "zero in the list above.", alert=True)
                return
            report = core.zero_influence_on_selection(
                joints=joints,
                cluster=cluster or None,
            )

            who = ", ".join(report["joints"][:4])
            if len(report["joints"]) > 4:
                who += " +%d more" % (len(report["joints"]) - 4)
            self._log(
                "Zeroed %s on %s — %d cleared, %d kept (dominant), %d skipped, "
                "%d already clean; %.3f weight removed."
                % (who, ", ".join(report["shapes"]) or "nothing",
                   report["cleared"], report["dominant"], report["skipped"],
                   report["untouched"], report["weightRemoved"])
            )
            if report["dominant"]:
                self._log("   %d vertex(es) KEPT — this joint is their main owner. "
                          "That is the real shoulder, not bleed." % report["dominant"])
            for path in report["snapshots"]:
                self._log("   snapshot: %s" % os.path.basename(path))
            if report["ignoredShapes"]:
                self._log("   ! selection spilled onto another mesh, ignored: %s"
                          % ", ".join(report["ignoredShapes"]))
            if report["skipped"]:
                self._log("   %d vertex(es) are driven ONLY by these joints — left "
                          "alone. Zeroing them would collapse the vertex."
                          % report["skipped"])
            if not report["cleared"]:
                self._log("Nothing changed. Wrong joint for this selection, or the "
                          "bleed is already gone.")
            self._reload_snapshots()
        except core.SkinBenchError as error:
            self._log("ZERO BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _heal_selection(self):
        try:
            report = core.heal_selection()
            self._log(
                "Healed %d vertex(es) on %s%s."
                % (report["healed"], ", ".join(report["shapes"]) or "nothing",
                   " — %d unmoved" % report["unchanged"]
                   if report["unchanged"] else "")
            )
            if report["healed"] > core.HEAL_CROWD:
                self._log("   ! %d vertices is a lot for Hammer — it re-averages "
                          "every vertex you give it, not just the bad ones. "
                          "Check the good paint nearby did not soften."
                          % report["healed"], alert=True)
            if report["unlocked"]:
                self._log("   unlocked %d influence(s) first — %s. They STAY "
                          "unlocked; a locked influence is skipped in silence."
                          % (len(report["unlocked"]),
                             ", ".join(report["unlocked"][:6])))
            for joint, count in sorted(
                    report["joints"].items(), key=lambda pair: -pair[1])[:5]:
                self._log("   %s changed on %d vertex(es)" % (joint, count))
            for line in report["examples"]:
                self._log("   %s" % line)
            if report["healed"] > len(report["examples"]):
                self._log("   full before/after list is in today's log file.")
            for path in report["snapshots"]:
                self._log("   snapshot: %s" % os.path.basename(path))
            if report["ignoredShapes"]:
                self._log("   ! selection spilled onto another mesh, ignored: %s"
                          % ", ".join(report["ignoredShapes"]))
            if not report["healed"]:
                self._log("Nothing changed — these vertices already match their "
                          "neighbours. The bad one is somewhere else.")
            self._reload_snapshots()
        except core.SkinBenchError as error:
            self._log("HEAL BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _ready_to_drag(self):
        try:
            mode = self.drag_constraint.currentData()
            report = core.ready_to_drag(constraint=mode)
            plain = {"surface": "sliding along the skin",
                     "edge": "sliding along the edges",
                     "none": "moving freely"}.get(report["constraint"],
                                                  report["constraint"])
            self._log("Ready — Move tool on %s, vertex mode, %s. Click a "
                      "vertex and drag it where it belongs."
                      % (report["mesh"], plain))
            if not report["honoured"]:
                self._log("   ! Maya would not take that constraint and left it "
                          "at '%s'." % report["constraint"], alert=True)
        except core.SkinBenchError as error:
            self._log("READY BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _clear_drags(self):
        try:
            report = core.clear_drags()
            if report["cleared"]:
                scope = (
                    " Scope: every skinned polygon mesh in the scene."
                    if report.get("scope") == "scene"
                    else " Scope: current selection."
                )
                self._log("Put %d dragged vertex(es) back on %s. Weights "
                          "untouched.%s" % (
                              report["cleared"],
                              ", ".join(sorted(set(report["shapes"]))),
                              scope,
                          ))
            elif not report.get("skipped"):
                self._log("No drags left on the mesh — nothing to put back.")
            if report.get("skipped"):
                names = ", ".join(item["shape"] for item in report["skipped"])
                self._log(
                    "   ! %d skinned shape%s could not be inspected or cleared: "
                    "%s. Treat %s as unverified."
                    % (
                        len(report["skipped"]),
                        "" if len(report["skipped"]) == 1 else "s",
                        names,
                        "it" if len(report["skipped"]) == 1 else "them",
                    ),
                    alert=True,
                )
        except core.SkinBenchError as error:
            self._log("CLEAR BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _drag_solve(self, dry_run):
        button = "PREVIEW SOLVE" if dry_run else "SOLVE"
        try:
            report = core.solve_dragged_vertices(
                max_influences=self.drag_max_influences.value(),
                dry_run=dry_run,
            )
            head = "Preview — nothing written." if dry_run else "Solved and painted."
            self._log("%s %d vertex(es) solved, %d refused, on %s."
                      % (head, len(report["solved"]), len(report["refused"]),
                         ", ".join(sorted(set(report["shapes"]))) or "nothing"))

            for row in report["solved"][:8]:
                self._log("   %s.vtx[%d] — delivers %d%% of your drag, %.4f short"
                          % (row["shape"], row["vertex"],
                             int(round(row["gain"] * 100)), row["residual"]))
                self._log("      %s" % ",  ".join(
                    "%s %d%%" % (joint, int(round(weight * 100)))
                    for joint, weight in row["share"][:6]))
                if "landed" in row:
                    self._log("      measured after painting: %.4f from where "
                              "you put it" % row["landed"])
            if len(report["solved"]) > 8:
                self._log("   …and %d more; full list in today's log file."
                          % (len(report["solved"]) - 8))

            for row in report["refused"][:6]:
                self._log("   %s.vtx[%d] REFUSED — %s"
                          % (row["shape"], row["vertex"], row["message"]),
                          alert=True)

            for path in report["snapshots"]:
                self._log("   snapshot: %s" % os.path.basename(path))
            if dry_run and report["solved"]:
                self._log("Happy with the shares above? Press "
                          "'Paint the solved weights'.")
            if not dry_run and report["written"]:
                self._reload_snapshots()
        except core.SkinBenchError as error:
            self._log("%s BLOCKED (nothing happened): %s" % (button, error),
                      alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _patch_joints(self, button):
        """The joints for the patch buttons, or None after logging why not."""
        joints = [
            item.data(QtCore.Qt.UserRole) for item in self.bleed_joints.selectedItems()
        ]
        if not joints:
            self._log(
                "%s BLOCKED (nothing happened): pick the joint whose home patch "
                "you want to keep." % button, alert=True)
            return None
        return joints

    def _patch_preview(self):
        button = "PREVIEW ISLANDS"
        try:
            joints = self._patch_joints(button)
            if not joints:
                return
            report = core.select_outside_patch(
                joints=joints,
                cluster=self.bleed_cluster.currentText() or None,
                link_floor=self.patch_stickiness.value(),
                keep_radius=self.patch_reach.value(),
            )
            self._remember_patch_dials()

            who = ", ".join(report["joints"][:4])
            if len(report["joints"]) > 4:
                who += " +%d more" % (len(report["joints"]) - 4)
            self._log(
                "Patch check on %s — %d vertex(es) carry this joint, in %d "
                "separate patch(es)."
                % (who, report["carrying"], report["islands"])
            )
            self._log(
                "   HOME patch: %d vertex(es), %.3f weight. That is where this "
                "joint really works."
                % (len(report["homeVertices"]), report["homeMass"])
            )
            self._log(
                "   %d other patch(es) — %d vertex(es), %.3f weight%s. Not "
                "joined to home, so they are the bleed."
                % (report["candidateIslands"], len(report["candidates"]),
                   report["weightRemoved"],
                   " (%d of them invisible dust)" % report["dust"]
                   if report["dust"] else "")
            )
            if report["disagreement"]:
                self._log(
                    "   !! HEADS UP: this joint's BIGGEST lump of paint (%.3f "
                    "weight) is NOT the one at the joint (%.3f). Keeping the "
                    "one at the joint, as asked — but the joint's main paint "
                    "living somewhere else is a rig issue worth a look before "
                    "you delete anything."
                    % (report["heaviestMass"], report["homeMass"]),
                    alert=True,
                )
            if report["otherNearVertices"]:
                self._log(
                    "   note: %d vertex(es) sit in OTHER patches that also reach "
                    "the joint. Only the biggest one is home, so those are in "
                    "the selection — Ctrl+drag them out if you want them kept."
                    % report["otherNearVertices"]
                )
            if report["needsRepaint"]:
                self._log(
                    "   %d vertex(es) left out — this joint is their main owner "
                    "and almost nothing else holds them. Deleting it would leave "
                    "them flapping, so repaint those by hand."
                    % len(report["needsRepaint"])
                )

            if not report["candidates"]:
                self._log("Nothing to delete — all of this joint's paint is "
                          "joined to its home patch.")
                return
            self._log(
                "Selected %d vertex(es). Nothing written yet — add with "
                "Shift+drag, remove with Ctrl+drag, or use Grow/Shrink, then "
                "press the button below." % len(report["candidates"])
            )
            self._log(
                "   Can't see them? Press F to frame the selection, and "
                "Show all if the view is still isolated."
            )
        except core.SkinBenchError as error:
            self._log("%s BLOCKED (nothing happened): %s" % (button, error), alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _patch_write(self):
        button = "ZERO OUTSIDE PATCH"
        try:
            joints = self._patch_joints(button)
            if not joints:
                return
            report = core.zero_patch_selection(
                joints=joints,
                cluster=self.bleed_cluster.currentText() or None,
            )
            who = ", ".join(report["joints"][:4])
            if len(report["joints"]) > 4:
                who += " +%d more" % (len(report["joints"]) - 4)
            self._log(
                "Zeroed %s on the %d vertex(es) that were SELECTED — not "
                "re-found, exactly what was on screen. %.3f weight removed."
                % (who, report["selected"], report["weightRemoved"])
            )
            self._log("   %d cleared, %d already clean."
                      % (report["zeroed"], report["untouched"]))
            for path in report["snapshots"]:
                self._log("   snapshot: %s" % os.path.basename(path))
            if report["needsRepaint"]:
                self._log(
                    "   %d vertex(es) LEFT ALONE — this joint is their main "
                    "owner and almost nothing else holds them. Repaint by hand."
                    % len(report["needsRepaint"])
                )
            if report["ignoredShapes"]:
                self._log("   ! selection spilled onto another mesh, ignored: %s"
                          % ", ".join(report["ignoredShapes"]))
            if not report["zeroed"]:
                self._log("Nothing changed — this joint holds no weight on the "
                          "selected vertices.")
            self._reload_snapshots()
        except core.SkinBenchError as error:
            self._log("%s BLOCKED (nothing happened): %s" % (button, error), alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _shrink(self):
        try:
            report = core.shrink_selection(steps=self.grow_steps.value())
            self._log(
                "Shrank %d ring(s) — %d vertex(es) selected, %d peeled off."
                % (report["steps"], report["after"], report["removed"])
            )
            if not report["removed"]:
                self._log(
                    "Nothing to peel — the selection has no outer ring to lose "
                    "(it covers the whole shell, or one more step would empty it).",
                    alert=True,
                )
        except core.SkinBenchError as error:
            self._log("SHRINK BLOCKED (nothing happened): %s" % error, alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _bleed(self, dry_run):
        # Name the button in every refusal. The posed-rig guard lives in
        # core.bleed_report, which Preview and Strip share -- so its message
        # used to arrive with no clue which click it belonged to, and Preview
        # read as a dead button during the artist's live ROM sweep.
        button = "PREVIEW" if dry_run else "STRIP"
        try:
            cluster = self.bleed_cluster.currentText()
            if not cluster:
                self._log(
                    "%s BLOCKED (nothing happened): pick a skinCluster first "
                    "— select the mesh, then Refresh." % button, alert=True)
                return
            joints = [
                item.data(QtCore.Qt.UserRole) for item in self.bleed_joints.selectedItems()
            ]
            report = core.strip_bleed(
                cluster=cluster,
                joints=joints or None,
                n=self.bleed_n.value(),
                dry_run=dry_run,
            )

            who = ", ".join(report["audited"][:4]) or "nothing"
            if len(report["audited"]) > 4:
                who += " +%d more" % (len(report["audited"]) - 4)
            # Name every bucket, every time. The artist read "blue paint that is not
            # selected" as the tool being broken; it was three different correct
            # answers with no labels on them. The four counts add up to
            # "vertices this joint touches at all", on purpose.
            self._log(
                "Bleed @ N=%g on %s — %d vertex(es) carry this joint, split into:"
                % (report["n"], who, report["carrying"])
            )
            self._log(
                "   %d strippable (SELECTED) — too far to reach, %.3f weight. "
                "This is the junk." % (len(report["vertices"]), report["totalWeight"])
            )
            self._log(
                "   %d kept — far, but this joint is their MAIN owner. Reassign "
                "or repaint those; never strip them." % len(report["kept"])
            )
            self._log(
                "   %d ignored — invisible dust under %.3f (%.3f weight all told). "
                "Clear it with Maya's Skin > Prune Small Weights at %.3f."
                % (report["dust"], report["floor"], report["dustWeight"],
                   report["floor"])
            )
            self._log(
                "   %d fine — close enough to the joint to be real weight."
                % report["near"]
            )
            for joint, reason in report["skipped"]:
                self._log("   ! skipped %s (%s)" % (joint, reason))
            for pair in report["pairs"][:8]:
                self._log(
                    "   vtx %-7d %-26s w=%.4f  %.1f bone lengths%s"
                    % (pair["vertex"], pair["joint"], pair["weight"], pair["ratio"],
                       "  DOMINANT — kept" if pair["dominant"] else "")
                )

            if not report["vertices"]:
                self._log("Nothing to strip at this N. Lower it to catch more.")
                return
            if dry_run:
                core.select_vertices(report["skinCluster"], report["vertices"])
                self._log(
                    "Preview only — those %d vertices are SELECTED. Look at them, "
                    "then Strip." % len(report["vertices"])
                )
                self._log(
                    "   Can't see them? Press F to frame the selection, and "
                    "Show all if the view is still isolated."
                )
            else:
                self._log(
                    "Stripped %d vertex(es). Snapshot: %s"
                    % (report["strippedVertices"], os.path.basename(report["snapshot"]))
                )
        except core.SkinBenchError as error:
            self._log("%s BLOCKED (nothing happened): %s" % (button, error), alert=True)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _sync_photo(self):
        try:
            cluster = self.photo_cluster.currentText()
            joint = self.photo_joint.currentText()
            reference = self.photo_reference.currentText()
            if not (cluster and joint and reference):
                self._log("! Pick a skinCluster, a joint, and a reference influence.")
                return
            core.sync_bind_photo(cluster, joint, reference)
            self._log("Bind photo synced: %s on %s (ref %s)" % (joint, cluster, reference))
        except core.SkinBenchError as error:
            self._log("! %s" % error)
        except Exception:
            self._log(traceback.format_exc())

    def _open_snapshot_folder(self):
        path = core.snapshot_dir()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        self._log("Snapshots: %s" % path)

    def _selected_mesh(self):
        selection = cmds.ls(selection=True, long=True, type="transform") or []
        meshes = [
            node for node in selection if cmds.listRelatives(node, shapes=True, type="mesh")
        ]
        if meshes:
            return meshes[0]
        mesh = self.mesh_field.text()
        if mesh and cmds.objExists(mesh):
            return mesh
        raise core.SkinBenchError("Select the mesh first.")

    # -- output ------------------------------------------------------------

    def _report(self, report, verb):
        self._log(
            "%s %s — %d influences, %d dropped"
            % (verb, report["skinCluster"], report["influences"], report["dropped"])
        )
        for old, new in report["remapped"]:
            self._log("   remapped %s -> %s" % (old.split("|")[-1], new.split("|")[-1]))
        for name in report["missing"]:
            self._log("   ! missing influence %s (weights renormalized)" % name.split("|")[-1])
        orphans = report.get("orphanedVertices") or []
        if orphans:
            self._log(
                "   ! %d vertices lost ALL their weight and are now selected — "
                "repaint them or reattach with the joints restored." % len(orphans)
            )
            core.select_vertices(report["skinCluster"], orphans)

    def _clear_log_pane(self):
        """Empty the PANE. The file keeps everything -- say so, every time."""
        self.log_view.clear()
        self._log("Screen cleared. Every earlier line is still in "
                  "%s." % os.path.basename(core.log_path()))

    def _open_log_folder(self):
        try:
            folder = core.log_dir()
            if hasattr(os, "startfile"):
                os.startfile(folder)                       # Windows
            else:
                import subprocess
                subprocess.Popen(["xdg-open", folder])
            self._log("Opened %s" % folder)
        except Exception:
            self._log(traceback.format_exc(), alert=True)

    def _log(self, message, alert=False):
        """One line to the pane, the Script Editor, and today's log file.

        ``alert=True`` also flashes the pane red. Use it whenever a button
        REFUSED — from the artist's side that is indistinguishable from a dead
        button unless something moves.
        """
        self.log_view.appendPlainText(message)
        self.log_view.verticalScrollBar().setValue(
            self.log_view.verticalScrollBar().maximum()
        )
        print("[SkinBench] %s" % message)
        core.write_log(message)
        if alert:
            self._flash_log()

    def _flash_log(self):
        """Paint the log pane red for a beat, then put it back.

        Opens the log first if it is closed: a red flash behind a collapsed
        pane is the same as no flash at all.
        """
        if not self.log_toggle.isChecked():
            self.log_toggle.setChecked(True)
        self.log_view.setStyleSheet(self._log_style(alert=True))
        QtCore.QTimer.singleShot(1400, self._unflash_log)

    def _unflash_log(self):
        try:
            self._apply_log_look()
        except RuntimeError:
            pass        # window closed while the flash was still running

    # -- remembered dials --------------------------------------------------

    def _settings(self):
        folder = os.path.join(cmds.internalVar(userAppDir=True), "skinbench")
        if not os.path.isdir(folder):
            os.makedirs(folder)
        return QtCore.QSettings(
            os.path.join(folder, "settings.ini"), QtCore.QSettings.IniFormat
        )

    def _restore_log_look(self):
        """Put the log's size/font/colours back, then paint the pane."""
        try:
            settings = self._settings()
            size = settings.value("log/size")
            font = settings.value("log/font")
            text = settings.value("log/text")
            background = settings.value("log/background")
            if size is not None:
                self._log_size = int(size)
            if font:
                self._log_font = font
            if text and background:
                self._log_colour = (text, background)
        except (ValueError, TypeError, RuntimeError, OSError):
            pass
        self._apply_log_look()

    def _remember_log_look(self):
        try:
            settings = self._settings()
            settings.setValue("log/size", int(self._log_size))
            settings.setValue("log/font", self._log_font)
            settings.setValue("log/text", self._log_colour[0])
            settings.setValue("log/background", self._log_colour[1])
        except (ValueError, TypeError, RuntimeError, OSError):
            pass

    def _restore_patch_dials(self):
        """Put stickiness and home reach back where they were left last time.

        Wrapped, like the disk log: a preference that cannot be read is not a
        reason to fail to open. The defaults are always a working answer.
        """
        try:
            settings = self._settings()
            stickiness = settings.value("patch/stickiness")
            reach = settings.value("patch/reach")
            if stickiness is not None:
                self.patch_stickiness.setValue(float(stickiness))
            if reach is not None:
                self.patch_reach.setValue(float(reach))
        except (ValueError, TypeError, RuntimeError, OSError):
            pass

    def _remember_patch_dials(self):
        try:
            settings = self._settings()
            settings.setValue("patch/stickiness", float(self.patch_stickiness.value()))
            settings.setValue("patch/reach", float(self.patch_reach.value()))
        except (ValueError, TypeError, RuntimeError, OSError):
            pass

    def _announce_log(self):
        """Say where the log file is, once, when the window opens."""
        path = core.write_log("SkinBench window opened.")
        self.log_view.setToolTip(
            "Every line here is also saved to:\n%s"
            % (path or "(disk log unavailable — the window log still works)")
        )
        if path:
            self._log("Log saved to %s" % path)
        else:
            self._log("! Could not write the log file. Everything else still works.")


def show():
    """Open (or raise) the SkinBench window."""
    global _window
    if _window is not None:
        try:
            _window.close()
            _window.deleteLater()
        except (RuntimeError, AttributeError):
            pass
    workspace = WINDOW_OBJECT_NAME + "WorkspaceControl"
    if cmds.workspaceControl(workspace, query=True, exists=True):
        cmds.deleteUI(workspace)
    _window = SkinBenchWindow()
    _window.show(dockable=True, retain=False)
    return _window
