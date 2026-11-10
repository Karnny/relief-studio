#!/bin/sh
# Launch the Relief Studio GUI with a Python that has numpy + Pillow.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)

if [ -n "$RELIEF_PYTHON" ]; then
  PY="$RELIEF_PYTHON"
elif [ -x "$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3" ]; then
  PY="$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3"
else
  PY=python3
fi

if ! "$PY" -c "import numpy, PIL" >/dev/null 2>&1; then
  echo "This Python ($PY) is missing numpy and/or Pillow." >&2
  echo "Install them:  $PY -m pip install numpy pillow" >&2
  echo "Or point RELIEF_PYTHON at a Python that has them." >&2
  exit 1
fi

exec "$PY" "$DIR/relief_gui.py" "$@"
