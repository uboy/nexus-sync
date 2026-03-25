import unittest

import nexussync


class NexusSyncStateTests(unittest.TestCase):
    def test_normalize_sync_state_migrates_legacy_synced_assets(self):
        legacy_state = {
            'last_sync_date': '2026-03-25T15:00:00+00:00',
            'synced_assets': [
                {
                    'path': '/@panda/sdk/-/sdk-1.5.0-dev.64980.tgz',
                    'syncedAt': '2026-03-25T15:13:24+00:00',
                }
            ],
        }

        normalized = nexussync.normalize_sync_state(legacy_state)

        self.assertEqual(normalized['last_discovery_date'], legacy_state['last_sync_date'])
        self.assertIn('@panda/sdk', normalized['known_packages'])
        package_state = normalized['known_packages']['@panda/sdk']
        self.assertEqual(package_state['known_versions'], ['1.5.0-dev.64980'])
        self.assertEqual(package_state['mirrored_versions'], ['1.5.0-dev.64980'])
        self.assertFalse(package_state['metadata_initialized'])

    def test_determine_versions_to_process_baselines_existing_known_versions(self):
        package_state = {
            'known_versions': ['1.0.0'],
            'pending_versions': [],
            'metadata_initialized': False,
        }

        result = nexussync.determine_versions_to_process(package_state, {'1.0.0', '1.1.0'})

        self.assertEqual(result, [])

    def test_determine_versions_to_process_uses_forced_versions(self):
        package_state = {
            'known_versions': ['1.0.0'],
            'pending_versions': ['1.0.5'],
            'metadata_initialized': True,
        }

        result = nexussync.determine_versions_to_process(package_state, {'1.0.0', '1.1.0'}, forced_versions=['1.1.0'])

        self.assertEqual(result, ['1.0.5', '1.1.0'])


class NexusSyncParsingTests(unittest.TestCase):
    def test_build_expected_tarball_path_prefers_metadata_tarball_url(self):
        version_metadata = {
            'dist': {
                'tarball': 'https://example.test/repository/koala-npm/@panda/sdk/-/sdk-1.5.0-dev.64980.tgz'
            }
        }

        path = nexussync.build_expected_tarball_path('@panda/sdk', '1.5.0-dev.64980', version_metadata)

        self.assertEqual(path, '/@panda/sdk/-/sdk-1.5.0-dev.64980.tgz')

    def test_collect_new_packages_from_assets_skips_known_names_and_non_tgz(self):
        assets = [
            {'path': '/@known/pkg', 'npm': {'name': '@known/pkg'}},
            {'path': '/@known/pkg/-/pkg-1.0.0.tgz', 'npm': {'name': '@known/pkg', 'version': '1.0.0'}},
            {'path': '/@new/pkg/-/pkg-2.0.0.tgz', 'npm': {'name': '@new/pkg', 'version': '2.0.0'}},
            {'path': '/plain/-/plain-3.1.4.tgz', 'npm': {'name': 'plain', 'version': '3.1.4'}},
        ]

        discovered = nexussync.collect_new_packages_from_assets(assets, {'@known/pkg'})

        self.assertEqual(discovered, {'@new/pkg': ['2.0.0'], 'plain': ['3.1.4']})

    def test_select_cache_invalidation_repositories_filters_proxy_npm_repos(self):
        repositories = [
            {'name': 'npm-proxy', 'type': 'proxy', 'format': 'npm'},
            {'name': 'maven-proxy', 'type': 'proxy', 'format': 'maven2'},
            {'name': 'npm-hosted', 'type': 'hosted', 'format': 'npm'},
        ]

        selected = nexussync.select_cache_invalidation_repositories(repositories, npm_only=True)

        self.assertEqual(selected, ['npm-proxy'])


if __name__ == '__main__':
    unittest.main()
