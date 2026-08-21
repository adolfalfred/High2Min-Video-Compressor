# Downloading High2Min safely

No GitHub account is required to download a public release.

## Download

Open the [latest High2Min release](https://github.com/adolfalfred/High2Min-Video-Compressor/releases/latest), expand **Assets**, then select:

- Windows: `High2Min-Video-Compressor-VERSION-windows-x86_64.zip`
- Linux: `High2Min-Video-Compressor-VERSION-linux-x86_64.tar.gz`
- Apple Silicon Mac: `High2Min-Video-Compressor-VERSION-macos-arm64.tar.gz`
- Intel Mac: `High2Min-Video-Compressor-VERSION-macos-x86_64.tar.gz`

Download the corresponding `.sha256` file as well. Do not download similarly named files from another website or an issue comment.

## Verify SHA-256

Windows PowerShell:

```powershell
Get-FileHash .\High2Min-Video-Compressor-VERSION-windows-x86_64.zip -Algorithm SHA256
Get-Content .\High2Min-Video-Compressor-VERSION-windows-x86_64.zip.sha256
```

The two long hash values must be identical.

After verification, extract the complete ZIP and double-click
`High2Min Video Compressor.exe`. Do not move the executable away from the accompanying `_internal`
directory. `high2min.exe` is the separate command-line interface for automation.

Linux or macOS:

```sh
shasum -a 256 High2Min-Video-Compressor-VERSION-PLATFORM.tar.gz
cat High2Min-Video-Compressor-VERSION-PLATFORM.tar.gz.sha256
```

## Verify that GitHub built it

With the GitHub CLI installed:

```sh
gh attestation verify High2Min-Video-Compressor-VERSION-PLATFORM.ARCHIVE \
  --repo adolfalfred/High2Min-Video-Compressor
```

A successful result links the archive to this repository, workflow, tag, and commit. This provenance does not replace antivirus scanning or operating-system code signing.

## Unsigned application warnings

The certificate-free releases are not Authenticode-signed or Apple-notarized.

- Windows may display a Microsoft Defender SmartScreen warning because the publisher is unidentified. Keep SmartScreen and Microsoft Defender enabled. Verify the checksum and provenance before deciding whether to run the application.
- macOS normally blocks an unidentified, unnotarized application. The app is ad-hoc signed to verify internal bundle integrity, but this is not an Apple publisher signature. Apple warns that overriding protection carries risk. Only consider **Privacy & Security → Open Anyway** after verifying the release and only on a machine where you accept that risk.

Obtaining Windows and Apple developer signing identities later is the only way to remove these warnings properly.
