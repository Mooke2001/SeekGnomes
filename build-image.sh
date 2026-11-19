#!/usr/bin/env bash
# Build only the already prepared, versioned context. Never fetch model keys.
set -euo pipefail
context="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
engine="${SEEKGNOMES_CONTAINER_ENGINE:-}"
if [[ "${1:-}" == --engine ]]; then
    [[ $# == 2 ]] || { echo 'Usage: ./build-image.sh [--engine docker|podman]' >&2; exit 2; }
    engine="$2"
elif [[ $# != 0 ]]; then
    echo 'Usage: ./build-image.sh [--engine docker|podman]' >&2
    exit 2
fi
if [[ -z "$engine" ]]; then
    if command -v docker >/dev/null 2>&1; then engine=docker
    elif command -v podman >/dev/null 2>&1; then engine=podman
    else echo 'Docker or Podman is required to build the image.' >&2; exit 2
    fi
fi
[[ "$engine" == docker || "$engine" == podman ]] || { echo 'Engine must be docker or podman.' >&2; exit 2; }
for entry in Dockerfile payload runtime mathlib-packages science-wheels harbor-wheels harness-assets source-manifest.json build-assets.json; do
    [[ -e "$context/$entry" && ! -L "$context/$entry" ]] || {
        echo "Missing or aliased build asset: $entry. Obtain the complete build context first." >&2
        exit 2
    }
done
command -v "$engine" >/dev/null 2>&1 || { echo "Container engine unavailable: $engine" >&2; exit 2; }
# A build is not a scientific or runtime acceptance test. The resulting image
# must pass doctor and self-test before creating validation-summary.json.
exec "$engine" build --iidfile "$context/build-image-id.txt" --tag seekgnomes:1.3.0 "$context"
