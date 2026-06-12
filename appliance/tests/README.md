# Appliance host-script tests (#395)

Host-portable pytest suite for the appliance host scripts.  These tests
run on any developer machine or CI runner with Python 3 and do NOT
require a database, Docker, or an appliance ISO.

## Files

| File | What it tests |
|---|---|
| `test_grub_render.py` | `spatium-grub-render` renderer via `--print` (DRY-RUN) |
| `test_host_migrate.py` | `spatium-host-migrate` orchestrator via a patched subprocess |

## How to run

```sh
# from the repo root:
python3 -m pytest appliance/tests/ -v

# or from this directory:
cd appliance/tests
pytest -v
```

`grub-script-check` tests are automatically skipped when the binary is not
on PATH (install `grub2-common` / `grub-common` to enable them).

## Notes

The orchestrator (`spatium-host-migrate`) hardcodes its working paths as
unconditional shell variable assignments rather than `${VAR:-/default}`
env-overridable forms.  Tests work around this by dynamically patching the
script text before running it in a subprocess (a safe, read-only rewrite of
just the path declarations + the appliance-gate check).  If the orchestrator
is ever refactored to support env-var overrides, the `_run_migrate()` helper
in `test_host_migrate.py` can be simplified accordingly.
