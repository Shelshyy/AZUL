"""
AZUL.py - public launcher for AZUL

This file stays simple and non-sensitive. It:
  - Finalizes core updates by swapping in a staged azul_core_new*.pyd file.
  - Imports azul_core, which contains the GUI, detection, controller, licensing, etc.

For distribution:
  - Ship this file as plain source (or as the entry point to your EXE).
  - Ship the compiled core .pyd (named CORE_LOCAL_FILENAME in azul_core.py) next to it.
"""

import os
import shutil

# These names must stay in sync with CORE_LOCAL_FILENAME and CORE_STAGED_FILENAME in azul_core.py
CORE_LOCAL_FILENAME = "azul_core.cp311-win_amd64.pyd"
CORE_STAGED_FILENAME = "azul_core_new.cp311-win_amd64.pyd"

def _finalize_core_update():
    """If an updated core has been staged, swap it in before importing azul_core."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    core_new = os.path.join(base_dir, CORE_STAGED_FILENAME)
    core_main = os.path.join(base_dir, CORE_LOCAL_FILENAME)

    if os.path.exists(core_new):
        try:
            # Back up existing core if present
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
                    # If we can't remove, still try to replace
                    pass
            # Swap staged core into place
            os.replace(core_new, core_main)
            print("[Azul] Swapped in updated core from", CORE_STAGED_FILENAME)

            # Optional cleanup: remove launcher backup if present
            launcher_backup = os.path.join(base_dir, "AZUL.py.bak")
            if os.path.exists(launcher_backup):
                try:
                    os.remove(launcher_backup)
                except Exception as e:
                    print("[Azul] Warning: could not remove launcher backup:", e)
        except Exception as e:
            print("[Azul] Failed to finalize core update:", e)

if __name__ == "__main__":
    _finalize_core_update()
    # Importing azul_core constructs and runs the full application.
    import azul_core  # noqa: F401
