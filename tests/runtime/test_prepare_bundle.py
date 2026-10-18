import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[2]
RUNTIME_TOOLS_PATH = REPOSITORY / 'scripts' / 'runtime'
sys.path.insert(0, str(RUNTIME_TOOLS_PATH))
import prepare_bundle


class PrepareBundleSnapshotTests(unittest.TestCase):
    def test_packages_installer_and_observer_from_source_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / 'checkout'
            snapshot = root / 'snapshot'
            backend_rust = checkout / 'backend-rust'
            snapshot_rust = snapshot / 'backend-rust'
            backend_rust.mkdir(parents=True)
            snapshot_rust.mkdir(parents=True)
            (backend_rust / 'install.py').write_text('checkout installer')
            (backend_rust / 'process_observer.py').write_text('checkout observer')
            (backend_rust / 'Cargo.lock').write_text('lock')
            (snapshot_rust / 'install.py').write_text('snapshot installer')
            (snapshot_rust / 'process_observer.py').write_text('snapshot observer')
            (snapshot_rust / 'Cargo.lock').write_text('lock')

            helper = checkout / 'backend/decky_loader/helpers.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('helper')
            (checkout / 'tests/fixtures/runtime').mkdir(parents=True)
            fixtures = checkout / 'tests/fixtures/runtime'
            (fixtures / 'main.py').write_text('pass')
            (fixtures / 'modern.js').write_text('')
            (fixtures / 'legacy.js').write_text('')
            overlays = []
            for name in prepare_bundle.SDK_OVERLAYS:
                path = checkout / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
                overlays.append(name)

            worker = root / 'worker'
            rust = root / 'rust'
            worker.write_text('worker')
            rust.write_text('rust')
            assets = root / 'assets'
            locales = root / 'locales'
            assets.mkdir()
            locales.mkdir()
            (assets / 'index.js').write_text('frontend')
            (locales / 'en-US.json').write_text('{}')
            validation = root / 'validation'
            validation.mkdir()
            (validation / 'verify.py').write_text('')
            (validation / 'validate_user.py').write_text('')

            config = {'sdk': {'tag': 'v1', 'commit': 'commit', 'backend_tree': 'backend', 'frontend_tree': 'frontend'},
                      'python': {'version': '3.13.0'}}
            runtime = {
                'frozen': True,
                'python': '3.13.0',
                'decky_version': 'v1',
                'sdk_sources': {'decky_loader.helpers': {
                    'sha256': hashlib.sha256(b'helper').hexdigest(), 'code_matches_source': True}},
                'json_encoder': {'selected': 'rust', 'default': 'rust', 'native': {
                    'module': '_decky_native_json', 'version': '0.1.0', 'sha256': 'native-hash'}},
            }
            provenance = {'native_json': {'sha256': 'native-hash'}}
            with (patch.object(prepare_bundle, 'REPO', checkout),
                  patch.object(prepare_bundle, 'BACKEND_RUST', backend_rust),
                  patch.object(prepare_bundle, 'HERE', root),
                  patch.object(prepare_bundle, 'BUILD_CONFIG', config),
                  patch.object(prepare_bundle, 'BASELINE_TAG', 'v1'),
                  patch.object(prepare_bundle.subprocess, 'run'),
                  patch.object(prepare_bundle.subprocess, 'check_output') as check_output):
                check_output.side_effect = [
                    'commit\nbackend\nfrontend\n',
                    json.dumps(runtime),
                ]
                output = root / 'output'
                prepare_bundle.prepare(worker, rust, assets, locales, output, 'test',
                                       build_provenance=provenance, source_root=snapshot)

            bundle = output / 'bundle'
            self.assertEqual((bundle / 'install.py').read_text(), 'snapshot installer')
            self.assertEqual((bundle / 'process_observer.py').read_text(), 'snapshot observer')
            self.assertEqual((output / 'process_observer.py').read_text(), 'snapshot observer')


if __name__ == '__main__':
    unittest.main()
