"""Duplicate-photo grouping: candidate pairs, similarity review, union-find.

Pure stdlib at module top (DSH-safe import, docs/DEVENV.md); distance
arithmetic is app.hashing.phash_distance (also pure Python).

Pipeline per docs/superpowers/specs/2026-11-18-photo-dedup-design.md §5:

1. Dissolve every pending group (its photos' group_id reset to NULL);
   groups already decided or dismissed as not_duplicate — and the photos
   inside them — are never touched again (user decisions are respected).
2. Candidate pairs from two independent sources:
   - time window: files sorted by captured_at (NULLs excluded), every
     pair closer than ``time_window_sec``;
   - BK-tree: a tree over every phash, queried within
     ``phash_threshold``, so same-scene shots far apart in time (e.g.
     tripod re-takes) are still found.
3. Similarity decision on each candidate pair: phash hamming distance
   <= threshold, and inside the boundary band [threshold-4, threshold) at
   least one of ahash/dhash distance <= threshold as well (voting
   review). Two files of the same photo (RAW + JPG) never count as a
   similar pair.
4. Union-find merges the photo ids of similar pairs; every component
   with >= 2 photos becomes a new groups row (status 'pending').
"""

import sqlite3
from datetime import datetime

from app.config import get_settings
from app.hashing import phash_distance


class _BKNode:
    """BK-tree node: a phash hex value plus children keyed by hamming distance."""

    __slots__ = ("value", "children")

    def __init__(self, value):
        self.value = value
        self.children = {}  # {distance from self.value: _BKNode}


def _bk_build(hexes):
    """Build a BK-tree from hex strings (duplicate values collapse).

    Returns the root _BKNode, or None when the input is empty.
    """
    root = None
    for hx in hexes:
        if root is None:
            root = _BKNode(hx)
            continue
        node = root
        while True:
            dist = phash_distance(node.value, hx)
            if dist == 0:
                break  # value already present in the tree
            child = node.children.get(dist)
            if child is None:
                node.children[dist] = _BKNode(hx)
                break
            node = child
    return root


def _bk_query(root, hx, threshold):
    """Return every value in the tree within `threshold` hamming distance of hx.

    Standard BK-tree pruning: a child at tree-distance ``edge`` from the
    current node can only hold matches when ``|edge - d| <= threshold``
    (triangle inequality on hamming distance), so every other branch is
    skipped.
    """
    results = []
    if root is None:
        return results
    stack = [root]
    while stack:
        node = stack.pop()
        dist = phash_distance(node.value, hx)
        if dist <= threshold:
            results.append(node.value)
        lo, hi = dist - threshold, dist + threshold
        for edge, child in node.children.items():
            if lo <= edge <= hi:
                stack.append(child)
    return results


class _UnionFind:
    """Photo-id union-find with path compression."""

    def __init__(self):
        self._parent = {}

    def find(self, x):
        parent = self._parent
        root = x
        while root in parent:
            root = parent[root]
        while x in parent:  # compress the walked path
            parent[x], x = root, parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _is_similar(fa, fb, threshold):
    """Candidate decision rule.

    phash distance <= threshold; when it falls in the boundary band
    [threshold-4, threshold) at least one of ahash/dhash must also be
    within the threshold (voting review). phash_distance returns 64 for
    missing hashes, which fails the <= threshold test on its own.
    """
    dist = phash_distance(fa["hash_phash"], fb["hash_phash"])
    if dist > threshold:
        return False
    if threshold - 4 <= dist < threshold:
        a = phash_distance(fa["hash_ahash"], fb["hash_ahash"])
        d = phash_distance(fa["hash_dhash"], fb["hash_dhash"])
        return a <= threshold or d <= threshold
    return True


def _dissolve_pending_groups(conn: sqlite3.Connection) -> None:
    """Delete pending groups and detach their photos.

    Decided and not_duplicate groups — and the photos inside them — are
    left untouched so re-running never overrides a user decision.
    A pending group that still has decision history (the user UNDID a
    decision and the group went back to pending to be re-decided) is
    kept as well: deleting it would break the decisions.group_id
    foreign key, and undoing means the user wants THIS group back, not
    a re-clustered one.
    """
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM groups WHERE status = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.group_id = groups.id)"
        )
    ]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    # Detach photos before deleting: photos.group_id references groups.id.
    conn.execute(f"UPDATE photos SET group_id = NULL WHERE group_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM groups WHERE id IN ({marks})", ids)
    conn.commit()


def _collect_time_pairs(files, window_sec, pairs):
    """Add (min_id, max_id) tuples for every pair captured within window_sec.

    Files with NULL or unparseable captured_at do not participate in the
    time window (the BK-tree still covers them).
    """
    timed = []
    for f in files:
        raw = f["captured_at"]
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        timed.append((ts, f))
    timed.sort(key=lambda item: item[0])
    for i in range(len(timed)):
        t_i, f_i = timed[i]
        for j in range(i + 1, len(timed)):
            t_j, f_j = timed[j]
            if (t_j - t_i).total_seconds() >= window_sec:
                break  # sorted: every later j is even farther away
            a, b = f_i["id"], f_j["id"]
            pairs.add((a, b) if a < b else (b, a))


def _collect_bk_pairs(files, threshold, progress_cb, pairs):
    """Add (min_id, max_id) tuples for every pair of files whose phashes are
    within `threshold` hamming distance, retrieved via a BK-tree over all
    phashes. Reports progress once per queried file."""
    files_by_hex = {}
    for f in files:
        files_by_hex.setdefault(f["hash_phash"], []).append(f["id"])
    root = _bk_build(files_by_hex)
    total = len(files)
    if progress_cb is not None:
        # fire once even with zero hashed files so the UI shows the stage
        progress_cb("group", 0, total)
    for done, f in enumerate(files, start=1):
        for hx in _bk_query(root, f["hash_phash"], threshold):
            for other in files_by_hex[hx]:
                if other != f["id"]:
                    a, b = f["id"], other
                    pairs.add((a, b) if a < b else (b, a))
        if progress_cb is not None:
            progress_cb("group", done, total)


def build_groups(conn: sqlite3.Connection, progress_cb=None) -> dict:
    """(Re)build all pending duplicate groups.

    Returns ``{"groups": n, "pairs": n}`` where ``groups`` counts the
    pending groups created in this run and ``pairs`` counts the validated
    similar file pairs that fed union-find. progress_cb, when given, is
    called as ``progress_cb("group", done, total)`` once
    per candidate file during the BK-tree scan.

    Idempotent: pending groups are dissolved and rebuilt from scratch;
    decided / not_duplicate groups and their photos are preserved as-is.
    """
    settings = get_settings(conn)
    threshold = settings["phash_threshold"]
    window_sec = settings["time_window_sec"]

    _dissolve_pending_groups(conn)

    # Candidate files: hashed, attributed to a photo, and that photo not
    # locked into a decided/not_duplicate group (those have group_id set).
    rows = conn.execute(
        "SELECT f.id, f.photo_id, f.hash_phash, f.hash_ahash, f.hash_dhash,"
        " f.captured_at"
        " FROM files f JOIN photos p ON p.id = f.photo_id"
        " WHERE p.group_id IS NULL AND f.hash_phash IS NOT NULL"
        " AND f.hash_phash != ''"
    ).fetchall()
    files = [dict(row) for row in rows]

    candidate_pairs = set()
    _collect_time_pairs(files, window_sec, candidate_pairs)
    _collect_bk_pairs(files, threshold, progress_cb, candidate_pairs)

    by_id = {f["id"]: f for f in files}
    uf = _UnionFind()
    similar_pairs = 0
    for fa_id, fb_id in sorted(candidate_pairs):
        fa, fb = by_id[fa_id], by_id[fb_id]
        if fa["photo_id"] == fb["photo_id"]:
            continue  # RAW+JPG of one photo: not a duplicate pair
        if not _is_similar(fa, fb, threshold):
            continue
        similar_pairs += 1
        uf.union(fa["photo_id"], fb["photo_id"])

    # Components with >= 2 photos become pending groups.
    components = {}
    for f in files:
        root = uf.find(f["photo_id"])
        components.setdefault(root, set()).add(f["photo_id"])
    created = 0
    now = datetime.now().isoformat()
    for members in components.values():
        if len(members) < 2:
            continue
        cur = conn.execute(
            "INSERT INTO groups (status, recommended_photo_id, created_at)"
            " VALUES ('pending', NULL, ?)",
            (now,),
        )
        group_id = cur.lastrowid
        conn.executemany(
            "UPDATE photos SET group_id = ? WHERE id = ?",
            [(group_id, pid) for pid in sorted(members)],
        )
        created += 1
    conn.commit()
    return {"groups": created, "pairs": similar_pairs}


def dismiss_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Mark a group as not_duplicate.

    The group row and its photos' group_id are kept — the dismissal stays
    queryable as history and protects the photos from being regrouped by
    later build_groups runs. A decision row with kept_photo_ids NULL is
    recorded (idempotently) so the dismissal is undoable like any other
    decision: undo_group flips the group back to pending.
    """
    status = conn.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    if status is None:
        return
    if status["status"] != "not_duplicate":
        conn.execute(
            "UPDATE groups SET status = 'not_duplicate' WHERE id = ?", (group_id,)
        )
        from datetime import datetime

        conn.execute(
            "INSERT INTO decisions (group_id, kept_photo_ids, decided_at)"
            " VALUES (?, NULL, ?)",
            (group_id, datetime.now().isoformat(timespec="seconds")),
        )
    conn.commit()
