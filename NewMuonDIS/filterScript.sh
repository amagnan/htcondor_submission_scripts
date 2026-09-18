#!/bin/bash
# Preview: ./filterScript.sh /path/to/inputs -- -g /path/to/geometry.root
# Submit:  ./filterScript.sh /path/to/inputs --submit -- -g /path/to/geometry.root
# Test:    ./filterScript.sh /path/to/inputs --submit --test -- -g /path/to/geometry.root
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/filterScript.py" "$@"
