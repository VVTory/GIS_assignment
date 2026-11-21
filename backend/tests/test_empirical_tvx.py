import numpy as np
from app.algorithms.empirical_tvx import BaselineParameters, estimate, saturation_slope_kpa_per_c

def test_saturation_slope_uses_kelvin_input_and_is_finite():
    value = saturation_slope_kpa_per_c(np.array([293.15]))
    assert np.isfinite(value[0]) and 0.1 < value[0] < 0.3

def test_estimate_runs_on_synthetic_unit_fixture():
    n = 1000
    fc = np.linspace(0.01, 0.99, n)
    ndvi = 0.05 + np.sqrt(fc) * (0.94 - 0.05)
    lst = 290 + (1 - fc) * 12 + np.sin(np.arange(n))
    result = estimate(lst, ndvi, parameters=BaselineParameters(min_bin_samples=3, min_bins=5))
    assert result["status"] == "ready"
    assert np.isfinite(result["ef"]).any()
    assert result["diagnostics"]["ta_source"] == "baseline_Ts_proxy"

def test_all_missing_returns_not_ready_without_fabricated_values():
    result = estimate(np.array([np.nan]), np.array([np.nan]))
    assert result["status"] == "not_ready" and np.isnan(result["ef"]).all()


def test_non_decreasing_dry_edge_is_not_published():
    fc = np.linspace(0.01, 0.99, 1000)
    ndvi = 0.05 + np.sqrt(fc) * 0.89
    result = estimate(290 + fc * 12, ndvi, parameters=BaselineParameters(min_bin_samples=3))
    assert result["reason"] == "non_decreasing_dry_edge"
    assert np.isnan(result["ef"]).all()


def test_output_within_physical_range_and_invalid_land_inputs_excluded():
    fc = np.linspace(0.01, 0.99, 1000)
    ndvi = 0.05 + np.sqrt(fc) * 0.89
    lst = 308 - fc * 15 + np.sin(np.arange(1000))
    ndvi[:2] = [-0.1, 1.2]
    result = estimate(lst, ndvi, parameters=BaselineParameters(min_bin_samples=3))
    values = result["ef"][np.isfinite(result["ef"])]
    assert values.size and np.all((values >= 0) & (values <= 1))
    assert np.isnan(result["ef"][:2]).all()
