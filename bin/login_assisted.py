#!/usr/bin/env python
"""Garmin login whose MFA code arrives via a file instead of a TTY.

``gh-sync login`` reads the code from stdin, which assumes a human at a
terminal. This variant lets an operator (or an agent driving a browser) fetch
the emailed code and drop it into a file while the login waits. Same token
store, same result: ``~/.garminconnect`` ends up populated.

Usage:
    python bin/login_assisted.py /path/to/code_file
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from gh_sync.config import GARMIN_TOKENS, Settings
from gh_sync.garmin_client import connect

POLL_SECONDS = 2
TIMEOUT_SECONDS = 600


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: login_assisted.py <code_file>", file=sys.stderr)
        return 2
    code_file = Path(sys.argv[1])
    code_file.unlink(missing_ok=True)

    settings = Settings.from_env()

    def wait_for_code() -> str:
        # Printed as a sentinel so the caller knows Garmin has sent the email
        # and it is safe to go looking for it.
        print(f"MFA_REQUESTED writing_to={code_file}", flush=True)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if code_file.exists():
                code = code_file.read_text().strip()
                if code:
                    code_file.unlink(missing_ok=True)
                    print(f"MFA_CODE_RECEIVED len={len(code)}", flush=True)
                    return code
            time.sleep(POLL_SECONDS)
        raise RuntimeError(f"No MFA code appeared in {code_file} within {TIMEOUT_SECONDS}s")

    import gh_sync.garmin_client as garmin_client

    garmin_client._prompt_for_mfa_code = wait_for_code  # noqa: SLF001

    print(f"LOGIN_START email={settings.garmin_email}", flush=True)
    try:
        client = connect(settings.garmin_email, settings.garmin_password, interactive=True)
    except Exception as exc:  # noqa: BLE001
        print(f"LOGIN_FAILED {type(exc).__name__}: {exc}", flush=True)
        return 1

    print(f"LOGIN_OK name={client.get_full_name()} tokens={GARMIN_TOKENS}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
