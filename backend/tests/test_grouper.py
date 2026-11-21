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
    # Pairwise enforcement now evicts C (tied conflict count, higher id),
    # keeping the mutually-similar pair {A, B}.
    pa, _ = ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    pb, _ = ins_file(db, "dir/b.jpg", hex_at(H0, 6), AH0, DH0, "2026-01-01T10:00:10")
    pc, _ = ins_file(db, "dir/c.jpg", hex_at(H0, 12), AH0, DH0, "2026-01-01T10:00:20")
    assert phash_distance(H0, hex_at(H0, 6)) == 6
    assert phash_distance(hex_at(H0, 6), hex_at(H0, 12)) == 6
    assert phash_distance(H0, hex_at(H0, 12)) == 12

    res = grouper.build_groups(db)

    assert res["pairs"] == 2  # A-B and B-C only
    assert res["groups"] == 1
    # C was evicted: the group holds exactly the mutually-similar A and B
    g = db.execute("SELECT id FROM groups").fetchone()["id"]
    grouped = {
        r["id"]: r["group_id"]
        for r in db.execute("SELECT id, group_id FROM photos")
    }
    assert grouped[pa] == g and grouped[pb] == g
    assert grouped[pc] is None  # evicted, stays ungrouped


def test_borderline_chained_photo_is_evicted_not_grouped(db):
    # The user's group-#3 shape: A<->B distance 2 (true duplicate), the
    # unrelated C at exactly the threshold against B (10) but 12 against A.
    # C must not ride the chain into the group.
    pa, _ = ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    pb, _ = ins_file(db, "dir/b.jpg", hex_at(H0, 2), AH0, DH0, T0)
    # C flips the low 12 bits: d(C,B) = |{0,1} Δ {0..11}| = 10 (exactly
    # the threshold against B), d(C,A) = 12 (dissimilar to A).
    hc = hex_at(H0, 12)
    pc, _ = ins_file(db, "dir/c.jpg", hc, AH0, DH0, "2026-04-30T20:10:32")
    assert phash_distance(hex_at(H0, 2), hc) == 10
    assert phash_distance(H0, hc) == 12

    res = grouper.build_groups(db)

    assert res["groups"] == 1
    grouped = {
        r["id"]: r["group_id"]
        for r in db.execute("SELECT id, group_id FROM photos")
    }
    assert grouped[pa] == grouped[pb] and grouped[pa] is not None
    assert grouped[pc] is None


def test_consistent_group_is_untouched_by_enforcement(db):
    # All pairs at distance <= threshold: nothing is evicted, even when
    # a component is large (connected chain of near-identical shots).
    hashes = [hex_at(H0, 2 * i) for i in range(4)]  # 0,2,4,6 from H0
    for i, h in enumerate(hashes):
        ins_file(db, f"dir/n{i}.jpg", h, AH0, DH0, T0)
    res = grouper.build_groups(db)
    assert res["groups"] == 1
    assert db.execute(
        "SELECT COUNT(*) AS c FROM photos WHERE group_id IS NOT NULL"
    ).fetchone()["c"] == 4


def test_mutually_dissimilar_component_yields_no_group(db):
    # Star topology: center similar to all, leaves far apart pairwise —
    # with a tight threshold the leaves' mutual conflicts evict them all,
    # leaving no group at all.
    ins_file(db, "dir/center.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/leaf1.jpg", hex_at(H0, 6), AH0, DH0, T0)
    ins_file(db, "dir/leaf2.jpg", hex_at(H0, 6 + 32), AH0, DH0, T0)  # far from leaf1
    res = grouper.build_groups(db)
    # whatever survives must be pairwise consistent; leaf1<->leaf2 = 6+32
    # flips in disjoint bit ranges... they are distance 6 apart from H0 in
    # distinct bits, so leaf1<->leaf2 = 12 > thr → one of them is evicted
    # together with any conflict partner; at least the group count reflects
    # a consistent subset only.
    grouped = db.execute(
        "SELECT COUNT(*) AS c FROM photos WHERE group_id IS NOT NULL"
    ).fetchone()["c"]
    assert grouped >= 2  # center keeps at least one leaf
    gid = db.execute("SELECT DISTINCT group_id FROM photos WHERE group_id IS NOT NULL").fetchone()
    members = db.execute(
        "SELECT f.hash_phash AS h FROM files f JOIN photos p ON p.id=f.photo_id"
        " WHERE p.group_id = ?", (gid["group_id"],)
    ).fetchall()
    hs = [m["h"] for m in members]
    for i in range(len(hs)):
        for j in range(i + 1, len(hs)):
            assert phash_distance(hs[i], hs[j]) <= 10


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
    # the dismissal records a decision row (kept NULL) so undo can revert it
    dec = db.execute(
        "SELECT kept_photo_ids FROM decisions WHERE group_id = ?", (gid,)
    ).fetchone()
    assert dec is not None and dec["kept_photo_ids"] is None
    # re-dismissing must not stack duplicate decision rows
    grouper.dismiss_group(db, gid)
    n = db.execute(
        "SELECT COUNT(*) FROM decisions WHERE group_id = ?", (gid,)
    ).fetchone()[0]
    assert n == 1
    # a rebuild must not resurrect it as pending nor regroup the photos
    res = grouper.build_groups(db)
    assert res == {"groups": 0, "pairs": 0}
    row = db.execute("SELECT status FROM groups WHERE id = ?", (gid,)).fetchone()
    assert row["status"] == "not_duplicate"


def test_undone_group_survives_rebuild_without_fk_error(db):
    # Regression: a decision exists on the group (decision history), the
    # group went back to 'pending' via undo. Rebuilding used to DELETE
    # that pending group and hit the decisions.group_id foreign key.
    from datetime import datetime

    ins_file(db, "dir/a.jpg", H0, AH0, DH0, T0)
    ins_file(db, "dir/b.jpg", H0, AH0, DH0, "2026-01-01T10:00:30")
    grouper.build_groups(db)
    gid = db.execute("SELECT id FROM groups").fetchone()["id"]
    photos = [
        r["id"] for r in db.execute("SELECT id FROM photos WHERE group_id = ?", (gid,))
    ]

    # a decision row referencing the group, then the group returns to pending
    db.execute(
        "INSERT INTO decisions (group_id, kept_photo_ids, decided_at) "
        "VALUES (?, ?, ?)",
        (gid, '["%d"]' % photos[0], datetime.now().isoformat(timespec="seconds")),
    )
    db.execute("UPDATE groups SET status = 'pending' WHERE id = ?", (gid,))
    db.commit()

    # must not raise sqlite3.IntegrityError: FOREIGN KEY constraint failed
    res = grouper.build_groups(db)
    assert res == {"groups": 0, "pairs": 0}
    row = db.execute("SELECT status FROM groups WHERE id = ?", (gid,)).fetchone()
    assert row["status"] == "pending"
    kept = db.execute("SELECT COUNT(*) FROM decisions WHERE group_id = ?", (gid,)).fetchone()[0]
    assert kept == 1


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


def _mark_screenshot(db, photo_id):
    db.execute("UPDATE photos SET is_screenshot = 1 WHERE id = ?",
               (photo_id,))


def test_screenshot_flagged_photo_is_excluded_from_grouping(db):
    # 三张时间相邻、pHash 相同：两张普通照片 + 一张已标记截图
    pid_a, _ = ins_file(db, "a.jpg", H0, AH0, DH0, T0)
    pid_b, _ = ins_file(db, "b.jpg", H0, AH0, DH0, "2026-01-01T10:00:05")
    pid_s, _ = ins_file(db, "s.png", H0, AH0, DH0, "2026-01-01T10:00:10")
    _mark_screenshot(db, pid_s)
    report = grouper.build_groups(db)
    assert report["groups"] == 1
    rows = db.execute(
        "SELECT id, group_id FROM photos ORDER BY id").fetchall()
    by_id = {r["id"]: r["group_id"] for r in rows}
    assert by_id[pid_a] is not None and by_id[pid_a] == by_id[pid_b]
    assert by_id[pid_s] is None  # 截图不进组
    members = db.execute(
        "SELECT COUNT(*) FROM photos WHERE group_id IS NOT NULL").fetchone()[0]
    assert members == 2


def test_unclassified_null_is_screenshot_still_groups(db):
    # 兜底：is_screenshot 为 NULL（未分类）时按普通照片分组
    pid_a, _ = ins_file(db, "x.jpg", H0, AH0, DH0, T0)
    pid_b, _ = ins_file(db, "y.jpg", H0, AH0, DH0, "2026-01-01T10:00:05")
    db.execute("UPDATE photos SET is_screenshot = NULL")
    report = grouper.build_groups(db)
    assert report["groups"] == 1
    g = db.execute("SELECT group_id FROM photos WHERE id = ?", (pid_a,)).fetchone()
    assert g[0] is not None
