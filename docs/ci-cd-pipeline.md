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

CI is a **required** check on `qa` and `main` for repos whose suite is green.
Repos still carrying test failures run CI for signal but do not gate on it yet;
see the table in the pipeline PR.

## Changing the pipeline

The workflow files, `promote.sh` and `bump_version.py` are **byte-identical
copies in all 16 repos** — each repo runs its own Actions and cannot import from
a sibling. Edit `.pipeline-templates/` in the `nw` repo and re-run the distribute
loop, then re-run the drift sweep. Both are recorded in the `dual-copy-guard`
skill reference (section 10).

`nw/tests/test_promotion_pipeline.py` pins the branch-owned-version guarantee
with a real git sandbox. It only runs in `nw`, so it cannot catch drift in the
other fifteen copies — use the sweep for that.
