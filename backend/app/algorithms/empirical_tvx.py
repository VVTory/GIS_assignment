"""Transparent engineering baseline for EF; not a paper reproduction."""
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class BaselineParameters:
    ndvi_soil: float = 0.05
    ndvi_vegetation: float = 0.94
    bin_width: float = 0.05
    min_bin_samples: int = 30
    min_bins: int = 5
    dry_quantile: float = 0.98
    wet_quantile: float = 0.05
    gamma_kpa_per_c: float = 0.0665
    method_version: str = "empirical_tvx_v1"


def saturation_slope_kpa_per_c(temp_k: np.ndarray) -> np.ndarray:
    """Slope of saturation vapor pressure, evaluated in Celsius."""
    tc = np.asarray(temp_k, dtype=np.float64) - 273.15
    es = 0.6108 * np.exp(17.27 * tc / (tc + 237.3))
    return 4098.0 * es / np.square(tc + 237.3)


def estimate(lst_k: np.ndarray, ndvi: np.ndarray, *, ta_k: np.ndarray | None = None, valid_mask: np.ndarray | None = None, parameters: BaselineParameters = BaselineParameters()) -> dict[str, Any]:
    """Estimate a single-scene engineering baseline; this is not annual calibration."""
    ts, vi = np.broadcast_arrays(np.asarray(lst_k, dtype=np.float64), np.asarray(ndvi, dtype=np.float64))
    mask = np.isfinite(ts) & np.isfinite(vi) & (ts > 200) & (ts < 400) & (vi >= 0) & (vi <= 1)
    if valid_mask is not None:
        mask &= np.broadcast_to(np.asarray(valid_mask, dtype=bool), ts.shape)
    if ta_k is None:
        ta, ta_source = ts.copy(), "baseline_Ts_proxy"
    else:
        ta = np.broadcast_to(np.asarray(ta_k, dtype=np.float64), ts.shape).copy()
        mask &= np.isfinite(ta)
        ta_source = "provided_air_temperature"
    output = np.full(ts.shape, np.nan, dtype=np.float32)
    qc = np.full(ts.shape, 1, dtype=np.uint8)
    if not np.any(mask):
        return {"ef": output, "qc": qc, "status": "not_ready", "reason": "no_valid_inputs", "diagnostics": {"valid_pixels": 0, "ta_source": ta_source}}
    fc = np.clip((vi - parameters.ndvi_soil) / (parameters.ndvi_vegetation - parameters.ndvi_soil), 0.0, 1.0) ** 2
    wet = float(np.quantile(ts[mask], parameters.wet_quantile))
    edges = np.arange(0.0, 1.0 + parameters.bin_width, parameters.bin_width)
    centers, dry_values = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        in_bin = mask & (fc >= lo) & ((fc < hi) if hi < 1 else (fc <= hi))
        if int(in_bin.sum()) >= parameters.min_bin_samples:
            centers.append(float((lo + hi) / 2))
            dry_values.append(float(np.quantile(ts[in_bin], parameters.dry_quantile)))
    if len(centers) < parameters.min_bins:
        qc[mask] = 2
        return {"ef": output, "qc": qc, "status": "not_ready", "reason": "insufficient_edge_samples", "diagnostics": {"valid_pixels": int(mask.sum()), "valid_bins": len(centers), "required_bins": parameters.min_bins, "ta_source": ta_source}}
    slope, intercept = np.polyfit(np.asarray(centers), np.asarray(dry_values), 1)
    if slope >= 0:
        qc[mask] = 5
        return {
            "ef": output, "qc": qc, "status": "not_ready",
            "reason": "non_decreasing_dry_edge",
            "diagnostics": {
                "valid_pixels": int(mask.sum()), "valid_bins": len(centers),
                "dry_edge_slope_k": float(slope), "dry_edge_intercept_k": float(intercept),
                "wet_edge_k": wet, "ta_source": ta_source,
            },
        }
    denom = slope * fc + intercept - wet
    small = mask & (denom <= 1e-6)
    qc[small] = 3
    good = mask & ~small
    d = np.full(ts.shape, np.nan)
    d[good] = (ts[good] - wet) / denom[good]
    invalid_d = good & ((d < 0) | (d > 1))
    qc[invalid_d] = 4
    good &= ~invalid_d
    delta = saturation_slope_kpa_per_c(ta)
    ef = 1.26 * (1.0 - d) * delta / (delta + parameters.gamma_kpa_per_c)
    finite = good & np.isfinite(ef) & (ef >= 0) & (ef <= 1)
    qc[good & ~finite] = 6
    output[finite] = ef[finite].astype(np.float32)
    qc[finite] = 0
    return {"ef": output, "qc": qc, "status": "ready" if finite.any() else "not_ready", "reason": None if finite.any() else "no_valid_output", "diagnostics": {"valid_pixels": int(mask.sum()), "output_pixels": int(finite.sum()), "valid_bins": len(centers), "wet_edge_k": wet, "dry_edge_slope_k": float(slope), "dry_edge_intercept_k": float(intercept), "ta_source": ta_source, "method_version": parameters.method_version, "scientific_status": "engineering_baseline_not_paper_reproduction"}}
