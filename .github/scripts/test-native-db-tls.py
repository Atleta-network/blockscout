#!/usr/bin/env python3
"""Check TLS boundary and preservation of deployment content."""
import importlib.util
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_tls', HERE / 'native-db-tls.py')
tls = importlib.util.module_from_spec(spec); spec.loader.exec_module(tls)
BASE = 'local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\nhost all all all scram-sha-256\n'
class NativeTLS(unittest.TestCase):
    def test_gateway_rejection_precedes_internal_allow(self):
        result = tls.build_hba(BASE, [('172.19.0.0/16', '172.19.0.1')])
        self.assertLess(result.index('hostnossl all all 172.19.0.1/32 reject'), result.index('host all all 172.19.0.0/16 scram-sha-256'))
        self.assertLess(result.index('host all all 172.19.0.0/16 scram-sha-256'), result.index('hostnossl all all 0.0.0.0/0 reject'))
        self.assertIn('hostssl all all ::/0 scram-sha-256', result)
        self.assertIn('hostnossl all all ::/0 reject', result)
        self.assertTrue(result.startswith(BASE.split('host all all all')[0]))
        self.assertNotIn('host all all all scram', result)
    def test_ipv6_gateway_also_requires_tls(self):
        self.assertIn('hostnossl all all fd00:19::1/128 reject', tls.build_hba(BASE, [('fd00:19::/64', 'fd00:19::1')]))
    def test_unreviewed_networks_refused(self):
        for networks in [[], [('0.0.0.0/0', '1.1.1.1')], [('172.19.0.0/16', '172.20.0.1')], [('127.0.0.0/8', '127.0.0.1')]]:
            with self.subTest(networks=networks), self.assertRaises(RuntimeError): tls.build_hba(BASE, networks)
    def test_unreviewed_hba_refused(self):
        for baseline in [BASE + 'host all all all scram-sha-256\n', BASE.replace('scram-sha-256', 'md5'), BASE + 'host all all 10.0.0.0/8 trust\n']:
            with self.assertRaises(RuntimeError): tls.build_hba(baseline, [('172.19.0.0/16', '172.19.0.1')])
    def test_patch_preserves_private_content_and_other_services(self):
        text = "services:\n  db-init:\n    image: postgres:17-bookworm\n  db:\n    image: postgres:17-bookworm\n    command: postgres -c max_connections=300\n    environment:\n      POSTGRES_PASSWORD: 'fixture-literal-$-with:punctuation'\n    volumes:\n      - ./original-data:/var/lib/postgresql/data\n  sibling:\n    image: original\n"
        result = tls.patch_service(text, 'db', ['postgres', '-c', 'max_connections=300'])
        self.assertEqual(result.split('  db:\n')[0], text.split('  db:\n')[0]); self.assertEqual(result.split('  sibling:\n')[1], text.split('  sibling:\n')[1])
        self.assertIn("POSTGRES_PASSWORD: 'fixture-literal-$-with:punctuation'", result)
        self.assertIn('./original-data:/var/lib/postgresql/data', result); self.assertIn('"max_connections=300"', result)
        self.assertIn('/etc/db-access/tls/blockscout-db:/run/db-tls:ro', result)
        with self.assertRaises(RuntimeError): tls.patch_service(result, 'db', ['postgres'])
    def test_drift_is_rejected_before_database_recreation(self):
        model={'volumes':[{'source':'/original','target':'/data','type':'bind'}], 'command':['postgres'], 'user':'2000:2000','environment':{'POSTGRES_PASSWORD':'fixture'},'ports':[{'target':5432,'published':'7432','protocol':'tcp'}]}
        container={'Mounts':[{'Source':'/original','Destination':'/data','Type':'bind','RW':True}],'Config':{'Cmd':['postgres'],'User':'2000:2000','Env':['POSTGRES_PASSWORD=fixture']},'HostConfig':{'PortBindings':{'5432/tcp':[{'HostIp':'','HostPort':'7432'}]}}}
        tls.require_live_baseline(model,container)
        for changed in [('source','/different'),('read_only',True)]:
            original=dict(model['volumes'][0]);model['volumes'][0][changed[0]]=changed[1]
            with self.assertRaises(RuntimeError): tls.require_live_baseline(model,container)
            model['volumes'][0]=original
        model['environment']['POSTGRES_PASSWORD']='different'
        with self.assertRaises(RuntimeError): tls.require_live_baseline(model,container)

    def test_source_services_keep_tls_after_deploy(self):
        root = HERE.parents[1] / 'docker-compose/services'
        for name, service in [('db.yml', 'db'), ('stats.yml', 'stats-db')]:
            text = (root / name).read_text().split('  ' + service + ':\n')[1]
            for setting in tls.tls_arguments()[1::2]: self.assertIn(setting, text)
            self.assertIn('/etc/db-access/postgres/blockscout/' + service + ':/run/db-access:ro', text)

    def test_failed_candidate_validation_restores_file_without_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / 'db.yml'
            private.write_bytes(b'original private compose')
            callbacks = []
            with self.assertRaisesRegex(RuntimeError, 'invalid candidate'):
                with tls.compose_transaction(private, private.read_bytes(), b'candidate', private.stat(), lambda: callbacks.append('recreated')):
                    self.assertEqual(private.read_bytes(), b'candidate')
                    raise RuntimeError('invalid candidate')
            self.assertEqual(private.read_bytes(), b'original private compose')
            self.assertEqual(callbacks, [])

    def test_failure_after_recreation_restores_file_before_database_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / 'db.yml'
            private.write_bytes(b'original private compose')
            observed = []
            with self.assertRaisesRegex(RuntimeError, 'apply failed'):
                with tls.compose_transaction(private, private.read_bytes(), b'candidate', private.stat(), lambda: observed.append(private.read_bytes())) as begin:
                    begin()
                    raise RuntimeError('apply failed')
            self.assertEqual(observed, [b'original private compose'])

    def rollback_fixture(self):
        model = {'volumes': [], 'command': ['postgres'], 'user': '2000:2000', 'environment': {'POSTGRES_PASSWORD': 'fixture'}, 'ports': []}
        container = {'State': {'Running': True, 'Health': {'Status': 'healthy'}}, 'Image': 'original-image', 'Mounts': [],
                     'Config': {'Cmd': ['postgres'], 'User': '2000:2000', 'Env': ['POSTGRES_PASSWORD=fixture']},
                     'HostConfig': {'PortBindings': {}}, 'NetworkSettings': {'Networks': {'project': {'IPAddress': '172.19.0.8'}}}}
        return model, container

    def test_rollback_requires_existing_password_login(self):
        model, container = self.rollback_fixture()
        def run(argv, **kwargs):
            return '0' if argv[-1] == 'SELECT 1' else ''
        with patch.object(tls, 'inspect', return_value=container), patch.object(tls, 'sql', return_value='cluster'), patch.object(tls, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'existing password'):
                tls.rollback_database(['docker', 'compose'], 'db', model, 'original-image', 'cluster', 'original-roles', 'fixture')

    def test_rollback_rejects_changed_password_verifiers(self):
        model, container = self.rollback_fixture()
        def sql(service, query):
            return 'cluster' if 'pg_control_system' in query else 'changed-roles'
        with patch.object(tls, 'inspect', return_value=container), patch.object(tls, 'sql', side_effect=sql), patch.object(tls, 'run', return_value='1'):
            with self.assertRaisesRegex(RuntimeError, 'roles/password verifiers'):
                tls.rollback_database(['docker', 'compose'], 'db', model, 'original-image', 'cluster', 'original-roles', 'fixture')

    def test_rollback_success_rechecks_old_credential_and_role_material(self):
        model, container = self.rollback_fixture()
        def sql(service, query):
            return 'cluster' if 'pg_control_system' in query else 'original-roles'
        with patch.object(tls, 'inspect', return_value=container), patch.object(tls, 'sql', side_effect=sql), patch.object(tls, 'run', return_value='1') as run:
            tls.rollback_database(['docker', 'compose'], 'db', model, 'original-image', 'cluster', 'original-roles', 'fixture')
        logins = [c for c in run.call_args_list if c.args[0][-1] == 'SELECT 1']
        self.assertEqual(len(logins), 1)
        self.assertEqual(logins[0].kwargs['env']['PGPASSWORD'], 'fixture')
        self.assertIn('host=172.19.0.8 ', logins[0].args[0][-5])
        self.assertEqual(run.call_args_list[0].kwargs['timeout'], 180)

    def test_lock_is_acquired_before_reading_the_live_baseline(self):
        observed = []
        @contextmanager
        def lock(base):
            observed.append(('lock', base))
            yield
            observed.append(('unlock', base))
        def baseline(environment, service):
            self.assertEqual(observed, [('lock', '/blockscout')])
            observed.append(('baseline', service))
            return 'done'
        with patch.object(tls, 'deployment_lock', side_effect=lock), patch.object(tls, 'apply_locked', side_effect=baseline):
            self.assertEqual(tls.apply('mainnet', 'db'), 'done')
        self.assertEqual(observed[-1], ('unlock', '/blockscout'))

    def test_lock_normalizes_symlinks_and_trailing_slashes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'physical base'
            base.mkdir()
            alias = Path(directory) / 'alias'
            alias.symlink_to(base, target_is_directory=True)
            self.assertEqual(tls.deployment_lock_path(str(base) + '///'), tls.deployment_lock_path(str(alias) + '/'))
        with self.assertRaises(RuntimeError):
            tls.deployment_lock_path('/')

    def test_shared_lock_keeps_project_owner_and_deployer_write_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'services').mkdir()
            private = base / 'services/db.yml'
            private.write_text('private fixture')
            lock = tls.deployment_lock_path(base)
            try:
                with tls.deployment_lock(base):
                    metadata = lock.stat()
                    self.assertEqual((metadata.st_uid, metadata.st_gid), (private.stat().st_uid, private.stat().st_gid))
                    self.assertEqual(metadata.st_mode & 0o777, 0o660)
            finally:
                lock.unlink(missing_ok=True)

    def test_lock_refuses_symlinks_and_nonregular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / 'untouched'
            target.write_bytes(b'private fixture')
            lock = tls.deployment_lock_path(base)
            try:
                lock.symlink_to(target)
                with self.assertRaises(OSError), tls.deployment_lock(base):
                    self.fail('symlink lock accepted')
                self.assertEqual(target.read_bytes(), b'private fixture')
                lock.unlink()
                os.mkfifo(lock, 0o600)
                with self.assertRaises((RuntimeError, OSError)), tls.deployment_lock(base):
                    self.fail('nonregular lock accepted')
            finally:
                lock.unlink(missing_ok=True)
if __name__ == '__main__': unittest.main()
