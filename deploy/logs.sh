#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

for name in server dispatcher; do
  file="${LOG_DIR}/${name}.log"
  echo "===== ${file} ====="
  if [[ -f "${file}" ]]; then
    python3 - "${file}" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]:
    print(line)
PY
  else
    echo "尚无日志"
  fi
done
