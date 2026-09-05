from __future__ import annotations

import os
import threading
import urllib.request
from unittest import TestCase, mock

import server


class CrossPlatformServerTests(TestCase):
    def test_bind_host_accepts_loopback_and_container_wildcard(self) -> None:
        self.assertEqual(server.bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(server.bind_host("0.0.0.0"), "0.0.0.0")
        self.assertEqual(server.bind_host("localhost"), "localhost")

    def test_bind_host_rejects_arbitrary_names_and_ipv6(self) -> None:
        with self.assertRaises(Exception):
            server.bind_host("example.com")
        with self.assertRaises(Exception):
            server.bind_host("::")

    def test_servers_have_public_health_checks(self) -> None:
        secret = "s" * 32
        with mock.patch.dict(os.environ, {"FRONTDESK_AUTH_SECRET": secret}, clear=False):
            web, administration = server.create_servers("127.0.0.1", 0, 0, secret)
        threads = [threading.Thread(target=item.serve_forever, daemon=True)
                   for item in (web, administration)]
        try:
            for thread in threads:
                thread.start()
            for item in (web, administration):
                port = item.server_address[1]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as response:
                    self.assertEqual(response.read(), b"ok")
        finally:
            for item in (web, administration):
                item.shutdown()
                item.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_web_and_admin_ports_must_differ(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be different"):
            server.create_servers("127.0.0.1", 8766, 8766, "s" * 32)
