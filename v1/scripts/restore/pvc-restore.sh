#!/usr/bin/env bash
set -euo pipefail

PVC="${1:-}"
FILE="${2:-}"

if [ -z "$PVC" ] || [ -z "$FILE" ]; then
    echo "Usage: make pvc-restore PVC=<pvc-name> FILE=<backup-file.tar.gz>"
    echo "Example: make pvc-restore PVC=kubeintellect-runtime-tools-pvc FILE=backups/kubeintellect-runtime-tools-pvc-2026-01-19-161017.tar.gz"
    exit 1
fi

bash backups/restore-pvc.sh "$PVC" "$FILE"
