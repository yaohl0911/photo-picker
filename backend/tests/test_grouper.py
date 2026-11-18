"""Logic-layer tests for app.grouper — DSH-safe, zero image decoding.

Rows are inserted directly into files/photos with hand-filled hash hex
columns, so what is under test is build_groups' pure logic: time-window and
BK-tree candidate generation, the threshold + voting-review similarity rule,
same-photo (RAW+JPG) exclusion, union-find merging, pending-group
dissolution, decided/not_duplicate protection, and dismiss_group. Real-image
hashing tests live in test_hashing.py; compute_hashes choreography tests in
test_hashing_logic.py.
"""

import random
import time

from app import grouper
from app.hashing import phash_distance

# Shared synthetic hash stand-ins (arbitrary 64-bit hex strings).
H0 = "0" * 16
AH0 = "a" * 16
DH0 = "d" * 16
T0 = "2026-01-01T10:00:00"


def hex_at(base_hex, dist):
    """Hex string at exactly `dist` hamming distance from base_hex."""
    v = int(base_hex, 16) ^ ((1 << dist) - 1)
    return f"{v:016x}"


def ins_file(db, path, phash, ahash, dhash, captured_at, photo_id=None, fmt="jpg"):
    """Insert a photo (or reuse photo_id) plus its file row with prefilled
    hash columns; return (photo_id, file_id). No commit needed: the same
    connection sees its own uncommitted writes."""
    if photo_id is None:
        cur = db.execute(
            "INSERT INTO photos (primary_file_id, group_id) VALUES (NULL, NULL)"
        )
        photo_id = cur.lastrowid
    cur = db.execute(
        "INSERT INTO files (path, mtime, size, file_hash, format, photo_id,"
        " captured_at, hash_phash, hash_ahash, hash_dhash)"
        " VALUES (?, 1000.0, 100, ?, ?, ?, ?, ?, ?, ?)",
        (str(path), "fh-" + str(path), fmt, photo_id, captured_at, phash, ahash, dhash),
    )
    file_id = cur.lastrowid
    db.execute(
        "UPDATE photos SET primary_file_id = ? WHERE id = ? AND primary_file_id IS NULL",
        (file_id, photo_id),
    )
    return photo_id, file_id


# --- candidate pairs & similarity rule --------------------------------------


def test_time_adjacent_similar_phash_forms_one_group(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:00:30")  # +30s

    res = grouper.build_groups(db)

    assert res == {"groups": 1, "pairs": 1}
    rows = db.execute("SELECT id, status FROM groups").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    gids = [r["group_id"] for r in db.execute("SELECT group_id FROM photos")]
    assert gids == [rows[0]["id"], rows[0]["id"]]


def test_time_adjacent_but_different_content_no_group(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", hex_at(H0, 40), AH0, DH0, "2026-01-01T10:00:30")

    res = grouper.build_groups(db)

    assert res == {"groups": 0, "pairs": 0}
    assert db.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"] == 0


def test_chain_transitive_merge_into_one_group(db):
    # d(A,B)=6 and d(B,C)=6 are both <= thr; d(A,C)=12 > thr, so A and C are
    # only linked transitively through B (triangle-inequality-legal chain).
    pa, _ = ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    pb, _ = ins_file(db, "dir/b.jpg", hex_at(H0, 6), AH0, DH0, "2026-01-01T10:00:10")
    pc, _ = ins_file(db, "dir/c.jpg", hex_at(H0, 12), AH0, DH0, "2026-01-01T10:00:20")
    assert phash_distance(H0, hex_at(H0, 6)) == 6
    assert phash_distance(hex_at(H0, 6), hex_at(H0, 12)) == 6
    assert phash_distance(H0, hex_at(H0, 12)) == 12

    res = grouper.build_groups(db)

    assert res["pairs"] == 2  # A-B and B-C only
    assert res["groups"] == 1
    assert db.execute(
        "SELECT COUNT(*) AS c FROM photos WHERE group_id IS NOT NULL"
    ).fetchone()["c"] == 3
    assert db.execute(
        "SELECT COUNT(DISTINCT group_id) AS c FROM photos WHERE group_id IS NOT NULL"
    ).fetchone()["c"] == 1


def test_cross_time_same_scene_found_by_bk_tree(db):
    # 10 minutes apart: far beyond the 60s time window; only the BK-tree
    # full retrieval can pair them.
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:10:00")

    res = grouper.build_groups(db)

    assert res == {"groups": 1, "pairs": 1}


def test_null_captured_at_pairs_only_via_bk_tree(db):
    # NULL captured_at is excluded from the time window but still indexed
    # in the BK-tree.
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, None)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, None)

    res = grouper.build_groups(db)

    assert res == {"groups": 1, "pairs": 1}


def test_boundary_band_requires_ahash_or_dhash_vote(db):
    # phash d=8 with thr=10 falls in the review band [6, 10); with ahash and
    # dhash both far away the vote fails and no pair forms.
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(
        db, "dir/b.jpg", hex_at(H0, 8), hex_at(AH0, 30), hex_at(DH0, 30),
        "2026-01-01T10:00:30",
    )

    res = grouper.build_groups(db)

    assert res == {"groups": 0, "pairs": 0}


def test_boundary_band_vote_passes_with_one_close_hash(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(
        db, "dir/b.jpg", hex_at(H0, 8), hex_at(AH0, 5), hex_at(DH0, 30),
        "2026-01-01T10:00:30",
    )

    res = grouper.build_groups(db)

    assert res == {"groups": 1, "pairs": 1}


def test_below_boundary_band_no_vote_needed(db):
    # phash d=5 < thr-4: no review even with far ahash/dhash.
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(
        db, "dir/b.jpg", hex_at(H0, 5), hex_at(AH0, 30), hex_at(DH0, 30),
        "2026-01-01T10:00:30",
    )

    res = grouper.build_groups(db)

    assert res == {"groups": 1, "pairs": 1}


def test_same_photo_raw_jpg_is_not_a_pair(db):
    pid, _ = ins_file(db, "dir/IMG_1.CR3", H0, AH0, DH0, T0, fmt="raw")
    ins_file(db, "dir/IMG_1.JPG", H0, AH0, DH0, T0, photo_id=pid)

    res = grouper.build_groups(db)

    assert res == {"groups": 0, "pairs": 0}
    row = db.execute("SELECT group_id FROM photos WHERE id = ?", (pid,)).fetchone()
    assert row["group_id"] is None


def test_single_photo_never_forms_group(db):
    ins_file(db, "dir/lonely.jpg", H0, AH0, DH0, T0)

    res = grouper.build_groups(db)

    assert res == {"groups": 0, "pairs": 0}
    assert db.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"] == 0


# --- protection, idempotency, dismissal --------------------------------------


def test_decided_group_and_photos_are_protected(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:00:30")
    assert grouper.build_groups(db)["groups"] == 1
    row = db.execute("SELECT id, status, created_at FROM groups").fetchone()
    gid, created = row["id"], row["created_at"]
    db.execute("UPDATE groups SET status = 'decided' WHERE id = ?", (gid,))
    db.commit()

    res = grouper.build_groups(db)

    # No new pending group for the decided photos...
    assert res == {"groups": 0, "pairs": 0}
    # ...and the original group row is untouched.
    rows = db.execute("SELECT id, status, created_at FROM groups").fetchall()
    assert [(r["id"], r["status"], r["created_at"]) for r in rows] == [
        (gid, "decided", created)
    ]
    gids = {r["group_id"] for r in db.execute("SELECT group_id FROM photos")}
    assert gids == {gid}


def test_build_groups_is_idempotent(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:00:30")

    res1 = grouper.build_groups(db)
    res2 = grouper.build_groups(db)

    assert res1 == res2 == {"groups": 1, "pairs": 1}
    assert db.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(DISTINCT group_id) AS c FROM photos WHERE group_id IS NOT NULL"
    ).fetchone()["c"] == 1


def test_dismiss_group_marks_not_duplicate_and_survives_rebuild(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:00:30")
    grouper.build_groups(db)
    gid = db.execute("SELECT id FROM groups").fetchone()["id"]

    grouper.dismiss_group(db, gid)

    row = db.execute("SELECT status FROM groups WHERE id = ?", (gid,)).fetchone()
    assert row["status"] == "not_duplicate"
    # photos keep their group_id (history stays queryable)
    gids = {r["group_id"] for r in db.execute("SELECT group_id FROM photos")}
    assert gids == {gid}
    # a rebuild must not resurrect it as pending nor regroup the photos
    res = grouper.build_groups(db)
    assert res == {"groups": 0, "pairs": 0}
    row = db.execute("SELECT status FROM groups WHERE id = ?", (gid,)).fetchone()
    assert row["status"] == "not_duplicate"


# --- BK-tree internals & performance ------------------------------------------


def test_bk_tree_query_matches_brute_force():
    rng = random.Random(7)
    hexes = [f"{rng.getrandbits(64):016x}" for _ in range(40)]
    # derived near-duplicates so the tree has real matches to find
    for i in range(10):
        hexes.append(hex_at(hexes[i], rng.randint(1, 12)))

    tree = grouper._bk_build(hexes)

    for q in hexes:
        for thr in (0, 4, 10):
            expected = sorted({h for h in hexes if phash_distance(q, h) <= thr})
            assert sorted(grouper._bk_query(tree, q, thr)) == expected


def test_bk_tree_500_files_under_one_second(db):
    rng = random.Random(42)
    for i in range(500):
        ins_file(
            db,
            f"dir/p{i:03d}.jpg",
            f"{rng.getrandbits(64):016x}",
            f"{rng.getrandbits(64):016x}",
            f"{rng.getrandbits(64):016x}",
            None,
        )

    t0 = time.time()
    res = grouper.build_groups(db)
    elapsed = time.time() - t0

    assert elapsed < 1.0
    assert res["groups"] == 0  # random 64-bit hashes never come out similar


def test_build_groups_reports_progress(db):
    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:00:30")
    calls = []

    grouper.build_groups(db, progress_cb=lambda *a, **kw: calls.append(kw if kw else dict(phase=a[0], done=a[1], total=a[2])))

    assert calls
    assert all(c["phase"] == "group" for c in calls)
    assert calls[-1]["done"] == calls[-1]["total"] == 2
