VENV := .venv

ifeq ($(OS),Windows_NT)
    # Windows: venv scripts live in Scripts/, executables end in .exe
    PYTHON_BOOTSTRAP := c:/Python314/python.exe
    VENV_BIN := $(VENV)/Scripts
    PYTHON := $(VENV_BIN)/python.exe
    PIP := $(VENV_BIN)/pip.exe
    PYTEST := $(VENV_BIN)/pytest.exe
    VENV_STAMP := $(VENV_BIN)/activate
else
    # Unix: venv binaries live in bin/
    PYTHON_BOOTSTRAP := python3.14
    VENV_BIN := $(VENV)/bin
    PYTHON := $(VENV_BIN)/python
    PIP := $(VENV_BIN)/pip
    PYTEST := $(VENV_BIN)/pytest
    VENV_STAMP := $(VENV_BIN)/activate
endif

.PHONY: install test clean run install-launcher

$(VENV_STAMP):
	"$(PYTHON_BOOTSTRAP)" -m venv $(VENV)
	"$(PYTHON)" -m pip install --upgrade pip
	"$(PYTHON)" -m pip install -e .
	"$(PYTHON)" -m pip install pytest

install: $(VENV_STAMP)

test: $(VENV_STAMP)
	"$(PYTHON)" -m pytest tests/ -v

run: $(VENV_STAMP)
	"$(PYTHON)" -m cshell2

install-launcher: $(VENV_STAMP)
	"$(PYTHON)" scripts/install_launcher.py

clean:
	"$(PYTHON_BOOTSTRAP)" -c "import shutil, pathlib; shutil.rmtree('$(VENV)', ignore_errors=True); shutil.rmtree('src/cshell2.egg-info', ignore_errors=True); [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
