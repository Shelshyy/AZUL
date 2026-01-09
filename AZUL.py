import os
import shutil

# These names must stay in sync with CORE_LOCAL_FILENAME and CORE_STAGED_FILENAME in azul_core.py
CORE_LOCAL_FILENAME = "azul_core.cp311-win_amd64.pyd"
CORE_STAGED_FILENAME = "azul_core_new.cp311-win_amd64.pyd"


def _finalize_core_update() -> None:
    """If an updated core has been staged, swap it in before importing azul_core."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    core_new = os.path.join(base_dir, CORE_STAGED_FILENAME)
    core_main = os.path.join(base_dir, CORE_LOCAL_FILENAME)

    if not os.path.exists(core_new):
        return

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


def main() -> None:
    # Handle any staged core update first
    _finalize_core_update()

    # Load controller injection first so its patches/hooks are active
    try:
        import controller_injection_VC_ENUM_PATCH  # noqa: F401
    except Exception as e:
        print("[Azul] Warning: controller injection module failed to load:", e)

    # Then load the main core (compiled .pyd) and start it
    try:
        import azul_core  # noqa: F401
    except Exception as e:
        print("[Azul] Failed to import azul_core module:", e)
        return

    # Replicate the entrypoint logic that lived under `if __name__ == "__main__"`
    try:
        headless = getattr(azul_core, "HEADLESS_SERVER", False)
        if headless:
            # Prefer the web/localhost server entrypoint if it exists
            run_server = getattr(azul_core, "run_localhost_server", None)
            if callable(run_server):
                run_server()
                return

        # Fallback to Tk GUI mainloop if available
        app = getattr(azul_core, "app", None)
        if app is not None and hasattr(app, "mainloop"):
            app.mainloop()
        else:
            # As a last resort, just keep the process alive so background threads can run
            print("[Azul] Warning: no explicit entrypoint found in azul_core (run_localhost_server/app).")
    except Exception as e:
        print("[Azul] Error while starting AZUL core:", e)


if __name__ == "__main__":
    main()
