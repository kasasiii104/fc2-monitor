import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fc2_monitor as monitor


def json_bytes(payload):
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class FetchStateShardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "fetch_state.json"
        self.shard_dir = self.root / "fetch_state-shards"
        self.enterContext(patch.multiple(
            monitor, FETCH_STATE_FILE=self.path, FETCH_STATE_SHARD_MIN_ENTRIES=3,
            FETCH_STATE_SHARD_TARGET=4, FETCH_STATE_SHARD_MAX_ENTRIES=32,
        ))
        self.state = {
            "stats": {"作品": 1},
            "attempts": {f"作品{i}": {"status": i} for i in reversed(range(48))},
            "cursor": "次",
            "cache": {f"key{i}": i for i in range(8)},
            "invalid-section": {str(i): i for i in range(4)},
        }

    def save(self):
        monitor.save_json(self.path, self.state)

    def manifest(self):
        return json.loads(self.path.read_bytes())

    def load(self, path=None):
        return monitor.load_json(path or self.path, {})

    def snapshot(self):
        return {path: path.read_bytes() for path in self.root.rglob("*.json")}

    def assert_invalid(self, reason):
        with self.assertRaisesRegex(monitor.StateStructureError, rf"{self.path.name}: .*{reason}"):
            self.load()

    def test_round_trip_preserves_order_and_legacy_bytes(self):
        self.save()
        manifest = self.manifest()
        self.assertEqual(manifest["format"], monitor.FETCH_STATE_SHARD_FORMAT)
        self.assertEqual(list(manifest["sections"]), ["attempts", "cache"])
        self.assertEqual(list(manifest["inline"]), ["stats", "cursor", "invalid-section"])
        with patch.object(monitor, "FETCH_STATE_FILE", self.path.with_name("other.json")):
            restored = self.load()
        self.assertEqual(restored, self.state)
        self.assertEqual(list(restored), list(self.state))
        for name in manifest["sections"]:
            self.assertEqual(list(restored[name]), list(self.state[name]))
        self.assertEqual(json_bytes(restored), json_bytes(self.state))

    def test_manifests_in_same_parent_use_separate_shard_directories(self):
        self.save()
        original_shards = set(self.shard_dir.glob("*.json"))
        other = self.root / "state.v2.json"
        other_shard_dir = self.root / "state.v2-shards"
        with patch.object(monitor, "FETCH_STATE_FILE", other):
            monitor.save_json(other, self.state)
            self.assertTrue(list(other_shard_dir.glob("*.json")))
            self.assertEqual(self.load(other), self.state)
            self.assertEqual(self.load(), self.state)

            wrong_stem = other.with_name("stateXv2.json")
            wrong_stem.write_bytes(other.read_bytes())
            with self.assertRaisesRegex(monitor.StateStructureError, "invalid shard path"):
                self.load(wrong_stem)

            monitor.save_json(other, {})
            self.assertFalse(list(other_shard_dir.glob("*.json")))
            self.assertEqual(set(self.shard_dir.glob("*.json")), original_shards)
            self.assertEqual(self.load(), self.state)

    def test_same_stem_non_json_path_preserves_fetch_state_shards(self):
        self.save()
        original = self.snapshot()
        other = self.path.with_suffix(".txt")
        with patch.object(monitor, "FETCH_STATE_FILE", other):
            for payload in ({}, {**self.state, "format": "legacy"}):
                monitor.save_json(other, payload)
                self.assertEqual(other.read_bytes(), json_bytes(payload))
                self.assertEqual(self.snapshot(), original)
                self.assertEqual(self.load(other), payload)
                self.assertEqual(self.load(), self.state)

    def test_data_shard_directory_is_reserved_for_data(self):
        data = {"items": [{"id": "1"}, {"id": "2"}]}
        for data_name, fetch_name in (("data.json", "data.txt"), ("library.json", "data.json")):
            with self.subTest(data_name=data_name, fetch_name=fetch_name):
                parent = self.root / fetch_name
                data_path = parent / data_name
                fetch_path = parent / fetch_name
                with patch.multiple(monitor, DATA_FILE=data_path, DATA_SHARD_ITEMS=1,
                                    FETCH_STATE_FILE=fetch_path):
                    monitor.save_json(data_path, data)
                    shard_dir = parent / monitor.DATA_SHARD_DIR
                    shards = {path.name: path.read_bytes() for path in shard_dir.glob("*.json")}
                    self.assertEqual(len(shards), 2)
                    for payload in ({}, self.state):
                        monitor.save_json(fetch_path, payload)
                        self.assertEqual(fetch_path.read_bytes(), json_bytes(payload))
                        self.assertEqual(
                            {path.name: path.read_bytes() for path in shard_dir.glob("*.json")}, shards
                        )
                        self.assertEqual(self.load(data_path), data)
                        self.assertEqual(self.load(fetch_path), payload)

    def test_data_sharding_takes_precedence_for_same_path(self):
        payload = {"items": [{"id": "1"}, {"id": "2"}]}
        with patch.multiple(monitor, DATA_FILE=self.path, DATA_SHARD_ITEMS=1):
            monitor.save_json(self.path, payload)
            self.assertEqual(self.manifest()["format"], monitor.DATA_SHARD_FORMAT)
            self.assertTrue(list((self.root / "data-shards").glob("data-*.json")))
            self.assertEqual(self.load(), payload)

    def test_top_level_format_round_trips_inline(self):
        format_name = monitor.FETCH_STATE_SHARD_FORMAT
        for payload in (
            {"format": format_name, "cursor": 7},
            {"format": format_name, "cursor": 7, "version": 1,
             "key_order": [], "inline": {}, "sections": {}},
            {"format": {"a": 1, "b": 2, "c": 3}, "cursor": 7},
        ):
            with self.subTest(payload=payload):
                monitor.save_json(self.path, payload)
                manifest = self.manifest()
                self.assertEqual(manifest["inline"], payload)
                self.assertEqual(manifest["sections"], {})
                self.assertEqual(json_bytes(self.load()), json_bytes(payload))

    def test_legacy_file_migrates_on_save(self):
        self.path.write_bytes(json_bytes(self.state))
        monitor.save_json(self.path, self.load())
        self.assertEqual(self.manifest()["format"], monitor.FETCH_STATE_SHARD_FORMAT)
        self.assertEqual(json_bytes(self.load()), json_bytes(self.state))

    def test_small_state_and_other_paths_keep_legacy_bytes(self):
        small = {"cursor": "次", "attempts": {"作品": 1}, "invalid-section": self.state["attempts"]}
        monitor.save_json(self.path, small)
        self.assertEqual(self.path.read_bytes(), json_bytes(small))
        other = self.root / "other.json"
        monitor.save_json(other, self.state)
        self.assertEqual(other.read_bytes(), json_bytes(self.state))
        self.assertFalse(self.shard_dir.exists())

    def test_local_edits_keep_other_shard_names(self):
        attempts = self.state["attempts"]
        edits = (
            ("insert first", {"first": 1, **attempts}),
            ("change value", {**attempts, "作品24": {"status": -1}}),
            ("delete", {key: value for key, value in attempts.items() if key != "作品25"}),
            ("append", {**attempts, "last": 1}),
        )
        for description, updated_attempts in edits:
            with self.subTest(edit=description):
                self.state["attempts"] = attempts
                self.save()
                before = {path.name for path in self.shard_dir.glob("*.json")}
                self.state["attempts"] = updated_attempts
                self.save()
                after = {path.name for path in self.shard_dir.glob("*.json")}
                self.assertIn(len(before - after), range(3))
                self.assertIn(len(after - before), range(3))
                self.assertTrue(before ^ after)
                referenced = {Path(shard["path"]).name
                              for section in self.manifest()["sections"].values()
                              for shard in section["shards"]}
                self.assertEqual(after, referenced)
                self.assertEqual(json_bytes(self.load()), json_bytes(self.state))

    def test_downgrade_removes_shards(self):
        self.save()
        unrelated = self.shard_dir / "data-001-0123456789abcdef.json"
        unrelated.write_bytes(json_bytes({}))
        self.state["attempts"] = {"作品": 1}
        self.state["cache"] = {}
        self.save()
        self.assertEqual(self.path.read_bytes(), json_bytes(self.state))
        self.assertEqual(list(self.shard_dir.glob("*.json")), [unrelated])

    def test_tampering_and_missing_shard(self):
        self.save()
        descriptor = self.manifest()["sections"]["attempts"]["shards"][0]
        shard = self.root / descriptor["path"]
        raw = shard.read_bytes()
        shard.write_bytes(raw[:-1] + b" ")
        self.assert_invalid("sha256 mismatch")
        shard.unlink()
        self.assert_invalid(shard.name)

    def test_invalid_manifest(self):
        self.save()
        original = self.path.read_bytes()
        corruptions = (
            ("version", lambda m, s: m.update(version=True)),
            ("version", lambda m, s: m.update(version=2)),
            ("invalid key_order", lambda m, s: m["key_order"].append("stats")),
            ("key_order", lambda m, s: m["key_order"].pop()),
            ("key_order", lambda m, s: m["inline"].update(attempts={})),
            ("entry_count", lambda m, s: s.update(entry_count=0)),
            ("nonempty list", lambda m, s: s["shards"].clear()),
            ("bytes mismatch", lambda m, s: s["shards"][0].update(bytes=0)),
            ("count mismatch", lambda m, s: s["shards"][0].update(count=0)),
            ("duplicate shard path", lambda m, s: s["shards"].append(s["shards"][0])),
            ("invalid shard path", lambda m, s: s["shards"].append(m["sections"]["cache"]["shards"][0])),
        )
        for reason, corrupt in corruptions:
            with self.subTest(reason=reason):
                manifest = json.loads(original)
                corrupt(manifest, manifest["sections"]["attempts"])
                self.path.write_bytes(json_bytes(manifest))
                self.assert_invalid(reason)

    def test_duplicate_keys_across_distinct_valid_shards(self):
        self.save()
        manifest = self.manifest()
        shards = manifest["sections"]["attempts"]["shards"]
        first = json.loads((self.root / shards[0]["path"]).read_bytes())["entries"]
        key = next(iter(first))
        raw = json_bytes({"entries": {key: "duplicate"}})
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"fetch_state-shards/attempts-{digest[:16]}.json"
        (self.root / relative).write_bytes(raw)
        shards.append({"path": relative, "count": 1, "bytes": len(raw), "sha256": digest})
        manifest["sections"]["attempts"]["entry_count"] += 1
        self.path.write_bytes(json_bytes(manifest))
        self.assert_invalid("duplicate key across shards")

    def test_size_limits_leave_all_files_unchanged(self):
        self.save()
        before = self.snapshot()
        self.state["attempts"]["作品24"] = "次" * 500
        with patch.object(monitor, "FETCH_STATE_SHARD_MAX_BYTES", 1000):
            with self.assertRaisesRegex(ValueError, r"attempts: \d+ bytes"):
                self.save()
        self.assertEqual(self.snapshot(), before)
        self.state["attempts"]["作品24"] = {"status": 24}
        self.state["cursor"] = "次" * 2000
        with patch.object(monitor, "FETCH_STATE_SHARD_MAX_BYTES", 1000):
            with self.assertRaisesRegex(ValueError, r"manifest: \d+ bytes"):
                self.save()
        self.assertEqual(self.snapshot(), before)

    def test_invalid_configuration_leaves_all_files_unchanged(self):
        self.save()
        before = self.snapshot()
        for setting in ("MIN_ENTRIES", "TARGET", "MAX_BYTES"):
            name = f"FETCH_STATE_SHARD_{setting}"
            for invalid in (0, -1, True, 1.5):
                with self.subTest(name=name, invalid=invalid):
                    with patch.object(monitor, name, invalid), self.assertRaisesRegex(ValueError, name):
                        monitor.save_json(self.path, {})
                    self.assertEqual(self.snapshot(), before)

    def test_failed_manifest_write_leaves_old_state_readable(self):
        self.save()
        before = self.snapshot()
        old_state = json_bytes(self.load())
        self.state["attempts"]["作品24"] = "new"
        atomic_write = monitor.atomic_write_text

        def fail_manifest(path, text):
            if path == self.path:
                raise OSError("manifest write failed")
            atomic_write(path, text)

        with patch.object(monitor, "atomic_write_text", side_effect=fail_manifest) as write:
            with self.assertRaisesRegex(OSError, "manifest write failed"):
                self.save()
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args_list[0].args[0].parent, self.shard_dir)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(json_bytes(self.load()), old_state)

    def test_validation_and_data_shards_coexist(self):
        data_path = self.root / "data.json"
        data = {"items": [{"id": "1"}, {"id": "2"}]}
        with patch.multiple(
            monitor, DATA_FILE=data_path, DATA_SHARD_ITEMS=1,
            CRAWL_FILE=self.root / "crawl_state.json", HISTORY_FILE=self.root / "history.json",
            VIEWS_HISTORY_FILE=self.root / "views_history.json",
        ), patch.dict("os.environ", {"MISSAV_GUARD_STATE_FILE": str(self.root / "guard.json")}):
            self.save()
            fetch_paths = set(self.shard_dir.glob("*.json"))
            monitor.save_json(data_path, data)
            self.assertEqual(set(self.shard_dir.glob("*.json")), fetch_paths)
            data_shard_dir = self.root / "data-shards"
            data_paths = set(data_shard_dir.glob("*.json"))
            self.assertTrue(data_paths)
            self.save()
            self.assertEqual(set(data_shard_dir.glob("*.json")), data_paths)
            self.assertEqual(monitor.validate_state_files()[data_path], data)
            monitor.save_json(self.path, {})
            self.assertEqual(set(data_shard_dir.glob("*.json")), data_paths)
