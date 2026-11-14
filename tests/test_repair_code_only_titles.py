import contextlib
import copy
import io
import unittest
from unittest.mock import Mock, patch

import repair_code_only_titles as repair


class CodeOnlyTitleRepairTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(repair.m.os.environ, {"RUNNER_TEMP": "", "GITHUB_RUN_ID": "", "RUN_DEADLINE_SEC": "2700"}))
        self.enterContext(patch.object(repair.m, "_RUN_STARTED", None))

    def test_generic_page_punctuation_and_site_names(self):
        for title in ("Just a moment...", "ログイン | MissAV", "Sign In - FC2",
                      "検索結果 - MissAV", "！ログイン…", "FC2-PPV | ログイン"):
            with self.subTest(title=title):
                self.assertTrue(repair.is_generic_page_title(title))

    def test_main_preserves_host_stop_without_reloading_state_per_item(self):
        items = [
            {"code": f"FC2-PPV-{num}", "code_num": num,
             "title": f"FC2-PPV-{num}",
             "url": f"https://{repair.m.FC2_MARKET_HOST}/article/{num}/"}
            for num in ("8000002", "8000001")
        ]
        # Copy on read/write models separate JSON loads and persisted snapshots.
        files = {repair.m.DATA_FILE: {"items": items}, repair.m.FETCH_STATE_FILE: {}}

        def load(path, default):
            return copy.deepcopy(files.get(path, default))

        def save(path, value):
            files[path] = copy.deepcopy(value)

        response = Mock(status_code=403, headers={"Retry-After": "90"}, text="")
        with patch.object(repair.m, "_FC2_STOPPED_THIS_RUN", False), \
                patch.object(repair.m, "load_json", side_effect=load) as loads, \
                patch.object(repair.m, "save_json", side_effect=save), \
                patch.object(repair.m, "now_ts", return_value=100000), \
                patch.object(repair.requests, "get", return_value=response) as get, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(), 0)
            saved = files[repair.m.FETCH_STATE_FILE]
            self.assertEqual(saved["fc2_market_host_stop"]["until"], 100090)
            repair.m._FC2_STOPPED_THIS_RUN = False
            self.assertTrue(repair.m.fc2_market_blocked(saved, 100001))
            self.assertEqual(sum(call.args[0] == repair.m.FETCH_STATE_FILE
                                 for call in loads.call_args_list), 1)
            official_calls = [call for call in get.call_args_list
                              if repair.m.FC2_MARKET_HOST in call.args[0]]
            self.assertEqual(len(official_calls), 1)

    def test_normal_titles_with_site_names_are_not_generic(self):
        for title in ("ログインして見たい素人", "ログインして見たい素人の…",
                      "会員登録特典つき 素人作品", "MissAV | 素人作品 ログイン特典",
                      "MissAV | ログイン | FC2"):
            with self.subTest(title=title):
                self.assertFalse(repair.is_generic_page_title(title))

    def run_repair(self, title, source="MissAV", attempted=True):
        item = {"code": "FC2-PPV-8000001", "code_num": "8000001", "title": title}
        if source is not None:
            item["title_source"] = source
        payload = {"items": [item]}
        state = {repair.STATE_KEY: {"attempts": {item["code"]: 1} if attempted else {}}}
        with patch.object(repair.m, "load_json", side_effect=[payload, state]), \
                patch.object(repair.m, "save_json") as save, \
                patch.object(repair.m, "now_ts", return_value=100000), \
                patch.object(repair, "fetch_title", return_value=("", "")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(repair.main(), 0)
        return item, state[repair.STATE_KEY], save

    def test_normal_titles_containing_generic_words_are_preserved(self):
        for title in ("彼女とログインした夜", "新規登録した彼女の物語", "パスワードを忘れた彼女",
                      "検索結果から始まる恋", "サインインした彼女", "ログイン - 特別編"):
            with self.subTest(title=title):
                self.assertFalse(repair.is_generic_page_title(title))
                item, stats, save = self.run_repair(title)
                self.assertEqual(item["title"], title)
                self.assertEqual(stats["rolled_back_generic_or_ambiguous"], 0)
                self.assertEqual(save.call_count, 1)

    def test_exact_generic_titles_with_repair_evidence_are_rolled_back(self):
        for title in ("ログイン", "新規登録", " | ログイン — ", "\tSign IN｜", "MissAV", "検索結果"):
            for source in ("MissAV", "取得元", "FC2ウォーカー"):
                with self.subTest(title=title, source=source):
                    self.assertTrue(repair.is_generic_page_title(title))
                    item, stats, save = self.run_repair(title, source=source)
                    self.assertEqual(item["title"], item["code"])
                    self.assertEqual(item["title_source"], "")
                    self.assertEqual(stats["rolled_back_generic_or_ambiguous"], 1)
                    self.assertEqual(save.call_count, 2)

    def test_generic_titles_without_repair_evidence_are_preserved(self):
        for source, attempted in ((None, False), ("MissAV", False), (None, True), ("FC2公式", True)):
            with self.subTest(source=source, attempted=attempted):
                item, stats, save = self.run_repair("ログイン", source, attempted)
                self.assertEqual(item["title"], "ログイン")
                self.assertEqual(stats["rolled_back_generic_or_ambiguous"], 0)
                self.assertEqual(save.call_count, 1)

    def test_ambiguous_missav_repair_is_still_rolled_back(self):
        item, stats, _ = self.run_repair("中文測試作品")
        self.assertEqual(item["title"], item["code"])
        self.assertEqual(stats["rolled_back_generic_or_ambiguous"], 1)
