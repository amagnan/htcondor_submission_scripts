#!/bin/bash

# Checker-driven production: ./gangaSubmit.sh prodWorkflow/disgen.yaml --test --submit
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$script_dir/prodWorkflow/gangaSubmit.sh" "$@"
