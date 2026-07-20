---
tags:
  - reference
---

# Troubleshooting

Solutions to the most common problems. If your issue isn't here, open an issue on [GitHub](https://github.com/MSKazemi/kube_q/issues).

---

## Connection Problems

### `Connection refused` or `Cannot connect to backend`

**Symptom:** kube-q exits at startup with a message like:

```
✗  Cannot reach backend at https://api.kubeintellect.com (Connection refused)
    Entering offline mode — use /url <URL> or /config set url=<URL> to point at a running backend.
```

**Causes and fixes:**

=== "Wrong URL"

    Check your `KUBE_Q_URL` is correct and the port matches what the backend is listening on:

    ```bash
    echo $KUBE_Q_URL
    curl -s http://localhost:8000/health
    ```

=== "Backend not running"

    Start your kube-q backend server. If you're using Docker:

    ```bash
    docker ps | grep kube-q
    ```

=== "Firewall / network"

    Ensure the host and port are reachable from your machine. If the backend is on a remote host, check security group / firewall rules.

=== "Skip the retry loop"

    If you want to fail fast instead of waiting:

    ```bash
    KUBE_Q_SKIP_HEALTH_CHECK=true kq
    ```

---

### `SSL: CERTIFICATE_VERIFY_FAILED`

**Symptom:**

```
httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed
```

**Fix:** Pass your custom CA certificate:

```bash
kq --ca-cert /path/to/ca.pem
```

Or set it in `.env`:

```bash
# ~/.kube-q/.env
KUBE_Q_CA_CERT=/path/to/ca.pem
```

This is common with corporate proxies, self-signed certificates, or internal CAs.

---

### Startup keeps retrying for minutes

By default, kube-q retries the health check for up to 5 minutes, waiting for the backend to come online. To skip this:

```bash
KUBE_Q_SKIP_HEALTH_CHECK=true kq
# or
kq --no-health-check
```

---

## Authentication Errors

### `401 Unauthorized`

**Symptom:**

```
✗  Authentication required. Set KUBE_Q_API_KEY or use --api-key.
```

**Fix:** Provide your API key:

=== "CLI flag"

    ```bash
    kq --api-key your-key-here
    ```

=== "Environment variable"

    ```bash
    export KUBE_Q_API_KEY=your-key-here
    kq
    ```

=== ".env file"

    ```bash
    # ~/.kube-q/.env
    KUBE_Q_API_KEY=your-key-here
    ```

---

### `403 Forbidden`

Your API key is recognized but doesn't have permission to perform the requested action. Contact your kube-q server administrator to check key permissions.

---

## Response & Streaming Issues

### Response cuts off mid-stream

The HTTP connection timed out before the backend finished responding. Increase the timeout:

```bash
KUBE_Q_TIMEOUT=300 kq
```

Or set it permanently:

```bash
# ~/.kube-q/.env
KUBE_Q_TIMEOUT=300
```

---

### No streaming — response appears all at once after a long wait

The server may have disabled SSE, or a proxy is buffering the response. Check:

1. Is there a proxy (nginx, Traefik, AWS ALB) between you and the backend? Ensure it is configured for SSE:
   - nginx: `proxy_buffering off; proxy_cache off;`
   - AWS ALB: use HTTP/1.1 target group
2. Try disabling streaming to confirm the backend is responsive:
   ```bash
   kq --no-stream
   ```

---

### `Event parse error` in the response

kube-q received a malformed SSE event from the backend. Enable debug logging to see the raw bytes:

```bash
kq --debug
```

Check `~/.kube-q/kube-q.log` for the raw HTTP traffic.

---

## REPL & Display Issues

### The terminal looks broken / garbled

kube-q uses Rich for rendering. Make sure your terminal supports:

- **UTF-8 encoding** — check with `echo $LANG` (should end in `.UTF-8`)
- **A modern terminal** — iTerm2, Warp, Windows Terminal, GNOME Terminal, or any xterm-compatible emulator

If you need plain text output (e.g. inside a script or on a minimal tty):

```bash
kq --output plain
```

---

### Tab completion doesn't work

Tab completion for slash commands is provided by `prompt_toolkit`. It only works in interactive REPL mode, not in `--query` mode.

Ensure you are running a real interactive terminal, not inside a pipe or subshell. If you launched `kq` via `$(...)` or `echo | kq`, completion will be disabled.

---

### `/search` returns no results even though I had sessions

The FTS5 search index was introduced in schema v3. If your `history.db` is from an older version, run kube-q once to trigger the automatic schema migration:

```bash
kq --no-health-check
```

Then try `/search` again. The migration backfills the FTS index from existing messages.

---

### Session history is gone

kube-q stores history in `~/.kube-q/history.db`. Check:

```bash
ls -lh ~/.kube-q/history.db
```

If the file was deleted or the path changed (e.g., you changed `HOME`), history won't be available.

!!! warning "History is local only"
    History is never uploaded to the server. There is no cloud backup. If `history.db` is deleted, those sessions are gone.

---

## File Attachments

### `File not found: @myfile.yaml`

- The path is resolved relative to your **current working directory** at the time you run `kq`. If the file is elsewhere, use an absolute path: `@/home/user/configs/myfile.yaml`.
- Paths with spaces must be quoted: `@"path/with spaces.yaml"`.

### `File too large` error

Files over **100 KB** are rejected. Split large files or trim them to the relevant section before attaching.

---

## Debug Logging

For any hard-to-diagnose problem, run kube-q in debug mode:

```bash
kq --debug
```

This logs all raw HTTP requests and responses to:

- **stderr** (visible in your terminal)
- **`~/.kube-q/kube-q.log`** (rotating, 5 MB × 3 files)

The log shows the full SSE event stream, headers, and retry attempts — useful for diagnosing proxy issues, TLS problems, or unexpected server responses.

---

## Still stuck?

1. Check the [FAQ](faq.md) for common questions
2. Search [GitHub Issues](https://github.com/MSKazemi/kube_q/issues) — your problem may already be reported
3. Open a new issue with:
   - kube-q version (`kq --version`)
   - The exact error message
   - The output of `kq --debug` with sensitive values redacted
