# Release process

This document describes how a new version of `drove` is cut. The process is **fully automated** by [`python-semantic-release`](https://python-semantic-release.readthedocs.io/) and runs on every push to `main`. As the repo maintainer you do **not** edit version numbers, tag commits, or publish releases by hand — your job is to make sure each PR carries the correct metadata so the automation produces the right result.

## TL;DR

1. Author a PR. Pick a [Conventional Commit](https://www.conventionalcommits.org/en/v1.0.0/) type for the **PR title**.
2. Add a bullet to `CHANGELOG.md` under `## [Unreleased]`.
3. **Squash-merge** to `main`.
4. The `Release` workflow bumps the version, rewrites the changelog, tags `vX.Y.Z`, and creates a GitHub Release.

If you only want a checklist, jump to [Maintainer checklist](#maintainer-checklist).

---

## Components involved

| Component | File | Purpose |
|---|---|---|
| Release workflow | `.github/workflows/release.yml` | Runs `python-semantic-release` on every push to `main`. |
| PR-title lint | `.github/workflows/pr-title.yml` | Enforces Conventional Commits on PR titles via `amannn/action-semantic-pull-request@v6`. |
| Semantic-release config | `pyproject.toml` (`[tool.semantic_release]`) | Defines how versions, tags, and the changelog are produced. |
| Version source of truth | `pyproject.toml` → `project.version` | The field semantic-release rewrites. |
| Human changelog | `CHANGELOG.md` | The `[Unreleased]` section is promoted to a versioned section on each release. |
| Contributor rules | `AGENTS.md` (Changelog section) | States that every change must add an `[Unreleased]` entry. |

---

## Step 1 — Write the PR

### 1a. PR title (drives the version bump)

The squash-merge commit on `main` takes the **PR title**, and that commit message is what semantic-release parses. So the PR title alone determines whether a release is cut and what kind.

Allowed types (from `pr-title.yml` and `pyproject.toml:77`):

```
build, chore, ci, docs, feat, fix, perf, refactor, style, test
```

Bump rules (from `pyproject.toml:78-79`):

| PR title prefix | Result |
|---|---|
| `fix: …` | **patch** bump (e.g. `0.1.0` → `0.1.1`) |
| `perf: …` | **patch** bump |
| `feat: …` | **minor** bump (e.g. `0.1.1` → `0.2.0`) |
| `feat!: …` *or* `BREAKING CHANGE:` in commit body | **major** bump (e.g. `0.2.0` → `1.0.0`) |
| `chore:`, `ci:`, `style:`, `test:`, `build:`, `docs:`, `refactor:` | **No release.** Also excluded from the rendered changelog (`pyproject.toml:69-74`). |

Title format constraints (from `pr-title.yml:31-33`):

- Subject must start with a letter.
- Subject must not end with a period.
- A scope is optional: `feat(cli): …` is fine, `feat: …` is fine.

**Examples of correct PR titles**

```
fix(proxy): return 503 when llama-server is unreachable
feat(cli): add `models prune` command
feat(api)!: rename /v1/completions request schema
perf(loader): cache tokenizer between requests
docs: clarify make install usage
```

**Examples of incorrect PR titles** (the `PR Title` check will fail):

```
Fix proxy 503 handling          # missing type
feat: Add models prune.         # ends with a period
update: tweak loader            # 'update' is not an allowed type
```

### 1b. Breaking changes

To force a major bump, either:

- Put `!` after the type/scope in the PR title:
  `feat(api)!: drop legacy /completions endpoint`
- **Or** include a `BREAKING CHANGE:` footer in the squash-merge commit body. Because the body comes from the PR description on squash-merge, write it like this in the PR description:

  ```
  Drops the legacy /completions endpoint in favour of /v1/chat/completions.

  BREAKING CHANGE: clients calling /completions must migrate.
  ```

### 1c. CHANGELOG entry (required for every PR)

Per `AGENTS.md:101`, every PR must add a bullet to `## [Unreleased]` in `CHANGELOG.md`. This applies to fixes, features, refactors, docs, and dependency bumps.

Rules:

- Use a Keep-a-Changelog subsection: `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, or `### Security`. Create the subsection under `[Unreleased]` if it doesn't exist yet.
- One bullet per user-visible change, **past tense**, describing impact not implementation.
- **Never** add a version number or release date — the automation does that.
- The entry's category should be consistent with the PR-title type (a `feat:` typically lands under `### Added` or `### Changed`; a `fix:` under `### Fixed`).

**Example diff to `CHANGELOG.md` for a `feat:` PR**

```diff
 ## [Unreleased]

 ### Added
 - Documented the repository install script in the README install instructions.
+- `drove models prune` command that removes unreferenced GGUF files from the model store.

 ### Fixed
 - Fixed model-store test lint issues so full-repository Ruff checks pass.
```

---

## Step 2 — Merge to `main`

Use **Squash and merge**. This is what makes the PR title become the commit message that semantic-release parses.

- Verify the squash-merge commit message in the GitHub merge dialog before clicking. GitHub may pre-fill it with the PR title (good) or with a list of all your branch commits (bad — semantic-release would parse those individually). Edit it down to the single Conventional Commit line if needed.
- Do **not** push directly to `main`. Always go through a PR so the title lint runs.
- Do **not** rebase- or merge-commit a feature branch into `main` unless every commit on that branch is itself a valid Conventional Commit; otherwise the parser may pick up unintended types.

---

## Step 3 — What the Release workflow does

`.github/workflows/release.yml` triggers on `push: branches: [main]` and runs `python-semantic-release/python-semantic-release@v9`. Configured by `[tool.semantic_release]` in `pyproject.toml`:

1. **Inspects commits since the last `vX.Y.Z` tag.** If none of them imply a bump (e.g. only `chore:` / `docs:` / `style:` / `test:` / `ci:`), it exits and **no release is made**.
2. **Computes the next version** based on the highest-impact commit (`feat!` > `feat` > `fix`/`perf`).
3. **Rewrites `pyproject.toml`**: `project.version` → new version (`version_toml = ["pyproject.toml:project.version"]`).
4. **Rewrites `CHANGELOG.md`**: moves the contents of `## [Unreleased]` into a new `## [X.Y.Z]` section, leaving `[Unreleased]` empty for the next cycle. Commits matching `^chore(\(.*\))?:`, `^ci…`, `^style…`, `^test…` are excluded from the auto-generated section (`pyproject.toml:69-74`).
5. **Commits** the changes back to `main` with message:

   ```
   chore(release): vX.Y.Z

   [skip ci]
   ```

   The `[skip ci]` token prevents the release workflow from re-triggering itself.
6. **Tags** the commit `vX.Y.Z` (`tag_format = "v{version}"`).
7. **Creates a GitHub Release** for that tag, with the rendered changelog as the body.

The job uses `permissions: contents: write` and the default `GITHUB_TOKEN`; no PAT or extra secret is required.

> **Note:** there is no PyPI publish step. `drove` is distributed via `make install` / `uv tool install` from the GitHub repository (smoke-tested by `.github/workflows/install.yml`), not PyPI. Adding PyPI publishing later would mean adding a `publish` step to `release.yml` and a `PYPI_TOKEN` secret.

---

## Worked example

Starting state: `pyproject.toml` says `version = "0.1.0"`, `CHANGELOG.md` has entries under `[Unreleased]`.

1. You merge a PR titled `feat(cli): add models prune command`.
2. GitHub pushes the squash commit to `main`.
3. `Release` workflow runs:
   - Sees a `feat:` commit since `v0.1.0` → next version is `0.2.0`.
   - Rewrites `pyproject.toml`: `version = "0.2.0"`.
   - Rewrites `CHANGELOG.md`:

     ```diff
     -## [Unreleased]
     -
     -### Added
     -- `drove models prune` command that removes unreferenced GGUF files from the model store.
     +## [Unreleased]
     +
     +## [0.2.0]
     +
     +### Added
     +- `drove models prune` command that removes unreferenced GGUF files from the model store.
     ```

   - Commits `chore(release): v0.2.0` to `main`.
   - Tags `v0.2.0`.
   - Publishes GitHub Release `v0.2.0` with the changelog body.

---

## Maintainer checklist

Before merging a PR:

- [ ] PR title is a valid Conventional Commit (the `PR Title` check is green).
- [ ] PR title's type matches the intent (`feat`, `fix`, `perf` for user-visible changes; `chore`/`ci`/`style`/`test`/`docs`/`refactor`/`build` for invisible changes).
- [ ] If breaking, `!` is in the title or `BREAKING CHANGE:` is in the description body.
- [ ] `CHANGELOG.md` has a new bullet under `[Unreleased]` in the right Keep-a-Changelog subsection.
- [ ] No manual edits to `project.version` in `pyproject.toml`.
- [ ] No manually created `vX.Y.Z` tag or GitHub Release.
- [ ] Squash-merge is selected and the squash commit message is the PR title (not a list of branch commits).

After merging:

- [ ] The `Release` workflow run for the merge commit is green.
- [ ] If a release was expected, a new `vX.Y.Z` tag and GitHub Release exist.
- [ ] If no release was expected (e.g. `docs:`-only PR), the workflow exited cleanly without bumping.

---

## Troubleshooting

**The `PR Title` check is failing.**
Edit the PR title to match `<type>(optional-scope)!?: <subject>`, where `<type>` is one of the allowed types and `<subject>` starts with a letter and does not end with a period. The check re-runs on title edits.

**I merged but no release was cut.**
Open the latest `Release` workflow run. The most likely causes:

- The squash-merge commit message didn't start with a release-triggering type. `chore:`, `ci:`, `style:`, `test:`, `docs:`, `refactor:`, and `build:` do not bump. If a release was expected, fix forward by landing a follow-up `feat:` or `fix:` PR.
- GitHub put the full list of branch commits into the squash commit body and none were `feat`/`fix`/`perf`. Same fix-forward approach.

**The wrong version was bumped (e.g. patch instead of minor).**
The bump is governed by the highest-impact commit since the last tag. If a `feat:` was mistitled as `fix:`, the next `feat:`-typed PR you land will produce the correct minor bump on top of the released patch — semantic-release will not retroactively re-tag.

**`CHANGELOG.md` ended up with an empty version section.**
This happens when every commit since the last tag matched an excluded pattern (`chore`, `ci`, `style`, `test`). If a release was nonetheless cut (e.g. via a `feat:`/`fix:` mixed in), edit the changelog by hand in a follow-up `docs:` PR — do not retag.

**I need to release urgently from a branch other than `main`.**
Not supported by the current configuration: `[tool.semantic_release.branches.main]` matches only `main` (`pyproject.toml:82-84`). Land the change on `main` via a hotfix PR.

**I need to skip a release for a particular merge.**
Use a non-bumping type (`chore:`, `docs:`, `refactor:`, etc.) for the PR title. The merge will still happen; no version will be cut.

---

## What you should never do manually

- Edit `project.version` in `pyproject.toml`.
- Add a `## [X.Y.Z]` heading or release date to `CHANGELOG.md`.
- Create a `vX.Y.Z` git tag.
- Create a GitHub Release.
- Push directly to `main`.

The automation owns all of these. Doing them by hand will desynchronise the version, tags, and changelog and confuse the next semantic-release run.
