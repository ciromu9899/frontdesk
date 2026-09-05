"""GitHub Issues and Discussions support channel.

The adapter accepts only signed webhooks, ignores bot output to prevent reply
loops, and keeps every GitHub installation in a separate FrontDesk tenant.
Replies use the narrow token supplied by the runtime. In GitHub Actions this is
the short-lived GITHUB_TOKEN; a GitHub App deployment supplies an installation
access token as FRONTDESK_GITHUB_TOKEN.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import resilience
from channels.base import PUBLIC, ChannelError, InboundMessage
from channels.signatures import verify_github

REST_BASE = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
API_VERSION = "2026-03-10"
SUPPORTED_ACTIONS = {
    "issues": {"opened", "reopened"},
    "issue_comment": {"created"},
    "discussion": {"created", "reopened"},
    "discussion_comment": {"created"},
}


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _headers(headers: dict) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _event_name(payload: dict) -> str:
    if "discussion" in payload:
        return "discussion_comment" if "comment" in payload else "discussion"
    if "issue" in payload:
        return "issue_comment" if "comment" in payload else "issues"
    return ""


def _event_identity(payload: dict, event_name: str) -> str:
    subject = payload.get("comment") or payload.get("discussion") or payload.get("issue") or {}
    return ":".join((event_name, str(payload.get("action", "")),
                     str(subject.get("id") or subject.get("node_id") or "")))


class GitHubChannel:
    name = "github"

    def __init__(self) -> None:
        self.webhook_secret = os.environ.get("FRONTDESK_GITHUB_WEBHOOK_SECRET", "")
        self.token = os.environ.get("FRONTDESK_GITHUB_TOKEN", "") or os.environ.get(
            "GITHUB_TOKEN", "")
        self.bot_login = os.environ.get("FRONTDESK_GITHUB_BOT_LOGIN", "").lower()
        self.api_base = os.environ.get("FRONTDESK_GITHUB_API_URL", REST_BASE).rstrip("/")
        self.graphql_url = os.environ.get("FRONTDESK_GITHUB_GRAPHQL_URL", GRAPHQL_URL)
        self._circuit = resilience.CircuitBreaker()

    def configured(self) -> bool:
        return bool(self.webhook_secret and self.token)

    def verify(self, headers: dict, body: bytes) -> bool:
        return verify_github(headers, body, self.webhook_secret)

    def parse(self, body: bytes) -> list[InboundMessage]:
        return self.parse_request({}, body)

    def parse_request(self, headers: dict, body: bytes) -> list[InboundMessage]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ChannelError("GitHub payload was not valid JSON.") from None
        if not isinstance(payload, dict):
            raise ChannelError("GitHub payload must be a JSON object.")

        lowered = _headers(headers)
        event_name = lowered.get("x-github-event") or str(
            payload.pop("_frontdesk_event", "")) or _event_name(payload)
        action = str(payload.get("action", ""))
        if action not in SUPPORTED_ACTIONS.get(event_name, set()):
            return []

        sender = payload.get("sender") or {}
        login = str(sender.get("login", "")).strip()
        sender_type = str(sender.get("type", ""))
        if (not login or sender_type.lower() == "bot" or login.lower().endswith("[bot]")
                or (self.bot_login and login.lower() == self.bot_login)):
            return []

        repository = payload.get("repository") or {}
        full_name = str(repository.get("full_name", "")).strip()
        if not full_name or "/" not in full_name:
            return []
        installation = payload.get("installation") or {}
        tenant_key = str(installation.get("id") or repository.get("id") or full_name)

        if event_name in {"issues", "issue_comment"}:
            issue = payload.get("issue") or {}
            if issue.get("pull_request"):
                return []
            number = issue.get("number")
            if not number:
                return []
            content = payload.get("comment") or issue
            title = str(issue.get("title", "")).strip()
            text = str(content.get("body", "")).strip()
            thread_key = f"issue:{full_name}:{number}"
            kind = "Issue comment" if event_name == "issue_comment" else "Issue"
        else:
            discussion = payload.get("discussion") or {}
            node_id = str(discussion.get("node_id", "")).strip()
            if not node_id:
                return []
            content = payload.get("comment") or discussion
            title = str(discussion.get("title", "")).strip()
            text = str(content.get("body", "")).strip()
            thread_key = f"discussion:{node_id}"
            kind = "Discussion comment" if event_name == "discussion_comment" else "Discussion"

        if not text:
            return []
        delivery = lowered.get("x-github-delivery") or _event_identity(payload, event_name)
        raw = dict(payload)
        raw["_frontdesk_delivery"] = delivery
        raw["_frontdesk_event"] = event_name
        raw["_frontdesk_source"] = str(content.get("html_url") or
                                        (payload.get("issue") or {}).get("html_url") or
                                        (payload.get("discussion") or {}).get("html_url") or "")
        prompt = (
            f"GitHub {kind} in {full_name}. Title: {title or '(no title)'}\n\n"
            f"Message from @{login}:\n{text}"
        )
        return [InboundMessage(
            channel=self.name,
            external_user_id=str(sender.get("id") or login),
            display_name=login,
            thread_key=thread_key,
            text=prompt,
            trust=PUBLIC,
            raw=raw,
            tenant_id=f"github:{tenant_key}",
        )]

    def send(self, thread_key: str, text: str) -> None:
        self._send(thread_key, text, "")

    def send_message(self, message: InboundMessage, text: str) -> None:
        delivery = str(message.raw.get("_frontdesk_delivery", ""))
        self._send(message.thread_key, text, delivery)

    def _send(self, thread_key: str, text: str, delivery: str) -> None:
        if not self.token:
            raise ChannelError("FRONTDESK_GITHUB_TOKEN or GITHUB_TOKEN is not set.")
        marker = f"<!-- frontdesk-delivery:{delivery} -->" if delivery else ""
        body = text.strip() + (f"\n\n{marker}" if marker else "")
        if thread_key.startswith("issue:"):
            target = thread_key[len("issue:"):]
            repository, separator, number = target.rpartition(":")
            if not separator or "/" not in repository or not number.isdigit():
                raise ChannelError("Invalid GitHub issue thread key.")
            owner, repo = repository.split("/", 1)
            path = (f"/repos/{urllib.parse.quote(owner, safe='')}/"
                    f"{urllib.parse.quote(repo, safe='')}/issues/{number}/comments")
            self._post_json(self.api_base + path, {"body": body})
            return
        if thread_key.startswith("discussion:"):
            discussion_id = thread_key[len("discussion:"):]
            query = (
                "mutation AddFrontDeskDiscussionComment($discussionId:ID!,$body:String!){"
                "addDiscussionComment(input:{discussionId:$discussionId,body:$body}){"
                "comment{id url}}}"
            )
            result = self._post_json(self.graphql_url, {
                "query": query,
                "variables": {"discussionId": discussion_id, "body": body},
            })
            if result.get("errors"):
                raise ChannelError(f"GitHub rejected the discussion reply: {result['errors']}")
            return
        raise ChannelError("Unknown GitHub thread key.")

    def _post_json(self, url: str, payload: dict) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "FrontDesk-GitHub-Support-Bot",
            },
            method="POST",
        )

        def perform() -> dict:
            with urllib.request.urlopen(  # nosec B310 - origins are fixed/configured by admin
                request, timeout=15, context=_ssl_context()
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            return resilience.execute(perform, retry_safe=False, breaker=self._circuit)
        except resilience.CircuitOpenError:
            raise ChannelError("GitHub circuit is open after repeated failures.") from None
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise ChannelError(f"GitHub API returned HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ChannelError(f"Could not reach GitHub: {exc}") from None
