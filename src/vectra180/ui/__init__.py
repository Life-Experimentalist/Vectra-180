"""Optional desktop user interface.

Nothing here is imported by the headless service. ``launch`` pulls DearPyGui in
lazily so that a Pi without a display never pays for -- or fails on -- a GUI
dependency it does not have.
"""

from vectra180.ui.desktop import DesktopApp, launch

__all__ = ["DesktopApp", "launch"]
