VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install test clean run

$(VENV)/bin/activate:
	python3.14 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e .
	$(PIP) install pytest

install: $(VENV)/bin/activate

test: $(VENV)/bin/activate
	$(VENV)/bin/pytest tests/ -v

run: $(VENV)/bin/activate
	$(PYTHON) -m cshell2

clean:
	rm -rf $(VENV)
	rm -rf src/cshell2.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
