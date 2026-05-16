VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: venv install test clean run

venv:
	python3.14 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .
	$(PIP) install pytest

install: venv

test: venv
	$(VENV)/bin/pytest tests/ -v

run: venv
	$(PYTHON) -m cshell2

clean:
	rm -rf $(VENV)
	rm -rf src/cshell2.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
