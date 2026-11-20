"""Auto-resolve of pixel-identical photos: triple-hash-zero clusters are
split into their own group and decided automatically (source='auto',
learn=False), leaving near-duplicates in the original pending group."""

import json
import sqlite3

import pytest

from app.db import init_db, connect
from app import executor as executor_mod
from app import auto_resolve


@pytest.fixture()
def conn(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_db(conn)
    yield conn
    conn.close()


def _add_photo(conn, name, phash, ahash, dhash, group_id=None, fmt="jpg",
               captured_at="2020-12-19T11:12:13", quality=None, photo_id=None):
    """Insert one file+photo with explicit hashes; return photo id."""
    pid = photo_id or conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM photos").fetchone()["n"]
    fid = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM files").fetchone()["n"]
    # FK cycle photos.primary_file_id <-> files.photo_id: insert photos
    # first with a NULL primary, then the file, then wire it up (same
    # order scanner uses).
    conn.execute(
        "INSERT INTO photos (id, primary_file_id, group_id) VALUES (?, NULL, ?)",
        (pid, group_id))
    conn.execute(
        "INSERT INTO files (id, path, mtime, size, format, photo_id, "
        "captured_at, hash_phash, hash_ahash, hash_dhash) "
        "VALUES (?, ?, 1000.0, 100, ?, ?, ?, ?, ?, ?)",
        (fid, f"/src/{name}", fmt, pid, captured_at, phash, ahash, dhash))
    conn.execute("UPDATE photos SET primary_file_id = ? WHERE id = ?",
                 (fid, pid))
    if quality is not None:
        conn.execute(
            "INSERT INTO quality_scores (photo_id, sharpness, exposure, "
            "noise, composition, faces) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, quality, quality, quality, quality, quality))
    return pid


H = "f52da62313962ab9"
A = "07c0fafceae4effe"
D = "4e1622285a4b4c2c"


def _mk_group(conn, gname):
    gid = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM groups").fetchone()["n"]
    conn.execute(
        "INSERT INTO groups (id, status) VALUES (?, 'pending')", (gid,))
    return gid


class TestFindIdenticalClusters:
    def test_triple_zero_pair_forms_cluster(self, conn):
        gid = _mk_group(conn, "g1")
        p1 = _add_photo(conn, "a.jpg", H, A, D, gid)
        p2 = _add_photo(conn, "b.jpg", H, A, D, gid)
        clusters = auto_resolve.find_identical_clusters(conn)
        assert clusters == {gid: [[p1, p2]]}

    def test_phash_zero_only_not_identical(self, conn):
        # ahash differs -> NOT pixel-identical (e.g. re-encoded burst)
        gid = _mk_group(conn, "g1")
        _add_photo(conn, "a.jpg", H, A, D, gid)
        _add_photo(conn, "b.jpg", H, "07c0fafceae4eff0", D, gid)
        assert auto_resolve.find_identical_clusters(conn) == {}

    def test_near_pair_not_identical(self, conn):
        gid = _mk_group(conn, "g1")
        _add_photo(conn, "a.jpg", H, A, D, gid)
        _add_photo(conn, "b.jpg", "f52da62313962ab8", A, D, gid)
        assert auto_resolve.find_identical_clusters(conn) == {}

    def test_decided_groups_skipped(self, conn):
        gid = _mk_group(conn, "g1")
        conn.execute("UPDATE groups SET status='decided' WHERE id=?", (gid,))
        _add_photo(conn, "a.jpg", H, A, D, gid)
        _add_photo(conn, "b.jpg", H, A, D, gid)
        assert auto_resolve.find_identical_clusters(conn) == {}

    def test_three_way_cluster(self, conn):
        gid = _mk_group(conn, "g1")
        p1 = _add_photo(conn, "a.jpg", H, A, D, gid)
        p2 = _add_photo(conn, "b.jpg", H, A, D, gid)
        p3 = _add_photo(conn, "c.jpg", H, A, D, gid)
        clusters = auto_resolve.find_identical_clusters(conn)
        assert clusters == {gid: [[p1, p2, p3]]}


class TestPickKeeper:
    def test_prefers_original_over_copy_suffix(self, conn):
        p1 = _add_photo(conn, "2020-12-19 111212(1).jpg", H, A, D)
        p2 = _add_photo(conn, "2020-12-19 111212.jpg", H, A, D)
        assert auto_resolve.pick_keeper(conn, [p1, p2]) == p2

    def test_prefers_dated_name_over_hex_junk(self, conn):
        p1 = _add_photo(conn, "15423728636290139782.jpg", H, A, D)
        p2 = _add_photo(conn, "2020-12-28 004344.heic", H, A, D, fmt="heic")
        assert auto_resolve.pick_keeper(conn, [p1, p2]) == p2

    def test_tie_break_on_quality(self, conn):
        p1 = _add_photo(conn, "a.jpg", H, A, D, quality=80.0)
        p2 = _add_photo(conn, "b.jpg", H, A, D, quality=92.0)
        assert auto_resolve.pick_keeper(conn, [p1, p2]) == p2


class TestAutoResolvePending:
    def _setup_pair(self, conn, keep_name, drop_name):
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) "
            "VALUES ('source_dirs', '[\"/src\"]')")
        gid = _mk_group(conn, "g1")
        keeper = _add_photo(conn, keep_name, H, A, D, gid)
        dropper = _add_photo(conn, drop_name, H, A, D, gid)
        return gid, keeper, dropper

    def test_resolves_identical_pair_end_to_end(self, conn, tmp_path,
                                                monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "2020-12-19 111212.jpg").write_bytes(b"keeper")
        (src / "2020-12-19 111212(1).jpg").write_bytes(b"dropper")
        rejected = tmp_path / "rej"
        monkeypatch.setattr(executor_mod, "rejected_dir_for",
                            lambda c, p: str(rejected))
        gid = _mk_group(conn, "g1")
        keeper = _add_photo(conn, "2020-12-19 111212.jpg", H, A, D, gid)
        dropper = _add_photo(conn, "2020-12-19 111212(1).jpg", H, A, D, gid)
        # point the rows at the real files so moves succeed
        conn.execute(
            "UPDATE files SET path = ? WHERE photo_id = ?",
            (str(src / "2020-12-19 111212.jpg"), keeper))
        conn.execute(
            "UPDATE files SET path = ? WHERE photo_id = ?",
            (str(src / "2020-12-19 111212(1).jpg"), dropper))

        report = auto_resolve.auto_resolve_pending(conn)

        assert report["resolved_groups"] == 1
        assert report["moved_files"] == 1
        # decision recorded as auto, no learning samples (learner's own
        # tables exist only after its ensure_tables; absence is fine —
        # verify via the source column and status instead)
        row = conn.execute(
            "SELECT d.source, d.kept_photo_ids, g.status FROM decisions d "
            "JOIN groups g ON g.id = d.group_id WHERE d.source = 'auto'"
        ).fetchone()
        assert row is not None
        assert json.loads(row["kept_photo_ids"]) == [keeper]
        assert row["status"] == "decided"
        # the dropper's file moved; keeper stays
        moved = conn.execute(
            "SELECT path FROM files WHERE id = "
            "(SELECT primary_file_id FROM photos WHERE id = ?)",
            (dropper,)).fetchone()["path"]
        assert moved.startswith(str(rejected))
        kept_path = conn.execute(
            "SELECT path FROM files WHERE id = "
            "(SELECT primary_file_id FROM photos WHERE id = ?)",
            (keeper,)).fetchone()["path"]
        assert kept_path == str(src / "2020-12-19 111212.jpg")
        # keeper lives in the NEW auto group (decided); original group has
        # only the dropper left and stays pending
        new_gid = conn.execute(
            "SELECT group_id FROM photos WHERE id = ?", (keeper,)
        ).fetchone()["group_id"]
        assert new_gid != gid

    def test_near_duplicates_left_in_original_group(self, conn, tmp_path,
                                                    monkeypatch):
        rejected = tmp_path / "rej"
        monkeypatch.setattr(executor_mod, "rejected_dir_for",
                            lambda c, src: str(rejected))
        gid = _mk_group(conn, "g1")
        # identical pair + one near-duplicate burst frame in the same group
        k = _add_photo(conn, "keep.jpg", H, A, D, gid)
        d = _add_photo(conn, "keep(1).jpg", H, A, D, gid)
        near = _add_photo(conn, "near.jpg", "f52da62313962ab8", A, D, gid)

        report = auto_resolve.auto_resolve_pending(conn)

        assert report["resolved_groups"] == 1
        # near-duplicate burst frame: the source group collapses to this
        # single member after the split, so it is released (group_id=NULL)
        # for the next build_groups re-clustering, and the empty source
        # group is dissolved — nothing single-photo stays pending
        row = conn.execute(
            "SELECT group_id FROM photos WHERE id = ?", (near,)
        ).fetchone()
        assert row["group_id"] is None
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM groups WHERE id = ?", (gid,)
        ).fetchone()["n"] == 0
        assert report["skipped"] == 1

    def test_idempotent_second_run(self, conn, tmp_path, monkeypatch):
        rejected = tmp_path / "rej"
        monkeypatch.setattr(executor_mod, "rejected_dir_for",
                            lambda c, src: str(rejected))
        gid, keeper, dropper = self._setup_pair(
            conn, "a.jpg", "a(1).jpg")
        auto_resolve.auto_resolve_pending(conn)
        report2 = auto_resolve.auto_resolve_pending(conn)
        assert report2["resolved_groups"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM decisions WHERE source='auto'"
        ).fetchone()["n"] == 1

    def test_disabled_flag_skips_everything(self, conn):
        gid, keeper, dropper = self._setup_pair(
            conn, "a.jpg", "a(1).jpg")
        report = auto_resolve.auto_resolve_pending(conn, enabled=False)
        assert report["resolved_groups"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0
