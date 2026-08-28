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
green. Repos still carrying pre-existing test failures run CI for signal but do
not gate on it — a permanently red required check blocks every promotion and
trains people to ignore the signal.

| Repo | Suite | Gating on qa/main |
|---|---|---|
| `nw` | green (181 passed) | yes (`test-unit`) |
| `cs` | green (503 passed) | yes |
| `ab` | green | yes |
| `dns`, `dhcp`, `ldap`, `opnsense`, `truenas` | green | yes |
| `cppm` | 1 failed / 51 passed | not yet |
| `netbox` | 11 failed / 176 passed | not yet |
| `pxmx` | collection error in `test_normalize_spoke_url.py`, plus 8 failures | not yet |
| `le` | 4 failed / 86 passed | not yet |
| `qa` | collection error (`hub_client` not importable) | not yet |
| `lm` | ~169 pre-existing failures | not yet |
| `kvm`, `tsa` | no test suite | n/a |

Promoting a repo to gating is a one-line ruleset change once its suite is green:
add a `required_status_checks` rule naming the job (`test`) to its
`protect-main` and `protect-qa` rulesets.

## Required repository settings

Two settings must be on, or the pipeline silently half-works:

- **Actions → General → Workflow permissions → "Allow GitHub Actions to create
  and approve pull requests."** Without it the promotion branch is still pushed
  but the PR step fails with `GitHub Actions is not permitted to create or
  approve pull requests`. Set via
  `gh api -X PUT repos/<owner>/<repo>/actions/permissions/workflow -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true`.
- **Automatically delete head branches**, so merged `promote/*` branches are
  cleaned up.

### First promotion PR in a repo needs a one-time workflow approval

`Actions -> General -> Fork pull request workflows from outside collaborators`
defaults to `first_time_contributors`, and `github-actions[bot]` counts as one.
So the **first** promotion PR a repo ever raises shows `action_required` with no
checks reported, and — on a repo that gates on CI — sits `MERGEABLE / BLOCKED`
because the required check can never appear.

Approve that first run once (`gh api -X POST
repos/<owner>/<repo>/actions/runs/<run_id>/approve`, or the "Approve and run"
button). Subsequent promotion PRs run automatically.

Do **not** "fix" this by setting the approval policy to `never`: that also lets
unapproved workflows run for genuine fork PRs from strangers.

## Changing the pipeline

The workflow files, `promote.sh` and `bump_version.py` are **byte-identical
copies in all 16 repos** — each repo runs its own Actions and cannot import from
a sibling. Edit `.pipeline-templates/` in the `nw` repo and re-run the distribute
loop, then re-run the drift sweep. Both are recorded in the `dual-copy-guard`
skill reference (section 10).

`nw/tests/test_promotion_pipeline.py` pins the branch-owned-version guarantee
with a real git sandbox. It only runs in `nw`, so it cannot catch drift in the
other fifteen copies — use the sweep for that.
