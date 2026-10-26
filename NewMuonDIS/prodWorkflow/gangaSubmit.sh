#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for argument in "$@"; do
    case "$argument" in
        -h|--help) exec python3 "$script_dir/workflow.py" "$@" ;;
    esac
done
exec /cvmfs/ganga.cern.ch/Ganga/install/ship/bin/ganga "$script_dir/workflow.py" "$@"
