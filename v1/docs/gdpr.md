# GDPR / Data Retention Policy

This document describes what personal data KubeIntellect stores, how long it is kept, and how to handle data subject requests.

---

## What is stored

KubeIntellect uses LibreChat as its chat frontend. LibreChat persists data in MongoDB (`LibreChat` database). The following collections contain personal data:

| Collection      | Personal data stored                                                              | Retention subject |
|-----------------|-----------------------------------------------------------------------------------|-------------------|
| `users`         | Email, username, hashed password, avatar URL, auth tokens, created/updated dates | No (account data) |
| `sessions`      | Session token, user ID, expiry timestamp                                          | Yes — expired sessions purged automatically by MongoDB TTL index |
| `conversations` | User ID, conversation title, model name, created/updated timestamps               | Yes — purged after `retentionDays` |
| `messages`      | User ID, conversation ID, message text (user prompts + AI responses), timestamps  | Yes — purged after `retentionDays` |
| `files`         | User ID, filename, storage path, MIME type, created timestamp                     | Yes — purged after `retentionDays` |

> **Sensitive content:** The `messages` collection contains the full text of every user prompt and every AI response. This is the highest-risk collection from a GDPR perspective.

---

## Retention policy

Default retention: **365 days** (configurable via Helm `gdprRetention.retentionDays`).

Purge is performed by a Kubernetes CronJob (`gdpr-retention`) that runs daily at 02:00 UTC. It deletes documents from `messages`, `conversations`, and `files` where `createdAt` is older than the retention window.

User accounts (`users`) are **not** automatically deleted. Account deletion must be performed manually or via a future self-service endpoint.

---

## Ingress / access logs

Azure App Gateway and nginx ingress both log request paths and, in some configurations, request bodies. These logs may capture fragments of user prompts.

**Action required per environment:**

- **Azure App Gateway:** Set log retention to ≤ 90 days in the Diagnostic Settings blade (Storage Account → Lifecycle Management policy).
- **nginx ingress:** Configure `logrotate` or a log-shipper retention policy; default is unbounded. Set `log_format` to exclude the request body if full logging is enabled.
- **Loki (Promtail):** Set `retention_period` in the Loki `limits_config` to match the chosen retention window. Default Kind install has no retention limit.

---

## Data subject requests

### Export a user's data

```bash
scripts/ops/export_chat_logs.sh --user <email_or_user_id> --output /tmp/export.json
```

The script exports all `conversations` and `messages` for the given user as a single JSON file suitable for delivery to the data subject.

### Delete a user's data

To delete all chat history for a user (does not delete the account):

```bash
scripts/ops/export_chat_logs.sh --user <email_or_user_id> --delete
```

To also delete the account:

```bash
# Connect to MongoDB and remove the user document
kubectl exec -n kubeintellect deploy/mongodb -- mongosh LibreChat \
  --eval 'db.users.deleteOne({ email: "<email>" })'
```

---

## Configuration

Retention is controlled by the `gdprRetention` block in the Helm values files:

```yaml
gdprRetention:
  enabled: true
  retentionDays: 365          # purge data older than this many days
  schedule: "0 2 * * *"      # cron schedule (default: 02:00 UTC daily)
  mongoUri: "mongodb://mongodb:27017/LibreChat"
```

Set `enabled: false` to disable automatic purging (not recommended for production).
