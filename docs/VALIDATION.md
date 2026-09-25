# Public source validation

Validated on Gentoo Linux, September 25, 2026, in the separate release checkout:

- Carlos Python backend: 973 unittest cases passed.
- Carlos Qt UI: build passed; client and QML interface CTest groups passed.
- HoloHand native application: build passed; gesture, preview, alignment groups passed.
- Remote backend: nine pytest cases passed (one aiohttp request-key warning).
- Mobile web client: production Vite build passed.
- Chromium browser: passkey pairing, real terminal, file edit/read, CSRF rejection,
  path rejection, and immediate device revocation passed.
- Both pinned hand models downloaded and passed SHA-256 verification.
- Shell scripts passed syntax checks. Installers were not run on the host.

The release scan checks common token patterns, personal runtime paths, and
excluded artifacts in the Git index. It is a heuristic, not a security audit.
Vendored patches, licenses, and Guacamole source retain upstream whitespace.

This does not establish real-room voice reliability, camera tracking accuracy,
iPhone cellular behavior, hardware wake, or FreeBSD compatibility for the modified
release checkout. Live desktop/input tests were not rerun because they can control
the current session. Model runtimes and the patched KRDP/Guacamole native stack
were not rebuilt in this source-release pass. Existing development installations
were not upgraded by preparing or pushing this repository.
