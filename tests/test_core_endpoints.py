import importlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class CoreEndpointTests(unittest.TestCase):
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
        self.server.API_KEYS_PATH = self.root / "api_keys.json"
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

    def test_auth_me_and_logout(self):
        token = self.login()
        headers = self.auth_headers(token)

        me_response = self.client.get("/auth/me", headers=headers)
        self.assertEqual(me_response.status_code, 200, me_response.text)
        self.assertEqual(me_response.json()["username"], "admin")
        self.assertEqual(me_response.json()["role"], "admin")

        logout_response = self.client.post("/auth/logout", headers=headers)
        self.assertEqual(logout_response.status_code, 200, logout_response.text)

        expired_response = self.client.get("/auth/me", headers=headers)
        self.assertEqual(expired_response.status_code, 401)

    def test_admin_user_crud_and_password_reset(self):
        admin_token = self.login()
        admin_headers = self.auth_headers(admin_token)

        create_response = self.client.post(
            "/admin/users",
            headers=admin_headers,
            json={
                "username": "writer",
                "password": "writer1234",
                "full_name": "Policy Writer",
                "role": "user",
            },
        )
        self.assertEqual(create_response.status_code, 200, create_response.text)
        user_id = create_response.json()["user"]["id"]

        update_response = self.client.patch(
            f"/admin/users/{user_id}",
            headers=admin_headers,
            json={"full_name": "Updated Writer", "role": "read_only", "is_active": True},
        )
        self.assertEqual(update_response.status_code, 200, update_response.text)
        self.assertEqual(update_response.json()["user"]["role"], "read_only")

        reset_response = self.client.post(
            f"/admin/users/{user_id}/reset-password",
            headers=admin_headers,
            json={"password": "newpass1234"},
        )
        self.assertEqual(reset_response.status_code, 200, reset_response.text)

        user_token = self.login("writer", "newpass1234")
        self.assertTrue(user_token)

        delete_response = self.client.delete(f"/admin/users/{user_id}", headers=admin_headers)
        self.assertEqual(delete_response.status_code, 200, delete_response.text)

        login_deleted = self.client.post(
            "/auth/login",
            json={"username": "writer", "password": "newpass1234"},
        )
        self.assertEqual(login_deleted.status_code, 401)

    def test_last_active_admin_cannot_be_disabled_or_deleted(self):
        token = self.login()
        headers = self.auth_headers(token)

        users = self.client.get("/admin/users", headers=headers).json()["users"]
        admin_id = next(user["id"] for user in users if user["username"] == "admin")

        disable_response = self.client.patch(
            f"/admin/users/{admin_id}",
            headers=headers,
            json={"is_active": False},
        )
        self.assertEqual(disable_response.status_code, 400)

        delete_response = self.client.delete(f"/admin/users/{admin_id}", headers=headers)
        self.assertEqual(delete_response.status_code, 400)

    def test_conversation_crud(self):
        token = self.login()
        headers = self.auth_headers(token)

        create_response = self.client.post(
            "/conversations",
            headers=headers,
            json={"title": "Policy chat", "model": "gemini:gemini-2.5-flash"},
        )
        self.assertEqual(create_response.status_code, 200, create_response.text)
        conv_id = create_response.json()["id"]

        list_response = self.client.get("/conversations", headers=headers)
        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertEqual(len(list_response.json()["conversations"]), 1)

        get_response = self.client.get(f"/conversations/{conv_id}", headers=headers)
        self.assertEqual(get_response.status_code, 200, get_response.text)
        self.assertEqual(get_response.json()["title"], "Policy chat")
        self.assertEqual(get_response.json()["messages"], [])

        title_response = self.client.patch(
            f"/conversations/{conv_id}/title",
            headers=headers,
            json={"title": "Updated title"},
        )
        self.assertEqual(title_response.status_code, 200, title_response.text)

        updated_response = self.client.get(f"/conversations/{conv_id}", headers=headers)
        self.assertEqual(updated_response.json()["title"], "Updated title")

        delete_response = self.client.delete(f"/conversations/{conv_id}", headers=headers)
        self.assertEqual(delete_response.status_code, 200, delete_response.text)

        missing_response = self.client.get(f"/conversations/{conv_id}", headers=headers)
        self.assertEqual(missing_response.status_code, 404)

    def test_file_upload_list_download_delete(self):
        token = self.login()
        headers = self.auth_headers(token)

        upload_response = self.client.post(
            "/upload",
            headers=headers,
            files={
                "file": (
                    "policy.txt",
                    b"This policy has enough words to be stored as a text RAG file for endpoint testing.",
                    "text/plain",
                )
            },
        )
        self.assertEqual(upload_response.status_code, 200, upload_response.text)
        self.assertEqual(upload_response.json()["stored_as"], "policy.txt")
        self.assertTrue((self.server.FILES_ROOT / "1" / "policy.txt").exists())

        files_response = self.client.get("/files", headers=headers)
        self.assertEqual(files_response.status_code, 200, files_response.text)
        names = [file["name"] for file in files_response.json()["files"]]
        self.assertIn("policy.txt", names)

        download_response = self.client.get("/files/user/policy/download", headers=headers)
        self.assertEqual(download_response.status_code, 200, download_response.text)
        self.assertIn(b"This policy", download_response.content)

        delete_response = self.client.delete("/files/user/policy", headers=headers)
        self.assertEqual(delete_response.status_code, 200, delete_response.text)
        self.assertFalse((self.server.FILES_ROOT / "1" / "policy.txt").exists())

    def test_health_reports_available_models_without_exposing_keys(self):
        self.server.API_KEYS_PATH.write_text(
            json.dumps({"gemini": {"api_key": "secret-gemini-key"}}),
            encoding="utf-8",
        )
        token = self.login()
        response = self.client.get("/health", headers=self.auth_headers(token))
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["providers"], ["gemini"])
        self.assertIn("gemini:gemini-2.5-flash", [model["id"] for model in data["models"]])
        self.assertNotIn("secret-gemini-key", response.text)

    def test_langchain_missing_dependency_returns_clear_error(self):
        original_import = self.server.__builtins__["__import__"]

        def fake_import(name, *args, **kwargs):
            if name == "langchain_core.messages":
                raise ImportError("mock missing langchain")
            return original_import(name, *args, **kwargs)

        self.server.__builtins__["__import__"] = fake_import
        try:
            with self.assertRaises(Exception) as ctx:
                self.server.to_langchain_messages([self.server.Message(role="user", content="hello")])
            self.assertIn("LangChain is not installed", str(ctx.exception))
        finally:
            self.server.__builtins__["__import__"] = original_import


if __name__ == "__main__":
    unittest.main()
