#!/usr/bin/env python3
import getpass
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: proxy_auth.py <auth-url> <username>", file=sys.stderr)
        return 2

    auth_url = sys.argv[1]
    username = sys.argv[2]
    password = os.environ.get("VINFAST_PROXY_PASSWORD")
    if not password:
        password = getpass.getpass(f"Proxy password for {username}: ")

    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, auth_url, username, password)
    opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(manager))
    try:
        with opener.open(auth_url, timeout=20) as response:
            print(f">>> Proxy auth response: {response.status}")
    except urllib.error.HTTPError as exc:
        print(f">>> Proxy auth failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f">>> Proxy auth failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
