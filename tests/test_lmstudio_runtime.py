"""Dedicated LM Studio ownership and startup contracts; no real model/server."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lmstudio_runtime as lm


class LMStudioRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = self.root / "LM Studio.exe"
        self.app.touch()
        self.lms = self.root / "lms.exe"
        self.lms.touch()
        self.profile = self.root / "managed"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.core_patch = patch.object(lm, "_other_core_processes", return_value=[])
        self.core_patch.start()
        self.addCleanup(self.core_patch.stop)

    def identity(self):
        return {"pid": 987654, "create_time": 123.5,
                "username": lm.psutil.Process().username(), "exe": str(self.app),
                "cmdline": [str(self.app), "--run-as-service", "--user-data-dir=" + str(self.profile / "AppData" / "Roaming" / "LM Studio")]}

    def backend(self):
        engine = self.root / "installed-backends" / "llama.cpp-fixture-1.2.3"
        engine.mkdir(parents=True)
        vendor = engine.parent / "vendor" / "fixture-cuda-vendor"
        vendor.mkdir(parents=True)
        (engine / "llama-server.exe").write_bytes(b"synthetic executable")
        (engine / "engine.node").write_bytes(b"synthetic engine library")
        (vendor / "dependency.dll").write_bytes(b"synthetic vendor library")
        manifest = {"manifest_version": "4", "extension_type": "engine", "engine": "llama.cpp",
                    "platform": "win", "cpu": {"architecture": "x86_64"},
                    "supported_model_formats": ["gguf"], "domains": ["llm"],
                    "name": "llama.cpp-fixture", "version": "1.2.3",
                    "engine_protocol_server": {"runtime_kind": "llama-server", "executable_relative_path": "llama-server.exe"},
                    "target_libraries": [{"name": "engine.node"}], "vendor_lib_package_names": ["fixture-cuda-vendor"]}
        (engine / "backend-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return engine, vendor, manifest

    def owned(self):
        profile, marker = lm.prepare_profile(self.profile, self.app)
        receipt = {"schema": 1, "token": marker["token"], "profile": str(profile),
                   "process": self.identity(), "host": "http://127.0.0.1:45678", "state": "ready",
                   "lms": str(self.lms), "logdir": str(self.logs)}
        lm._write(profile / lm.RECEIPT, receipt)
        return receipt

    def test_endpoint_dedicated_loopback_only(self):
        self.assertEqual(lm.endpoint("http://localhost:45678/v1"), ("http://127.0.0.1:45678", 45678))
        for url in ("http://example.com:45678", "http://0.0.0.0:45678", "http://u:p@localhost:45", "http://localhost:45?token=x", "https://localhost:45", "http://localhost:45/other"):
            with self.subTest(url=url), self.assertRaises(lm.RuntimeError_):
                lm.endpoint(url)

    def test_child_profile_environment_does_not_change_parent(self):
        before = dict(os.environ)
        env = lm.profile_environment(self.profile)
        self.assertEqual(env["USERPROFILE"], str(self.profile))
        self.assertEqual(env["APPDATA"], str(self.profile / "AppData" / "Roaming"))
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(env.get("HOME"), before.get("HOME"))

    def test_fresh_profile_only_points_to_explicit_model_directory(self):
        models = self.root / "weights"
        models.mkdir()
        sentinel = models / "model.gguf"
        sentinel.write_bytes(b"unchanged model fixture")
        profile, marker = lm.prepare_profile(self.profile, self.app, models)
        self.assertEqual(json.loads((profile / ".lmstudio" / "settings.json").read_text()), {"downloadsFolder": str(models)})
        self.assertEqual((profile / ".lmstudio-home-pointer").read_text(), str(profile / ".lmstudio"))
        self.assertEqual(sentinel.read_bytes(), b"unchanged model fixture")
        self.assertEqual(list(models.iterdir()), [sentinel])
        self.assertEqual(lm.prepare_profile(profile, self.app, models)[1], marker)

    def test_no_default_copy_of_models_settings_or_credentials(self):
        profile, _ = lm.prepare_profile(self.profile, self.app)
        self.assertFalse((profile / ".lmstudio" / "settings.json").exists())
        self.assertFalse((profile / ".lmstudio" / "models").exists())
        self.assertFalse((profile / ".lmstudio" / "credentials").exists())

    def test_existing_unowned_even_empty_profile_rejected(self):
        self.profile.mkdir()
        with self.assertRaisesRegex(lm.RuntimeError_, "not owned"):
            lm.prepare_profile(self.profile, self.app)

    def test_changed_owned_profile_settings_rejected(self):
        lm.prepare_profile(self.profile, self.app)
        models = self.root / "weights"
        models.mkdir()
        with self.assertRaisesRegex(lm.RuntimeError_, "settings differ"):
            lm.prepare_profile(self.profile, self.app, models)

    def test_current_user_profile_cannot_be_used(self):
        with patch.dict(os.environ, {"USERPROFILE": str(self.profile)}):
            with self.assertRaisesRegex(lm.RuntimeError_, "ordinary profile"):
                lm.prepare_profile(self.profile, self.app)
        self.assertFalse(self.profile.exists())

    def test_selected_backend_and_only_declared_vendor_are_copied_without_source_changes(self):
        engine, vendor, _ = self.backend()
        unrelated = vendor.parent / "unrelated-vendor"
        unrelated.mkdir()
        (unrelated / "private.dll").write_bytes(b"not needed")
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in engine.parent.rglob("*") if p.is_file()}
        profile, marker = lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        copied = profile / ".lmstudio/extensions/backends"
        self.assertEqual((copied / engine.name / "engine.node").read_bytes(), b"synthetic engine library")
        self.assertEqual((copied / "vendor/fixture-cuda-vendor/dependency.dll").read_bytes(), b"synthetic vendor library")
        self.assertFalse((copied / "vendor/unrelated-vendor").exists())
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in engine.parent.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(marker["backend_source"], str(engine))
        self.assertEqual(lm.prepare_profile(profile, self.app, backend_dir=engine)[1], marker)

    def test_missing_backend_vendor_is_rejected_before_profile_creation(self):
        engine, vendor, manifest = self.backend()
        manifest["vendor_lib_package_names"] = ["missing"]
        (engine / "backend-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(lm.RuntimeError_, "dependency is missing"):
            lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        self.assertFalse(self.profile.exists())

    def test_incompatible_backend_manifest_is_rejected_before_profile_creation(self):
        engine, _, manifest = self.backend()
        for key, value in (("platform", "linux"), ("engine", "mlx"), ("manifest_version", "999"), ("cpu", None)):
            changed = dict(manifest, **{key: value})
            (engine / "backend-manifest.json").write_text(json.dumps(changed), encoding="utf-8")
            with self.subTest(key=key), self.assertRaisesRegex(lm.RuntimeError_, "incompatible"):
                lm.prepare_profile(self.profile, self.app, backend_dir=engine)
            self.assertFalse(self.profile.exists())

    def test_backend_vendor_path_traversal_is_rejected(self):
        engine, _, manifest = self.backend()
        manifest["vendor_lib_package_names"] = ["../../outside"]
        (engine / "backend-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(lm.RuntimeError_, "dependency list"):
            lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        self.assertFalse(self.profile.exists())

    def test_backend_missing_target_binary_is_rejected(self):
        engine, _, manifest = self.backend()
        manifest["engine_protocol_server"]["executable_relative_path"] = "missing.exe"
        (engine / "backend-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(lm.RuntimeError_, "missing/outside"):
            lm.prepare_profile(self.profile, self.app, backend_dir=engine)

    def test_backend_malformed_protocol_is_rejected(self):
        engine, _, manifest = self.backend()
        manifest["engine_protocol_server"] = None
        (engine / "backend-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(lm.RuntimeError_, "protocol declaration"):
            lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        self.assertFalse(self.profile.exists())

    def test_invalid_existing_profile_is_rejected_before_backend_read_or_copy(self):
        self.profile.mkdir()
        with patch.object(lm, "_backend_source") as source, patch.object(lm, "_copy_backend") as copy:
            with self.assertRaisesRegex(lm.RuntimeError_, "not owned"):
                lm.prepare_profile(self.profile, self.app, backend_dir=self.root / "does-not-exist")
            source.assert_not_called()
            copy.assert_not_called()

    def test_changed_owned_backend_snapshot_is_not_silently_replaced(self):
        engine, _, _ = self.backend()
        profile, _ = lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        target = profile / ".lmstudio/extensions/backends" / engine.name / "engine.node"
        target.write_bytes(b"changed target")
        with self.assertRaisesRegex(lm.RuntimeError_, "snapshot was changed"):
            lm.prepare_profile(profile, self.app, backend_dir=engine)
        self.assertEqual(target.read_bytes(), b"changed target")

    def test_adding_backend_to_existing_profile_requires_new_profile(self):
        lm.prepare_profile(self.profile, self.app)
        engine, _, _ = self.backend()
        with self.assertRaisesRegex(lm.RuntimeError_, "settings differ"):
            lm.prepare_profile(self.profile, self.app, backend_dir=engine)

    def test_profile_nested_in_engine_or_vendor_cannot_mutate_source(self):
        engine, vendor, _ = self.backend()
        before = {str(p): p.read_bytes() for p in engine.parent.rglob("*") if p.is_file()}
        for tree in (engine, vendor):
            destination = tree / "new-profile"
            with self.subTest(tree=tree), patch.object(lm, "_copy_backend") as copy:
                with self.assertRaisesRegex(lm.RuntimeError_, "inside a backend or vendor source"):
                    lm.prepare_profile(destination, self.app, backend_dir=engine)
                copy.assert_not_called()
                self.assertFalse(destination.exists())
        self.assertEqual(before, {str(p): p.read_bytes() for p in engine.parent.rglob("*") if p.is_file()})

    def test_added_backend_file_is_not_accepted_as_owned_snapshot(self):
        engine, _, _ = self.backend()
        profile, _ = lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        (profile / ".lmstudio/extensions/backends" / engine.name / "extra.node").write_bytes(b"extra")
        with self.assertRaisesRegex(lm.RuntimeError_, "snapshot was changed"):
            lm.prepare_profile(profile, self.app, backend_dir=engine)

    def test_backend_file_added_during_copy_is_not_in_inventory(self):
        engine, _, _ = self.backend()
        real_copy = lm.shutil.copytree
        def copy(source, target, **kwargs):
            result = real_copy(source, target, **kwargs)
            if Path(source) == engine:
                (Path(target) / "extra.node").write_bytes(b"extra")
            return result
        with patch.object(lm.shutil, "copytree", side_effect=copy):
            with self.assertRaisesRegex(lm.RuntimeError_, "snapshot was changed"):
                lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        self.assertFalse((self.profile / lm.MARKER).exists())

    def test_linked_backend_ancestor_is_rejected_before_reading_snapshot_files(self):
        engine, _, _ = self.backend()
        profile, marker = lm.prepare_profile(self.profile, self.app, backend_dir=engine)
        ancestor = profile / ".lmstudio/extensions"
        with patch.object(lm, "_linked", side_effect=lambda p: p == ancestor):
            with self.assertRaisesRegex(lm.RuntimeError_, "linked directory"):
                lm._verify_backend_snapshot(profile, marker)

    def test_unowned_running_runtime_not_adopted(self):
        with patch.object(lm, "_profile_processes", return_value=[444]), patch.object(lm.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(lm.RuntimeError_, "already live"):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            spawn.assert_not_called()

    def test_occupied_port_never_reconfigures_existing_service(self):
        with patch.object(lm, "_profile_processes", return_value=[]), patch.object(lm, "_listener_pid", return_value={999}), patch.object(lm.subprocess, "Popen") as spawn, patch.object(lm.subprocess, "run") as cli:
            with self.assertRaisesRegex(lm.RuntimeError_, "already occupied"):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            spawn.assert_not_called()
            cli.assert_not_called()

    def test_existing_owned_runtime_belongs_to_other_worker_and_is_not_adopted(self):
        old = self.owned()
        with patch.object(lm, "_profile_processes", return_value=[old["process"]["pid"]]), patch.object(lm, "_same_process", return_value=True), patch.object(lm.subprocess, "run") as cli, patch.object(lm, "_listener_pid", return_value={old["process"]["pid"]}), patch.object(lm, "_probe", return_value=0), patch.object(lm.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(lm.RuntimeError_, "refusing adoption"):
                lm.ensure_server(old["host"], self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            spawn.assert_not_called()
            cli.assert_not_called()

    def test_owned_but_unresponsive_does_not_start_duplicate(self):
        self.owned()
        with patch.object(lm, "_profile_processes", return_value=[]), patch.object(lm, "_same_process", return_value=True), patch.object(lm.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(lm.RuntimeError_, "no duplicate"):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            spawn.assert_not_called()

    def test_different_listener_pid_is_not_accepted_as_health_and_new_app_is_cleaned(self):
        fake = Mock(pid=987654, returncode=None)
        fake.poll.return_value = None
        with patch.object(lm, "_profile_processes", return_value=[]), patch.object(lm, "process_identity", return_value=self.identity()), patch.object(lm, "_listener_pid", side_effect=[set(), {999}]), patch.object(lm, "_probe") as probe, patch.object(lm.subprocess, "Popen", return_value=fake), patch.object(lm.socket, "socket"), patch.object(lm, "_stop_server") as stop:
            with self.assertRaisesRegex(lm.RuntimeError_, "listener ownership"):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            probe.assert_not_called()
            self.assertEqual(stop.call_args.args[0]["process"]["pid"], fake.pid)

    def test_start_is_detached_hidden_and_logged_before_readiness(self):
        fake = Mock(pid=987654, returncode=None)
        fake.poll.return_value = None
        def probe(*_):
            saved = json.loads((self.profile / lm.RECEIPT).read_text())
            self.assertEqual(saved["state"], "starting")
            return 0
        with patch.object(lm, "_profile_processes", return_value=[]), patch.object(lm, "process_identity", return_value=self.identity()), patch.object(lm, "_listener_pid", side_effect=[set(), {fake.pid}]), patch.object(lm.subprocess, "run") as cli, patch.object(lm, "_probe", side_effect=probe), patch.object(lm.subprocess, "Popen", return_value=fake) as spawn, patch.object(lm.socket, "socket"):
            receipt = lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            kwargs = spawn.call_args.kwargs
            self.assertTrue(kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
            self.assertFalse(kwargs["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
            self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["USERPROFILE"], str(self.profile))
            self.assertEqual(receipt["process"]["pid"], fake.pid)
            cli.assert_not_called()
            config = json.loads((self.profile / ".lmstudio" / ".internal" / "http-server-config.json").read_text())
            self.assertTrue(config["autoStartOnLaunch"])
            self.assertEqual(config["port"], 45678)
            self.assertEqual(config["networkInterface"], "127.0.0.1")

    def test_first_receipt_write_failure_cleans_only_new_popen_handle(self):
        fake = Mock(pid=987654, returncode=None)
        real_write = lm._write
        def write(path, value):
            if path.name == lm.RECEIPT:
                raise OSError("synthetic full disk")
            return real_write(path, value)
        with patch.object(lm, "_profile_processes", return_value=[]), patch.object(lm, "process_identity", return_value=self.identity()), patch.object(lm, "_listener_pid", return_value=set()), patch.object(lm.subprocess, "Popen", return_value=fake), patch.object(lm.socket, "socket"), patch.object(lm, "_write", side_effect=write), patch.object(lm, "_stop_new_process") as cleanup, patch.object(lm, "_stop_server") as receipt_cleanup:
            with self.assertRaisesRegex(OSError, "full disk"):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            cleanup.assert_called_once_with(fake)
            receipt_cleanup.assert_not_called()

    def test_stop_refuses_foreign_receipt_without_touching_process(self):
        receipt = self.owned()
        receipt["token"] = "wrong"
        with patch.object(lm, "_same_process") as same:
            with self.assertRaisesRegex(lm.RuntimeError_, "receipt mismatch"):
                lm.stop_server(receipt)
            same.assert_not_called()

    def test_reused_pid_is_not_terminated(self):
        receipt = self.owned()
        with patch.object(lm, "_same_process", return_value=False), patch.object(lm.psutil, "Process") as process:
            result = lm.stop_server(receipt)
            self.assertFalse(result["stopped"])
            process.assert_not_called()

    def test_process_creation_time_is_part_of_ownership(self):
        identity = self.identity()
        with patch.object(lm, "process_identity", return_value=dict(identity, create_time=identity["create_time"] + 1)):
            self.assertFalse(lm._same_process(identity))

    def test_profile_lock_prevents_simultaneous_lifecycle_commands(self):
        lm.prepare_profile(self.profile, self.app)
        with lm._profile_lock(self.profile):
            with self.assertRaisesRegex(lm.RuntimeError_, "another worker"):
                with lm._profile_lock(self.profile):
                    self.fail("second worker obtained lock")

    def test_rejecting_old_live_profile_does_not_trigger_cleanup(self):
        self.owned()
        with patch.object(lm, "_profile_processes", return_value=[987654]), patch.object(lm, "_stop_server") as cleanup:
            with self.assertRaises(lm.RuntimeError_):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            cleanup.assert_not_called()

    def test_existing_other_runtime_prevents_modal_or_adoption(self):
        with patch.object(lm, "_profile_processes", return_value=[]), patch.object(lm, "_other_core_processes", return_value=[123]), patch.object(lm.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(lm.RuntimeError_, "existing runtime is untouched"):
                lm.ensure_server("http://127.0.0.1:45678", self.lms, self.logs, profile_dir=self.profile, app_path=self.app)
            spawn.assert_not_called()

    def test_explicit_llmster_uses_unique_public_instance_timestamp(self):
        daemon = self.root / "llmster.exe"
        daemon.touch()
        profile, marker = lm.prepare_profile(self.profile, daemon)
        argument = "--start-timestamp=" + marker["token"]
        identity = dict(self.identity(), exe=str(daemon), cmdline=[str(daemon), argument])
        self.assertEqual(lm.find_app(self.lms, daemon), daemon)
        self.assertEqual(lm._instance_argument(profile, daemon), argument)
        lm._verify_identity(identity, daemon, profile)
        identity["cmdline"] = [str(daemon), "--start-timestamp=someone-else"]
        with self.assertRaises(lm.RuntimeError_):
            lm._verify_identity(identity, daemon, profile)

    def test_empty_owned_native_model_list_does_not_call_global_cli(self):
        receipt = self.owned()
        with patch.object(lm, "_assert_live_receipt", return_value=receipt["host"]), patch.object(lm, "_http_json", return_value={"models": []}) as http, patch.object(lm.subprocess, "run") as cli:
            self.assertEqual(lm.unload_owned_models(receipt)["loaded_remaining"], [])
            self.assertEqual(http.call_count, 2)
            cli.assert_not_called()

    def test_native_unload_only_exact_owned_instances_and_rechecks_empty(self):
        receipt = self.owned()
        initial = {"models": [{"key": "local-model", "loaded_instances": [{"id": "owned-instance-1"}, {"id": "owned-instance-2"}]}]}
        with patch.object(lm, "_assert_live_receipt", return_value=receipt["host"]) as owned, patch.object(lm, "_http_json", side_effect=[initial, {"instance_id": "owned-instance-1"}, {"instance_id": "owned-instance-2"}, {"models": []}]) as http:
            result = lm.unload_owned_models(receipt)
            self.assertEqual(result["unloaded"], ["owned-instance-1", "owned-instance-2"])
            self.assertEqual(http.call_args_list[1].args, (receipt["host"], "/api/v1/models/unload", {"instance_id": "owned-instance-1"}))
            self.assertEqual(owned.call_count, 4)

    def test_native_unload_fails_closed_when_model_list_schema_unknown(self):
        receipt = self.owned()
        with patch.object(lm, "_assert_live_receipt", return_value=receipt["host"]), patch.object(lm, "_http_json", return_value={"data": []}) as http, patch.object(lm.subprocess, "run") as cli:
            with self.assertRaisesRegex(lm.RuntimeError_, "refusing global CLI"):
                lm.unload_owned_models(receipt)
            self.assertEqual(http.call_count, 1)
            cli.assert_not_called()

    def test_replaced_listener_blocks_owned_model_operations(self):
        receipt = self.owned()
        with patch.object(lm, "_same_process", return_value=True), patch.object(lm, "_listener_pid", return_value={123}), patch.object(lm, "_http_json") as http:
            with self.assertRaisesRegex(lm.RuntimeError_, "no longer belongs"):
                lm.unload_owned_models(receipt)
            http.assert_not_called()

    def test_remaining_loaded_instances_block_gpu_handoff(self):
        receipt = self.owned()
        initial = {"models": [{"loaded_instances": [{"id": "instance"}]}]}
        with patch.object(lm, "_assert_live_receipt", return_value=receipt["host"]), patch.object(lm, "_http_json", side_effect=[initial, {"instance_id": "instance"}, initial]):
            with self.assertRaisesRegex(lm.RuntimeError_, "still has loaded"):
                lm.unload_owned_models(receipt)


if __name__ == "__main__":
    unittest.main()
