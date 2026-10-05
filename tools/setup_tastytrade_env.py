"""Manual hidden-input setup only. No network, account or repository operation."""
import argparse
import getpass
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile


def save(path: Path, client_secret: str, refresh_token: str):
    if any(not v or any(c in v for c in "\n\r\0") for v in (client_secret, refresh_token)):
        raise ValueError("Credentials must be nonempty single-line values")
    if path.is_symlink():
        raise ValueError("Refusing a symlink env file")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = path.read_text() if path.exists() else ""
    pattern = re.compile(r"^\s*(?:export\s+)?(?:TASTYTRADE_CLIENT_SECRET|TASTYTRADE_REFRESH_TOKEN)=")
    lines = [line for line in old.splitlines() if not pattern.match(line)]
    lines += ["export TASTYTRADE_CLIENT_SECRET="+shlex.quote(client_secret),
              "export TASTYTRADE_REFRESH_TOKEN="+shlex.quote(refresh_token)]
    fd, temporary = tempfile.mkstemp(prefix=".quotes-env-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("\n".join(lines)+"\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path.home()/".config/trading-desk/env")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run this setup in an interactive terminal; hidden input is required")
    save(args.env_file, getpass.getpass("tastytrade OAuth client secret (hidden): "),
         getpass.getpass("tastytrade refresh token (hidden): "))
    print("tastytrade credentials saved; existing settings preserved; file mode 0600. Zero API calls.")


if __name__ == "__main__":
    main()
