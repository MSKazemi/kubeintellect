#!/usr/bin/env bash
set -euo pipefail

FILE="${1:-}"

if [ -z "$FILE" ]; then
    RESTORE_FILE=$(ls -t backups/kubeintellect-pg-*.dump 2>/dev/null | head -1)
    if [ -z "$RESTORE_FILE" ]; then
        echo "Error: No backup files found in backups/"
        echo "Usage: make postgres-restore [FILE=path/to/backup.dump]"
        exit 1
    fi
    echo "Auto-selected most recent backup: $RESTORE_FILE"
else
    RESTORE_FILE="$FILE"
    echo "Using specified file: $RESTORE_FILE"
fi

PGPASSWORD=$(kubectl get secret -n kubeintellect postgres-secret \
    -o jsonpath='{.data.password}' | base64 -d)

kubectl exec -i -n kubeintellect deploy/postgres -- \
    env PGPASSWORD="$PGPASSWORD" \
    pg_restore -U kubeuser -d kubeintellectdb --clean --if-exists < "$RESTORE_FILE"
