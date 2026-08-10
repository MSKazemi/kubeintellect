# File Attachments

Embed a file's contents directly in your message using `@`:

```
what is wrong with this deployment? @deployment.yaml
compare these two configs: @old.yaml @new.yaml
why is this pod failing? @pod.yaml @service.yaml
```

The file contents are embedded as a fenced code block and sent to the AI as part of your message. You can attach multiple files per message.

---

## Syntax

```
@filename                  # relative to current directory
@~/path/to/file.json       # home-relative
@/absolute/path/to/file    # absolute path
@"path/with spaces.txt"    # quote paths that contain spaces
```

The `@` reference can appear anywhere in the message:

```
what changed? @before.yaml @after.yaml
@pod.yaml — is this resource request reasonable?
check @deploy.yaml and tell me why the rollout is stuck
```

---

## Supported file types

`yaml`, `json`, `py`, `sh`, `go`, `tf`, `toml`, `js`, `ts`, `rs`, `java`, `xml`, `html`, `md`, `txt`, `log`, and more.

**Limit:** 100 KB per file. Files larger than 100 KB are rejected with an error message.

---

## Example workflow

```
You> what is wrong with this deployment? @deployment.yaml

kube-q> The deployment has resource limits set too low — the container
        requests 128Mi memory but the JVM heap alone requires at least 512Mi...
```

```
You> @pod.yaml @configmap.yaml — why is the config not being picked up?

kube-q> The ConfigMap mount in the pod spec references 'app-config' but the
        ConfigMap is named 'app-configuration' — there's a name mismatch...
```
