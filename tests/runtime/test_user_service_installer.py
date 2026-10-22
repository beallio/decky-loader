"""User-manager, bundle identity, and legacy restoration safety behavior."""
import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import shlex
import subprocess
import sys
import tempfile
import threading
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

    def test_generated_unit_roundtrip_deduplicates_listener_environment(self) -> None:
        paths = {
            "unit": Path("loader.service"),
            "homebrew": Path("/home/deck/homebrew"),
            "services": Path("/home/deck/homebrew/services"),
            "binary": Path("/runtime/decky-loader-runtime"),
        }
        generated = (
            "[Service]\nEnvironment=\n"
            'Environment="KEEP=plain" "QUOTED=value with spaces" SERVER_HOST=old SERVER_PORT=1\n'
            "Environment=SERVER_HOST=older\nEnvironment=SERVER_PORT=2\n"
            "EnvironmentFile=-/etc/decky-loader.env\n"
            "[Install]\n"
        )

        def unit_source(*_args, **_kwargs):
            return SimpleNamespace(stdout=generated)

        with patch.object(installer, "_systemctl", side_effect=unit_source):
            for listen in ("127.0.0.1:1337", "127.0.0.1:1337", "127.0.0.2:1444"):
                generated = installer._generated_unit(
                    paths, Path("/runtime"), "deck", "loader.service", listen,
                    "http://127.0.0.1:8080", False, "default", "https://api.github.com",
                    "beallio/decky-loader", "https://nightly.link", "1.0",
                ).decode()

        service = generated.split("[Service]\n", 1)[1].split("\n[Install]", 1)[0]
        environment_lines = [line for line in service.splitlines() if line.startswith("Environment=")]
        assignments = [
            entry for line in environment_lines
            for entry in shlex.split(line.split("=", 1)[1])
        ]
        self.assertEqual(sum(entry.startswith("SERVER_HOST=") for entry in assignments), 1)
        self.assertEqual(sum(entry.startswith("SERVER_PORT=") for entry in assignments), 1)
        self.assertIn("Environment=", service)
        self.assertEqual(environment_lines.count("Environment="), 1)
        self.assertIn("KEEP=plain", assignments)
        self.assertIn("QUOTED=value with spaces", assignments)
        self.assertIn("EnvironmentFile=-/etc/decky-loader.env", service)
        self.assertIn("SERVER_HOST=127.0.0.2", service)
        self.assertIn("SERVER_PORT=1444", service)

    def write_bundle(self, bundle: Path, sdk_tag: str, worker_version: str, worker_python: str = "3.11.9") -> None:
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
                "tag": sdk_tag,
                "commit": "a" * 40,
                "backend_tree": "b" * 40,
                "frontend_tree": "c" * 40,
            },
            "cargo_lock_sha256": "d" * 64,
            "runtime": {
                "frozen": True,
                "python": worker_python,
                "decky_version": worker_version,
            },
            "worker_packaging": "onefile",
            "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        }
        (bundle / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_stable_bundle_without_private_candidate_flags_is_accepted(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            self.write_bundle(bundle, "v3.2.10", "v3.2.10")
            validated, parsed = installer._validate_bundle(str(bundle), user)
        self.assertEqual(validated, bundle)
        self.assertEqual(parsed["release_version"], "v1.2.3-rust.4")

    def test_build_facts_of_a_newer_release_do_not_block_installation(self):
        # The installed installer checks new releases; build facts are CI's job, not its.
        user = pwd.getpwuid(os.geteuid())
        for sdk_tag, worker_version, worker_python in (
            ("v3.3.0", "v3.3.0", "3.14.8"),
            ("v4.0.0-rc1", "v4.0.0-rc1", "3.15.0"),
            ("main", "v3.2.10", "3.10.13"),
        ):
            with self.subTest(sdk_tag=sdk_tag, worker_python=worker_python), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary)
                self.write_bundle(bundle, sdk_tag, worker_version, worker_python)
                installer._validate_bundle(str(bundle), user)

    def test_changed_or_unlisted_bundle_files_are_rejected(self):
        user = pwd.getpwuid(os.geteuid())
        for change in ("modify", "add"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary)
                self.write_bundle(bundle, "v3.3.0", "v3.3.0")
                if change == "modify":
                    (bundle / "assets/index.js").write_bytes(b"tampered")
                else:
                    (bundle / "assets/extra.js").write_bytes(b"unlisted")
                with self.assertRaisesRegex(installer.InstallerError, "checksum"):
                    installer._validate_bundle(str(bundle), user)

    def test_update_hands_over_to_the_bundle_installer_only_when_it_matches_the_manifest(self):
        argv = ["install.py", "queue-install-bundle", "--bundle", "B", "--version", "v1.2.3-rust.4"]
        for tampered in (False, True):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as temporary:
                bundle = Path(temporary)
                self.write_bundle(bundle, "v3.3.0", "v3.3.0")
                if tampered:
                    (bundle / "install.py").write_bytes(b"changed installer")
                with patch.object(installer.os, "execv") as execv, patch.object(installer.sys, "argv", argv):
                    if tampered:
                        with self.assertRaisesRegex(installer.InstallerError, "checksum"):
                            installer._hand_over_to_bundle_installer(str(bundle))
                        execv.assert_not_called()
                    else:
                        installer._hand_over_to_bundle_installer(str(bundle))
                        execv.assert_called_once_with(
                            installer.sys.executable,
                            [installer.sys.executable, "-B", str(bundle / "install.py"), *argv[1:]],
                        )

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

    def test_exclusive_json_is_not_visible_until_fully_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "accepted.json"
            write_started = threading.Event()
            allow_write = threading.Event()
            results = []
            errors = []
            original_fdopen = os.fdopen

            class PausedStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *arguments):
                    return self.stream.__exit__(*arguments)

                def write(self, data):
                    write_started.set()
                    if not allow_write.wait(5):
                        raise TimeoutError("writer was not released")
                    return self.stream.write(data)

                def flush(self):
                    return self.stream.flush()

                def fileno(self):
                    return self.stream.fileno()

            def paused_fdopen(descriptor, mode):
                return PausedStream(original_fdopen(descriptor, mode))

            def write_json():
                try:
                    results.append(installer._write_json_exclusive(path, {"payload": "complete" * 4096}))
                except BaseException as error:
                    errors.append(error)

            with patch.object(installer.os, "fdopen", side_effect=paused_fdopen):
                writer = threading.Thread(target=write_json)
                writer.start()
                try:
                    self.assertTrue(write_started.wait(5))
                    self.assertFalse(path.exists(), "readers must not observe the final path during the write")
                finally:
                    allow_write.set()
                    writer.join(timeout=5)

            self.assertFalse(writer.is_alive())
            if errors:
                raise errors[0]
            self.assertEqual(results, [True])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"payload": "complete" * 4096})
            self.assertFalse(installer._write_json_exclusive(path, {"payload": "replacement"}))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"payload": "complete" * 4096})

    def test_installer_requires_python_311(self):
        with patch.object(installer.sys, "version_info", (3, 10, 13)):
            with self.assertRaisesRegex(installer.InstallerError, "Python 3.11 or newer"):
                installer._require_system_python()
        with patch.object(installer.sys, "version_info", (3, 11, 0)):
            installer._require_system_python()

    def test_runtime_staging_fsyncs_copied_files_and_directories(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            self.write_bundle(bundle, "v3.2.10", "v3.2.10")
            manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
            paths = {name: root / name for name in ("runtimes", "homebrew", "services", "state")}
            for path in paths.values():
                path.mkdir()
            files_synced = []
            directories_synced = []
            original_sync_file = installer._fsync_file
            original_sync_directory = installer._fsync_directory

            def sync_file(path):
                files_synced.append(path)
                original_sync_file(path)

            def sync_directory(path):
                directories_synced.append(path)
                original_sync_directory(path)

            with (
                patch.object(installer, "_check_runtime_access"),
                patch.object(installer, "_fsync_file", side_effect=sync_file),
                patch.object(installer, "_fsync_directory", side_effect=sync_directory),
            ):
                staging, final = installer._make_runtime(bundle, manifest, paths, "transaction", user)

            expected_files = {path for path in staging.rglob("*") if path.is_file()}
            expected_directories = {staging, *(path for path in staging.rglob("*") if path.is_dir())}
            self.assertEqual(set(files_synced), expected_files)
            self.assertEqual(set(directories_synced), expected_directories)
            self.assertTrue(staging.is_dir())
            self.assertFalse(final.exists())

    def test_prune_keeps_three_undo_steps_and_the_runtimes_they_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name: root / name for name in ("homebrew", "history", "runtimes", "units")}
            for path in paths.values():
                path.mkdir()
            paths.update(unit=root / "units" / "plugin_loader.service", head=root / "head.json")

            def runtime(number):
                return f"{number + 100:032x}"

            def unit_text(number):
                return f"ExecStart=/x --worker {paths['runtimes']}/{runtime(number)}/DeckyPluginWorker\n"

            def write_record(number, operation, parent):
                record_id = f"{number:032x}"
                before = paths["history"] / record_id / "before"
                before.mkdir(parents=True)
                (before / "unit").write_text(unit_text(number - 1), encoding="utf-8")
                record = {
                    "schema": 1, "id": record_id, "service": "plugin_loader.service",
                    "homebrew": str(paths["homebrew"]), "operation": operation,
                    "parent": parent, "files": {}, "activation": {},
                }
                (paths["history"] / record_id / "record.json").write_text(json.dumps(record), encoding="utf-8")
                return record_id

            parent = None
            for number in range(1, 6):
                (paths["runtimes"] / runtime(number)).mkdir()
                parent = write_record(number, "install", parent)
            orphan_rollback = write_record(99, "rollback", None)
            paths["unit"].write_text(unit_text(5), encoding="utf-8")
            paths["head"].write_text(json.dumps({"id": parent}), encoding="utf-8")

            installer._prune_history(paths)

            self.assertEqual({entry.name for entry in paths["history"].iterdir()}, {f"{n:032x}" for n in (3, 4, 5)})
            self.assertNotIn(orphan_rollback, {entry.name for entry in paths["history"].iterdir()})
            self.assertIsNone(installer._current_record(paths, f"{3:032x}")["parent"])
            self.assertEqual({entry.name for entry in paths["runtimes"].iterdir()}, {runtime(n) for n in (2, 3, 4, 5)})

            # A current unit that names no runtime must not make every runtime look unused.
            paths["unit"].write_text("ExecStart=/x\n", encoding="utf-8")
            installer._prune_history(paths)
            self.assertEqual({entry.name for entry in paths["runtimes"].iterdir()}, {runtime(n) for n in (2, 3, 4, 5)})

    def test_root_plugin_adoption_restores_original_when_stage_rename_fails(self):
        user = pwd.getpwuid(os.geteuid())
        with tempfile.TemporaryDirectory() as temporary:
            plugins = Path(temporary) / "plugins"
            original = plugins / "Demo"
            original.mkdir(parents=True)
            (original / "plugin.json").write_text('{"flags":["root"]}', encoding="utf-8")
            (original / "main.py").write_text("original plugin\n", encoding="utf-8")
            plugins_fd = os.open(plugins, os.O_RDONLY | os.O_DIRECTORY)
            created_stage_inodes = set()
            original_mkdir = os.mkdir
            original_fstat = os.fstat
            original_rename = os.rename
            rename_calls = []

            def record_stage_mkdir(name, mode=0o777, *, dir_fd=None):
                result = original_mkdir(name, mode, dir_fd=dir_fd)
                if isinstance(name, str) and name.startswith(".decky-root-adopt-"):
                    created_stage_inodes.add(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_ino)
                return result

            def report_root_owned_stage(descriptor):
                info = original_fstat(descriptor)
                if info.st_ino in created_stage_inodes:
                    return os.stat_result((*info[:4], 0, *info[5:]))
                return info

            def fail_install_rename(source, destination, **kwargs):
                rename_calls.append((source, destination))
                if len(rename_calls) == 2:
                    raise OSError("synthetic staged rename failure")
                return original_rename(source, destination, **kwargs)

            # _adopt_root_plugin owns the descriptor from _open_plugins_root and closes it.
            with (
                patch.object(installer, "_open_plugins_root", return_value=plugins_fd),
                patch.object(installer, "_root_host_tree_hash", return_value={"tree_sha256": "a" * 64}),
                patch.object(installer.os, "mkdir", side_effect=record_stage_mkdir),
                patch.object(installer.os, "fstat", side_effect=report_root_owned_stage),
                patch.object(installer.os, "rename", side_effect=fail_install_rename),
            ):
                with self.assertRaisesRegex(OSError, "synthetic staged rename failure"):
                    installer._adopt_root_plugin(Path("/runtime"), Path(temporary), "Demo", user)

            self.assertEqual((original / "plugin.json").read_text(encoding="utf-8"), '{"flags":["root"]}')
            self.assertEqual((original / "main.py").read_text(encoding="utf-8"), "original plugin\n")
            self.assertEqual({path.name for path in plugins.iterdir()}, {"Demo"})

    def test_failed_root_enable_restores_files_and_removes_the_new_runtime(self):
        user = pwd.getpwuid(os.geteuid())
        for failing in ("daemon-reload", "enable"):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state_root = root / "root-host"
                state = state_root / str(user.pw_uid)
                units = root / "systemd"
                state.mkdir(parents=True)
                units.mkdir()
                config = state / "config.json"
                original_config = json.dumps({"release_repository": "owner/previous", "runtime": "old"}).encode()
                config.write_bytes(original_config)
                config.chmod(0o640)
                state_file = state / "state.json"
                original_state = b'{"plugins": {}}\n'
                state_file.write_bytes(original_state)
                unit_name = installer._root_host_unit_name(user)
                service = units / f"{unit_name}.service"
                socket = units / f"{unit_name}.socket"
                original_service = b"previous service unit\n"
                service.write_bytes(original_service)
                service.chmod(0o600)
                new_runtime = state / "runtimes" / "new"
                calls = []

                def install_runtime(bundle, account, state_directory, created):
                    new_runtime.mkdir(parents=True)
                    created.append(new_runtime)
                    return new_runtime, {"release_version": "new-release"}

                def systemctl(paths, *arguments, check=True, **kwargs):
                    calls.append(arguments)
                    # Forward steps run checked; the rollback's own calls are unchecked and succeed.
                    if check and arguments[0] == failing:
                        raise installer.InstallerError(f"synthetic {failing} failure")
                    return subprocess.CompletedProcess(arguments, 0, "", "")

                def read_config(path):
                    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

                args = argparse.Namespace(user=user.pw_name, homebrew=str(root / "homebrew"),
                                          bundle=str(root / "bundle"), adopt=None)
                with (
                    patch.object(installer, "_require_root"),
                    patch.object(installer, "_homebrew_path", return_value=root / "homebrew"),
                    patch.object(installer, "ROOT_HOST_STATE", state_root),
                    patch.object(installer, "ROOT_HOST_UNIT_DIRECTORY", units),
                    patch.object(installer, "_install_root_runtime", side_effect=install_runtime),
                    patch.object(installer, "_read_root_host_json", side_effect=read_config),
                    patch.object(installer, "_systemctl", side_effect=systemctl),
                ):
                    with self.assertRaisesRegex(installer.InstallerError, f"synthetic {failing} failure"):
                        installer._run_enable_root_plugins(args)

                self.assertEqual(config.read_bytes(), original_config)
                self.assertEqual(config.stat().st_mode & 0o777, 0o640)
                self.assertEqual(state_file.read_bytes(), original_state)
                self.assertEqual(service.read_bytes(), original_service)
                self.assertEqual(service.stat().st_mode & 0o777, 0o600)
                self.assertFalse(socket.exists())
                self.assertFalse(new_runtime.exists())
                # The socket did not exist before, so a socket this command enabled is disabled again.
                self.assertIn(("disable", "--now", socket.name), calls)

    def test_restored_file_gets_snapshot_contents_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "plugin_loader.service"
            snapshot = base / "snapshot" / target.name
            target.write_bytes(b"before")
            target.chmod(0o640)
            try:
                os.setxattr(target, "user.decky", b"kept")
            except OSError as error:
                self.skipTest(f"user xattrs unavailable: {error}")
            os.utime(target, ns=(1_000_000_000, 2_000_000_000))
            state = installer._snapshot_file(target, snapshot)
            target.write_bytes(b"after")
            target.chmod(0o600)
            os.removexattr(target, "user.decky")
            installer._restore_file(target, snapshot, state)
            self.assertEqual(target.read_bytes(), b"before")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(target.stat().st_mtime_ns, 2_000_000_000)
            self.assertEqual(os.getxattr(target, "user.decky"), b"kept")


if __name__ == "__main__":
    unittest.main()
