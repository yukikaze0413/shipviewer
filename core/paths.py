"""Runtime path helpers for development and packaged builds."""

import os
import sys


def app_base_dir():
    """Directory that contains editable user data for this app."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_base_dir():
    """Directory that contains bundled program resources."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return app_base_dir()


def app_path(*parts):
    return os.path.normpath(os.path.join(app_base_dir(), *parts))


def resource_path(*parts):
    return os.path.normpath(os.path.join(resource_base_dir(), *parts))

