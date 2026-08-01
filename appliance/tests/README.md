# Appliance host-script tests (#395)

Host-portable pytest suite for the appliance host scripts.  These tests
run on any developer machine or CI runner with Python 3 and do NOT
require a database, Docker, or an appliance ISO.

## Files

| File | What it tests |
|---|---|
| `test_cluster_join_failure_reason.py` | `spatium-cluster-join`'s failure-reason classifier (#590) |
| `test_cluster_join_identity.py` | `spatium-cluster-join`'s cluster-identity wipe (#590) |
| `test_firewall_webui_sentinel.py` | Web UI reachability before the supervisor exists (#769) |
| `test_firstboot_member_guard.py` | `spatiumddi-firstboot` not re-seeding manifests on a joined member (#590) |
| `test_frontend_boot_gate.py` | SPA fallback landing on the initialising page (#767) |
| `test_grub_render.py` | `spatium-grub-render` renderer via `--print` (DRY-RUN) |
| `test_host_migrate.py` | `spatium-host-migrate` orchestrator via a patched subprocess |
| `test_install_done_gate.py` | The headless-install Done-screen gate |
| `test_preseed_lint.py` | `spatium-install --check-preseed` linter |
| `test_preseed_security.py` | The #549 preseed installer's security guards (#581) |
| `test_slot_status_active_version.py` | `spatium-upgrade-slot status` reading the active slot's version without a mount (#788) |
| `test_slot_upgrade_runner.py` | `spatiumddi-slot-upgrade` dead/stalled-apply guards (#421) |

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
