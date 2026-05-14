import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


LONG_DISCOUNT_TEXT = """
Store discount policy for the butcher counter and meat products.

Every Monday, customers receive a fifty percent discount on meat products. The Monday discount applies to beef, pork, chicken, sausages, and prepared meat bundles sold from the butcher counter. The discount is active for the full business day on Monday, from opening time until closing time, and it is available to any customer who buys eligible meat products.

The Monday meat discount is a regular weekly promotion, not a one-time event. Cashiers must apply the fifty percent reduction at checkout when the purchase contains eligible meat products on a Monday. The promotion does not require a coupon, membership card, manager approval, or minimum purchase amount. The discount should be visible on the receipt as a meat department discount.
""".strip()


LONG_NO_DISCOUNT_TEXT = """
Store discount policy for grocery, meat, and butcher counter products.

The store does not offer discounts on any day of the week. Customers must pay the listed shelf price for all products, including beef, pork, chicken, sausages, prepared meat bundles, fish, bakery items, produce, beverages, household products, and general grocery items. No department has a recurring weekly discount program.

There is no Monday meat discount. Cashiers must not reduce the price of butcher counter products on Monday or on any other day. The store does not authorize automatic percentage reductions, coupon-free discounts, loyalty exceptions, or manager-approved reductions for meat products. All meat products must be sold at the regular listed price unless the owner issues a signed written notice for a specific emergency correction.
""".strip()


class RagPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = importlib.import_module("server")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server.DB_PATH = self.root / "emma.db"
        self.server.FILES_ROOT = self.root / "files"
        self.server.CHUNKS_ROOT = self.root / "chunks"
        self.server.GLOBAL_FILES_DIR = self.server.FILES_ROOT / "global"
        self.server.GLOBAL_CHUNKS_DIR = self.server.CHUNKS_ROOT / "global"
        self.server.LOGS_DIR = self.root / "logs" / "chat_audit"
        self.server.init_db()
        self.client = TestClient(self.server.app)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def login(self, username="admin", password="admin1234"):
        response = self.client.post("/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def auth_headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_process_rag_file_creates_chunks_and_file_index(self):
        files_dir = self.server.user_files_dir(1)
        chunks_dir = self.server.user_chunks_dir(1)
        txt_path = files_dir / "discounts.txt"
        txt_path.write_text(LONG_DISCOUNT_TEXT, encoding="utf-8")

        asyncio.run(self.server.process_rag_file(txt_path, chunks_dir, "user", 1))

        chunk_data = json.loads((chunks_dir / "discounts.json").read_text(encoding="utf-8"))
        self.assertEqual(chunk_data["source"], "discounts.txt")
        self.assertEqual(chunk_data["scope"], "user")
        self.assertEqual(chunk_data["owner_id"], 1)
        self.assertGreater(chunk_data["total"], 0)
        self.assertIn("fifty percent discount", chunk_data["chunks"][0]["text"])

        index = json.loads((files_dir / "files_index.json").read_text(encoding="utf-8"))
        self.assertIn("discounts", index)
        self.assertIn("Store discount policy", index["discounts"])

    def test_process_rag_file_saves_mocked_inconsistencies(self):
        async def fake_compare(**_kwargs):
            return {
                "has_inconsistencies": True,
                "summary": "The discount rules conflict.",
                "items": [
                    {
                        "topic": "Monday meat discount",
                        "new_claim": "No discounts are allowed.",
                        "existing_claim": "A fifty percent Monday discount applies.",
                        "severity": "high",
                    }
                ],
            }

        original_compare = self.server.compare_documents_for_inconsistencies
        self.server.compare_documents_for_inconsistencies = fake_compare
        try:
            user = {"id": 1, "username": "admin", "full_name": "admin", "role": "admin"}
            global_file = self.server.GLOBAL_FILES_DIR / "discounts.txt"
            self.server.GLOBAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
            global_file.write_text(LONG_DISCOUNT_TEXT, encoding="utf-8")
            asyncio.run(
                self.server.process_rag_file(
                    global_file,
                    self.server.GLOBAL_CHUNKS_DIR,
                    "global",
                    None,
                )
            )

            files_dir = self.server.user_files_dir(1)
            chunks_dir = self.server.user_chunks_dir(1)
            new_file = files_dir / "no_discounts.txt"
            new_file.write_text(LONG_NO_DISCOUNT_TEXT, encoding="utf-8")
            asyncio.run(self.server.process_rag_file(new_file, chunks_dir, "user", 1, user))

            conflicts = json.loads((files_dir / "conflicts_index.json").read_text(encoding="utf-8"))
            self.assertTrue(conflicts["no_discounts"]["has_any"])
            match = conflicts["no_discounts"]["matches"][0]
            self.assertEqual(match["name"], "discounts.txt")
            self.assertEqual(match["items"][0]["severity"], "high")
        finally:
            self.server.compare_documents_for_inconsistencies = original_compare

    def test_chat_endpoint_sends_all_visible_chunks_to_model(self):
        captured = {}

        def fake_resolve_model(selection):
            return {"id": selection, "provider": "fake", "model": selection}

        async def fake_generate_ai_reply(_model, messages):
            captured["messages"] = messages
            return "[RAG]\nThe visible RAGs contain conflicting discount rules."

        original_resolve = self.server.resolve_model
        original_generate = self.server.generate_ai_reply
        self.server.resolve_model = fake_resolve_model
        self.server.generate_ai_reply = fake_generate_ai_reply
        try:
            user_files = self.server.user_files_dir(1)
            user_chunks = self.server.user_chunks_dir(1)
            first = user_files / "discounts.txt"
            second = user_files / "no_discounts.txt"
            first.write_text(LONG_DISCOUNT_TEXT, encoding="utf-8")
            second.write_text(LONG_NO_DISCOUNT_TEXT, encoding="utf-8")
            asyncio.run(self.server.process_rag_file(first, user_chunks, "user", 1))
            asyncio.run(self.server.process_rag_file(second, user_chunks, "user", 1))

            token = self.login()
            response = self.client.post(
                "/chat",
                headers=self.auth_headers(token),
                json={
                    "model": "fake:test",
                    "stream": False,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Do we have Monday meat discounts?",
                        }
                    ],
                },
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["tag"], "[RAG]")
            prompt = captured["messages"][-1].content
            self.assertIn("CONTEXT:", prompt)
            self.assertIn("SOURCE: mine/discounts#0000", prompt)
            self.assertIn("SOURCE: mine/no_discounts#0000", prompt)
            self.assertIn("fifty percent discount", prompt)
            self.assertIn("There is no Monday meat discount", prompt)
            self.assertIn("QUESTION:\nDo we have Monday meat discounts?", prompt)
        finally:
            self.server.resolve_model = original_resolve
            self.server.generate_ai_reply = original_generate


if __name__ == "__main__":
    unittest.main()
