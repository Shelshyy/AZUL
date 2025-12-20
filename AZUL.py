"""
AZUL.py - public launcher for AZUL

This file stays simple and non-sensitive. It:
  - Finalizes core updates by swapping in azul_core_new.pyd -> azul_core.pyd
  - Imports azul_core, which contains the GUI, detection, controller, licensing, etc.

For distribution:
  - Ship this file as plain source (or as the entry point to your EXE).
  - Ship `azul_core.pyd` (compiled from azul_core.py) next to it.
"""

import os
import shutil

def _finalize_core_update():
    """If an updated core has been staged as azul_core_new.pyd, swap it in."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    core_new = os.path.join(base_dir, "azul_core_new.pyd")
    core_main = os.path.join(base_dir, "azul_core.pyd")

    if os.path.exists(core_new):
        try:
            if os.path.exists(core_main):
                backup = core_main + ".bak"
                try:
                    shutil.copy2(core_main, backup)
                    print("[Azul] Backup of existing core saved as", backup)
                except Exception as e:
                    print("[Azul] Warning: could not back up existing core:", e)
                try:
                    os.remove(core_main)
                except Exception:
                    # If we can't remove, we'll still try to replace
                    pass
            os.replace(core_new, core_main)
            print("[Azul] Swapped in updated core from azul_core_new.pyd.")
            # Optional cleanup: remove old backup files to keep folder tidy
            try:
                core_backup = core_main + ".bak"
                if os.path.exists(core_backup):
                    os.remove(core_backup)
                launcher_backup = os.path.join(base_dir, "AZUL.py.bak")
                if os.path.exists(launcher_backup):
                    os.remove(launcher_backup)
            except Exception as e:
                print("[Azul] Warning: could not remove backup files:", e)
        except Exception as e:
            print("[Azul] Failed to finalize core update:", e)

if __name__ == "__main__":
    _finalize_core_update()
    # Importing azul_core constructs and runs the full application.
    import azul_core  # noqa: F401
