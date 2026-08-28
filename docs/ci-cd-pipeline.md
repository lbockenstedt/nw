---
title: CI/CD pipeline (dev → qa → main)
summary: How code and versions flow through the three branches in every repo, what gates each hop, and how deployed instances pick changes up.
audience: contributors
applies_to: all 16 repos
---

# CI/CD pipeline: dev → qa → main

Every repo in the workspace uses the same three-branch flow and the same
workflow files.

```
  contributor push ──▶ dev ──promote PR──▶ qa ──promote PR──▶ main
                        │                  │                   │
                     CI runs            CI runs             CI runs
                   (direct push)      (PR required)       (PR required)
                        │                  │                   │
                   dev instances      qa instances       prod instances
```

## Branch roles

| Branch | Who writes to it | Protection | Purpose |
|---|---|---|---|
| `dev` | contributors, directly | force-push + deletion blocked | integration |
| `qa` | promotion PR only | PR required, force-push + deletion blocked | release candidate |
| `main` | promotion PR only | PR required, force-push + deletion blocked | production |

The repository owner keeps an admin bypass so automation can still merge.

## Promotion

`.github/workflows/promote.yml` runs on every push to `dev` and `qa`:

- push to `dev` → prepares a PR into `qa`
- push to `qa` → prepares a PR into `main`
- it can also be triggered by hand (**Actions → promote → Run workflow**)

Rather than raising the PR from `dev` directly, it builds a
`promote/<src>-to-<tgt>` branch. That extra branch is what makes versioning work
(below) and gives one place to resolve the recurring `VERSION` conflict.

If the merge conflicts on anything **other** than `VERSION`, the job fails and
asks a human to resolve it. Bots do not guess at real code.

A promotion that would carry no code change is a no-op: the job reports
"Nothing to promote" and opens no PR. So a `VERSION`-only commit never produces
an empty promotion PR.

## VERSION is branch-owned

**A version set on one branch never leaks to another.** Each branch advances its
own sequence.

If `dev` is on `10.00` and `qa` is on `1.45`, promoting `dev` → `qa` gives qa
`1.46` — never `10.00`. Promoting `qa` → `main` then advances `main` from its own
value (say `1.13` → `1.14`), not from qa's.

This works because `promote.sh` pins every tracked `VERSION` file back to the
target branch's value, then bumps it by one minor step inside the promotion
commit. Carrying **code only** also resolves the `VERSION` merge conflict that
would otherwise stall every promotion PR.

The bump happens *in the PR* rather than as a later push, because `qa` and
`main` require a PR — a bot pushing straight at them would be rejected by the
ruleset.

`MAJOR` is never advanced automatically. At `MINOR` 99 the bump **holds** and
says so loudly in the job log; set the next major by hand.

## How deployed instances pick changes up

Deployment is **pull-based**. A hub polls its own branch and updates itself, so
a host checked out on `dev` follows `dev` and one on `main` follows `main`.

The primary signal is **commit SHA** comparison, not `VERSION` — see
`_update_available` in `lm/core/src/update_pipeline.py`. So `dev` and `qa`
instances still pick up every change even though only promotions bump `VERSION`.
The `VERSION` comparison is a fallback.

## CI

`.github/workflows/ci.yml` runs `pytest` on pushes to `dev` and on PRs into
`main`, `qa` and `dev`.

Most repos inherit `BaseSpoke` from the `lm` repo (imported as `base_spoke` /
`core.src.base_spoke`). Locally that resolves through the sibling `lm` checkout;
in CI the repo is alone, so the workflow sparse-checks-out `lm/core` into `_lm`
and puts it on `PYTHONPATH`. Without that, collection fails with
`ModuleNotFoundError: No module named 'core'`.

A repo's CI is a **required** check on `qa` and `main` only once its suite is
green. A permanently red required check blocks every promotion and trains people
to ignore the signal, so gating was switched on per repo as each suite went
green. **Every repo with a suite now gates.**

| Repo | Suite | Gating on qa/main |
|---|---|---|
| `nw` | green | yes (`lint-python`, `lint-shell`, `test-unit`) |
| `cs` | green (503 passed) | yes |
| `ab` | green | yes |
| `dns`, `dhcp`, `ldap`, `opnsense`, `truenas` | green | yes |
| `lm` | green (~2908 passed, one process per component) | yes (`test`) |
| `pxmx` | green (406 passed) | yes (`test`) |
| `netbox` | green (191 passed) | yes (`test`) |
| `cppm` | green (73 passed) | yes (`test`) |
| `le` | green (90 passed) | yes (`test`) |
| `qa` | green (7 passed) | yes (`test`) |
| `kvm`, `tsa` | no test suite | n/a |

Promoting a repo to gating is a one-line ruleset change once its suite is green:
add a `required_status_checks` rule naming the job (`test`) to its
`protect-main` and `protect-qa` rulesets.

## Hang protection

Every job carries `timeout-minutes: 20` and every pytest invocation runs under
`pytest-timeout` (`--timeout=300 --timeout-method=thread`). This is not
belt-and-braces: `le`'s CI had **never once completed** — each run hung in
"Run tests" and was killed at GitHub's 6h ceiling, so the repo showed a
perpetual *in progress* rather than a red X and the failure stayed invisible.
With both caps a hang now fails in minutes and the thread method prints the
traceback of the test that stuck.

## Required repository settings

Two settings must be on, or the pipeline silently half-works:

- **Actions → General → Workflow permissions → "Allow GitHub Actions to create
  and approve pull requests."** Without it the promotion branch is still pushed
  but the PR step fails with `GitHub Actions is not permitted to create or
  approve pull requests`. Set via
  `gh api -X PUT repos/<owner>/<repo>/actions/permissions/workflow -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true`.
- **Automatically delete head branches**, so merged `promote/*` branches are
  cleaned up.

### Bot-opened PRs and parked CI runs

A PR opened with `GITHUB_TOKEN` does **not** start its checks. GitHub parks the
run as `action_required`. On a repo that requires CI on `qa`/`main` that is a
deadlock: the required check can never report, so the promotion PR sits
`MERGEABLE / BLOCKED` forever.

`promote.yml` handles this itself — after opening the PR it approves the parked
run for that exact promotion commit, so the pipeline stays hands-free.

Two things that do **not** fix it, both verified the hard way:

- Relaxing `Actions -> General -> Fork pull request workflows` (the
  `fork-pr-contributor-approval` policy, e.g. to
  `first_time_contributors_new_to_github`). It is the *bot-authored PR* that is
  gated, not an untrusted contributor, so the run stays parked — and relaxing it
  would weaken a genuine protection against strangers' fork PRs.
- Assuming `first_time_contributors` means literally once. The second promotion
  PR was parked exactly like the first.

The approval step must match on the promotion commit's `head_sha`. Approving
whichever parked run exists first races with the run GitHub is still creating
for the new head, which leaves the PR unchecked.

## Changing the pipeline

The workflow files, `promote.sh` and `bump_version.py` are **byte-identical
copies in all 16 repos** — each repo runs its own Actions and cannot import from
a sibling. Edit `.pipeline-templates/` in the `nw` repo and re-run the distribute
loop, then re-run the drift sweep. Both are recorded in the `dual-copy-guard`
skill reference (section 10).

`nw/tests/test_promotion_pipeline.py` pins the branch-owned-version guarantee
with a real git sandbox. It only runs in `nw`, so it cannot catch drift in the
other fifteen copies — use the sweep for that.

