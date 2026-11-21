"""Auto-resolution of pixel-identical photos.

A group is auto-resolved when two or more of its photos are *identical*:
pHash, aHash AND dHash distances are all zero (the 256px grayscale
representations are bit-for-bit equal — e.g. a saved duplicate or a
cross-device re-encode of the very same photo). Near-duplicate bursts
(pHash distance 1-10) are NEVER auto-resolved: which burst frame to keep
is a quality judgement the user still makes (the recommender already
penalizes copy-suffix and hex-junk names there).

Mechanism — split into a fresh group, then decide it:
  1. find_identical_clusters() scans pending groups for identical photos
  2. each cluster with >1 photo is moved OUT of its original group into a
     brand-new pending group (the original group keeps any near-duplicate
     photos and stays pending for the user)
  3. executor.apply_decision(new_group, [keeper], learn=False,
     source='auto') closes the new group — file moves, undo, and history
     all reuse the existing group-level machinery untouched.

Keeper rule (deterministic, no learned weights — an auto rule must be
explainable): prefer no copy-suffix, then filename regularity (dated
names beat hex junk), then EXIF presence, then RAW, then quality score,
then the lower photo id.

Auto decisions never produce preference samples: a rule is not a taste
signal.
"""

import sqlite3
from datetime import datetime

from .executor import apply_decision
from .hashing import phash_distance
from .recommender import _filename_regularity, _is_copy_suffix


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hamming(hex_a: str, hex_b: str) -> int:
    if not hex_a or not hex_b:
        return 64
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def _identical(row_a, row_b) -> bool:
    """Triple-hash-zero test: pixel-identical at analysis resolution."""
    return (
        phash_distance(row_a["phash"], row_b["phash"]) == 0
        and _hamming(row_a["ahash"], row_b["ahash"]) == 0
        and _hamming(row_a["dhash"], row_b["dhash"]) == 0
    )


def find_identical_clusters(conn: sqlite3.Connection) -> dict:
    """Map {pending group_id: [cluster, ...]} of identical photos.

    Only clusters with at least two photos are returned; groups without
    one are omitted entirely. Decided / dismissed groups are skipped.
    """
    rows = conn.execute(
        """
        SELECT p.id AS pid, p.group_id AS gid, f.path AS path,
               f.hash_phash AS phash, f.hash_ahash AS ahash,
               f.hash_dhash AS dhash
        FROM photos p
        JOIN groups g ON g.id = p.group_id
        JOIN files f ON f.id = p.primary_file_id
        WHERE g.status = 'pending' AND f.hash_phash IS NOT NULL
        AND COALESCE(p.is_screenshot, 0) = 0
        ORDER BY p.group_id, p.id
        """
    ).fetchall()

    by_group: dict[int, list] = {}
    for row in rows:
        by_group.setdefault(row["gid"], []).append(row)

    result: dict[int, list] = {}
    for gid, photos in by_group.items():
        parent = {row["pid"]: row["pid"] for row in photos}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(photos)):
            for j in range(i + 1, len(photos)):
                if _identical(photos[i], photos[j]):
                    parent[find(photos[i]["pid"])] = find(photos[j]["pid"])
        clusters: dict[int, list] = {}
        for row in photos:
            clusters.setdefault(find(row["pid"]), []).append(row["pid"])
        real = sorted((sorted(c) for c in clusters.values() if len(c) > 1))
        if real:
            result[gid] = real
    return result


def pick_keeper(conn: sqlite3.Connection, photo_ids) -> int:
    """Deterministic keeper choice over identical photos. See module doc."""
    placeholders = ",".join("?" for _ in photo_ids)
    rows = conn.execute(
        f"""
        SELECT p.id AS pid, f.path AS path, f.format AS format,
               f.captured_at AS captured_at,
               q.sharpness AS sharpness, q.exposure AS exposure,
               q.noise AS noise, q.composition AS composition,
               q.faces AS faces
        FROM photos p
        JOIN files f ON f.id = p.primary_file_id
        LEFT JOIN quality_scores q ON q.photo_id = p.id
        WHERE p.id IN ({placeholders})
        """,
        tuple(photo_ids),
    ).fetchall()

    def avg_score(row):
        dims = [row[k] for k in
                ("sharpness", "exposure", "noise", "composition", "faces")]
        dims = [d for d in dims if d is not None]
        return sum(dims) / len(dims) if dims else -1.0

    def sort_key(row):
        import os
        name = os.path.basename(row["path"] or "")
        return (
            0 if _is_copy_suffix(name) else 1,          # original first
            _filename_regularity(name),                 # dated > junk
            1 if row["captured_at"] else 0,             # EXIF presence
            1 if row["format"] == "raw" else 0,         # RAW beats derived
            avg_score(row),                             # quality
            -row["pid"],                                # stable, lowest id
        )

    best = max(rows, key=sort_key)
    return best["pid"]


def auto_resolve_pending(conn: sqlite3.Connection, progress_cb=None,
                         enabled: bool = True) -> dict:
    """Auto-decide every pending group that contains identical photos.

    Returns {resolved_groups, moved_files, skipped}. ``progress_cb`` (if
    given) receives ("auto", done, total) across the groups processed.
    When ``enabled`` is False the scan is skipped and a zero report is
    returned (the caller reads the user's setting).
    """
    report = {"resolved_groups": 0, "moved_files": 0, "skipped": 0}
    if not enabled:
        return report

    clusters_by_group = find_identical_clusters(conn)
    total = len(clusters_by_group)

    touched_groups = []
    for done, (gid, clusters) in enumerate(sorted(clusters_by_group.items())):
        if progress_cb is not None:
            progress_cb("auto", done, total)
        touched_groups.append(gid)
        for cluster in clusters:
            # 1. split the cluster into its own fresh pending group
            new_gid = conn.execute(
                "INSERT INTO groups (status, created_at) VALUES ('pending', ?)",
                (_now_iso(),),
            ).lastrowid
            placeholders = ",".join("?" for _ in cluster)
            conn.execute(
                f"UPDATE photos SET group_id = ? WHERE id IN ({placeholders})",
                (new_gid, *cluster),
            )
            conn.commit()
            # 2. decide it: keep the rule-picked original, learn nothing
            keeper = pick_keeper(conn, cluster)
            decision_id = apply_decision(
                conn, new_gid, [keeper], learn=False, source="auto")
            # mirror the user-facing API contract: the kept photo becomes
            # the group's recommended photo so decided groups opened from
            # history show the actual outcome, not a fresh quality ranking
            conn.execute(
                "UPDATE groups SET recommended_photo_id = ? WHERE id = ?",
                (keeper, new_gid))
            moved = conn.execute(
                "SELECT COUNT(*) AS n FROM move_log WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()["n"]
            report["moved_files"] += moved
            report["resolved_groups"] += 1

    # dissolved leftover source groups: a pending group that only existed
    # to hold the identical cluster collapses to <=1 member after the
    # split — release the member and drop the empty shell (build_groups
    # would redo this on the next pipeline run anyway; doing it now keeps
    # the pending queue clean for immediate review)
    for gid in touched_groups:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM photos WHERE group_id = ?", (gid,)
        ).fetchone()["n"]
        if remaining <= 1:
            if remaining == 1:
                conn.execute(
                    "UPDATE photos SET group_id = NULL WHERE group_id = ?",
                    (gid,))
            conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
            report["skipped"] += 1
    conn.commit()

    if progress_cb is not None:
        progress_cb("auto", total, total)
    return report
