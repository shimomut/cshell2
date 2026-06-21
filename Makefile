VENV := .venv

ifeq ($(OS),Windows_NT)
    # Windows: venv scripts live in Scripts/, executables end in .exe.
    # Prefer python3.14 if it's already on PATH; otherwise fall back to the
    # `py` launcher.
    PY_ON_PATH := $(shell where python3.14 2>nul)
    ifneq ($(strip $(PY_ON_PATH)),)
        PYTHON_BOOTSTRAP := python3.14
    else
        PYTHON_BOOTSTRAP := py
    endif
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

$(info Using Python bootstrap: $(PYTHON_BOOTSTRAP))

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
