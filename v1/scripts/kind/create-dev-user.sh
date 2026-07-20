#!/bin/bash
# Create (or skip if exists) the dev user in LibreChat's MongoDB.
# Safe to run multiple times — uses upsert with $setOnInsert.
#
# Usage:
#   bash scripts/dev/create-dev-user.sh
#   make kind-dev-create-user

set -euo pipefail

NAMESPACE="${NAMESPACE:-kubeintellect}"
EMAIL="${EMAIL:-admin@kubeintellect.local}"
NAME="${NAME:-Admin}"
USERNAME="${USERNAME:-admin}"
PASSWORD="${PASSWORD:-12345678}"

echo "Waiting for LibreChat pod to be ready..."
kubectl wait --for=condition=ready pod -l app=librechat \
  -n "$NAMESPACE" --timeout=120s

echo "Waiting for MongoDB pod to be ready..."
kubectl wait --for=condition=ready pod -l app=mongodb \
  -n "$NAMESPACE" --timeout=120s

echo "Hashing password..."
HASH=$(kubectl exec -n "$NAMESPACE" deploy/librechat -- \
  node -e "const b = require('bcryptjs'); console.log(b.hashSync('$PASSWORD', 12));")

echo "Creating user $EMAIL in MongoDB..."
kubectl exec -n "$NAMESPACE" deploy/mongodb -- \
  mongosh LibreChat --quiet --eval "
    const result = db.users.updateOne(
      { email: '$EMAIL' },
      {
        \$setOnInsert: {
          name: '$NAME',
          username: '$USERNAME',
          email: '$EMAIL',
          emailVerified: true,
          password: '$HASH',
          avatar: '',
          provider: 'local',
          role: 'ADMIN',
          plugins: [],
          refreshToken: [],
          createdAt: new Date(),
          updatedAt: new Date()
        }
      },
      { upsert: true }
    );
    if (result.upsertedCount === 1) {
      print('User created: $EMAIL');
    } else {
      print('User already exists, skipped: $EMAIL');
    }
  "

echo ""
echo "Done."
echo "  URL:      http://kubeintellect.chat.local"
echo "  Email:    $EMAIL"
echo "  Password: $PASSWORD"
