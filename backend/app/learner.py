"""Pairwise preference learner — learns which photo qualities the user favors.

The learner turns "the user kept photo A and rejected photo B" decisions into
pairwise training samples and fits a Bradley-Terry-style logistic model

    P(kept ≻ rejected) = sigmoid(w · Δx/100 + b)

where Δx = x_kept − x_rejected over the five quality dimensions of
app.recommender.DIMENSIONS (raw score deltas, typically ±40 points).

Implementation notes / conventions
-----------------------------------
* Pure stdlib (json, math, sqlite3, datetime) — scipy is unavailable in DSH
  sessions, so gradient descent is hand-rolled full-batch GD with
  backtracking (learning rate halved whenever a step raises the loss).
* Scale convention: training uses Δx/100 internally, and the stored
  preference_model weights are on that per-100-point scale. A weight of 1.0
  therefore means "one logit-odds step per 100 points of difference", the
  same order of magnitude as app.recommender.DEFAULT_WEIGHTS so both can be
  shown on the same 0–2 preferences panel. Sample x_json columns store the
  RAW deltas.
* EMA toward the old model: w_final = (1−β)·w_old + β·w_trained with
  β = n/(n+50) where n is the number of samples (the NEW-model share: few
  samples stay close to the old model, hundreds of samples track the new
  one). bias always uses the trained value directly. With no prior model
  the trained weights are stored as-is.
* Locked dimensions (settings key 'locked_dims'): their gradient is zeroed,
  so the weight stays exactly at its starting (current-model) value through
  training, EMA, and clamping.
* Weight clamp per dimension: [-3.0, 5.0].
* Regularization: λ = 0.1·√n on ||w||² only (bias unregularized).
* GD hyper-parameters: lr starts at 0.05, at most 500 iterations, early
  stop when the full gradient norm < 1e-6.

Public API
----------
- ensure_tables(conn)
- record_decision_samples(conn, group_id, kept_ids, decision_id=None) -> int
- retrain(conn) -> dict
- get_current_weights(conn) -> dict
- set_locked(conn, dims) -> None
"""

import json
import math
from datetime import datetime

from app.recommender import DIMENSIONS, DEFAULT_WEIGHTS

#: Settings key holding the JSON list of locked dimensions.
LOCKED_SETTINGS_KEY = "locked_dims"

#: Score substituted for a missing dimension when building feature deltas.
NEUTRAL_SCORE = 50.0

#: Feature scale: training uses Δx/FEATURE_SCALE so weights land near 1.0.
FEATURE_SCALE = 100.0

#: Stored-weight bounds (per-100-point scale; keeps values displayable).
WEIGHT_MIN = -3.0
WEIGHT_MAX = 5.0

#: Sample weight applied to feedback-amplified pairs (dislike / like).
FEEDBACK_WEIGHT = 3.0

#: GD hyper-parameters.
LEARNING_RATE = 0.05
MAX_ITERATIONS = 500
GRADIENT_TOL = 1e-6
#: EMA half-life denominator: beta = n / (n + EMA_SMOOTHING).
EMA_SMOOTHING = 50.0

#: L2 regularization: lambda = L2_BASE * sqrt(n).
L2_BASE = 0.1

#: Sigmoid argument clamp to avoid math.exp overflow.
_SIGMOID_CLAMP = 30.0

#: EMA exponent denominator: old weight share is (1 - n/(n + EMA_SMOOTHING)).


_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS preference_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER,
    group_id INTEGER,
    x_json TEXT,
    label INTEGER,
    weight REAL,
    created_at TEXT
)
"""


def ensure_tables(conn):
    """Create preference_samples if missing (learner-owned table).

    Called at the top of every public function; app.db.init_db deliberately
    does not know about this table.
    """
    conn.execute(_SAMPLES_DDL)
    conn.commit()


def record_decision_samples(conn, group_id, kept_ids, decision_id=None):
    """Record pairwise preference samples for one decision.

    The group's photos are split into an effective kept side and an
    effective rejected side:

    * photos listed in kept_ids are kept;
    * photos with a 'like' feedback event in this group are treated as kept;
    * photos with a 'dislike' feedback event are treated as rejected even if
      they appear in kept_ids (dislike wins when both events exist);
    * all remaining group members are rejected.

    Every (kept, rejected) pair produces two rows: Δx = kept − rejected with
    label 1, and its negation with label 0. A pair whose kept side is liked
    or whose rejected side is disliked gets sample weight 3.0 (feedback
    triples the pair's influence); other pairs get 1.0.

    Groups with status 'not_duplicate' are not duplicate decisions, so they
    contribute nothing and 0 is returned (decided groups DO record — the
    executor marks status before calling). Empty kept or rejected side means
    no comparable pairs and 0 rows.

    Returns the number of rows inserted.
    """
    ensure_tables(conn)
    group = conn.execute(
        "SELECT status FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    if group is None:
        raise KeyError(f"group {group_id} does not exist")
    if group["status"] == "not_duplicate":
        return 0

    members = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM photos WHERE group_id = ? ORDER BY id", (group_id,)
        )
    ]
    keep_set = set(kept_ids)
    liked, disliked = _group_feedback(conn, group_id)
    kept_effective = [
        m for m in members
        if m not in disliked and (m in keep_set or m in liked)
    ]
    kept_set = set(kept_effective)
    rejected_effective = [m for m in members if m not in kept_set]

    if not kept_effective or not rejected_effective:
        return 0

    now = datetime.now().isoformat()
    rows = []
    for kept_id in kept_effective:
        for rejected_id in rejected_effective:
            weight = (
                FEEDBACK_WEIGHT
                if (rejected_id in disliked or kept_id in liked)
                else 1.0
            )
            delta = _feature_delta(conn, kept_id, rejected_id)
            rows.append((decision_id, group_id, json.dumps(delta), 1, weight, now))
            mirror = [-value for value in delta]
            rows.append((decision_id, group_id, json.dumps(mirror), 0, weight, now))

    conn.executemany(
        "INSERT INTO preference_samples"
        " (decision_id, group_id, x_json, label, weight, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def retrain(conn):
    """Retrain the preference model from all stored samples.

    Runs full-batch gradient descent on the weighted logistic loss with L2
    regularization (λ = 0.1·√n) over feature-scaled (x/100) samples, blends
    the result toward the previous model via EMA (β = n/(n+50)), clamps
    weights to [-3, 5], and stores the result as a new preference_model row.

    With no samples at all the current model is returned unchanged (version
    is NOT incremented and nothing is written; no model at all → version 0
    with DEFAULT_WEIGHTS).

    Returns dict with keys: version, weights, bias, sample_count,
    iterations, final_loss.
    """
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT x_json, label, weight FROM preference_samples"
    ).fetchall()
    current = _load_model(conn)

    if not rows:
        return _unchanged_result(current)

    n = len(rows)
    samples = []
    for row in rows:
        raw = json.loads(row["x_json"])
        feats = [
            float(raw[i]) / FEATURE_SCALE for i in range(len(DIMENSIONS))
        ]
        samples.append((feats, float(row["label"]), float(row["weight"])))

    locked = _load_locked(conn)
    if current is not None:
        w = [float(current["weights"].get(dim, DEFAULT_WEIGHTS[dim])) for dim in DIMENSIONS]
        b = float(current["bias"])
    else:
        w = [0.2] * len(DIMENSIONS)
        b = 0.0

    lam = L2_BASE * math.sqrt(n)
    lr = LEARNING_RATE
    iterations = 0
    for _ in range(MAX_ITERATIONS):
        grad_w, grad_b, loss = _loss_and_grad(samples, w, b, lam)
        for i, dim in enumerate(DIMENSIONS):
            if dim in locked:
                grad_w[i] = 0.0
        norm = math.sqrt(sum(g * g for g in grad_w) + grad_b * grad_b)
        if norm < GRADIENT_TOL:
            break
        # Backtracking line search: halve the step while it raises the loss.
        stepped = False
        for _ in range(60):
            cand_w = [w[i] - lr * grad_w[i] for i in range(len(w))]
            cand_b = b - lr * grad_b
            if _loss(samples, cand_w, cand_b, lam) <= loss:
                w, b = cand_w, cand_b
                stepped = True
                break
            lr *= 0.5
        if not stepped:
            break  # step size underflowed; treat as converged
        iterations += 1

    # EMA blend toward the previous model (β = share of the NEW weights).
    if current is not None:
        old = [float(current["weights"].get(dim, DEFAULT_WEIGHTS[dim])) for dim in DIMENSIONS]
        beta = n / (n + EMA_SMOOTHING)
        w = [(1.0 - beta) * o + beta * t for o, t in zip(old, w)]

    w = [min(WEIGHT_MAX, max(WEIGHT_MIN, value)) for value in w]

    weights = {dim: w[i] for i, dim in enumerate(DIMENSIONS)}
    version = (
        conn.execute("SELECT MAX(version) FROM preference_model").fetchone()[0] or 0
    ) + 1
    conn.execute(
        "INSERT INTO preference_model"
        " (version, weights_json, bias, sample_count, trained_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (version, json.dumps(weights), b, n, datetime.now().isoformat()),
    )
    conn.commit()

    return {
        "version": version,
        "weights": weights,
        "bias": b,
        "sample_count": n,
        "iterations": iterations,
        "final_loss": _loss(samples, w, b, lam),
    }


def get_current_weights(conn):
    """Return the latest model: {weights, bias, sample_count, version, locked}.

    locked is the canonical-order list of locked dimensions. Without any
    stored model, DEFAULT_WEIGHTS are returned with version 0.
    """
    ensure_tables(conn)
    current = _load_model(conn)
    locked = _load_locked(conn)
    if current is None:
        return {
            "weights": dict(DEFAULT_WEIGHTS),
            "bias": 0.0,
            "sample_count": 0,
            "version": 0,
            "locked": locked,
        }
    weights = {
        dim: float(current["weights"].get(dim, DEFAULT_WEIGHTS[dim]))
        for dim in DIMENSIONS
    }
    return {
        "weights": weights,
        "bias": current["bias"],
        "sample_count": current["sample_count"],
        "version": current["version"],
        "locked": locked,
    }


def set_locked(conn, dims):
    """Store the list of locked dimensions (settings key 'locked_dims').

    Every entry must be one of app.recommender.DIMENSIONS, else ValueError
    (raised before anything is written).
    """
    ensure_tables(conn)
    dims = list(dims)
    unknown = sorted({d for d in dims if d not in DIMENSIONS})
    if unknown:
        raise ValueError(
            "unknown dimension(s) to lock: " + ", ".join(repr(d) for d in unknown)
        )
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (LOCKED_SETTINGS_KEY, json.dumps(dims)),
    )
    conn.commit()


# --- internals ---------------------------------------------------------------


def _sigmoid(z):
    if z > _SIGMOID_CLAMP:
        z = _SIGMOID_CLAMP
    elif z < -_SIGMOID_CLAMP:
        z = -_SIGMOID_CLAMP
    return 1.0 / (1.0 + math.exp(-z))


def _scores(conn, photo_id):
    row = conn.execute(
        "SELECT sharpness, exposure, noise, composition, faces"
        " FROM quality_scores WHERE photo_id = ?",
        (photo_id,),
    ).fetchone()
    if row is None:
        return {}
    return {dim: row[dim] for dim in DIMENSIONS if row[dim] is not None}


def _feature_delta(conn, kept_id, rejected_id):
    kept = _scores(conn, kept_id)
    rejected = _scores(conn, rejected_id)
    return [
        float(kept.get(dim, NEUTRAL_SCORE) - rejected.get(dim, NEUTRAL_SCORE))
        for dim in DIMENSIONS
    ]


def _group_feedback(conn, group_id):
    liked, disliked = set(), set()
    for row in conn.execute(
        "SELECT photo_id, action FROM feedback_events WHERE group_id = ?",
        (group_id,),
    ):
        if row["action"] == "dislike":
            disliked.add(row["photo_id"])
        elif row["action"] == "like":
            liked.add(row["photo_id"])
    liked -= disliked  # a photo with both events: dislike wins
    return liked, disliked


def _load_model(conn):
    row = conn.execute(
        "SELECT version, weights_json, bias, sample_count FROM preference_model"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        weights = json.loads(row["weights_json"]) if row["weights_json"] else {}
    except (TypeError, ValueError):
        weights = {}
    if not isinstance(weights, dict):
        weights = {}
    return {
        "version": row["version"],
        "weights": weights,
        "bias": float(row["bias"] or 0.0),
        "sample_count": row["sample_count"] or 0,
    }


def _load_locked(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LOCKED_SETTINGS_KEY,)
    ).fetchone()
    if row is None or not row["value"]:
        return []
    try:
        dims = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    if not isinstance(dims, list):
        return []
    return [d for d in DIMENSIONS if d in dims]


def _unchanged_result(current):
    if current is None:
        return {
            "version": 0,
            "weights": dict(DEFAULT_WEIGHTS),
            "bias": 0.0,
            "sample_count": 0,
            "iterations": 0,
            "final_loss": 0.0,
        }
    return {
        "version": current["version"],
        "weights": {
            dim: float(current["weights"].get(dim, DEFAULT_WEIGHTS[dim]))
            for dim in DIMENSIONS
        },
        "bias": current["bias"],
        "sample_count": current["sample_count"],
        "iterations": 0,
        "final_loss": 0.0,
    }


def _loss(samples, w, b, lam):
    """Weighted logistic loss + L2 (bias unregularized), at (w, b)."""
    loss = 0.0
    for x, y, sw in samples:
        z = b + sum(wi * xi for wi, xi in zip(w, x))
        p = _sigmoid(z)
        if y > 0.5:
            loss -= sw * math.log(p)
        else:
            loss -= sw * math.log(1.0 - p)
    loss += lam * sum(v * v for v in w)
    return loss


def _loss_and_grad(samples, w, b, lam):
    """Loss and its gradient (locked dims are NOT zeroed here)."""
    loss = 0.0
    grad_w = [0.0] * len(w)
    grad_b = 0.0
    for x, y, sw in samples:
        z = b + sum(wi * xi for wi, xi in zip(w, x))
        p = _sigmoid(z)
        if y > 0.5:
            loss -= sw * math.log(p)
        else:
            loss -= sw * math.log(1.0 - p)
        resid = sw * (p - y)  # d(-loglik)/dz
        for i, xi in enumerate(x):
            grad_w[i] += resid * xi
        grad_b += resid
    for i, wi in enumerate(w):
        grad_w[i] += 2.0 * lam * wi
    loss += lam * sum(v * v for v in w)
    return grad_w, grad_b, loss
