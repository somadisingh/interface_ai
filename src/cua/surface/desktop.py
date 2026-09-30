"""Desktop surface — designed, not built.

A native desktop application exposes an accessibility tree through the OS (Windows UI
Automation, macOS AX API, Linux AT-SPI). That tree has the same concepts the artifact schema
already uses, which is why the schema needs no changes to cover desktop apps:

=========================  =====================================================
Artifact concept           Desktop equivalent
=========================  =====================================================
``RoleLocator``            UIA ControlType / AX role + Name / AXTitle
``LabelLocator``           LabeledBy relation / AXTitleUIElement
``AnchorLocator``          sibling/parent traversal in the tree (same row/pane)
``TableCellLocator``       Grid/Table patterns (row, column header)
``TextLocator``            Name / AXValue of static text
``frame`` path             window → pane path (top-level window title, child panes)
=========================  =====================================================

Web-only fallbacks (``attribute``, ``css``) would simply be absent from desktop artifacts, and
a desktop surface would fall back to image-template matching as its last resort instead.

Handoff on desktop needs a shared session (e.g. an RDP/VNC session both the automation and
the operator attach to); the control-lease model is unchanged.
"""

from __future__ import annotations


class DesktopSurface:
    """Placeholder that documents the seam. Not implemented in this project."""

    def __init__(self) -> None:
        raise NotImplementedError(
            "Desktop surfaces are designed but not implemented; see this module's docstring."
        )
