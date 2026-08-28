# Changelog

All notable changes to this repository are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

> **Scope.** This is the **repo-wide** changelog. It tracks shared infrastructure and the
> **active development line** — currently the **v4** platform and the **v5** design tier
> (whose slices ship as default-off flags inside the v4 server, ADR-101). The frozen
> generations keep their own history: **v3** has `v3/CHANGELOG.md`; **v1** and **v2** are
> versioned by their git tags (`v1.0`, `v2.0.x`). See the root `README.md` for the v1→v5 lineage.

## [Unreleased]

### Fixed

- **The quickstart's first step could not be followed** (`v4/docs/quickstart.md`,
  `v4/docs/install/no-cluster.md`, `v4/tests/test_the_quickstart_can_actually_be_followed.py`).
  Step 1 of the "first answer in ~2 minutes" path told the reader to open
  `kubeintellect.com/demo`, enter their email, and copy a personal `ki-ro-…` key that "appears
  instantly on the page and is emailed to you" and "expires after 30 days". Walked on 2026-08-28:
  that page is the browser terminal demo — "No sign-up required", no email field, no key. The mint
  route it describes (`POST /v1/auth/demo-keys`) exists in this repository and is **absent from the
  deployed API**, whose OpenAPI document lists five paths in total. A reader following the
  quickstart stops at step 1 with nothing to paste.

  What does work was measured rather than assumed: `pip install kube-q`, Python 3.12+, and `kq`'s
  default backend `https://api.kubeintellect.com`, which answers `/v1/healthz` in ~0.4 s. Both
  pages now describe that path, and `KUBE_Q_API_KEY` is described where it actually applies:
  pointing `kq` at your own server.

- **…and the first fix for it removed the credential that made it work.** The correction above
  originally replaced step 1 with "the demo needs no key". That was false. Measured on 2026-08-29
  by installing the published `kube-q` wheel from PyPI into a clean virtualenv: a well-formed
  `POST /v1/chat/completions` with no credentials returns **401**
  `{"detail":"Authorization: Bearer <api_key> required"}`. The earlier probe that read as
  "auth is open" sent an empty body and received a 422 — FastAPI validates the request body
  *before* the auth dependency runs, so a schema rejection had been mistaken for an
  authorization result. A 422 on a malformed body says nothing about whether an endpoint is
  authenticated.

  The working credential was already published: `ki-ro-dev`, the shared read-only demo key named
  in `README.md`, `v4/README.md` and the Hugging Face Space's own README. With it, the freshly
  installed wheel answered "how many pods are running in the cluster?" in **2.5 s**. Both pages
  now hand the reader that key, state plainly that it is shared, rate-limited and read-only, and
  keep `KUBE_Q_API_KEY` for your own server. Four tests were added, because every one of the
  previous seven pinned the *absence* of a wrong instruction and none pinned the *presence* of a
  working one — all seven stayed green across the regression. Three of the four fail against the
  state that shipped.

  Seven tests pin what the doc-claims gate cannot, because it compares numbers: that the
  quickstart's CLI table and `app/cli.py` name the same commands in both directions — it was four
  short (`chain-export`, `chain-verify-export`, `chain-truncate`, `provenance`), now added — and
  that no document sends a reader to that page for a key or promises a demo-key lifetime.

- **Nothing could run the test suite the way `main` runs it** (`scripts/check-public-checkout.sh`,
  `Makefile`, `v4/tests/test_a_test_can_be_green_only_where_the_private_tier_exists.py`). Every
  local gate runs against the working tree, which carries the private research materials the root
  `.gitignore` names. A test that reads one of those paths is green here and red on `main` — which
  has happened twice, most recently in `74ac4cf`. The proof was a manual hide-the-paths-and-restore
  dance, so in practice it was run after the breakage, not before.

  `make check-public-checkout` exports `HEAD` into a throwaway directory and runs `make setup`
  there. The non-obvious requirement, and the reason the script asserts it before running anything:
  the export must be a real git checkout. Twelve tests in the server suite decide what to scan
  from `git ls-files`, and a plain `tar -x` of `git archive` has no index at all — measured, the
  suite then reports **twelve failures that do not exist on `main`**. The script gives the export an index
  and commits it — `actions/checkout` gives CI a real `HEAD`, so an export
  without one is *less* faithful than the clone it stands in for, and the first version of this
  script proved that by failing its own test inside its own export. It then refuses to run if the
  export differs from `HEAD` by even one path, which is how the one tracked file the committed
  `.gitignore` also matches was found.

  Measured on the current `HEAD` (`f09b8f7`): the public checkout is **green** — all nine gates,
  5403 passed / 16 skipped in the server suite, 749 in the kq suite, in about four minutes. So
  there is no third occurrence outstanding. The 16 skips against 9 locally are the honest
  difference: six assertions are guarded on a private-tier file being present and simply do not
  run in CI.

  Then the instrument failed its own test on `main`, in exactly the class it is named for. Its
  export commits as the source repository's own identity, so the guard hook passes on it rather
  than being bypassed — and `git var GIT_AUTHOR_IDENT` resolves on any developer machine (git
  falls back to the passwd GECOS name) but exits 128 on a runner, which has none. Green here, red
  there. Two things were needed and both are pinned by a test that reproduces a runner exactly, by
  emptying `GIT_AUTHOR_NAME` rather than merely scrubbing config: a synthetic author when git has
  no identity to offer, and passing that author as *environment* rather than `-c user.name`, since
  an empty `GIT_AUTHOR_NAME` in the caller's environment overrides any config the script sets.

  Ten tests pin the instrument itself, including the index-fidelity property and — derived from
  the private index at runtime rather than enumerated in a published file — that no private-tier
  path reaches the export. Also corrected: `make help` said `setup` ran "all eight CI gates"; it
  runs nine.

- **The ninth event the server sends was the one its wire module never declared**
  (`v4/packages/ki-protocol/ki_protocol/wire.py`, `ki_protocol/events.py`,
  `app/api/v1/endpoints/chat_completions.py`). `ki_protocol/__init__.py` calls `wire` "canonical
  for what the server sends". It was not: `chat_completions.py` emitted the usage frame as a
  hand-written `{"type": "usage", **meter.as_dict()}` dict, validated by nothing.

  There **is** a guard for this seam — `packages/kube-q/tests/core/test_the_wire_has_two_halves.py`,
  written after the 2026-08-20 audit that found five of eight emission models arriving stripped.
  Re-measured 2026-08-28, that fix holds: all eight round-trip losslessly. But the guard is
  *generative over `ki_protocol.wire`* — it builds a sample per declared model — so an event that
  never enters `wire.py` has no sample to generate from and is invisible to it. Being outside the
  wire module is exactly what exempted `usage` from the check designed to catch this, and both
  halves then drifted unobserved: the server sent `llm_calls` and the client had no field for it,
  so it was dropped on arrival, while the client declared a `model` the server has never sent,
  which read `""` for every caller. `llm_calls` is the count `core/usage.py` keeps deliberately,
  so that "called 40 times, reported no tokens" stays distinguishable from a genuinely cheap
  request — that distinction was being discarded at the wire.

  `wire.UsageEvent` now declares the frame, the endpoint emits through it, and `UsageData` carries
  `llm_calls`. Adding the model to `wire.py` made the existing generative guard cover `usage`
  automatically — it failed on the next run until a sample was added, which is the mechanism
  working. Four tests were added for the direction that guard structurally cannot see: a type the
  *client* declares that no server model emits. `model` is left empty and documented rather than
  filled — a request can span the coordinator and subagent tiers, so there is no single honest
  value, and inventing one would make the field lie instead of merely stay blank.

  Also: `packages/kube-q/scripts/check-event-parity.py` calls itself a CI guard and has had no
  target since the web app became a PTY terminal wrapper — `web/lib/` does not exist, nothing
  under `web/` parses `ki_event`, and no workflow or Makefile invokes it, so it died without ever
  failing and produced a `FileNotFoundError` traceback when run by hand. It now reports that
  plainly and points at the test that does run.

### Added

- **The restore proof is now tested against a real PostgreSQL**
  (`v4/tests/test_the_restore_proof_holds_against_a_real_database.py`). `backup.verify` is
  driver-agnostic and its unit tests drive it with a dict — and both A12 defects fixed this cycle
  lived precisely in what a dict cannot model: an exit code, and a poisoned transaction. The chain
  subsystem next door has had a real-database test since it shipped, and a live adversarial
  rehearsal of ten cases against it (tampered archive, wrong scope, progressive pruning,
  undeclared deletion, deletion one row beyond a legitimate declaration, on both `decision_log`
  and `memory_audit`) found nothing wrong. The backup path was the one that never adopted the
  harness. These tests drive the real psycopg adapter against a real server; the missing-table
  case fails without the autocommit fix. Skips without docker, like the eight files that already
  use this pattern.

- **The narrated demo's sources are tracked, and its claims are now checked** (`scripts/demo/
  video/`, `v4/tests/test_the_video_says_only_what_the_product_does.py`). The scene spec called
  itself the single source of truth and said every claim in it was checked against a file in
  this repository — true of the terminal scenes, which name their transcript and line numbers,
  and untrue of the cards, where the product claims live. The audit found the video saying
  **"read-only by default"** twice, which is false: with no keys configured the server treats
  every unauthenticated caller as `admin` (`REQUIRE_AUTH` is off), and what is actually true is
  that it reads before it writes and stops every mutating command at a gate. Also corrected: a
  role claim naming three of the four roles, and four card citations resolving one directory
  too high. The palette and mark were a second kind of unchecked claim — an accent belonging to
  no stylesheet or brand asset, and a placeholder logo drawn as a hexagon with a `K` in it. Both
  now come from the shipped mark and the site's own tokens, each recorded beside the file it
  came from. `scripts/check-text-encoding.py` gained `wave` to its non-text `open()` owners: the
  gate was asking an audio decoder for an encoding parameter it does not have.

- **A hash chain can now be pruned without the prune looking like an attack** (`kubeintellect
  chain-truncate`, `app/db/chain_truncation.py`, schema `chain_truncation`, **schema version
  2**). The export half above stopped at a written refusal whose last item was a change to the
  verifier: `verify_chain` starts at `seq 0` with an empty `prev_hash`, so a legitimately
  truncated chain failed it — shipping the DELETE first would have turned every pruned
  install's housekeeping into a permanent tamper alarm. So a truncation is now *declared*: one
  transaction writes a `chain_truncation` row — scope, `through_seq`, the seq the chain resumes
  at, the hash it resumes from, and the verified archive's hash — and only then deletes.
  Interrupted between the two you get a declared gap that does not exist yet, which verifies
  fine; the opposite order does not. Both verifiers (`flight_recorder.verify_episode`,
  `security.verify_memory_chain`) consult it, and only when a chain no longer starts at `seq
  0` — a whole chain takes the old path exactly, so a missing or unreadable table cannot soften
  an existing verdict. Without a matching record a short chain is still **TAMPERED**; a record
  that contradicts the surviving rows is TAMPERED too; a record that cannot be read is
  `unverified`, never `intact`. `chain-truncate` refuses, before writing anything, on a stale
  archive, a chain that changed since the export, a truncation leaving nothing, or a surviving
  chain that does not link to the archive's last hash — that seam is why a forged record cannot
  launder an edit, only a deletion an operator could have made legitimately anyway. Proved
  against a real Postgres, across both drivers (the CLI truncates through psycopg, the server
  verifies through asyncpg). Retention still refuses to prune either ledger on a schedule: this
  is a manual, per-chain act with an archive in hand. **Operators must re-run `kubeintellect
  db-init`**; until then the command fails cleanly and `kq v5-status` reports a stale schema.

- **A hash chain can now be archived in a form that still verifies** (`kubeintellect
  chain-export`, `kubeintellect chain-verify-export`, `app/db/chain_export.py`). Retention
  refuses to prune `decision_log` and `memory_audit` — correctly, since deleting their newest
  rows breaks no link and would make an install's own housekeeping indistinguishable from an
  attack — and its written reason ended *"needs a signed export-then-truncate flow"*. No such
  flow existed, so the refusal was permanent and the two fastest-growing tables in the schema
  were the two nothing could ever bound. This is the export half, useful on its own: `pg_dump`
  gives you a copy of a chain, and a copy of tamper-evidence that cannot itself be checked is
  not evidence. An archive carries the rows verbatim, the head anchor as it stood, the link
  verdict computed at export time and a SHA-256 over all of it, so it can be checked years
  later with no database present. Segment-aware: an archive need not start at `seq 0`, which is
  why it does not reuse the whole-chain verifier. Exporting a broken chain is allowed and says
  so — the archive is how you keep the evidence of a break. Nothing deletes anything:
  `TRUNCATION_PREREQUISITES` lists what a truncation must record first, and the last item is a
  change to the *verifier*, not the data. Documented limit, carried inside every archive: a
  content hash proves the file has not been edited, not who wrote it.

- **The memory tamper verdict now reaches a human, and is proved against a real database**
  (`kq v5-status` `memory_chain` row). The verifier shipped earlier the same day put its verdict
  under `memory.chain` on `/healthz` and `/v1/v5/status` and stopped there — `/healthz` is a
  kubelet probe endpoint, and an operator asking whether this product's memory can be trusted
  types `kq v5-status`, which said nothing. All five states are rendered, including the two that
  are not faults: a row that appeared only on bad news cannot be used to confirm anything, and
  its absence would be indistinguishable from an older server. `off` is not styled as healthy
  and `unverified` is not styled as a tamper. The age is always shown, with a red `STALE` marker
  when the last verdict is older than the interval allows — the server reports the last
  *recorded* verdict, so `intact` from a verifier that died reads as reassurance otherwise.
  Separately, every test of the verifier until now drove a fake pool that accepted any SQL and
  returned what the test handed it — the same hole that hid a hybrid-recall channel returning
  zero rows on 225 of 225 real queries. Twelve new tests run the real append path against a real
  Postgres and then tamper with it in SQL: an edited payload, an interior delete and a reorder
  break a link; deleting the two newest rows breaks **no** link (asserted directly) and is
  caught only by the head anchor; a renamed table reads as `unverified`, never as tampered. One
  test pins the documented limit rather than hiding it — forging the head *as well as* the rows
  does restore an `intact` verdict, which is what tamper-evidence rather than prevention means.

- **The memory tamper detector is now actually run** (`MEMORY_CHAIN_VERIFY_INTERVAL_S`,
  `memory.chain` on `GET /healthz`). `verify_memory_chain` — the hash-chain check that detects a
  silent edit, a reorder or a truncation of the memory audit log — was written, tested in four
  test modules, specified in ADR-018 and documented in `docs/security.md`, and **nothing in a
  running server ever called it**: the only callers were the test suite and an offline probe
  script. A hash chain accuses nobody on its own, so a verifier nobody asks detects nothing, and
  the difference between that and having no tamper-evidence is documentation. The server now
  verifies once at startup and then on an interval (default 900s), records the verdict, and
  reports it. `chain.state` is deliberately five-valued, not a boolean: `off` (hardening
  disabled, so nothing writes the chain), `never-checked`, `unverified` (a check ran and could
  not conclude — an unreachable database), `intact`, `TAMPERED`. Only `TAMPERED` sets
  `memory.healthy` false; `unverified` does not, because a detector that cries tamper when its
  own database is down teaches operators to ignore it. A verdict older than 2.5 intervals is
  reported `stale`, so a verifier that stopped does not look like one that keeps agreeing with
  itself. The check is periodic rather than on-demand because verifying reads every audit row
  for the cluster while `/healthz` is probed every few seconds. Off by default, with
  `MEMORY_SECURITY_HARDENING`.

- **The required-checks list is now recorded next to the workflow that produces it**
  (`.github/required-checks.yml`, `make check-required`). `ci.yml` produces **15** named checks
  and `main` requires **9**, and nothing joined the two lists — a job could be added, renamed or
  left out of the required set and every PR would keep merging green, which is the one thing a
  required-check list exists to prevent. The record names all 15: the nine required, and six
  that are not, each with a dated reason. Three of those six are marked as what they actually
  are — **gaps nobody chose**, not policy. The sharpest: **`Container image (build + serve)` is
  not a required check**, and it is the only check that proves the published image starts at all
  against a real Postgres, so a PR that breaks the image merges green today. The two frozen-suite
  checks (v2, v3) and the web build are unrequired for no recorded reason either. The comparator
  runs offline (CI and the record must partition each other exactly) and, with an authenticated
  `gh`, against the live setting in both directions — a context required in the settings but no
  longer produced by CI leaves every PR pending forever, which is as broken as the reverse. It is
  deliberately not a CI job: a workflow reading its own branch protection would be a repo-scoped
  credential added to produce a report. The record also pins `strict: false` (a PR need not be up
  to date with `main`, which is how two individually green PRs once turned `main` red on their
  sum) and `enforce_admins: false`, so neither is mistaken for something it is not.

- **The contributor gate is now the same gate CI runs, and what only CI can see is written
  down.** `scripts/dev-setup.sh` printed *"Gate 1/8 — ruff check (this IS the CI lint gate)"*
  while running `ruff check` over two packages; CI had been linting `packages/kube-q/`, `tests/`
  and `scripts/` since 2026-08-24. A contributor could pass `make setup` on a branch CI would
  reject for a lint error in the very test file they had just added — a setup script that
  converts a real failure into a surprise is worse than none. The scope now matches CI exactly.
  Separately, `v4/scripts/check_doc_claims.py` — which recollects every documented count (both
  suite totals, playbooks, detectors, providers, v5 flags, CLI exit codes) — **ran in no CI job
  at all**, so numbers that one new playbook moves six of were enforced only by memory. It now
  runs in CI, riding inside the existing **Lint (ruff)** job rather than a new one, because
  branch protection matches required checks by name and a new name would leave every open PR
  unmergeable until the settings caught up (#167). `make setup` runs nine gates, and its summary
  now states the honest arithmetic: CI is 10 jobs expanding to **15 named checks**, a laptop
  reproduces **six**, and the other nine are listed individually with the reason each is CI-only
  — including that the two py3.14 checks are `continue-on-error` and cannot block a merge. A new
  suite fails if any of that drifts: same ruff scope in both places, the doc-claims step present,
  every CI check either mapped to a local command or carrying a dated reason, and the gate count
  identical in `dev-setup.sh`, `AGENTS.md` and the `Makefile`.

- **One tag now ships a complete release, in an order that cannot half-publish it.** The tag
  fan-out was five workflows that never speak to each other, and `release-binaries.yml` ended in
  `gh release upload <tag>` — which requires a release that already exists, and **nothing in CI
  created one.** The written procedure was to push the tag and then run `gh release create` by
  hand fast enough to beat the build to its upload step: lose that race and the binaries job
  fails *after* the image, the chart and PyPI have already published, leaving a version that
  exists everywhere except the page people download from; win it, and the release is published
  while the archives are still building, which fires `release: released` at `krew.yml` before its
  four assets exist — so krew failed on **every** release by construction and was repaired by
  hand afterwards. The release job now **creates the release itself, as a draft**, attests and
  attaches the archives, and **publishes last**, so the downstream event fires on a release that
  is already complete and krew succeeds on its first run. A release created by hand is left
  alone — it may be a deliberate draft or prerelease, which is not this job's decision. A new
  suite gates the ordering, refuses any `gh release upload` in a job that does not first ensure
  the release exists, and requires every distribution channel to be either produced by the tag or
  **written down as manual with a dated reason** — Snap (needs an explicit dispatch and a store
  credential that can expire; a tag trigger would add a channel that silently no-ops), the
  Homebrew tap (a separate repository, no cross-repo token) and the demo Space (not an install
  path). That channel list is checked against the supply-chain module's, because two independent
  lists of where a project publishes is exactly how one of them goes quiet unnoticed.

- **The sensorium can be scoped to fit a large cluster — and says so when it is** (enterprise A5,
  **ADR-020**). Perception runs two `kubectl get … -A --watch` subprocesses, and the structural
  problem is not throughput but the relist: a watch that drops is replaced by one that emits the
  *entire* current state before it emits any change, and that recurs on every disconnect. The
  bounded queue added earlier converts what would be unbounded memory growth into a counted loss;
  it does not make the firehose smaller. ADR-020 therefore keeps the transport and makes **scope**
  the supported lever: new `SENSORIUM_WATCH_NAMESPACES` (comma-separated, empty = `-A`) starts one
  named stream per resource per namespace, so `stream_health()` reports *which* scope failed.
  Scoping is the dangerous kind of fix — it works by creating a blind spot — so it is **disclosed
  rather than silent**: `perception_state()` carries the scope and `perception_gaps()` reports it,
  in the one classifier `GET /v1/findings`, `kq findings` and the morning digest all read, because
  an empty findings list from a scoped sensorium is not a statement about the cluster and the
  person reading it is usually not the person who set the flag. The observation queue also warns
  once at 80% of its depth, naming both levers, while nothing has been shed yet — until now the
  first signal an operator got was `shed_total`, which only ever speaks *after* perception has
  already been lost. **Not green:** the ceiling itself is unchanged, no supported cluster size is
  claimed because none has ever been measured, and the real answer — a shared informer with
  server-side field selection — is deferred to v5 with a benchmark attached rather than swapped
  in blind under every detector, the staleness filter and the RBAC story at once.

- **Signed provenance on every released artifact** (enterprise A13). Nothing this project
  published could be checked by whoever installed it. The `kq` release job wrote a
  `checksums.txt` and uploaded it to the **same release page** as the tarballs it checksums —
  which detects a corrupted download and nothing else, since anyone able to replace a tarball can
  replace the checksums beside it in the same breath. Each publishing workflow now emits a keyless
  sigstore **build attestation**, minted under GitHub's OIDC issuer and recorded in a public
  transparency log, binding the artifact's **digest** (never a tag — a tag is a mutable pointer)
  to the commit, workflow and run that produced it: the container image plus an SPDX **SBOM
  generated from the built image** rather than from the lockfile, the OCI Helm chart, the frozen
  `kq` binaries, and PEP 740 attestations on the PyPI wheels. New `app/core/supply_chain.py` and
  `kubeintellect provenance` print the exact verification commands and the signer identity each
  must pin to — generated from the same constants the workflows are named by, because a
  verification command is worth only as much as the identity it names, that identity is a *file
  path*, and dropping `--signer-workflow` still "verifies" while accepting an attestation from
  **any** workflow in the repository. 33 tests assert that every workflow firing on a `v*` tag
  either attests or appears in `UNATTESTED_WORKFLOWS` with a dated reason (a new distribution
  channel cannot inherit silence), that the attesting jobs hold the `id-token`/`attestations:
  write` permissions their step needs — otherwise a tag build discovers that at its last step,
  after publishing — that the chart job refuses to attest when `helm push` prints no digest, and
  that every action in every workflow is pinned to a commit SHA, since a floating tag on the
  action that mints your provenance makes the provenance only as good as whoever can move it.
  Snap, the Homebrew tap, the krew index and the Hugging Face Space are recorded as deliberately
  un-attested, each with a dated reason. Documented in `docs/security.md` § 8. **Not green:** no
  release has been signed yet — these steps fire on the next `v*` tag, so every artifact published
  to date carries nothing; attestation is not a reproducible build; and there is no
  dependency-level provenance.

- **A backup that can be proved to have restored** (enterprise A12). `docs/operations.md` already
  carried the right `pg_dump` / `psql` commands and the right `ON_ERROR_STOP=1` warning; what no
  operator could answer afterwards was *did everything come back*. For most tables a wrong answer
  is lost data — for `decision_log` and `memory_audit` it is worse. They are hash chains, and a
  restore that drops their **newest** rows breaks no link: the surviving rows hash correctly, chain
  verification returns intact, and the postmortem prints its intact-chain banner over a record that
  is quietly short. `decision_log_head` and `memory_chain_head` exist to catch that, and nothing
  compared them. New `app/db/backup.py` and two commands: `kubeintellect backup-manifest` records
  the schema version and DDL fingerprint, exact row counts for every table whose loss is a
  data-loss event, and how far each hash chain got; `kubeintellect verify-restore` re-measures a
  restored database against it and **exits 1** if anything is missing, so it can be wired into a
  rehearsal. It reports every discrepancy rather than the first — mid-incident, a list of three
  things to fix beats one error and a re-run — and a short table, a table the restore never
  created, and a truncated chain are three different messages. A manifest taken from an already
  damaged source is recorded as such rather than adopted as the definition of correct. Both
  functions are read-only and driver-agnostic. `docs/operations.md` now states RPO and RTO for the
  reference deployment, and states plainly what is not automated: no scheduled backup in the Helm
  chart, no off-site copy, no automated rehearsal.

- **The database now says which schema it has, and drift is loud** (enterprise A11). `schema.sql`
  is idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) and applied by hand, and
  nothing recorded that it had ever been run — so "upgraded the image and forgot `db-init`",
  "rolled the Deployment back while the database kept the newer schema" and "correct" were
  indistinguishable. The first two do not fail loudly: every memory, recorder and audit write is
  fire-and-forget by design, so a missing column is a logged warning inside a swallowed exception —
  memory quietly stops recording while `/healthz` keeps reporting `enabled: true`. New
  `schema_migrations` ledger plus `app/db/schema_version.py`: `db-init` stamps the version, its
  fingerprint and the time, and `schema.sql` stamps the version itself as well, because the Helm
  chart's db-init Job pipes the file into psql and never runs the Python CLI. The server reads the
  ledger once when the memory pool opens and reports `db_schema` on `GET /healthz` as `current`,
  `stale`, `ahead` (a rollback — the direction re-running the DDL cannot fix), `unrecorded`, or
  `unknown` when the check itself could not run, which is never reported as a verdict. A non-current
  schema deliberately does not move the top-level `status`: failing liveness would turn a fixable
  database into a restart loop. A **pinned DDL fingerprint** fails the test suite if `schema.sql`
  changes without a version bump, so the version is enforced rather than remembered. Not a
  migration tool: there are still no ordered revisions, no down-migrations, and idempotent DDL
  cannot express a rename, a type change or a backfill — that gap is stated in the module and
  dated.

- **Every route can now say no** (enterprise A16). There was no rate limiter anywhere: one
  caller — a retry loop, a misconfigured CI job, a stolen readonly key — could issue unbounded
  `POST /v1/chat/completions`, and each of those is an LLM call against the operator's spend and a
  `kubectl` fan-out against their API server. The spend guard in `autonomy/budget.py` bounds what
  the *watchtower* does on its own; nothing bounded what a client asked for. New
  `app/api/rate_limit.py`: a token bucket per caller, on by default at `RATE_LIMIT_PER_MIN` 120 /
  `RATE_LIMIT_BURST` 30, answering `429` with a `Retry-After` that is the real time until one
  request will succeed. Keyed by a SHA-256 of the bearer token, never the key itself and never an
  IP — behind an Ingress every request shares one address, so an address-keyed limiter throttles
  the whole tenancy when one client misbehaves. `/healthz`, `/readyz`, `/v1/healthz`, `/v1/readyz`
  and `/metrics` are never limited: a 429 to the kubelet's probe would restart the pod under
  exactly the load the limiter exists to survive, and a throttled scrape target silently becomes
  an unmonitored one. Mounted inside `CORSMiddleware` so a browser client can read the 429 instead
  of an opaque network error, and inside `RequestLoggingMiddleware` so a rejection appears in the
  access log. The bucket table is capped (`RATE_LIMIT_MAX_TRACKED`) with least-recently-seen
  eviction, because the limiter runs before route authentication and arbitrary bearer tokens can
  otherwise turn it into a memory-exhaustion vector. Two limits are documented rather than tuned
  away: the counters are **per replica**, so N replicas admit up to N× the limit, and this is fair
  use rather than DDoS defence — volumetric abuse belongs at the ingress.

- **Data retention, and a written refusal for the tables that must not have it** (enterprise
  A10). Nothing in the tree deleted a row on a clock: twenty append-only tables, `request_log`
  growing by one row per chat completion, no prune and no setting to ask for one. New
  `app/memory/retention.py` runs inside the consolidation loop under `MEMORY_RETENTION_DAYS`
  (default `0` = keep everything — a data-deleting default would discard history on upgrade),
  ageing out `request_log`, `session_notes`, `fleet_signals`, terminal `prospective_memory` rows
  and `promotion_outcomes`, at most 5 000 rows per table per pass so it never holds a lock.
  What it refuses is the point: `decision_log` and `memory_audit` are hash-chained
  tamper-evidence whose own schema comments record that deleting the newest rows breaks no link
  and is therefore invisible to chain verification — a prune there would ship a tamper-evidence
  bypass as housekeeping and leave the head anchors contradicting the chain for ever. Those two,
  their head anchors and `episodes` (what the agent recalls; deliberate deletion already has an
  RTBF path) each carry a dated reason in `retention.REFUSED`, asserted by the test suite so a
  later rule cannot quietly start deleting one. `promotion_outcomes` has a hard floor at the
  ADR-102 90-day window: a shorter retention setting is clamped up rather than honoured, because
  pruning inside that window would delete the failures the A3 statistical brake demotes on and
  so would quietly widen what the watchtower may do unattended.

- **The fourth brake on the autonomous-write path is now visible to an operator, and the module
  that would deliver "earned autonomy" stopped claiming it does.** `/v1/v5/status` promises "which
  v5 slices are active, and whether the fail-closed brakes are engaged"; it listed three (kill
  switch, change freeze, spend cap) and could not show the ADR-102 revocation gate at all. Two
  quiet failures followed. First, `KI_V5_STATISTICAL_PROMOTION` appeared under `active_flags` with
  nothing anywhere saying which direction it acts in — a switch named *statistical promotion*
  reported as active reads as *rungs are being earned here*, which is the one thing this build does
  not do. Second, `degraded_experimental_flags()` — the function that exists precisely to catch
  "you set it and nothing happened, because the subsystem is dead" — filtered on the `MEMORY_`
  prefix, and **both** halves of this flag read through the memory hierarchy's pool; flag on and
  hierarchy down, the surface said active while the A3 gate was governed by the allowlist alone.
  `/v1/v5/status` now carries an `autonomy_promotion` block and `kq v5-status` renders it (the
  CLI-parity guard in `test_v5_flag_wiring.py` caught the omission). `enabled` and `operating` are
  deliberately separate fields: they differ exactly when the flag is on and there is no store to
  read. A store that exists and cannot be read reports `operating: false` with the error — never a
  clean record. `samples` is the count inside the ADR-102 rolling window, not the table size, since
  every threshold in `promotion_stats` is measured against that window.

  `promotion_engine`'s module docstring described itself as *"the 'earned, not configured' autonomy
  no published tool ships (C5)"* — a product claim, in the source of the module that would deliver
  it, and untrue of what ships: the only production caller can revoke `watchtower-autofix`'s A3
  authority and never grant it. The docstring now states which half ships and why the other cannot
  be fed from these samples. Autonomy here is configured and can be taken away statistically; it is
  not granted statistically.

- **A collapsed record can now take the watchtower's write authority back — and can never hand it
  out.** Pass 269 gave `promotion_outcomes` a writer; nothing read it, so ADR-102's *fast down, slow
  up* had no down at all: a class whose agreement had collapsed kept auto-fixing forever. Behind
  `KI_V5_STATISTICAL_PROMOTION` (default off), the watchtower now asks the store whether
  `watchtower-autofix` still holds its unattended-write authority before letting an investigation fix
  anything, and closes the A3 gate when a demotion trigger has fired — two postcondition failures
  within 24 h will do it. The direction is deliberately one-way: **revoke, never grant.** Every
  sample in the store comes from a fix the allowlist already permitted, so earning authority from
  them would be circular — the grant producing the evidence for the grant — and gating the write on
  earned rungs would deadlock a class with no samples out of ever producing one. ADR-102 earns rungs
  from *shadow* agreement, which this system does not run yet, so the allowlist stays the only way
  up. Flag on with no outcome store ⇒ the brake abstains and logs that it is not operating (failing
  closed there would silently disable A3 on any deployment that enabled a promotion flag without
  Postgres); a store that exists and cannot be read ⇒ auto-fix is revoked rather than assumed clean.

- **The scheduled post-fix re-check now reads the cluster before it records that it re-checked.**
  Prospective memory (ADR-017, `MEMORY_PROSPECTIVE`, default off) exists to answer the one question
  an operator asks after an autonomous fix — *did it hold?* Its dispatcher was pluggable "so the
  watchtower can wire a real investigation", and nothing outside the tests ever called
  `set_dispatch`, so production always ran the fallback: it logged the row, returned `"rechecked"`
  without looking at anything, and `_TERMINAL` mapped that to `status='done'`. Every scheduled
  re-check therefore closed as a completed verification of a cluster nobody had read, and the row
  was indistinguishable from one that genuinely passed. The fix makes the default *be* the
  verifier rather than keep waiting for a caller that was never written: two read-only `kubectl
  get` reads of the row's namespace (pods + Warning events, off-thread so a slow cluster cannot
  stall the consolidation loop), scanned by the same `_scan_snapshot` that
  `coordinator._verify_resolution` uses — so the codebase's two post-fix graders cannot disagree
  about the word "resolved", lingering warnings over healthy pods included. Outcomes are now
  `resolved` / `still_broken`, and a read that *failed* is `unverified`, which is deliberately
  absent from `_TERMINAL`: the row returns to `pending` and retries next pass instead of closing on
  an answer nobody obtained. `set_dispatch` stays as the injection seam for a richer (LLM-driven)
  re-check and for tests — it is no longer load-bearing. `"rechecked"` is retained in `_TERMINAL`
  only so pre-existing rows still read as terminal; nothing produces it any more.

- **The ADR-102 promotion store has a production writer, and the column that identifies its
  samples now holds the right value.** `promotion_outcomes` is the evidence an action class needs
  to *earn* a rung — the "earned, not configured" autonomy the project claims as a
  differentiator. `record_outcome` was called only from its own tests, so `outcomes_from_store`
  returned `[]` for every class, `decide()` answered `hold` with the honest reason *"n 0 < n_min
  20"*, and every class sat at its configured rung permanently. The demotion direction is the
  sharp end: ADR-102 is *fast down, slow up*, so a class whose agreement had collapsed could not
  be demoted either. Wiring a writer first needed a way to tell an autonomous attempt from a
  human-driven one, and the column that exists for that was wrong — `write_episode(trigger_kind=…)`
  had three call sites, and while `cortex/graph.py` read `state["trigger_source"]`, **both** V2
  coordinator writers hardcoded `"user_query"`. `CORTEX_V4_ENABLED` is off by default, so in the
  shipped configuration every watchtower investigation was stored as a user query: the digest
  could not ask the column and fell back to matching *"autonomous investigation"* in
  `trigger_detail`, and provenance drives write-admission trust (`security._TRUST`: detector 1.0,
  user_query 0.4), so detector-derived episodes were validated as if a chat client had typed them.
  All three writers now share one `episodes.trigger_kind_for`. On top of it,
  `promotion_source.record_autonomous_attempt` records one sample per autonomously-attempted,
  cluster-verified fix, behind `KI_V5_STATISTICAL_PROMOTION` (default off) and fire-and-forget
  like every other memory side effect. Three cases are deliberately **not** recorded, because each
  would be a sample the store invented: a fix a human asked for, a report-only investigation, and
  an outcome whose post-fix cluster read failed — `_verify_resolution` reports that as `None` for
  the same reason it reports verification-disabled as `None`, and a read is most likely to fail
  right after the disruptive change being graded. `critical` is always `False` here: nothing on
  this path attributes a Sev-1/Sev-2 to an action, so the M4 demotion trigger is not fed from it.
  Nothing acts on the evidence yet — `earned_rung` is still read only at the ACI chokepoint, which
  has no production caller — and `docs/how-it-works.md` says exactly that.

- **A page that states its own permissions now has to have asked: `GET /v1/auth/whoami`.** The
  Hugging Face Space printed, in fixed HTML, *"This demo key holds the `readonly` role, so a
  write is refused by RBAC before it runs"*. Nothing read the key's role. `KI_API_KEY` and
  `KI_API_BASE` are both environment variables, and the `ki-ro-`/`ki-op-` prefix is a naming
  convention rather than a grant — a key called `ki-op-…` sitting in
  `KUBEINTELLECT_READONLY_KEYS` is `readonly`, an unrecognised key is rejected, and a
  deployment with no keys configured makes every caller `admin`. The sentence was therefore
  true only for the one deployment it was written for, and it cost a real take: the first
  chat-UI recording was made with an operator key, under a footer asserting the opposite, next
  to an agent that would have executed the write. The new route returns the **caller's own**
  role and nothing else; it sits on the authenticated router, so an absent or unrecognised key
  never reaches the handler. The Space renders what comes back, one sentence per role matched
  to the four-tier model in `app/api/v1/auth.py`, and when the probe fails — including against
  a server too old to serve the route — it says the permissions are unconfirmed and tells the
  visitor to treat the deployment as write-capable, rather than falling back to the
  flattering answer. The live-cluster chip carried the same unchecked claim in three fewer
  words (`N namespaces · read-only`) and now names the role too. `docs/api-reference.md`'s
  *"the key's prefix determines the role"* is corrected in the same pass.

- **A detector predicate that matches a *healthy* object is refused when it is authored.** The
  dead-predicate family got three gates because a predicate that can never fire contributes
  silence, and silence reads as a healthy cluster. The mirror image had none: a Pod predicate
  matching a healthy status fires on every pod on the cluster, for ever. `nl:soak-cpu-saturated`,
  authored (ADR-012) from *"a workload is pinned at its CPU limit"*, compiled to
  `{kind: Pod, status_regex: "^Running$"}` — `WatchPredicate.matches` tests the status and
  nothing else, there is no namespace or label scope, and the trend predicate carrying the actual
  CPU condition runs on a separate loop and is OR'd, never AND'd. On a soak cluster its ring held
  46 findings before any fault was injected, every one a `kube-system` coredns pod with
  `evidence: "pod status=Running"`. `predicate_shape.predicate_health_errors` now asks the
  predicate the same question the engine will — `status_regex.search(status)` against the
  statuses `pod_display_status` emits for a healthy object — and the authoring and review gates
  refuse it. `^NotReady$` and `^.*NotReady.*$` are untouched: `Ready` is a substring of
  `NotReady`, and anchoring is what keeps them apart. Zero shipped playbooks trip it.
- **The engine loads such a detector anyway, and says so.** Refusing at load would delete the
  evidence that the detector is wrong and move a measured false-positive rate toward the
  pre-registered direction by removing the rows that falsify it — the mistake a previous liveness
  gate made before `dropped_predicates` split refusal per predicate. `DetectBlock.fires_on_healthy`
  carries the reason, the loader logs `db_detector_fires_on_healthy_objects`, and
  `GET /v1/detectors/<n>/shadow-findings` puts it in `watching_reason`, because `watching: true`
  on a detector that fires on health is true and misleading in the same breath — a reviewer reads
  a firing count as a fault count.

- **A local-LLM judge now records the machine it ran on.** `rescore_ollama.py` writes
  `judge/provenance-<tag>.json` next to each re-scoring: CPU model, core count, AVX-512 flags,
  Ollama version, model digest, size and quantization, and the pinned sampling. Everything read
  locally is prefixed `client_`, and the inference host is recorded only when stated
  (`RESCORE_INFERENCE_HOST`) — because a tunnelled Ollama answers on `127.0.0.1`, so the script's
  own CPU may not be the CPU that ran inference, and a confidently wrong answer to "which machine
  produced this?" is worse than an absent one. This
  exists because the host turns out to be part of the result (see *Fixed*, below), and
  `scores_<tag>.json` carries scenarios and nothing else — so the machine behind an existing
  cross-family table is unrecoverable from the artefact. It lands in the `judge/` subdirectory
  and never as a key inside the scores mapping: both alternatives recreate the phantom-scenario
  defect, since every `*.json` beside a run's records and every key in the scores dict is read
  as a scenario. Provenance never raises — an unreachable Ollama degrades to a partial record
  rather than failing the run it describes.

- **`/healthz` reports what memory *did*, not only that its pool is up.** `.memory` now carries
  `recall_attempts`, `recall_hits`, `recall_failures`, `episodes_written`, a `symptoms` list and a
  single `healthy` boolean. `enabled: true, healthy: false` is the connected-but-doing-nothing
  case, which was previously indistinguishable from a healthy server on a quiet cluster — an
  evaluation lane once ran nine hours reporting `state: "ready"` while nothing was ever written or
  recalled, and nothing in the response was false. A cold store is not a fault (symptoms stay
  silent for the first ten attempts) and memory switched off by flag reports `healthy: true`.
  `healthy` deliberately does **not** move the top-level `status`: `/healthz` is liveness, and
  failing it because memory is cold would turn a degraded subsystem into a crash loop. Alert on
  `.memory.healthy`, restart on `.status`.
- **The stream reports what a request cost.** A new `usage` `ki_event` carries `prompt_tokens`,
  `completion_tokens`, `total_tokens` and `llm_calls`, summed across every model call a turn makes
  — triage, the coordinator loop, the subagent fan-out, verification — and emitted immediately
  before the `finish_reason` terminator so a client that stops reading there still sees it.
  Previously the server surfaced usage nowhere at all. `llm_calls` is what makes a zero
  interpretable: `0` means no model was called, while a positive count with `total_tokens: 0`
  means the provider reported nothing, which is an instrumentation gap rather than a free turn.
  Reported whether or not Langfuse tracing is enabled.

- **A gate now keeps the two contributor rosters in sync** (#167).
  `.all-contributorsrc` and the **Contributors** table in `README.md` are both hand-maintained,
  nothing compared them, and they had drifted: 5 people on one side, 8 on the other, and one
  contributor on neither. `scripts/check-contributor-roster.py` fails naming exactly which
  handles are missing from which surface, plus a person listed twice or spelled two ways.

  It refuses to pass on an **empty** read. A comparison that quietly stops seeing rows — a
  renamed heading, a changed row format — would otherwise compare two empty sets and report
  green forever, which is how this repo has lost a guard before.

  `make check-roster`, and a step inside the existing **Syntax warnings** CI job. It joins that
  job rather than adding one of its own on purpose: branch protection matches required checks by
  name, so a new job name is a check `main` does not require, and every open PR would sit
  unmergeable until the settings caught up.

  Two related gaps closed while wiring it: `scripts/dev-setup.sh` never ran the encoding gate
  added in #161, and `make check-encoding` was missing from `.PHONY` and from `make help`.
  Contributor setup now runs eight gates, not six, and `AGENTS.md`, `CONTRIBUTING.md` and
  `TRIAGE.md` say so.

### Fixed

- **One missing table made `verify-restore` invent eight more problems** (`app/cli.py`
  `_backup_query`, `v4/tests/test_one_missing_table_must_not_invent_eight_more.py`).
  `app/db/backup.verify` is built to report *every* discrepancy rather than the first — it
  catches each check's exception and carries on, because mid-incident a list of three things to
  fix beats one error and a re-run. The driver underneath defeated it: the connection was not in
  autocommit, so the first failing statement poisoned the transaction and every later query died
  with *current transaction is aborted*. Measured against a real restore missing exactly one
  table: **9 discrepancies reported, 1 true**, and six of the false ones said `the restore did
  not create it` about tables that were present and correct. An operator reading that
  mid-recovery concludes seven tables were lost when one was. The same database now reports 2,
  both `memory_audit`, both true. Nothing caught it because the suite drives `verify` with a
  dict, and a dict has no transaction to poison. `chain-truncate` keeps its explicit transaction
  — there the record and the DELETE must land together or not at all.

- **`backup-manifest --out FILE` exited 0 without writing FILE** (`app/cli.py`,
  `v4/tests/test_a_manifest_that_was_never_written_is_not_a_success.py`). In SQLite mode the
  command printed its "the whole database is one file, copy it while stopped" advisory and
  returned — before `args.out` was read at all. The advisory is right (none of the counted
  tables exists there; that file holds the LangGraph checkpointer), but reporting it as success
  meant the documented chain `backup-manifest --out m.json && pg_dump …` ran its second half
  believing the first half had produced proof. The manifest is the *only* thing that can see a
  restore which silently dropped the newest rows of `decision_log` or `memory_audit` — such a
  restore breaks no hash link, so the shortened record still verifies — and its absence is
  discovered at restore time, exactly when it can no longer be taken. A request for a file that
  produces no file now exits 1 and says what to do instead; with no `--out`, nothing was
  promised and the advisory still exits 0.

- **One observation stream had two bounded queues and only one knob.** The sensorium sink calls
  `engine.process()` and `enqueue_observation()`; the second feeds a queue drained onto the
  knowledge graph, and it was created with a hardcoded `maxsize=10_000` while the queue directly
  upstream took `SENSORIUM_QUEUE_MAXSIZE` from configuration. An operator on a busy cluster could
  raise the sensorium buffer, watch `shed_total` stop climbing, and conclude the loss was fixed —
  while the memory queue behind it kept dropping at the old depth. Both losses were always
  reported (`shed_total`, and `observations_dropped` on `/healthz`); what was missing was the
  ability to do anything about the second one. Now `MEMORY_OBS_QUEUE_MAXSIZE`, defaulting to its
  twin and documented beside it.

- **The demos page documented eighteen recordings and showed none of them.**
  `scripts/demo/DEMOS.md` describes eight recorded scenarios across 352 lines, and embedded no
  image at all: the sixteen rendered GIFs and the two browser-UI GIFs were committed to the
  public repository and referenced only by the directory listing at the foot of the page. A
  reader had no way to see any recording without cloning the repo. The current (`gifs-kq/`)
  corpus and both chat-UI recordings are now embedded inline, collapsed behind `<details>` so
  the page does not pull 3 MB unasked; the superseded `gifs/` corpus stays unembedded on
  purpose and the page now says so. `v4/tests/test_the_demos_page_shows_the_demos.py` gates
  both directions — every current recording is shown somewhere, and every embedded path
  resolves — so a new recording that nobody links fails at the point it is added.

- **The security page described the Homebrew tap and the krew index inaccurately.** The tap's
  formula does pin a `sha256` and it verifies — the pinned digest matches the file byte for byte
  — but what it pins is the **PyPI sdist** published by `publish.yml`, not a GitHub release
  archive and not the PyInstaller binaries `release-binaries.yml` attests; and the file it
  resolves to carries no PEP 740 attestation, because those were only enabled on 2026-08-28. The
  krew row implied a live channel: nothing is installable with `kubectl krew` today. Both rows now
  say what was measured.
- **`.krew.yaml`'s `caveats` carried usage strings the krew maintainers had asked us to remove** —
  an `export`, a `kubectl kq` invocation and a `helm install` command. Krew already tells users
  how to run a plugin and links the homepage, so the block now states what the client needs and
  points at the documentation.

- **`kubeintellect status` described a configuration assembled from a file it never named, and
  the documented precedence was wrong for every non-CLI launch.** Two `.env` files are read —
  `~/.kubeintellect/.env` and a `./.env` in the working directory — and the two loaders disagree
  about which wins. Measured with one key set differently in both: `kubeintellect serve` resolves
  the **home** value (its loader exports that file into the environment first, and a real
  environment variable outranks any `.env`), while a directly-launched server — uvicorn, the
  container image, the Helm chart — resolves the **working-directory** value (`Settings` lists
  `./.env` last, and the last file has priority). `configuration.md` claimed a project `.env` was
  "lowest priority" and "can never override the admin keys in your `~/.kubeintellect/.env`";
  measured, a directly-launched server takes `KUBEINTELLECT_ADMIN_KEYS` from the project file.
  The Config row now names **every** file it read, marks a working-directory-only config as
  present rather than missing, and prints the keys the two files set to different values. Which
  file *should* win is still open; being told that both were read is not.

- **The documented `helm install` could never have worked — the chart repository it names does not
  exist.** `helm repo add kubeintellect https://mskazemi.github.io/kubeintellect` fails immediately
  (*"not a valid chart repository … index.yaml : 404 Not Found"*): that URL is the documentation
  site, and `helm-publish.yml` has only ever published the chart as an **OCI artifact**. The three
  `docs/index.md` quick-starts (v4, v3, v2) now use the OCI form, which needs no `helm repo add` —
  verified by running it: `helm template kubeintellect
  oci://ghcr.io/mskazemi/charts/kubeintellect` resolves 2.3.1 and renders the manifests.

- **The README's Docker command could not run, on any tag, and never could.** `docker run --rm -it
  ghcr.io/mskazemi/kubeintellect:2.2.0 --help` exits **127** — *exec: "--help": executable file not
  found in $PATH* — because the image has no `Entrypoint` and its `Cmd` is the uvicorn server, so
  the flag is read as the command. No release could have fixed it: it was a documentation defect
  for as long as the block existed. There is also no `kubeintellect` console script inside the
  image to fall back on. The block now runs the server, which is what the surrounding text
  promised, and shows the `/healthz` line that proves it came up — verified verbatim against
  `2.2.0`: `{"status":"ok","arm":"v4",...}`.
- **Every published image carried `pytest`, `mypy` and `ruff` inside the runtime venv, while
  `deploy/image.md` said build tools were stripped from it.** The Dockerfile passed
  `--no-dev --all-extras`, and `kube-q` declares its test and lint toolchain as a PEP 621
  *extra* named `dev` — an extra is not a dependency-group, so `--all-extras` re-added precisely
  what `--no-dev` excluded. The build now names its runtime extras (`--extra tracing --extra
  metrics`). Measured on a local build: venv **296 MB → 200 MB**, image **646 MB → 519 MB**, with
  `langfuse` and `prometheus_fastapi_instrumentator` still present, `/healthz` and `/metrics` both
  200. Takes effect on the next published image.

- **The anti-flap band demoted classes for being young, and its audit line said their agreement had
  fallen.** A Wilson lower bound is driven by sample size as well as by failures, and
  `hysteresis_breach` compared it to `θ − 0.05` at any n. Measured: at θ=0.95 a class with a
  **flawless 15-for-15** record scored 0.8472, breached the band, and `evaluate_demotion` dropped it
  a rung with the reason *"hysteresis: last-50 LCB < θ − 0.05"* — a recorded statement that a class
  which had never failed was getting worse. A perfect record needs n ≥ 16 (θ=0.90), 25 (θ=0.95) or
  43 (θ=0.99) before the band is even reachable. The resulting order was backwards: a class with
  **zero** events was not demotable (the empty-window early-out), while the same class became
  demotable the moment it succeeded once and stayed so through its first two dozen consecutive
  successes — evidence of competence was strictly worse than no evidence. The band now abstains
  wherever a perfect record at that sample size could not clear it, so a breach is always
  attributable to failures. `cusum_trip` — two postcondition failures within 24 h, at any sample
  size — is untouched and remains the trigger for a class that is genuinely going wrong early.

- **`kubeintellect init` no longer declares a Prometheus, Loki and Grafana it has never seen —
  and no longer discards the ones it detected.** Two bugs in the same three lines of the
  generated `.env`. The template called `_line("PROMETHEUS_URL", "http://172.18.0.2:30090")`,
  and `_line` reads its second argument as a *value*, not an example — so the line was written
  **active** for every install. `172.18.0.2` is the first address of Docker's default `kind`
  bridge, so a user who declined the Kind cluster, or pointed at EKS, met three red ✗ on their
  first `kubeintellect status` for services they had never installed; the comment directly above
  those lines already claimed they were "set automatically … when observability stack is
  installed", which is what the code was meant to do and did not. The mirror-image bug hid
  behind it: `_setup_observability()` detects the real node IP and appends it to the config
  file, but it runs *after* `existing` is loaded, so the template rewrite at the end of
  `cmd_init` overwrote the detected value with the hardcoded guess — and on a default `kind`
  bridge the guess is frequently the same address, which is why it survived. `_line` now
  separates `hint` from `default`, and the observability keys are re-read from the file before
  the rewrite. Pinned in both directions by
  `v4/tests/test_init_does_not_invent_an_observability_endpoint.py`. The documented behaviour
  (`v4/docs/configuration.md` — default `""`, set only when the stack is installed) was correct
  throughout; it is the code that has caught up.


- **A dead predicate took the detector's live predicates down with it.** The load-time liveness
  gate refused any stored detector carrying a predicate that could never fire — and refused the
  whole row. One stored detector carried an unfilled forecast template beside a Pod predicate
  matching every healthy `Running` pod: refusing the row deleted a real, firing predicate to be
  rid of a dead one. The loader now drops the dead predicate, keeps the live ones, logs
  `db_detector_predicate_can_never_fire` with the reason and the surviving count, and records
  the drop on the compiled block so `watching_reason` can say that the predicate in the store is
  not the predicate that ran. A row with no live predicate left is still refused whole — that is
  a dead detector and there is nothing to keep.

- **`watching: true` meant *loaded*, not *evaluated*.** `/v1/detectors/{name}/shadow-findings`
  reported `watching: true` for any detector the engine held in its shadow set, including a
  detector whose only predicates are *trend* predicates on a server where
  `PREDICTIVE_DETECTION_ENABLED` is false — a configuration in which nothing evaluates them and
  the firing count is therefore not a measurement. On the live soak that was two of eight
  detectors, each reporting `watching: true` beside a `predictive: "off"` server. The field now
  means the engine will actually evaluate the detector, and a new `watching_reason` string says
  which of the four cases produced the answer — not loaded, watch predicates evaluated per
  observation, trend predicates evaluated on the predictive interval, or loaded-but-inert with
  the flag named. A precision or recall figure whose denominator counts an inert detector is
  measuring the flag, not the predicate.

- **A container killed for memory was invisible to the thing watching for it.**
  `pod_display_status` claimed to mirror the STATUS column `kubectl get pods` prints, and had no
  *terminated* branch — so `OOMKilled`, `Error`, `Completed`, `ExitCode:N` and `Signal:N` were
  all outside the range of the only function a Pod predicate is ever matched against. A detector
  whose predicate was `^OOMKilled$` could not fire on any cluster, and its silence read as a
  cluster that never ran out of memory. The function now implements kubectl's `printPod` in full:
  `status.reason` as the *base* rather than a last-resort fallback, the container loop walked in
  reverse so the first container has the last word, the terminated branch, the
  `Completed`-beside-a-runner → `Running`/`NotReady` rule, and `Terminating` only while the phase
  is non-terminal (a `Succeeded`/`Failed` pod under deletion keeps its own outcome instead of
  having it erased). Thirteen of the twenty new tests fail against the previous implementation.

- **A forecast pinned to the authoring model's own template was staged, stored and offered for
  promotion.** `predicate_liveness_errors` only ever walked `watch_predicates`; `trend_predicates`
  had no liveness gate at any of the three points a detector passes through. Two detectors staged
  on a soak cluster forecast `kube_deployment_status_replicas{deployment="your-deployment-name"}`
  — the template, unfilled — matched no series for twenty-four hours, and reported nothing. The
  new `trend_liveness_errors` refuses a selector whose label value is an unfilled template
  (`your-*`, `<name>`, `{{ … }}`, `${…}`, `CHANGEME`), an `min_r2` above 1.0, a non-positive ETA
  bound or lookback window, and a `direction` that is neither `rising` nor `falling` — that last
  one because `project_eta` treats anything but `falling` as rising, so a typo did not fail, it
  silently asked the opposite question. Ordinary names that merely *look* like placeholders
  (`example`, `foo`, `test`, `my-app`) are deliberately left alone: refusing a working detector
  is the worse error. Wired into the authoring validator, the engine's load path, and the
  promotion gate.

- **The promotion gate could not see the detectors it was built to stop.** `review._liveness_error`
  read `WHERE cluster_id = $1` while `stage_candidate` writes `cluster_id = 'global'`, so on any
  deployment that sets `CLUSTER_ID` the lookup found no row, returned `None` — which the caller
  reads as *no reason to refuse* — and promoted without ever checking. Same defect, and the same
  fix, as `engine.load_db_detectors`: `cluster_id IN ($1, 'global')`, preferring the
  cluster-specific row when both exist.

- **A judge's stated total is no longer taken as the score.** The rubric is eight dimensions of
  1–5, so the total is defined by the dimensions rather than observed — but both judges returned
  their own arithmetic and both code paths trusted it. Measured over every score in the tree: the
  primary judge disagreed with its own dimensions in **0 of 1761** records; the local robustness
  judge in **494 of 665 (74%)**, mean −3.35, and one verdict read `44/40` against a 40-point
  maximum. `normalize_verdict()` is now the single implementation and recomputes the total,
  clamps each dimension, and derives pass/fail from the threshold. It changes no stored primary
  total and corrects five stored `result` fields where the judge scored at or above the threshold
  and still wrote "fail". A judge *error* record is exempt — a judge that could not be reached did
  not score 8/40, it did not score.
- **A fault-probe timeout no longer reads as a missing fault.** `runner._wait_for_fault` greps
  `kubectl get pods` for pod-phase keywords, every one of which is a failure phase, so a fault
  that leaves pods `Running` is outside what it can observe — four scenarios time out
  deterministically while the fault is demonstrably present. The timeout branch now separates
  `unobservable` (pods healthy; a limit of the probe) from `no_pods` (the one timeout worth
  investigating), and the observation, including the keyword list that defines the blind spot,
  is stored on the record instead of printed and discarded.
- **Run metadata names the code that ran, or says why it cannot.** `_write_metadata` shelled
  `git rev-parse HEAD` and fell back to `"unknown"` on any exception, which fired for every run
  on a VM with no `.git`. The correct commit was already in `provenance/manifest.json` in the
  same directory, resolved by `_harness_block` — one run directory, two provenance files
  disagreeing, and the one every reader opens held the wrong one. Metadata now asks that same
  resolver and records `git_commit_source`; an unresolvable commit reads `"unavailable"` with the
  reason, never `"unknown"`, which reads like a value.

- **"Temperature 0 and a fixed seed" does not make a local judge reproducible, and the docstring
  said it did.** `rescore_ollama.py` claimed "Reproducible by anyone with the same Ollama model
  pulled." Measured on the nine-scenario calibration set: the same `gemma3:12b` (digest
  `f4031aab637d`), the same Ollama `0.32.15`, the same seed and temperature, and **both hosts
  running fully on CPU** (`size_vram: 0` on each, so no GPU confound) — yet **5 of 9 scenarios
  scored differently across two machines**, mean |Δ| 2.11, max **8 points out of 40**. A second
  run on the *same* host reproduced all **9/9** exactly. The hosts differ only in SIMD path and
  thread count (an i7-1370P without AVX-512 at 20 threads versus an EPYC 9V74 with `avx512_vnni`
  at 16); threaded matmul reduces partial sums in a hardware-dependent order, floating-point
  addition is not associative, and greedy decoding turns a near-tie into a different token. The
  docstring now states the measurement and directs readers to regenerate tables from the archived
  `scores_<tag>.json` rather than by re-judging. The submitted paper needs no correction: its
  appendix already describes this judge as deterministically *configured* rather than provably
  bit-exact, and names backend and kernel scheduling among the residual sources.
- **A cost report wrote a file nobody asked for, into a directory this repo does not have.**
  `evaluation/cost.py` declared `--output` with `default="paper/tables/cost.md"` — the canonical
  trees are `papers/` (plural) and `evaluation/runs/**/_analysis/`. Every defaulted invocation
  silently created an untracked `paper/` at the repo root holding a second, divergent copy of a
  table that already existed somewhere canonical, and nothing warned, because writing an
  unrequested file is indistinguishable from success. `./ship` stages with `add -A` on both gits,
  so a stray root directory was one careless invocation from being committed. The table now always
  goes to stdout and a file is written only when `--output` is given.

- **A test suite that could reach a live judge, and only passed because none was running.**
  `evaluation/test_judge_model_axis_refusals.py` proved the completeness gate had let a design
  through by watching the robustness judge's pre-flight abort — an abort that happened only
  because no Ollama server was up. Once one was running locally the same test stopped proving
  anything and started *judging*: 10 arm-repeats x 27 synthetic scenarios against a real 12B
  model, for minutes, competing with the campaign's own judge validation for that server, and
  the processes outlived the pytest run that spawned them. The tests now point the judge at a
  closed port, so the abort is the same on every machine: the file went from 127s and failing
  to **7.3s and green**.
- **The judging gate counted answers, not the records the judges read.**
  `judge_model_axis.sh` required 27 `*_response.txt` per arm-repeat but never checked for the
  27 `<scenario>.json` records that `score_run` and `rescore_ollama` actually score. An
  arm-repeat that lost its records passed the gate, both judges wrote a scores file reporting
  "0 scenarios" and exited 0, and only `analyze_h2.py` objected — at the end, with the credit
  and the hours already spent, which is the exact outcome the gate exists to prevent.

- **`evaluation/check_fault_confirmation.py`** — tells a fault-probe blind spot apart from a
  missing fault. `_wait_for_fault` matches pod-phase keywords only, so four of the 27 scenarios
  can never be confirmed and log `timeout — proceeding.` in every arm; the line reads as an
  absent fault and is not one. The checker classifies each scenario (confirmed / sporadic /
  blind spot) and fails only on an *empty observation* — never confirmed **and** no substantive
  answer in any arm, the one case that carries no evidence in either direction.

- **A test module narrowed the blocklist for the whole pytest process, and eight security cases
  silently stopped existing.** `evaluation/test_autonomy_containment.py` pinned its policy with
  `os.environ.setdefault(...)` at *module scope* — under a comment saying it made the test
  env-independent, which it achieved by making every later module depend on it instead. Because
  `v4/tests/test_the_snapshot_is_not_a_second_cluster.py` parametrised over
  `settings.kubectl_blocked_namespaces`, the narrowed list *deleted* its cases rather than failing
  them: every case proving `cert-manager`, `ingress-nginx`, `kube-public` and `kube-node-lease` are
  refused at the funnel and absent from the prompt simply was not collected. Nothing turned red;
  the suite reported green having tested less of the product. The only visible symptom was the
  doc-claims gate reporting a suite-count drift nobody had caused (4551 vs 4543), which reads as a
  stale number rather than as missing coverage. The blocklist cases now parametrise over the
  *shipped* default read from the `Settings` field, a fixture pins the guard to that same set, and
  a new test asserts the collected node ids are identical under a deliberately narrowed
  `KUBECTL_BLOCKED_NAMESPACES`. `setdefault` had not even delivered what it promised: where the
  variable was already set, the ambient value won.

- **A rate-limited judge scored as a bad answer.** `evaluation/judge.py` turned any transport
  exception into `{"total": 0, "result": "error"}` with no retry, so a single HTTP 429 removed
  that scenario from `scores.json`, which in turn made `analyze_h2.py` refuse the whole arm for
  being short. Judging 270 scenarios serially against a 5,000 TPM deployment, that made *which
  model reached the primary analysis* a function of when the quota happened to run out. The
  transport now retries a transient failure (429, 5xx, timeout, connection reset) up to six times
  with jittered exponential backoff, honouring a server `Retry-After` when one is sent, and a
  deterministic failure (HTTP 400, content filter, unknown deployment) still fails on the first
  attempt rather than six. A judge error also records *why* it failed instead of the bare string
  `judge failed`. Retrying changes no measurement — the rubric, prompt and model are identical on
  attempt 4 and attempt 1.
- **A cost row named the directory, not the arm.** `evaluation/cost.py` labelled each row with
  `run_dir.name`, which is the literal string `results` for every lane that nests its output — the
  model-axis lane writes `runs/model-axis/<arm>-r<n>/results/`, so five arms produced five rows all
  called `results` and no dollar figure could be attributed to a model. The arms differ in unit
  price by 20x. The label now comes from the run's own `metadata.json["tag"]`, falling back to the
  arm directory when the leaf is generic.
- **The cross-family judge is now required to demonstrate discrimination before it is used.**
  `evaluation/validate_robustness_judge.py` scores a candidate on nine calibration scenarios drawn
  from an archived run and adopts it only if its totals span at least 8 points, separate the
  known-good from the known-bad group by at least 4 points, and leave headroom (at most 3 of 9
  perfect). A second judge that scores everything 40/40 agrees with nothing and disagrees with
  nothing, so it cannot serve as a control; candidates are tried in a fixed order and, if none
  passes, the robustness analysis is reported as not performed rather than quietly skipped.
- **An unreachable cross-family judge was recorded as a judge that scored zero.**
  `rescore_ollama.py`'s `_call_ollama` returned `""` after exhausting its retries; the parser
  turned that into `{}` and the normaliser into every dimension `0`, `total: 0`, `result: "fail"`
  — a transport timeout written into `scores_<tag>.json` as the judge's considered opinion that
  the answer was worthless, then averaged into the arm's mean. A 12B judge takes ~71 s per
  scenario on CPU and the timeout was 180 s, so this would have pulled down whichever arm happened
  to be judged while the host was slow. It now raises, the scenario is **excluded** rather than
  scored, the reason is written to `judge/errors-<tag>.json` (a subdirectory, so it cannot become
  a phantom scenario), an unparseable reply is treated the same way, and a run that comes up short
  exits 3 instead of reporting a complete-looking mean. Default timeout raised to 900 s
  (`RESCORE_TIMEOUT_S`).
- **The cross-family judge could run on the host it was measuring.** `rescore_ollama.py` serves a
  12B model on CPU, which takes every core of the evaluation VM, while the runs it scores recorded
  `latency_s` on that same VM. Starting a re-score during a campaign lane took the 1-minute load
  average from 0.44 to 9.6 on 16 cores. It now refuses to start while `run_model_axis_lane.sh`,
  `run_rca_lift_lane.sh` or `run_campaign.sh` is alive on the host, naming the process it found;
  `--allow-concurrent-lane` overrides it for runs whose latency is being discarded anyway.
  The guard distinguishes a lane being **run** from a command that merely names one: a
  `pgrep -f` substring match also catches an editor open on the lane script, a `less` on its
  log, and the guard's own command line, and a guard nobody can satisfy is one that gets
  routinely overridden. The lane script must be `argv[0]`, or `argv[1..]` of a shell.
- **A second judge's scores file was loaded as an evaluation scenario.** `evaluation/compare.py`'s
  `load_run` skipped exactly `metadata.json` and `scores.json`, so the cross-family re-scoring
  written by `rescore_ollama.py` as `scores_<tag>.json` was globbed in as a scenario record with
  `aggregate_score` 0.0, `latency_ms` 0 and 0 tool calls — present only in run directories that had
  been re-scored, which made it a bias rather than noise. Judge means, pass rates, resolution rates
  and token means are provably unaffected (each drops the record through a `None`/`> 0` guard);
  latency and tool-call means were biased low by 1.3–1.6% on the arms that had a second judge and
  0% on the one that did not. Now skips every `scores*.json`.
- **The v4 test suite was red, and its result depended on an untracked file.**
  `compute_rqb_table`'s pass-rate assertion had been left on a strict `> 28` after the function was
  corrected to `>= 28` (`judge.PASS_THRESHOLD`), and on a bare token mean after that cell grew its
  own denominator. Separately, `evaluation/runner.py` calls `load_dotenv()` at import time, so
  importing one test module loaded the developer's repo-root `.env` into `os.environ` for the rest
  of the process — green on CI, which has no `.env`, and red on any machine that does. Test
  environments are now snapshotted per test.
- **The importance-ranked recall query could never run.** `_SQL_RECALL_TRGM_IMP` sorted by
  `sim * <weight>` where `sim` is a SELECT-list alias, which PostgreSQL rejects inside a larger
  expression, so the query raised `column "sim" does not exist` on **every** call. It is the query
  used when `MEMORY_IMPORTANCE` is on and `MEMORY_HYBRID_RETRIEVAL` is off. The non-hybrid error
  path then returned `[]`, so a query the database could not parse reached the model as an
  *absence of prior incidents*; it now raises `MemoryUnavailable`, matching the hybrid path.

- **"Do not retry an unavailable tool" had nothing to key on in six of its eight cases.** The
  cortex gather prompt has instructed the model since V4 to stop calling a tool that "replies that
  it is not configured or unavailable". Driving every such reply: only the two "the URL is unset"
  messages contained either word. A missing `kubectl`/`helm` binary, a refused Prometheus or Loki
  connection, and an unreachable cluster carried neither — the three cases where a retry provably
  cannot succeed. All of them now append a shared `[unavailable]` marker that the prompt names
  explicitly and that `POLICY_LINE_RE` protects from the downstream trims, while answerable
  failures (a missing pod, an RBAC denial) deliberately keep it off, since a differently shaped
  command can still get at those.
- **kubectl's most common cluster-down message matched no error pattern.** "The connection to the
  server 127.0.0.1:6443 was refused - did you specify the right host or port?" puts four words
  between "connection" and "refused", so the `apiserver_unreachable` pattern missed it: a cluster
  that was simply down produced no interpreter hint and no classification at all.

### Fixed

- **Two truncation markers used words no prompt named, and the cortex prompts named none at all.**
  The coordinator instructs the model to warn when tool output contains `[truncated` or
  `chars omitted`. Driving every shortening site over its cap and measuring what it really emitted:
  `run_kubectl` and `query_loki` conformed, `run_helm` wrote `[... N chars truncated]` and the
  cortex subagent summary bound wrote `…[summary truncated …]` — matching neither string, and
  therefore also unrecognised as policy lines the downstream trims must preserve. The cortex
  gather and synthesis tiers, meanwhile, carried no instruction about truncated output whatsoever.
  Marker text, instruction, and the patterns they must agree on now come from one module
  (`app/tools/output_policy.py`); the triage tier, which answers in strict JSON and must not print
  a warning, gets the inference rule instead — partial context is not evidence of health. Triage's
  own `snapshot[:3000]` slice, the third silent cut in this family, now reports what it dropped and
  keeps the `[Protected] … withheld` sentence that used to fall off its end.

### Fixed

- **The V4 cortex deleted the truncation notice `run_kubectl` had just written — deterministically,
  not occasionally.** `run_kubectl` caps its output at 8 000 characters and appends
  `[truncated: N chars omitted …]` *after* that cap, so an over-cap listing comes back at 8 173
  characters; the cortex bound was `content[:8000]`, the same number, so it removed exactly the
  notice on every single over-cap listing, and took the `[Protected] … withheld` sentence off a
  filtered one at the budget the same way. The model then read a clipped listing as a complete
  one on a route that is not optional for everyone — `LLM_PROVIDER=anthropic` requires
  `CORTEX_V4_ENABLED`. The two layers that bound tool results now share one predicate for "this
  line is about the result, not part of it" (`app/tools/output_policy.py`) and both carry those
  lines across their own cut. The chop of the rows themselves stays silent by default, which is
  what the ADR-101 harness flag exists to change; destroying another layer's sentence was never
  part of that trade.

### Fixed

- **The coordinator's tool-output trimmer dropped rows in silence, and deleted the `[Protected]`
  notice attached one layer below.** `_trim_tool_output` announced a loss only when what remained
  still exceeded the character cap. Measured on a 200-pod `kubectl get pods`, the model received
  a 30-row table with no marker at all — and because the "keep" pattern retains unhealthy rows,
  the dropped ones are the healthy ones, so "how many pods are Running?" answered 30. Worse, the
  withheld-namespace sentence `run_kubectl` appends is the listing's *last* line and matches no
  important-row pattern, so any filtered listing longer than 30 rows reached the agent with the
  announcement removed: the guarantee held for the tool's return value and not for what the agent
  read. Policy lines (`[Protected]`, a tool's own `[truncated` marker) are now lifted out of the
  trim and re-attached, dropped rows and lines are counted, and the marker uses the vocabulary the
  coordinator's own system prompt tells the model to watch for — it previously said "chars
  trimmed", matching neither pattern that instruction names, four hundred lines up the same file.

### Fixed

- **`GET /v1/namespaces` removed protected namespaces without saying it had, and `kq` reported
  them as missing.** The 2026-08-20 pass added the protected-namespace filter and the
  "this listing is NOT the complete set" announcement on the same day; `run_kubectl` received
  both, this endpoint received only the filter. So the product answered one question three ways —
  `kubectl get ns monitoring` refused out loud, `kubectl get ns` returned a listing marked
  incomplete, and `GET /v1/namespaces` returned `{"namespaces": ["default","shop"]}` in silence.
  `kq` treats a definite absence as proof, so `/ns monitoring` printed *"Namespace 'monitoring'
  not found in the cluster"* — false — and recommended `list all ns`, the one path that would
  have contradicted it. The response now carries the shared `withheldByPolicy` marker (a count,
  never the withheld names), and `kq` keeps *withheld* and *absent* apart: absence from a listing
  that admits it is short is no longer treated as evidence.

### Fixed

- **Blocked-namespace guard: a combined shorthand group hid the namespace entirely.** kubectl
  parses `-n kube-system`, `-nkube-system` and `-Rn kube-system` identically, but the flag reader
  looked for an argument *beginning* with `-n`, so every spelling in the combined-group family was
  invisible — `kubectl get pods -Rn kube-system`, `kubectl exec -itn kube-system pod -- sh` and
  `kubectl logs -fn kube-system pod` all reached the cluster with the guard having seen no
  namespace at all. Same class as the 2026-08-20 attached-shorthand fix, one form further out.
  Groups are now walked the way pflag walks them — left to right, stopping at the first letter
  that takes a value, so the `n` in `-ojson` is not misread as a namespace. The boolean/value
  split is measured from `kubectl <verb> --help` across every subcommand; `-f` and `-p` are the
  only letters whose meaning depends on the verb (boolean on `logs`, a value flag elsewhere), so
  the verb is threaded through to the reader rather than guessed. The `-o` reader used by the
  namespace-listing filters gained the same reach.

### Fixed

- **Flight recorder: three shapes escaped the payload redactor.** `_scrub_value` documented
  "every string *anywhere* in a payload"; a string used as a **dict key**, anything nested past
  the walk's depth bound, and a **set or frozenset** were all persisted verbatim into the
  tamper-evident `decision_log` while `REFLEXION_REDACT_SECRETS` reported that secrets were being
  scrubbed. The depth bound is the notable one: it exists so a self-referential payload cannot
  hang the drain task, but returning the unscanned subtree made the cycle guard a redaction hole,
  failing open in a module whose output is permanent — it now emits
  `<redacted-unscannable-depth>` instead. Keys are redacted with a new, narrower
  `redact_identifier`, since the full redactor maps the ordinary field names `token` and
  `password` to the same drop marker and would have merged two fields of an audit record into
  one; colliding secret keys are suffixed rather than merged. No call site produces any of the
  three shapes today, so this was a latent fail-open rather than an observed leak.

### Fixed

- **`ALLOWED_ORIGINS` was the one comma-separated guard setting the guard audit did not audit,
  and the only one that failed in both directions.** `main.py` passed
  `ALLOWED_ORIGINS.split(",")` straight to `CORSMiddleware`, which compares origins as exact
  strings, so the natural `http://a.example, http://b.example` produced a second entry with a
  leading space that no browser `Origin` header matches — that origin was silently not allowed
  and nothing said why. Whitespace is now stripped in a new `Settings.allowed_origins`, which can
  only ever allow origins the operator explicitly wrote. What stripping cannot repair is now
  reported through `unenforceable_guard_config` (startup log, `GET /v1/v5/status`,
  `kq v5-status`): a trailing slash, a missing scheme, a value that yields no usable origin —
  and `*`, which is reported as a security problem rather than a typo. CORS is configured with
  `allow_credentials=True`, so with a wildcard the server echoes the *calling* origin back and
  marks it credentialed; the browser rule that refuses credentials against a wildcard is never
  reached, and any site a logged-in operator visits can call the API with their session. Nothing
  is refused or silently rewritten — same posture as the rest of the guard audit.

### Fixed

- **`/healthz` had no field for the one subsystem whose job is watching.** The response already
  carried `leader`, `audit`, `memory` and `recorder` — each added because an empty table is
  indistinguishable from a quiet cluster — but nothing about perception. A sensorium that raised
  on the way up produced `status: "ok"` with no mention of it, while the cause was already
  recorded one call away in `sensorium_absence()` and reachable only from `/v1/findings` and the
  digest, which you consult once you already suspect a problem. `/healthz` now carries a
  `sensorium` block reporting the precise absence state (`disabled_by_flag`, `no_detectors`,
  `start_failed`, `standby`, `stopped`) so an outage is distinguishable from a leader-election
  standby, where a peer holds the singleton lock and is watching. `watching` is reported
  separately from `enabled`: the detector engine exists whether or not any `kubectl --watch`
  stream is connected, and the watch loop returns permanently when kubectl is missing, so
  `enabled: true, watching: false` is a real and durable state. A failure reading stream health
  is reported in the block rather than raised — a liveness probe that 500s restarts the pod.

### Fixed

- **Right-to-be-forgotten reported a completed purge for data it had not deleted.**
  `memory/security.forget_subject` (R8.4) returned a bare per-table row-count dict, in which a
  failed delete left no trace but an *absent key* and `{}` meant both "no database pool" and "the
  first delete failed". An entity purge called without a `cluster_id` was worse: `kg_entities`
  rows all carry a real cluster, so the delete ran against the literal cluster named `''`, matched
  nothing, and returned `{'kg_entities': 0}` — a well-formed receipt for an entity still in the
  graph. The same `''` was a deliberate cross-cluster wildcard eleven lines above, where
  `user_id` still bounds it, and a literal key below, where nothing does. It now returns a
  `ForgetResult(counts, complete, error)`: `complete` is only ever `True` when every requested
  delete ran, an entity purge without a cluster is refused rather than widened or guessed, and a
  request naming no subject is not reported as a completed forget. Fail-open control flow is
  unchanged — one dead relation still does not abort the call — but the caller is now told the
  purge was partial. Not flag-gated, and no code path calls it yet.

### Fixed

- **Change-first RCA read the change ledger under a key nothing was ever written to.** The two
  halves of the feature in `cortex/graph.py` resolved the cluster id differently: the ledger append
  fell back to `get_cluster_id()`, the prompt prior fell back to `""`. Any investigation whose state
  carries no cluster id — which is every watchdog-dispatched one (`sensorium/watchdog_dispatch.py`
  builds `cluster_id: ""`) — recorded the change and then read back nothing, and an empty prior
  renders as no block at all, so the prompt was indistinguishable from one for a cluster where
  nothing had changed. Both halves now resolve the id the same way, and an unresolvable cluster
  lands on the shared `UNRESOLVED_CLUSTER_ID` sentinel on both sides rather than on two different
  values. Behind `CORTEX_V5_ENABLED` + `KI_V5_CHANGE_LEDGER` / `KI_V5_CHANGE_FIRST_RCA`, all
  default-off experimental, so no default configuration was affected.

### Fixed
- a **failed cluster-change read reached the model as "nothing changed"** — in the one block whose
  job is to stop it ruling out a recent change. `kg.changes()` swallowed every exception and
  returned `[]`, `recent_changes_block` rendered that as `""`, and `_hierarchy_context` renders `""`
  by omitting the whole `## Recent cluster changes (last 15m)` section — which is byte for byte what
  a genuinely calm cluster produces. Measured 2026-08-24 at the layer that actually fails: a dead
  pool, a calm cluster and a KG that was never started all produced `block=''`, the last with no
  warning at all. "What changed in the last fifteen minutes" is the first question of an incident.
  Four lines above in the same function, the **episodes** half has appended an explicit
  `## Memory unavailable` block since pass 46, saying *"this is not the same as there being none"* —
  the argument was already written down for the sibling lookup. `kg.changes()` now raises the new
  `KGUnavailable` (twin of `episodes.MemoryUnavailable`), `recent_changes_block` propagates it, and
  the node injects `## Recent changes unavailable`, which tells the model not to rule out a recent
  change as the cause, and sets `degraded=true`. A calm cluster still injects nothing. **The graph
  write path is unchanged** — `upsert_entity`/`open_edge`/`close_edge` and the ingest helpers still
  swallow, because a failed observation write must never kill a turn and nothing reads its result as
  a fact. A KG with no pool is still `[]`: a configuration state, not a failed query.

- 🔐 the memory audit chain reported **an unreachable database as tampering**.
  `verify_memory_chain` returned a `bool` documented as *"True iff the chain is intact"*, and it
  gave a database failure two opposite verdicts: a failed row fetch returned `False` — the value
  that means TAMPERED — while a failed anchor read and a missing pool both returned `True`, the
  value that means intact. A tamper detector that cries tamper whenever its own Postgres is
  unreachable makes a false accusation about the operator's own data, and the cost is not the one
  wrong answer but that operators learn to ignore the alarm, which takes the real one with it. The
  sibling test file already owned that rule for the anchor path (*"tamper-evidence is worthless if
  operators learn to ignore it"*); the fetch path in the same function had the opposite reflex. A
  fourth state escaped as an exception: a `memory_chain_head` row this build cannot parse raised
  `ValueError` out of a bare `int(head["seq"])`, where every other failure produced a verdict.
  `verify_memory_chain` now returns the `ChainVerdict(valid, verified)` the flight recorder already
  uses for the same question — one vocabulary, both chains — an unparseable anchor returns a verdict
  instead of raising, and `docs/security.md` documents that both flags must be read. A **missing**
  anchor stays verified: the read ran and there is nothing to contradict (a chain written before the
  anchor existed).

- a **novelty score that never ran** was stored as the strongest novelty claim the module makes.
  `episodes.surprise` is a KG-novelty proxy in `[0,1]` where **`1.0` means nothing similar has ever
  been seen**, and `_surprise_novelty` returned `1.0` from all three paths where it measured nothing:
  the `similarity()` query raising, no pool, and empty episode text. On a database without the
  `pg_trgm` extension the query raises for *every* write, so the column fills with a maximum no
  measurement produced — while the same write logs `surprise scoring failed`. One row, two
  contradictory statements, and the row is the audit record. The column is a **nullable** `REAL`
  whose NULL already means "not scored" (every pre-P6 row, and every write with `MEMORY_IMPORTANCE`
  off), so the third state needed no new vocabulary. `_surprise_novelty` now returns `float | None`,
  the write stores `NULL`, and the warning names what was stored. **Fail-open is unchanged** — the
  gate reads `surprise is not None` first, so a failed score still never drops a write; it just no
  longer claims a measurement happened. A genuinely empty table still scores a real `1.0`: that is a
  query that ran and found nothing similar.

- an audit chain **nobody could check** was reported as a chain that checked out. `verify_episode`
  returned a `bool`, and every path where the `decision_log_head` anchor could not be consulted —
  the read raised, the head row was unparseable, there was no pool — returned the same `True` an
  agreeing anchor returns. In the middle case the recorder logged *"truncation of this episode is
  NOT currently detectable"* and the same call handed its caller the value that renders
  `✓ chain intact`: `kq replay` exited `0`, and the postmortem printed its `✅ Audit chain verified
  intact` banner. The third state was already spoken everywhere else — `kq replay` has owned exit
  `4` for an unverified chain, and the postmortem payload has carried `chain_verified` beside
  `chain_valid` — only the function producing the verdict could not say it. `verify_episode` now
  returns `ChainVerdict(valid, verified)`; `replay_meta` carries `chain_verified`; `kq replay`
  exits `4` on it (absent ⇒ `true`, so older servers read as before); and an episode with no
  records whose anchor could not be read answers `503` rather than `404` (never existed) or `409`
  (every record removed), neither of which is known. A **missing** anchor stays verified: the read
  ran and found nothing to contradict the records.

### Fixed
- `kubeintellect init` printed `── Setup complete ──` four lines below its own
  `[error] OPENAI_API_KEY: LLM_PROVIDER=openai but OPENAI_API_KEY is not set`, offered to
  install a systemd service that would start the server on every login, and exited `0`.
  Pressing Enter at the key prompt — the most likely thing a first-time user without a key
  to hand does — produced exactly that. `kubeintellect status` had classified the same issue
  list, from the same `_validate_config`, as an exit-1 failure since earlier the same day:
  one classifier, two consumers, and the one that *wrote* the file was the optimistic one.
  `init` now prints `── Setup INCOMPLETE ──`, names the blocking settings, points at
  `kubeintellect status`, starts nothing, and exits `1`. The config file and API key are
  still written so a re-run resumes. Warnings — a missing kubeconfig before `kind-setup`, an
  unset admin key — remain warnings and still complete the setup.

### Fixed
- the exit-code tables in `docs/cli-reference.md` were unverified prose. They are the
  machine-readable half of the CLI contract — what a script branches on — and `kq replay`
  documented five codes while returning six: the sixth, `2` for a usage error, hid inside
  `return 0 if asked_for_help else 2`, one statement yielding two codes. Six commands
  (`completion`, `config`, `digest`, `findings`, `preference`, `v5-status`) had no table at
  all while returning `1` and `2`. `kq replay`'s own usage text — printed *because* of a
  usage error — never named the code a usage error returns. All are now documented, and
  `scripts/check_doc_claims.py` (`make docs-check`) reads each command's real return set
  out of the AST, following helper returns, and fails when a table or a usage text
  disagrees, omits a non-zero code, or lists one the command cannot produce. Anything it
  cannot resolve is an error rather than a silent skip.

### Fixed
- `kq detector promote` exited `1` — "the request failed", the code a script retries — when the
  server answered `409`, which it uses only to refuse promoting a detector whose predicate can
  never match anything. That verdict is never worth retrying; it now exits `3`, the code the
  command already uses for a rejection on the merits, with the server's reason. `404` names the
  missing detector instead of blaming the command, and the two copies of "read the server's
  `detail`" were merged into one that handles FastAPI's validation-error list form.

### Fixed
- `GET /v1/detectors/{name}/shadow-findings` answered `200` with an empty `findings` list for a
  sensorium that was not running at all, for a detector this process had never loaded, and for a
  detector that ran and stayed quiet — and that count is what a reviewer promotes or rejects the
  candidate on. It now answers `503` when there is no engine, in a body that refuses to claim the
  detector fired nothing, and reports `watching` plus the firing ring's `capacity`/`saturated` so
  the count is not mistaken for a total. `kq detector shadow` surfaces both caveats.
  `list_detectors`, two functions above, had drawn exactly this line already.

### Fixed
- `GET /v1/events/replay/{session_id}` answered three different states with one identical
  response — HTTP 200 and a lone `data: [DONE]`: a session this process never saw, a session
  that genuinely emitted nothing, and a session whose in-memory history was lost to a restart or
  belongs to another replica. Since the endpoint is advertised for "UIs that reconnect", the
  third case rendered a real investigation as an empty one. It now answers `404` when the process
  holds no history, in a body that explicitly refuses to claim the session never ran and points
  at the durable `/v1/episodes/{id}/replay`; and the stream is prefixed with a `replay_meta`
  frame carrying `records` and `durable: false`, so a genuinely empty session says `records: 0`.

### Fixed
- an SDK caller that broke out of `stream()` early — the pattern the SDK docs themselves show —
  destroyed the only record that the stream had dropped frames: the loss warning was emitted on
  the line after the loop, which a `break` never reaches. It is now emitted from a `finally`, and
  the loss counters are exposed as `client.last_stream_stats` so a fail-closed caller can inspect
  them, which `SseStats` had always claimed was possible but never was outside the CLI.

### Fixed
- the async SDK's `health()` reported a hostname that does not resolve as "Connection refused —
  nothing is listening at …", naming the wrong cause and sending the reader to check the port
  rather than the hostname; the sync `health()` had the right answer, and a test for it, the
  whole time. Its timeout message also omitted the duration in force, and the "fast connectivity
  check" ran on the 120 s query timeout instead of 5 s. Both checks now share one classifier.

### Fixed
- `AsyncKubeQClient.stream` returned normally with zero events when the server could not be
  reached at all — a `return` from an async generator is a clean end of stream, so a connection
  failure was indistinguishable from a successful empty answer. The sync client raised, and the
  SDK docs promised both would; the async half now re-raises the last transport error. The
  "Retry behaviour" section of the SDK docs also named a retry schedule the code has never used
  and described `query()`'s error handling as `stream()`'s; both corrected and test-pinned.

### Fixed
- `kq replay` exited `1` — the code for "no such episode, or the request failed" — when the
  server reported HTTP 409, which it uses only for total truncation: the chain anchor proves the
  episode had records and none survive. The strongest tamper signal the system can emit was
  indistinguishable from a typo'd ID, and the documented `[ $? -eq 3 ]` tamper branch could not
  fire on it. It now exits `3`. Because the request is streamed, the response body was also
  never read, so `explain()` discarded every server `detail` on this path in favour of httpx's
  status line; the body is now read before `raise_for_status()`.

### Fixed
- the morning digest reported *"Quiet watch: no findings in the last 24h."* over a window in
  which the sensorium's watch queue had dropped observations. `app.detectors.perception` exists
  so that `kq findings` and `kq digest` cannot answer differently about the same window — its
  docstring says "the classification lives here once, and both surfaces read it" — but the
  shedding rule had been added to `kq findings` alone, so the classifier still returned no gaps
  for a connected-but-lossy sensorium. The queue is now part of `PerceptionState`, and
  `perception_gaps` reports the dropped count and queue high-water, so the digest degrades for
  the same reason and with the same numbers the findings surface prints.

### Fixed
- `scripts/check-file-modes.sh` reported the tracked paths it had skipped only when it had
  nothing else to say. The `note: N tracked path(s) were skipped` line lived inside the
  all-clear branch, so a run that found violations printed `checked N file(s)` with no hint that
  others were never examined, and `--fix` printed `fixed: …` and exited 0 while those paths
  stayed unknowable — a completion claim over an incomplete examination. The note is now hoisted
  above every verdict the script can reach, so a branch added later cannot omit it.

### Fixed
- `scripts/check-syntax-warnings.py` reported `syntax OK` over files it never opened. Any path
  it cannot read is skipped — correctly, since a pre-commit hook that passes a just-deleted path
  must not be turned red — but the skip was silent, so `check-syntax-warnings.py a.py b.py c.py`
  with two missing files printed `syntax OK — 1 file(s)` and exit 0, and with *all* files missing
  printed `syntax OK — 0 file(s)`: the vacuity guard added for the tracked-tree form reads
  `if not argv and checked == 0`, so it was switched off for exactly the input mode a CI hook
  uses. The exit code is unchanged (a settled contract); the gate now lists every skipped path on
  stderr, carries `(N of M skipped)` in its verdict line, and never prints the word OK over a scan
  that compiled nothing.

### Fixed
- `kq findings` printed the green `No findings` all-clear while the sensorium was dropping
  observations. The watch queue sheds the oldest observation on overflow by design, making the
  loss silent at the point it happens; `queue.shed_total` in `GET /v1/findings` is the only
  record of it and nothing read the field. The command now reports **Perception is lossy** with
  the dropped count and queue high-water — above the findings table as well, since shedding
  makes a non-empty list incomplete too — and the all-clear is now reachable only when the
  stream is connected, Prometheus is queryable *and* nothing was shed.

### Fixed
- `kq postmortem` rendered the audit-chain tamper warning and still exited `0`, while
  `kq replay` and `kq export` map the same verdict to exit 3/4/5. The cause was server-side:
  `format=markdown` returned only the rendered prose, so the verdict reached the caller as one
  of four English banners. The response now carries `chain_valid`, `chain_verified`,
  `events_lost`, `gaps` and `enrichment_failed` alongside the markdown (additive; `format=json`
  unchanged), and the command follows the documented exit-code convention. An older server that
  sends no verdict fields is reported as exit 4, not 0.

### Fixed
- **`kq config show` reported success over a config it had just called invalid.** The command
  printed `⚠ Invalid values detected:` and returned **0** — the human-readable answer and the
  machine-readable one disagreeing, and `kq config show || exit 1` in an install script or a CI
  pre-flight only reads the second. Its sibling `config set` already returned 2 for a bad key, and
  `load_config(strict=True)` already exits 2 on exactly these errors, so `show` was the odd one
  out; it now exits 2 as well, and a valid config still exits 0. Second defect in the same code:
  both `config show` and `config set` rendered `err.splitlines()[0]`. `validate_config` builds
  each message in three parts on purpose — its docstring promises "the offending value, the
  matching env var (so the user knows what to edit), and what a valid value looks like" — and the
  last two sit on lines 2 and 3, so the user saw `Invalid URL: 'not-a-url' — must start with
  http://` and never `Fix: set KUBE_Q_URL in ~/.kube-q/.env`. Both now print the whole message.
- **The autonomy demotion path treated a critical failure as *less* serious than an ordinary
  one.** `cusum_trip` in `app/autonomy/promotion_stats.py` implements ADR §4.4's fast trigger —
  2 postcondition failures within 24 h ⇒ demote one rung — but counted
  `not e.success and not e.critical`, excluding exactly the failures the two severity triggers
  exist for. Those two triggers (Sev-1/2 two-rung drop, M4 rule) are L4-scoped by the ADR's own
  wording, so below L4 nothing fast was watching, and the hysteresis band cannot cover the gap:
  a class with an earned record keeps its last-50 LCB above `θ − 0.05` through exactly two
  failures, which is why the trip exists. Measured 2026-08-24 on an L3 class with 48 clean runs
  and then two failures 12 h apart: as ordinary failures they tripped CUSUM and the class dropped
  to L2; with `critical=True` on the same two events the answer was `no demotion trigger` and the
  class kept its rung. The asymmetry now runs one way only — a critical event blocks promotion
  (`M4 > 0`) **and** forces demotion — and a property test asserts at every rung that making the
  same failures critical can never produce a better outcome. Two related fixes: the CUSUM reason
  now names critical involvement, so the audit trail cannot record a routine trip where a Sev-1
  occurred; and a `sev_attributed`/`m4_at_l4` signal reported below L4 no longer returns
  `no demotion trigger` — the rung is still unchanged, because the ADR scopes those triggers to
  L4, but the decision says the signal was received and not applicable rather than reporting a
  quiet class. **Latent, not live:** nothing writes `promotion_outcomes` yet, so this code does
  not currently govern any cluster; it is the function that will decide when to take authority
  away.
- **A capped memory section read to the model as the operator's complete state.** The pinned
  context in `app/db/memory_store.py` is assembled from four bounded queries — 8 operator
  preferences, 5 failure patterns, 3 session notes, 3 past RCA outcomes — so the block stays
  inside its ~500-token budget. The bounds are deliberate; the silence about them was the defect.
  Measured 2026-08-24 with 12 explicit preferences stored: the prompt carried 8, under a header
  reading `## Operator Preferences (remembered)`, and `NEVER drain node-07, it hosts the license
  server` was one of the four dropped with no marker of any kind. Read literally — the only way a
  model can read it — that block states the instruction does not exist. This module already
  refuses the same mistake one axis over (`_partial_failure_notice` exists because *"a missing
  section must not read as an empty one"*); a capped section must not read as a complete one for
  the same reason. Each section now queries one row past its cap: the extra row coming back is
  proof more exist and the section closes with a line saying so, and it not coming back is proof
  they do not and the section stays silent — one query, not two, which is also why the notice
  never states a count nobody measured. Separately, a stored remediation cut to 160 characters for
  the same budget is now marked `…[fix truncated, not the whole command]`; a `kubectl` command cut
  mid-flag reads as a complete command otherwise.
- **An event the watcher could not date bypassed the staleness filter, silently.**
  `app/sensorium/k8s_watcher.py` drops replayed `Event` history so a reconnect does not re-fire
  detectors on minutes-old warnings, but `_event_timestamp` returns `None` both for an event with
  no timestamp and for one whose timestamp it cannot parse, and the call site skipped the
  comparison entirely on `None`. Measured 2026-08-24: a 45-minute-old `OOMKilling` with no
  `lastTimestamp`, and the same event with an RFC1123 timestamp, both fired a detector as if they
  had just happened. Failing open is kept — refusing a real incident over a formatting quirk is
  the worse error for a watchtower — but it is no longer silent: one `k8s_watcher: … the
  event-staleness filter cannot run` warning per distinct reason per connection, re-armed on
  every (re)connect, so "the filter passed this event" and "the filter did not run" are now
  distinguishable in the log. Second defect in the same function, and a missed detection rather
  than a cosmetic one: a timestamp with no timezone suffix parsed to a naive `datetime`, whose
  `.timestamp()` reads it as **local** time. On a CEST host a real `OOMKilling` from 10 minutes
  ago was dated 110 minutes old and dropped by a watch that had been connected for 20 minutes.
  Naive timestamps are now dated as UTC, which is what Kubernetes emits.
- **A detector tuning knob set to `0` was read as "not set".** Every trend-predicate knob in
  `parse_detect_block` was read as `entry.get(key) or default`, which cannot distinguish an absent
  key from an explicit zero. Measured 2026-08-24: a hand-authored predicate with
  `window_minutes: 0, projection_horizon_minutes: 0, fire_if_eta_within_minutes: 0, min_r2: 0`
  loaded as `30 / 120 / 30 / 0.5` — every value replaced, with nothing logged. The two cases pull
  opposite ways, so a single rule does not cover them. `min_r2: 0` is a **real setting**: the
  projection gates on `r2 < min_r2`, so zero deliberately disables the fit-quality check, and
  restoring `0.5` makes the detector quieter than its author wrote it — a false negative, the
  failure nobody notices. It is now honoured silently. The three interval knobs at zero instead
  produce a predicate the engine can never fire — a zero window fits no samples, and an ETA of
  zero or less is dropped before the `fire_if_eta_within_minutes` test — which is the same
  dead-detector trap this module already refuses to ship for promql-only blocks; those now fall
  back **with a warning** naming the field, the authored value, the specific reason it cannot
  work, and that the detector which loaded is not the one that was written. `debounce_seconds`
  keeps `0` (the documented "fire immediately") and refuses a negative value, which would disable
  debouncing rather than shorten it. A non-numeric value and an out-of-range `min_r2` also warn
  instead of failing silently; an absent key and an explicit `null` still take the default
  quietly. A corpus guard asserts all 24 authored knob values across the 20 shipped playbook
  detect blocks parse to exactly what the YAML says. 21 new tests, 9/9 mutants killed.
- **A container with no memory limit was reported as "within healthy bands".** `rightsizing.recommend()`
  computed `peak / limit if limit else 0.0`. A missing limit is not a ratio of zero — and zero is the
  most reassuring value on that scale, below every threshold the function tests. So an **unbounded**
  container, the highest-risk memory configuration there is, fell through every branch into the
  all-clear; the only thing stopping it also being advised to *shrink* was an unexplained `0 < ratio`
  guard, which is why the bug presented as silence rather than a wrong action. Measured 2026-08-24,
  three distinct states returned the identical sentence at the identical confidence (`is_noop=True`,
  `0.5`): no limit with a 900 MB peak; no limit and no observation; a limit with no observation.
  `ratio` is now `None` rather than `0.0` when there is no limit, and an unbounded container with an
  observed peak yields `set_memory_limit` sized at ~1.25× peak with a rationale naming that it can
  evict its node. A container with no peak observation is not assessed at all: it says so, and carries
  confidence `0.0` rather than the `0.5` that used to assert a considered judgement of no change. The
  `Recommendation` dataclass gained `assessed`, because `is_noop` alone cannot tell "judged and
  healthy" from "never observed" and a no-op reads as an all-clear. An OOMKill with no limit now says
  *set* rather than *raise*. The over-provisioned boundary is unchanged. **Note:** `recommend()` has
  no production caller yet (v5 P4 groundwork, `KI_V5_RIGHTSIZING` default-off), so this was a latent
  defect rather than one users were seeing. 16 new tests, 9/9 mutants killed.
- **A plan step whose tool calls all failed was ticked green.** `gather_tools` advanced the
  investigation plan with `model_copy(update={"status": "done"})` unconditionally, once the tool
  batch returned. But `_run_one` deliberately converts a tool exception into an ordinary
  `ToolMessage` ("Tool error: …") so the model can read and react to it, and an unrecognised tool
  name into "Unknown tool: …" — both return normally. So the batch returning was the entire test,
  and it returns identically whether every tool worked or every tool failed. `kq` renders `done`
  as a green `✓`. Measured 2026-08-24: a two-call batch where one tool raised `connection refused`
  and the other did not exist produced `status='done'` on the step "Check pod events" — the
  investigation looked like it was progressing while it gathered nothing at all. `PlanStep.status`
  gained a `failed` state (`✗`, red, defined in both the coloured and neutral themes), used when
  every call in the batch failed; a partly-successful batch stays `done` because it did gather
  evidence. Either way the failures are now named in a server-side warning with the tool and the
  reason. The cursor still advances on failure — a plan that stops advancing hangs the live view —
  and an empty batch does not invent a failure. 14 new tests, 9/9 mutants killed.
- **Deleting an episode's newest events left a chain that verified as intact.** The flight
  recorder's `verify_chain` recomputes `prev_hash`/`seq` links, which catches an edit, a reorder
  and an interior delete. It cannot catch a **truncation**: remove the last rows and what remains
  is a shorter, perfectly valid chain. Measured 2026-08-24 — a 9-event episode with its newest 3
  events deleted still verified, and `render_markdown` printed `✅ Audit chain verified intact —
  every event below is tamper-evident` over the shortened record; the controls (front truncation,
  edited payload, reorder) all correctly failed, so the verifier was sound and the *question* was
  too narrow. The codebase had already named this exact hole and closed it one module over —
  `memory_chain_head` exists so a shorter memory-audit chain contradicts its anchor — but the
  chain that renders a banner to a human had no anchor at all. Added `decision_log_head`, written
  alongside the events on every successful flush (in its own `try`, so a head that fails to write
  can never turn into a lost batch). `verify_episode()` is now the full verdict — links **and**
  anchor — and both callers use it: the postmortem banner and `GET /v1/episodes/{id}/replay`.
  Three further consequences: an append after a truncation continues past the head rather than
  the surviving tail, so it cannot heal the hole and erase the evidence; an episode with an anchor
  but no surviving rows is reported as *"every event has been removed"* instead of *"no recorded
  events"*; and that episode's replay answers **409** rather than the `404` that would launder a
  total truncation into "this never existed". A genuinely empty episode, an episode older than
  the anchor, an unreadable head and a head whose row cannot be parsed are all still reported as
  untampered — the last two with a warning naming that truncation is currently undetectable.
  20 new tests, 10/10 mutants killed.
- **The adversarial reviewer was handed a silently truncated world.** `_gathered_evidence` in
  `cortex/graph.py` concatenates this turn's tool and fan-out output and ended in a bare `[:8000]`.
  That text is the reviewer's *entire* input — it sees the claim and this, and is asked which
  statements the evidence does not support, under a standing instruction to treat "not found"
  conclusions with suspicion. A silent slice therefore does not merely lose evidence, it
  manufactures the reviewer's grounds for objecting. Measured 2026-08-24 on a six-read gather of
  8,749 characters: the decisive `Reason: OOMKilled / Limits: memory 128Mi` lines fell **749
  characters past the cut**, and what the reviewer received ended mid-row at `web-022   1/1` — a
  partial line that reads as a complete one. Nothing in the text said any of it was missing, so the
  same fix that made a dead reviewer announce itself could still be undone by an over-budget
  gather. The cut is now line-aligned — unless the nearest line break would waste more than half
  the budget, in which case the character cut stands — and carries an explicit marker naming how
  many of how many characters were dropped, in the same terms `run_kubectl` already uses for
  partial tool output: absence of a fact from that text is not evidence that it was not observed.
  Under-budget evidence is returned untouched. Rider on the same class: the stored-detector skip
  summary added yesterday capped its detail at `skipped[:5]` without saying so, and now appends
  `(+N more)`. `_bound_tool_content` is deliberately unchanged — its silent chop is a documented
  v4 default with a never-silent v5 alternative behind a flag, and switching the default is an
  owner call. 16 new tests, 9/9 mutants killed.
- **A postmortem that could not read a section rendered exactly like one that had nothing to
  say.** `build_postmortem` enriches the deterministic timeline from two best-effort sources, and
  both returned `None` on failure — the same `None` that legitimately means "nothing to add".
  `_fetch_episode_meta` supplies **root cause and outcome** from the L1 episode store and returned
  `None` both for "no row for this episode" (the investigation never concluded — a finding about
  the incident) and for "the query raised" (a revoked grant — a fact about us); the renderer prints
  the section under `if pm["root_cause"]:`, so both silently omitted it. `synthesize_narrative` did
  the same across "feature off", "nothing to narrate" and "the model call failed". Measured
  2026-08-24 against a `fetchrow` that raises, with "no row" as the control: the two documents were
  **byte-identical at 528 bytes**, both carrying `✅ Audit chain verified intact`. That banner is a
  claim about the records; it sat above a document silently missing the section a reader opens a
  postmortem for. Both enrichments now raise on failure, `build_postmortem` records what could not
  be read, and `render_markdown` prints a **POSTMORTEM INCOMPLETE** banner immediately below the
  chain verdict naming each one — the same three-state treatment the chain and gap banners already
  had. A missing episode row and a disabled narrative are not failures and print nothing. The
  timeline, the chain verdict and every recorded fact are unaffected. 16 new tests, 9/9 mutants
  killed.
- **A failed detector read disarmed the live watchtower.** `load_db_detectors` documented itself
  as *"fail-open (no pool / query error → empty tuples)"*, which is right for a read — but its only
  caller does not read, it **assigns**: `_engine.detectors = tuple(load_detectors()) + active`. So
  the empty tuple a failed query returned silently replaced every promoted detector in the running
  engine. Measured 2026-08-24 on the refresh loop: 3 promoted detectors loaded (23 total), the next
  refresh's query raises, engine drops to 20 — disarmed until the DB came back
  `DB_DETECTOR_REFRESH_SECONDS` later. Nothing reported the consequence, because the refresh's own
  summary was gated on `if active or shadow:` — false for exactly the case where coverage had just
  been removed. The correct handler already existed in `_refresh_db_detectors` (`except …: return`,
  which keeps the loaded set) and was unreachable; `load_db_detectors` now raises
  `DetectorStoreUnavailable` — the exception this subsystem already had for this distinction,
  previously applied only to the read path — so a failed refresh keeps what is loaded and says what
  it kept. An empty result from a *successful* query still applies, because that is a human
  demoting detectors. Two further fixes ride along: a counts change is now logged **including a
  drop to zero**, and the four ways a stored row was discarded in complete silence (unparseable
  JSON, not a detect block, would not compile, compiled to nothing) are counted and named, so a
  table of malformed rows is no longer indistinguishable from an empty table. 17 new tests, 9/9
  mutants killed.
- **A memory-consolidation worker in which every pass failed reported exactly what an idle one
  reports.** Each of the eight passes fails safe to a `0` counter — correct — but `0` is also what
  a pass returns when it ran and found nothing to do, so the dict `run_consolidation_once` returns
  (documented "for tests/digest") could not tell the two apart. Measured 2026-08-24 against a pool
  whose every statement raises, with a healthy-but-idle pool as the control: both produced
  `{"backfilled": 0, "stale_edges_closed": 0, "detector_candidates": 0, "prefs_inferred": 0,
  "prefs_forgotten": 0}` — byte-identical. The passes do each log their own `WARNING`, so this was
  never a silent outage in the log; it was a silent outage in the machine-readable result. And
  because the worker's summary line was gated on `if any(stats.values())`, the one line that could
  have said *the pass completed, here is what it did* fired for neither state, leaving an operator
  to correlate five `WARNING`s from three loggers every 600 seconds. A new `app.memory.pass_health`
  register is written from each guard; the worker now sets `failed_passes` in the counters and logs
  `consolidation_pass INCOMPLETE — N of M passes failed [...]` at `WARNING`, naming each pass and
  its reason. The failure discipline is unchanged — a pass that raises still never stops the loop —
  and a genuinely idle pass still logs nothing. 25 new tests, 11/11 mutants killed.
- **A dead RCA reviewer and an approving one produced the same answer.** With
  `KI_V5_VERIFY_LADDER` on, `render_review_note` returned the empty string when the adversarial
  reviewer had `errored=True`, and `cortex/graph.py` appends the note only when it is non-empty —
  so a reviewer that timed out, crashed or replied in prose left the user's answer **byte-identical
  to a verified one**. The only signal that verification happened was the absence of a caveat,
  which is also what a total instrument outage looks like. Failing open is the documented and
  correct behaviour; failing *silent* was not part of it. A third state now renders
  **"⚠ Verification NOT PERFORMED … treat this answer as unverified"**, which states a fact about
  the reviewer without contradicting the answer. Second defect, same block: the confidence line was
  gated on `if review.confidence:`, so it vanished for exactly **0.0** — the reviewer declaring no
  confidence in the RCA at all — leaving the caveat quietest at its own maximum alarm, and `0.0`
  doubled as the fallback for an absent or unparseable field. `confidence` is now `float | None`
  and is always stated. 23 new tests, 9/9 mutants killed. Also repairs the *Workflow nodes* table
  in `docs/agent-behaviors.md`, which an earlier entry in this release had split in two.
- **The responder brief displayed its confidence only when the number was reassuring.**
  `render_brief` gated the line on `if brief.confidence:`, so a brief rating itself **0.05**
  printed *"RCA confidence: 5%"* while one rating itself **0.0** — the least confident it can be —
  printed nothing at all: the only brief carrying no caveat was the worst one. The same truthiness
  test collapsed three states into that silence (the model said zero, omitted the field, or
  answered `"high"`). This is text appended to the investigation answer an on-call responder reads
  (`cortex/graph.py`, behind `KI_V5_ESCALATION_BRIEFS`), so a signal that reached the dataclass and
  not the markdown had not been delivered. `confidence` is now `float | None` — absent and zero are
  different answers — and a confidence is always stated, including when there is none to state.
  Two corrections ride along: the line was labelled *RCA confidence* when the value is the brief
  writer's confidence in the plan it just wrote, and `fell_back` existed on the dataclass but
  nothing rendered it, so a fallback brief was visually identical to a real one. The heading now
  says **FALLBACK**. 17 new tests, 11/11 mutants killed.
- **A security auto-repair that never ran reported "no change to propose".** `propose_fix` fails
  safe by returning the ORIGINAL manifest — correct — but that was the *whole* answer, so a repair
  that failed was byte-identical to one that found nothing to change. Measured 2026-08-24 end to
  end: an LLM exception, an empty response and a refusal in prose all reached `open_pr` as
  *"no change to propose (fix is a no-op)"*, the sentence a compliant manifest earns, while the
  misconfig stood untouched. `propose_fix` now returns a `RepairProposal(manifest, repaired,
  reason)`, the reason travels with the `FixPR`, and `open_pr` says *"no fix was produced, so the
  violation is UNRESOLVED — … This is NOT a statement that the manifest complies."*
- **The same path opened a pull request for a missing newline.** `_strip_fences` ends in `.strip()`,
  so a model that **echoed the manifest back** — its way of saying nothing needs changing — came
  back one trailing newline shorter than it went in. That is a real diff (one line removed, the
  same line added), `is_noop` was `False`, and `open_pr` pushed a branch and opened a PR titled as
  a security fix whose entire content was the newline. `unified_diff` now normalises trailing
  newlines on both sides; trailing *content* still diffs. Across both fixes, five repair outcomes
  that previously produced two PRs and one indistinguishable message now produce one PR and four
  distinct messages. 14 new tests, 11/11 mutants killed.
- **`rolled_back` was reported for four rollbacks that never happened.** `execute_transactional`
  issued the rollback command and discarded its result. Measured 2026-08-24: a rollback refused by
  KubeIntellect (`[Protected] …`), rejected by the API server (`[kubectl exited 1] Error from
  server (Forbidden) …`), never delivered (`The connection to the server … was refused`), or
  simply silent (`(no output)`) **all** returned `rolled_back` — four ways to leave the cluster in
  exactly the half-applied state this executor exists to prevent, while telling the caller and the
  audit trail that it had been restored. That is the one status a trust plane must be able to
  believe, because it is what says no operator needs to look. The rollback result is now read
  through the same classifier as the apply, with the apply side's own vocabulary mirrored:
  `rollback_refused`, `rollback_failed`, and `rollback_unconfirmed` for silence — nothing said it
  failed, but nothing said it ran. `rolled_back` is now only returned when the rollback is
  confirmed. 15 new tests, 9/9 mutants killed.
- **A complete `kubectl diff`, and an authoritative `can-i: no`, were reported as failures.**
  Second consequence of stating a non-zero exit (see the two entries below): `kubectl diff` exits
  **1 when it finds differences** — 0 means none, >1 means the tool failed — and `kubectl auth
  can-i` exits **1 when the answer is `no`**. Both are documented and both are the ordinary
  outcome. Measured 2026-08-24: a real diff came back as `[kubectl exited 1] (kubectl wrote
  nothing to stderr)` with the diff itself demoted into the block that says *"it may be partial,
  and absence from it is NOT evidence"*, and the ACI `diff_change` verb returned that as
  `ok=True`. `can-i` was left asymmetric — a clean `yes` against a `no` the agent had been warned
  about — which is unusable for reasoning about its own permissions. Both verbs now return the
  plain result; every other verb, and any exit code above the documented one, is still reported as
  a failure. 24 new tests, 8/8 mutants killed.
- **A regression introduced the same day: every `kubectl` failure classified as a normal result.**
  `run_kubectl` was changed on 2026-08-24 to state a non-zero exit (`[kubectl exited 1] Error from
  server (Forbidden): …`), which is strictly more information — but `aci/kubectl_output.py`
  classifies on **line prefixes**, and the new marker sits in front of kubectl's own line.
  Measured by driving the real tool: an RBAC `Forbidden`, a refused API server and a bad local
  path all returned `classify_output() == "ok"` with `reached_cluster() == True`, and the health
  oracle answered *"deployment 'web' not found in 'prod'"* about a namespace it never read — the
  exact verdict `postcondition.py` was written on 2026-08-20 to stop it giving. `execute_transactional`
  reads that `met=False` as a failed mitigation and **rolls back**, so an instrument outage would
  have become a live mutation. The classifier now recognises the marker as the authoritative
  failure signal, and strips it before the reachability check so a server *rejection* is still
  distinguished from never reaching the server. Every ACI test built its input by hand, which is
  how the tool and its classifier were free to drift; the new suite drives `run_kubectl` itself.
  21 new tests, 9/10 mutants killed (the tenth proven equivalent — `re.match` is anchored with or
  without `^`, verified over 336 constructed lines).
- **A successful `helm list` and an unreachable cluster returned the same string.** `run_helm`
  merged `stdout + stderr` before anything decided what the output was, so helm's routine
  `WARNING: Kubernetes configuration file is group-readable` — printed whenever `~/.kube/config`
  is group-readable, which is the common case — became part of the document handed to
  `json.loads`. Measured 2026-08-24 against a fake `helm` on `PATH`: a `helm list -A -o json`
  that **succeeded** and listed one release, and one that failed with
  `Error: Kubernetes cluster unreachable`, both returned *"[Protected] This release listing could
  not be parsed, so releases in protected namespaces could not be removed from it."* — the
  release deleted, the error deleted, and *protection* named as the cause of neither. Separately,
  `proc.returncode` reached the answer only when helm printed nothing at all, so every other
  failure was returned as though it were the result. stdout and stderr are now kept apart: the
  namespace and protected-kind filters see stdout only, a non-zero exit returns
  `[helm exited N] <stderr>` (matching `run_kubectl`), output printed before an error is labelled
  *"absence from it is NOT evidence"*, and a zero-exit stderr warning is appended in a block that
  says it is a warning about the client. 25 new tests, 12/12 mutants killed.
- **`kubectl` failed and the agent was told the cluster was empty.** Two independent paths in
  `run_kubectl` turned a failure into an answer, both measured 2026-08-24 by driving
  `subprocess.run`. (1) The pipe emulator ran over a merged `stdout or stderr` string, so an RBAC
  `Forbidden` piped through `grep Running` returned `(no matching lines)` — **byte-for-byte the
  same string** a successful listing with nothing running produces, and the error hint the module
  had just attached ("Insufficient RBAC permissions") was filtered away with it. A real shell never
  pipes stderr, so this was not even faithful emulation. (2) `stdout or stderr` keeps stdout
  whenever it has anything, so `kubectl get pods -A` with one namespace forbidden returned a
  complete-looking listing with no sign a namespace had been denied. Both land on the evidence path
  the grounded-diagnosis claim rests on — the model reads a tool result as observation. The pipe
  and the blocked-namespace filters now see stdout only; a non-zero exit is stated outright
  (`[kubectl exited N]`, and `(kubectl wrote nothing to stderr)` when there is no message); and a
  partial listing is returned alongside the error, marked as possibly partial. The success path is
  byte-for-byte unchanged.
- **The postmortem raised a tamper alarm over records nobody had read.** `chain_valid` is a
  `bool` carrying three outcomes, and `render_markdown` read `false` as "the hashes disagree".
  Measured 2026-08-24 on both paths that never verify anything — a recorder that could not be
  reached, and an episode with no recorded events — each printed *"**AUDIT CHAIN BROKEN** — the
  recorded events may have been altered"*. On the unreadable path that also contradicted the same
  postmortem's own summary line, which says the recorder could not be read. A tamper warning is
  only worth printing if it never fires when nothing was tampered with, so the false one was
  costing the real one its meaning. The postmortem now carries `chain_verified`, and the markdown
  render has a third banner — **AUDIT CHAIN NOT VERIFIED** — with the reason underneath; this is
  the same distinction `kq replay` already draws with its exit code `4`. `chain_valid` keeps its
  type and its existing meaning, and the **RECORD INCOMPLETE** banner is unchanged: intact,
  complete and verified are three separate claims.
- **A flag can be ON, wired, and doing nothing — and only half of that was reported.**
  `set_but_unwired_flags` answers "the operator set a switch no code reads". It does not answer the
  case one level out: the code *does* read the flag, but the subsystem it lives in is dead.
  Measured 2026-08-24 with `MEMORY_HIERARCHY_ENABLED=true` and the hierarchy unable to reach
  Postgres — `/healthz` returned `memory: {enabled: false, state: "unavailable"}` and
  `experimental_flags: ["MEMORY_HIERARCHY_ENABLED"]` **in the same response**, and `/v1/v5/status`,
  whose docstring promises "which v5 slices are active", reported the flag active and carried no
  way to see the outage at all. Every `MEMORY_*` slice runs inside that hierarchy, so up to ten
  flags could read as active while none could act — and the commonest case needs no outage at all:
  turning a slice on while `MEMORY_HIERARCHY_ENABLED` is `false` leaves it with no hierarchy to run
  inside. Both endpoints now carry `degraded_experimental_flags`, and `/v1/v5/status` carries the
  `memory` state and reason it previously could not show. The flags deliberately **stay** in
  `active_flags` / `experimental_flags`: that list is rollout identity — which arm the pod was
  configured as — and a Postgres blip must not make it flap.
- **The CLI half of the same lie: `kq findings` printed one word for all four absences.**
  The server-side fix below gave `GET /v1/findings` a `sensorium_reason` field, but `kq findings`
  — the interface `docs/cli-reference.md` tells operators to use — still read only the coarse
  `sensorium` value and printed *"Sensorium is disabled on this server."* Feeding it the four real
  sentences produced **one** distinct output, so the classification existed and reached nobody. The
  command now prints the server's sentence, and says it does not know when talking to a server that
  predates the field. Exit codes and every other `sensorium` state are unchanged.
- **`sensorium: disabled` named two causes as fact, for four unrelated situations.** Four paths
  end at "no detector engine on this replica", and `perception_gaps` described every one of them
  with the same sentence — *"the sensorium is not running (SENSORIUM_ENABLED=false, or no
  compiled detectors loaded)"*. Driving each real path and reading the output produced that
  identical string four times, and for two of the four it is **false**: a `start_sensorium` that
  **raised** (the lifespan catches every exception and logs one `WARNING`, correctly, so that
  perception failing never costs availability) was reported to the operator as a configuration
  choice rather than the outage it is; and a **leader-election standby**, which serves the API
  normally and watches nothing by design, was described with two causes that were both untrue —
  so in a two-replica deployment half of all `/v1/findings` responses and every digest built on a
  standby made a healthy cluster read as an unmonitored one. Each path that can leave the engine
  absent now records which one it was; `PerceptionState` carries `sensorium_reason`, empty while
  perceiving; a failed start says it is an outage and carries the exception message; a standby
  says another replica is perceiving and points at it; and `GET /v1/findings` returns the field on
  both of its branches. The coarse `sensorium` values are unchanged, so existing clients and
  `kq findings` are unaffected.
- **The flight recorder's own invariant did not cover its largest loss mode.** The module's
  stated guarantee — *"every loss is carried forward and written **into the chain** as a
  `recorder_gap` record on the next successful flush"* — is what makes the tamper-evidence claim
  meaningful, because *intact* and *complete* are different properties. But that machinery hangs
  entirely off a flush, and `init_recorder` gave up after one failed connect: no pool, no queue,
  no drain task, no retry. `record()` then began `if _queue is None: return`, so a recorder that
  never started dropped every event with no marker, no counter and no way back, for the life of
  the process. Measured with the mid-flight loss as the control: three events against a
  never-started recorder left `_pending_gaps` empty, where the same loss mid-flight leaves
  `{'ep': (1, reason)}`. Nothing reported it either — `/healthz` carried `audit` and `memory` but
  no recorder field, and the module exported no status accessor; the read path was already honest
  (`fetch_episode` raises `RecorderUnavailable`) but only for someone who already suspected it and
  asked about a specific episode. The pool is now retried every 30 seconds and reconnecting
  finishes startup properly; a loss with no queue is carried exactly like a failed flush, so the
  outage is written into the chain once recording resumes, before the events that followed it;
  the per-episode gap ledger is bounded with the overflow still counted; and `recorder_status()`
  — `ready` / `flag` / `sqlite` / `unavailable`, with the reason and `lost_while_down` — is
  reported on `/healthz`. Configuration-off states carry nothing (there is no chain to be honest
  about) and skipped token frames are not losses.
- **A server whose memory never started looked like a cluster where nothing had happened.**
  `init_memory` caught every connection error, logged one `WARNING`, set `_pool = None` and
  returned — **before starting any background task**, so nothing was left running that could try
  again. A pod scheduled before Postgres accepts connections (the ordinary rollout case) then ran
  for its whole life with no episodes, no knowledge graph, no consolidation, no preference
  learning, no promotion and no prospective recheck, and only a restart could recover it.
  Measured against a refused connect, with the healthy case as the control: `memory_active()`
  `False` vs `True`, background tasks **0 vs 3**, observation queue `None` vs a live queue — and
  every sensorium observation discarded by a silent `return`. Neither `/healthz` nor `/v5/status`
  carried any memory field, so an empty knowledge graph was indistinguishable from a quiet
  cluster. The pool is now retried every 30 seconds by a reconnect loop that completes startup
  properly when Postgres arrives (the previous code could not, because the wiring lived inline in
  `init_memory`); `memory_status()` reports `ready` / `flag` / `sqlite` / `unavailable` with the
  reason and a count of discarded observations — including the full-queue drop, which used to be
  a bare `pass`; and `/healthz` carries it next to `leader` and `audit`. The first discarded
  observation and every thousandth after it warn. `/healthz` still checks nothing and still
  answers `200` while memory is down — a pod with no memory is degraded, not wedged.
- **A server that audited nothing reported that it was fine.** `init_audit_pool` caught every
  connection error, logged one `WARNING`, and left `_pool = None` — with no retry, so the pod
  being scheduled before Postgres accepts connections (the ordinary case during a rollout)
  disabled the audit log for the whole life of the process, and only a restart could fix it.
  Measured with a refused connect: three admin requests dropped with no exception and no second
  log line; `/healthz` answered `status: "ok"` with no field about the audit log at all;
  `request_log` was empty, which is indistinguishable from a server that took no traffic; and
  the module exported no accessor to ask with. Meanwhile `docs/operations.md` said *"Every API
  request is recorded fire-and-forget in `request_log`"*, and `app/core/leader.py` justified the
  advisory-lock design on the premise that *"`init_audit_pool` exits the process when [Postgres]
  is unreachable"* — it never did, which also made `main.py`'s `sys.exit(1)` around it
  unreachable. The pool is now retried from the write path (throttled to 30s, off the request
  path), so a startup race heals itself; `audit_status()` reports `ready` / `sqlite` /
  `unavailable` with the reason and a count of unrecorded requests; `/healthz` carries it next
  to `leader`; and the first dropped request and every hundredth after it warn, so the outage
  does not go silent once the startup log has scrolled away. `/healthz` still checks nothing and
  still answers `200` while the audit log is down — an unaudited pod is degraded, not wedged.
  The false claim in `leader.py` is corrected and the two disable paths are documented.
- **"The flight recorder is off" was answered as "that episode does not exist".** `fetch_episode`
  returned `[]` for three unrelated states — the episode genuinely has no rows, the recorder was
  never started, and the query failed — and `GET /v1/episodes/{id}/replay` turned all three into
  `404 no recorded episode '<id>'`, the one answer that is a positive claim about the world.
  Measured with the recorder's pool unset, a real episode id came back 404, and `kq replay`
  rendered it as *"No recorded episode"* with exit 1. On the audit surface that is the most
  expensive wrong answer available: an operator asking for the decision log of the incident they
  are living through is told it was never recorded. `fetch_episode` now raises
  `RecorderUnavailable` for the two unreadable states, the endpoint answers **503** with a detail
  saying it is *not* a statement about the episode, the grounded postmortem carries
  `recorder_available` and stops sharing one sentence (*"No recorded events for this episode (or
  recorder unavailable)"*) with the genuinely-empty case, and `kq replay` / `kq export` no longer
  restate either as an absence. A `404` for an episode that truly has no rows is unchanged.
- **One failing memory section read as that section being empty.** `load_memory_context`
  already raised `MemoryStoreUnavailable` for a total outage — because `""` means both "nothing
  stored" and "could not look", and `memory_loader` turns that into a block telling the model
  *"this is not the same as there being none"*. Four section loaders defeated it one level in:
  each wrapped its own query in `try/except: return []`, two silently and two at `logger.debug`,
  which a default deployment never emits. Reproduced with a `user_prefs` query raising
  `column "confidence" does not exist` (a half-applied migration) and the other three sections
  healthy: the coordinator received a context that reads as complete, with no notice, no
  exception and nothing above INFO in the log — so the operator's stored preferences, which is
  where "never restart pods in prod" lives, were simply absent. Sections are now loaded
  individually so one bad query still costs only itself, but any that fails is logged at
  `WARNING` and named in a `## Memory partially unavailable` block, placed **first** so the
  context budget cannot truncate it away. A section with no rows is still not a failure.
- **An unreadable `kq` history store was rendered as an empty one.** `kube_q.cli.store` swallows
  every `sqlite3.Error` by design — a broken local cache must not crash the REPL — but each read
  then returns `[]`, which is also what a fresh install returns. Measured against a
  `~/.kube-q/history.db` holding one line of text, `kq --list` printed *"No sessions found."* and
  exited 0, with nothing on stderr: the `_logger.warning` meant to say otherwise goes to a logger
  with no stderr handler outside `--debug`. A user with a hundred sessions was told they had none,
  and never learned that deleting one file would bring them all back. The store now records why
  the last operation failed (cleared on every new one, so a stale error can never be printed beside
  a healthy empty result), and every empty-result surface asks first — `kq --list`, `kq --search`,
  and the REPL's `/list`, `/search`, `/branches` and resume picker. A genuinely empty store still
  prints the plain line. `docs/session-history.md` documents the message, the causes and the fix,
  and its schema-version row is corrected from v3 to the v4 the code has been writing.
- **`kubeintellect status` always exited 0, however red the board was.** It is documented as a
  "Health dashboard for every component" and is the obvious thing to put in a Makefile, a
  container healthcheck or `kubeintellect status && kubeintellect serve` — but measured in a
  clean HOME it printed four `✗` rows (missing config file, no LLM key, unreachable PostgreSQL,
  missing kubeconfig) and still returned success, so none of those uses could ever fail. It now
  exits **1** when any row is `✗` or the configuration has an error-level issue, and names what
  is broken on the last line. A `-` row means *not configured* — a choice, not a fault — and
  never fails, so nobody running without Prometheus goes red for it.
- **`kq` could not read the kubeconfig kubectl writes.** The fallback context scan — used when
  `kubectl` is absent, so the only source there is — matched only the `- name:` ordering, while
  kubectl writes `- context:` with `name:` on the next line. A file with two contexts parsed as
  zero: no `/context` tab-completion and "No kubectl contexts found (is kubectl installed and is
  `~/.kube/config` valid?)" for a kubeconfig that was perfectly valid. The scan now keys off an
  entry's key positions rather than which key carries the dash, and still refuses `users:` names
  and the `name:` inside a nested `extensions:` entry.
- **`kq`'s `/plugins` told you to install plugins you had already installed.** It read only the
  registry, so a plugins directory full of files that had all failed to import produced *"No
  plugins loaded. Drop Python files into `~/.kube-q/plugins/`…"*. It now reports the failures with
  their reasons whether or not anything loaded. The `kq` plugin system is also documented for the
  first time in `docs/cli-reference.md` — directory, load order, the `register()` contract,
  `PluginContext`, the trust warning and the failure behaviour.
- **A `kq` plugin that failed to import was silent, and a half-loaded one still advertised its
  commands.** `load_plugins` claimed its errors were "printed as a dim warning"; the `kube_q`
  logger has no stderr handler outside `--debug`, so with start-up logging configured a broken
  plugin produced empty stdout *and* empty stderr — the file was indistinguishable from one never
  installed. Worse, `register` runs at import time, so a module that registered a command and
  then raised left that command listed by `/plugins`, offered by tab-completion and dispatchable
  while the banner said the plugin had not loaded. Failures now reach the screen via `plugins.load_failures()`, and a failed module's
  registrations are rolled back and its entry removed from `sys.modules`.
- **`kq` ignored profiles and project-local `.env` files.** `load_config` documents four
  layers — `~/.kube-q/.env`, then the active profile, then `./.env`, then the shell — but the
  loader copied each file straight into `os.environ` and skipped any key already present, so the
  *first* file read won. A profile and a directory-local `./.env` were therefore silent no-ops for
  every key `~/.kube-q/.env` already set, which is exactly what `kq config set` writes there
  (`url`, `api_key`, `context`): `kq config profile` could not repoint a configured client at
  another cluster. `kq config show`'s **Source** column had the matching half — it derived the
  source by comparing the environment against `~/.kube-q/.env`, which cannot see a profile or a
  local `.env`, and labelled both "shell env" while `docs/cli-reference.md` promises the column
  names them. The layers are now merged explicitly, a shell export still outranks every file, and
  the column reports the layer that actually won.
- **Four commands printed a `✓` for a subprocess whose result they never read.**
  `subprocess.run(..., check=False)` with the result discarded is silent by construction: the
  command fails, Python runs the next line, and the next line was the success message.
  `kubeintellect kind-setup` printed a five-row table of RCA scenarios kubectl had refused to
  create and "Sample pods deployed" over an empty `demo` namespace; `service uninstall` printed
  "Service removed" while the server was still running; and `kubeintellect set` printed
  "service restarted to apply changes" for a restart systemd never performed — the only signal a
  user gets that changed configuration reached the running server. All four now check the return
  code and print what the tool actually said. A new AST guard
  (`tests/test_no_unchecked_subprocess_success.py`) fails the suite on any new discarded result
  in `app/` unless it is listed with the reason it is safe; the eight that remain are
  fire-and-forget setup steps and the `service start|stop|status|logs` pass-throughs.
- **`kubeintellect service install` reported a service systemd had refused.** Both `systemctl`
  calls ran with the return code discarded and stderr swallowed, then the caller printed
  "Service installed — server will start automatically on login" unconditionally. Over SSH with
  no login session `systemctl --user` answers `Failed to connect to bus` and enables nothing. The
  `init` wizard was worse: it printed the same ✓, opened `kq` and returned, skipping the fallback
  that starts a server for the session — so a first run ended at a `kq` prompt with nothing behind
  it. The installer now reports what systemd actually did, in systemd's words, with the remedy,
  and exits non-zero; the wizard falls through to the fallback instead of returning. The unit also
  uses `EnvironmentFile=-`, so running `service install` before `init` no longer produces a unit
  that cannot start.
- **Four shipped playbooks could not run their own first investigation step.** The Helm chart's
  read-only role granted no access to `resourcequotas`, `horizontalpodautoscalers`,
  `storageclasses`, `apiservices` or the webhook configurations — all named directly by the
  `QuotaExceeded`, `HPANotScaling`, `PvcPending` and `WebhookAdmissionRejected` playbooks. Each
  returned `Error from server (Forbidden)` during the incident the playbook exists for, while
  `helm install` succeeded, `kubeintellect status` reported a reachable cluster and the playbook
  count said 23. The six grants are added, and a new test derives the required permissions from
  the playbooks so a new playbook cannot ship a step the role would refuse. Secrets remain
  deliberately ungranted, named as an exception with the reason: `get` on a Secret returns its
  values and RBAC has no key-only read.
- **The FAQ understated the product by five playbooks.** `docs/faq.md` — the "why not just use
  ChatGPT?" page — said **18 deterministic playbooks** while the library had reached 23. The
  doc-claims gate could not see it: it checks an enumerated list of `(doc, regex)` pairs, and
  neither `faq.md` nor `examples.md` appeared in it. Both are now in the table so `make docs-fix`
  heals them, and a new population-wide check walks every tracked doc instead of a list, so a
  count claim written in any doc is verified from the moment it is written. `docs/changelog.md` is
  excluded by name: a changelog records what was true at a release and must not be rewritten.
- **The Helm chart accepted a shutdown configuration it documents as broken.** `drainSeconds` is
  spent inside `terminationGracePeriodSeconds`, so `--set drainSeconds=60` against the default 45s
  grace rendered a pod that Kubernetes SIGKILLs 15 seconds before it finishes draining — silently
  reintroducing the dropped requests the preStop hook exists to prevent, with no error anywhere and
  a symptom (a few lost requests per rolling update) that nobody traces back to a values file. The
  chart now fails to render below the 15-second floor the shutdown-contract tests already required,
  naming the value to set. That rule previously held only for values files committed to the
  repository, never for `--set` or a private values file — which is how the knob is actually tuned.
- **`kubeintellect status` reported a configuration the server did not run on.** `status` read
  `~/.kubeintellect/.env` *and* a directory-local `./.env`; `serve` and `db-init` read only the
  first, although `docs/configuration.md` has always documented `./.env` as a config source and
  `docs/index.md` tells readers to `cp .env.example .env`. In one directory, at one moment, the
  board said `LLM ✓ configured`, `Auth ✓ enabled` and `No configuration issues found` while the
  server started with no API key and **open access**. All four commands now resolve configuration
  through one loader, so the board and the runtime cannot disagree. Precedence is unchanged and
  now documented correctly: shell environment, then `~/.kubeintellect/.env`, then `./.env` — a
  repo `.env` can never override the admin keys in your user config.
- `kubeintellect status` now validates the **effective** configuration — the merge of the shell
  environment, `~/.kubeintellect/.env` and `./.env`, which is what the rows above it and the server
  itself read. It previously validated `~/.kubeintellect/.env` alone, so the closing summary could
  contradict the board printed three lines above it: reporting a key as missing when it lives in
  `./.env`, or printing "No configuration issues found" over a row that had just failed because the
  shell overrode `LLM_PROVIDER`. The all-clear now also names the sources it checked.
- **The `LLM:` row on the status board meant "these environment variables are non-empty".** It
  printed a bare ✓ that an operator reads as *the model is usable*, while a revoked key, an endpoint
  with a typo and a deployment name that does not exist are all indistinguishable from a working
  configuration at that point — and each surfaces on the first incident. The row keeps its ✓, which
  is honest about what was measured, and now says `configured — not verified, no request is made`.
  A live credential check is deliberately not added here: the cheapest useful probe is a round trip
  to a paid endpoint and `status` is run casually and often, so that is a product decision rather
  than a repair.
- **`kubeintellect status` reported a cluster it had never contacted.** Every other row on that
  board is measured — the database with a real `psycopg.connect`, Prometheus, Loki, Grafana and
  Langfuse each over HTTP — but `Kube:` was green on `Path.exists()` alone, decorated with a context
  name read out of that same local file. An expired credential, a stopped kind cluster or a VPN that
  is down all printed ✓ to an operator asking "can it see my cluster?" before an incident. The row
  now probes the API server and has three states, because two cannot express it: **reachable**,
  **did not answer**, and **not verified** when there is no `kubectl` to ask with — the last of which
  is deliberately not ✓. The bound is a hard `subprocess` timeout, not `--request-timeout`, which
  governs the API request rather than the connection attempts in front of it.
- **The CI lint job had never read most of the tree.** The `ruff check` step named exactly two
  paths — `packages/kubeintellect-server/app/` and `packages/ki-protocol/` — so all 167 files under
  `v4/tests/`, the whole `kube-q` CLI and every build script were invisible to it: a contributor
  adding a test file got a green lint job that had not opened their file. Measured across the blind
  spot: **31 findings** (25 `E702` semicolon statements, 5 `E501`, 1 deliberate late import now
  annotated with its reason). All fixed — the two semicolon rewrites verified by AST equality, not
  by eye — and the step, plus `make lint`, now covers **392 of 392** tracked Python files under
  `v4/`. A new gate asserts coverage rather than a fixed list, so a new package fails until the step
  is widened, and `make lint` is held to the same paths as CI.
- **A promotion outcome source that failed was indistinguishable from one with no data.**
  `promotion_engine.outcomes_for` swallowed every exception and returned `[]` with no log line
  anywhere, so a broken store read exactly like an action class that has not run in shadow yet —
  and the decision that follows reports `hold` because *"n 0 < n_min 20"*, which reads as *not
  enough evidence* rather than *the evidence could not be read*. ADR-102 is **fast down, slow up**,
  so the direction that suffers is demotion: a class whose shadow agreement had collapsed would be
  held at its current rung indefinitely by a read failure nobody could see. `read_outcomes()` now
  returns the timeline **and** whether the source answered, and logs the failure with the action
  class; `outcomes_for` keeps its signature and still never raises.
- **`AGENTS.md` sized the `ruff format` gap at `~108` files when it was 116** — an ungated number
  in the file whose own policy is that derived numbers are gated, sitting in the "known pre-existing
  debt" section contributors read to decide what they may ignore. `check_doc_claims.py` now derives
  it, so `make docs-fix` heals it like every other measured claim, and it is counted over the files
  **git tracks** rather than by walking the directory — a stray unformatted file in someone's
  working copy must not move a number published in a doc.
- **`how-it-works.md` named a security boundary that is not in the write path.** It told readers
  that every proposed mutation is routed through the single write-authority decision in
  `app/tools/aci/mutating.py` — *"the gate is enforced server-side at that chokepoint"* — composing
  *"the action class's statistically earned rung"*. Measured against the AST: `decide_write` and
  `plan_mutation` have **no production caller**, `earned_rung` always arrives as its `L2` default,
  and `promotion_outcomes` — the ADR-102 store that would earn it — is created by the schema and by
  the chart but **written by nothing** outside its own tests. (A text scan says otherwise:
  `app/memory/prospective.py` defines an unrelated `record_outcome`.) The live A3 brake is
  `autonomy.watchtower`, composing the ADR-003 ladder, the `(playbook, namespace)` allowlist and
  `auto_write_permitted()`, which denies on an engaged kill switch or a declared change freeze —
  so the paragraph now describes that, and marks the ACI chokepoint as the designed destination it
  is. Pinned as an **equivalence**: wiring the chokepoint up fails the new gate exactly as loudly
  as un-wiring it, and names the paragraph to update.
- **The gated test count published in `AGENTS.md` was a property of the machine, not of the
  repository.** `scripts/check_doc_claims.py` derived it by collecting whatever `v4/tests/` held on
  disk, and `test_db_init_reports_failure.py` parametrized its psql sweep over `docs/**/*.md` the
  same way. This tree carries `docs/evaluation.md` and `tests/test_evaluation_runner.py`, which
  `.git/info/exclude` deliberately keeps out of the public repository, so every `make docs-fix` wrote
  a number no clone could reproduce — and `tests/test_doc_claims.py` then failed for a contributor on
  a file they had never touched. Any file not yet `git add`ed moved it too: measured, three stray
  test functions and one stray markdown file shifted the published figure by four. Both now read the
  set of files the repository actually carries, and fall back to everything on disk — never to
  nothing — where that cannot be determined. Measured in a clone-like checkout: the claim was
  unreproducible before, and both trees now collect **3568**.
- **Two test files could only ever pass on the machine that wrote them.**
  `test_observability_tools_honour_the_blocklist.py` (33 tests) read `LOKI_URL` / `PROMETHEUS_URL`
  from whatever `v4/.env` the developer happened to have; no workflow sets either, so in a clean
  checkout both tools short-circuit with *"… is not configured"* — the input-gate assertions fail
  outright and, worse, every `assert "SECRET" not in out` passes **because nothing ran**. The file
  now configures its own unroutable datasources, and pins that vacuity as an explicit case.
  `test_the_other_maps_match_the_territory.py` asserted against `v4/CLAUDE.md`, which is tracked
  only in the private tree: four `FileNotFoundError`s anywhere else. That one class is now guarded
  by an existence check, and a companion test fails if a second conditional ever appears — counted
  from the AST, since a substring count of `skipif` also matches its own assertion.
  Measured in a checkout without either file: 31 failures before, 0 after.
- **42 text-mode reads across 10 test files named no encoding**, which the platform default
  decides — not UTF-8 on Windows or under the C locale (#136/#156).
- **Both repo-root CI gates could report a clean tree having examined nothing.**
  `scripts/check-file-modes.sh` printed *"file modes OK — every tracked file outside v1-v3 is
  executable if and only if it has a shebang"*, which is a claim about the tree; what it measures
  is one git index, skipping in silence every tracked path that is not a regular file in the
  current checkout. A sparse or partial checkout therefore produced that same confident line over
  **zero** files, and exited 0. Both gates now state how many files they actually examined, count
  and report the paths they had to skip, and **fail** when that count is zero.
  `check-file-modes.sh` also takes `--git-dir DIR`, so the invariant can be pointed at another
  index — and its `--fix` hint now names the index that was checked rather than the default one.
  Neither gate had a test driving it before this; both do now.
- **The memory audit chain could not see its own tail being cut off.** `security.md` promised that
  a silent edit, delete or reorder of learned memory is detectable; measured against the real append
  path, deleting the *newest* entries verified as intact, and the next legitimate append continued
  from the surviving tail so the loss became invisible permanently — which is exactly the deletion an
  attacker would make. A `memory_chain_head` row now anchors how far the chain got, a shorter chain
  contradicts it, and an append after a truncation continues past the head instead of filling the gap.
  It remains tamper-evidence, not prevention, and the documentation now says so — along with the fact
  that no endpoint, command or scheduled check invokes the verifier today.
- **Every knowledge-graph edge claimed a provenance it could not resolve.** `kg_edges.source_kind`
  is `NOT NULL DEFAULT 'observation'` and is the sole input to the memory write-admission trust
  score, where `observation` scores 1.0 — yet every edge the sensorium ingest path wrote carried
  that claim with `source_id = NULL`, so *which* observation was unanswerable. Edges derived from
  the cluster watch now cite the apiserver's `uid` + `resourceVersion` for the exact object version
  behind the fact. An observation with no identity still writes its edges, with no citation rather
  than a synthetic one — a reference that looks resolvable and is not would be worse than a blank.
- **A chat client could claim sensor provenance and bypass the whole memory-poisoning guard.**
  `MEMORY_SECURITY_HARDENING`'s write-admission guard treats provenance as its primary, non-LLM
  validator: at trust ≥ 0.9 a write is admitted as `sensor_trusted` *before* the injection-signature
  check, the rate limiter and the contradiction check run. The cortex `remember` node derived that
  provenance from `state["user_id"]`, which is `body.user` — a free-form field of the chat request —
  so a caller who sent `{"user": "watchtower"}` had their episode stored as detector-derived at trust
  1.0 with every validator skipped, which is precisely the MINJA query-only attack the guard exists
  to stop. Provenance is now a separate turn-state field (`trigger_source`) that only an in-process
  caller can set, applied *after* the `extra_state` merge so no caller-supplied key can override it;
  the watchtower asks for `detector` explicitly and the HTTP endpoint cannot pass it at all.
  Both flags are off by default, so no default deployment was exposed.
- **A dropped SSE frame was indistinguishable from a frame the server never sent.** Both
  `kube-q` SSE parsers swallowed any frame they could not decode, and neither could see a stream
  that ended mid-frame. They now count losses into an optional `SseStats` without changing what
  they yield, and the chat stream warns that an answer may be incomplete instead of silently
  presenting a partial one as whole. See the `kube-q` changelog.
- **A stored detector that can never fire was still loaded, and could still be promoted.** The
  liveness check added for natural-language authoring guarded one door; `memory/consolidation.py`
  writes to the `detectors` table as well, `promote_candidate` only flipped `status` without
  re-reading the predicate, and any row written before that gate existed was still in the table.
  Three dead rows — #114's spaced alternation, a `kind` the engine never matches, and a `Pod`
  predicate with no `status_regex` — loaded as two active and one shadow detector. The shadow
  case is the damaging one: shadow detectors are promoted on their precision record, and a
  detector that cannot fire shows zero firings, which is indistinguishable from a condition that
  never occurred. `load_db_detectors` now drops such a row with a warning naming it and the
  reason (per row — one bad candidate cannot cost the cluster its other detectors), and
  `POST /v1/detectors/{name}/promote` answers **409** with the reason instead of a cheerful
  `status: active`. A store read failure is still not treated as evidence of deadness, and a
  missing detector is still a 404; demotion is never blocked.
- **A mistyped `triggers:` key silently removed a playbook from the router.** `Trigger` reads
  exactly `pod_status_regex`, `event_reason_regex` and `event_message_regex` via `raw.get`, so
  a near miss — `reason_regex:` one level up, or a key one character short — compiled to a
  trigger holding nothing. The playbook still loaded, still counted toward the playbook total
  and still passed the schema check, while `match_playbooks` iterated it forever without ever
  matching; the `if not pb.triggers` guard did not fire either, because the tuple is non-empty.
  This is the router-side twin of #114's dead `detect:` predicate. The loader now warns with
  the playbook name and the offending keys, and `tests/test_every_playbook_is_reachable.py`
  turns it into a failure — it also checks every one of the 41 shipped trigger regexes actually
  routes to its own playbook, so the two streams cannot be crossed unnoticed. All 23 shipped
  playbooks were already reachable; nothing that fires today stops firing.
- **A natural-language-authored detector could be staged as a permanent no-op, and its zero
  firings would read as "the condition never occurred".** `validate_detect_block` documented
  itself as *"the compiler **is** the validator"* — but the compiler only proves a predicate is
  well-formed, never that it can match anything. Four shapes passed it with zero errors: a
  `kind` the engine does not handle, a `kind` in the wrong case (`matches()` is
  case-sensitive), a `Pod`/`Node` predicate with no `status_regex`, and a space inside an
  anchored alternation — #114's exact mistake, which a model writing regexes from prose makes
  more readily than a person reading the schema. A new `detectors/predicate_shape` module
  expands a pattern into every string it can produce and requires each to be a value a cluster
  actually emits (`message_regex` is exempt — an event message is prose; an unexpandable
  pattern is treated as *unknown*, not dead, so a valid-but-exotic regex still passes). The
  validator now returns those as errors, and the 30 shipped predicates are checked the same way
  in CI. Asserting mere satisfiability does **not** work here and was tried first: the sample is
  generated from the pattern, so the stray space rides along and the assertion passes.
- **`kq replay` printed a blank summary for every detector firing.** The hash-chained
  `decision_log` has two readers — `app/digest/postmortem.py`, which builds the incident
  narrative, and `kq replay`, which streams the record back to a terminal — and each turned a
  row into a line of text with its own code. The postmortem handled all eleven recorded kinds;
  the CLI matched seven *top-level field names*, so any row whose content lives elsewhere
  rendered as an empty cell: `finding` (a `findings:<cluster>` episode contains nothing else,
  so `kq replay findings:default` printed N rows of nothing while the detectors had certainly
  fired), `plan` (all in `steps`) and `ki_otel_span` (all in `attributes`). A blank cell does
  not read as *no summary available*; it reads as *this event carried nothing*, which is the
  one thing a tamper-evident log must never say falsely. The summariser now lives once, in
  `ki_protocol.record.summarise_record`, and both readers call it. Separately, the replay
  endpoint yielded the payload alone, so the `type` column depended on each payload happening
  to echo its own kind — every hand-written recorder call remembered to, but `otel_spans`
  never did, and those rows replayed as type `?`; the row's `kind` is now authoritative. A new
  suite reads *both* artefacts, so a kind nothing summarises fails a test instead of quietly
  rendering as an empty cell.
- **The two halves of `ki-protocol` did not agree, and the SDK dropped payloads because of it.**
  `ki_protocol.wire` (server emission models) and `ki_protocol.events` (the client's typed union
  and `parse_event`) are one contract whose own docstring says *"wire-format changes must update
  both modules together"* — a rule kept by discipline and by nothing else: no test in either suite
  imported both halves, so each was only ever checked against its own fixtures. Serialising every
  emission model and feeding it to `parse_event` showed five of the eight arriving stripped —
  `tool_result.output`, `tool_call.command`, `error.error` and all four `hitl_request` fields were
  dropped into models that declare different names for the same things, and `plan` (emitted since
  V2, and on every Cortex turn) was discarded entirely because the client union never carried it.
  A dropped payload is worse than a rejected frame: `summary=""` is the shape of a tool that
  returned nothing. `parse_event` now maps the wire's field names onto the client's, only where
  the client-side name is not already set, and `PlanEvent`/`PlanData` join the union. The `kq`
  REPL was never affected — it reads the raw frames — but `KubeQClient`/`AsyncKubeQClient`, the
  documented SDK entry points, were. A generative test now walks every model in `wire` and fails
  if one has no client counterpart.
- **The connection/identity refusal turned the v5 capability sandbox off.** Refusing every
  `--as` flag is right for one a model wrote; `app/tools/aci/sandbox.py` is not a caller
  choosing an identity but app code narrowing it to a ServiceAccount with strictly fewer
  rights — and it runs with `hitl_bypass=True` on exactly that basis. Measured, `run_as("get
  pods -n prod", "read-only")` returned `[Protected] '--as' is not permitted.` and reached
  kubectl not at all: the app-side gate given up, the cluster-side gate never applied, and a
  refusal string handed back where the caller expected command output. `run_kubectl` now
  accepts one impersonation token when the run config names that exact token
  (`sandbox_identity`, injected by the graph the way `hitl_bypass` and `user_role` are, never
  writable by a model); a different value, a second identity flag, or any other connection flag
  beside it is refused as before. The existing sandbox tests could not see the break — every
  case injects a `_runner`, so none of them crosses the seam into the real tool.

### Documentation
- **The architecture code map pointed at modules that do not exist.** `docs/architecture.md`
  carries the map a contributor or integrator reads instead of the source tree. Three of its 34
  `.py` entries named a module that exists nowhere under the server package — `endpoints/stream.py`,
  `endpoints/memory.py` and `db/memory.py` — and one endpoint annotation, `GET
  /v1/chat/stream/{session_id}`, named a route the server does not expose (cited twice: in the
  request-flow diagram and in the map). Following the page, an integrator would have opened an SSE
  connection to a path that 404s and read it as a server fault. In fact `POST /v1/chat/completions`
  **is** the stream — it returns a `StreamingResponse` with `media_type="text/event-stream"` — and
  the separate SSE route is `GET /v1/events/replay/{session_id}`, served by `events.py`; pinned
  context is `preferences.py`, and the store lives under `app/memory/`. Map and diagram corrected,
  and `db/` now lists the flight recorder it had omitted. 14 tests hold every module the map names
  and every `/v1` path any doc page cites to what the server actually has, with non-vacuity floors
  so a regex that stops matching fails instead of passing silently.

### Security
- **The "your setting did nothing" report could not say it about eleven of its own entries.**
  `UNWIRED_EXPERIMENTAL_FLAGS` (`app/core/version.py`) lists the v5 settings that are declared,
  documented and read by no code, and both its own comment and `docs/v5-experimental-flags.md` —
  a public page in the docs nav — promise that setting one surfaces it under
  `set_but_unwired_flags` in `GET /healthz`, `GET /v1/v5/status` and the startup line. But
  `set_but_unwired_flags()` was `_on_booleans() & UNWIRED_EXPERIMENTAL_FLAGS`, and `_on_booleans()`
  filters `isinstance(value, bool)` — while **11 of the 26 entries are `float` or `int`**. No value
  an operator could give them would ever reach that report. Measured 2026-08-20 with three knobs
  moved off their defaults next to one boolean as a control: `KI_V5_RIGHTSIZING=true` was reported
  by `/healthz` and `version_line()`; `KI_V5_AGENT_COST_RATE_CAP=0.10`,
  `KI_V5_SPEND_OUT_PRICE_PER_1K=0.99` and `KI_V5_DETECTOR_MIN_FIRINGS=3` were reported by nothing,
  anywhere. The cost cap is the one that matters — it reads as a spend brake, the page describes it
  as *"USD/min above this ⇒ runaway spend"*, and it was the quietest of the eleven. A knob now
  counts as set when it differs from its **declared default**, not when it is truthy:
  `KI_V5_AGENT_COST_RATE_CAP=0` is a deliberate instruction, and `KI_V5_STAGE_SIZE=1` is already
  the default. `active_experimental_flags()` is deliberately unchanged — a knob is configuration,
  not on/off runtime identity, which is a different question from "did what I set do anything".
  117 tests pin it, including a negative control over all 21 *wired* experimental knobs and a gate
  that walks every ⚠️ row on the public page and checks it can actually be surfaced.
- **The reporter that exists to catch guards protecting nothing had that exact hole, and it
  failed open.** `app/core/config_audit.py` was written so an operator who configures a
  protection is told when it does nothing; `GET /v1/v5/status` and `kq v5-status` carry the
  result. `autonomy_override_problems()` validated an entry's `=` and its *level* and never the
  namespace it names — but `level_for_namespace` is an exact dict hit on a lowercased key, so an
  entry whose namespace is not a real namespace name parses cleanly, stores a key nothing can
  match, and was reported as fine. Measured with `AUTONOMY_LEVEL=A3` and
  `AUTONOMY_NAMESPACE_LEVELS="prod-*=A0"` — a natural thing to write, because the sibling
  `AUTONOMY_A3_ALLOWLIST` *does* take globs and `docs/configuration.md` said so one line away:
  `unenforceable_guard_config()` returned `[]`, `level_for_namespace("prod-web")` returned **A3**
  rather than the pinned A0, and with `AUTONOMY_A3_ALLOWLIST="CrashLoopBackOff/prod-*"`,
  `a3_allowed("CrashLoopBackOff", "prod-web")` returned **True** — the watchtower would auto-fix
  in precisely the namespaces the operator believed were pinned to investigate-only. A glob, a
  `?`, a slash and an embedded space were all silent. `a3_allowlist_problems()` had the same class
  of gap (empty playbook, empty pattern, a second `/`), failing closed rather than open. Both now
  validate the whole entry, and the glob message names the setting where globs *do* work. The
  empty key is deliberately still not reported: `level_for_namespace("")` is the cluster-scoped
  lookup, so `=A0` really does pin cluster-scoped objects. 53 tests pin it, each asserting the
  underlying behaviour first so the report is never its own only evidence.
- **The break-glass page promised a stop button the product exposes nowhere.**
  `docs/autonomy.md#stopping-the-agent-break-glass` — the page an operator reads *during* an
  incident — said the two write brakes bind "without a redeploy" and listed the kill switch as
  engageable by "`KI_V5_KILL_SWITCH=true`, or the runtime toggle (no restart)". Measured across
  every operator surface: no API route matches kill/stop/freeze/brake in any of the 19 paths
  (`GET /v1/v5/status` reports the brakes and nothing sets one); no `kq` command engages one; the
  chart's ConfigMap is an explicit key allowlist with no `extraEnv` escape, so neither setting is
  reachable through Helm values; and `engage_kill_switch()` has no caller in `app/` outside
  `budget.py`. Settings are read once at process start — setting `KI_V5_KILL_SWITCH=true` in the
  environment of a running process leaves `kill_switch_engaged()` False. So engaging a brake
  required the restart the page said was unnecessary, and the in-process toggle sets a module
  global in one process: even wired to a route it would stop only the replica that served the
  request. The gates themselves were correct; the sentence was not. The page now documents what
  exists (`kubectl set env deploy/kubeintellect KI_V5_KILL_SWITCH=true`, confirm with
  `kq v5-status`) and states plainly which surfaces are missing and why the toggle is per-replica;
  `budget.py`'s module docstring carried the same claim and is corrected. 24 tests hold the claim
  to the mechanism — they re-permit it automatically if the toggle ever gets a production caller.
- **`GET /v1/v5/status` read a brake source directly instead of its reader.** `kill_switch_engaged`
  was reported through `kill_switch_engaged()`; `change_freeze` was reported as
  `settings.KI_V5_CHANGE_FREEZE`, bypassing the `change_freeze_active()` reader the write gates
  use. Behaviour is identical today — no caller injects freeze windows — so this is a consistency
  fix, not a live defect; it removes the second place a future freeze source could be honoured by
  the gates and not by the surface that reports them.
- **A declared change freeze stopped one of the two write gates.** `KI_V5_CHANGE_FREEZE` is an
  operator saying *stop* — `GET /v1/v5/status` reports it and `kq v5-status` prints it. Its sibling
  brake, the kill switch, is read through one `kill_switch_engaged()` that composes its two sources,
  so both write gates obey it. The change freeze had no such reader: `auto_write_permitted` (the
  watchtower's A3 path) read the settings flag, while `gate_write` (the ACI write chokepoint) read
  only an injected `(now_epoch, freeze_windows)` pair — which its one caller passes as neither. With
  `KI_V5_CHANGE_FREEZE=true` and nothing else set, `gate_write()` returned *allow* and
  `decide_write("kubectl scale …", earned_rung="L4")` returned **auto**, while the same settings
  denied on the kill switch. Both gates now read one `change_freeze_active()`. Scope, stated plainly:
  `decide_write`/`plan_mutation` have no production caller yet, so unlike the kill-switch defect this
  was latent rather than live — what was live is a status surface reporting a brake that half the
  gate surface did not implement. 45 tests pin it, including that patching the single reader moves
  both gates.
- **A `readonly` API key could grant itself `cluster-admin` via `kubectl auth reconcile`.** The
  verb logic is an allowlist — a verb is a write unless it is on the read-only list — so a verb
  wrongly *on* that list is an open door, not a missing rule. `auth` was on it because
  `kubectl auth can-i` and `auth whoami` ask questions, but `kubectl auth reconcile` **writes**:
  it creates and updates Roles, RoleBindings, ClusterRoles and ClusterRoleBindings from a
  manifest. Measured 2026-08-20 with a stubbed kubectl, a `readonly` key ran
  `kubectl auth reconcile -f -` carrying a ClusterRoleBinding to `cluster-admin` with no approval
  prompt, while `kubectl create -f -` with the identical manifest was refused. `auth` now sits in
  `_READ_ONLY_SUBCOMMANDS` alongside `rollout`, `config` and `certificate`, so `can-i` and
  `whoami` still read and everything else — including a subcommand a future kubectl adds — is a
  write. The two tables are additionally asserted disjoint, because a verb in both reads as a
  blanket read before its subcommand is consulted.
- **`kubectl cluster-info dump` returned the contents of every protected namespace.** Read-only
  against the cluster is not read-only against what may be read. `cluster-info dump` walks every
  namespace and prints pod specs, events and container logs; no namespace filter reaches it,
  because the verb names no resource type and both filters key off that. Measured 2026-08-20, a
  `readonly` key ran `kubectl cluster-info dump --all-namespaces` unfiltered. It is a concatenated
  dump with no per-object shape to filter, so it is now refused for every role on the same rule
  `-o custom-columns` is, with a message pointing at `-n <namespace>`. Bare `cluster-info` is
  untouched. 55 tests added across both fixes.
- **A boolean listed as a value flag made one shared parse swallow the verb — and every gate with
  it.** `_skip_flags` is the single walk that finds the verb for every gate in `run_kubectl`; it
  consults `_VALUE_FLAGS` to decide whether a flag consumes the token after it.
  `--warnings-as-errors` is a boolean (pflag accepts it bare) and it was in that table, so the
  walk consumed the verb as its value. Measured 2026-08-20 with `hitl_bypass` on (an
  `auto_approve` session or "approve all"): `kubectl --warnings-as-errors get secrets -n prod` and
  `... get sa -n prod` **ran and returned credential rows** where the unprefixed forms are
  refused, and `kubectl --warnings-as-errors delete namespace shop` ran with **no always-confirm
  prompt**. At the gate level the verb read as `secrets` / `namespace` / `image`, so
  `_extract_resource_type` returned `None` (nothing to compare against the blocklist),
  `_classify_risk` fell from `high` to `medium`, and `_requires_always_confirm` returned `False`.
  Routing every gate through one parse — the fix for four earlier defects in this family — is what
  made a single wrong table row move all of them at once. `--warnings-as-errors` is removed from
  the table, a `_BOOLEAN_GLOBAL_FLAGS` set records kubectl's boolean globals, the two are asserted
  disjoint, and a command corpus asserts that a flag carrying no meaning about the request changes
  no gate's answer. 84 tests added.
- **`helm get manifest` stripped the release's Secrets — unless you wrote `-n` before the verb.**
  `run_helm` removes protected kinds from rendered manifests because `helm get manifest` returns
  a release's own `kind: Secret` objects with their base64 `data:` intact, which is exactly what
  `kubectl get secret` is refused for under every role. It decided when to strip from the first
  non-flag token in `tokens[2:]`, so any global flag before the verb put its *value* there:
  measured 2026-08-20, `helm -n prod get manifest shop`, `helm --namespace prod get manifest
  shop` and `helm -n prod get all shop` all returned the base64 password in full, while
  `helm get manifest shop -n prod` stripped it. `helm get hooks` renders manifests too and was
  never on the enumerated list at all. Stripping now runs on **every** `helm get` — the decision
  is removed rather than the parse repaired — and `_extract_verb`'s flag walk is a shared
  `_skip_flags()` helper, matching `run_kubectl`.
- **A quoted or comment-suffixed `kind:` kept a Secret in a Helm manifest.** The same stripper
  matched the kind as a bare token to end-of-line (`^kind:\s*([A-Za-z0-9.-]+)\s*$`), so
  `kind: "Secret"` and `kind: Secret  # managed by the platform team` — both ordinary YAML a
  chart can render — failed to match and the document was returned with its `data:` block. Quotes
  and trailing comments are now part of the line, not part of the value. 40 tests added across
  both fixes.
- **The namespace filter handled `-o jsonpath` by splitting the output on spaces — that is one
  jsonpath.** A bare `kubectl get namespaces` is allowed *because* the protected entries are
  stripped from the answer. For `-o jsonpath` the filter dropped whitespace-separated tokens
  equal to a blocked name, which works only for the expression that prints bare names separated
  by spaces. jsonpath prints whatever the caller asks for: measured 2026-08-20 against the
  default blocklist, `{range .items[*]}{.metadata.name}{","}{end}` returned
  `default,kube-system,monitoring,` and the `=`/`:` variants returned `kube-system=Active` — in
  full, **with no withheld note**, so the answer looked complete. The name was still there, it
  was simply no longer a whole token, and there is no separator jsonpath cannot produce.
  `-o custom-columns` and `-o go-template` were already refused for exactly this reason, and the
  `--all-namespaces` sibling already refused jsonpath too — two functions doing one job gave two
  answers for one format. The branch is removed rather than patched, so jsonpath now falls to the
  same `_FIXED_SHAPE_FORMATS` allowlist as every other caller-shaped format. Table, `-o wide`,
  `-o name`, `-o json`, `-o yaml` and `describe` are filtered exactly as before. 25 tests added.
- **The protected-namespace guard read one name, in one of the two places kubectl puts it.**
  `_targeted_namespace` exists because an infrastructure namespace can be a command's
  positional target rather than its `-n` value, and the documented rule is a hard refusal
  including reads. It took the first non-flag token *after* the resource kind — so it missed
  the `resource/name` shorthand its own sibling `_extract_resource_type` documents and handles,
  and it missed every name after the first. Measured 2026-08-20 against the default blocklist:
  `kubectl delete ns/kube-system`, `kubectl delete namespace/kube-system`,
  `kubectl delete ns shop kube-system` and the **ungated read** `kubectl get ns/kube-system`
  all ran; only `kubectl delete ns kube-system` was refused. Now `_targeted_namespaces()`
  returns every name in both spellings and the guard tests all of them, while
  `kubectl delete ns tenant-a tenant-b` stays a normal HITL-gated operation. Its remaining
  `args.index(verb)` went too — `_operand_index()` is the one parse the verb, the resource
  type, the always-confirm gate and this guard now share. 40 tests added.
- **One flag between the verb and its target turned off the gate that cannot be turned off.**
  `_requires_always_confirm` is the only gate in `run_kubectl` that fires *through*
  `hitl_bypass` — cascading deletes (`namespace`, `pv`, `crd`) and live workload mutations
  (`set image`, `set resources`) prompt the human even on an auto-approve session, because
  none has a rollback path. It read its target as the fixed index `args[2]`, which is the
  operand only when the command is written verb-first with nothing in between. Measured
  2026-08-20: `kubectl delete -n prod namespace shop`, `kubectl -n prod delete namespace
  shop`, `kubectl delete --force namespace shop`, `kubectl delete --ignore-not-found pv
  my-volume` and `kubectl -n prod set image deploy/api api=nginx` all returned `False` — so
  the *natural* way to write a cascading namespace deletion executed with no prompt while the
  awkward way stopped and asked. `drain` was never affected (matched on the verb alone). The
  same positional trap had already been fixed in both sibling parsers; this one was missed, so
  all three now share one `_operand_after_verb()` helper. That helper also removes a second
  assumption in `_extract_resource_type`, which located the verb with `args.index(verb)` — the
  *first* place the verb string appears, which a flag value can take: in a namespace named
  `get`, `kubectl --namespace get get secrets` read the resource as `get` and missed the
  Secret block. 54 tests added.
- **"approve all" bypassed HITL for one turn; four documentation surfaces said "for the rest
  of the session".** `stream_events` rebuilds its run config on every call, and `hitl_bypass`
  comes from `auto_approve` — the request body (`kq --auto-approve`) or the *current* message.
  Nothing persists it, and the `kq` REPL does not latch it either. Measured over three turns:
  `"approve all"` → bypass on, then the next two turns → bypass off, gated again. Meanwhile
  `docs/security.md` said *"session-wide bypass"*, `docs/examples.md` and `docs/cli-reference.md`
  said *"for the rest of a session"*, `docs/api-reference.md` said *"for the rest of the
  session"*, and the log line announced *"HITL bypass enabled for session=…"*. The gap is in the
  **safe** direction — the gate stays on — so the behaviour is left alone and the claim is
  corrected everywhere, with `tests/test_approve_all_is_one_turn.py` pinning what actually
  happens. Whether the bypass *should* span a session is an owner decision: widening the
  product's central safety gate is not a side effect of fixing a sentence.
- **The secret redactor was a YAML redactor, and reported itself applied to any text.**
  Everything in `app/utils/redact.py` is line-aware — `key: value` matching, following a
  credential key onto the lines its value occupies, recognising keys that are secrets by
  convention — and every one of those rules was written against the shape `kubectl -o yaml`
  produces. `kubectl -o json`, an equally ordinary read and the form the API itself returns,
  writes quoted keys, which `_LINE_RE` did not match at all; both lines of the Kubernetes env
  idiom fell through to the free-text branch. Measured on the same object: `-o yaml` gave
  `value: <redacted>`, `-o json` stored `"value": "hunter2-prod-db"`, and a Secret's `tls.key`
  went the same way. Whether a credential was caught depended on the `-o` flag the caller
  happened to pass. The parser now captures the key's quote (back-referenced, so an opening
  quote requires a closing one) and `_unwrap_value` gives the value's punctuation back to the
  emitter — so a redacted JSON document is **still valid JSON**, keys and non-secret values
  intact, rather than mangled text. A new suite renders the same objects both ways and asserts
  neither leaks.
- **The flight recorder's secret scrubber walked one level of the payload and reported itself
  applied.** `REFLEXION_REDACT_SECRETS` (on by default) is documented as *apply secret/URL/token
  scrubbing before persisting*, and `flight_recorder._scrub` said *redact secrets from string
  fields*. What it did was iterate `payload.items()` and redact the values that happened to be
  strings — anything inside a list or a nested object went into the hash-chained `decision_log`
  verbatim. Measured: `{"attributes": {"ki.action": "kubectl … --token=AKIA…"}}` kept the token,
  and so did a `plan`'s `steps`. Which payloads were covered was an accident of how each call
  site shaped its dict; `rollback_point.pre_state` is safe only because `kubectl_tool` redacts
  every capture itself before handing it over. The scrubber now walks dicts and lists to a bound
  of six levels. The 1,500-character cap deliberately keeps its current reach — top-level fields
  only — because a nested string arrives already capped by its producer, and re-capping a
  rollback pre-state at 1,500 would cost that capture its restorability to enforce a limit
  nothing asked for. `docs/data-handling.md`, which had stated the old limit accurately, is
  updated.
- **A rollback capture whose only redaction was a private key was described as, in full,
  "redacted".** `kubectl_tool._capture_note` tells the operator what redaction did to a
  pre-state capture, and it counted three of the six markers `redact_secrets` can emit — a
  hand-copied subset. A PEM block (`<redacted-pem-block>`) and a Secret's `data:` block
  (`<redacted-block>`) were not among them, so the most secret-dense objects there are produced
  the least informative note. The vocabulary now lives once, as `redact.REDACTION_MARKERS` +
  `count_redactions`, and a test reads the tuple against the literals in `redact.py`'s own
  source so a new marker cannot be added without joining it.
- **The cluster snapshot pasted into every prompt was a second, unguarded kubectl.**
  `context_fetcher` pre-fetches `kubectl get pods --all-namespaces` and
  `kubectl get events --all-namespaces --field-selector=type=Warning` through its own
  `subprocess.run`, not through `run_kubectl`, and enforced none of that tool's policy. The
  identical command *through* the tool has its blocked-namespace rows removed; the snapshot
  pasted the whole table into the coordinator's system prompt on every turn, warning `MESSAGE`
  column included — which is where a `FailedMount` event names the secret it could not find and
  a failing probe names the apiserver's address. `snapshot_pod_count` counted those pods too, so
  the model was told a number it could never reproduce with a tool call. Worse, the same executor
  is handed `-n <namespace>` built from the `TARGETED:` line the **model** writes: `run_kubectl`
  refuses `describe pod etcd-control-plane -n kube-system`, while the same read through the
  snapshot path returned the full description — environment variable names, mounted certificate
  paths — into the prompt. The blocklist and the connection/identity flag family are now enforced
  at the one place the subprocess is launched, cluster-wide tables are row-filtered with the same
  `namespace_guard` helper `run_kubectl` uses, and a filtered listing says so rather than
  reading as a complete one.
- **Detector findings carried raw Kubernetes event text out of the blocked namespaces.** The
  sensorium runs `kubectl get pods -A --watch` and `kubectl get events -A --watch` as raw
  subprocesses, not through `run_kubectl`, and `app/sensorium/` has no reference to the namespace
  blocklist. That is deliberate — a watchtower that cannot see the infrastructure namespaces
  cannot tell a quiet cluster from an unwatched one — but the free text came with it: a finding
  in `kube-system`, `kubeintellect` or `monitoring` carried up to 140 characters of raw event
  message (`MountVolume.SetUp failed … secret "kubeintellect-secrets" not found …`), and
  `GET /v1/findings` returns it to every caller with no role parameter at all. An event `message`
  is arbitrary cluster text; every other field a finding carries is an enum or an object name.
  The message is now withheld for a blocked namespace and everything else is kept, so the
  operator still learns that coredns is crash-looping. Only `pod_status` observations reach the
  knowledge graph, so no event text was stored there — verified, not assumed. See
  `tests/test_the_watch_channel_respects_the_blocklist.py`.

### Documentation

- **Corrected the RBAC table in `docs/security.md`.** It stated that infrastructure-namespace
  access, reads included, is blocked for admin, operator and readonly. True of every agent tool;
  never true of the sensorium, which is cluster-wide by design. The table now carries an explicit
  row for detector findings from those namespaces instead of leaving it to be discovered.

- **`kubectl get pods --server=http://attacker.example.com -A` ran, and so did `--as=system:masters`.**
  Every gate in `run_kubectl` and `run_helm` reasons about *what* is being asked — the verb, the
  resource, the namespace, the role. Nothing looked at *which cluster the command talks to* or
  *under whose identity*, and nothing rejected the flags that decide it. Measured by capturing
  the argv that reaches `subprocess.run`, `--as`, `--as-group`, `--server`, `--kubeconfig`,
  `--context`, `--token`, `--insecure-skip-tls-verify` and Helm's `--kube-as-user`,
  `--kube-apiserver`, `--kube-token`, `--kube-context` all executed byte-for-byte on the plain
  read path with no role required. Impersonation still needed the ServiceAccount to hold
  `impersonate`, which the shipped chart does not grant — it failed closed *at the API server*,
  not in-app, and the chart offers `rbac.clusterAdmin: true` under which it would have worked.
  Redirection needs no cluster permission at all: the response is then whatever that endpoint
  returns, handed to the model as cluster truth, with the namespace filters reporting how much
  they withheld from attacker-supplied text. Both tools now refuse the connection/identity family
  — refused, not stripped, since silently dropping a flag answers a different question than the
  one asked. The `--as…` and `--kube-*` families are matched by prefix. See
  `tests/test_the_cluster_and_identity_are_not_arguments.py`.

- **`kubectl get pods -A -o custom-columns=...` returned the protected namespaces' rows.** Both
  namespace filters ended in a branch that assumes a kubectl table with NAMESPACE (or NAME) as
  the first column. They refused `-o name` and `-o jsonpath` *by name* — a deny-list of the two
  formats someone thought of — and let everything else reach that branch. `-o custom-columns`,
  `-o go-template`, `-o template` and their `-file` variants render whatever the caller asked for
  in whatever order, so the assumption is false: measured through the real tool, the `kube-system`
  and `monitoring` rows came back whole and unannotated, from `get pods -A` and from
  `get namespaces` alike. The same command with `NAME` in the *first* column was filtered
  correctly, which is what makes it an assumption rather than a check. Inverted to an
  **allowlist** of the shapes kubectl itself decides (`""`, `wide`, `json`, `yaml`), so an
  unanticipated format fails closed; a structured payload that is not a list of items now fails
  closed too. The tool's own parse-error message, which advised using `-o custom-columns`, now
  points at `-o json`. See `tests/test_only_a_shape_kubectl_chose_can_be_filtered.py`.

- **`query_prometheus` printed `= N/A` over live metrics, and crashed on `scalar(...)`.** The
  tool discarded Prometheus's `resultType` and chose its renderer from `range_minutes` — the
  *caller's* argument. An instant query whose expression carries a range selector
  (`container_cpu_usage_seconds_total{...}[5m]`; the tool's own docstring examples use
  `rate(x[5m])`) comes back as a **matrix**, whose entries have `values`, not `value` — so every
  series rendered `= N/A`, which is the shape of *no data*, over samples that were right there.
  A `scalar`/`string` result is a bare `[timestamp, "value"]` pair rather than a list of series,
  so it reached the namespace filter and raised `AttributeError: 'int' object has no attribute
  'get'` straight out of the tool — the guard was what destroyed the answer. Fixed: dispatch on
  `resultType`, render scalars and strings, never hand a non-mapping to the filter, and have
  `series_labels()` return `{}` for one instead of raising. `query_prometheus_series` (the
  detector entry point) now reports an unprojectable shape as an error rather than an empty
  list, so "no data" keeps meaning one thing. Detector paths were checked and were never
  affected. See `tests/test_prometheus_renders_what_it_was_sent.py`.

- **`query_loki` returned `kube-system` metric series with the namespace blocklist switched
  off.** The guard was applied to the wrong key on most metric queries. The tool decided log-vs-
  metric by testing whether the LogQL text *starts with* one of eight function names, and that
  guess also chose where the filter looked for labels — `stream` for logs, `metric` for metrics.
  A Loki **matrix** response has no `stream` key, so a misclassified metric query filtered
  against `{}`, `""` is in no blocklist, and every series passed. Seven of ten ordinary metric
  expressions failed the test, including `sum by (namespace) (rate({app="web"}[5m]))` — a space
  after `sum`, not a parenthesis. The answer came back with the blocked series present, no labels
  printed, and no notice that anything had been filtered. Fixed twice over, since either repair
  alone closes it: rendering and filtering now follow Loki's own `data.resultType` (it had
  already said what it returned), and `series_labels()` consults every known label container so a
  wrong hint cannot disable the guard. The classification survives only to choose request
  parameters. See `tests/test_loki_namespace_filter_survives_a_misroute.py`.

- **`kubectl logs … | grep -A 3 Traceback` answered "(no matching lines)" for a log containing
  the traceback.** `run_kubectl` reimplements `grep` in Python for its `|` support — a
  documented defence layer that had **no tests at all**. The parser skipped every token starting
  with `-` that was not `-v`, `-i` or `-E`, which produced two silently wrong answers, both
  measured against this machine's `/usr/bin/grep`:
  a **value-taking flag left its value in the pattern** (`grep -A 3 Traceback` searched for
  `"3 Traceback"` and returned nothing where real grep returns five lines — and `-A`/`-B`/`-C`
  is *the* idiom for reading a stack trace out of a log, so the agent was told the traceback in
  front of it did not exist); and **combined short flags vanished** (`-iv` matches neither `-i`
  nor `-v`, so `grep -iv info` ran as `grep info` and returned the exact **complement** of the
  requested set). `-c`, `-l`, `-q`, `-m`, `-o` were likewise ignored rather than honoured.
  The emulator now parses grep's arguments the way grep does — short clusters, attached values
  (`-A3`), `--flag=value`, `--` — and implements `-v -i -E -F -w -x -c -n -o -s -a -A -B -C -m
  -e`. **Anything it does not implement is named and refused**, which is the rule the module's
  own docstring already stated for unsupported *commands*. Correctness is held by a differential
  test that runs every supported combination through both implementations and compares byte for
  byte. `tests/test_pipe_grep_matches_real_grep.py`.
- **A filtered listing and a complete listing were the same bytes.** The namespace blocklist
  enforces itself two ways — a *refusal* (`kubectl get ns monitoring` → `[Protected] …`) and a
  *filter* (rows removed from a listing). The refusal is impossible to miss; the filter was
  silent on five of its six paths. Measured 2026-08-20: `kubectl get namespaces` dropped 3 rows,
  `kubectl get ns -o name` 2, `kubectl get ns -o json` 2, `kubectl describe namespaces` 2 and
  `helm list -A` 2 — none of them said anything. Only `kubectl get pods -A` appended a notice.
  The consequence is a false statement about the cluster: an agent asked whether the
  `monitoring` namespace exists runs `kubectl get namespaces`, receives a list it has no way to
  know is short, and answers **no**. Every filter now says what it withheld, and the note says
  *"This listing is NOT the complete set."*
  The sixth path told the truth and broke the format doing it: the `-A` filter appended its
  `[Protected]` sentence *after* `json.dumps`, so `kubectl get pods -A -o json` returned output
  that `json.loads` rejects with `Extra data` — a pre-existing defect an existing test had
  worked around by parsing only `out.split("\n[Protected]")[0]`. Structured output now carries
  the notice as a `withheldByPolicy` field inside the document.
  `kubectl_tool` also carried a byte-identical private copy of the notice helper; there is now
  one wording, in `namespace_guard`, for kubectl, Helm and the observability tools alike.
  **One documented limit**: `helm list -A -o json` is a bare JSON array — no field can hold the
  notice and nothing may follow it without making the payload unparseable. That case is logged
  server-side and asserted as a limit rather than left to be discovered.
  `tests/test_a_filtered_listing_says_so.py`.
- **The secret redactor deleted the label and kept the credential.** `redact_secrets` is the
  single funnel every stored artefact passes through — rollback captures, mutation captures,
  episode summaries and root causes, preferences, flight-recorder payload fields. It classified
  each line independently and dropped any line containing a keyword. YAML puts the name of a
  thing and its value on *different* lines, so measured against a plain Deployment:
  `- name: DB_PASSWORD` was dropped as `# <redacted-line>` and `value: hunter2-prod-db` was
  **kept verbatim**. The stored record was worse than an unredacted one — the credential
  survived and the only occurrence of the word "password" did not, so the review procedure the
  module's own docstring prescribes (*grep stored data for patterns we missed*) returned nothing
  on a record that was leaking. A `tls.key: |` block scalar stored its entire PEM body for the
  same reason plus two more: `tls.key` contains no keyword, and
  `-----BEGIN RSA PRIVATE KEY-----` does not match `private_key`.
  Redaction is now line-*aware*: the key is kept and the value replaced, a credential key is
  followed onto the lines its value actually occupies (block scalars and the `name:`/`value:`
  pair Kubernetes uses for env vars), PEM armour is redacted wherever it appears, and keys that
  are credentials by convention with no keyword in them (`tls.key`, `.dockerconfigjson`,
  `id_rsa`) are recognised. Structural fields such as `kind:` now survive, because a type name
  is not a credential and deleting it was the wrong half to delete.
  Two limits are asserted rather than left to be discovered: an unlabelled value (`foo: hunter2`)
  is kept, and a base64 blob embedded mid-line survives unless the whole value is base64 —
  widening the token pattern across `+` and `/` would redact filesystem paths that diagnostics
  need. This guards what is **stored**; it is not applied to the prompt sent to the model
  provider. `tests/test_redaction_keeps_the_label_not_the_secret.py`.
- **`KUBECTL_BLOCKED_RESOURCES="ConfigMap"` — the spelling Kubernetes itself uses for `kind:` —
  blocked nothing at all.** The shipped defaults are kubectl's lowercase plural
  (`secret,secrets,serviceaccount,serviceaccounts`), and every comparison folded only the
  command line, so an operator extending the list had to guess both the case *and* the number
  the code expected. Measured 2026-08-20 against the real `_check_protected_access`: with
  `ConfigMap` configured, `get configmap`, `get ConfigMap`, `get configmaps` and `get cm` were
  **all allowed**; with the lowercase singular `configmap`, `get configmaps` was still allowed.
  The configured side is now case-folded in `Settings.kubectl_blocked_resources` and expanded
  across singular and plural in `kubectl_tool._blocked_resources()` with the `-es`/`-ies` rules
  Kubernetes resource names actually follow (`ingress` ⇒ `ingresses`, `networkpolicy` ⇒
  `networkpolicies`, not the naive `+ "s"` that produces `ingres`); `helm_tool`'s manifest
  stripping reads the same expansion, so the two tools cannot disagree. An entry that can never
  match a resource type is reported through `config_audit` / `GET /v1/v5/status` / `kq v5-status`
  like any other unenforceable guard setting.
  **The credential floor was never affected** — `ALWAYS_BLOCKED_RESOURCES` is re-added
  unconditionally, and `get secrets` was measured blocked in every configuration tried.
  **kubectl short names are still not derived**: they come from API discovery, not from the
  string, so blocking `configmaps` does not block `cm`. That limit is asserted by a test rather
  than left to be discovered. `tests/test_blocked_resources_spelling.py`.
- **One capital letter in `KUBECTL_BLOCKED_NAMESPACES` disabled every namespace guard, silently.**
  Eight comparison sites across `kubectl_tool`, `helm_tool` and `namespace_guard` read
  `<value>.lower() in blocked` — the normalisation was applied to one side only, and
  `Settings.kubectl_blocked_namespaces` kept whatever case the operator typed. Measured
  2026-08-20 with `KUBECTL_BLOCKED_NAMESPACES="Kube-System"`, against the real guards:
  `kubectl get pods -A` returned the two `kube-system` rows the filter exists to remove;
  `kubectl delete deployment coredns -n kube-system` was **allowed**, where it is normally a
  `[Protected]` refusal at every role; the Loki/Prometheus query guard passed a
  `{namespace="kube-system"}` selector straight through; and the autonomy ladder returned `A1`
  where protected namespaces are meant to be pinned to `A0`. Nothing logged, nothing errored —
  the configuration *looked* correct. `ladder._normalise` even carried the docstring *"Match how
  the kubectl tool compares namespaces, so the two cannot disagree"*: it folded the namespace
  under test but not the set, and the kubectl gate folded neither. The blocklist is now folded in
  `config.py` (Kubernetes namespace names are RFC 1123 labels, so folding can only ever add
  protection), the command-line side is folded too — an LLM-written `-nKUBE-SYSTEM` no longer
  slips past — and a test asserts the property directly: a namespace is pinned to `A0` exactly
  when the kubectl gate refuses it.
- **Every guard setting is a comma-separated string whose parser discards silently.** What
  case-folding cannot repair is now reported instead of vanishing: a `KUBECTL_BLOCKED_NAMESPACES`
  entry that is not a legal namespace name (`kube-*` — a glob `AUTONOMY_A3_ALLOWLIST` supports and
  this setting does not — or `ingress/nginx`, or anything over 63 characters); an
  `AUTONOMY_NAMESPACE_LEVELS` entry the parser drops, which fails **open** by leaving the
  namespace on the permissive default the override existed to tighten; and a malformed
  `AUTONOMY_A3_ALLOWLIST` entry, which fails closed. New `app/core/config_audit.py` logs each at
  startup as `guard_config_unenforceable` and `GET /v1/v5/status` returns them under
  `unenforceable_guard_config`, rendered by `kq v5-status` — the same treatment
  `set_but_unwired_flags` already gives a switch that does nothing, one level down. Never fatal:
  an operator typo must not take the agent offline, only become impossible to miss.
- **The morning digest called a window quiet that `kq findings` refused to call clear.** Two
  surfaces answer the same question about the same window — `GET /v1/findings` (rendered by
  `kq findings`) and the digest (`kq digest`) — and each computed the answer itself. The digest
  validated its *recording* sources (recorder flag, SQLite mode, watchtower flag, pool, and both
  queries) and never asked whether anything had been **looking**. Measured 2026-08-20 with a stub
  Postgres pool answering every query truthfully with zero rows, all recorder flags on, and no
  watch stream connected: `/v1/findings` returned `{"sensorium": "starting", "findings": []}` and
  `kq findings` printed *"Sensorium is not watching … an empty result here does NOT mean the
  cluster is healthy"*, while `kq digest` over the same window printed **"Quiet watch: no findings
  in the last 24h."** The same held with `SENSORIUM_ENABLED=false`, where no detector could ever
  have fired. A flawless empty record and an empty cluster read identically. The classification
  now lives once in `app/detectors/perception.py` (`perception_state` / `perception_gaps`) and both
  surfaces read it, so they cannot answer differently; a disconnected watch stream or a blind
  predictive layer is a `degraded_reason` like any unreadable source, and the digest leads with
  *"Digest INCOMPLETE … This is NOT a quiet watch"*. Stated in the docs rather than implied: stream
  health is the state **now**, not a history of the window, so the reported gaps are a lower bound
  on the blindness in it.
- **The anticipatory detectors reported an all-clear that a connection refusal produced.** The
  layer whose entire job is to warn *before* a failure had two ways to go quiet without saying so.
  `query_prometheus_range_raw` returned only the series from `_query_raw` and **discarded the error
  string**, so every caller saw an unreachable Prometheus as an empty result set. Measured
  2026-08-20 against a real closed TCP port (`PROMETHEUS_URL=http://127.0.0.1:9`): the agentic/GPU
  collector's `_default_scalar("…sandbox_escape_attempts…")` returned **0.0** — the value that
  means *no escape attempts were observed* — and `collect_and_detect()` returned **`[]`**, which
  the caller reads as "clear". Same result with `PROMETHEUS_URL` unset. An instrument that reports
  `0` when it cannot read is worse than one that reports nothing: zero is an observation, and this
  one was never made. `_default_scalar` now returns `None` on a query error and the collector emits
  a `metrics-unavailable` warning hit naming how many of the seven agent/GPU signals it could not
  read, so the detectors say *blind*, not *clear*. Note this reverses a contract that a test had
  written down as intended behaviour (`test_scalar_exception_safe`, "verify the default path
  swallows errors → 0 → no hit"); it is renamed and inverted, and flagged for review.
- The detector engine had the same hole one level up, plus a guard that could never fire.
  `evaluate_trends` logged `trend_query_error` from an `except` block — but `_query_raw` returns
  its errors and does not raise, so a Prometheus outage could never reach that handler; the
  documented warning had never been emitted by the failure it exists for. `evaluate_trends` now
  reads `query_prometheus_series` (series, error) and, on an error, records `trend_blind_since` /
  `last_trend_error` and logs `trend_query_unavailable` instead of evaluating a trend against an
  empty series.
- `GET /v1/findings` now carries `predictive` (`active` / `blind` / `off`), `predictive_detectors`
  and `predictive_error` alongside `sensorium`. The two are independent claims — a connected watch
  stream says nothing about whether Prometheus answered — and `kq findings` no longer prints the
  green *"No findings · N detectors watching"* line while predictive detection is blind.
- **Two of the project's own refusals came back from a read verb as cluster state — one of them as
  a clean bill of health.** `_run`, the shared tail of all four ACI read verbs, separates "here is
  the cluster" from "here is why I could not look" purely by reading `run_kubectl`'s string, and it
  did so with a hand-kept marker list (`"blocked protected"`, `"requires confirmation"`, `"HITL"`,
  `"not permitted"`) plus `lowered.startswith("error")`. Measured 2026-08-20 against the real
  `run_kubectl`: `[Error] kubectl is not installed or not found in PATH…` returned **ok=True** with
  the error text as the body the model reads as cluster state, and `_health_from` found the word
  "error" in it and reported the target as **FAILED** — a verdict about a workload derived from a
  missing binary. `startswith("error")` never fired, because the string starts with `[`. Worse,
  `[Unsupported] 'kubectl edit' requires an interactive terminal which is not available…` also
  returned ok=True and read as **CURRENT**, because the phrase *"is not available"* contains
  "available" — a refusal reported as healthy. The three `[Protected]` refusals were caught only
  because their wording happens to contain "not permitted": a coincidence of phrasing, not a check.
  `_run` now classifies the string once through the shared `kubectl_output` reader, so a refusal or
  an error is always `ok=False` with the text in `error` and never in `body`. Separately,
  `_health_from` matched its status keywords as substrings anywhere in the body, so a Deployment
  named `error-budget-exporter` read as FAILED and a `crashloop-detector` as crashlooping; health
  words are now matched as whole whitespace-separated fields (splitting on whitespace only — a
  hyphen is part of a Kubernetes name, and splitting on it is what turned `error-budget-exporter`
  into the word "error").

- **The capability sandbox ran unbounded commands when it did not recognise the role — with the
  approval gate already switched off.** `run_as` (v5 P3 two-axis sandbox) executes with
  `hitl_bypass=True`: the app-level HITL prompt is deliberately given up there, on the stated
  grounds that the impersonated ServiceAccount's RBAC is the real guard. That trade only holds
  while the impersonation is definitely applied. It was not — an unrecognised role produced no
  flags, `as_impersonated` documented itself as a "no-op", and `run_as` executed the command
  anyway, unimpersonated, returning an ordinary output string that said nothing about it. Measured
  2026-08-20: `run_as("delete deployment web -n prod", "typo")` sent exactly
  `delete deployment web -n prod` to the seam, and against the real `run_kubectl` that command runs
  through to execution under `hitl_bypass=True` while the same command with `hitl_bypass=False`
  stops at the approval interrupt — so both guards were off at once, each because the other was
  presumed present. The role vocabulary makes it reachable by accident rather than by malice: this
  module's roles are `read-only` / `namespace-write` / `never-cluster-admin`, while the API-key
  roles used everywhere else in the codebase are `readonly` / `operator` / `admin` / `superadmin`,
  so passing `"readonly"` — the spelling the rest of the project uses — silently disabled the
  sandbox. A second hole: the command could bring its **own** identity. Real kubectl (v1.36.3,
  `kubectl options`) documents `--as-group=[]` as *"Group to impersonate for the operation, this
  flag can be repeated"*, so a command carrying `--as-group=system:masters` defeats the
  never-cluster-admin property whatever ServiceAccount the appended `--as` flag names. `run_as` now
  raises `SandboxContractError` — running nothing — for an unknown role, for a command that sets
  any of `--as` / `--as-group` / `--as-uid` itself (matched as whole tokens, so a label value like
  `team=--as-group` is not one), and as a last check if the impersonation flag is somehow absent
  from what would be executed. The pure flag builders keep their behaviour: `impersonation_args`
  still returns `""` for an unknown role, which is honest — there are no flags — and `run_as` is
  the seam that refuses to act on it.

- **The pre-apply validation gate reported "would apply cleanly" for commands the API server never
  saw — and could switch its own server-side check off.** `validate_mutation` (v5 P3 chokepoint) is
  the third link of validate → apply → verify, and it read `run_kubectl`'s prose the same way the
  other two did: `ok = not admission and "error" not in output.lower()`. Measured 2026-08-20 with
  the **real `run_kubectl`**: all five safety-gate refusals — readonly key on a write, operator key
  on a high-risk verb, protected namespace, cluster-wide mutation, terminal-only verb — contain
  none of those words, so every one produced `DryRunResult(ok=True, admission_denied=False)`: a
  claim that the API server and its admission chain accepted a command that was never sent. An
  unreachable cluster (`The connection to the server localhost:8080 was refused …`) and
  `run_kubectl`'s own `(no output)` placeholder took the same path. The flag handling had the
  mirror-image hole: `_with_server_dry_run` left the command untouched whenever the **substring**
  `--dry-run` appeared, so `--dry-run=none` (which real kubectl v1.36.3 documents as the default —
  *"--dry-run='none': Must be \"none\", \"server\", or \"client\""* — i.e. not a dry run), a bare
  `--dry-run` (*"deprecated and can be replaced with --dry-run=client"*), and an explicit
  `--dry-run=client` all suppressed the server-side validation while the result still claimed to be
  one; `kubectl label deploy/web team=--dry-run` tripped it from inside a label value.
  `DryRunResult` gained `validated`: false means the API server never answered, so `ok` and
  `admission_denied` are statements about nothing. `_with_server_dry_run` now matches whole tokens
  and rewrites any dry-run spelling to `--dry-run=server`. And `plan_mutation` downgrades an `auto`
  decision to `approve` when the dry-run did not run — an unrun check is not a passed check, and
  auto-execution is earned against evidence the server would accept the command. The production
  HITL gate in `kubectl_tool.py` runs the same test against `args`, a `list[str]`, so there it is
  exact token membership and `--dry-run=none` correctly still requires human approval; the defect
  was confined to this module.

- **The health oracle could not tell "not ready" from "I could not look" — and the executor rolled
  back on both.** `deployment_ready` (v5 P3 TNR verification rung) reads the cluster through
  `run_kubectl`, which returns a string and discards the exit code. Anything that is not a
  `kubectl get` table therefore has no READY column, the parser found no row, and the oracle
  answered `met=False, "deployment 'web' not found in 'prod'"` — the same verdict it gives a
  genuinely unhealthy deployment. Measured 2026-08-20 with the **real `run_kubectl`**: a read of a
  protected namespace returns `[Protected] Access to namespace 'kube-system' is not permitted`, and
  a machine without the binary returns `[Error] kubectl is not installed or not found in PATH`;
  both became *"deployment 'web' not found in 'prod'"* — a health verdict about a namespace the
  oracle never looked at. `execute_transactional` reads `met=False` as a failed mitigation and runs
  the rollback command, so an **instrument outage became a live mutation against a cluster we had
  just been told we cannot read**. Real kubectl's `The connection to the server localhost:8080 was
  refused …` and `run_kubectl`'s own `(no output)` placeholder took the same path.
  `PostconditionResult` gained `evaluated`: false means the oracle has no observation at all, so
  `met` must not be read as a verdict. `execute_transactional` answers that with the new
  `VERIFY_INCONCLUSIVE` — escalate, **never roll back** — while a read that succeeded and simply
  does not contain the row stays a real observation (`evaluated=True, met=False`). The
  string-reading itself moved into `app/tools/aci/kubectl_output.py` so the apply side and the
  verify side cannot disagree about what a given `run_kubectl` result meant. Same scope caveat as
  the entry below: this executor has no production caller yet.

- **A refused mutation was read as a successful one, and the executor then rolled back a change
  that had never happened.** `execute_transactional` (v5 P3 TNR, shipped default-off and listed in
  the v5 P3 entry below as "transactional apply→verify→auto-rollback") promises a mitigation
  either commits or leaves the cluster as it was. It decided whether the apply had happened with
  `not any(e in output.lower() for e in ("error", "exit=1", "not found", "forbidden", "invalid"))`
  — a substring scan over prose, on a seam whose input is a human-readable string because
  `run_kubectl` returns text and discards the exit code. Measured 2026-08-20 by driving the **real
  `run_kubectl`**: every safety gate in the project answers a blocked mutation with a marker
  string, and **all five read as SUCCESS** — readonly key on a write and operator key on a
  high-risk verb (`[Permission Denied]`), infrastructure namespace and cluster-wide mutation
  (`[Protected]`), a verb needing a terminal (`[Unsupported]`). End to end, a refused
  `kubectl scale deploy/web --replicas=3` produced `status: rolled_back` and issued
  `kubectl scale deploy/web --replicas=1` **against the live cluster**, undoing something that was
  never done: the safety gate's refusal was converted into a mutation. The reverse direction is as
  wrong and more likely — `deployment.apps/error-budget-exporter configured` is a successful apply
  whose *resource name* contains "error", so it was reported `apply_failed`, the postcondition
  oracle never ran, and the change stayed live and unverified. Real kubectl text was also missed
  entirely: `The connection to the server localhost:8080 was refused …` contains none of those
  five keywords. Replaced with `classify_apply()`, which reads KubeIntellect's own refusal markers
  and kubectl's error openers as **line prefixes** rather than substrings anywhere, and returns
  three outcomes: `APPLY_REFUSED` (nothing ran, nothing to roll back), `APPLY_FAILED` (kubectl
  rejected it), or applied — where the postcondition oracle, not a keyword, is the authority.
  Scope, stated plainly: `execute_transactional` has **no production caller** today (tests only),
  so this is a guarantee that could not have been delivered, not a cluster that was harmed.

- **Rollback points reported themselves armed while holding something that cannot be applied.**
  Before every mutating `kubectl` command the tool layer captures the target's current YAML and
  records it as a `rollback_point`; the digest listed them under *"Rollback points armed"*, the
  server logged `rollback_point_armed`, and `docs/flight-recorder.md` said recovery is "manual
  but mechanical: pipe the captured state into `kubectl apply -f -`". What is actually stored is
  `redact_secrets(yaml, max_chars=4000)`, and both of those transformations can destroy the
  object. Measured 2026-08-20 against real `bitnami/kubectl:latest` output at both ends: for a
  **Secret**, the line `kind: Secret` contains a redaction keyword and is dropped, so kubectl
  answers `error: unable to decode "STDIN": Object 'Kind' is missing` — nothing to restore; for a
  **ConfigMap** whose values are token-shaped, every value becomes `<redacted-token>` and the
  result is still **valid** (`kubectl label --local` accepts it as `configmap/app-config`), so the
  documented recovery **succeeds and overwrites the live configuration with placeholders** — a
  restore that destroys exactly what it was meant to protect; and any object over the cap (this
  project's own chart `values.yaml` is 7.4 KB) is cut mid-line and no longer parses. Redaction is
  not the defect and is not negotiable — the alternative is credentials in Postgres — so the
  capture is now compared against what kubectl produced and the record says which of the two it
  is: `restorable` plus `capture_notes` naming what changed. A capture that cannot be applied is
  still recorded, as evidence of what the object looked like, but the log says *"NOT restorable,
  do not apply it"* instead of *armed*, the digest section became *"Pre-mutation state captures
  (N of M restorable)"* with a per-entry verdict, and the postmortem timeline marks it. Records
  written before the field existed are reported as *unknown*, never promoted to armed.

- **The tamper-evident audit log could lose records and still verify as intact.**
  `docs/flight-recorder.md` promised there is "no way to edit, insert, or delete a record
  without the chain failing verification afterwards", and two paragraphs later stated that
  during a recorder outage "events are dropped, not buffered to disk". Both could not be true.
  Measured 2026-08-20 by driving the real recorder against a real `postgres:16-alpine`: with the
  `decision_log` table removed mid-episode, the in-process chain head advanced anyway, so the
  same process resumed at a skipped `seq` and `verify_chain` reported the episode as **broken** —
  `kq replay` exit 3, *"records may have been tampered with"*, permanently on the record for a
  database blip that altered nothing. Worse in the other direction: if the process restarted
  after the outage, the head was re-read from the database, the sequence came back contiguous,
  and the result was six rows, `seq 0-5`, **`chain_valid=true`** — with three recorded events
  silently gone and the postmortem printing *"✅ Audit chain verified intact — every event below
  is tamper-evident."* A third path made loss permanent: a failed chain-head lookup was swallowed
  and cached as a genesis head, so every later batch for that episode collided with
  `UNIQUE (episode_id, seq)` and was dropped for good, while the log blamed "duplicate key" —
  a symptom of its own retry, not the outage. The write path stays fire-and-forget (a recorder
  outage must never break a user response), but loss is no longer invisible: a failed batch drops
  the cached chain head instead of advancing it, so the chain stays contiguous and a blip is not
  reported as tampering; the count and the real cause are carried forward and written **into the
  chain** as a `recorder_gap` record the moment writes recover, where it cannot be removed without
  breaking verification. `kq replay` and `kq export` gained exit code **5** — chain intact, episode
  incomplete — and `kq postmortem` prints a **RECORD INCOMPLETE** banner beside the ✅ verdict with
  the number lost and why. *Intact* and *complete* are two claims, and only the first was ever
  a property of a hash chain.

- **The zero-token detection layer reported itself "active" while nothing was being watched.**
  `GET /v1/findings` returned `{"sensorium": "active", "detectors": N}` whenever a `DetectorEngine`
  object had been constructed — a fact about object lifetime, not about perception. Nothing
  anywhere tracked whether a `kubectl --watch` stream was connected. Measured 2026-08-20 by
  starting the real watchers on a host without kubectl: both watch tasks hit `FileNotFoundError`
  and **`return`** — that loop exits permanently and never reconnects for the rest of the process
  lifetime — and the endpoint still answered `{"sensorium": "active", "detectors": 20,
  "findings": []}`, which `kq findings` renders as the green line *"No findings · 20 detectors
  watching"*. Nothing was watching. An RBAC denial reaches the same silence by a different route:
  kubectl exits non-zero, the loop retries forever at a 60-second backoff cap, and `stderr` was
  sent to `DEVNULL`, discarding the one piece of information that explains it — `pods is
  forbidden: User "system:serviceaccount:…" cannot watch`. This is the layer that is supposed to
  notice trouble without an LLM, so an empty findings list from a deaf sensorium is the most
  expensive kind of silence. Each watch stream now records its own health — connected, permanently
  stopped, consecutive failures, and kubectl's own stderr as the reason, captured through a
  concurrent drain so a full stderr pipe can never block the child. `sensorium` became a real
  state (`active` only while a stream is connected, otherwise `disabled`, `starting`,
  `reconnecting` or `stopped`), the streams are reported alongside it, and `kq findings` prints the
  green all-clear **only** when the sensorium is genuinely watching — otherwise it says an empty
  result does not mean the cluster is healthy and lists each stream with its reason. The active
  path, the disabled path and the findings table are unchanged and asserted as such.

- **The morning digest said "Quiet watch" when nothing had been recorded.** `kq digest` is the
  operator's check on what the agent did overnight, so an empty result is only reassuring if the
  sources were readable. Measured 2026-08-20, four materially different states produced the
  identical, confident line *"Quiet watch: no findings in the last 24h."*: a genuinely quiet night;
  a `decision_log` query that raised (caught as `except Exception: rows = []`); **SQLite mode**, a
  supported and documented configuration in which — per `docs/flight-recorder.md` — there is no
  `decision_log` table at all, so the digest structurally cannot have data; and
  `FLIGHT_RECORDER_ENABLED=false`, where nothing is ever written. Only a missing connection pool
  was reported honestly. `kq digest` rendered that sentence and exited `0` in every case, so a
  night with recording switched off was indistinguishable from a night on which nothing went
  wrong. The digest now carries `degraded` and `degraded_reasons`, empty exactly when the digest
  is a real observation of the window; it names the setting an operator would change
  (`FLIGHT_RECORDER_ENABLED`, `USE_SQLITE`, `WATCHTOWER_ENABLED`) rather than only the resulting
  error; the summary leads with `Digest INCOMPLETE … This is NOT a quiet watch`; and the rendered
  markdown opens with a warning block before any section. Degraded does not mean suppressed —
  whatever was readable is still reported, and a partially-readable digest keeps its sections and
  still says so. A genuinely quiet watch is unchanged and is asserted as such. `kq digest`
  deliberately still exits `0`: it is a successful report of a degraded state, and scripts should
  branch on `degraded` in the JSON form.

- **A predicate type the schema, the docs and the detector-authoring prompt all treat as working
  is never evaluated.** Playbook `detect:` blocks accept three predicate types. `watch_predicates`
  are matched by `DetectorEngine.process()`; `trend_predicates` are evaluated by the periodic tick
  (ADR-010). **Nothing has ever read `DetectBlock.promql`** — verified 2026-08-20 across every
  module in the server package. It was nonetheless treated as real everywhere else:
  `parse_detect_block` accepted it as sufficient to make a block valid, `_is_detect_block` counted
  it when deciding a database row was a recompilable detector, and `authoring.py` told the
  NL-authoring model *"promql: list of instant PromQL strings (firing = non-empty result)"* — so
  ADR-012 could mint a PromQL-only shadow candidate that validates, is staged for human promotion,
  accrues no precision because it cannot fire, and would still never fire once promoted.
  `Finding.source` likewise documents an unreachable `"promql"` value. Stated honestly: all 21
  `promql:` queries in the shipped playbooks sit alongside real `watch_predicates`, so no shipped
  detector is dead and nothing that fires today stops firing — what was false is the additional
  coverage those queries appear to claim and the validity of a PromQL-only detector. This is the
  same shape as the `kind:` trap already documented in the playbook reference: it parses, loads,
  counts toward the detector total, passes the schema check, and matches nothing, ever. PromQL now
  does not on its own make a block valid — a PromQL-only block is rejected at parse time with a
  warning naming the reason — and the authoring prompt and error message say plainly that the key
  is recorded but not evaluated. The queries themselves are kept and their count is pinned by a
  test, so removing them has to be deliberate. **Evaluation has not been implemented**: that is a
  new capability with its own failure semantics, not an audit fix, and a test now fails
  deliberately if anything starts reading the field, so the docs cannot drift back out of date.

- **A cluster read that failed was reported to the model as a cluster that was empty and
  healthy — and to memory as a fix that worked.** `context_fetcher` pre-fetches pods and Warning
  events before every turn; its runner returned `proc.stdout or proc.stderr` and never looked at
  the exit code, so kubectl's error text was handed to the pod-table parser as cluster data.
  Measured 2026-08-20 against the real binary (`bitnami/kubectl:latest`), the two failure shapes
  produced two different lies. A connection failure prints three lines, two of which have enough
  whitespace-separated columns to be counted as pod rows ⇒ `pod_count=2`, a quantity invented out
  of an error message. A single-line failure — `error: You must be logged in to the server
  (Unauthorized)` — was consumed as the header row ⇒ `pod_count=0, has_issues=False`. Three
  consumers acted on that. (1) The prompt: `_snapshot_sufficiency_block` asserts *"the cluster
  snapshot above was fetched Ns ago and contains 0 pods. Health flags: issues=false,
  warnings=false"* and then instructs the model to **prefer answering directly from the snapshot**
  for exactly the questions "how many pods", "is the cluster healthy", "what's running" — measured
  with the pods read failing and the events read succeeding-and-empty, an ordinary asymmetric
  failure. (2) R4 post-fix verification: `_verify_resolution` documents "None if verification …
  failed to run", but nothing raised, so a failed read scanned clean and it returned
  `(True, "resolved")`, recording an unverified fix as verified — and `promotion.py` selects those
  rows (`WHERE verified = TRUE`) to mint learned rules and detector candidates. A cluster read is
  most likely to fail immediately after a disruptive change, which is precisely when R4 runs.
  (3) Playbook triggers ran their regexes over the stderr text. Each read is now checked by exit
  code and carries an `ok` flag; a failed read sets a new `snapshot_read_failed` state field,
  renders as an explicit **UNAVAILABLE** section that still shows kubectl's reason but is never
  labelled pod state, is never parsed for a count, is not matched against playbook triggers, and
  makes the sufficiency block assert no count and no health flags while requiring a fresh fetch.
  R4 returns `(None, None)` on a failed read. The healthy path is unchanged and is asserted as
  such.

- **Cluster identity was unresolvable in the only mode the chart ships, so every deployment
  wrote into a scope every other cluster reads.** `cluster_id.py` exists, in its own words, so
  that "patterns from a Kind dev cluster would pollute prompts on prod EKS and vice versa" —
  memory, episodes, learned failure patterns and findings are all scoped by the id it returns.
  Two of its three strategies shelled out to `kubectl config`, which needs a kubeconfig **file**.
  An in-cluster deployment has none: the chart sets `KUBECONFIG_PATH: ""` so kubectl
  authenticates with the pod's ServiceAccount. Verified 2026-08-20 against the real binary
  (`bitnami/kubectl:latest`, no kubeconfig): `kubectl config current-context` exits 1 with empty
  stdout, and `config view --minify` exits 1 with "current-context must exist in order to
  minify". Both strategies therefore returned nothing and identity fell through to the literal
  `"unknown"`. The module's docstring called that "a sentinel that read paths can filter out",
  which is the opposite of what the read paths do: `memory_store` recalls with
  `cluster_id IN ($1, 'unknown')` — deliberately, so pre-column rows still match — which makes
  the sentinel a **cross-cluster wildcard**. Two clusters sharing a database both wrote it and
  both read each other's rows, which is exactly the contamination the module was written to
  prevent, arriving by default and only in production: on a laptop a kubeconfig is present and
  identity resolves, so it looked correct in development. `docs/reflexion.md` further claimed
  the sentinel rows "age out via retention" so the system "naturally converges to per-cluster
  patterns only" — untrue in-cluster, where fresh `'unknown'` rows are minted continuously.
  Identity is now resolvable: a new `CLUSTER_ID` setting takes precedence over every probe
  (Helm: `config.clusterId`), and where it is unset the `kube-system` namespace UID — the
  conventional cluster identifier — is tried before giving up. The fallback still exists and
  still returns the sentinel, because filtering those rows on read would discard the legitimate
  data of every single-cluster deployment; it now logs a warning naming both the fix and the
  consequence, and `cluster_id_is_resolved()` lets callers tell a real identity from the
  sentinel. Two stale doc claims that the fingerprint hashes a "namespace count" were corrected
  — no namespace count has ever been part of it.

- **The kill switch an operator can see was not the kill switch the agent obeyed.**
  `GET /v1/v5/status` reports `kill_switch_engaged` — annotated in the response model as
  "⇒ all autonomous writes denied" — and `kq v5-status` prints it in red. But
  `auto_write_permitted()` returned **allow** when `KI_V5_BLAST_RADIUS_BUDGET` was false,
  *before* consulting the kill switch, and that flag defaults to `False`. Measured 2026-08-20
  through the real watchtower path in the default configuration: with the kill switch engaged,
  `kill_switch_engaged()` returned `True` (what the API and CLI report) while
  `watchtower._should_auto_fix()` returned `True` — the agent kept auto-fixing. A declared
  `KI_V5_CHANGE_FREEZE` was ignored the same way. The failure mode is the worst available for a
  break-glass control: an operator stopping the agent mid-incident was told it had stopped, and
  so did not reach for a real brake. The runtime toggle exists precisely so a stop needs no
  redeploy, yet it was inert unless an unrelated experimental flag had been enabled by env var
  beforehand. Both brakes now bind regardless of `KI_V5_BLAST_RADIUS_BUDGET`, which is left with
  no consumer at all: `gate_write` never read it either, so the flag's only effect in the whole
  codebase was to disable a brake. It is recorded as unwired in `UNWIRED_EXPERIMENTAL_FLAGS`
  rather than given an invented purpose. Default behaviour is unchanged — a deployment
  with no brake engaged sees the same ladder as before. Two existing tests asserted the defect
  (`auto_write_permitted().allow is True` with the switch engaged, commented "gate inactive ⇒
  ladder unchanged"); they now assert the corrected contract, alongside a new suite that checks
  the reported state against the actual write decision over all sixteen combinations of the four
  inputs, so the signal and the behaviour cannot drift apart again.

- **Cluster-scoped objects escaped the autonomy safety model, which is built on namespaces.**
  A Node, PersistentVolume or ClusterRole has no namespace, so a Warning event about one
  (`NodeNotReady`, `Rebooted`, `KubeletHasDiskPressure` — among the most common warnings in any
  cluster) reaches the watchtower with `namespace=""`. That fell through to the configured
  default rather than being pinned. Because `fnmatch("", "*")` is true and `*` is the natural
  way to write "all my namespaces" in an allowlist whose docstring advertises glob support, an
  operator who set `AUTONOMY_A3_ALLOWLIST=SomePlaybook/*` silently made **Nodes auto-fixable** —
  where an unattended remediation (cordon, drain, delete) is the least recoverable action the
  system can take. Measured 2026-08-20 by feeding `_event_observation` a real-shaped
  `NodeNotReady` event: `level_for_namespace("")` returned the default and
  `a3_allowed("NodeNotReady", "")` returned `True`. An unattributable namespace is now capped at
  A1 — investigated and reported, never mutated — with `a3_allowed` refusing it independently of
  the cap; the cap is a ceiling, so a deployment pinned to A0 stays at A0. Observation is
  unaffected. The ladder now also normalises namespace names (strip + lowercase) the way
  `run_kubectl` does, so the two components cannot disagree about which namespace is protected.

- **Ten of twelve `/v1` routes answered without an API key.** Authentication was a per-endpoint
  convention — each handler called `get_user_role(request)` itself — so it was enforced exactly
  where somebody had remembered it. Measured 2026-08-20 with auth enabled: `/v1/digest`,
  `/v1/findings`, `/v1/episodes/{id}/replay` (the flight recorder — every command run and its
  output), `/v1/episodes/{id}/postmortem`, `/v1/events/replay/{session}`, `/v1/namespaces`,
  `/v1/v5/status`, and the **read** halves of `/v1/detectors` and `/v1/preferences` all returned
  data to a request carrying no `Authorization` header; only `/v1/chat/completions` and
  `/v1/auth/demo-keys` challenged. In `detectors.py` and `preferences.py` a `_require_writer`
  helper gated every mutation and no read — the same "a read is a safe default" assumption found
  in `run_helm` and the Loki/Prometheus tools the day before. Authentication is now a dependency
  on the API router, so every route inherits it and a route added later cannot forget it;
  `/healthz` and `/readyz` are mounted on a separate public router because they must answer an
  unauthenticated kubelet. The documented open mode (no keys configured ⇒ every caller is
  `admin`) is unchanged, and the per-verb role checks in the tools are untouched.

### Fixed
- **An unreachable cluster was reported as a cluster with no namespaces.** `GET /v1/namespaces`
  shells out to `kubectl get namespaces` and never checked the return code, so an unreachable API
  server, an expired credential, an RBAC denial or a bad `KUBECONFIG_PATH` produced empty stdout
  and was returned as `200 {"namespaces": []}`. The wrong answer then travelled: `kq` validates
  `/ns <name>` against this list, and its REPL is deliberately careful — it distinguishes present,
  absent and *undetermined*, and only rejects on a definite absence so that a backend outage
  cannot block an operator. A `200` with an empty list is a definite absence, so the care was
  defeated and the REPL answered **"Namespace 'prod' not found in the cluster"** during exactly
  the incident where the operator's credentials had expired — pointing them at a deleted namespace
  instead of at their kubeconfig. The same empty list silently emptied `kq`'s namespace
  tab-completion. Measured 2026-08-20 end to end. The endpoint now returns **503** with the first
  line of kubectl's stderr (`FileNotFoundError` and a timeout included), so an empty list means
  one thing only; the protected-namespace filter is re-asserted by test so the new error handling
  cannot drop it.

- `kq`'s `fetch_namespaces` mapped any `200` to `body.get("namespaces", [])`, so a response it
  could not interpret — a missing key, a `null`, a gateway's own JSON — also became "zero
  namespaces" rather than "unknown". Only a genuine list now counts as an answer; everything else
  is `None`, which the caller already handles by failing open.

- `kq`'s health check reported `"did not respond within 5 s"` whatever timeout was in force, even
  though it is configurable via `KUBE_Q_HEALTH_TIMEOUT`. It now names the real value.

- **The database migration could not fail.** Every path that applies `schema.sql` ran `psql -f`
  without `ON_ERROR_STOP=1`. psql's documented default is to print an error, continue to the next
  statement, and **exit 0** — so a migration that applied nothing still reported success. Measured
  2026-08-20 against `postgres:16-alpine`, the image the chart's Job uses, running the real schema
  as a role without `CREATE` on `public` (the ordinary shape of a managed instance): **70
  statements failed, 0 of 18 tables were created, psql exited 0**. Kubernetes reads that exit code,
  so the `job-db-init` Job was marked `Succeeded` and `helm upgrade` reported `deployed`. Nothing
  downstream contradicted it — `/readyz` deliberately does not probe Postgres, and memory/recorder
  writes are fire-and-forget by design, so the product degraded silently with only an unwatched
  warning line in the server log. All five call sites now pass `ON_ERROR_STOP=1
  --single-transaction`, making the migration all-or-nothing rather than half-applied: the Helm
  Job, `make db-init`, the documented Alibaba RDS command, the schema header comment, and the
  documented **restore** command in `docs/operations.md` — where the same default meant a disaster
  recovery could report success and restore nothing. The `pip`/CLI path (`kubeintellect db-init`)
  was already correct: it uses psycopg, which raises, and it exits 1. A new suite asserts the flags
  on every shipped path, and that the two assumptions behind `--single-transaction` still hold (no
  `CREATE INDEX CONCURRENTLY`; every statement idempotent, since the Job re-runs on each upgrade).

- **The Helm chart shipped an unguarded manual copy of the schema.** `configmap-schema.yaml`
  embeds 456 lines of SQL literally rather than reading `schema.sql`, and the Job applies the copy.
  They are byte-identical today — verified — but nothing enforced that, and a stale-but-valid copy
  would apply cleanly and report success. Now gated by a test that diffs the two.

- The test harness claimed to force auth off and did not: `conftest.py` cleared three of the
  four key lists, so a local `.env` carrying a superadmin key (or `DEMO_KEY_HMAC_SECRET`) left
  `settings.auth_enabled` true while the comment said otherwise. Invisible until the routes
  began enforcing it, at which point nine tests failed with 401. All five inputs are now cleared.

- **`--all-namespaces` names every namespace, so it named none the guard could check.** The
  protected-namespace check asks which namespace a command names; a command naming *all* of them
  names none in particular, and for eleven passes of hardening it simply did not fire on `-A`.
  Measured 2026-08-20 against the real tool: `kubectl get pods -n kube-system` was refused while
  `kubectl get pods -A` returned the identical rows plus `kubeintellect` and `monitoring`;
  `kubectl get events -A` and `kubectl get configmaps -A -o yaml` likewise. Worse on the write
  side — `kubectl delete pods -n kube-system` was refused while `kubectl delete pods
  --all-namespaces` reached the approval prompt and, once approved, would have deleted pods in
  `kube-system`, `monitoring` and `kubeintellect`, the namespace KubeIntellect itself runs in;
  it composed badly with the fail-open approval gate fixed the same day. Cluster-wide
  **mutations are now refused for every role including superadmin**, with a message pointing at
  `-n <namespace>`. Cluster-wide **reads keep working and are filtered** — table, `-o wide`,
  `-o json`, `-o yaml` and `describe` drop entries from blocked namespaces and state how many
  were withheld. `-o name` and `-o jsonpath` carry no namespace to filter on and are refused
  rather than passed through, the same fail-closed choice made for an unparseable payload.

### Fixed
- `mkdocs build --strict` exited **0** on a broken intra-page anchor, reporting a link as
  resolved when it was not — measured by deliberately breaking one. `validation.anchors` (and
  unrecognized/absolute links, omitted files) is now raised to `warn`, which `--strict` turns
  into an error; verified red-green. No pre-existing link in the site was broken.

- **The two observability tools reached the same cluster's data with no blocklist at all.**
  Four tools are registered for the agent. `run_kubectl` and `run_helm` reach the cluster
  through a command line and gate on `-n`; `query_loki` and `query_prometheus` reach the same
  data through a *query language*, where the namespace is a label matcher, and enforced nothing.
  Measured 2026-08-20 against the real tools: `{namespace="kube-system"}`,
  `{namespace="kubeintellect"} |= "key"`, `{namespace="monitoring"} |~ "token|password"`,
  `rate({namespace="cert-manager"} |= "error" [5m])` and `kube_secret_info{namespace=
  "kubeintellect"}` all executed and returned their data. Loki is the sharper end: `kubectl logs
  -n kube-system` is refused precisely because logs carry credentials in plaintext, and
  `query_loki` advertises itself as the better way to read logs. Both tools now gate twice — the
  query is refused if it positively selects a blocked namespace (negative matchers are not
  mistaken for selection), and every returned stream/series is dropped if its own `namespace`
  label is blocked, which also catches `{app="nginx"}` matching a pod in `kube-system`. The
  response states how many results were withheld. The detector engine's
  `query_prometheus_range_raw` is deliberately exempt: its PromQL comes from human-reviewed
  playbooks and is meant to watch `kube-system`. Residual documented in `docs/security.md`: a
  result with no `namespace` label passes, so an aggregation that discards the label can still
  return a scalar computed partly over a blocked namespace.

- **The agent had a second way to reach the cluster, and it enforced neither blocklist.**
  `run_helm` is read-only against the cluster — its verb check is an allowlist, so it can never
  mutate a release — but read-only against the *cluster* is not read-only against *what may be
  read*. It applied no namespace blocklist and no resource blocklist, so it answered in
  protected namespaces questions `run_kubectl` refuses for every role. Measured 2026-08-20
  against the real tool: `helm list -n kube-system`, `helm get values kubeintellect -n
  kubeintellect`, `helm get manifest web -n prod`, `helm get all prometheus -n monitoring` and
  `helm status cert-manager -n cert-manager` all executed. `helm get manifest` renders the
  release's own `kind: Secret` objects with their base64 `data:` intact — precisely what
  `kubectl get secret` is refused for unconditionally. Separately, `GET /v1/namespaces` runs its
  own `kubectl get namespaces` and returned the blocked namespaces in full, because the pass that
  added the namespace output filter added it to the tool and not to the route. `docs/security.md`
  stated both guarantees about the product while one of three code paths enforced them. All three
  now share one definition, read from `KUBECTL_BLOCKED_NAMESPACES` rather than copied.
  `helm list -A` is filtered in table, JSON and YAML, failing closed on an unparseable payload.

### Fixed
- `run_helm` read its subcommand as `tokens[1]`, so `helm -n prod list` was rejected as an
  unsupported subcommand. Behind the allowlist this was a usability bug rather than a bypass —
  the same parser defect that was a *bypass* in `run_kubectl`'s deny-list (fixed 2026-08-13).
  It now uses the shared flag-aware parser. Recorded in `docs/security.md` as the argument for
  preferring allowlists: an allowlist turns a parser bug into a complaint, a deny-list turns the
  same bug into a hole.
- `docs/security.md` layer 6 carried a dangling half-sentence left by an earlier edit.
- **A HITL denial that was not one of 13 exact phrases executed the command.** The paused graph
  was resumed with `Command(resume=not is_denial(user_message))`, so approval was the default and
  only a recognised *denial* prevented execution. `is_approval()` already existed and nothing
  called it for this decision. Measured 2026-08-20 by driving the real `stream_events` with a
  thread paused at an interrupt: `"No."` (with a full stop), `"NO!"`, `"no thanks"`,
  `"don't do that"`, `"cancel it"`, `"stop it"`, `"not yet"`, `"wait"`, `"hold on"`, `"why?"`,
  `"what will that do?"` and an **empty message** all resumed with `True` and ran the destructive
  command. `docs/security.md` has always documented the opposite — *"anything else → treated as
  denial"* — so the published contract was false in the fail-open direction, on the last gate
  between an LLM and a destructive cluster operation. The resume value is now
  `is_approval(msg) or is_auto_approve_request(msg)`; case, surrounding quotes and trailing
  `.`/`!` are normalised so `No.` and `YES!` read as intended; an unrecognised reply cancels and
  logs a warning naming it.

### Fixed
- **`kubectl rollout restart|undo|pause` armed no rollback point**, despite
  `docs/flight-recorder.md` promising one before *"every mutating `kubectl` command"*. The
  arming condition still used the enumerated `_HIGH_RISK | _MEDIUM_RISK` deny-list after the
  approval gate had moved to `_is_write_verb`, so the two consumers of "is this destructive"
  disagreed; any verb this build does not know also armed nothing. Both now use the same test.
  Two silent no-ops inside the capture are fixed as well: `rollout` puts a subcommand before its
  target, so the pre-state read was built as `kubectl get restart deployment/api -o yaml`, and
  `kubectl label pod api-1 tier=web` kept `tier=web` as a resource name — both rejected by
  kubectl and both swallowed by the deliberately best-effort wrapper, arming nothing while
  appearing to arm something.

### Security
- **The namespace listing filter worked in three of its six output formats.** A bare
  `kubectl get namespaces` is deliberately allowed *because* blocked namespaces are stripped from
  the result. Measured 2026-08-20 with a `readonly` key: the default table, `-o wide`, `-o name`
  and `-o jsonpath` filtered correctly, while **`-o json`, `-o yaml` and `-oname` returned the
  blocked namespaces in full**, and **`kubectl describe namespaces` was not filtered at all**.
  Three separate causes in one function: the `-o` reader did not accept pflag's attached
  shorthand (the same gap fixed for `-n`, now sharing one `_flag_value` parser so they cannot
  drift again); `json`/`yaml` returned the payload unchanged behind the comment *"too complex to
  strip reliably; blocked at execution anyway"*, whose second half was false — nothing blocks a
  bare listing at execution; and the filter passed a hardcoded `"get"` to `_extract_resource_type`,
  so for any other verb the resource came back `None` and the filter returned early, handing back
  the labels, annotations and quotas of every namespace. `-o json`/`-o yaml` are now parsed,
  filtered and re-serialised, and a payload that cannot be parsed is replaced rather than
  returned unfiltered. What leaked was namespace names and metadata, not credentials.

### Security
- **`kubectl apply -f <path|URL>` and `-k <URL>` ran with the manifest never reaching
  KubeIntellect.** Pass-55's fix taught the protected-access checks to read a manifest on stdin;
  these forms put it somewhere the process cannot reach at all. Measured 2026-08-20 for
  `operator` and `admin`, `kubectl apply -f /tmp/payload.yaml`,
  `kubectl apply -f https://example.com/m.yaml` and `kubectl apply -k https://github.com/…` all
  executed. Three properties failed together: the protected-resource and protected-namespace
  checks saw a command naming neither, so a Secret or a write into `kube-system` was invisible to
  both; the approval prompt carried `stdin: null` and a `human_summary` that was just the command
  line, so the approver had nothing to review — and for a URL the content did not exist yet at
  approval time, since kubectl fetches it afterwards; and that fetch is unreviewed outbound
  network access from the KubeIntellect pod. Any `-f`/`--filename`/`-k`/`--kustomize` whose value
  is not `-` is now refused with a message pointing at the supported stdin form, in every
  spelling pflag accepts (`-f x`, `-f=x`, `-fx`, `--filename=x`). `kubectl logs -f` is
  unaffected — there `-f` means `--follow`.

### Security
- **A manifest on stdin was invisible to both protected-access checks.** `kubectl apply -f -`
  names neither a resource nor a namespace on the command line — they are the manifest's `kind:`
  and `metadata.namespace:` — and every check parsed argv. Measured 2026-08-20 for `operator` and
  `admin`: applying a Pod whose `metadata.namespace` was `kube-system` reached an ordinary
  approval prompt, while `kubectl apply -f - -n kube-system` was refused outright; a
  `kind: Secret` manifest was likewise only prompted, while `kubectl create secret generic` was
  refused. This is the form KubeIntellect itself recommends — the `kubectl edit` rejection message
  points users at `kubectl apply -f -` with stdin. Both checks now read the manifest as well as
  argv, walking every document in a multi-document stream and the items of a `kind: List`, and
  the superadmin re-check uses the same manifest-aware helper so that role does not get the
  closed bypass handed back. Scope is deliberately the manifest's `kind` and
  `metadata.namespace` and nothing deeper: a Pod that mounts a Secret in its own namespace is
  what Pods are for and still applies.

### Security
- **`kubectl -nkube-system` reached a protected namespace that `kubectl -n kube-system` could
  not.** kubectl parses flags with pflag, which accepts a shorthand's value attached to it —
  `-n kube-system`, `-n=kube-system` and `-nkube-system` are the same command. `_extract_namespace`
  read only the spaced form and `--namespace=`, so for the other two it returned `None` and the
  protected-namespace check never ran its comparison: the guard did not decide the namespace was
  permitted, it never learned there was one. Measured 2026-08-20 through the real tool,
  `kubectl get pods -nkube-system` **ran** for `readonly`, `operator` and `admin` — all three of
  which are refused the identical command written with a space — and an admin's
  `kubectl delete pod x -nkube-system` was downgraded from an outright `[Protected]` refusal to an
  ordinary approval prompt. All five spellings are now equivalent, `superadmin` keeps its
  documented bypass, and an unprotected namespace still works in every form.

### Fixed
- **Three documentation surfaces understated the infrastructure-namespace block.**
  `docs/security.md` said *"read-only verbs always allowed"* in the HITL sequence and titled its
  matrix row *"Writes to infrastructure namespaces"*, and `docs/architecture.md` said *"infra
  namespace writes blocked"* for `admin`. The code blocks **all** access including reads
  (`kubectl get pods -n kube-system` is refused for every role but `superadmin`), which is the
  stronger and intended behaviour — a Secret is reachable through a Pod spec, an Event or a
  ConfigMap, not only through `kubectl get secret`. The docs now say so.

### Security
- **The command gate was a deny-list, so every kubectl verb it did not name counted as a read.**
  `DESTRUCTIVE_VERBS` enumerated 13 verbs; kubectl has many more. Measured through the real tool
  with a **read-only** API key, all of these executed with no approval prompt: `label`,
  `annotate`, `rollout restart`, `rollout undo`, `cp`, `debug`, `expose`, `autoscale`,
  `port-forward` and `attach`. `kubectl cp prod/api-1:/etc/creds /tmp/` copies files out of a
  container — mounted Secrets included — and `kubectl debug node/node-1` starts a privileged pod
  on the node, so the two worst cases were readable by the role explicitly defined as unable to
  read Secrets. The default is now inverted: `_READ_ONLY_VERBS` is an **allowlist** and anything
  absent is treated as a write, so a verb introduced by a future kubectl release arrives blocked
  rather than pre-approved. `rollout` is judged by its subcommand — `status` and `history` stay
  available to a read-only key, `restart` / `undo` / `pause` / `resume` do not. `cp` and `debug`
  are classified high-risk. The ACI bounds guard (`is_read_only`) delegates to the same function
  instead of keeping its own copy, which fixes the same `rollout restart` hole there.

### Security
- **kubectl's own alternative spellings walked through the credential block.** The blocklist
  compares literal strings, so `kubectl get sa` — the documented short name for
  ServiceAccounts — and the fully-qualified `kubectl get secrets.v1.` / `serviceaccounts.v1.`
  form returned the objects, as did `sa/default`. Resource tokens are now normalised (short
  name, `resource.version.group` suffix, `resource/instance`, case) before being matched, at
  both comparison sites including the superadmin re-check. Unrelated CRDs that merely start
  with the same letters (`secretstores`, `sealedsecrets`) are unaffected.
- **A protected namespace named as the command's target was only prompt-gated, not blocked.**
  `kubectl delete pod x -n kube-system` was refused outright, but `kubectl delete namespace
  kube-system` reached a human approval prompt instead — the same protected namespace, and the
  documentation says infrastructure namespaces are blocked including reads. The namespace guard
  now also reads a positional target. Listing namespaces still works (the output is filtered),
  and deleting an ordinary namespace is still a normal approval-gated operation.
- **Reordering a kubectl command bypassed every safety gate, including the read-only role.**
  The subcommand verb was parsed as the second token, so `kubectl -n prod delete deployment api`
  — as valid as the canonical order, and a form an LLM writes routinely — parsed its verb as
  `-n`, a token in no risk set, no role set and no rejected set. Every check in `run_kubectl`
  keys off that value, so all of them fell together. Measured through the real tool: a
  **read-only** API key could delete Deployments and PersistentVolumeClaims and drain nodes,
  destructive commands executed with **no approval prompt**, and six of eleven ordinary ways of
  writing a Secret read returned the Secret. The verb and resource parsers now skip global flags
  wherever they appear, and, as defence in depth, a destructive verb appearing anywhere in the
  command is gated even if the parse misses it — matched on whole tokens, so `-l app=delete`
  does not trip it. No configuration change is required. All 1093 pre-existing tests passed
  before and after: every one of them writes the canonical order, which is why nothing caught it.
- **Tuning the kubectl blocklist silently unblocked every Secret in the cluster.**
  `KUBECTL_BLOCKED_RESOURCES` (Helm: `config.blockedResources`) *replaces* its list rather than
  extending it, and `values.yaml` said *"Override to add tenant-specific or environment-specific
  namespaces"* — so an operator following the documentation and adding `configmap` removed
  `secret`. Verified through the real `run_kubectl`: reading every Secret in a namespace, listing
  ServiceAccounts, and reading **this release's own API keys** all went from blocked to allowed,
  with no warning anywhere, while the guard still answered with its promise that Secrets are
  *"shielded from inspection to protect cluster credentials"*. The four credential types
  (`secret`, `secrets`, `serviceaccount`, `serviceaccounts`) are now re-added unconditionally via
  `ALWAYS_BLOCKED_RESOURCES` and cannot be configured away; operator additions still apply.
  Namespaces deliberately keep **no** floor — letting the agent investigate `monitoring` is a
  legitimate choice — but the values file now states the replace-not-merge semantics instead of
  inviting the mistake. Requires no action on upgrade; a deployment that had narrowed the list
  regains credential protection automatically.

### Fixed
- **A server crash mid-answer made `kq -q` exit `0`.** When the chat stream raised, the failure
  path ended it with the *same* frames a successful answer ends with — a `finish_reason: "stop"`
  chunk then `[DONE]` — and put the reason in `content` as `[Error: …]`. Prose is not a signal:
  `run_single_query()` scores any non-empty text as an answer, so in a script or CI job a crashed
  turn was indistinguishable from a real result, and a **partially streamed diagnosis was
  presented as a complete one**. The stream now also emits a `ki_event` of type `error` with
  **`fatal: true`**, and `kq` discards the partial answer and exits non-zero. `fatal` is what
  separates this from the error events emitted when one tool fails and the agent recovers and
  answers anyway — those still count as answers. The `[Error: …]` content chunk is retained so
  OpenAI-compatible clients that ignore the side channel still see a reason rather than an
  unexplained empty completion, and the `error` event type is now documented in the API
  reference (it never was).
- **The Helm chart's rolling-update drain never worked, and four places documented it as if it
  did.** The chart, the deployment template, `app/core/readiness.py`, `app/api/v1/endpoints/health.py`
  and the public API reference all described the same sequence: `SIGTERM` flips `/readyz` to `503`,
  Kubernetes stops routing, then the pools close. Sending a real `SIGTERM` to a real server and
  probing it showed the transition is `200` → **connection refused**: uvicorn closes its listening
  socket first and runs the application's shutdown hook last, so the `503` is never observable and
  a request arriving on a not-yet-updated kube-proxy route is *refused* rather than served — worse
  than the problem the flag was added to fix. The chart now carries a **`preStop` sleep**
  (`drainSeconds`, default `5`), which Kubernetes runs *before* `SIGTERM` and which actually holds
  the socket open while the Endpoints removal propagates. `set_ready(False)` is kept as honest
  in-process state but no longer claimed as a traffic-control mechanism, and every one of those
  five descriptions was corrected. New `tests/test_chart_shutdown_contract.py` pins the hook and
  the arithmetic — both failure modes (no hook; grace period at or below the drain) are otherwise
  silent outside a cluster under load.
- **The other half of the coordinator's memory context was still silent.** The previous entry made
  episode recall report its own outage; `load_memory_context()` — the V4 pinned block carrying
  operator preferences, failure hints, session notes and past RCA — still returned `""` on any
  failure. `""` is exactly what a brand-new user with nothing stored produces, so a Postgres outage
  reached the model as a clean slate: no preferences to honour, no precedent to build on, and no
  signal anywhere. It now raises `MemoryStoreUnavailable`, and the node injects the same explicit
  **"Memory unavailable"** block rather than an empty string. `USE_SQLITE` returning `""` is a
  configured state, not an outage, and is unchanged.
- **`GET /v1/preferences` answered `200` with an empty list when the store was unreadable**, so
  `kq preference list` printed `No preferences remembered` during a database outage — inviting the
  operator to re-enter preferences that already existed. It now answers **503**, matching
  `GET /v1/detectors`. Unlike episode recall, no agent path depends on this read, so there is
  nothing to fail open for.

- **A memory outage reached the model as "this cluster has no history".** When both recall channels
  failed, `recall_episodes()` returned `[]` — the same value as a genuine zero-recall. Downstream,
  `render_recall_block([])` returns `""`, so the triage prompt simply **omitted** the "Similar past
  episodes" section, and the log line read `episodes=0` exactly as it does when nothing matched.
  Neither the model nor the operator could tell that recall had failed, on the one capability this
  product is differentiated by; a Postgres blip silently degraded every investigation to
  memoryless, and absence of recalled precedent reads as absence of precedent.

  Recall now raises `MemoryUnavailable`. The agent turn still survives — an investigation without
  memory beats no investigation — but the injected context carries an explicit **"Memory
  unavailable"** block telling the model that prior history could not be checked and must not be
  reported as absent, and the log line gains `degraded=true`. A genuine empty recall is unchanged
  and still injects nothing.

  Same change fixes a second, quieter failure: `regenerate_file_plane()` fed that `[]` straight into
  the L0 projection, **overwriting a good `CLUSTER.md` / `MEMORY.md` with an empty one** on any
  recall error. It catches the new exception and leaves the previous projection intact.

- **An unreadable detector store reported "no detectors" instead of an error.**
  `review.list_detectors()` returned `[]` both when no memory pool was configured and when the
  query raised, so `GET /v1/detectors` answered `200 {"detectors": []}` in either case and
  `kq detector list` printed `No detectors.` and exited `0`. An operator asking what watches their
  cluster was told *nothing does* when the truth was that the question had not been answered — and
  the only trace was a server-side log line they would never see.

  The store now raises `DetectorStoreUnavailable`, which the endpoint translates to **503**. An
  empty list means exactly one thing: the store was read and holds nothing. This follows the
  pattern `GET /v1/findings` already used, where `sensorium: disabled` is reported rather than
  disguised as an innocent empty result.

- **`kq detector new` exited `0` when the detector was rejected.** A description the compiler
  refuses comes back as a normal `200` carrying `staged: false` and the compile errors — a valid
  response, not an HTTP failure — so `raise_for_status()` passed and the command returned success.
  The human-readable output was honest (`Not staged.` plus each error); only the machine-readable
  status lied, which is the half a script reads. `kq detector new … && kq detector promote …`
  proceeded as though a detector existed.

  It now exits **`3`** when nothing was staged, and the exit table is documented in
  `kq detector --help` and the CLI reference. This matters most for NL-authored detectors
  specifically: the author writes plain English and cannot read the compiled predicate to check
  whether it survived.

- **`kq replay` reported an unverified hash chain as intact.** The flight recorder is hash-chained
  so tampering with stored records is detectable, and `kq replay` is the command that detects it.
  The server sends its verdict as a `replay_meta` SSE frame — but the SSE reader silently discards
  any frame it cannot parse (`except json.JSONDecodeError: pass`), and an older or proxied server
  may not send one at all. In that case the command printed **no verdict line whatsoever** and
  returned **`0`**, which the documented contract defines as *"chain intact"*. Absence of evidence
  was being reported as evidence of integrity, so a script gating on `kq replay … && promote` could
  not tell a missing check from a passed one.

  It now fails closed with a new exit code **`4` — chain NOT VERIFIED**, and prints an explicit
  warning that the rendered records are unverified. Exit `0` still means verified intact and `3`
  still means broken; both were re-asserted by test. Verified empirically against four streams
  (valid, tampered, truncated verdict frame, absent verdict frame).

- **The server reported experimental features as active that no code implemented.** 25 of the 60
  declared `KI_V5_*` / `CORTEX_V5_*` flags are read by nothing — they were written when the
  configuration surface ran ahead of the implementation. Because `active_experimental_flags()`
  reported *any* true boolean carrying an experimental prefix, setting one of them made
  `GET /healthz`, `GET /v1/v5/status`, `kq v5-status` and the startup log line all state that the
  slice was on. An operator could enable `KI_V5_RIGHTSIZING`, read back that rightsizing was
  active, and be wrong — on precisely the surface used to confirm a rollout.

  Those flags are now excluded from the active set and reported separately under
  **`set_but_unwired_flags`** (both endpoints, plus `[set but NOT WIRED, no effect: …]` in the log
  line), so a setting that does nothing is visible rather than either misreported or silently
  swallowed. The list lives in `app/core/version.py` and is checked against real `settings.<FLAG>`
  usage by `tests/test_v5_flag_wiring.py`, so it can only shrink: wiring a flag without removing
  its entry fails the suite, and so does adding a new unwired one. `docs/v5-experimental-flags.md`
  marks each affected row.

  `kq v5-status` shows the same information as a red `set_but_unwired_flags` row, rendered only
  when non-empty — otherwise excluding those flags from `active_flags` would have replaced a wrong
  answer with a missing one, and the operator would read `(none — v4 baseline)` after setting a
  switch. A newer `kq` against an older server tolerates the absent field.

  All 10 `MEMORY_*` booleans were verified wired; the gap was confined to the v5 track. No
  behaviour changes for anyone who had not set one of the 25 flags — the default install reports
  an empty list exactly as before.

### Fixed
- **The agent's loop bound existed where it could not do harm, and was missing where it could.**
  The read-only RCA subagents were capped at 50 recursion units (~16 tool calls), but the
  **coordinator** — which holds the write-capable toolset — and the outer graph both inherited
  LangGraph 1.x's default `recursion_limit` of **10007** (~3,300 ReAct steps). `GraphRecursionError`
  was caught nowhere in the codebase, so exhausting that budget destroyed the entire turn with an
  uncaught exception instead of returning what had already been found.

  Both loops now carry an explicit, configurable budget — `AGENT_GRAPH_RECURSION_LIMIT` (120) and
  `AGENT_COORDINATOR_RECURSION_LIMIT` (150) — and **exhaustion halts and escalates to the operator
  with the partial result**, on both the single-turn and the streaming path. The defaults are a
  runaway backstop set well above observed usage, not a tuned budget.

  Highest-exposure path: an auto-approve session (`hitl_bypass`), where writes execute unprompted.
  The always-confirm set still gated the largest blast radius — cascading deletes of
  `namespace`/`pv`/`crd`, and `set image`/`set resources` — so this was never an unbounded-destruction
  bug; it was an unbounded *loop* issuing medium-risk writes with no ceiling and no clean failure.

  9 new tests, red-green verified: with the fix reverted, 4 of them fail — the exhaustion test with
  exactly the uncaught `GraphRecursionError` that was the defect.
### Changed
- **The contributor roster now names everyone who has contributed.** `.all-contributorsrc`
  listed 5 people while the README credited 8, and one contributor —
  [@Chris7717](https://github.com/Chris7717), who wrote the `HPANotScaling` playbook (#114)
  and filed the `.env.example` report (#115) — appeared on neither. Four entries added:
  [@floze-the-genius](https://github.com/floze-the-genius) (#109),
  [@Priyanshu608](https://github.com/Priyanshu608) (#108),
  [@Chris7717](https://github.com/Chris7717) (#114, #115) and
  [@AshSgDe29071999](https://github.com/AshSgDe29071999) (#107).

  #107 did not merge — it collided with an already-claimed issue — and it is listed anyway.
  The roster records the contribution that arrived, not whether it happened to land: running
  that branch as a control is the only reason we know the `pytest_configure` hook in #109 is
  load-bearing rather than incidental.

  The two surfaces are now in sync at 9 people. Nothing keeps them that way yet — see #167.

### Fixed
- **Every text-mode read and write now names its encoding, and a gate keeps it that way**
  (#156, #161) — thanks to [@shaurya703](https://github.com/shaurya703). A bare
  `read_text()` / `write_text()` / `open()` decodes with the *platform default*, which is
  CP1252 or CP936 on Windows and ASCII under the POSIX `C` locale. That is the bug #136 hit
  in the playbook loader, where a non-UTF-8 locale made the loader silently **drop** a
  playbook containing em-dashes; #138 fixed that one call site and left the class alive at
  62 more, several of them read-modify-write cycles on the user's own `.env` and config
  files — where a decode failure does not crash, it writes mangled content back.

  All 62 are fixed, across the whole CI-linted scope rather than only the three files #156
  enumerates: a gate that exempts tests and probes is a gate with a carve-out, and those are
  the places this class survives in. The frozen `v1/`–`v3/` are untouched (ADR-001/002).

  `scripts/check-text-encoding.py` holds the class, with `make check-encoding` and a step in
  the existing `Syntax warnings` CI job. Like `check-syntax-warnings.py` it is stdlib-only
  and deliberately **independent of ruff and of the `<0.16` pin**, so the blind spot that pin
  creates cannot reach it. Ruff's `PLW1514` covers the same rule and would replace it once
  the pin lifts and the linted scope widens.

  The gate is precise about what it does *not* flag, because a red run whose printed fix
  breaks your code is worse than no gate: `webbrowser.open(url)`, `zipfile.ZipFile(z).open(m)`
  and the compression modules' binary default are left alone, an encoding passed positionally
  is accepted, and an explicit `gzip.open(p, "rt")` is still caught. A source file it cannot
  decode is **reported**, never silently skipped.

### Security
- **`nanoid` bumped 3.3.17 → 3.3.18 in `v4/packages/kube-q/web`** (GHSA high: custom generators
  can loop indefinitely when size is zero). Transitive via Next.js/postcss in the web PTY relay.
  This was the **only** open Dependabot alert affecting `v4/` — `npm audit` now reports **0
  vulnerabilities** for that tree.

  The other 137 open alerts are all in `v1/`–`v3/`, which are frozen by ADR-001/002 so the
  published paper's results stay reproducible. Per the existing `SECURITY.md` policy ("About
  dependency alerts in `v1/`–`v3/`"), those are an accepted trade-off: nothing from `v1/`–`v3/`
  is published to any registry and none of it is deployable. They have been dismissed as
  *tolerable risk* with that justification, so the security tab now reflects the written policy
  instead of contradicting it.

### Added
- **CI now runs the frozen arms' test suites** — `Tests (v2 · frozen)` and `Tests (v3 · frozen)`
  (#155). The frozen generations are not developed any more, but shared fixes still land in them
  (the UTF-8 playbook-loader fix did, in both), and nothing in CI had ever run their regression
  tests. Each arm resolves from its own lockfile rather than the v4 workspace.

  Fixing the gate exposed that **v2's suite could not run at all** — for anyone, including on a
  maintainer's own machine. Five test modules import `evaluation/`, the offline scoring harness
  that produced the campaign numbers; it is deliberately not part of this repository, so on any
  clone those imports fail. Four of them fail at *module scope*, which does not break four
  modules — it aborts collection for the **whole suite**, so the other thirteen never ran either.
  Guarding the five with `pytest.importorskip` takes v2 from `4 errors during collection`,
  **0 tests run**, to **259 passed, 5 skipped**. The guard is conditional, not a deletion: where
  the harness is present, all five modules still run.

- **CI now gates the demo front-end** (`Web (lint + build)`). `v4/packages/kube-q/web` had no
  gate of any kind: every existing job is scoped to the Python tree, so nothing in CI had ever
  run `npm ci`, `eslint` or `next build`. The gap was not theoretical — the eslint 9 → 10 bump
  (#142) reported **all 15 checks green** while `npm run lint` failed outright with
  `contextOrFilename.getFilename is not a function`, because eslint 10 removed the legacy
  rule-context API that `eslint-plugin-react` (vendored inside `eslint-config-next@16.3.0`)
  still calls. The job runs `npm ci` rather than `npm install` so a lockfile that disagrees with
  the manifest fails instead of being silently re-resolved.

### Changed
- **ruff 0.16 backlog: 362 → 210 findings** (#75), clearing every modernization family —
  `UP045`/`UP006`/`UP035`/`UP037` (typing syntax), `I001`, `PIE810`, `RUF059`, `RUF012`,
  `FURB162`, `FURB188`, `RUF015`, `SIM102`/`SIM113`/`SIM114`, `C408`, `F401`, `F841`,
  `ISC004`, `PLR0124`. Both suites (1047 + 317), mypy and the pinned lint gate stay green.

  **One finding in that set was a trap, and it is now guarded.**
  `ruff --select UP045 --fix` rewrites the canary inside
  `tests/test_injected_config_invariant.py` from `Annotated[Optional[RunnableConfig],
  InjectedToolArg]` to `Annotated[RunnableConfig | None, InjectedToolArg]` — **and the suite
  still reports 8 passed.** Neither spelling is in langchain_core's match list, so the canary
  goes on reporting `None` either way; the autofix leaves a green test that no longer exercises
  the exact form AGENTS.md invariant #6 forbids. That line now carries a suppression and the
  reasoning, matching the existing one on `app/agent/nodes/coordinator.py`, where the same
  rewrite would silently stop the run config being injected and take `user_role` and
  `hitl_bypass` with it.

  Also fixed while in there: `ki_protocol/events.py` caught `(ValidationError, Exception)`,
  which is exactly `except Exception` because `ValidationError` is a subclass — the tuple only
  made the intent read narrower than it was. The breadth is deliberate for that decoder, so it
  is now stated plainly.

  The `ruff<0.16` pin stays for now. The remainder is **141 `BLE001`** (blind-except, mostly
  deliberate CLI and agent boundary handlers) and **30 `PLW1510`**; both are project-wide policy
  calls rather than cleanup, and are tracked in #75.

### Fixed
- **The published container image could not start.** `docker run` on any released image failed
  immediately with `No module named uvicorn`, and it had been that way since the image was
  introduced. The builder stage resolved dependencies on `uv:python3.12-bookworm-slim` while the
  runtime stage ran `python:3.13-slim`. The venv is copied wholesale between them, but a venv's
  packages live at the version-stamped `lib/python3.X/site-packages` and its `bin/python` is only
  a symlink to `/usr/local/bin/python` — which resolves to the *runtime* interpreter. So Python
  3.13 looked for `lib/python3.13/site-packages`, found only `lib/python3.12`, and started with
  **no site-packages on `sys.path` at all**.

  Nothing caught it because nothing ever ran the image. `docker-publish.yml` builds, pushes and
  writes a summary; a `docker build` that succeeds proves only that the layers assembled. This is
  the same shape as the `kubeintellect --version` regression that shipped to PyPI under a green
  install-smoke job.

  The builder is now `uv:python3.13-bookworm-slim`, matching the runtime. Two guards were added
  so it cannot recur silently: the Dockerfile asserts at build time that the copied venv matches
  the running interpreter and that `uvicorn`, `fastapi`, `pydantic_core` and `pydantic_settings`
  import (this travels with the Dockerfile, so the publish path is covered too), and a new CI job
  `Container image (build + serve)` starts the image against Postgres and requires a 200 from
  `/healthz`.

  Verified end to end: the fixed image returns
  `{"status":"ok","arm":"v4",...}` and logs `Application startup complete`, where the previous
  image fails to load its own entry point.

- **The documented one-shot query form had never worked, and a failed one exited 0** (#151,
  reported by [@ybayraktarb](https://github.com/ybayraktarb) while verifying the install path on
  k3s/k3d for #100). `kq "question"` reads the first positional as a *subcommand*, so it printed
  `Unknown command` and exited 2 — in 14 places including the root `README.md` that serves as the
  GitHub landing page, the social-preview generator, the issue templates and the v2/v3 READMEs.
  All corrected to `kq -q "..."`; the one remaining occurrence, in
  `v4/packages/kube-q/docs/cli-reference.md`, is deliberate — it documents that the bare form
  fails.

  The same pass fixed the exit code behind it: authentication failures, non-200s, invalid JSON and
  exhausted retries all printed a red message and **exited 0**, so a script or CI job could not
  tell an answer from an outage. `run_single_query` now returns success as a bool and `main()`
  exits 1. A mutation paused for HITL approval returns no text and is still a success.

- **`ADOPTERS.md` has its first entry** — k3s/k3d on macOS, contributed by
  [@ybayraktarb](https://github.com/ybayraktarb) (#151). The table was honestly empty rather than
  padded; it is now honestly not.

### Fixed
- **Every playbook silently failed to load on a non-UTF-8 locale** (#136, #138 — reported and
  fixed by [@uuzzrm](https://github.com/uuzzrm)). The playbook loader read its YAML with a bare
  `path.read_text()`, which decodes using the *platform default* encoding. On Windows
  (CP1252/CP936) or under the POSIX `C` locale the em-dashes the playbooks contain raise
  `UnicodeDecodeError` — and `_load_all()`'s per-file `except` swallowed it, so the server came
  up with **zero** playbooks and nothing in the logs that looked like a failure. Now read
  explicitly as UTF-8 in the v2, v3 and v4 loaders, each with a regression test that fails if the
  encoding argument is ever dropped again.

  ⚠️ CI runs the **v4 and kube-q suites only**, so the v2/v3 regression tests are not guarded by
  CI. They were run by hand for this merge (v2 25 passed, v3 12 passed).

- **A malformed triage reply silently discarded the user's request** (#22, #133 — contributed by
  [@uuzzrm](https://github.com/uuzzrm)). The triage tier answers in strict JSON, and a reply that
  did not parse was converted straight into `{"mode": "investigate", "plan": []}`. A user who
  asked a *chat* question got a full cluster investigation instead, and nothing recorded that the
  model's answer had been thrown away. The reply now goes back to the model with a corrective
  hint, up to **3 attempts** total, before falling back to the investigate default.

  Follow-up in the same seam: `_parse_triage_json_strict` now also rejects a `plan` that is not a
  list. `triage()` does `(parsed.get("plan") or [])[:6]`, so a *string* plan sliced into six
  single characters and emitted six one-character PlanSteps — pre-existing, and now sent back
  through the repair loop instead. The echoed malformed reply is capped at 2 000 chars so a
  pathological reply cannot be re-sent on every remaining attempt and exhaust the context window.

### Fixed
- **`v4/uv.lock` was stale — it still recorded `kubeintellect 2.2.0` after the 2.3.1 bump.**
  `uv lock --check` failed against the committed lockfile (rc=1, *"the lockfile needs to be
  updated"*), so `uv sync --locked` / `--frozen` — what a reproducible release build should use —
  would have failed for three days. Re-locked, and **CI now runs `uv lock --check`** so it cannot
  drift silently again.

### Added
- **The install smoke test now probes `--version`, not just `--help`.** `--help` is not sufficient
  on its own: argparse exits 0 for `-h` even when a required subcommand is missing, so a broken
  top-level parser passes a `--help` check. That is not hypothetical — `kubeintellect --version`
  shipped to PyPI printing a usage error and exiting 2, and this job was green the whole time.
  The project's own launch pre-flight had already documented the blind spot; CI had not adopted it.

### Fixed
- **`gitops.py`'s default command runner could hang a request indefinitely.** `_default_runner`
  shelled out to `git push` and `gh pr create` with **no timeout**. Both block rather than fail
  in the common failure modes — `git push` waits on a credential prompt that will never be
  answered (there is no tty), on a half-open connection, or on a stale `index.lock`; `gh` waits
  on auth. Unbounded, that hangs the calling request with no upper limit, which is the worst
  failure shape for an incident-response tool: it fails exactly when someone needs an answer.

  Now bounded at 60s, converting a hang into an ordinary non-zero result that `open_pr`'s
  existing graceful-degradation paths already handle — a push timeout reports push failure
  rather than falsely claiming the branch was pushed. **Latent, not live**: the module is a v5
  P3 slice that nothing currently calls, so no request path was affected; it is fixed now
  because it would have become live the moment the fix-PR flow is wired up.

  Found by AST-auditing every `subprocess` call site in `app/` for a `timeout` kwarg. The other
  results are correct as they stand: the ten request-path calls all specify timeouts,
  `sensorium/k8s_watcher.py` is a deliberately long-lived `kubectl --watch`, and the remaining
  hits are in the operator-interactive admin CLI where a hang is visible and interruptible.

### Fixed
- **Readiness and liveness were the same static probe, so rolling updates dropped requests.**
  `/healthz` is deliberately static — a liveness probe that touches a dependency turns one
  database blip into a cluster-wide restart loop — but the Helm chart pointed **both**
  `livenessProbe` and `readinessProbe` at it. A replica therefore kept answering "route traffic
  to me" right up until process exit. Kubernetes removes a terminating pod from Endpoints
  asynchronously, so during that window requests were still being routed to a replica that had
  already begun closing its pools.

  Adds `GET /readyz` (`app/core/readiness.py`): 200 while serving, **503 as soon as shutdown
  begins** — the lifespan flips it *before* tearing anything down. The chart now points
  `readinessProbe` at `/readyz` with `failureThreshold: 1`, and sets
  `terminationGracePeriodSeconds` (default 45) so the drain window has somewhere to happen.

  `/readyz` deliberately **does not probe Postgres**. That would look more thorough and be more
  dangerous: when the shared database blips, every replica goes unready at once and the Service
  is left with no endpoints, converting degradation into a total outage. Dependency health
  belongs in alerting, not in a probe that controls routing. Six tests lock this down, including
  an explicit assertion that `/readyz` never touches the database, and that liveness stays 200
  while draining (failing it would have Kubernetes kill the pod mid-drain).

### Fixed
- **The four ACI read verbs declared an injected run config they could never receive.**
  `app/tools/aci/read_verbs.py` annotated the parameter
  `Annotated[Optional[RunnableConfig], InjectedToolArg]`. `langchain_core` matches the injected
  run config **by identity** (`type_ is RunnableConfig`), so the widened form is not matched and
  those tools always received `config=None` — the exact failure mode AGENTS.md safety invariant
  #6 exists to prevent, and the one `ruff`'s `UP045` actively suggests.

  **No RBAC decision was affected**: those verbs are read-only and never read `user_role`, so
  nothing failed open in practice. It is recorded as a fix rather than a footnote because the
  parameter advertised itself as carrying `user_role`, so the first RBAC check added there
  would have silently failed open. Corrected to bare `RunnableConfig` at all four sites, with
  the reasoning inline at the call site.

### Added
- **`v4/tests/test_injected_config_invariant.py` — a gate for the annotation invariant.**
  It scans every `config: Annotated[…, InjectedToolArg]` parameter under `app/` and fails if any
  is not bare `RunnableConfig`, plus canary tests proving against the installed `langchain_core`
  that the bare form *is* injected and the widened form is *not* — so the invariant's premise is
  re-proven on every run rather than assumed.

  This closes a real hole. mypy cannot catch it (both forms type-check; the correct one needs a
  `# type: ignore`), ruff cannot (the `<0.16` pin exists because `UP045` suggests the broken
  form), and behavioural tests mostly cannot — a tool that never *reads* `config` passes every
  test while silently receiving `None`, which is precisely how this survived. Red-green proven:
  reintroducing the widened form on a single parameter fails the suite with the offending file
  and line. Server suite 1023 → **1031**.

### Added
- **Hugging Face Space** — [`mskazemi/kubeintellect`](https://huggingface.co/spaces/mskazemi/kubeintellect),
  a Gradio chat client over the public read-only demo API (`deploy/huggingface-space/`). It renders
  the `ki_event` side channel (status / plan / tool_call / tool_result) as collapsible activity
  blocks, so a visitor can see the actual `kubectl` calls behind each answer. A new discovery
  surface: the repo has never had a developer-community referrer, and HF is where the adjacent
  Kubernetes-agent audience already is.

  Two things worth knowing for anyone maintaining it. The demo key holds the `readonly` role, so a
  write request is **refused by RBAC** and never reaches the human-approval prompt — the Space says
  exactly that rather than implying it demonstrates the HITL flow. And a Gradio Space on the free
  tier is pinned to ZeroGPU hardware, which aborts at startup with *"No @spaces.GPU function
  detected"*; since this app is pure I/O it carries a guarded no-op probe purely to satisfy that
  check. Downgrading to free `cpu-basic` requires a PRO subscription.

### Fixed
- **`kubeintellect --version` printed an argparse usage error instead of a version.** The
  subcommand parser is declared `required=True` and no `--version` argument existed, so the flag
  fell through to "the following arguments are required: command" and exited 2. `kq --version` had
  worked all along, which is how this survived — the two CLIs were never checked together. It now
  prints `kubeintellect <version>` resolved from installed distribution metadata (falling back to
  `unknown` in a bare source tree), matching how `kube-q` already does it. `main()` also takes an
  optional `argv` so the behaviour is testable without touching `sys.argv`, and regression tests
  cover the flag, the version string, and that a bare invocation still exits 2.


## [2.3.1] – 2026-08-15

### Fixed
- **The krew manifest could not be rendered**, so `kubectl kq` was never submitted to
  `krew-index` on the v2.3.0 release. `addURIAndSha` emits its `sha256:` continuation line at a
  hard-coded four-space indent, so the template call has to sit at four spaces itself; ours was
  nested two deeper, which put `uri:` at six and `sha256:` at four and made the mapping
  inconsistent — krew-release-bot failed with *"error converting YAML to JSON: yaml: line 55:
  did not find expected '-' indicator"*. Reindented to the canonical form and checked by
  rendering the template with a stub helper, which reproduces the failure on the old file and
  parses on the new one. The v2.3.0 release assets themselves were fine.


## [2.3.0] – 2026-08-15

### Added
- **Two more playbooks — `PvcPending` and `LivenessProbeFailing`** (#94, #95, #127, #128) —
  contributed by [@hariomlohardev](https://github.com/hariomlohardev). The library is now **23
  playbooks / 20 compiled detectors / 3 LLM-only**.

  Both arrived with a working `triggers:` block and a `detect:` block that could not fire, which
  is the [#114](https://github.com/MSKazemi/kubeintellect/pull/114) failure again: `PvcPending`
  declared `kind: PersistentVolumeClaim`, but `kind:` selects the observation *channel* and
  `WatchPredicate.matches()` only knows `Pod`/`Event`/`Node` — every other value falls through to
  `return False`. It parsed, loaded, counted toward the detector total and passed the schema
  check while being a permanent no-op. Re-seated on `kind: Event` + `involved_kind:
  PersistentVolumeClaim`, and `StorageClassNotFound` dropped because no controller emits it
  (`FailedBinding` and `ProvisioningFailed` do).

  `LivenessProbeFailing` matched `reason: ^Unhealthy$` with no message co-condition. The kubelet
  emits `Unhealthy` for **both** probe kinds, so it fired on every readiness failure as well —
  duplicating `ReadinessProbeFailing` and reporting a restart loop that was not happening. Both
  playbooks now carry the message that names the probe, and `ReadinessProbeFailing` no longer
  claims `Liveness probe failed`: a failed readiness probe pulls the pod out of Service
  endpoints, a failed liveness probe makes the kubelet restart the container. Different symptom,
  different fix.

  Guarded in `tests/test_detectors.py`: `test_every_watch_predicate_uses_a_known_observation_kind`
  is a class guard over every shipped detector for the dead-`kind:` family, plus `TestPvcPending
  Detector` and `TestProbeDetectorsDoNotCrossFire` (both directions). All five fail against the
  originals — verified by reverting.
- **Worked examples for the remaining eight `kq` subcommands** (#86–#93, #119–#126) — contributed
  by [@hariomlohardev](https://github.com/hariomlohardev). `v4/docs/examples.md` covered 2 of 10
  subcommands; it now covers all 10, as sections 10–17. Every transcript is real output, checked
  against `kube-q` 1.5.0 byte-for-byte at merge time.

  Adopted with edits. The eight PRs each numbered their section `10` and each closed with the
  same copied sentence — *"a zero-token local operation … it never contacts the server"* — which
  is true of `kq config show` and `kq completion`, and false of `replay`, `postmortem`, `export`,
  `detector`, `preference` and `v5-status`, all of which call the server. In an incident tool a
  confident wrong statement is the failure mode that matters, so each section now says what its
  command actually does. Six of the eight `cli-reference.md` anchors did not exist (`#kq-replay`
  vs the real `#kq-replay-session-id`, etc.) and are fixed; `mkdocs build` is clean. The `kq
  config show` transcript had the contributor's home directory in it, now `/home/you`.

- **The Helm chart is published — `oci://ghcr.io/mskazemi/charts/kubeintellect`.** It existed
  in-tree but had never been pushed anywhere, so there was no supported way to install the
  server. `helm-publish.yml` lints and renders the chart on every PR that touches it and pushes
  it to GHCR on each `v*` tag, taking chart `version` and `appVersion` from the tag so they
  cannot drift from the release. The chart is listed on Artifact Hub at
  [artifacthub.io/packages/helm/kubeintellect/kubeintellect](https://artifacthub.io/packages/helm/kubeintellect/kubeintellect),
  and `artifacthub-repo.yml` is pushed as a sibling OCI artifact for the Verified Publisher
  label. A chart `README.md` documents the LLM providers, the RBAC/HITL model, and the fact that
  the chart writes `metadata.namespace` from `.Values.namespace` rather than the release
  namespace.
- **Standalone `kq` binaries on every release** — linux and darwin, amd64 and arm64, frozen with
  PyInstaller and attached to the GitHub Release with checksums. They need no Python on the
  target machine (verified under `env -i`). `pipx install kube-q` is unchanged and remains the
  path for anyone who already has Python; these archives exist so downstream package managers
  have something to consume.
- **`kubectl kq` submitted to krew** — `.krew.yaml` plus a workflow that opens the
  `kubernetes-sigs/krew-index` PR on each release, guarded by a step that refuses to run unless
  all four platform archives are actually on the release, so the bot can never checksum a file
  that is not there.
### Changed
- **The memory recall similarity floor is configurable** (#14, #116) — contributed by
  [@uuzzrm](https://github.com/uuzzrm). The `pg_trgm` noise floor was hard-coded as `0.02` twice,
  independently, in `memory/episodes.py` and `memory/summaries.py`, so tuning recall for a
  cluster with an unusual vocabulary meant editing two constants in two modules and hoping they
  stayed equal. Both now read `MEMORY_RECALL_SIMILARITY_FLOOR`, a validated setting
  (`ge=0.0, le=1.0`) whose default is the same `0.02`. No behaviour change out of the box. The
  hybrid RRF path still receives the threshold in SQL and still does **not** re-apply it
  post-fetch, which is what keeps a lexical-only match from being dropped (ADR-014).
- **Cleared three ruff-0.16 rule families from the backlog** (#79, #110) — contributed by
  [@hariomlohardev](https://github.com/hariomlohardev). `UP017` (`datetime.timezone.utc` →
  `datetime.UTC`, 5 sites), `UP041` (`asyncio.TimeoutError` → `TimeoutError`, 2 sites) and
  `RUF022` (sort `__all__`, 3 sites). All three are provably no-ops here: `datetime.UTC` is an
  alias for the same object since 3.11, `asyncio.TimeoutError` **is** the builtin `TimeoutError`
  since 3.11 (the same class, not a subclass, and both sites are `except` clauses), and nothing
  iterates `__all__` in an order-dependent way. Every package declares `requires-python = ">=3.12"`.

  The four `from datetime import datetime, timezone` lines were rewritten to import `UTC` rather
  than left importing a now-unused name, which is what keeps `F401` quiet under the pinned `ruff`
  without a second cleanup pass. The deliberate `# noqa: UP017` in `kube_q/cli/store.py` is
  untouched, and no annotation was widened — `UP045` stays out of scope because on this codebase
  it silently disables RBAC and the HITL gate (AGENTS.md invariant #6). One slice of #75; the pin
  itself stays until the rest clears.

- **The Homebrew formula moved to its own tap, `MSKazemi/homebrew-kube-q`.** The install docs had
  instructed `brew tap MSKazemi/kube-q` in four places for a tap that did not exist. It does now,
  and its CI runs `brew audit --strict --online` followed by `brew install --build-from-source`
  and actually runs the binary, on every push and weekly. That immediately surfaced two
  violations the `#113` fix had missed — build dependencies must precede runtime dependencies,
  and passing `0` to `shell_output` is redundant — neither of which anyone could have seen,
  because `brew` had never once been run against the formula. The in-tree copy under
  `v4/packages/kube-q/Formula/` is deleted so the two cannot diverge; the tap also polls PyPI
  daily and only commits a version bump after that same audit-and-install gate passes.
### Fixed
- **`.env.example` was unreachable from a fresh clone** (#115, #117) — fixed by
  [@hariomlohardev](https://github.com/hariomlohardev). About fifteen places
  (`CONTRIBUTING.md`, `v4/README.md`, `v4/Makefile`, five deploy docs,
  `langfuse-provision.sh`) tell you to `cp .env.example .env`, and the file was not in the repo.
  The reported cause — a commit deleting it — was not the live one: `.gitignore`'s `.env.*`
  matches `.env.example`, so it could not be re-added at all until that pattern was negated.
  Now `!.env.example` / `!**/.env.example`, with `v4/.env.example` tracked.

  Adopted with one change: the PR restored the 244-line template from before the file was lost,
  which no longer describes the product — it omits the `qwen` and `anthropic` providers (both
  supported, and both asserted by `test_doc_claims`) and still tells you to copy Langfuse keys
  out of the UI, which `make langfuse-provision` replaced. Merged the current 289-line template
  instead. Scanned: every credential field is empty or an obvious placeholder.
- **The Homebrew formula's `desc` was still too long for `brew audit --strict`** (#113, #118) —
  fixed by [@hariomlohardev](https://github.com/hariomlohardev), who also sorted the `resource`
  blocks alphabetically and added `scripts/verify-brew.sh` so #113's two never-run commands are
  reproducible.

  Adopted with a correction: brew measures `"<name>: <desc>"`, not the description alone, so the
  PR's 75-character `desc` was still 83 with the `kube-q: ` prefix and would still have been
  flagged — and the script asserted `desc <= 80`, the wrong threshold, so it reported PASS on it.
  Both now use the real rule; the description is 78 including the prefix. `brew audit` and `brew
  install` themselves remain unrun — still no Homebrew on any machine here — so #113 stays open
  for that, but the static half is now checkable by anyone with `bash`.
- **The Homebrew formula could not install, and misstated the licence** (#56, #111) — fixed by
  [@uuzzrm](https://github.com/uuzzrm). It declared `license "MIT"` on AGPL code, pointed
  `homepage` at `MSKazemi/kube_q` (the #74/#78 defect, which the issue had not caught), targeted
  the pre-relicense 1.0.0, and carried four `resource` blocks for a seven-dependency package —
  **two of them with 63-character sha256 values**, which are not valid digests at all rather than
  merely stale ones. Now 1.5.0, `AGPL-3.0-or-later`, the canonical homepage, and the complete
  18-resource dependency tree with `certifi` taken from Homebrew.

  Verified by downloading every artifact and hashing the bytes — 19/19 match — and by resolving
  `kube-q==1.5.0` independently for Python 3.12: the closure is exactly complete, nothing missing
  and nothing extra. **`brew audit --strict` and `brew install --build-from-source` have still
  not been run by anyone**; neither contributor nor maintainer has Homebrew available, so the
  `maturin`/`rust` build path for `pydantic-core` is reasoned rather than observed. Tracked in
  #113. The formula is not a tap, so nothing is installable from here either way — the licence
  misstatement was the live defect, and it is fixed.

  The issue itself was **partly wrong** and has been corrected in place: `kube-q` 1.0.0 does
  exist (uploaded 2026-04-10, the oldest release) and its recorded sha256 matched the formula, so
  the "points at a release that does not exist" premise was false. It survived a re-verification
  because that check printed `sorted(releases)[-3:]` — the last three entries, which structurally
  cannot show 1.0.0.
- **The `kq` test suite failed depending on the contributor's terminal** (#106, #109) — fixed by
  [@floze-the-genius](https://github.com/floze-the-genius). The suite inherited `COLUMNS`, `TERM`
  and `NO_COLOR` from the invoking shell, which changed Rich's table wrapping and the theme the
  module-level console is built with. A contributor on an 80-column terminal could watch tests
  fail before touching a line of code, on a suite that was green in CI. On `2c1f676`:
  `COLUMNS=40` → 5 failed, `COLUMNS=60` → 1 failed, `TERM=dumb` → 1 failed, `NO_COLOR=1` →
  1 failed.

  The fix pins the rendering environment in `pytest_configure` — **before** test modules are
  imported — and re-applies it per test with an autouse fixture that `monkeypatch` can still
  override, restoring the invoking environment in `pytest_unconfigure`. **No assertion was
  weakened**, which was the point: the tempting fix is to loosen assertions until they pass at
  any width, which makes the suite green and stops it testing anything.

  The `pytest_configure` half is load-bearing, and
  [@AshSgDe29071999](https://github.com/AshSgDe29071999) is why that is documented rather than
  assumed. They independently diagnosed the same root cause and submitted the fixture-only
  form (#107); running it as a control showed it clears four of the five environments and
  still fails under `NO_COLOR=1`, because an autouse fixture runs after collection, by which
  point the module-level console already exists with the stripped theme. Now 312/312 on all
  five single-variable runs, on all three applied together, and on a clean environment.
- **The published `kube-q` package pointed users, and their bug reports, at the wrong
  repository** (#74, #78) — every `[project.urls]` entry resolved to `MSKazemi/kube_q`, a
  pre-AGPL snapshot that is not where `kube-q` is developed. On a package serving ~160
  downloads a month, "Homepage", "Repository" and "Bug Tracker" all led away from the canonical
  repo, and the PyPI page carried no link to the docs, the changelog, or this repository. Fixed
  by [@shaurya703](https://github.com/shaurya703), who also gave `ki-protocol` the `authors`,
  `[project.urls]` and `classifiers` it had never had — its page was blank, including **no
  licence classifier at all** on AGPL code.

  All three distributions moved to the [PEP 639](https://peps.python.org/pep-0639/) form
  (`license = "AGPL-3.0-or-later"` + `license-files`). This removes a defect the issue had not
  spotted: `kube-q` used `license = { file = "LICENSE" }`, which dumped the **entire 34 KB AGPL
  text** into the `License:` metadata header. Every wheel now reports a machine-readable
  `License-Expression` under `Metadata-Version: 2.4`, and the redundant
  `License :: OSI Approved :: …` classifier is gone — PyPI rejects an upload carrying both.

  **@shaurya703 also caught that `license-files` resolves relative to each package directory**,
  and that neither `ki-protocol` nor `kubeintellect-server` had a `LICENSE` of its own — so
  those wheels had been shipping **without the licence text**, a real compliance gap in an
  AGPL project that was invisible from the source tree. Both now carry a byte-identical copy,
  verified in the built artifacts (`twine check` passes on all six).

  The live PyPI pages stay wrong until the next publish; this fixes the source of them.
- **The `kube-q` documentation still sent readers to the old repository** — 13 files across
  `v4/` linked `MSKazemi/kube_q`, including `v4/packages/kube-q/README.md`, which **is** the
  body of the PyPI page: it told readers to `git clone https://github.com/MSKazemi/kube_q`.
  So the metadata fix above would have corrected the sidebar links while the page underneath
  still pointed elsewhere. Found by [@shaurya703](https://github.com/shaurya703) while working
  on #78, from a single `mkdocs.yml` observation. The from-source instructions now clone the
  monorepo and `cd kubeintellect/v4/packages/kube-q` (verified end to end: clone → `pip install
  -e .` → `kq --version` reports 1.5.0 — which only resolves at all now that `ki-protocol` is
  published). The mkdocs social link to `ghcr.io/mskazemi/kube_q` was a **404** and now points
  at the image that exists. The Homebrew formula's stale homepage is deliberately untouched —
  it belongs to #56.
- **Imports are sorted across `v4/`** (#76, #77) — `ruff` 0.16 enables `I001` by default and
  reported 75 unsorted blocks across 60 files, part of what blocks lifting the `ruff<0.16` pin.
  Cleared by [@shaurya703](https://github.com/shaurya703), verified at the AST level rather
  than by eye: the imported-symbol set is identical in all 60 files and there is no non-import
  code change anywhere. The one manual edit is the interesting one — ruff's autofix wraps the
  long `langchain_anthropic` import into parenthesized form, which moves
  `# type: ignore[import-not-found]` off the `from` line and breaks the suppression (mypy 0 →
  1). The ignore now sits on the `from … import (` line, keeping both the sorted form and the
  suppression. `UP045` remains untouched by design; the remaining ~364 findings are tracked in
  #75.
- **The Greetings workflow had never posted a single greeting** — it passed the
  `first-interaction` action its inputs hyphenated (`issue-message`, `pr-message`,
  `repo-token`) while the action declares them underscored. The runner exposes unknown inputs
  under their own names rather than rejecting them, so the action threw
  `Input required and not supplied: issue_message` on every issue and every pull request since
  the workflow landed; `repo-token` only appeared to work because `repo_token` defaults to
  `${{ github.token }}`. Every first-time contributor got silence plus a red X — precisely the
  two things the workflow's own header says it exists to prevent, and what #73's author saw.
  The failure was easy to dismiss as bot noise because it also fired on every Dependabot PR,
  leaving them permanently `UNSTABLE`.
- **The demo UI is ESLint-clean again** (`v4/packages/kube-q/web`, #50, #73) — 2 errors and 2
  warnings, reported by [@AdvaitVarhade](https://github.com/AdvaitVarhade), who also traced the
  two `react-hooks/set-state-in-effect` errors to `app/page.tsx`; the issue had attributed them
  to `PtyTerminal.tsx`. `PtyTerminal` now builds its status notifier inside the connection
  effect and reaches the current callback through a ref, so the effect depends on `authToken`
  alone — a re-rendered parent no longer tears down a live PTY session — and the unused
  `useState` import is gone.

  The two errors in `page.tsx` are fixed by removing the effects rather than deferring them.
  `sessionStorage` is now read through `useSyncExternalStore`, which is SSR-safe and derives the
  token during render, so the `tokenReady` state and its mount effect are both gone; and the
  auth-failure prompt is raised in the `onStatusChange` callback that causes it instead of from
  an effect watching the state that callback just wrote. Deferring the same `setState` behind
  `setTimeout(…, 0)` would satisfy the linter while making the behaviour worse — the cascading
  render still happens, now after paint, so the terminal pane visibly flashes empty on every
  load. Verified in a browser as well as by `npm run lint` and `npm run build`: the terminal
  mounts, connects, and propagates status, with no hydration or `getSnapshot` warning under
  dev StrictMode.

  The ref that carries the callback is updated from an effect rather than during render, since
  React may discard a render and a ref written there would outlive it. The xterm-init failure
  path now builds its message as a text node instead of assigning `innerHTML`.

- **The Helm chart could never pull its image.** `values.yaml` defaulted `image.tag` to
  `dev-latest`, a tag that has never existed in the registry — GHCR has `latest` and `2.0.0`
  through `2.2.0` and nothing else — so a default `helm install` went straight to
  `ImagePullBackOff`. The tag now defaults to the chart's `appVersion`, and a CI step renders the
  chart and asserts the image matches, so it cannot regress silently. The same dead tag was
  copied into all seven per-cloud `values-*.yaml.example` files and is corrected there too.
- **The container image had never reached Docker Hub.** `docker.io/kazemi/kubeintellect` did not
  exist. Nothing was structurally broken: the `v2.2.0` tag run predated the credentials being
  added, and both later runs were dry runs, which log in to both registries (proving the
  credentials) while pushing nothing. The image is now published.
### Added
- **An `HPANotScaling` playbook** (#97, #114) — the 21st, contributed by
  [@Chris7717](https://github.com/Chris7717). It covers the autoscaler that does nothing and
  looks identical to one that is simply not needed, and it separates the two root causes that
  Kubernetes reports through the *same* `FailedGetResourceMetric` /
  `FailedComputeMetricsReplicas` events but that need different fixes: metrics-server missing or
  unreachable (cluster-wide) versus a container with no `resources.requests.cpu`
  (workload-specific). Both were reproduced against a kind cluster, so the
  `investigation_steps` and `expected_evidence` are transcribed from real `kubectl` output
  rather than reasoned. It compiles to a detector — 18 of the 21 playbooks now do.

  Adopted with two maintainer fixes to the `detect:` block, neither of which any existing gate
  could see. The `reason_regex` was `"^(FailedGetResourceMetric | FailedComputeMetricsReplicas)$"`:
  the spaces around the alternation bar bind to the branches, so the compiled predicate required
  an event reason *containing a space* and could therefore never match anything — the detector
  loaded, counted, and passed the schema check while being a permanent no-op. The stored PromQL
  named `kube_horizontalpoduatoscaler_status_condition` (transposed) and matched `status="False"`,
  where kube-state-metrics emits the status label lower-case; that arm is latent until #20 lands,
  but it was stored wrong.

  The gate gap mattered more than either typo: the PR's tests exercised `match_playbooks()` — the
  prompt-side `triggers:` path — and nothing anywhere asserted that a compiled predicate can
  actually *fire*. `test_detectors.py` now checks this playbook against both real event reasons,
  and a new class guard rejects any shipped `reason_regex` whose alternatives carry surrounding
  whitespace, since a Kubernetes event reason never contains a space. Both fail against the
  original file.

- **A `DeploymentRolloutStuck` playbook** (#98, #112) — the 20th, contributed by
  [@hariomlohardev](https://github.com/hariomlohardev). It covers the rollout that fails
  *without an outage*: the new ReplicaSet never becomes Available, the old one keeps serving
  traffic, and nothing looks broken from the outside until someone asks why the deploy never
  finished. It compiles to a detector (17 of the 20 playbooks now do), with a 600-second
  debounce so a slow-but-progressing rollout does not fire it.

  The part worth copying is that it **routes rather than duplicates**: `ProgressDeadlineExceeded`
  is almost always a symptom, so the investigation steps chain to `ReadinessProbeFailing`,
  `PendingInsufficientResources`, `PendingSchedulingConstraints` and `ImagePullBackOff` for the
  actual cause, and the fix template opens with "do **not** force-delete pods." The fourth
  `expected_evidence` entry names the condition under which the playbook does *not* apply,
  which is a discipline none of the previous nineteen had.

- **A `NetworkPolicyBlocking` playbook** (#99, #108) — the 19th, contributed by
  [@Priyanshu608](https://github.com/Priyanshu608). It covers the one failure in the set that
  emits **no evidence at all**: a NetworkPolicy denial is discarded in the CNI datapath, so the
  API server records no event and no Warning is ever produced. The Service is healthy, endpoints
  exist, DNS resolves — and the connection simply hangs. The playbook makes that *absence* the
  signal, which is what none of the other 18 could express.

  Two maintainer corrections landed on top. The submitted trigger also matched
  **`connection refused`**, which is evidence *against* a policy drop: a refusal means a TCP RST
  came back, so the packet was delivered. Matching it would have pointed the agent at a
  NetworkPolicy for what is almost always a wrong port or a missing endpoint —
  `ServiceUnreachable`'s territory. It now matches timeout signatures only, and
  `test_networkpolicy_blocking_ignores_connection_refused` locks that in. The `triggers:` block
  was also nested under `detect:`, where the loader never reads it
  ([`loader.py`](v4/packages/kubeintellect-server/app/agent/playbooks/loader.py) takes
  `triggers` from the top level), so the playbook loaded cleanly into the registry and then
  never fired — inert, with no error anywhere. `test_each_playbook_has_complete_schema` catches
  exactly this and did.

- **The test suites now run on Python 3.13 as well as 3.12** (`Tests (… · py3.13)`), closing
  a gap where the project was verified on an interpreter it does not ship. `v4/Dockerfile`'s
  runtime stage is `python:3.13-slim` and all three distributions declare
  `requires-python = ">=3.12"`, so every container and every `pip install` on a current
  machine already ran 3.13 — while CI pinned 3.12 in every job. Both suites pass unchanged
  (990 server + 312 `kq`), so this adds coverage rather than fixing a break; the point is that
  a future 3.13-only regression now fails a gate instead of reaching users. Added as a
  separate job rather than a `python-version` matrix axis on purpose: the axis would rename
  `Tests (server)`, and branch protection matches required checks by name. The
  `Programming Language :: Python :: 3.13` classifier on the server distribution is now
  earned rather than assumed.
- **A `Syntax warnings` CI gate (`make check-syntax`, `scripts/check-syntax-warnings.py`)** —
  compiles every tracked Python file outside the frozen v1–v3 trees with `SyntaxWarning`
  promoted to an error, on the newest supported interpreter. This closes the structural blind
  spot that let #63 reach an outside contributor: the pinned `ruff` does not report an invalid
  escape sequence in the CI-linted scope, `mypy` never compiles source, and pytest triggers the
  warning only on a cold `.pyc` cache — so a green suite was not evidence either way. The
  defect class is not cosmetic: #63's non-raw string was also corrupting the jsonpath examples
  in the coordinator prompt. Like the `File modes` gate it is deliberately dependency-free
  (stdlib only, no `uv sync`) so it stays correct however the `ruff` pin is eventually lifted,
  and it uses `compile()` rather than `compileall` so it leaves no `.pyc` files behind.
  `make setup` now runs all six gates instead of four.
- **A `File modes` CI gate (`make check-modes`, `scripts/check-file-modes.sh`)** enforcing one
  invariant outside the frozen v1–v3 generations: *a tracked file is executable if and only if
  it starts with a shebang*. This closes a structural blind spot rather than a one-off mess —
  `ruff` is pinned `<0.16` and `EXE002` only became a default rule in 0.16, so the lint
  gate could not see a stray `+x` bit at all, which is how 94 library modules silently acquired
  one. The guard is deliberately dependency-free (git + coreutils, no `uv sync`), so it stays
  correct whichever way the ruff upgrade lands, and it runs in seconds. It also covers the
  inverse defect (`EXE001`: a shebang'd script that is not executable, i.e. one you cannot run),
  which `ruff` only reports for Python. `make fix-modes` corrects violations in place, updating
  both the working tree and the index — a `chmod` alone would leave the committed mode unchanged.
- **One-command contributor setup: `make setup` (`scripts/dev-setup.sh`).** Installs `uv` if
  missing, installs the `v4` workspace, then runs the *exact* four gate commands CI runs
  (ruff, mypy, server suite, `kq` suite) and reports which passed. A contributor therefore
  learns their environment is correct *before* changing anything, and never debugs their setup
  and their change at the same time. It also names the pre-existing debt that is **not** their
  bug (`make lint`'s non-gate `ruff format --check`, the deliberate `ruff<0.16` pin). No
  cluster, Docker daemon, or LLM API key is required — the suites are mocked.
- **`.devcontainer/devcontainer.json`** — zero-install contribution via GitHub Codespaces or
  VS Code Dev Containers, pinned to the Python 3.12 that CI pins and running the same setup
  script on create.
- **`AGENTS.md`** ([agents.md](https://agents.md/) format) — machine-readable repository rules
  for AI coding agents: gate commands, the six safety invariants, testing expectations, and
  the pre-existing debt not to "fix". Human authority remains `CONTRIBUTING.md`.
- **A `Contributing` section on the README front page**, plus a live good-first-issue badge and
  a header nav link. The invitation previously existed only as one sentence under `Maintainer`
  near the bottom of a 221-line file.
- **`.github/workflows/greetings.yml`** — welcomes a contributor's first issue and first PR,
  and pre-empts the two things that most often make newcomers give up: silence, and a fork PR
  that appears stalled because GitHub runs no CI for first-time contributors until a maintainer
  approves the run.
- **`.github/workflows/labeler.yml` + `.github/labeler.yml`** — path-based `area/*` PR
  labelling mirroring the issue taxonomy in `TRIAGE.md`, so a contributor never needs to know
  the repo's internal structure to be routed correctly.
- **`.github/ISSUE_TEMPLATE/documentation.yml`** — a documentation issue template, making
  "the docs were unclear" a first-class report rather than a bug-report misfit.
- **A `Contributing` section in `llms.txt`**, so answer engines asked how to contribute to
  KubeIntellect have a grounded answer (Python-3.12-only prerequisite, `v4/` scope, the HITL
  invariant, and where the good first issues are).

- **`kq export <session-id>` — export a diagnosis report to JSON or YAML.** Serializes the
  same grounded postmortem `kq postmortem` renders (a view over the hash-chained decision
  log) for archiving, ticket attachments, or downstream tooling. `--format json|yaml`,
  `--output PATH`. Stdout stays machine-parseable (notes and warnings go to stderr), so
  `kq export <id> | jq` works. Exit codes follow the `kq replay` convention: `3` when the
  audit chain is broken, and `4` when the recorder holds no events for the session — in
  which case nothing is written, rather than emitting an empty-but-plausible report.
  Closes #58. Thanks to @AdvaitVarhade for proposing the capability and the initial
  implementation.

- **`Types (mypy)` is now a blocking CI gate**, and `make typecheck` runs it locally. The
  workspace type-checks with **zero errors** across all 171 source files, down from 30. The
  remaining `ruff format` debt is unaffected and still not a gate.
- `tests/test_workflow_config_injection.py` — a regression guard asserting that every graph
  node and tool declaring a `config` parameter actually receives the run config. See below
  for why that is not obvious.

### Fixed
- **Cleared the stray executable bit from 94 source files** under
  `v4/packages/kubeintellect-server/app/` and `v4/packages/ki-protocol/` — file-mode only
  (`100755 → 100644`), zero content changes, verified by the patch containing no `+`/`-` content
  lines. These are library modules with no shebang, so `chmod -x` is the correct fix rather than
  adding one. Clears `EXE002` in the CI-linted scope and takes the ruff 0.16 finding count from
  438 to 342, unblocking part of #64. Thanks to [@hariomlohardev](https://github.com/hariomlohardev)
  ([#70](https://github.com/MSKazemi/kubeintellect/pull/70)).
- **Extended the same mode-only sweep to the remaining 282 files** outside that lint path
  (the rest of `v4/`, `deploy/`, and three root files), and gave the four shebang'd scripts that
  were *not* executable their `+x` back. The frozen v1–v3 generations are deliberately untouched
  (ADR-001/002): they are closed to changes and not built by CI, so rewriting ~500 of their file
  modes would be churn against immutable history for no gate benefit.
- **The coordinator system prompt was silently corrupted.** `_COORDINATOR_SYSTEM` was a
  non-raw string containing jsonpath separators, so `{"\n"}` and `{"\t"}` were interpreted
  before the model saw them — a real newline split an example `kubectl get pods -o jsonpath=…`
  command mid-line, and a literal tab replaced another. Now a raw string. This also clears the
  `SyntaxWarning: invalid escape sequence '\`'` that Python is scheduled to turn into a
  `SyntaxError` (#63).
- **RCA synthesis crashed on providers that return content blocks.** `_synthesize` called
  `response.content.strip()`, which raises `AttributeError` when `content` is a list rather
  than a string; it now uses `response.text`, which flattens the blocks.
- **API keys are wrapped in `SecretStr`** before being handed to `ChatOpenAI` /
  `AzureChatOpenAI`, so a client repr or traceback cannot render the raw key.
- `init_graph()` raises a named error when `POSTGRES_DSN` is unset with `USE_SQLITE=false`,
  instead of failing inside the driver.
- **Four type errors in `app/cli.py`** (#55): `callable` used as a type annotation is now
  `Callable[[], None]`, and the two `subprocess.run` calls that rebound a
  `CompletedProcess[str]` variable declare `text=True`, so the type is consistent without
  changing runtime behaviour. Thanks to @hariomlohardev for the fix (#57).
- Dropped an f-prefix from a placeholder-free string in `v4/scripts/fix_pr_probe.py` (F541).

### Changed
- **The PyPI release is unblocked — all three distributions are published and current** (#66,
  closed). `ki-protocol` **1.0.0**, `kube-q` **1.5.0**, `kubeintellect` **2.2.0**, all reporting
  `AGPL-3.0-or-later` instead of the stale `MIT` metadata. Verified in clean venvs:
  `pip install kube-q` yields 1.5.0 with all ten subcommands (checked *without* `--help`, per
  the argparse trap), and `pip install kubeintellect` yields 2.2.0 with a working entry point.

  Two defects surfaced while unblocking it, neither of which the issue anticipated:
  **(1) `kube-q`'s trusted publisher authorized the wrong repository** — `MSKazemi/kube_q`
  rather than `MSKazemi/kubeintellect` — so the root `publish.yml` could never have published
  it, and confirming only the *workflow filename* would not have caught it.
  **(2) A trusted publisher registered without an environment does not match a workflow that
  runs with one.** `kubeintellect`'s publisher said `Environment: (Any)` while `publish.yml`
  runs `environment: pypi`, and every upload failed with `403 OIDC scoped token is not valid
  for project 'kubeintellect'` while the two publishers naming `pypi` explicitly succeeded in
  the same run. Registering a second publisher with the environment set explicitly fixed it.
- **README's PyPI warning block is gone** — it existed only while the published packages were
  behind, and both install paths now work as documented. The server quickstart also no longer
  claims `kubeintellect init` "creates a Kind cluster, deploys samples"; verified against the
  published 2.2.0 CLI, `init` writes `~/.kubeintellect/.env` and cluster creation is the
  separate `kind-setup` command.
- **`TRIAGE.md` no longer tells contributors that `mypy` is non-blocking debt.** It became a
  required CI check in `0c7b055`; a contributor following the old text would have pushed a PR
  expecting a `mypy` failure to be ignorable and had it blocked instead.
- `CONTRIBUTING.md` leads with the one-command path and states up front that **no cluster,
  Docker daemon, or LLM API key is needed** to run the suites — previously the requirements
  list implied all three were mandatory before you could contribute a typo fix.
- `types-PyYAML` added to the dev dependency group; the server's mypy baseline drops from
  29 to 27 errors (two pre-existing missing-stub errors resolved).
- **First `[tool.mypy]` configuration for the workspace** (#53) — `python_version = "3.12"`
  plus a per-module `ignore_missing_imports` override for `asyncpg`, which ships no `py.typed`
  marker. Clears the 5 remaining `[import-untyped]` errors without touching source. Thanks to
  @hariomlohardev (#65).
- `max_tokens=` → `max_completion_tokens=` on the OpenAI/Azure chat clients — the same
  pydantic field under its public alias, so the request payload is unchanged.
- Container runtime image moves from `python:3.12-slim` to `python:3.13-slim` (#59).
  Verified independently of CI, which does not build the image: the full dependency set
  resolves on CPython 3.13.14, both entry points start, and the 986-test server suite passes.
- `uvicorn[standard]` floor raised `>=0.32` → `>=0.52.1` to match the resolved version (#61).

## [2.2.0] – 2026-08-08

### Documentation
- **Canonical repo consolidated to `MSKazemi/kubeintellect`.** Repointed the README star badge
  (was rendering the org mirror's 0-star count), `CITATION.cff` `repository-code`, and the four
  `llms.txt` doc links from the `kubeintellect/kubeintellect` org mirror to the personal repo, which
  holds the organic stars. Ends the star-count fragmentation between the two identical public repos
  (org mirror to be redirected + archived).
- **v4 product docs — major quality pass.** Audited every page against the code and fixed
  drift (homepage playbook stat 10→18, `KUBE_Q_URL` default, `AZURE_OPENAI_API_VERSION`,
  demo-key TTL vars, a misplaced CLI flag table); documented the previously-undocumented
  `/v1/preferences` API and the `kq config` command group; added new **FAQ**,
  **Upgrade & feature-flag guide**, **Examples & cookbook**, and **Changelog** pages; deepened
  the quickstart with an end-to-end happy path; added front-matter descriptions to 13 pages;
  fixed all broken cross-page anchors; relocated the Qwen-hackathon submission collateral out
  of the product docs (to `v4/hackathon-submission/`) and clarified the Install-vs-Deploy nav
  split. `mkdocs build` is clean.
- **Investor pack + industry white paper refined (private, not in the public mirror).**
  Consistency/honesty pass on the fundraising docs (numeric cross-checks against the financial
  model, explicitly-labelled illustrative assumptions, real cited market sources — CNCF 2025,
  AIOps market size — with honest `[SOURCE NEEDED]` gaps, no fabricated traction) and the LaTeX
  white paper (MAPE-K/autonomic-computing citation, an evaluation results figure, benchmark-
  scoped detection claims, trimmed redundancy); all PDFs rebuild clean.

### Added
- **Snap packaging for the `kq` CLI.** New `snap/snapcraft.yaml` builds a `kubeintellect` snap
  (core24, amd64 + arm64) carrying the terminal client, with `snap/README.md` documenting the
  build, the confinement trade-offs, and the store-publishing steps. Deliberately **strict**
  confinement rather than the `classic` that `kubectl`/`helm`/`k9s` use: `kq` is an HTTP client
  whose filesystem needs are two known directories, so they are requested as explicit
  `personal-files` plugs — `dot-kube` (read `~/.kube`) and `dot-kube-q` (write `~/.kube-q`) —
  since the `home` interface excludes top-level dot-directories. `HOME` is remapped to
  `$SNAP_REAL_HOME` so `Path.home()` resolves outside the sandbox. Packaged as a single `python`
  part that vendors the client past its Next.js demo UI (sourcing `packages/kube-q` directly
  would drag ~600 MB of `node_modules` through the pull step) and installs it together with
  `ki-protocol` in one `pip` call, because `ki-protocol` is not on PyPI yet. Not published to
  the Snap Store yet — the name is unregistered and `personal-files` needs an approved snap
  declaration.
- **Snap CI.** `.github/workflows/snap.yml` builds both architectures on any PR touching
  `snap/`, `kube-q`, or `ki-protocol`, installs the built snap on the runner and smoke-tests
  `--version`, `--help`, and completion generation; publishing is a separate manually-dispatched
  job gated on a `SNAPCRAFT_STORE_CREDENTIALS` secret.
- **Community: adoption and prioritisation surfaces.** `ADOPTERS.md` (honestly empty rather
  than padded) plus a pinned adoption thread (#51), and a pinned roadmap voting index (#52)
  that makes 👍 reactions the explicit prioritisation signal for the "Next" list; the six
  roadmap issues carry a new `roadmap` label. New `adoption`, `needs-info`, and
  `area/packaging` labels.
- **Community: `SUPPORT.md` and `TRIAGE.md`.** `SUPPORT.md` routes questions to the right
  channel, says what to include, and states plainly that there is no SLA. `TRIAGE.md`
  documents the full issue lifecycle — the five triage questions, what every label means and
  what it promises, how to claim an issue, the two-week unassignment rule, and what each close
  reason means — written so that a non-maintainer can triage without repo permissions.
- **Container image publishing to GHCR and Docker Hub.** New `.github/workflows/docker-publish.yml`
  builds the v4 image once and publishes it to `ghcr.io/mskazemi/kubeintellect` and
  `docker.io/kazemi/kubeintellect`, tagged by semver plus `latest` and the commit sha, with OCI
  labels and `VERSION`/`GIT_SHA` build args. Triggered by a `v*` tag or manually, with a dry-run
  option. Docker Hub is skipped with a warning until `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` are set.
  Private material cannot reach the image: CI checks out the public repository, the build context is
  `v4/`, and the Dockerfile copies only the three workspace package source trees — verified against a
  local build, whose image contains just `app/`, `ki_protocol/` and `kube_q/`.
- **Doc-claims drift guard (v4).** New `v4/scripts/check_doc_claims.py` reads the canonical
  numbers straight from code — 18 shipped playbooks (loader), 16 baseline compiled detectors
  (`load_detectors()`), the valid LLM-provider set (`config.py`), and the count of `KI_V5_*`
  flags — and asserts every numbered claim in the docs still matches, exiting non-zero on drift.
  Wired as `make docs-check` and enforced by `tests/test_doc_claims.py`. It caught a real drift:
  the `api-reference.md` `/v1/findings` example said `"detectors": 18` (the playbook count) and is
  now corrected to `16` (what the endpoint actually reports at baseline). Chosen as a *check* rather
  than a prose auto-generator so hand-written docs are guarded, not clobbered.
- **Documentation standardized across all five versions (v1–v5).** New
  `design/adr/002-standard-doc-surface.md` defines a canonical doc surface + mkdocs nav +
  metadata standard (the doc analog of ADR-001); every version's docs were brought to it
  as-built. v3 gained 7 missing canonical pages (cli-reference, api-reference, capabilities,
  troubleshooting, operations, glossary, memory); verified-against-code accuracy fixes landed
  across v1 (agent count 13), v2 (18 playbooks), v3 (2 providers, 4 role tiers, DeepAgents
  agent-behaviors), and v4 (18 failure detectors, `openai|azure|qwen|anthropic` providers).
  Root `README.md` now documents the full v1→v5 lineage; `v5/README.md` added (design tier).
- **Architecture reference extended to five generations.** `architecture-comparison/`
  (both the Markdown edition and the LaTeX/PDF, now 31 pp) gained code-grounded **V4**
  (platform) and **V5** (design-tier) chapters, a V4 architecture diagram, and updated
  lineage/comparison/module-map tables — one as-built v1→v5 technical reference.
- **Cortex mypy cleanup (quality gate).** Full-gate audit (1251 tests + ruff green) found and fixed 3 latent type issues in `app/cortex` (synthesize `messages: list[BaseMessage]`, remember `isinstance` narrowing, optional `langchain_anthropic` import-ignore); `mypy app/cortex` now clean, no behavior change.
- **v5 experimental-flags reference.** New `docs/v5-experimental-flags.md` catalogs all ~60 `KI_V5_*`/`CORTEX_V5_ENABLED` flags (flag / default / purpose, generated from `config.py`), linked from `configuration.md` — one place to see every additive default-off toggle.
- **`/v1/v5/status` API-reference entry + boot-time version log.** Documented the endpoint (fields + example) in `docs/api-reference.md`; the server now logs its full version identity + active v5 flags at startup (ADR-019).
- **`kq v5-status` CLI + CLI-reference entry.** Terminal view of the v5 trust plane (version, active flags, kill switch / change freeze / spend cap) via `GET /v1/v5/status`; documented in `docs/cli-reference.md`.
- **v5 trust-plane observability + arm-3 calibration harness.** New `GET /v5/status` surfaces version identity, active `KI_V5_*` flags, and the fail-closed brakes (kill switch / change freeze / spend cap) in one read-only call. Added `calibrate_offline_weight` (ADR-102 arm-3 calibration harness; simulation-validated, production cap value awaits real matched offline+live shadow data).
- **v5 fix-PR write class validated END-TO-END with a real PR.** The misconfig fix-PR flow (repair → fix_pr → push → PR) opened a genuine GitHub pull request (MSKazemi/kubeintellect-private#1) proposing `runAsNonRoot: false → true` — the P3 first write class exercised against a live remote, not just locally.
- **v5 data-source activation (additive, default-off).** Agentic-workload + GPU-health metric collector (queries Prometheus for agent tool-call rate/cost, sandbox-escape, ResourceClaim/ECC/GPU-OOM → runs the predicates; active/dormant-until-breach) and a Postgres fleet-signal store that pools per-cluster signals into fleet-wide pattern detection (LIVE-validated on real PG: 3 clusters → critical FleetAlert, tenant-isolated).
- **v5 integration-activation (additive, default-off).** Pre-capture wired into the live watchtower
  (imminent predicted finding → arm recorders before death); change-watchdog loop activated in the
  consolidation worker (changes-since-last-sweep → read-only fan-out investigation, timestamp-deduped)
  — the P4 anticipation loop now runs end-to-end (agent change → ledger → sweep → investigate-the-diff).
- **v5 loop-closing + fleet slices (additive, default-off).** P3: spend usage source (OTel token
  spend → deny-before-breach, closes the spend-budget loop) and blast-radius composite gate (budget
  + staged-propagation + failure-domain in one fail-closed verdict). P5: fleet-wide signal pooling
  (same signal on ≥N clusters/tenant → FleetAlert, tenant-isolated) and fleet-store RLS tenancy
  scaffolding (ADR-105 policy defined, disabled pending GUC-binding). Each behind a `KI_V5_*` flag.
- **v5 P3/P4 hardening slices (additive, default-off).** P3: failure-domain + change-schedule budget
  (per-zone unavailability caps ≤~⅓ + maintenance windows, REQ-sysadmin-18). P4: NL-detector →
  statistical ladder (ADR-012 shadow-precision gate, human review retained), agentic-workload SRE +
  GPU-health detectors (agent-runaway/sandbox-escape + GPU/ResourceClaim health, the incumbent-empty
  surface D17/SD-D), predictive pre-capture (arm recorders before an imminent predicted death,
  A-CH-20-16). Each behind a `KI_V5_*` flag.
- **v5 P3 Trust plane + P4 Anticipation + P5 Fleet (additive, all default-off; validated live where a
  cluster/DB/LLM applied).** P3: blast-radius/spend budget gate (kill switch, change freeze, spend
  cap), statistical promotion engine (ADR-102) + Postgres outcome store, mutating-verb chokepoint
  (rollback classification + write-authority), server-side dry-run, machine-checkable postcondition
  oracle, transactional apply→verify→auto-rollback (TNR), two-axis capability sandbox (SA
  impersonation), security-outcome gate, misconfig LLM auto-repair + fix-PR generator + GitOps PR
  opener, staged propagation (never instant-global), failure-domain + change-schedule budget. P4:
  heterogeneous model routing + air-gap read-only floor (ADR-103), per-change ephemeral watchdog +
  fan-out dispatch, evidence-grounded rightsizing, predictive-detection fusion. P5: cross-cluster
  fleet memory exchange with strict tenant isolation + Postgres-backed durable store (ADR-105). Every
  slice gated behind a `KI_V5_*` flag (default off ⇒ byte-identical V4). LIVE-validated on n1 Kind +
  Azure gpt-4o + Postgres: P3 write path (real mutation + rollback + RBAC), fix-PR (Azure + git),
  promotion loop (real PG), fleet isolation (real PG). ADRs 101/102/103/105 technical gates met
  (awaiting owner ratification); ADR-104 deferred.

### Fixed
- **`CONTRIBUTING.md` gave commands that do not exist.** The quality-gate section told
  contributors to run `uv run mypy src` (there is no `src/` in the workspace layout) and
  `uv run ruff check .` (CI lints only `packages/kubeintellect-server/app/` and
  `packages/ki-protocol/`); both are replaced with the exact commands from `ci.yml`, plus a note
  that `make lint`'s `ruff format --check` is not a CI gate. The dev-setup snippet also still
  claimed the archived org mirror was canonical. Added a worked first-PR walkthrough against a
  real open issue.
- **Container image licence label corrected (legal).** `v4/Dockerfile` labelled the image
  `org.opencontainers.image.licenses="MIT"` while the project is AGPL-3.0. Every published image
  would have misrepresented its terms. Now `AGPL-3.0-only`, with `url` and `documentation` labels
  added and the `source` URL cased to match the canonical repo. Caught while inspecting the built
  image before enabling public publishing.
- **v3 HITL fail-open closed (safety).** `v3/app/agent/hitl.py` approval/denial detection now
  matches a leading decisive token, not only exact whole-message phrases — a multi-word denial
  ("no don't do that") is no longer silently treated as approval by the `resume = not is_denial(...)`
  resume gate. Part of the 2026-07-10 v3 code-improvement batch (registry single-source-of-truth,
  bounded `deepagents`, memory/429/snapshot truncation logging, `SNAPSHOT_MAX_CHARS` config) —
  full detail in `v3/CHANGELOG.md`.

## [2.1.0] – 2026-07-05

### Added
- **v5 P0 foundations + P2 investigation-core (additive, all default-off; ADR-019 → v4.x, NOT a fork).**
  P0: K8s-ACI v0 read verbs, the ADR-101 harness subagent contract + read-only fan-out seam and body,
  OTel GenAI spans as hash-chained `decision_log` rows, Wilson-LCB promotion-stats (ADR-102), and the
  OpsMemBench deterministic core + live driver. P2: adversarial verification ladder, never-silent
  responsiveness heartbeat + latency budget, escalation-avoidance briefs, and runbooks-as-skills. Every
  slice is inert unless its `CORTEX_V5_ENABLED` + `KI_V5_*` flag is set (flags off ⇒ byte-identical V4).
  Validated live on a Kind cluster (35/35 probes).
- **Version identity surface (ADR-019).** `GET /healthz` now returns the three version axes — `arm`
  (the `KI_VERSION` generation), `version` (the package SemVer, which is what distinguishes v4 / v4.1 /
  v4.2), and the active `experimental_flags` — so a running instance is fully identifiable. New
  `app/core/version.py` (`version_info` / `version_line`). Server SemVer **2.0.2 → 2.1.0**.

### Added
- **Qwen Cloud support (Qwen Cloud Hackathon — MemoryAgent track).** `LLM_PROVIDER=qwen`
  is a first-class provider that auto-targets Alibaba DashScope's OpenAI-compatible
  endpoint (`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`) with `qwen-max`
  (coordinator/synthesis) + `qwen-plus` (parallel RCA subagents); `DASHSCOPE_API_KEY` /
  `QWEN_API_KEY` are honest aliases for the key. `OPENAI_BASE_URL` is now honored by the
  OpenAI client factory (covers Cortex too). `scripts/verify_qwen.py` checks live chat +
  tool-calling. Cost table extended with qwen-max/plus/turbo pricing.
- **Operator-preference memory (MemoryAgent).** `app/memory/preferences.py` upgrades the
  thin `user_prefs` table into a learned layer: explicit (confidence 1.0, immortal) +
  behaviour-**inferred** preferences (e.g. `default_namespace` from RCA history) with
  confidence, decay, and **forgetting** (`preference_purge()`), learned/forgotten by the
  consolidation worker and injected into the prompt. New `kq preference set/list/forget`
  CLI + `GET/PUT/DELETE /v1/preferences` API.
- **Memory V5 upgrade — hybrid recall + bi-temporal knowledge graph (behind default-off
  flags; MemoryAgent).** Grounded in a state-of-the-art study (`design/memory-v5/`: 619
  systems / 1023 resources / ADRs 013–018). Two additive, flag-gated slices ship inside V4:
  (1) **`MEMORY_HYBRID_RETRIEVAL`** — episode recall fuses the `pg_trgm` channel with a
  full-text `ts_rank` channel via Reciprocal Rank Fusion (RRF, k=60) in one query, with a
  functional FTS GIN index (`idx_episodes_fts`, no table rewrite) and graceful fallback to
  the trigram baseline. (2) **`MEMORY_BITEMPORAL_ENABLED`** — the temporal KG gains a
  transaction-time axis (`ingested_at`/`retracted_at`): event-time `valid_from`, point-in-time
  `as_of(valid_t, tx_t)` queries, retract-not-delete supersede (audit-preserving), and a
  `mean_ingest_lag_seconds` freshness signal. (3) **`MEMORY_KG_PPR`** — multi-hop
  blast-radius over the KG: a bounded ≤3-hop induced subgraph via a recursive CTE, then
  Personalized PageRank computed **in-process** (dependency-free power iteration), so
  `kg.ppr_blast_radius(seeds)` ranks the entities most related to an incident. (4)
  **`MEMORY_WRITE_RECONCILE`** — Mem0-style write reconciliation: `kg.reconcile_edge()`
  decides ADD/UPDATE/**RETRACT**/NOOP against existing memory behind a query-independent
  salience gate (dedup + supersede); RETRACT sets `retracted_at` (never hard-deletes) and
  defaults to ADD when confidence is low. (5) **`MEMORY_PROMOTION`** — the learning loop: the
  consolidation worker promotes verified, recurring episodes into a new `semantic_rules` table
  (IF-context → THEN-guidance); a rule that recurs enough goes `active` (injected into the
  prompt) and is eligible to seed a detector *candidate* (reusing the existing human-review
  flow). (6) **`MEMORY_IMPORTANCE`** — importance/surprise-weighted retention (ADR-017): each
  episode write is scored for `importance` (incident severity — regression > partial > resolved,
  boosted by verified + confidence) and `surprise` (a KG-novelty proxy), stored on new
  `episodes.importance`/`surprise` columns; recall then ranks **recency × importance ×
  relevance** (importance modulates *ranking only*, never retention/audit), and a surprise gate
  drops redundant *low-value* auto-writes (unverified + report-only near-duplicates) while always
  keeping verified/actioned episodes. (7) **`MEMORY_PROSPECTIVE`** — first-class prospective
  memory (ADR-017): a new `prospective_memory` table lets the watchtower record a "re-check
  condition C at/after T" after an autonomous fix ("did the fix hold?"); the consolidation
  scheduler claims due re-checks (atomic `FOR UPDATE SKIP LOCKED`), fires each through the
  autonomy ladder (A0 namespaces never fire), and records the outcome. (8)
  **`MEMORY_SECURITY_HARDENING`** — security-hardened write path (ADR-018): defends the top
  threat the study surfaced, **MINJA-style query-only memory injection**, with a
  write-admission guard of **diverse, non-LLM-primary** validators (design review F5: a quorum
  of the same LLM fails together under MINJA) — provenance/**trust** scoring (sensor = ground
  truth, user-chat = low-trust), a heuristic persistent-instruction injection-signature check,
  a per-requester sliding-window rate limiter, and a caller-supplied contradiction check vs
  high-confidence sensor facts; a quarantined write is dropped, and a `[0,1]` provenance `trust`
  is stamped on every episode. Adds **tamper-evidence** (R8.2): an append-only, per-cluster
  SHA-256 **hash chain** over memory-mutating events (admitted writes and quarantined poison
  attempts) in a new `memory_audit` table, computed with the same primitive as the flight
  recorder (ADR-005) and verifiable via `verify_memory_chain` — a silent edit, delete, or reorder
  of learned memory is detectable. Ships with an always-available **right-to-be-forgotten**
  (`forget_subject`) and the pgbouncer-safe **`SET LOCAL ki.cluster_id`** tenant-context helper
  for the Row-Level-Security scaffolding (RLS policies documented in `schema.sql`, enabled with
  the paired GUC discipline). (9) **`MEMORY_SUMMARY_TREE`** — a RAPTOR/GraphRAG-style **summary
  hierarchy** (spec R7): the consolidation worker builds one deterministic theme summary per
  `(cluster, playbook|namespace)` signature into a new `memory_summaries` table, so the agent can
  answer theme-level questions ("what keeps failing in payments?") without scanning every episode;
  matching theme summaries are injected into the triage prompt alongside episode recall (via
  `memory_loader`), so a query gets cross-incident context, not just the top-k episodes.
  **Regeneration is tied to KG change-rate** — a theme is rebuilt only when new episodes arrived or
  the cluster's KG edge count moved (a conditional `ON CONFLICT … WHERE` upsert), never on a fixed
  clock; abstractive LLM roll-up is deferred (deterministic aggregates today). All default off;
  memory-never-breaks-a-request discipline preserved (the guard fails *open*). All migrations
  additive/idempotent (PostgreSQL 16-compatible).
- **OpsMemBench — an ops-memory benchmark (Memory V5 P9).** A new benchmark harness
  (`evaluation/opsmembench/`) measuring five memory-dependent operator abilities no existing
  benchmark covers — **M1** cross-incident recall, **M2** temporal/time-travel reasoning, **M3**
  knowledge updating/contradiction, **M4** learned detection, **M5** abstention/no-false-memory —
  over versioned, deterministic Kubernetes incident timelines whose gold answers derive from the
  injection script (no LLM judge for the core metrics; leakage-free). Ships pure metric functions
  (recall@k, MRR, set-F1, abstention/false-recall, latency percentiles), a `ScoreCard` grader with
  an `ablation_table` (the paper's core "V5 minus one decision at a time" result), a sample timeline
  (`oomkill-recurrence`), and a CLI (`selftest` / `grade` / `ablation`). Runnable today offline via
  `make opsmembench`; the live cluster driver (drive the agent through fault injection → record
  predictions → `grade`) is the documented completion step. Fills the gap between chat-memory
  benchmarks (LongMemEval, LOCOMO) and memoryless ops-RCA benchmarks (OpenRCA, RCAEval, PetShop).
  A second, harder timeline (`fleet-multi-theme`, distractor-heavy — stresses M1 precision and M5
  abstention) and a one-command ablation demo (`make opsmembench-demo`: No-memory / V4-flat /
  V5-full) reproduce the paper's core table offline, showing memory ability rise monotonically as
  design decisions are added.
- **Multi-cloud deployment — runs on any provider.** First-class Helm overlays,
  `make` targets, and runbooks for **AWS EKS** (`values-aws.yaml.example`,
  `make aws-deploy-kubeintellect`, `docs/deploy/aws.md`, ALB ingress + RDS) and
  **GCP GKE** (`values-gcp.yaml.example`, `make gcp-deploy-kubeintellect`,
  `docs/deploy/gcp.md`, GCE ingress + Cloud SQL), alongside the existing Azure AKS and
  Alibaba ACK/ECS paths. A shared `_cloud-deploy` recipe keeps the LLM provider
  decoupled from the cloud (azure | openai | qwen | anthropic on any of them), and
  `docs/deploy/cloud.md` gains a full provider matrix (ingress class / managed DB /
  storage class per cloud).
- **Memory-ablation experiment harness** (`evaluation/memory_ablation.py`): quantifies
  the "smarter after every incident" claim by running each recurring incident across two
  fresh sessions with the memory hierarchy toggled on vs off, and reports the Encounter-2
  delta in investigation steps / latency / recall rate. Runnable via
  `python -m evaluation.memory_ablation run --arm {on,off}` + `compare`.
- **Full-showcase feature flags are now chart-configurable + a one-file overlay.**
  The chart ConfigMap now exposes `PREDICTIVE_DETECTION_ENABLED`,
  `NL_DETECTOR_AUTHORING_ENABLED`, and `POSTMORTEM_LLM_NARRATIVE` (alongside the
  existing Cortex/Watchtower/Autonomy toggles), and `values-showcase.yaml.example`
  flips every V4 layer on in one file — layer it on any cloud overlay for the full
  MemoryAgent demo.
- **Anthropic/Claude wired into the Helm chart** (`secrets.anthropicApiKey` +
  `ANTHROPIC_API_KEY`/`ANTHROPIC_LARGE_MODEL`/`ANTHROPIC_SMALL_MODEL`), so all four
  LLM providers (azure/openai/qwen/anthropic) are deployable, not just runnable locally.
- **Alibaba Cloud deployment path.** Two overlays: `values-alibaba.yaml.example`
  (managed ACK/ACR/ApsaraDB RDS/ESSD, production-grade) and
  `values-ecs-k3s.yaml.example` (the cost-friendly single-ECS + single-node k3s path:
  in-cluster Postgres on `local-path`, `ClusterIP`, no RDS/SLB, trimmed to a 2C4G box).
  `make alibaba-deploy-kubeintellect` + `scripts/alibaba_ecs_k3s_bootstrap.sh` (one-shot
  k3s+helm install) + `docs/deploy/alibaba.md` runbook with coupon/no-out-of-pocket guardrails.
- **Hackathon submission docs:** `docs/memoryagent-design.md`, `docs/demo-script.md`,
  `docs/architecture-diagram.md`, `docs/qwen-cloud-integration.md`, `SUBMISSION.md`.
- **Three new V4 operator capabilities** (all flag-gated, fail-open, zero-token at
  runtime; ADR-010/011/012):
  - **Anticipatory / predictive detection** (`PREDICTIVE_DETECTION_ENABLED`): trend
    predicates project a range-PromQL metric toward its threshold via a hand-rolled
    least-squares slope and fire a `predicted` finding *before* a slow-burn failure
    (e.g. OOM) manifests. Predicted findings are capped at autonomy `A1` — they
    investigate but never auto-fix.
  - **Grounded incident postmortems** (`GET /v1/episodes/{id}/postmortem`,
    `kq postmortem`): a read-only narrative over the hash-chained flight recorder
    where every line cites its event sequence number and the audit chain is verified;
    an optional LLM narrative is constrained to the recorded events.
  - **Natural-language detector authoring** (`NL_DETECTOR_AUTHORING_ENABLED`,
    `POST /v1/detectors`, `kq detector`): compile a plain-English failure into a
    detector that runs in **shadow** (observes only, never reaches the watchtower)
    until a human promotes it.
- **Unified root infrastructure.** Cluster + observability are now a single
  source at the repository root: a root `Makefile`, `deploy/` (kind, Helm
  Langfuse chart, Grafana, docker-compose monitoring), and `scripts/`
  (`kind/create-kind-cluster.sh`, `langfuse-provision.sh`), shared by all
  versions against one `testbed-v2` cluster and the `monitoring` namespace.
- **Langfuse auto-provisioning** (`make langfuse-provision`): generates a project
  key pair once and injects it into `.env` (and fans it into `v2/v3/v4/.env`),
  then `make langfuse-install` deploys with those keys — eliminating the previous
  manual key sync and the `sk-lf-change-me` placeholder.
- **Per-version cost attribution** via a `KI_VERSION` config field and
  `version:vN` Langfuse trace tags emitted from all instrumented versions.
- **Paper manuscript** (`paper/`): FGCS `elsarticle` source for "From Assistant
  to Autonomous Operator," with the related-work survey, architecture/pillar
  sections, and the multi-version evaluation tables/figures.

### Changed
- **Relicensed v4 from MIT to AGPL-3.0-or-later (dual-licensed).** The open-source
  license is now GNU AGPL-3.0-or-later; a separate commercial license is available
  (`LICENSING.md`). Updated `v4/LICENSE`, `kube-q/LICENSE`, the three `pyproject.toml`
  declarations + OSI classifiers, README badge, and added `CITATION.cff` +
  `THIRD_PARTY_NOTICES.md`.
- **Per-version (`v2/`, `v3/`, `v4/`) Makefiles are now app-only** — shared infra
  targets and duplicated `deploy/`/`scripts/` directories moved to the root.
- **Stronger, independent evaluation judge.** The LLM-as-judge is decoupled from
  the system-under-test's coordinator and points at a separate Azure deployment
  (`EVAL_JUDGE_AZURE_*`), with reasoning-model request support.

### Fixed
- **Langfuse Redis auth**: the Langfuse Redis now runs with `--requirepass` and a
  matching client secret, clearing the recurring `langfuse-worker` `ERR AUTH`
  warnings.
- **Observability ingress**: routed `langfuse.local` / `prometheus.local` /
  `loki.local` correctly (ingress controller pinned to the node that maps host
  port 80), restoring host→cluster reachability for trace/metric/log collection.

### Notes
- The evaluation harness, run artifacts, and `.env` files remain local-only
  (git-ignored); they are not part of the published tree.
