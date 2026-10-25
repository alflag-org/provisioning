"""Every nested HTTP prototype must verify the certificate before sending a token."""

import importlib.util
import unittest
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / 'roles/services/zabbix_api/filter_plugins/zabbix_tls.py'
SPEC = importlib.util.spec_from_file_location('zabbix_tls', PATH)
tls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tls)


class HttpTemplateTlsTests(unittest.TestCase):
    def test_nested_http_prototypes_require_peer_and_hostname_verification(self):
        source = {'discovery_rules': [{'item_prototypes': [
            {'type': 'HTTP_AGENT', 'verify_peer': 'NO', 'url': 'https://node/api'},
            {'type': 'DEPENDENT', 'key': 'dependent'}]}]}
        result = tls.verify_http_tls(source)
        http, dependent = result['discovery_rules'][0]['item_prototypes']
        self.assertEqual(http['verify_peer'], 'YES')
        self.assertEqual(http['verify_host'], 'YES')
        self.assertEqual(http['url'], 'https://node/api')
        self.assertEqual(dependent, {'type': 'DEPENDENT', 'key': 'dependent'})
        self.assertEqual(source['discovery_rules'][0]['item_prototypes'][0]['verify_peer'], 'NO')


if __name__ == '__main__':
    unittest.main()
