"""Run one GitHub Actions event through FrontDesk and post the answer."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from channels.dispatch import Dispatcher
from channels.github import GitHubChannel


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    if not event_path or not event_name:
        print("GITHUB_EVENT_PATH and GITHUB_EVENT_NAME are required.", file=sys.stderr)
        return 2
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read the GitHub event: {exc}", file=sys.stderr)
        return 2
    payload["_frontdesk_event"] = event_name
    channel = GitHubChannel()
    messages = channel.parse(json.dumps(payload).encode("utf-8"))
    dispatcher = Dispatcher(
        persona=os.environ.get("FRONTDESK_GITHUB_PERSONA", "github-support"),
        provider=os.environ.get("FRONTDESK_GITHUB_PROVIDER", "auto"),
    )
    try:
        for message in messages:
            reply = dispatcher.handle(message)
            if reply:
                channel.send_message(message, reply)
    except Exception as exc:
        print(f"GitHub support processing failed: {exc}", file=sys.stderr)
        return 1
    print(f"Processed {len(messages)} GitHub support message(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
