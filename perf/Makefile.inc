# ── SpatiumDDI performance test suite (perf/) ────────────────────────────────────
# Included by the root Makefile.  These targets run the OFF-BOX load + soak harness
# against a clean appliance.  See docs/PERFORMANCE_TESTING.md and perf/README.md.
# Everything uses python3 (no bare `python` on PATH) with perf/harness on PYTHONPATH.

.PHONY: perf-help perf-deps perf-validate perf-compile perf-dry perf-smoke perf-run \
        perf-resume perf-seed perf-status perf-stop perf-warroom perf-report \
        perf-queryset perf-tui

PERF_DIR   = perf
PERF_PY    = PYTHONPATH=$(PERF_DIR)/harness python3 -m spddi_perf.cli
MANIFEST  ?= $(PERF_DIR)/manifests/university-24h.yaml
PERF_ARGS ?=

perf-help:
	@echo "SpatiumDDI performance suite targets (off-box; see perf/README.md):"
	@echo "  make perf-deps                     pip install perf/requirements.txt (load-gen box)"
	@echo "  make perf-validate MANIFEST=...    load + print the resolved run plan"
	@echo "  make perf-compile                  py_compile every perf/ python file"
	@echo "  make perf-dry MANIFEST=...         dry engine run (no workers, compressed clock)"
	@echo "  make perf-smoke [PERF_ARGS=...]    the gated ~40min smoke run (smoke.yaml)"
	@echo "  make perf-run MANIFEST=...         a full run (default: university-24h.yaml)"
	@echo "  make perf-resume RUN_ID=...        continue a crashed/paused run"
	@echo "  make perf-seed MANIFEST=...        run only the seeder (provision + seed)"
	@echo "  make perf-queryset MANIFEST=...    build + safety-validate the DNS query set"
	@echo "  make perf-status [RUN_ID=...]      print state.json (latest run if omitted)"
	@echo "  make perf-stop  [RUN_ID=...]       trip the kill-switch (graceful ramp-to-zero)"
	@echo "  make perf-tui [MANIFEST=...] [RUN_ID=...]   interactive console — pick/start/watch a run"
	@echo "  make perf-warroom                  the terminal live-status fallback (§6.6)"
	@echo "  make perf-report RUN_ID=... [BASELINE=...]  (re)generate the run report"

perf-deps:
	python3 -m pip install -r $(PERF_DIR)/requirements.txt

perf-validate:
	$(PERF_PY) validate --manifest $(MANIFEST)

# Quick CI-ish check: every perf python file imports/compiles.
perf-compile:
	@cd $(PERF_DIR) && PYTHONPATH=harness python3 -m py_compile $$(find . -name '*.py' -not -path './run/*') \
	  && echo "✓ perf py_compile OK"

# Dry engine validation — exercises controller/phases/setpoints/checkpoint/watchdog
# with NO subprocess workers and a compressed clock. Needs no appliance.
perf-dry:
	$(PERF_PY) run --manifest $(MANIFEST) --no-workers --time-scale 1200

perf-smoke:
	$(PERF_PY) smoke $(PERF_ARGS)

perf-run:
	$(PERF_PY) run --manifest $(MANIFEST) $(PERF_ARGS)

perf-resume:
	@test -n "$(RUN_ID)" || { echo "usage: make perf-resume RUN_ID=<run_id>"; exit 2; }
	$(PERF_PY) resume --run-id $(RUN_ID) $(PERF_ARGS)

perf-seed:
	$(PERF_PY) seed --manifest $(MANIFEST)

perf-queryset:
	PYTHONPATH=$(PERF_DIR)/harness python3 $(PERF_DIR)/generators/dns/gen_dns_queryset.py \
	  --run-id queryset --run-root $(PERF_DIR)/run --manifest $(MANIFEST) --self-check

perf-status:
	$(PERF_PY) status $(if $(RUN_ID),--run-id $(RUN_ID),)

perf-stop:
	$(PERF_PY) stop $(if $(RUN_ID),--run-id $(RUN_ID),)

perf-tui:
	$(PERF_PY) tui $(if $(MANIFEST),--manifest $(MANIFEST),) $(if $(RUN_ID),--run-id $(RUN_ID),)

perf-warroom:
	bash $(PERF_DIR)/warroom/spatium-warroom.sh

perf-report:
	@test -n "$(RUN_ID)" || { echo "usage: make perf-report RUN_ID=<run_id> [BASELINE=<run_id>]"; exit 2; }
	$(PERF_PY) report --run-id $(RUN_ID) $(if $(BASELINE),--baseline $(BASELINE),)
