"""Train the carry estimate (commentator/carry_estimate.py) from a season DB, and write it beside it.

    pip install scikit-learn
    python tools/train_carry_estimate.py --db logs/season.db

What it learns: GSPro's own carry before the lie and course (the CARRY (raw) tile) from the ball
numbers a monitor sends: ball speed, launch angle, total spin and spin axis. GSPro works the carry out
from these itself, whatever monitor sent them, so a model learned from one league's shots holds for
a monitor that sends no carry. Only shots whose raw tile and launch monitor carry agree are learned
from (one of the two misread otherwise). Rounds are held out whole to report the accuracy.

The guard: the estimate is only made for a normal full swing. Tops and thin shots (low launch) and
big hooks and slices (a wide spin axis) carry unlike anything nearby in the data, and inputs outside
the ranges learned from get no estimate either.

The model is gradient-boosted trees, written out as plain arrays so the app evaluates them with no
scikit-learn; this script checks the written model gives scikit-learn's predictions.
"""

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

OUT = Path(__file__).resolve().parents[1] / "commentator" / "carry_estimate.json"
FEATURES = ("ball_speed", "launch_angle", "total_spin", "spin_axis")   # spin axis as its size: left or right alike
AGREE_YARDS = 1.5
MIN_LAUNCH = 6.0     # degrees: below it, a top or a thin shot
MAX_AXIS = 35.0      # degrees: beyond it, a big hook or slice
PARAMS = dict(max_iter=300, learning_rate=0.1, max_leaf_nodes=15, random_state=0)


def load(db: Path):
    rows = sqlite3.connect(db).execute(
        """SELECT ball_speed, launch_angle, total_spin, spin_axis, carry_raw, units, carry_raw_tile_yards, session_id
           FROM shots WHERE is_putt = 0 AND excluded = 0 AND ball_speed > 20 AND launch_angle > 0
           AND total_spin > 0 AND carry_raw_tile_yards > 5 AND carry_raw > 0""").fetchall()
    rows = [r for r in rows if (r[5] or "yards").lower().startswith("y")]
    x = np.array([[r[0], r[1], r[2], abs(r[3] or 0)] for r in rows], float)
    y = np.array([r[6] for r in rows], float)
    clean = np.abs(np.array([r[4] for r in rows]) - y) <= AGREE_YARDS
    return x[clean], y[clean], np.array([r[7] for r in rows])[clean]


def guard(x: np.ndarray) -> dict[str, list[float]]:
    lo, hi = np.percentile(x, 0.5, axis=0), np.percentile(x, 99.5, axis=0)
    return {"ball_speed": [round(float(lo[0]), 1), round(float(hi[0]), 1)],
            "launch_angle": [MIN_LAUNCH, round(float(hi[1]), 1)],
            "total_spin": [round(float(lo[2])), round(float(hi[2]))],
            "spin_axis": [0.0, MAX_AXIS]}


def inside(x: np.ndarray, g: dict[str, list[float]]) -> np.ndarray:
    ok = np.ones(len(x), bool)
    for i, name in enumerate(FEATURES):
        ok &= (x[:, i] >= g[name][0]) & (x[:, i] <= g[name][1])
    return ok


def report(x, y, sessions) -> None:
    rounds = np.unique(sessions)
    np.random.default_rng(0).shuffle(rounds)
    pred = np.full(len(y), np.nan)
    for held in np.array_split(rounds, 5):
        test = np.isin(sessions, held)
        pred[test] = HistGradientBoostingRegressor(**PARAMS).fit(x[~test], y[~test]).predict(x[test])
    kept = inside(x, guard(x))
    err = np.abs(pred - y)
    print(f"{len(y)} shots learned from; held-out rounds, guarded (the flight call's 50+ yards):")
    for lo, hi in ((50, 120), (120, 180), (180, 400)):
        k = kept & (y >= lo) & (y < hi)
        print(f"  {lo:>3}-{hi:<3} yd: estimated {k.sum() / ((y >= lo) & (y < hi)).sum():.0%}, typical miss "
              f"{np.median(err[k]):.1f} yd, 90% within {np.percentile(err[k], 90):.1f} yd, within the flight "
              f"call's tolerance {np.mean(err[k] <= np.maximum(5, 0.06 * y[k])):.1%}")


def export(model: HistGradientBoostingRegressor) -> list[dict[str, list]]:
    trees = []
    for (predictor,) in model._predictors:
        nodes = predictor.nodes
        leaf = nodes["is_leaf"].astype(bool)
        trees.append({
            "feature": np.where(leaf, -1, nodes["feature_idx"]).astype(int).tolist(),
            "threshold": [float(t) for t in nodes["num_threshold"]],   # exact: a shot can sit right on a split
            "left": nodes["left"].astype(int).tolist(),
            "right": nodes["right"].astype(int).tolist(),
            "value": [round(float(v), 5) for v in nodes["value"]],
        })
    return trees


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    x, y, sessions = load(args.db)
    report(x, y, sessions)
    model = HistGradientBoostingRegressor(**PARAMS).fit(x, y)
    data = {"features": list(FEATURES), "base": round(float(model._baseline_prediction.ravel()[0]), 5),
            "guard": guard(x), "shots": len(y), "trees": export(model)}

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from commentator.carry_estimate import Model
    ours = Model(data)
    worst = max(abs(ours.predict(*row) - want) for row, want in zip(x, model.predict(x)))
    if worst > 0.05:
        raise SystemExit(f"the written model is {worst:.3f} yd off scikit-learn's: not written")
    args.out.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size // 1024} KB); guard {data['guard']}")


if __name__ == "__main__":
    main()
