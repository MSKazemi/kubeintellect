#!/usr/bin/env bash
# export_chat_logs.sh — GDPR data export / deletion for a single LibreChat user.
#
# Usage:
#   Export:  scripts/ops/export_chat_logs.sh --user <email_or_user_id> --output /tmp/export.json
#   Delete:  scripts/ops/export_chat_logs.sh --user <email_or_user_id> --delete
#
# Options:
#   --user      <email or MongoDB ObjectId>   (required)
#   --output    <file path>                   write export JSON here (default: ./librechat_export_<ts>.json)
#   --delete                                  delete all messages + conversations for the user (no export)
#   --namespace <k8s namespace>               default: kubeintellect
#   --mongo-pod <pod name prefix>             default: mongodb
#   --db        <database name>               default: LibreChat
#
# The script runs mongosh inside the MongoDB pod via kubectl exec.
# No local MongoDB client required.

set -euo pipefail

NAMESPACE="kubeintellect"
MONGO_POD_PREFIX="mongodb"
DB="LibreChat"
USER_ARG=""
OUTPUT=""
DELETE_MODE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)       USER_ARG="$2"; shift 2 ;;
    --output)     OUTPUT="$2"; shift 2 ;;
    --delete)     DELETE_MODE=true; shift ;;
    --namespace)  NAMESPACE="$2"; shift 2 ;;
    --mongo-pod)  MONGO_POD_PREFIX="$2"; shift 2 ;;
    --db)         DB="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$USER_ARG" ]]; then
  echo "Error: --user is required" >&2
  exit 1
fi

# Resolve the MongoDB pod name
MONGO_POD=$(kubectl get pods -n "$NAMESPACE" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[*].metadata.name}' \
  | tr ' ' '\n' | grep "^${MONGO_POD_PREFIX}" | head -1)

if [[ -z "$MONGO_POD" ]]; then
  echo "Error: no running pod matching '${MONGO_POD_PREFIX}' in namespace '${NAMESPACE}'" >&2
  exit 1
fi

echo "Using pod: $MONGO_POD (namespace: $NAMESPACE, db: $DB)"

# Build the mongosh JS that resolves the user (by email or ObjectId)
RESOLVE_USER_JS="
var userQuery = '${USER_ARG}'.match(/^[0-9a-f]{24}$/)
  ? { _id: ObjectId('${USER_ARG}') }
  : { email: '${USER_ARG}' };
var user = db.users.findOne(userQuery);
if (!user) { print('ERROR: user not found'); quit(1); }
var uid = user._id;
"

if $DELETE_MODE; then
  echo "Deleting all chat data for user: ${USER_ARG}"
  kubectl exec -n "$NAMESPACE" "$MONGO_POD" -- mongosh "$DB" --quiet --eval "
${RESOLVE_USER_JS}
var msgs  = db.messages.deleteMany({ user: uid });
var convs = db.conversations.deleteMany({ user: uid });
var files = db.files.deleteMany({ user: uid });
print('Deleted — messages:', msgs.deletedCount,
      'conversations:', convs.deletedCount,
      'files:', files.deletedCount);
"
  echo "Done."
else
  # Export mode
  if [[ -z "$OUTPUT" ]]; then
    OUTPUT="./librechat_export_$(date +%Y%m%d_%H%M%S).json"
  fi

  echo "Exporting data for user: ${USER_ARG} → ${OUTPUT}"
  kubectl exec -n "$NAMESPACE" "$MONGO_POD" -- mongosh "$DB" --quiet --eval "
${RESOLVE_USER_JS}
var conversations = db.conversations.find({ user: uid }).toArray();
var messages      = db.messages.find({ user: uid }).toArray();
var files         = db.files.find({ user: uid }).toArray();
print(JSON.stringify({
  user: { id: uid.toString(), email: user.email, username: user.username },
  exportedAt: new Date().toISOString(),
  conversations: conversations,
  messages: messages,
  files: files
}, null, 2));
" > "$OUTPUT"

  echo "Export complete: $OUTPUT"
  echo "Record count:"
  python3 -c "
import json, sys
d = json.load(open('${OUTPUT}'))
print('  conversations:', len(d['conversations']))
print('  messages:     ', len(d['messages']))
print('  files:        ', len(d['files']))
" 2>/dev/null || true
fi
