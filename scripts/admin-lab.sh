#!/bin/bash
set -euo pipefail
# No operator installation, node image pulls, or cluster-wide configuration edits.
exec python3 "$(dirname "$0")/admin_lab.py" "$@"
