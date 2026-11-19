import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import compact_public_catalog as compact
import fc2_monitor as monitor


def json_bytes(payload):
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class DataShardTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "data.json"
        self.shard_dir = root / "data-shards"
        self.enterContext(patch.multiple(
            monitor, DATA_FILE=self.path, DATA_SHARD_ITEMS=3, DATA_SHARD_MAX_BYTES=50_000_000,
            CRAWL_FILE=root / "crawl_state.json", FETCH_STATE_FILE=root / "fetch_state.json",
            HISTORY_FILE=root / "notified_ids.json", VIEWS_HISTORY_FILE=root / "views_history.json",
        ))
        self.enterContext(patch.dict(
            "os.environ", {"MISSAV_GUARD_STATE_FILE": str(root / "guard.json")}
        ))
        self.payload = {
            "updated_at": "2026-11-19",
            "extra": "日本語",
            "items": [{"title": f"作品{i}", "code": i} for i in range(10)],
        }

    def manifest(self):
        return json.loads(self.path.read_bytes())

    @property
    def first_shard(self):
        return self.path.parent / self.manifest()["shards"][0]["path"]

    def assert_round_trip(self, path):
        restored = monitor.load_json(path, {})
        self.assertEqual(restored, self.payload)
        self.assertEqual(json_bytes(restored), json_bytes(self.payload))

    def test_round_trip_preserves_order_and_shard_metadata(self):
        monitor.save_json(self.path, self.payload)
        manifest = self.manifest()
        self.assertEqual(manifest["format"], "fc2-monitor-data-shards")
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["item_count"], 10)
        self.assertEqual([shard["count"] for shard in manifest["shards"]], [3, 3, 3, 1])
        for number, shard in enumerate(manifest["shards"], 1):
            raw = (self.path.parent / shard["path"]).read_bytes()
            start = (number - 1) * 3
            self.assertEqual(raw, json_bytes({"items": self.payload["items"][start:start + 3]}))
            self.assertEqual(shard["bytes"], len(raw))
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(shard["sha256"], digest)
            self.assertEqual(shard["path"], f"data-shards/data-{number:03d}-{digest[:16]}.json")
        other = self.path.with_name("copy.json")
        other.write_bytes(self.path.read_bytes())
        self.assert_round_trip(self.path)
        self.assert_round_trip(other)

    def test_legacy_file_migrates_on_save(self):
        self.path.write_bytes(json_bytes(self.payload))
        self.assert_round_trip(self.path)
        monitor.save_json(self.path, monitor.load_json(self.path, {}))
        self.assertTrue(self.first_shard.is_file())
        self.assert_round_trip(self.path)

    def test_small_and_non_data_files_keep_legacy_bytes(self):
        small = {**self.payload, "items": self.payload["items"][:3]}
        monitor.save_json(self.path, small)
        self.assertEqual(self.path.read_bytes(), json_bytes(small))
        other = self.path.with_name("other.json")
        monitor.save_json(other, self.payload)
        self.assertEqual(other.read_bytes(), json_bytes(self.payload))
        self.assertFalse(self.shard_dir.exists())

    def test_shrinking_cleans_stale_shards(self):
        monitor.save_json(self.path, self.payload)
        old_second = self.manifest()["shards"][1]["path"]
        monitor.save_json(self.path, {**self.payload, "items": self.payload["items"][:4]})
        live_paths = {shard["path"] for shard in self.manifest()["shards"]}
        self.assertEqual(len(live_paths), 2)
        self.assertNotIn(old_second, live_paths)
        self.assertEqual({p.relative_to(self.path.parent).as_posix() for p in self.shard_dir.iterdir()},
                         live_paths)
        small = {**self.payload, "items": self.payload["items"][:3]}
        monitor.save_json(self.path, small)
        self.assertEqual(self.path.read_bytes(), json_bytes(small))
        self.assertEqual(list(self.shard_dir.iterdir()), [])

    def test_invalid_manifest_fields(self):
        for field, value, message in (
            ("version", 2, "version"),
            ("count", 2, "data-001-[0-9a-f]{16}.*: count"),
            ("item_count", 9, "item_count"),
        ):
            with self.subTest(field=field):
                monitor.save_json(self.path, self.payload)
                manifest = self.manifest()
                target = manifest["shards"][0] if field == "count" else manifest
                target[field] = value
                self.path.write_bytes(json_bytes(manifest))
                with self.assertRaisesRegex(monitor.StateStructureError, message):
                    monitor.load_json(self.path, {})

    def test_tampered_shard(self):
        monitor.save_json(self.path, self.payload)
        self.first_shard.write_bytes(self.first_shard.read_bytes().replace(b'": 0', b'": 9', 1))
        with self.assertRaisesRegex(monitor.StateStructureError, "data-001-[0-9a-f]{16}.*: sha256"):
            monitor.load_json(self.path, {})

    def test_missing_shard(self):
        monitor.save_json(self.path, self.payload)
        self.first_shard.rename(self.shard_dir / "missing.json")
        with self.assertRaisesRegex(monitor.StateStructureError, "data-001-[0-9a-f]{16}.*:"):
            monitor.load_json(self.path, {})

    def test_shard_items_must_be_a_list(self):
        monitor.save_json(self.path, self.payload)
        raw = json_bytes({"items": {}})
        digest = hashlib.sha256(raw).hexdigest()
        relative_path = f"data-shards/data-001-{digest[:16]}.json"
        (self.path.parent / relative_path).write_bytes(raw)
        manifest = self.manifest()
        manifest["shards"][0].update(path=relative_path, bytes=len(raw), sha256=digest)
        self.path.write_bytes(json_bytes(manifest))
        with self.assertRaisesRegex(monitor.StateStructureError, "data-001-[0-9a-f]{16}.*: items"):
            monitor.load_json(self.path, {})

    def test_size_limit_checks_later_shards_before_writing_anything(self):
        monitor.save_json(self.path, self.payload)
        before = {p: p.read_bytes() for p in [self.path, *self.shard_dir.iterdir()]}
        revised = {**self.payload, "items": [{"title": "新"}] * 3 + [{"title": "大" * 100}]}
        oversized = len(json_bytes({"items": revised["items"][3:]}))
        with patch.object(monitor, "DATA_SHARD_MAX_BYTES", 200):
            with patch.object(monitor, "atomic_write_text", wraps=monitor.atomic_write_text) as write:
                with self.assertRaisesRegex(ValueError, f"002: {oversized} bytes"):
                    monitor.save_json(self.path, revised)
                write.assert_not_called()
        after = {p: p.read_bytes() for p in [self.path, *self.shard_dir.iterdir()]}
        self.assertEqual(after, before)

    def test_invalid_settings_do_not_change_existing_files(self):
        monitor.save_json(self.path, self.payload)
        before = {p: p.read_bytes() for p in [self.path, *self.shard_dir.iterdir()]}
        small = {**self.payload, "items": []}
        for name in ("DATA_SHARD_ITEMS", "DATA_SHARD_MAX_BYTES"):
            for value in (0, -1, True, 1.5, "3"):
                for payload in (self.payload, small):
                    with self.subTest(name=name, value=value, count=len(payload["items"])):
                        with patch.object(monitor, name, value):
                            with self.assertRaisesRegex(ValueError, f"{name}.*{value!r}"):
                                monitor.save_json(self.path, payload)
                        after = {p: p.read_bytes() for p in [self.path, *self.shard_dir.iterdir()]}
                        self.assertEqual(after, before)
        other = self.path.with_name("other.json")
        with patch.multiple(monitor, DATA_SHARD_ITEMS=0, DATA_SHARD_MAX_BYTES=-1):
            monitor.save_json(other, self.payload)
        self.assertEqual(other.read_bytes(), json_bytes(self.payload))

    def test_failed_manifest_write_preserves_previous_generation(self):
        monitor.save_json(self.path, self.payload)
        before = {p: p.read_bytes() for p in [self.path, *self.shard_dir.iterdir()]}
        revised = {**self.payload, "items": [{"title": "新", "code": 0}, *self.payload["items"][1:]]}
        atomic_write = monitor.atomic_write_text

        def fail_manifest(path, text):
            if path == self.path:
                raise OSError("manifest write failed")
            atomic_write(path, text)

        with patch.object(monitor, "atomic_write_text", side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, "manifest write failed"):
                monitor.save_json(self.path, revised)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assert_round_trip(self.path)

    def test_invalid_shard_descriptors(self):
        monitor.save_json(self.path, self.payload)
        original = self.path.read_bytes()
        for case, message in (
            ("duplicate", "duplicate shard path"),
            ("traversal", "invalid path"),
            ("absolute", "invalid path"),
            ("malformed", "invalid path"),
            ("gap", "expected shard number 1"),
            ("filename hash", "filename hash mismatch"),
            ("wrong bytes", "bytes mismatch"),
            ("negative bytes", "bytes mismatch"),
            ("boolean bytes", "bytes mismatch"),
        ):
            with self.subTest(case=case):
                manifest = json.loads(original)
                shards = manifest["shards"]
                first = shards[0]
                if case == "duplicate":
                    shards[1] = first.copy()
                elif case == "traversal":
                    first["path"] = "data-shards/../" + first["path"]
                elif case == "absolute":
                    first["path"] = str(self.path.parent / first["path"])
                elif case == "malformed":
                    first["path"] = first["path"].replace("data-001-", "data-01-")
                elif case == "gap":
                    del shards[0]
                elif case == "filename hash":
                    raw = (self.path.parent / first["path"]).read_bytes()
                    first["path"] = "data-shards/data-001-ffffffffffffffff.json"
                    (self.path.parent / first["path"]).write_bytes(raw)
                elif case == "wrong bytes":
                    first["bytes"] += 1
                elif case == "negative bytes":
                    first["bytes"] = -1
                else:
                    first["bytes"] = True
                self.path.write_bytes(json_bytes(manifest))
                with self.assertRaisesRegex(monitor.StateStructureError, message):
                    monitor.load_json(self.path, {})

    def test_compactor_builds_all_public_records_from_shards(self):
        payload = {
            **self.payload,
            "items": [{**item, "code": str(1234567 + i)}
                      for i, item in enumerate(self.payload["items"])],
        }
        monitor.save_json(self.path, payload)
        self.assertGreater(len(self.manifest()["shards"]), 1)
        root = self.path.parent
        catalog = root / "catalog.json"
        site_js = root / "site.js"
        site_js.write_text("// PUBLIC_CHUNK_LOADER_V1\n", encoding="utf-8")
        with patch.multiple(
            compact, DOCS=root, DATA=self.path, CATALOG=catalog,
            INDEX=root / "index.html", SITE_JS=site_js, BOOTSTRAP_ITEMS=3, CHUNK_ITEMS=3,
        ), redirect_stdout(io.StringIO()):
            compact.main()

        public = json.loads(catalog.read_bytes())
        rows = public["items"]
        for chunk in public["chunks"]:
            rows.extend(json.loads((root / chunk).read_bytes())["items"])
        self.assertEqual(public["item_count_total"], len(payload["items"]))
        self.assertEqual([row["code"] for row in rows], [item["code"] for item in payload["items"]])

    def test_validate_state_files_loads_sharded_data(self):
        monitor.save_json(self.path, self.payload)
        self.assertEqual(monitor.validate_state_files()[self.path], self.payload)


if __name__ == "__main__":
    unittest.main()
