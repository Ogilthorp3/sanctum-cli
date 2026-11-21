# Release checklist: sanctum-cli 1.0.0

**Nothing on this list runs before Sunday 2026-11-22 21:38 EST (2026-11-23 02:38 UTC)**, when the
stability soak ends. Until then `release/1.0.0` stays an unmerged branch and no `v1.0.0` tag exists.

What is ready, cut from `6025804` (the tip of `feat/satellite-profile-v2`, whose tree equals `main`
at `32bef66`):

| Branch | Commit | Content |
|---|---|---|
| `fix/tailnet-peer-identity` | `f7ff29dd26874f949ec9dbb049ffde84bbd68b5b` (tree `0794e4f5c1a84a830bfec948d2dd713c88a53440`) | tailnet peer identity fix: `sanctum_cli/hive/peers.py` (new), `sanctum_cli/commands/node.py`, `sanctum_cli/hive/route.py`, `tests/commands/test_node_tailnet_identity.py` (new), `tests/hive/test_hive_route.py`, CHANGELOG Security entry |
| `release/1.0.0` | one commit on top of `f7ff29d` | version 1.0.0 (`pyproject.toml`, `uv.lock`, README status line), CHANGELOG `[1.0.0] - 2026-11-22` and compare links, this file |

The branches were cut on the satellite (worktree `~/Projects/sanctum-cli-1.0`), which cannot reach
GitHub. The hub pushes: every GitHub step below runs in the hub's `sanctum-cli` clone. A satellite
is the kind of machine this release stops trusting, so the hub reviews what it receives (step 1)
before anything leaves for GitHub.

## 0. Gate

```sh
date -u    # must be 2026-11-23 02:38 UTC or later
```

- The soak ended clean (no open incident against the hub or the satellite). If not, stop.
- Decide what the branch deliberately left alone, which only bumped version numbers: README line 9
  still says "public beta", `pyproject.toml` still has `Development Status :: 4 - Beta`, and README
  and SPEC still list Sigstore release signing as "roadmap (v1.0)". Any change there is a commit on
  `release/1.0.0` before step 3, and then runs the gates again.
- The CHANGELOG dates 1.0.0 `2026-11-22`. If the tag is cut after midnight EST, correct the date in
  the `[1.0.0]` heading on `release/1.0.0` first.

## 1. Hand the branches to the hub

On the satellite (a bundle made without a base carries the whole history, so it needs nothing on
the hub):

```sh
git -C ~/Projects/sanctum-cli-1.0 bundle create /tmp/sanctum-cli-1.0.0.bundle \
  fix/tailnet-peer-identity release/1.0.0
shasum -a 256 /tmp/sanctum-cli-1.0.0.bundle
```

The tailnet ACL lets the hub and admin machines reach a satellite, and never lets a satellite SSH to
the hub, so the hub pulls the bundle:

```sh
scp <satellite>:/tmp/sanctum-cli-1.0.0.bundle /tmp/   # on the hub
```

On the hub:

```sh
shasum -a 256 /tmp/sanctum-cli-1.0.0.bundle      # same sum as on the satellite
git fetch origin
git bundle verify /tmp/sanctum-cli-1.0.0.bundle
git fetch /tmp/sanctum-cli-1.0.0.bundle 'refs/heads/*:refs/remotes/sat/*'
git rev-parse sat/fix/tailnet-peer-identity      # f7ff29dd26874f949ec9dbb049ffde84bbd68b5b
git log --oneline -2 sat/release/1.0.0           # the release commit, then f7ff29d
git diff --stat 6025804 origin/main              # empty: main is still the tree the branches were cut from
git diff 6025804 sat/release/1.0.0               # read all of it: the whole release, as received
```

The fix commit must be `f7ff29dd26874f949ec9dbb049ffde84bbd68b5b` and the release commit the one
recorded when the branch was cut; anything else, or a diff touching files beyond those listed in the
table above, means stop. If `git diff --stat 6025804 origin/main` is not empty, `main` moved after the
cut: the rebases below still apply, but review the difference and expect the gates to judge the
combination, not the branches as cut.

## 2. Land the fix on main, CI green

```sh
git switch -c fix/tailnet-peer-identity sat/fix/tailnet-peer-identity
git rebase --onto origin/main 6025804 fix/tailnet-peer-identity   # replays f7ff29d only
git diff --stat sat/fix/tailnet-peer-identity fix/tailnet-peer-identity   # empty when main had not moved
```

Run the CI gates on the hub exactly as CI does (Python 3.12, editable install with the dev extra):

```sh
/opt/homebrew/bin/python3.12 -m venv /tmp/sanctum-1.0.0-venv
/tmp/sanctum-1.0.0-venv/bin/pip install -e '.[dev]'
PATH=/tmp/sanctum-1.0.0-venv/bin:$PATH ruff check sanctum_cli tests
PATH=/tmp/sanctum-1.0.0-venv/bin:$PATH mypy sanctum_cli
PATH=/tmp/sanctum-1.0.0-venv/bin:$PATH pytest
```

At the cut these gave: ruff clean, mypy clean (135 files), pytest 2824 passed, 4 skipped, 1 deselected.

```sh
git push origin fix/tailnet-peer-identity
gh pr create --base main --head fix/tailnet-peer-identity --fill
gh pr checks fix/tailnet-peer-identity --watch            # CI green on the branch
gh pr merge fix/tailnet-peer-identity --merge
git fetch origin
gh run list --branch main --workflow CI --limit 1         # the run for the merge commit
gh run watch <run-id> --exit-status                       # CI green on main
```

## 3. Merge release/1.0.0 and tag v1.0.0 on the merge commit

```sh
git switch -c release/1.0.0 sat/release/1.0.0
git rebase --onto origin/main sat/fix/tailnet-peer-identity release/1.0.0   # replays the release commit
git diff origin/main release/1.0.0 --stat   # only CHANGELOG.md, README.md, RELEASE-CHECKLIST-1.0.0.md, pyproject.toml, uv.lock
git push origin release/1.0.0
gh pr create --base main --head release/1.0.0 --title "release: v1.0.0" --fill
gh pr checks release/1.0.0 --watch
gh pr merge release/1.0.0 --merge
git switch main && git pull --ff-only
git log -1 --format='%H %P %s'              # the merge commit: two parents
gh run watch <run-id of that commit> --exit-status    # CI green on the merge commit before tagging
```

Tag that merge commit (annotated, like `v0.15.5`) and push only the tag:

```sh
git tag -a v1.0.0 -m "sanctum-cli 1.0.0" <merge-commit-sha>
git push origin v1.0.0
git ls-remote origin 'refs/tags/v1.0.0^{}'  # prints <merge-commit-sha>
```

If prior versions have a GitHub release (`gh release view v0.15.5`), make one for 1.0.0 from its
CHANGELOG section:

```sh
awk '/^## \[1\.0\.0\]/{f=1; next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/sanctum-cli-1.0.0-notes.md
gh release create v1.0.0 --title "sanctum-cli 1.0.0" --notes-file /tmp/sanctum-cli-1.0.0-notes.md
```

## 4. The tarball and the Homebrew tap

The tap is `ogilthorp3/homebrew-sanctum`, formula `Formula/sanctum-cli.rb`. It builds from the
GitHub tag tarball (version taken from the URL; no `version` line) into a virtualenv with
`PIP_NO_INDEX=1`, from 66 pinned PyPI resources.

Fetch the tarball, take its sha256, and check it holds exactly the tag's tree:

```sh
curl -fsSL -o /tmp/sanctum-cli-v1.0.0.tar.gz \
  https://github.com/Ogilthorp3/sanctum-cli/archive/refs/tags/v1.0.0.tar.gz
shasum -a 256 /tmp/sanctum-cli-v1.0.0.tar.gz          # this is the formula's sha256
mkdir -p /tmp/v1-tar /tmp/v1-git
tar -xzf /tmp/sanctum-cli-v1.0.0.tar.gz --strip-components=1 -C /tmp/v1-tar
git archive v1.0.0 | tar -x -C /tmp/v1-git
diff -r /tmp/v1-tar /tmp/v1-git && echo "tarball == tag"
curl -fsSL https://github.com/Ogilthorp3/sanctum-cli/archive/refs/tags/v1.0.0.tar.gz | shasum -a 256   # same sum again
```

Edit the formula on a branch of the tap, so the tap's CI tests it before `main` serves it:

```sh
cd "$(brew --repository ogilthorp3/sanctum)"
git switch main && git pull --ff-only
git switch -c sanctum-cli-1.0.0
```

In `Formula/sanctum-cli.rb`:

- `url "https://github.com/Ogilthorp3/sanctum-cli/archive/refs/tags/v1.0.0.tar.gz"`
- `sha256 "<the sum from shasum above>"`
- replace the comment above `url` ("Keep v0.15.5 release tarball until ... tags v1.0.0; bump url +
  sha256 ...") with one saying the formula tracks the v1.0.0 release tarball (head tracks main).
- resources: none change. 1.0.0 declares the same runtime dependencies as `main`; `websocket-client`,
  the one added after 0.15.5, is already pinned (1.9.2). If the tap CI's offline install reports a
  missing distribution, add that resource (sdist URL and sha256 from PyPI) before going further.

```sh
git commit -am "sanctum-cli 1.0.0"
git push origin sanctum-cli-1.0.0
gh run watch "$(gh run list --branch sanctum-cli-1.0.0 --workflow 'Test Formula' --limit 1 \
  --json databaseId --jq '.[0].databaseId')" --exit-status
```

That workflow installs the formula on a clean `macos-latest` runner (downloads the tarball, checks
the sha256, builds the venv), runs `sanctum --version`, `sanctum init`, `sanctum status`, the haus
banner check, and `brew test` (which asserts `1.0.0` in `sanctum --version`). When it is green:

```sh
git switch main && git merge --ff-only sanctum-cli-1.0.0 && git push origin main
```

## 5. Post-release smoke

Run `brew update` once, then keep Homebrew from updating itself between the dry run and the upgrade,
so both see the same formulae. Capture `sanctum self-test --json` before upgrading, to compare.

On the hub:

```sh
brew update
sanctum self-test --json > /tmp/self-test-0.15.5.json
HOMEBREW_NO_AUTO_UPDATE=1 brew upgrade --dry-run sanctum-cli
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew upgrade sanctum-cli
sanctum --version                            # sanctum 1.0.0
sanctum self-test --json                     # tier hub; nothing fails that passed in the capture
sanctum node resolve <hub>                   # this machine's own tailnet address
sanctum node vault-host                      # hop to the same address
```

On the satellite, the brain's virtualenv runs on Homebrew's python@3.12, and upgrading sanctum-cli
can upgrade python@3.12 and its libraries with it. Upgrade only when the dry run moves none of
python@3.12, openssl@3, sqlite, xz, readline or mpdecimal:

```sh
brew update
sanctum self-test --json > /tmp/self-test-0.15.5.json
HOMEBREW_NO_AUTO_UPDATE=1 brew upgrade --dry-run sanctum-cli
#   lists any of python@3.12 openssl@3 sqlite xz readline mpdecimal?  -> stop; do not upgrade the
#   satellite tonight, and plan the brain venv's move first
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew upgrade sanctum-cli
sanctum --version                            # sanctum 1.0.0
sanctum self-test --json                     # tier satellite; hub probes n/a; local brain answers
sanctum node resolve <hub>                   # the hub's address, never the satellite's own
```

## 6. Rollback

- **Before the tag is pushed:** stop. The fix stays on `main` (it is the security fix); drop the
  release PR and branch. Nothing was released: the formula still names 0.15.5.
- **Tag pushed, tap not updated yet:** users are unaffected (the formula still names 0.15.5). Fix
  forward as 1.0.1. Never move or re-push `v1.0.0`: the formula's sha256 pins that tag's tarball.
- **Tap updated, install or smoke fails:** revert the tap commit and push it (its CI must go green on
  the revert), then on each machine that took 1.0.0:

  ```sh
  brew update                                # picks up the reverted formula (0.15.5)
  brew uninstall --force sanctum-cli         # every installed version, the kept 0.15.5 keg included
  HOMEBREW_NO_AUTO_UPDATE=1 brew install --dry-run ogilthorp3/sanctum/sanctum-cli   # satellite: same stop rule as step 5
  HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install ogilthorp3/sanctum/sanctum-cli
  sanctum --version                          # sanctum 0.15.5
  sanctum self-test --json                   # as in the capture
  ```

  Homebrew does not touch `~/.sanctum`. 0.15.5 does not have the identity fix: until 1.0.1, use the
  hub's MagicDNS name directly rather than `sanctum node resolve`, `vault-host`, `route` or
  `export-council`.
- **Satellite, python@3.12 moved anyway:** do not run `brew cleanup` (with cleanup off, the previous
  keg is still in the Cellar); check the brain venv before anything else.
- **Withdraw 1.0.0 from main:** `git revert -m 1 <merge-commit-sha>` reverts the release commit
  only; the identity fix stays.
