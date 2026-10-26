"""Trigger the configured docs hook with the same behavior on every OS."""

import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    hook = Path(".docs-deploy-hook")
    if not hook.is_file() or not (url := hook.read_text(encoding="utf-8").strip()):
        print(
            ".docs-deploy-hook missing — put the Cloudflare Pages deploy hook URL in it",
            file=sys.stderr,
        )
        return 1
    try:
        with build_opener(NoRedirect).open(
            Request(url, method="POST"), timeout=90
        ) as response:
            if not 200 <= response.status < 300:
                print(
                    f"Docs deploy hook failed: HTTP {response.status}", file=sys.stderr
                )
                return 1
    except HTTPError as exc:
        print(f"Docs deploy hook failed: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (OSError, URLError, ValueError, HTTPException):
        # The hook URL is a secret: do not echo exception messages containing it.
        print("Docs deploy hook request failed", file=sys.stderr)
        return 1
    print("docs-site deploy triggered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
