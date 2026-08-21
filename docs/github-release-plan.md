# High2Min Video Compressor: safe deployment plan

This repository currently uses a certificate-free release path. GitHub builds each platform natively, verifies the archives, generates provenance attestations, and prepares a draft release. Windows Authenticode and Apple Developer ID signing are optional future improvements because those services require separate verified developer identities.

## Current certificate-free plan

1. CI tests Windows, Linux, Apple Silicon macOS, and Intel macOS on standard GitHub-hosted runners.
2. Pushing a matching version tag, such as `v0.8.3`, starts `.github/workflows/release.yml`.
3. Each runner builds and smoke-tests its own binary archive. GitHub records a provenance attestation for the archive.
4. A final job verifies all four internal manifests and SHA-256 sidecars, creates `SHA256SUMS.txt`, and opens a draft GitHub Release.
5. The repository owner reviews the draft and manually publishes it. Anyone can then download it without a GitHub account from the Releases page.
6. Release notes and the download guide clearly state that Windows and macOS binaries are unsigned and explain how to verify provenance and checksums.

This is the strongest practical distribution path without Windows or Apple signing identities, but it cannot remove Microsoft SmartScreen or Apple Gatekeeper publisher warnings.

## Release requirements

- Build every binary on its target operating system and architecture; never label a cross-assembled package as natively tested.
- Keep source videos, compressed book videos, PDFs, `.venv`, local job state, and release archives out of Git history.
- Require all unit tests, CLI contract validation, UI construction, native drag-and-drop loading, live percentage events, bundled FFmpeg, and a synthetic compression test on each native runner.
- Reject any compressed test output that contains audio, cannot decode, exceeds 5 MiB, changes aspect ratio unexpectedly, or falls below the configured SSIM floor.
- Produce SHA-256 sidecars and link each binary to its exact source commit through provenance attestations. Add executable signing, timestamping, and SBOM attestations when the required identities and tooling are available.
- Publish assets to a draft release first. Only publish after all assets, signatures, checksums, and release notes have been independently verified.

## Build and trust flow

```text
protected source tag
        |
        v
native test/build matrix
  | Windows x64  -> native build + checksum + provenance
  | Linux x64    -> deterministic archive + verify
  | macOS arm64  -> native build + checksum + provenance
  ` macOS Intel  -> native build + checksum + provenance
        |
        v
SHA-256 + manifest + SBOM + GitHub provenance attestation
        |
        v
draft GitHub Release -> human approval -> immutable publication
```

## Phase 1: repository preparation

1. Create the GitHub repository from this folder and choose the organization-approved license.
2. Commit source, tests, schemas, build scripts, icon sources/assets, documentation, `pyproject.toml`, and `uv.lock`.
3. Do not commit `releases/` archives. The supplied `.gitignore` keeps the human-readable release index while excluding binaries.
4. Add `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`, and a changelog before the first public release.
5. Enable default-branch protection, secret scanning, dependency review, Dependabot, and mandatory status checks.

## Phase 2: continuous integration

Use a matrix in `.github/workflows/ci.yml`:

| Target | GitHub-hosted runner | Required validation |
|---|---|---|
| Windows x86-64 | `windows-2025` | Unit tests, GUI/console PE subsystem checks, native Tk/TkDND UI smoke, icon, synthetic compression |
| Linux x86-64 | `ubuntu-24.04` | Unit tests, Xvfb Tk/TkDND UI smoke, synthetic compression |
| macOS Apple Silicon | `macos-15` | Unit tests, arm64 app architecture, strict nested code integrity, native app/UI smoke, synthetic compression |
| macOS Intel | `macos-15-intel` | Unit tests, x86-64 app architecture, strict nested code integrity, native app/UI smoke, synthetic compression |

Generate a small synthetic video during the workflow. This prevents copyrighted or pupil-book media from entering GitHub artifacts.

## Phase 3: signed release workflow

Create `.github/workflows/release.yml`, triggered only by a protected semantic tag such as `v0.8.3` and an optional manual dispatch for release candidates.

1. Confirm that the tag, package version, changelog, and release title match.
2. Run the complete native matrix again and build each archive with the existing builders.
3. Windows: currently publish an unsigned native ZIP containing a terminal-free desktop executable and a separate console CLI, with a checksum and provenance attestation. If a signing identity is obtained later, sign both executables and any installer with Azure Artifact Signing or an organization-owned Authenticode certificate before archiving.
4. macOS: currently publish clearly labelled native `.app` archives with ad-hoc bundle-integrity signatures, checksums, and provenance. If an Apple Developer identity is obtained later, sign nested executables and the `.app`, submit it for notarization, staple the ticket, and validate it with Gatekeeper before packaging.
5. Linux: verify executable permissions and archive manifest; add a Sigstore or organization GPG signature if policy requires it.
6. Produce SHA-256 sidecars, `release-index-vVERSION.json`, CycloneDX or SPDX SBOMs, and GitHub build-provenance attestations.
7. Upload per-platform workflow artifacts with short retention. A separate release job downloads and verifies them before creating one draft GitHub Release.
8. A protected `release` environment requires a human reviewer. After approval, publish once and enable immutable releases so the tag and assets cannot be silently replaced.

## Secret and permission policy

- Prefer federated OIDC for Azure; do not store exportable Windows signing keys in the repository.
- Store Apple API key material, issuer/team IDs, and certificate passwords only as protected environment secrets. Restrict the environment to release tags and named reviewers.
- Pin third-party GitHub Actions to full commit SHAs. Grant workflow permissions per job: normally `contents: read`; only the attestation/release job receives `id-token: write`, `attestations: write`, and narrowly scoped `contents: write`.
- Never run a signing workflow from an untrusted pull request or with pull-request-controlled scripts.
- Keep release logs free of secret values and rotate credentials immediately after any suspected disclosure.

## Publication checklist

1. Build `v0.8.3` as a draft release and test downloads on clean Windows, macOS Apple Silicon, macOS Intel, and Linux machines before publication.
2. Confirm Windows signature/SmartScreen behavior and macOS Gatekeeper/notarization without bypass instructions.
3. Verify every `.sha256`, internal release manifest, SBOM, and GitHub attestation from a clean machine.
4. Perform an accessibility pass: keyboard navigation, screen-reader names, visible progress, drag-and-drop fallback, and cancellation/resume.
5. Create the stable release as a draft, attach every final asset, review the generated notes, then publish it as immutable.
6. If any binary is wrong, do not overwrite it. Withdraw the draft or publish a new patch version; never reuse a published immutable tag.

## Trade-offs and later decisions

- Separate Intel and Apple Silicon macOS archives are easier to test and diagnose than a universal bundle; reconsider a universal DMG after stable native CI.
- Cloud Windows signing is operationally safer than exporting a PFX, but it requires Azure identity setup and service cost.
- GitHub-hosted native builds provide clean, reproducible environments; self-hosted runners are justified only for hardware signing or capacity requirements and need stronger isolation.
- Defer automatic updates, Windows ARM64, Linux ARM64, Homebrew, WinGet, and package installers until the four core artifacts have a stable, signed release process.

## Authoritative references

- [GitHub-hosted runner images and architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
- [GitHub immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
- [Microsoft Artifact Signing integrations](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations)
- [Apple notarization requirements](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
