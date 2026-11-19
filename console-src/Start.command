#!/usr/bin/env bash
set -euo pipefail
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -f "$package_dir/server.py" && -f "$package_dir/../source-manifest.json" ]]; then
  console_dir="$package_dir"
  package_dir="$(cd -- "$console_dir/.." && pwd -P)"
else
  console_dir="$package_dir/console"
fi
results_dir="${SEEKGNOMES_RESULTS_DIR:-$package_dir/results}"
if [[ -z "${SEEKGNOMES_RESULTS_DIR:-}" && -n "${WSL_DISTRO_NAME:-}" && "$package_dir" == /mnt/* ]]; then
  results_dir="${XDG_DATA_HOME:-$HOME/.local/share}/seekgnomes/results"
fi
exec python3 -B "$console_dir/server.py" --package-dir "$package_dir" --console-dir "$console_dir" --results-dir "$results_dir" "$@"
