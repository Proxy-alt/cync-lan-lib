# Releasing

This repository ships one artifact: the `cync-lan` core protocol library.

| Artifact | Branch | Version lives in | Changelog | Distribution | Tag prefix |
|---|---|---|---|---|---|
| `cync-lan` core protocol library | `main` | `pyproject.toml`'s `version` | `CHANGELOG.md` | PyPI, via Trusted Publishing | `cync-lan-vX.Y.Z` |

Two other artifacts consume this library and are released independently,
from their own repositories:

- [`Proxy-alt/cync-lan-mqtt`](https://github.com/Proxy-alt/cync-lan-mqtt) -
  the `cync-lan-mqtt` Docker/MQTT add-on.
- [`Proxy-alt/cync-lan`](https://github.com/Proxy-alt/cync-lan) - the Home
  Assistant `cync_lan` custom_component, distributed via HACS.

Both depend on this library as a normal PyPI dependency, so bumping it here
does not require bumping either consumer, and vice versa. Neither vendors a
copy.

## Releasing

1. Bump `pyproject.toml`'s `version` (semver).
2. Add a matching entry at the top of `CHANGELOG.md`, **using that exact
   version string as the `### ` heading** - the release workflow parses this
   heading out verbatim.
3. Run the test suite (`pytest tests/`) and confirm it passes before
   pushing. The release workflow also runs it as a gate before tagging or
   publishing, but don't rely on CI to catch something you could've caught
   locally first.
4. Commit and push to `main`.

Tagging, the GitHub release, and the PyPI publish are automated from there.

## Release vs prerelease

Decided from the version string alone, by the workflow's "Classify the
version" step. There is no flag or manual toggle:

| Version   | Result                          |
|-----------|---------------------------------|
| `0.6.0`   | full release                    |
| `0.6.0b1` | prerelease (a beta)             |
| anything else | **the run fails**           |

Anything matching neither shape is a hard error, so a typo like `0.6.O` or
`0.6` fails loudly instead of quietly shipping as the wrong kind.

- **`bN` means digits.** This is not a style preference: the package
  uploads to PyPI, so its version must be valid PEP 440, and PEP 440's
  pre-release segment is `b` followed by a *number*. A commit sha is
  rejected - `packaging.version.Version("0.6.0b2aad577")` raises
  `InvalidVersion`, and the upload would fail *after* the tag and GitHub
  release already existed. (`0.6.0+2aad577` parses, but PyPI refuses local
  versions on upload, so that is no escape hatch either.) The same rule
  applies in both consumer repositories - one rule beats an exception
  nobody remembers.
- **Betas do not need a CHANGELOG entry**; full releases still do. Betas
  get a generated stub instead, so a quick `bN` build does not need a
  changelog edit to get out the door.

## What the workflow does

`.github/workflows/publish_pypi_core.yml`, on a push to `main` that changes
`pyproject.toml`:

1. Runs the test suite as a release gate.
2. Reads the new version out of `pyproject.toml`.
3. Checks whether a tag for that version already exists (`git ls-remote
   --tags`) - if so, does nothing.
4. Extracts that version's own section out of `CHANGELOG.md` (the `###
   <version>` heading must match the version file *exactly*, or this step
   fails loudly rather than silently publishing an empty or wrong release).
   Betas are exempt, per above.
5. Tags the current commit and creates a GitHub Release from it via `gh
   release create`, passing `--latest` or `--prerelease` per the table
   above.
6. Builds and publishes to PyPI via Trusted Publishing (no stored API token
   - PyPI trusts this specific GitHub Actions workflow directly).

The publish step gates on PyPI's own state rather than on the tag check, so
a `workflow_dispatch` re-run can retry a failed upload alone without needing
a new version. It's also `workflow_dispatch`-triggerable for a manual re-run
without an empty commit.

### Trusted Publishing setup

This requires a PyPI "pending publisher" configured once, in PyPI's web UI
(Account Settings -> Publishing):

| Field | Value |
|---|---|
| Repository | `Proxy-alt/cync-lan-lib` |
| Workflow filename | `publish_pypi_core.yml` |
| Environment | `pypi-core` |

**This changed when the library moved out of `Proxy-alt/cync-lan`.** The
old publisher pointed at that repository; a publisher naming the wrong repo
does not fail at tag time, it fails at the upload step, *after* the tag and
GitHub release already exist. If a release tags cleanly but never lands on
PyPI, check this first.

## Why this library has its own repository

It used to live on a `core` branch of `Proxy-alt/cync-lan`, alongside the
add-on (`python`) and the HA integration (`feature/ha-custom-component`) -
three separately-versioned artifacts sharing one GitHub release list.

That broke HACS. HACS does not read the "Latest" flag: it calls the
`/releases` **list** endpoint and takes the first non-draft, non-prerelease
entry in list order, which GitHub returns newest-first by date. So whenever
a `cync-lan-v*` or `cync-lan-mqtt-v*` release happened to be the most
recent, HACS advertised *that* tag as the integration's update - and those
tags point at trees with no `custom_components/` directory, so the download
failed and the update was uninstallable.

Marking the integration's releases `--latest` did not help, because the flag
is never consulted. Cutting the other two with `--latest=false`, which is
what the workflows used to do, was addressing a mechanism that does not
exist. The only lever HACS actually honours is *what is in the release list
at all* - and `hacs.json` has no tag filter. One artifact per repository is
what fixes it, permanently.

Tags are invisible to HACS (it needs published releases), so the historical
`cync-lan-v*` tags left behind in `Proxy-alt/cync-lan` are harmless.
