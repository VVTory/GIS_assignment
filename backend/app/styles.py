"""Versioned colour contracts shared by the API, browser and GeoServer SLD."""
import numpy as np

STYLE_VERSION = "surface-palettes-v2"
PALETTES = {
    "ndvi": {"colors": ["#9b6b43", "#ede5bd", "#a5cf76", "#4b9d53", "#125b35"], "label": "稀疏植被 → 茂密植被", "range": [-0.2, 1.0]},
    "lst": {"colors": ["#365fa9", "#8ac5dc", "#fff0b0", "#eb9352", "#b43638"], "label": "较低温度 → 较高温度"},
    "soil_moisture": {"colors": ["#b88352", "#eee4cc", "#a8d6d1", "#4b9bb9", "#245187"], "label": "较干 → 较湿", "range": [0.0, 0.6]},
    "ef": {"colors": ["#c5814f", "#e5d7a5", "#acd3a1", "#56a19a", "#315c87"], "label": "较低 EF → 较高 EF", "range": [0.0, 1.0]},
}


def style_for(variable, assets):
    spec = PALETTES.get(variable, PALETTES["ef"])
    limits = spec.get("range")
    if limits is None:
        peers = [a["statistics"] for a in assets if a.get("variable") == variable and a.get("statistics", {}).get("min") is not None]
        limits = [min(p["min"] for p in peers), max(p["max"] for p in peers)] if peers else [-3.15, 46.85]
    return {**spec, "range": limits, "version": STYLE_VERSION}


def rgba(values, valid, spec):
    lo, hi = spec["range"]
    scaled = np.where(valid, np.clip((values - lo) / max(hi - lo, 1e-6), 0, 1), 0)
    colors = np.asarray([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in spec["colors"]])
    stops = np.linspace(0, 1, len(colors))
    rgb = [np.interp(scaled, stops, colors[:, i]) for i in range(3)]
    return np.stack([*rgb, valid * 255], axis=-1).astype(np.uint8)
