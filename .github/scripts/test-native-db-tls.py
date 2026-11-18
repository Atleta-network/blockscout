#!/usr/bin/env python3
"""Check TLS boundary and preservation of deployment content."""
import importlib.util
from pathlib import Path
import unittest
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
if __name__ == '__main__': unittest.main()
