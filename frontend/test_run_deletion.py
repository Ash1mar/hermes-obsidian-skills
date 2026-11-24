"""Filesystem isolation and recovery checks for permanent console deletion."""

from contextlib import closing, nullcontext
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from server import Console, Handler
from workflow import UserError, save


@unittest.skipUnless(sys.platform == "linux", "console deletion uses Linux paths and locks")
class ConsoleDeletionTests(unittest.TestCase):
    run_id = "console-" + "a" * 20
    vault_name = "删除测试 Vault"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "operator-console"
        self.directory = self.state / self.run_id
        (self.directory / "short").mkdir(parents=True)
        self.vault = self.root / "owned-vault"
        self.manifest = self.vault / "_system/vault.json"
        self.manifest.parent.mkdir(parents=True)
        save(self.manifest, {"vault": {"id": self.run_id, "name": self.vault_name,
                                       "security_domain": "internal-test"}})
        self.original = self.root / "original.pdf"
        self.original.write_bytes(b"original source")
        self.request = {"operation_id": self.run_id, "vault_id": self.run_id,
                        "vault_path": str(self.vault), "vault_name": self.vault_name,
                        "security_domain": "internal-test", "sources": [str(self.original)]}
        save(self.directory / "request.json", self.request)
        self.provider_root = self.root / "provider-state"
        self.provider_root.mkdir()
        self.config = {"state": str(self.state), "writable": True,
                       "provider_state": str(self.provider_root),
                       "provider_config": str(self.root / "adapter.json"),
                       "skills_root": str(self.root / "skills"), "distro": "fixture"}
        save(Path(self.config["provider_config"]), {"transport": "command", "command": ["qmd-like-rag"],
                                                   "state_root": str(self.provider_root)})
        self.console = Console(self.config, [])
        patcher = patch("run_deletion.hold_drained", side_effect=lambda *args, **kwargs: nullcontext([]))
        self.drain = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("run_deletion.remove_boards")
        self.addCleanup(patcher.stop)
        patcher.start()

    def delete(self):
        return self.console.delete(self.run_id, {"confirmation": self.vault_name})

    def start_http(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.console = self.console
        self.addCleanup(server.server_close)
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        self.addCleanup(serving.join, 2)
        self.addCleanup(server.shutdown)

        def request(method, path, body=None):
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as connection:
                data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())

        return request

    def test_http_delete_requires_confirmation_then_removes_record_from_list_and_detail(self):
        request = self.start_http()
        address = f"/api/runs/{self.run_id}"
        _, listing = request("GET", "/api/runs")
        self.assertEqual([row["id"] for row in listing["runs"]], [self.run_id])
        status, error = request("POST", f"{address}/delete", {"vault_name": self.vault_name})
        self.assertEqual(status, 400)
        self.assertIn("error", error)
        self.assertTrue(self.manifest.is_file())

        self.assertEqual(request("POST", f"{address}/delete", {"confirmation": self.vault_name}),
                         (200, {"id": self.run_id, "deleted": True}))
        status, listing = request("GET", "/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(listing["runs"], [])
        self.assertEqual(request("GET", address)[0], 404)

    def test_removes_vault_record_and_derived_index_but_keeps_original_and_sibling(self):
        digest = hashlib.sha256(self.vault.as_posix().encode()).hexdigest()[:12]
        provider = self.provider_root / f"owned-vault-{digest}"
        provider.mkdir()
        (provider / "sync.lock").touch()
        (provider / "bm25.json").write_bytes(b"derived index")
        sibling = self.provider_root / "another-vault"
        sibling.mkdir()
        save(self.state / "active.json", {"run": self.run_id, "attempt": "a-old"})

        self.assertEqual(self.delete(), {"id": self.run_id, "deleted": True})

        self.assertFalse(self.vault.exists())
        self.assertFalse(self.directory.exists())
        self.assertFalse(provider.exists())
        self.assertFalse((self.state / "active.json").exists())
        self.assertEqual(self.console.list_runs(), [])
        self.assertTrue(sibling.is_dir())
        self.assertEqual(self.original.read_bytes(), b"original source")

    def test_rejects_mismatched_confirmation_and_foreign_vault_identity(self):
        with self.assertRaises(UserError):
            self.console.delete(self.run_id, {"confirmation": self.vault_name + " "})
        save(self.manifest, {"vault": {"id": "another-vault"}})
        with self.assertRaises(UserError):
            self.delete()

        self.drain.assert_not_called()
        self.assertTrue(self.manifest.is_file())
        self.assertTrue((self.directory / "request.json").is_file())

    def test_rejects_vault_symlink_and_preserves_target(self):
        other = self.vault.with_name("replacement-vault")
        self.vault.rename(other)
        self.vault.symlink_to(other, target_is_directory=True)

        with self.assertRaises(UserError):
            self.delete()

        self.drain.assert_not_called()
        self.assertTrue(self.vault.is_symlink())
        self.assertTrue((other / "_system/vault.json").is_file())

    def test_rejects_vault_overlap_with_another_run(self):
        other_id = "console-" + "b" * 20
        other = self.state / other_id
        other.mkdir()
        pending_vault = self.vault / "another-vault"
        save(other / "request.json", {**self.request, "operation_id": other_id, "vault_id": other_id,
                                      "vault_path": str(pending_vault)})

        with self.assertRaises(UserError):
            self.delete()

        self.drain.assert_not_called()
        self.assertTrue(self.manifest.is_file())
        self.assertTrue((other / "request.json").is_file())

    def test_refuses_read_only_history_and_active_dispatch(self):
        with closing(self.console.lock()):
            with self.assertRaises(UserError):
                self.delete()
        with self.assertRaises(UserError):
            Console({**self.config, "writable": False}, []).delete(
                self.run_id, {"confirmation": self.vault_name})
        history = self.root / "history"
        self.directory.rename(history)
        with self.assertRaises(UserError):
            Console(self.config, [history]).delete(self.run_id, {"confirmation": self.vault_name})

        self.drain.assert_not_called()
        self.assertTrue(self.manifest.is_file())
        self.assertTrue((history / "request.json").is_file())

    def test_partial_removal_keeps_original_scope_for_retry(self):
        remove_tree = shutil.rmtree

        def fail_during_vault_removal(path, *args, **kwargs):
            if Path(path) == self.vault:
                self.manifest.unlink()
                raise PermissionError("fixture failed partway through removal")
            return remove_tree(path, *args, **kwargs)

        with patch("run_deletion.shutil.rmtree", side_effect=fail_during_vault_removal):
            with self.assertRaises(UserError):
                self.delete()

        rows = self.console.list_runs()
        self.assertEqual([row["id"] for row in rows], [self.run_id])
        self.assertTrue(rows[0]["deletion_pending"])
        self.assertFalse(self.manifest.exists())
        self.assertTrue(self.vault.exists())

        self.console = Console(self.config, [])
        request = self.start_http()
        address = f"/api/runs/{self.run_id}"
        status, pending = request("GET", address)
        self.assertEqual(status, 200)
        self.assertTrue(pending["deletion_pending"])
        self.assertEqual(pending["vault_name"], self.vault_name)
        self.assertEqual(request("POST", f"{address}/delete", {"confirmation": self.vault_name}),
                         (200, {"id": self.run_id, "deleted": True}))
        self.assertEqual(self.console.list_runs(), [])
        self.assertFalse(self.vault.exists())
        self.assertEqual(self.original.read_bytes(), b"original source")
