# Releasing (Phase 28)

How the first GitHub release, `v0.1.0`, is cut, what it contains, and how to undo it. The release text is [release-notes-v0.1.0.md](release-notes-v0.1.0.md); the assistant cannot see or change GitHub, so **you** run the commands below.

## What a release is here
A tag on the commit that CI passed on, plus the release notes. GitHub adds the source archives itself. Nothing else is attached:
- **No data, models or secrets.** `.gitignore` keeps generated data, `secrets/`, `api_keys.json` and key files out of Git, and the secret scan runs in CI.
- **No Docker images.** There is no registry; images are built from the Dockerfiles.
- **No claim of stability.** `0.x` means the interfaces may still change. The version is set in `pyproject.toml` and `src/mmrisk/__init__.py` and a test keeps them equal to the first CHANGELOG heading.

## Record the CI run for the release commit first
The scorecard only credits CI that ran after an area's last change. The Phase 28 push changed the documentation area, so until a green run for that push is recorded the scorecard shows it at E2, and the release notes (which say 11 areas at E3) would be ahead of the evidence. So:
1. Push the Phase 28 commits and wait for a green run (`gh run list --limit 1`).
2. In `docs/verification-status.md` put that run's ID after "Latest green run:", and in `docs/scorecard.json` set `ci.run_id`, `ci.match` and `ci.covers_through_phase` to 28 (the ID, the ID, 28).
3. Run `.\scripts\dev.ps1 scorecard`, commit ("docs: record CI run for the release commit"), push, and wait for green again. Tag that commit.

## Before you tag (all must hold)
```powershell
cd C:\Users\geek\projects\mobile-money-fraud-detection-platform
git switch main
git pull
git status --short                      # expected: nothing (clean tree)
gh run list --branch main --limit 1     # expected: the latest run is completed / success
.\scripts\dev.ps1 lint
.\scripts\dev.ps1 secscan               # expected: no secrets found
.\scripts\dev.ps1 scorecard-check       # expected: scorecard check: ok (23 areas)
Select-String '"covers_through_phase": 28' docs\scorecard.json   # expected: one match
git tag -l v0.1.0                       # expected: empty (the tag does not exist yet)
gh release list                         # expected: no v0.1.0
```
The tag must point at a commit whose CI run is green. If the latest run is red or still running, wait and fix it first; do not tag.

## Cut the release
```powershell
git tag -a v0.1.0 -m "v0.1.0: first release (synthetic data, plan-only cloud design)"
git push origin v0.1.0
gh release create v0.1.0 --verify-tag --title "v0.1.0: first release" --notes-file docs/release-notes-v0.1.0.md
gh release view v0.1.0
```
Expected: `gh release view` shows the title and the notes, and the release page lists the tag. Open it in a browser and read the notes once as a stranger would.

## Undo it
A tag and a release are cheap to remove if nobody has built on them:
```powershell
gh release delete v0.1.0 --yes
git push origin --delete v0.1.0
git tag -d v0.1.0
```
Do not move a tag that others may have fetched; cut `v0.1.1` instead.

## Next releases
1. Move the CHANGELOG `[Unreleased]` entries under a new `## [x.y.z] - date` heading.
2. Update the version in `pyproject.toml` and `src/mmrisk/__init__.py`, and `tests/unit/test_smoke.py`.
3. Copy `release-notes-v0.1.0.md` to a new file, update the scorecard counts (a test compares them with the computed levels) and the test count, then follow the steps above.
4. Container image tags in [cloud-deployment.md](cloud-deployment.md) use the version too; keep them in step.

## What the assistant could not check
- That `git tag`, `git push` and `gh release create` work with your credentials and permissions (TO VERIFY by running them).
- How the release page looks. Read it in a browser.
