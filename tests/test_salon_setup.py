import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import admin
import rag


class SalonSetupTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for patcher in (
            mock.patch.object(rag, "KNOWLEDGE_DIR", root / "knowledge"),
            mock.patch.object(rag, "DATA_DIR", root),
            mock.patch.object(rag, "PERSISTENT_KNOWLEDGE", False),
            mock.patch.dict(os.environ, {"FRONTDESK_MULTI_TENANT_KNOWLEDGE": "1"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.values = dict(name="Rose Salon", hours="Monday 9am to 5pm",
                           services="Haircut USD 45", booking_url="https://example.com/book")

    def test_save_reload_search_and_tenant_isolation(self):
        admin.save_salon_profile("salon-a", self.values)
        self.assertEqual(admin.salon_profile("salon-a"), self.values)
        self.assertTrue(rag.search("Haircut", tenant_id="salon-a"))
        self.assertFalse(rag.search("Haircut", tenant_id="salon-b"))
        self.assertEqual(admin.salon_profile("salon-b"), {})
        admin.save_salon_profile("salon-a", {**self.values, "services": "Manicure GBP 20"})
        document = (rag.tenant_paths("salon-a")[0] / "frontdesk-salon-profile.txt").read_text()
        self.assertNotIn("Haircut", document)
        self.assertIn("Manicure GBP 20", document)

    def test_invalid_input_preserves_saved_details(self):
        admin.save_salon_profile("salon-a", self.values)
        for changes in ({"name": ""}, {"services": "a" * 6001},
                        {"booking_url": "javascript:alert(1)"},
                        {"booking_url": "https://user:password@example.com"}):
            with self.assertRaises(ValueError):
                admin.save_salon_profile("salon-a", {**self.values, **changes})
        self.assertEqual(admin.salon_profile("salon-a"), self.values)

    def test_reindex_failure_rolls_back(self):
        admin.save_salon_profile("salon-a", self.values)
        with mock.patch.object(rag, "build_index", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                admin.save_salon_profile("salon-a", {**self.values, "name": "Changed"})
        self.assertEqual(admin.salon_profile("salon-a"), self.values)
        self.assertTrue(rag.search("Haircut", tenant_id="salon-a"))

    def test_html_is_escaped_and_booking_is_optional(self):
        admin.save_salon_profile("salon-a", {**self.values, "booking_url": ""})
        page = admin.salon_setup_form({"name": '"><script>alert(1)</script>'}, "csrf")
        self.assertNotIn("<script>", page)
        self.assertIn('name="csrf"', page)

    def test_shared_fallback_cannot_receive_tenant_setup(self):
        with mock.patch.dict(os.environ, {"FRONTDESK_MULTI_TENANT_KNOWLEDGE": "0"}):
            with self.assertRaises(ValueError):
                admin.save_salon_profile("salon-a", self.values)
