from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import tempfile
import urllib.error
from pathlib import Path
from unittest import TestCase, mock

import channels
import github_bot
import webhooks
from channels.github import GitHubChannel
from channels.signatures import verify_github


class _Headers:
    def get_content_type(self) -> str:
        return "application/json"


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = _Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


def issue_payload(*, action: str = "opened", sender_type: str = "User",
                  include_pull_request: bool = False) -> dict:
    issue = {
        "id": 501, "number": 42, "node_id": "I_kwIssue",
        "title": "Install fails on macOS", "body": "How can I diagnose this?",
        "html_url": "https://github.com/acme/widgets/issues/42",
    }
    if include_pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/pulls/42"}
    return {
        "action": action,
        "issue": issue,
        "repository": {"id": 99, "full_name": "acme/widgets"},
        "installation": {"id": 777},
        "sender": {"id": 12, "login": "octocat", "type": sender_type},
    }


def discussion_payload(*, comment: bool = False) -> dict:
    payload = {
        "action": "created",
        "discussion": {
            "id": 601, "node_id": "D_kwDiscussion", "title": "Configuration help",
            "body": "Which setting should I use?",
        },
        "repository": {"id": 100, "full_name": "acme/widgets"},
        "installation": {"id": 778},
        "sender": {"id": 13, "login": "mona", "type": "User"},
    }
    if comment:
        payload["comment"] = {"id": 602, "node_id": "DC_kwComment",
                              "body": "I have the same question."}
    return payload


class GitHubSignatureTests(TestCase):
    def test_signature_covers_exact_bytes(self) -> None:
        secret = "webhook-secret"
        body = b'{"action":"opened"}'
        signature = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature}
        self.assertTrue(verify_github(headers, body, secret))
        self.assertFalse(verify_github(headers, body + b" ", secret))
        self.assertFalse(verify_github({}, body, secret))


class GitHubParsingTests(TestCase):
    def setUp(self) -> None:
        with mock.patch.dict(os.environ, {
            "FRONTDESK_GITHUB_WEBHOOK_SECRET": "secret",
            "FRONTDESK_GITHUB_TOKEN": "token",
        }):
            self.channel = GitHubChannel()

    def test_issue_becomes_public_tenant_scoped_message(self) -> None:
        messages = self.channel.parse_request(
            {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "delivery-1"},
            json.dumps(issue_payload()).encode())
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.channel, "github")
        self.assertEqual(message.trust, channels.PUBLIC)
        self.assertEqual(message.tenant_id, "github:777")
        self.assertEqual(message.thread_key, "issue:acme/widgets:42")
        self.assertIn("Install fails on macOS", message.text)
        self.assertIn("@octocat", message.text)
        self.assertEqual(message.raw["_frontdesk_delivery"], "delivery-1")
        self.assertEqual(message.raw["_frontdesk_source"],
                         "https://github.com/acme/widgets/issues/42")

    def test_issue_comment_stays_in_issue_thread(self) -> None:
        payload = issue_payload(action="created")
        payload["comment"] = {"id": 502, "body": "Here is the error."}
        messages = self.channel.parse_request(
            {"X-GitHub-Event": "issue_comment"}, json.dumps(payload).encode())
        self.assertEqual(messages[0].thread_key, "issue:acme/widgets:42")
        self.assertIn("Here is the error", messages[0].text)

    def test_discussion_and_comment_share_discussion_thread(self) -> None:
        for event_name, payload in (
            ("discussion", discussion_payload()),
            ("discussion_comment", discussion_payload(comment=True)),
        ):
            with self.subTest(event=event_name):
                messages = self.channel.parse_request(
                    {"X-GitHub-Event": event_name}, json.dumps(payload).encode())
                self.assertEqual(messages[0].thread_key, "discussion:D_kwDiscussion")
                self.assertEqual(messages[0].tenant_id, "github:778")

    def test_bot_pull_request_and_unsupported_action_are_ignored(self) -> None:
        cases = [
            ("issues", issue_payload(sender_type="Bot")),
            ("issues", issue_payload(include_pull_request=True)),
            ("issues", issue_payload(action="closed")),
        ]
        for event_name, payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.channel.parse_request(
                    {"X-GitHub-Event": event_name}, json.dumps(payload).encode()), [])

    def test_malformed_json_is_rejected(self) -> None:
        with self.assertRaisesRegex(channels.ChannelError, "valid JSON"):
            self.channel.parse_request({"X-GitHub-Event": "issues"}, b"{")


class GitHubReplyTests(TestCase):
    def setUp(self) -> None:
        with mock.patch.dict(os.environ, {"FRONTDESK_GITHUB_TOKEN": "short-lived-token"}):
            self.channel = GitHubChannel()

    def test_issue_reply_uses_rest_and_delivery_marker(self) -> None:
        with mock.patch("channels.github.urllib.request.urlopen",
                        return_value=_Response({"id": 1})) as opened:
            message = self.channel.parse_request(
                {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "D-1"},
                json.dumps(issue_payload()).encode())[0]
            self.channel.send_message(message, "Try the doctor command.")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/acme/widgets/issues/42/comments")
        self.assertEqual(request.get_header("Authorization"), "Bearer short-lived-token")
        sent = json.loads(request.data)
        self.assertIn("Try the doctor command", sent["body"])
        self.assertIn("frontdesk-delivery:D-1", sent["body"])

    def test_discussion_reply_uses_graphql_mutation(self) -> None:
        with mock.patch("channels.github.urllib.request.urlopen",
                        return_value=_Response({"data": {"addDiscussionComment": {
                            "comment": {"id": "C1"}}}})) as opened:
            self.channel.send("discussion:D_kwDiscussion", "Known fix")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/graphql")
        sent = json.loads(request.data)
        self.assertEqual(sent["variables"]["discussionId"], "D_kwDiscussion")
        self.assertEqual(sent["variables"]["body"], "Known fix")

    def test_api_failure_is_not_reported_as_success(self) -> None:
        failure = urllib.error.HTTPError(
            "https://api.github.com", 403, "forbidden", {}, io.BytesIO(b'{"message":"denied"}'))
        with mock.patch("channels.github.urllib.request.urlopen", side_effect=failure):
            with self.assertRaisesRegex(channels.ChannelError, "HTTP 403"):
                self.channel.send("issue:acme/widgets:42", "answer")


class GitHubWebhookRoutingTests(TestCase):
    def test_delivery_key_and_tenant_are_github_scoped(self) -> None:
        body = json.dumps(issue_payload()).encode()
        headers = {"X-GitHub-Delivery": "abc-123"}
        self.assertEqual(webhooks.delivery_key("github", headers, body),
                         "github:abc-123")
        self.assertEqual(webhooks.delivery_tenant("github", body), "github:777")

    def test_github_is_in_channel_registry(self) -> None:
        self.assertIn("github", channels.available())

    def test_duplicate_delivery_survives_a_new_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "frontdesk.db")
            with mock.patch.dict(os.environ, {"FRONTDESK_STATE_DB": database}):
                self.assertFalse(webhooks.already_handled(
                    "github:D1", tenant_id="github:777"))
                self.assertTrue(webhooks.already_handled(
                    "github:D1", tenant_id="github:777"))


class GitHubActionEntryPointTests(TestCase):
    def test_action_event_is_dispatched_and_replied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps(issue_payload()), encoding="utf-8")
            fake_message = channels.InboundMessage(
                "github", "12", "issue:acme/widgets:42", "question",
                tenant_id="github:777")
            channel = mock.Mock()
            channel.parse.return_value = [fake_message]
            dispatcher = mock.Mock()
            dispatcher.handle.return_value = "answer"
            with mock.patch.dict(os.environ, {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_EVENT_NAME": "issues",
                "FRONTDESK_GITHUB_PROVIDER": "echo",
            }), mock.patch("github_bot.GitHubChannel", return_value=channel), \
                    mock.patch("github_bot.Dispatcher", return_value=dispatcher):
                self.assertEqual(github_bot.main(), 0)
            dispatcher.handle.assert_called_once_with(fake_message)
            channel.send_message.assert_called_once_with(fake_message, "answer")
