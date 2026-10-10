"""Vision: turn camera frames into posture *readings*.

Public contract (agreed in hour 0, everything downstream depends on it)::

    {
        "ts":          1760101234.5,   # wall-clock seconds
        "person":      True,
        "seated":      True,
        "neck_angle":  18.4,           # degrees from vertical
        "torso_angle": 7.1,            # degrees from vertical
        "score":       0.86,           # 0..1, 1 == calibrated upright posture
    }

Downstream code only ever sees readings -- never frames.
"""

__all__ = [
    "angles",
    "calibrate",
    "detector",
    "landmarks",
    "source",
    "synthetic",
]
