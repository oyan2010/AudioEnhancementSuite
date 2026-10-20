"""
Audio Enhancement Automation Suite
Entry point.
"""

import sys
import os

# ── Ensure project root is on sys.path ────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ui.main_window import MainWindow
from ui.styles import STYLESHEET


def main():
    # High-DPI support
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("Audio Enhancement Suite")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("AudioSuite")

    # Default font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
