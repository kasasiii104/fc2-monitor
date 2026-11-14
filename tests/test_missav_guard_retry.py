import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import fc2_monitor as m
import missav_guard as g


class MissAVGuardRetryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(m.os.environ, {"RUNNER_TEMP": "", "GITHUB_RUN_ID": "", "RUN_DEADLINE_SEC": "2700"}))
        self.enterContext(patch.object(m, "_RUN_STARTED", None))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / 'guard.json'
        for p in [patch.object(g, 'STATE_FILE', self.state),
                  patch.object(m, 'DATA_FILE', root / 'data.json'),
                  patch.object(m, 'HISTORY_FILE', root / 'history.json'),
                  patch.object(m, 'NOTIFY_QUEUE_FILE', root / 'notify.json'),
                  patch.object(m, 'CRAWL_FILE', root / 'crawl.json'),
                  patch.object(m, 'now_ts', return_value=1_900_000_000),
                  patch.object(m, 'now_jst', return_value='2030-03-17 12:00:00')]:
            p.start()
            self.addCleanup(p.stop)
        self.item = {'code': 'FC2-PPV-8000001', 'code_num': '8000001',
                     'discovery_kind': 'recent', 'title': '作品タイトル',
                     'sources': {'MissAV': 'https://missav.ws/ja/fc2-ppv-8000001'}}
        self.dead = {'status': 'dead', 'reason': 'all_missav_hosts_404'}
        self.live = {'status': 'alive', 'reason': 'detail_200',
                     'live_url': 'https://missav.live/ja/fc2-ppv-8000001'}

    def enqueue(self, guard):
        with patch.object(guard, 'probe', return_value=self.dead):
            self.assertEqual(g.filter_unseen([deepcopy(self.item)], set(), guard), [])
        return guard.retry_queue[self.item['code']]

    def test_dead_recent_snapshot_is_persistent_and_independent(self):
        guard = g.Guard()
        row = self.enqueue(guard)
        self.assertEqual(row['discovery_kind'], 'recent')
        self.assertEqual(row['attempts'], 1)
        self.item['sources']['MissAV'] = 'changed'
        self.assertNotEqual(row['item']['sources'], self.item['sources'])
        guard.save()
        self.assertEqual(g.Guard().retry_queue, guard.retry_queue)

    def test_absent_video_recovers_as_recent_after_reload(self):
        guard = g.Guard()
        self.enqueue(guard)
        guard.save()
        guard = g.Guard()
        with patch.object(guard, 'probe', return_value=self.live) as probe:
            kept = g.filter_unseen([], set(), guard)
        probe.assert_called_once_with('8000001')
        self.assertEqual(kept[0]['discovery_kind'], 'recent')
        self.assertEqual(kept[0]['url'], self.live['live_url'])
        self.assertEqual(guard.recovered, {self.item["code"]})
        self.assertEqual(guard.retry_queue[self.item["code"]]["recovered_at"], m.now_ts())

    def test_checks_truncation_does_not_truncate_retry_queue(self):
        guard = g.Guard()
        self.enqueue(guard)
        guard.checks.update({str(i): {'checked_at': i} for i in range(40001)})
        guard.save()
        loaded = g.Guard()
        self.assertEqual(len(loaded.checks), 40000)
        self.assertEqual(loaded.retry_queue, guard.retry_queue)

    def test_archive_arrival_keeps_first_kind_and_has_no_duplicates(self):
        guard = g.Guard()
        original = deepcopy(self.enqueue(guard))
        archive = {**self.item, 'discovery_kind': 'archive', 'title': 'later'}
        with patch.object(guard, 'probe', return_value=self.dead):
            g.filter_unseen([archive], set(), guard)
        row = guard.retry_queue[self.item['code']]
        self.assertEqual(row['item'], original['item'])
        self.assertEqual(row['queued_at'], original['queued_at'])
        self.assertEqual(row['attempts'], 2)
        with patch.object(guard, 'probe', return_value=self.live) as probe:
            kept = g.filter_unseen([archive, deepcopy(archive)], set(), guard)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['discovery_kind'], 'recent')
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(guard.recovered, {self.item["code"]})

    def test_access_limited_stops_oldest_first_retry_loop(self):
        guard = g.Guard()
        self.enqueue(guard)
        other = {**self.item, 'code': 'FC2-PPV-8000002', 'code_num': '8000002'}
        g._queue_retry(other, self.dead, guard)
        guard.retry_queue[self.item['code']]['queued_at'] -= 1
        limited = {'status': 'unknown', 'reason': 'access_limited'}
        with patch.object(guard, 'probe', return_value=limited) as probe:
            self.assertEqual(g.filter_unseen([], set(), guard), [])
        probe.assert_called_once_with('8000001')
        self.assertEqual(len(guard.retry_queue), 2)
        self.assertEqual(guard.retry_queue[self.item['code']]['last_status'], 'unknown')
        self.assertEqual(guard.retry_queue[self.item['code']]['attempts'], 2)
        self.assertEqual(guard.retry_queue[other['code']]['attempts'], 1)

    def test_expiration_and_legacy_or_invalid_queue(self):
        for payload in ({'checks': {}}, {'checks': {}, 'retry_queue': []}):
            m.save_json(self.state, payload)
            self.assertEqual(g.Guard().retry_queue, {})
        guard = g.Guard()
        row = self.enqueue(guard)
        row['queued_at'] -= g.MISSAV_RETRY_TTL_SEC + 1
        with patch.object(guard, 'probe') as probe:
            self.assertEqual(g.filter_unseen([], set(), guard), [])
        probe.assert_not_called()
        guard.save()
        self.assertFalse(g.Guard().retry_queue)

    def test_malformed_snapshot_is_normalized_or_dropped_before_recovery(self):
        base = {'queued_at': m.now_ts(), 'attempts': 1, 'discovery_kind': 'recent'}
        m.save_json(self.state, {'checks': {}, 'retry_queue': {
            'FC2-PPV-8000001': {**base, 'item': {'code': 'FC2-PPV-8000001', 'sources': ['x'], 'title': 5}},
            'FC2-PPV-8000002': {**base, 'item': {'code': 'FC2-PPV-9999999'}},
            'FC2-PPV-8000003': {**base, 'item': {'sources': {}}},
            # code_num agrees with the key but the item's own code does not.
            'FC2-PPV-8000004': {**base, 'item': {'code': 'FC2-PPV-9999999', 'code_num': '8000004'}},
        }})
        guard = g.Guard()
        self.assertEqual(list(guard.retry_queue), ['FC2-PPV-8000001'])
        with patch.object(guard, 'probe', return_value=self.live):
            kept = g.filter_unseen([], set(), guard)
        self.assertEqual([item['code'] for item in kept], ['FC2-PPV-8000001'])
        self.assertEqual(kept[0]['sources'], {'MissAV': self.live['live_url']})
        self.assertEqual(kept[0]['title'], '')

    def test_five_digit_code_survives_notify_queue_round_trip(self):
        m.hold_notification('FC2-PPV-12345', {'code': 'FC2-PPV-12345'}, 'missav_dead')
        self.assertEqual([row['code'] for row in m.load_notify_queue()], ['FC2-PPV-12345'])

    def test_existing_code_returns_snapshot_without_probe_or_duplicate(self):
        guard = g.Guard()
        self.enqueue(guard)
        with patch.object(guard, 'probe') as probe:
            kept = g.filter_unseen([], {self.item['code']}, guard)
        probe.assert_not_called()
        self.assertEqual(kept, [self.item])
        self.assertEqual(guard.recovered, {self.item["code"]})
        self.enqueue(guard)
        with patch.object(guard, 'probe') as probe:
            kept = g.filter_unseen([{**self.item, 'discovery_kind': 'archive'}],
                                   {self.item['code']}, guard)
        probe.assert_not_called()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['discovery_kind'], 'recent')

    def test_retry_limit_cap_and_cache(self):
        guard = g.Guard()
        self.enqueue(guard)
        other = {**self.item, 'code': 'FC2-PPV-8000002', 'code_num': '8000002'}
        g._queue_retry(other, self.dead, guard)
        guard.retry_queue[self.item['code']]['queued_at'] -= 1
        with patch.object(g, 'MISSAV_RETRY_LIMIT', 1), patch.object(guard, 'probe', return_value=self.dead) as probe:
            g.filter_unseen([], set(), guard)
        probe.assert_called_once_with('8000001')
        with patch.object(g, 'RETRY_QUEUE_MAX', 1), patch.object(g, 'MISSAV_RETRY_LIMIT', 0):
            g.filter_unseen([], set(), guard)
        self.assertEqual(list(guard.retry_queue), [other['code']])
        guard.checks[other['code']] = {**self.dead, 'checked_at': m.now_ts()}
        with patch.object(g, '_existence_request') as request:
            g.filter_unseen([], set(), guard)
        request.assert_not_called()

    def test_retry_limit_rotates_past_dead_head_and_reads_legacy_rows(self):
        guard = g.Guard()
        self.enqueue(guard)
        other = {**self.item, 'code': 'FC2-PPV-8000002', 'code_num': '8000002'}
        g._queue_retry(other, self.dead, guard)
        for row in guard.retry_queue.values():
            row.pop('last_tried_at')
        guard.save()
        guard = g.Guard()
        with patch.object(g, 'MISSAV_RETRY_LIMIT', 1), \
             patch.object(m, 'now_ts', return_value=1_900_000_001), \
             patch.object(guard, 'probe', return_value=self.dead) as probe:
            g.filter_unseen([], set(), guard)
            probe.assert_called_once_with('8000001')
            probe.reset_mock()
            g.filter_unseen([], set(), guard)
            probe.assert_called_once_with('8000002')
        self.assertEqual(guard.retry_queue[other['code']]['last_tried_at'], 1_900_000_001)

    def test_present_and_existing_rows_do_not_use_retry_slots(self):
        for present in (False, True):
            with self.subTest(present=present):
                guard = g.Guard()
                self.enqueue(guard)
                other = {**self.item, 'code': 'FC2-PPV-8000002', 'code_num': '8000002'}
                g._queue_retry(other, self.dead, guard)
                with patch.object(g, 'MISSAV_RETRY_LIMIT', 1), \
                     patch.object(guard, 'probe', return_value=self.live) as probe:
                    kept = g.filter_unseen([deepcopy(self.item)] if present else [],
                                           set() if present else {self.item['code']}, guard)
                self.assertIn(unittest.mock.call('8000002'), probe.call_args_list)
                self.assertEqual(len(kept), 2)

    def test_prune_only_queues_pending_unprotected_snapshots(self):
        other = {**self.item, 'code': 'FC2-PPV-8000002', 'code_num': '8000002'}
        for already_queued in (False, True):
            with self.subTest(already_queued=already_queued):
                guard = g.Guard()
                guard.retry_queue.clear()
                if already_queued:
                    self.enqueue(guard)
                    guard.retry_queue[self.item['code']]['discovery_kind'] = 'archive'
                before = deepcopy(guard.retry_queue)
                m.save_json(m.DATA_FILE, {'items': [self.item, other]})
                m.save_json(m.HISTORY_FILE, {'ids': [self.item['code'], other['code']]})
                m.save_notify_queue([{'code': self.item['code'], 'queued_at': m.now_ts(),
                                      'item': {**self.item, 'title': 'pending snapshot'}}])
                with patch.object(guard, 'probe', return_value=self.dead):
                    g.prune_saved(guard)
                row = guard.retry_queue[self.item['code']]
                if already_queued:
                    self.assertEqual(guard.retry_queue, before)
                else:
                    self.assertEqual(row, {'item': self.item, 'discovery_kind': 'recent',
                                           'queued_at': m.now_ts(), 'attempts': 0,
                                           'last_reason': 'pruned_404', 'last_status': 'dead'})
                    self.assertIsNot(row['item'], self.item)
                self.assertNotIn(other['code'], guard.retry_queue)
                self.assertTrue(m.load_notify_queue()[0]["held"])
                self.assertEqual(m.load_json(m.HISTORY_FILE, {})['ids'], [])
                self.assertEqual(g.Guard().retry_queue, guard.retry_queue)

    def send_with_real_main(self):
        # Keep queue processing real while isolating metadata/network side effects.
        def optional_step(label, action, *args, **kwargs):
            if action is m.send_telegram:
                return action(*args, **kwargs)
        other = {**self.item, 'code': 'FC2-PPV-7000000', 'code_num': '7000000'}
        with patch.object(m, 'get_latest_videos', return_value=([other], {})), \
             patch.object(m, 'run_optional_step', side_effect=optional_step), \
             patch.object(m, 'send_telegram', return_value=True) as send, \
             patch.object(m, 'save_discovery_cooldowns'), \
             patch.object(m, 'write_site'), \
             patch.object(m, 'build_update_status', return_value={}), \
             patch.object(m, 'INCLUDE_KEYWORDS', []), \
             patch.object(m, 'EXCLUDE_KEYWORDS', []):
            self.assertEqual(m.main(), 0)
        return send

    def test_held_recent_survives_other_discovery_history_and_guarded_recovery(self):
        code = self.item['code']
        m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-7000000']})
        guard = g.Guard()
        self.enqueue(guard)
        guard.save()
        self.assertTrue(m.load_notify_queue()[0]['held'])
        self.send_with_real_main().assert_not_called()
        # efficient/fastlane has recorded the work before the guarded recovery.
        m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-7000000', code]})
        real_main = m.main
        with patch.object(m, 'get_latest_videos', return_value=([], {})), \
             patch.object(m, 'main', side_effect=lambda: real_main(discovery_only=True)), \
             patch.object(g.Guard, 'probe', return_value=self.live), \
             patch.object(m, 'save_discovery_cooldowns'), \
             patch.object(m, 'write_site'), \
             patch.object(m, 'build_update_status', return_value={}):
            self.assertEqual(g.run_guarded_monitor(), 0)
        self.assertFalse(g.Guard().retry_queue)
        self.assertFalse(m.load_notify_queue()[0].get('held', False))
        send = self.send_with_real_main()
        send.assert_called_once()
        self.assertIn(code, send.call_args.args[0])
        self.assertEqual(m.load_notify_queue(), [])

    def test_pruned_pending_survives_archive_retry_and_eventually_sends(self):
        for archive in (False, True):
            for held in (False, True):
                with self.subTest(archive=archive, held=held):
                    guard = g.Guard()
                    guard.retry_queue.clear()
                    if archive:
                        g._queue_retry({**self.item, 'discovery_kind': 'archive'}, self.dead, guard)
                    m.save_json(m.DATA_FILE, {'items': [self.item]})
                    m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-7000000', self.item['code']]})
                    m.save_notify_queue([{'code': self.item['code'], 'item': self.item,
                                          'queued_at': m.now_ts(), 'held': held}])
                    with patch.object(guard, 'probe', return_value=self.dead):
                        g.prune_saved(guard)
                    row = m.load_notify_queue()[0]
                    self.assertTrue(row['held'])
                    self.assertEqual(row['held_reason'], 'pruned_404')
                    self.send_with_real_main().assert_not_called()
                    with patch.object(guard, 'probe', return_value=self.live):
                        kept = g.filter_unseen([], set(), guard)
                    self.assertEqual(kept[0]['discovery_kind'], 'archive' if archive else 'recent')
                    self.send_with_real_main().assert_called_once()
                    self.assertEqual(m.load_notify_queue(), [])

    def test_hold_only_unknown_recent_with_nonempty_history(self):
        code = self.item['code']
        for known in ([], ['FC2-PPV-7000000'], [code]):
            for kind in ('recent', 'archive'):
                for status in ('dead', 'unknown'):
                    with self.subTest(known=known, kind=kind, status=status):
                        m.save_notify_queue([])
                        m.save_json(m.HISTORY_FILE, {'ids': known})
                        guard = g.Guard()
                        item = {**self.item, 'discovery_kind': kind}
                        with patch.object(guard, 'probe', return_value={'status': status}):
                            g.filter_unseen([item], set(), guard)
                        pending = m.load_notify_queue()
                        self.assertEqual(bool(pending), bool(known and code not in known and kind != 'archive'))
                        if pending:
                            self.assertEqual(pending[0]['held_reason'], 'missav_' + status)

    def test_release_all_recovery_paths_without_creating_queue_rows(self):
        for retry in (False, True):
            for present, existing in ((True, False), (True, True), (False, False), (False, True)):
                if not retry and not present:
                    continue
                with self.subTest(retry=retry, present=present, existing=existing):
                    guard = g.Guard()
                    if retry:
                        g._queue_retry(self.item, self.dead, guard)
                    m.save_notify_queue([])
                    m.hold_notification(self.item['code'], self.item, 'missav_dead')
                    with patch.object(guard, 'probe', return_value=self.live):
                        kept = g.filter_unseen([deepcopy(self.item)] if present else [],
                                               {self.item['code']} if existing else set(), guard)
                    self.assertEqual(len(kept), 1)
                    # Non-retry existing items bypass verification; all recovery paths release.
                    if retry or not existing:
                        self.assertFalse(m.load_notify_queue()[0].get('held', False))
                    m.save_notify_queue([])
                    with patch.object(guard, 'probe', return_value=self.live):
                        g.filter_unseen([deepcopy(self.item)], set(), guard)
                    self.assertEqual(m.load_notify_queue(), [])

    def test_first_retry_kind_controls_hold_for_absent_and_present_items(self):
        m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-7000000']})
        for first_kind in ('recent', 'archive'):
            for present in (False, True):
                with self.subTest(first_kind=first_kind, present=present):
                    guard = g.Guard()
                    g._queue_retry({**self.item, 'discovery_kind': first_kind}, self.dead, guard)
                    m.save_notify_queue([])
                    with patch.object(guard, 'probe', return_value=self.dead):
                        g.filter_unseen([{**self.item, 'discovery_kind': 'archive' if first_kind == 'recent' else 'recent'}]
                                        if present else [], set(), guard)
                    self.assertEqual(bool(m.load_notify_queue()), first_kind == 'recent')

    def test_retry_rows_normalize_kind_and_timestamp_and_drop_only_invalid(self):
        base = {'item': self.item, 'queued_at': m.now_ts(), 'attempts': 0}
        rows = {
            self.item['code']: {**base, 'last_tried_at': 'invalid'},
            'FC2-PPV-8000002': {**base, 'discovery_kind': False,
                                'item': {**self.item, 'code': 'FC2-PPV-8000002', 'code_num': '8000002'}},
            'FC2-PPV-8000003': {**base, 'attempts': True},
            'FC2-PPV-8000004': {**base, 'queued_at': True},
            'FC2-PPV-8000005': {**base, 'item': []},
            'bad-code': base,
            'FC2-PPV-1234': base,
            'FC2-PPV-123456789': base,
        }
        m.save_json(self.state, {'retry_queue': rows})
        guard = g.Guard()
        self.assertEqual(set(guard.retry_queue), {self.item['code'], 'FC2-PPV-8000002'})
        for row in guard.retry_queue.values():
            self.assertEqual(row['discovery_kind'], 'recent')
            self.assertNotIn('last_tried_at', row)
        with patch.object(guard, 'probe', return_value=self.dead):
            g.filter_unseen([], set(), guard)

    def test_main_failure_before_history_retains_recovery_for_next_run(self):
        guard = g.Guard()
        self.enqueue(guard)
        guard.save()
        original_latest = m.get_latest_videos

        def failing_main():
            videos, _ = m.get_latest_videos()
            self.assertEqual(len(videos), 1)
            raise RuntimeError('before history save')

        def successful_main():
            videos, _ = m.get_latest_videos()
            self.assertEqual([v['code'] for v in videos], [self.item['code']])
            m.save_json(m.HISTORY_FILE, {'ids': [self.item['code']]})
            return 0

        with patch.object(m, 'get_latest_videos', return_value=([], {})), \
             patch.object(g.Guard, 'probe', return_value=self.live), \
             patch.object(m, 'main', side_effect=failing_main):
            with self.assertRaisesRegex(RuntimeError, 'before history'):
                g.run_guarded_monitor()
        self.assertIs(m.get_latest_videos, original_latest)
        self.assertIn('recovered_at', g.Guard().retry_queue[self.item['code']])
        with patch.object(m, 'get_latest_videos', return_value=([self.item, deepcopy(self.item)], {})), \
             patch.object(g.Guard, 'probe', return_value=self.live), \
             patch.object(m, 'main', side_effect=successful_main):
            self.assertEqual(g.run_guarded_monitor(), 0)
        self.assertFalse(g.Guard().retry_queue)

    def test_run_guarded_monitor_recovers_into_real_main_notify_queue(self):
        m.save_json(m.HISTORY_FILE, {'ids': ['FC2-PPV-7000000']})
        real_main = m.main
        original_latest = m.get_latest_videos
        with patch.object(m, 'get_latest_videos', side_effect=[([deepcopy(self.item)], {}), ([], {})]), \
             patch.object(m, 'main', side_effect=lambda: real_main(discovery_only=True)), \
             patch.object(m, 'save_discovery_cooldowns'), \
             patch.object(m, 'write_site'), \
             patch.object(m, 'build_update_status', return_value={}), \
             patch.object(g.Guard, 'probe', side_effect=[self.dead, self.live]):
            self.assertEqual(g.run_guarded_monitor(), 1)
            self.assertIn(self.item['code'], g.Guard().retry_queue)
            self.assertEqual(g.run_guarded_monitor(), 0)
        self.assertIs(m.get_latest_videos, original_latest)
        queued = m.load_notify_queue()
        self.assertEqual([row['code'] for row in queued], [self.item['code']])
        saved = m.load_json(m.DATA_FILE, {})['items'][0]
        self.assertTrue(saved['is_new'])
        self.assertEqual(saved['discovery_kind'], 'recent')
        self.assertFalse(g.Guard().retry_queue)


if __name__ == '__main__':
    unittest.main()
