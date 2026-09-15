#!/bin/zsh
set -e

SCRIPT_DIR=${0:A:h}
PYTHON_BIN=/Users/chenshu/.pyenv/versions/3.12.9/bin/python

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN=$(command -v python3)
fi

cd "$SCRIPT_DIR"
exec "$PYTHON_BIN" acoustic_acquisition.py "$@"
