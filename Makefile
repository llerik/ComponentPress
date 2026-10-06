PYTHON ?= python3.14
VENV_PYTHON := .venv/bin/python
READY := .venv/.componentpress-ready

.PHONY: run cli test setup

run: $(READY)
	$(VENV_PYTHON) -m componentpress $(ARGS)

cli: $(READY)
	$(VENV_PYTHON) -m componentpress $(ARGS)

test: $(READY)
	$(VENV_PYTHON) -m pytest $(ARGS)

setup: $(READY)

$(READY): Makefile pyproject.toml
	@if ! command -v "$(PYTHON)" >/dev/null 2>&1; then \
		echo "Python 3.14 was not found. Install it or set PYTHON=/path/to/python3.14." >&2; \
		exit 1; \
	fi
	@if [ ! -x "$(VENV_PYTHON)" ]; then "$(PYTHON)" -m venv .venv; fi
	"$(VENV_PYTHON)" -m pip install -e '.[test]'
	@touch "$(READY)"
