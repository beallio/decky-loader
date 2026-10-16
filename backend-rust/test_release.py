import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).parent))
import release


class PrereleaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR') or '/var/tmp/omp-1000')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tag = 'v3.2.10-rust.7-rc.1'
        self.commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=release.REPO, text=True).strip()
        source = subprocess.check_output(['git', 'show', f'{self.commit}:backend/worker_main.py'], cwd=release.REPO)
        self.payload = {'decky-loader-runtime': b'backend bytes', 'DeckyPluginWorker': b'worker bytes'}
        self.runtime = {'python': release.CONFIG['python']['version'], 'decky_version': release.CONFIG['sdk']['tag']}
        self.manifest = {'kind': 'decky-loader-runtime', 'release_version': self.tag,
                         'source_baseline': release.CONFIG['sdk'], 'runtime': self.runtime,
                         'build': {'base_commit': self.commit,
                                   'source_sha256': {'backend/worker_main.py': hashlib.sha256(source).hexdigest()}},
                         'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in self.payload.items()}}

    def write_bundle(self, modes=None):
        with zipfile.ZipFile(self.root / release.ASSET_NAME, 'w') as archive:
            for name, data in {**self.payload, 'bundle.json': json.dumps(self.manifest).encode()}.items():
                info = zipfile.ZipInfo(name)
                mode = (modes or {}).get(name, 0o644 if name == 'bundle.json' else 0o755)
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, data)
        checksum = hashlib.sha256((self.root / release.ASSET_NAME).read_bytes()).hexdigest()
        (self.root / 'SHA256SUMS').write_text(f'{checksum}  {release.ASSET_NAME}\n')
        (self.root / 'build-result.json').write_text(json.dumps({
            'artifact_sha256': checksum, 'runtime': self.runtime,
            'source_baseline': release.CONFIG['sdk'], 'files': len(self.manifest['sha256']),
        }))

    def test_rejects_changed_payload_even_with_matching_outer_checksums(self):
        self.payload['DeckyPluginWorker'] = b'changed after manifest was made'
        self.write_bundle()
        with self.assertRaises(ValueError):
            release.validate(self.root, self.tag, self.commit)

    def test_rejects_missing_payload_from_an_otherwise_valid_zip(self):
        del self.payload['DeckyPluginWorker']
        self.write_bundle()
        with self.assertRaises(ValueError):
            release.validate(self.root, self.tag, self.commit)

    def test_rejects_a_candidate_from_another_source_or_version(self):
        self.write_bundle()
        with self.assertRaises(ValueError):
            release.validate(self.root, self.tag, '2' * 40)
        with self.assertRaises(ValueError):
            release.validate(self.root, 'v3.2.10-rust.8-rc.1', self.commit)

    def test_rejects_source_changes_not_present_in_the_release_commit(self):
        self.manifest['build']['source_sha256']['backend/worker_main.py'] = '0' * 64
        self.write_bundle()
        with self.assertRaises(ValueError):
            release.validate(self.root, self.tag, self.commit)

    def test_rejects_unusable_executables_and_stable_python_tags(self):
        self.write_bundle({'DeckyPluginWorker': 0o644})
        with self.assertRaises(ValueError):
            release.validate(self.root, self.tag, self.commit)
        self.manifest['release_version'] = 'v3.2.10'
        self.write_bundle()
        with self.assertRaises(ValueError):
            release.validate(self.root, 'v3.2.10', self.commit)


if __name__ == '__main__':
    unittest.main()
