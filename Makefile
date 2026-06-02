VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
HOST    ?= 127.0.0.1
PORT    ?= 8000
BASE    := http://$(HOST):$(PORT)
# AUTO=approve|decline|error => start with an automatic response on.
AUTO    ?=

.DEFAULT_GOAL := help

.PHONY: help install run dev smoke clean console pay decline error stop down restart

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

$(VENV): requirements.txt ## Create venv and install deps
	python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt
	@touch $(VENV)

install: $(VENV) ## Install dependencies into the venv

run: $(VENV) ## Run the mock server (AUTO=approve|decline|error for auto-response)
	MENTA_MOCK_AUTO=$(AUTO) $(PY) -m uvicorn mock_menta:app --host $(HOST) --port $(PORT)

dev: $(VENV) ## Run with auto-reload (AUTO=approve|decline|error for auto-response)
	MENTA_MOCK_AUTO=$(AUTO) $(PY) -m uvicorn mock_menta:app --host $(HOST) --port $(PORT) --reload

stop: ## Stop whatever is serving on PORT (default 8000)
	@pid=$$(lsof -ti tcp:$(PORT) -sTCP:LISTEN); \
	if [ -n "$$pid" ]; then echo "killing $$pid on port $(PORT)"; kill $$pid; \
	else echo "nothing listening on port $(PORT)"; fi

down: stop ## Alias for stop

restart: stop ## Stop the server on PORT, then run a fresh one
	@sleep 1
	$(MAKE) run

console: ## Open the interactive console in a browser
	open $(BASE)/console || xdg-open $(BASE)/console

pay: ## Mark an intention paid:    make pay RID=<request_id>
	@curl -fsS -X POST "$(BASE)/__mock__/intentions/$(RID)/pay" && echo

decline: ## Mark an intention declined: make decline RID=<request_id>
	@curl -fsS -X POST "$(BASE)/__mock__/intentions/$(RID)/decline" && echo

error: ## Mark an intention errored:  make error RID=<request_id>
	@curl -fsS -X POST "$(BASE)/__mock__/intentions/$(RID)/error" && echo

smoke: $(VENV) ## Quick check: boot the server, hit /health, shut down
	@$(PY) -m uvicorn mock_menta:app --host $(HOST) --port $(PORT) & \
	SRV_PID=$$!; \
	sleep 2; \
	curl -fsS http://$(HOST):$(PORT)/health && echo " OK" || (kill $$SRV_PID; exit 1); \
	kill $$SRV_PID

clean: ## Remove the venv and caches
	rm -rf $(VENV) __pycache__ .pytest_cache
