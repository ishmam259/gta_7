"""Keep a free-tier deployment from sleeping during the judging window.

Free plans on several hosts spin a service down after ~15 minutes of inactivity and
take 30-60s to wake. The judge's per-request limit is 30s, so a cold start during
evaluation is a failed request. Run this against the deployed base URL for the whole
window and the service never goes idle.

    python scripts/keep_warm.py https://your-host            # ping /health every 10 min
    python scripts/keep_warm.py https://your-host --every 300

Stop it with Ctrl+C when judging is over.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Ping /health so a free-tier host stays awake.")
    parser.add_argument("base_url", help="Deployed service base URL")
    parser.add_argument(
        "--every",
        type=int,
        default=600,
        help="Seconds between pings (default 600; keep it under the host's idle timeout)",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="Per-ping timeout in seconds")
    args = parser.parse_args()

    url = args.base_url.rstrip("/") + "/health"
    print(f"Keeping {url} warm every {args.every}s. Ctrl+C to stop.\n")

    consecutive_failures = 0
    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        started = time.perf_counter()
        try:
            response = httpx.get(url, timeout=args.timeout)
            elapsed = time.perf_counter() - started
            ok = response.status_code == 200 and response.json().get("status") == "ok"
            consecutive_failures = 0 if ok else consecutive_failures + 1
            flag = "" if elapsed < 5 else "   <-- SLOW: it had gone cold"
            print(f"{stamp}  {response.status_code}  {elapsed:5.2f}s{flag}")
        except Exception as exc:
            consecutive_failures += 1
            print(f"{stamp}  FAILED  {type(exc).__name__}: {exc}")

        if consecutive_failures >= 3:
            print(
                f"\n!! {consecutive_failures} consecutive failures - the deployment looks down. "
                "Check the host's dashboard now.\n"
            )

        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
