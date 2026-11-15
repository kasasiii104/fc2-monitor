import unittest
from unittest.mock import patch

import fc2_monitor as m


class HostAllowlistTests(unittest.TestCase):
    def test_defaults_and_normalization(self):
        rules = m.build_host_allowlists({})
        for purpose, hosts in m.DEFAULT_PURPOSE_HOSTS.items():
            for host in hosts:
                for authority in (host, host.upper() + '.', host + ':443'):
                    with self.subTest(purpose=purpose, authority=authority):
                        self.assertTrue(m.url_allowed_for('https://' + authority, purpose, rules))
            self.assertFalse(m.url_allowed_for('https://unrelated.example', purpose, rules))

    def test_official_media_and_subdomain_boundaries(self):
        cases = {
            'fc2_official': ((), ('redirect.fc2.com', 'video.fc2.com', 'foo.adult.contents.fc2.com')),
            'media': (('storage123.contents.fc2.com', 'vip-videoprem1.fc2.com', 'fourhoi.com'),
                      ('xiaojiadianmovie.be', 'evilfc2.com', 'fc2.com.evil.example',
                       'notfourhoi.com', 'sub.fourhoi.com', 'fc2.com')),
            'missav': (('x.missav.ws',), ('evilmissav.ws', 'missav.ws.evil.example')),
            'supjav': (('www.supjav.com',), ('evilsupjav.com',)),
            'javdb': (('x.javdb.com',), ('eviljavdb.com',)),
            'fc2cmadb': ((), ('sub.fc2cmadb.com',)),
            'walker': ((), ('sub.fc2cm.h-walker.net',)),
            'av123': ((), ('sub.123av.com',)),
        }
        for purpose, (allowed, denied) in cases.items():
            for expected, hosts in ((True, allowed), (False, denied)):
                for host in hosts:
                    with self.subTest(purpose=purpose, host=host):
                        self.assertEqual(m.url_allowed_for('https://' + host, purpose), expected)

    def test_invalid_inputs_for_all_purposes(self):
        for purpose, hosts in m.DEFAULT_PURPOSE_HOSTS.items():
            host = next(iter(hosts))
            for url in (None, 1, [], {}, b'https://fourhoi.com', '', 'https:///path',
                        'https://[broken', 'http://' + host, 'ftp://' + host,
                        'https://user@' + host, 'https://:pass@' + host,
                        'https://' + host + ':bad', 'https://' + host + ':65536',
                        'https://' + host + ':8443'):
                with self.subTest(purpose=purpose, url=url):
                    self.assertFalse(m.url_allowed_for(url, purpose))

    def test_overrides_are_purpose_specific(self):
        for env, purpose in (('MISSAV_URL', 'missav'), ('JAVDB_URL', 'javdb'),
                             ('SUPJAV_URL', 'supjav'), ('FC2CMADB_URL', 'fc2cmadb'),
                             ('H_WALKER_URL', 'walker')):
            rules = m.build_host_allowlists({env: 'https://CUSTOM.EXAMPLE./path'})
            for other in rules:
                self.assertEqual(m.url_allowed_for('https://custom.example', other, rules), other == purpose)
            self.assertEqual(m.url_allowed_for('https://sub.custom.example', purpose, rules),
                             purpose in ('missav', 'javdb', 'supjav'))

    def test_invalid_overrides_are_ignored(self):
        base = m.build_host_allowlists({})
        for value in (None, 123, '', 'broken', 'https:///path', 'https://[broken',
                      'https://custom.example:bad', 'https://custom.example:65536',
                      'https://user@custom.example', 'http://custom.example'):
            with self.subTest(value=value):
                self.assertEqual(m.build_host_allowlists({'MISSAV_URL': value}), base)

    def test_override_port_requires_exact_host_port_pair(self):
        rules = m.build_host_allowlists({'MISSAV_URL': 'https://custom.example:8443/path'})
        for authority in ('custom.example:8443', 'CUSTOM.EXAMPLE.:8443',
                          'custom.example', 'custom.example:443'):
            self.assertTrue(m.url_allowed_for('https://' + authority, 'missav', rules))
        for authority in ('custom.example:9443', 'sub.custom.example:8443', 'missav.ws:8443'):
            self.assertFalse(m.url_allowed_for('https://' + authority, 'missav', rules))
        self.assertFalse(m.url_allowed_for('https://custom.example:8443', 'javdb', rules))

    def test_link_only_host_is_not_allowed(self):
        self.assertEqual(m.LINK_ONLY_HOSTS, frozenset((
            'missav.ws', 'supjav.com', 'javdb.com', 'adult.contents.fc2.com',
            'fc2cmadb.com', '123av.com', 'javfc2.xyz')))
        for purpose in m.DEFAULT_PURPOSE_HOSTS:
            self.assertFalse(m.url_allowed_for('https://javfc2.xyz', purpose))

    def test_unknown_purpose_raises(self):
        for url in (None, 'https://missav.ws'):
            with self.assertRaises(ValueError):
                m.url_allowed_for(url, 'typo')

    def test_module_rules_and_discovery_defaults(self):
        self.assertEqual(m.HOST_ALLOWLISTS, m.build_host_allowlists({
            'MISSAV_URL': m.MISSAV_URL, 'JAVDB_URL': m.JAVDB_URL,
            'SUPJAV_URL': m.SUPJAV_URL, 'FC2CMADB_URL': m.FC2CMADB_URL,
            'H_WALKER_URL': m.H_WALKER_URL}))
        rules = m.build_host_allowlists({'MISSAV_URL': 'https://custom.example'})
        with patch.object(m, 'HOST_ALLOWLISTS', rules):
            self.assertEqual(m.discovery_source('https://custom.example'), '')
            self.assertEqual(m.discovery_source('https://x.missav.ws'), 'missav')
