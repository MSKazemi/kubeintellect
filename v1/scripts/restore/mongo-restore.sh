#!/usr/bin/env bash
set -euo pipefail

FILE="${1:-}"

if [ -n "$FILE" ]; then
    RESTORE_FILE="$FILE"
    echo "Using specified file: $RESTORE_FILE"
else
    RESTORE_FILE=$(ls -t backups/kubeintellect-chats-*.gz 2>/dev/null | head -1)
    if [ -z "$RESTORE_FILE" ]; then
        echo "Error: No backup files found in backups/ directory"
        echo "Usage: make mongo-restore [FILE=path/to/backup.gz]"
        exit 1
    fi
    echo "Automatically selected most recent backup: $RESTORE_FILE"
fi

echo "Restoring from: $RESTORE_FILE"
kubectl exec -i -n kubeintellect deploy/mongodb -- \
    mongorestore --host mongodb.kubeintellect.svc.cluster.local --port 27017 \
    --db LibreChat --drop --archive --gzip < "$RESTORE_FILE"
