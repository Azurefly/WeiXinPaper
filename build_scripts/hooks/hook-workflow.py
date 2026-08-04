"""Override the unrelated PyPI ``workflow`` package hook.

The application imports the local ``backend/workflow.py`` module.  The
third-party hook bundled by PyInstaller assumes that this name belongs to a
separately installed distribution and tries to copy package metadata that the
local module does not have.
"""

datas = []
hiddenimports = []
