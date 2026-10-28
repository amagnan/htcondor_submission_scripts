#!/bin/bash

# Production: ./gangaSubmit.sh production/disgen.yaml --test --submit
# Legacy:     ./gangaSubmit.sh --test --submit
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
case "${1-}" in
    *.yaml|*.yml) script=gangaProdScript.py ;;
    *) script=gangaScript.py ;;
esac
exec /cvmfs/ganga.cern.ch/Ganga/install/ship/bin/ganga "$script_dir/$script" "$@"
