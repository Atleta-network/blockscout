#!/usr/bin/env python3
"""Exercise private upload and CI/native lock interoperability with fake commands.

No SSH, Docker daemon, database or real deployment credentials are used.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_tls', HERE / 'native-db-tls.py')
tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tls)
REAL_FLOCK = shutil.which('flock')


class DeploymentTransaction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='blockscout-transaction-')
        self.root = Path(self.temp.name)
        self.checkout = self.root / 'checkout'
        self.base = self.root / 'live__base with space'
        self.base.mkdir()
        self.alias = self.root / 'alias'
        self.alias.symlink_to(self.base, target_is_directory=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        scripts = self.checkout / '.github/scripts'
        scripts.mkdir(parents=True)
        for name in ['upload-deployment.sh', 'deploy-remote.sh']:
            shutil.copyfile(HERE / name, scripts / name)
        self.bundle = self.checkout / 'docker-compose'
        (self.bundle / 'services').mkdir(parents=True)
        (self.bundle / 'envs').mkdir()
        (self.bundle / 'docker-compose.yml').write_text('services: {}\n')
        self.secret = 'fake-private-deployment-value'
        (self.bundle / '.env').write_text('VALUE=' + self.secret + '\n')
        (self.base / '.env').write_text('VALUE=original-live-value\n')
        for name in ['common-blockscout.env', 'common-stats.env', 'common-user-ops-indexer.env']:
            (self.bundle / 'envs' / name).write_text('VALUE=' + self.secret + '\n')
        (self.bundle / 'services/frontend.yml').write_text('image: old/blockscout-frontend:old\n')
        (self.bundle / 'services/backend.yml').write_text('image: old/blockscout:old\n')
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'HOST_1': 'fake.invalid', 'HOST_1_USERNAME': 'deploy',
                    'BASE_PATH': str(self.alias) + '///', 'GITHUB_RUN_ID': '123456', 'GITHUB_RUN_ATTEMPT': '1',
                    'CI_REGISTRY': 'registry.example.invalid', 'CI_REGISTRY_REPO': 'test', 'SERVICE_TAG': 'a' * 40,
                    'DEPLOY_ID': '123456-1', 'TRACE_FILE': str(self.root / 'trace.jsonl'),
                    'LOCK_READY': str(self.root / 'lock-ready'), 'FAKE_PRIVATE_VALUE': self.secret}
        self.executable('ssh', "import subprocess,sys\nraise SystemExit(subprocess.call(['bash','-c',sys.argv[-1]]))\n")
        self.executable('sudo', 'raise SystemExit(0)\n')
        self.executable('docker', """import json,os,pathlib,sys
args=sys.argv[1:]
with open(os.environ['TRACE_FILE'],'a') as f:f.write(json.dumps({'args':args,'cwd':os.getcwd()})+'\\n')
if args[0]=='inspect':
 if '-f' in args:print('healthy' if 'Health' in args[args.index('-f')+1] else 'running')
 else:print('{}')
else:
 print(os.environ['FAKE_PRIVATE_VALUE'])
 if os.environ.get('REJECT_STAGED_CONFIG')=='1' and args[:3]==['compose','config','--quiet'] and '/.incoming/' in os.getcwd():raise SystemExit(2)
""")
        if REAL_FLOCK:
            self.executable('flock', "import os,pathlib,sys\npathlib.Path(os.environ['LOCK_READY']).touch()\nos.execv(" + repr(REAL_FLOCK) + ',[' + repr(REAL_FLOCK) + ']+sys.argv[1:])\n')

    def executable(self, name, source):
        path = self.bin / name
        path.write_text('#!/usr/bin/env python3\n' + source)
        path.chmod(0o755)

    def tearDown(self):
        path = tls.deployment_lock_path(self.base)
        if path.exists():
            path.unlink()
        self.temp.cleanup()

    def upload(self):
        result = subprocess.run(['bash', str(self.checkout / '.github/scripts/upload-deployment.sh')],
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        return self.base / '.incoming/123456-1'

    def test_upload_changes_only_private_per_run_staging(self):
        stage = self.upload()
        self.assertEqual((self.base / '.env').read_text(), 'VALUE=original-live-value\n')
        self.assertEqual((stage / 'docker-compose/.env').read_text(), 'VALUE=' + self.secret + '\n')
        self.assertEqual(stage.stat().st_mode & 0o077, 0)
        self.assertEqual((stage / 'docker-compose/.env').stat().st_mode & 0o077, 0)
        self.assertTrue((stage / '.github/scripts/deploy-remote.sh').is_file())
        duplicate = subprocess.run(['bash', str(self.checkout / '.github/scripts/upload-deployment.sh')],
                                   env=self.env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual((self.base / '.env').read_text(), 'VALUE=original-live-value\n')

    @unittest.skipUnless(REAL_FLOCK and os.uname().sysname == 'Linux', 'Requires the production Linux flock utility')
    def test_ci_install_and_up_wait_for_native_lock_even_through_alias(self):
        stage = self.upload()
        with tls.deployment_lock(self.base):
            process = subprocess.Popen(['bash', str(stage / '.github/scripts/deploy-remote.sh')],
                                       env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 5
            while not Path(self.env['LOCK_READY']).exists() and time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                time.sleep(0.02)
            self.assertTrue(Path(self.env['LOCK_READY']).exists())
            self.assertIsNone(process.poll())
            self.assertEqual((self.base / '.env').read_text(), 'VALUE=original-live-value\n')
            self.assertFalse(Path(self.env['TRACE_FILE']).exists())
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn(self.secret, stdout + stderr)
        self.assertEqual((self.base / '.env').read_text(), 'VALUE=' + self.secret + '\n')
        records = [json.loads(line) for line in Path(self.env['TRACE_FILE']).read_text().splitlines()]
        ups = [r for r in records if r['args'][:2] == ['compose', 'up']]
        self.assertEqual(len(ups), 1)
        self.assertEqual(Path(ups[0]['cwd']).resolve(), self.base.resolve())
        self.assertFalse(stage.exists())

    @unittest.skipUnless(REAL_FLOCK and os.uname().sysname == 'Linux', 'Requires the production Linux flock utility')
    def test_invalid_staged_config_keeps_live_env_and_private_diagnostics(self):
        stage = self.upload()
        result = subprocess.run(['bash', str(stage / '.github/scripts/deploy-remote.sh')],
                                env={**self.env, 'REJECT_STAGED_CONFIG': '1'}, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.base / '.env').read_text(), 'VALUE=original-live-value\n')
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        log = stage / 'deploy-private.log'
        self.assertIn(self.secret, log.read_text())
        self.assertEqual(log.stat().st_mode & 0o077, 0)


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') == 'true' and (not REAL_FLOCK or os.uname().sysname != 'Linux'):
        raise SystemExit('CI must run the Linux flock interoperability checks without skipping them')
    unittest.main()
