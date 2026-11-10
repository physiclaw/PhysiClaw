"""Deploy docs, wait for readiness, and audit both live sites locally."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

PRODUCTION = "https://docs.physiclaw.ai"
PREFIX = "physiclaw-hardware-v"
WAIT_TIMEOUT = 900
POLL_INTERVAL = 15


class DeployError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Tee:
    """Keep plain, line-buffered diagnostics in the terminal and the run log."""

    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.console.write(text)
        self.log.write(text)
        self.flush()
        return len(text)

    def flush(self):
        self.console.flush()
        self.log.flush()

    def isatty(self):
        return False


def trigger_hook(url):
    """POST once; never log a hook URL, response body, or raw HTTP exception."""
    try:
        with build_opener(NoRedirect).open(
            Request(url, method="POST"), timeout=90
        ) as response:
            if not 200 <= response.status < 300:
                raise DeployError(f"Docs deploy hook failed: HTTP {response.status}")
            try:
                payload = json.loads(response.read(1024 * 1024))
            except ValueError:
                payload = None
    except HTTPError as exc:
        raise DeployError(f"Docs deploy hook failed: HTTP {exc.code}") from None
    except (OSError, ValueError, HTTPException):
        raise DeployError(
            "Docs deploy hook request failed; it may have been accepted. "
            "Check Cloudflare before retrying."
        ) from None
    # A release tag alone cannot distinguish two deployments of the same release.
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict) or payload.get("success") is not True:
        raise DeployError("Docs hook did not return a successful deployment response")
    url = result.get("url", "")
    if not isinstance(url, str) or not re.fullmatch(
        r"https://[a-f0-9]{8}\.[a-z0-9-]+\.pages\.dev/?", url
    ):
        raise DeployError(
            "Docs hook accepted, but no unique Pages deployment URL was returned. "
            "Inspect Cloudflare; do not treat the existing production site as ready."
        )
    if result.get("environment") != "production" or result.get("is_skipped"):
        raise DeployError("Docs hook did not start a production deployment")
    print("docs-site deploy triggered")
    return url.rstrip("/")


def resolve_tag(pinned):
    if pinned:
        return pinned

    from scripts import check_site_assets as assets

    with tempfile.TemporaryDirectory(prefix="physiclaw-release-") as directory:
        audit = assets.Audit(
            assets.Downloads(Path(directory), 30), assets.INVENTORY, 1, quiet=True
        )
        return audit.newest_hardware(pinned)


def wait_for_deployment(deployment_url, tag, timeout, interval):
    """Probe small uncached markers, never repeatedly download the full audit."""
    deadline = time.monotonic() + timeout
    opener = build_opener(NoRedirect)
    for origin in (deployment_url, PRODUCTION):
        print(f"Waiting for {origin} to serve {tag} (up to {timeout:g}s total)")
        while (remaining := deadline - time.monotonic()) > 0:
            # Bypass negative edge caches too: a new deployment can initially 404.
            request = Request(
                f"{origin}/.release-version?check={time.time_ns()}",
                headers={"Cache-Control": "no-cache", "User-Agent": "physiclaw-deploy"},
            )
            try:
                with opener.open(request, timeout=min(10, remaining)) as response:
                    if (
                        response.status == 200
                        and response.read(1024).decode("utf-8").strip() == tag
                        and time.monotonic() <= deadline
                    ):
                        break
            except HTTPError as exc:
                if exc.code not in {404, 408, 429, 500, 502, 503, 504}:
                    raise DeployError(
                        f"Deployment readiness failed: HTTP {exc.code} at {origin}"
                    ) from None
            except (OSError, HTTPException, UnicodeError):
                pass
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        else:
            raise DeployError(
                f"Deployment readiness timed out after {timeout:g}s at {origin}. "
                "Inspect Cloudflare for a failed or queued build."
            )
    print("Deployment is available; checking both production sites")


def audit_sites(tag, report):
    from scripts import check_site_assets

    return check_site_assets.main(["--hardware-tag", tag, "--report", str(report)])


def run(args, directory):
    tag = args.hardware_tag
    try:
        hook = None
        if not args.check_only:
            path = Path(".docs-deploy-hook")
            hook = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            if not hook:
                raise DeployError(
                    ".docs-deploy-hook missing — put the Cloudflare Pages deploy hook URL in it"
                )
        # Freeze the expected release before the hook, even for a standalone run.
        tag = resolve_tag(tag)
        print(f"Expected hardware release: {tag}")
        if hook:
            deployment_url = trigger_hook(hook)
            (directory / "deployment.json").write_text(
                json.dumps({"url": deployment_url, "hardware_tag": tag}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            wait_for_deployment(
                deployment_url, tag, args.wait_timeout, args.poll_interval
            )
        if audit_sites(tag, directory / "assets.csv"):
            raise DeployError(
                "Site asset verification failed; see assets.csv and check.log"
            )
        print("Site asset verification passed")
        return 0
    except (DeployError, OSError, ValueError, KeyError, HTTPException) as exc:
        print(str(exc), file=sys.stderr)
        version = tag.removeprefix(PREFIX) if tag else "X.Y"
        print(
            f"The published release is unchanged. Check without redeploying: "
            f"make check-site-assets HW_VERSION={version}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        version_arg = f" HW_VERSION={tag.removeprefix(PREFIX)}" if tag else ""
        print(
            "Verification interrupted; an accepted deployment continues in Cloudflare. "
            f"Retry with make check-site-assets{version_arg}.",
            file=sys.stderr,
        )
        return 130


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="audit now without triggering or waiting for a build",
    )
    version = os.environ.get("HW_VERSION", "")
    parser.add_argument(
        "--hardware-tag",
        default=PREFIX + version if version else None,
        help="expected release (default: HW_VERSION, or newest published hardware release)",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=WAIT_TIMEOUT,
        help="readiness deadline in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL,
        help="seconds between readiness probes (default: %(default)s)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("hardware/output/site-asset-report"),
        help="parent directory for per-run reports (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    if args.hardware_tag and not re.fullmatch(
        re.escape(PREFIX) + r"\d+(?:\.\d+)*", args.hardware_tag
    ):
        parser.error("hardware tag must be physiclaw-hardware-vX.Y (or X.Y.Z)")
    for value in (args.wait_timeout, args.poll_interval):
        if not math.isfinite(value) or value <= 0:
            parser.error("wait timeout and poll interval must be finite and positive")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(
        tempfile.mkdtemp(
            prefix=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-"), dir=args.report_dir
        )
    )
    print(f"Site asset report: {directory.resolve()}")
    with (
        (directory / "check.log").open("w", encoding="utf-8") as log,
        redirect_stdout(Tee(sys.stdout, log)),
        redirect_stderr(Tee(sys.stderr, log)),
    ):
        return run(args, directory)


if __name__ == "__main__":
    # Support python scripts/docs_deploy.py and external make -f/runpy callers.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
