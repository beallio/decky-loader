"""User-manager, bundle identity, and legacy restoration safety behavior."""
import hashlib
import importlib.util
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = REPO_ROOT / "backend-rust" / "install.py"
sys.path.insert(0, str(INSTALLER_PATH.parent))
_INSTALLER_SPEC = importlib.util.spec_from_file_location("rust_runtime_user_installer", INSTALLER_PATH)
installer = importlib.util.module_from_spec(_INSTALLER_SPEC)
sys.modules[_INSTALLER_SPEC.name] = installer
_INSTALLER_SPEC.loader.exec_module(installer)
from process_observer import Process


class UserServiceInstallerTests(unittest.TestCase):
    def test_stable_bundle_without_private_candidate_flags_is_accepted(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            files = {
                "decky-loader-runtime": b"rust executable",
                "DeckyPluginWorker": b"worker executable",
                "install.py": b"installer payload",
                "process_observer.py": b"observer payload",
                "assets/index.js": b"frontend asset",
                "locales/en-US.json": b"{}",
            }
            for relative, contents in files.items():
                path = bundle / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
                if relative in ("decky-loader-runtime", "DeckyPluginWorker"):
                    path.chmod(0o755)
            manifest = {
                "kind": "decky-loader-runtime",
                "release_version": "v1.2.3-rust.4",
                "rust_executable": "decky-loader-runtime",
                "source_baseline": {
                    "tag": "v3.2.10",
                    "commit": "a" * 40,
                    "backend_tree": "b" * 40,
                    "frontend_tree": "c" * 40,
                },
                "cargo_lock_sha256": "d" * 64,
                "runtime": {
                    "frozen": True,
                    "python": "3.11.9",
                    "decky_version": "v3.2.10",
                },
                "worker_packaging": "onefile",
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            }
            (bundle / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
            validated, parsed = installer._validate_bundle(str(bundle), user)
        self.assertEqual(validated, bundle)
        self.assertEqual(parsed["release_version"], "v1.2.3-rust.4")

    def test_manager_unit_directory_comes_from_structured_manager_path(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_directory = root / "manager-config" / "systemd" / "user"
            manager_directory.mkdir(parents=True)
            caller_directory = root / "caller-config" / "systemd" / "user"
            caller_directory.mkdir(parents=True)
            with (
                patch.object(
                    installer, "_manager_unit_paths",
                    return_value=(manager_directory, Path("/usr/lib/systemd/user")),
                ),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(caller_directory.parent.parent)}),
            ):
                resolved = installer._user_unit_directory(user, create=False)
            self.assertEqual(resolved, manager_directory)
            self.assertTrue(caller_directory.is_dir())

    def test_unit_path_query_decodes_structured_paths_with_spaces(self):
        user = pwd.getpwuid(os.geteuid())
        expected = Path("/tmp/manager config/systemd/user")
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"type": "as", "data": [str(expected)]}),
            stderr="",
        )
        with (
            patch.object(installer, "_busctl_path", return_value=Path("/usr/bin/busctl")),
            patch.object(installer, "_run_manager_command", return_value=result) as run,
        ):
            paths = installer._manager_unit_paths(user)
        self.assertEqual(paths, (expected,))
        self.assertIn("--json=short", run.call_args.args[1])

    def test_user_manager_commands_do_not_inherit_caller_config_home(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as runtime:
            with (
                patch.object(installer, "_user_runtime_directory", return_value=Path(runtime)),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": "/caller/config"}),
            ):
                environment = installer._manager_environment(user)
        self.assertNotIn("XDG_CONFIG_HOME", environment)
        self.assertEqual(environment["XDG_RUNTIME_DIR"], runtime)


    def test_manager_unit_directory_check_does_not_create_missing_config_path(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unit_directory = home / ".config" / "systemd" / "user"
            with patch.object(installer, "_manager_unit_paths", return_value=(unit_directory,)):
                resolved = installer._user_unit_directory(user, create=False)
            self.assertEqual(resolved, unit_directory)
            self.assertFalse(unit_directory.exists())

    def test_explicit_unit_directory_must_belong_to_manager_search_path(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "systemd" / "user"
            selected.mkdir(parents=True)
            other = Path(temporary) / "other" / "systemd" / "user"
            with patch.object(installer, "_manager_unit_paths", return_value=(selected,)):
                with self.assertRaisesRegex(installer.InstallerError, "not in .*manager UnitPath"):
                    installer._user_unit_directory(user, value=str(other))
            self.assertFalse(other.exists())

    def test_paths_reuse_persisted_manager_unit_directory(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            homebrew = root / "homebrew"
            homebrew.mkdir()
            first_directory = root / "manager-config" / "systemd" / "user"
            first_directory.mkdir(parents=True)
            later_directory = root / "later-config" / "systemd" / "user"
            later_directory.mkdir(parents=True)
            with (
                patch.object(installer, "_require_user"),
                patch.object(
                    installer, "_manager_unit_paths",
                    side_effect=((first_directory,), (later_directory, first_directory)),
                ),
            ):
                first_paths = installer._paths(homebrew, "plugin_loader", user)
                second_paths = installer._paths(homebrew, "plugin_loader", user)
            self.assertEqual(first_paths["unit_dir"], first_directory)
            self.assertEqual(second_paths["unit_dir"], first_directory)
            directory_record = (
                homebrew / "services" / ".decky-user-installer" / "plugin_loader.service"
                / "unit-directory.json"
            )
            record = json.loads(directory_record.read_text(encoding="utf-8"))
            self.assertEqual(record["user_uid"], user.pw_uid)
            self.assertEqual(record["unit_directory"], str(first_directory))


    def test_bundle_verification_accepts_missing_homebrew_without_creating_it(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            homebrew = root / "fresh-homebrew"
            bundle = root / "bundle"
            manifest = {"release_version": "v1.2.3-rust.4"}
            args = installer.argparse.Namespace(
                user=user.pw_name,
                homebrew=str(homebrew),
                service_name="plugin_loader",
                bundle=str(bundle),
                version="v1.2.3-rust.4",
            )
            with (
                patch.object(installer, "_require_user"),
                patch.object(installer, "_validate_bundle", return_value=(bundle, manifest)),
                patch.object(installer, "_verify_runtime"),
            ):
                verified_user, resolved_homebrew, *_ = installer._verify_bundle(args)
            self.assertEqual(verified_user, user)
            self.assertEqual(resolved_homebrew, homebrew)
            self.assertFalse(homebrew.exists())

    def test_bundle_check_is_nonmutating_for_missing_homebrew_and_unit_path(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            homebrew = root / "fresh-homebrew"
            unit_directory = root / ".config" / "systemd" / "user"
            bundle = root / "bundle"
            manifest = {"release_version": "v1.2.3-rust.4"}
            with (
                patch.object(
                    installer, "_verify_bundle",
                    return_value=(user, homebrew, "plugin_loader.service", bundle, manifest),
                ),
                patch.object(installer, "_user_unit_directory", return_value=unit_directory),
                patch.object(installer, "_assert_no_system_instance"),
                patch.object(installer, "_assert_user_unit_source"),
            ):
                result = installer._check_bundle(installer.argparse.Namespace(migrate_legacy=False))
            self.assertEqual(result["unit_directory"], str(unit_directory))
            self.assertFalse(homebrew.exists())
            self.assertFalse(unit_directory.exists())

    def test_explicit_migration_check_runs_readiness_not_duplicate_rejection(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            homebrew = root / "existing-homebrew"
            unit_directory = root / ".config" / "systemd" / "user"
            bundle = root / "bundle"
            manifest = {"release_version": "v1.2.3-rust.4"}
            readiness = {"ready": True}
            with (
                patch.object(
                    installer, "_verify_bundle",
                    return_value=(user, homebrew, "plugin_loader.service", bundle, manifest),
                ),
                patch.object(installer, "_user_unit_directory", return_value=unit_directory),
                patch.object(installer, "_check_legacy_migration", return_value=readiness) as preflight,
                patch.object(installer, "_assert_no_system_instance") as duplicate_guard,
                patch.object(installer, "_assert_user_unit_source"),
            ):
                result = installer._check_bundle(installer.argparse.Namespace(migrate_legacy=True))
            duplicate_guard.assert_not_called()
            preflight.assert_called_once_with(
                homebrew,
                "plugin_loader.service",
                user,
                user_unit_directory=str(unit_directory),
            )
            self.assertEqual(result["legacy_migration"], readiness)
            self.assertEqual(result["unit_directory"], str(unit_directory))


    def test_system_service_instance_blocks_normal_user_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = {
                "homebrew": Path(temporary),
                "unit": Path("plugin_loader.service"),
                "scope": "user",
                "user": pwd.getpwuid(os.geteuid()),
            }
            with patch.object(installer, "_unit_properties", return_value={
                "LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0",
            }):
                with self.assertRaisesRegex(installer.InstallerError, "migrate-legacy-system"):
                    installer._assert_no_system_instance(paths)

    def test_masked_legacy_service_allows_user_scope_only_after_completed_migration(self):
        paths = {
            "homebrew": Path("/nonexistent-homebrew"),
            "unit": Path("plugin_loader.service"),
            "scope": "user",
            "user": pwd.getpwuid(os.geteuid()),
        }
        masked = {"LoadState": "masked", "ActiveState": "inactive", "MainPID": "0"}
        for phase, allowed in (("services-user-owned", True), ("forgotten", True), ("parked", False)):
            with self.subTest(phase=phase), \
                    patch.object(installer, "_unit_properties", return_value=masked), \
                    patch.object(installer, "_read_legacy_status", return_value={"phase": phase}):
                if allowed:
                    installer._assert_no_system_instance(paths)
                else:
                    with self.assertRaises(installer.InstallerError):
                        installer._assert_no_system_instance(paths)

    def legacy_args(self):
        user = pwd.getpwuid(os.geteuid())
        return SimpleNamespace(homebrew="/", service_name="plugin_loader", user=user.pw_name,
                               user_unit_directory=None), user

    def test_forget_requires_a_completed_migration(self):
        args, user = self.legacy_args()
        record = {"user_uid": user.pw_uid, "user": user.pw_name, "phase": "parked", "service": "plugin_loader.service"}
        with patch.object(installer, "_require_root"), \
                patch.object(installer, "_read_legacy_record", return_value=(Path("/nonexistent"), record)), \
                patch.object(installer, "_write_legacy_record") as write:
            with self.assertRaisesRegex(installer.InstallerError, "completed migration"):
                installer._run_legacy_forget(args)
        write.assert_not_called()

    def test_restore_refuses_after_forget(self):
        args, user = self.legacy_args()
        for phase in ("forgetting", "forgotten"):
            record = {"user_uid": user.pw_uid, "user": user.pw_name, "phase": phase, "service": "plugin_loader.service"}
            with self.subTest(phase=phase), \
                    patch.object(installer, "_require_root"), \
                    patch.object(installer, "_read_legacy_record", return_value=(Path("/nonexistent"), record)), \
                    patch.object(installer, "_write_legacy_record") as write:
                with self.assertRaisesRegex(installer.InstallerError, "cannot be restored"):
                    installer._run_legacy_restore(args)
                write.assert_not_called()

    def test_recorded_services_directory_accepts_only_its_journaled_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            homebrew = Path(temporary)
            services = homebrew / "services"
            services.mkdir(mode=0o755)
            info = services.stat()
            # The journal is JSON, so the identity is reloaded as a list.
            record = json.loads(json.dumps({
                "user_uid": os.geteuid(),
                "services_directory": {"identity": [info.st_dev, info.st_ino]},
            }))
            homebrew_fd, services_fd, opened = installer._open_recorded_services(homebrew, record)
            os.close(services_fd)
            os.close(homebrew_fd)
            self.assertEqual((opened.st_dev, opened.st_ino), (info.st_dev, info.st_ino))
            services.rename(homebrew / "replaced")
            services.mkdir(mode=0o755)
            with self.assertRaisesRegex(installer.InstallerError, "changed after its migration snapshot"):
                installer._open_recorded_services(homebrew, record)

    @unittest.skipIf(os.geteuid() == 0, "requires an ordinary user-owned directory")
    def test_legacy_snapshot_rejects_user_owned_system_unit_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "plugin_loader.service"
            original.write_bytes(b"original unit")
            original.chmod(0o640)
            saved_file = root / "snapshot"
            with self.assertRaisesRegex(installer.InstallerError, "root-protected"):
                installer._snapshot_legacy_file(original, saved_file)
            self.assertEqual(original.read_bytes(), b"original unit")
            self.assertEqual(original.stat().st_mode & 0o777, 0o640)
            self.assertFalse(saved_file.exists())

    def test_restore_retry_skips_an_already_removed_persistent_mask(self):
        # After a partial restore the original unit is back at the mask path; a retry
        # must not compare it with the old mask identity or rewrite the journal.
        record = {
            "service": "plugin_loader.service",
            "persistent_mask": {
                "path": "/etc/systemd/system/plugin_loader.service",
                "identity": [1, 2],
                "state": "removed",
            },
        }
        with patch.object(installer, "_write_legacy_record") as write:
            installer._remove_legacy_persistent_mask(record, Path("/nonexistent/record.json"))
        write.assert_not_called()
        self.assertEqual(record["persistent_mask"]["state"], "removed")

    @unittest.skipIf(os.geteuid() == 0, "stop-record recovery uses a non-root user runtime directory")
    def test_shutdown_record_survives_fresh_call_and_does_not_claim_reused_pid(self):
        user = pwd.getpwuid(os.geteuid())
        unit = "decky-stop-fresh-recovery.service"
        group = f"/user.slice/user-{user.pw_uid}.slice/user@{user.pw_uid}.service/app.slice/{unit}"
        process = [Process(42, 100, "Z", 1, 42, 42, "plugin helper", (group,))]
        properties_calls = [0]
        clock = [0.0]

        def properties(_paths):
            result = {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "MainPID": "0",
                "ControlGroup": group if properties_calls[0] == 0 else "",
            }
            properties_calls[0] += 1
            return result

        def snapshot(control_group, *, retained=None):
            current = process[0]
            if control_group != group:
                return {}
            if retained and current.start_ticks != retained.get(current.pid):
                return {}
            if group in current.cgroups:
                return {current.pid: current}
            return {}

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as runtime:
            with (
                patch.object(installer, "_user_runtime_directory", return_value=Path(runtime)),
                patch.object(installer, "_unit_properties", side_effect=properties),
                patch.object(installer, "_systemctl", side_effect=AssertionError("unit is already inactive")),
                patch.object(installer, "cgroup_processes", side_effect=snapshot),
                patch.object(installer.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(installer.time, "sleep", side_effect=sleep),
            ):
                with self.assertRaisesRegex(installer.InstallerError, "PID 42"):
                    installer.stop_service(unit)
                state_file = Path(runtime) / "decky-loader" / "stop-records" / f"{unit}.json"
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["identities"], [[42, 100]])

                # A new stop call reloads the persisted start epoch. A reused PID
                # outside the stopped cgroup is not adopted as the old helper.
                process[0] = Process(42, 200, "S", 1, 50, 50, "unrelated", ("/other",))
                properties_calls[0] = 0
                installer.stop_service(unit)
                self.assertFalse(state_file.exists())


if __name__ == "__main__":
    unittest.main()
