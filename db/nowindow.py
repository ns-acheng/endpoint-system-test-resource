"""Import this first in any script that Task Scheduler runs under pythonw.exe.

pythonw has no console, so every console program it spawns (python.exe, git, schtasks,
powershell, wevtutil) gets a NEW console window that flashes on the user's desktop.
This makes subprocess create those children with CREATE_NO_WINDOW. It does nothing when
the process has a console (an interactive shell), so normal runs behave as before.
"""

import subprocess
import sys

if sys.platform == "win32":
    import ctypes

    if not ctypes.windll.kernel32.GetConsoleWindow():
        _orig_init = subprocess.Popen.__init__

        def _init(self, *args, **kwargs):
            if not kwargs.get("creationflags"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            _orig_init(self, *args, **kwargs)

        subprocess.Popen.__init__ = _init
