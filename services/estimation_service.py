"""Repair cost ESTIMATE from detected damage, using configurable sample rates.

This is a rule-based estimate, not an AI calculation and not a final repair cost.
Every amount comes from the RATES table below, so a claims manager can tune
them to real workshop rates without touching any other code.
"""
import config

# ---------------------------------------------------------------------------
# CONFIGURABLE SAMPLE RATES (Indian Rupees - INR). Sample rates per damage type.
# Structure: RATES[damage_type][severity] = (min_cost, max_cost)
# ---------------------------------------------------------------------------
RATES = {
    "DENT": {
        "MINOR": (2500, 3500),
        "MODERATE": (3500, 6000),
        "SEVERE": (6000, 8400),
        "base": 3000,
    },
    "SCRATCH": {
        "MINOR": (1000, 2000),
        "MODERATE": (2000, 3500),
        "SEVERE": (3500, 5000),
        "base": 1500,
    },
    "CRACK": {
        "MINOR": (2500, 4000),
        "MODERATE": (4000, 7000),
        "SEVERE": (7000, 15000),
        "base": 3250,
    },
    "PAINT_DAMAGE": {
        "MINOR": (2000, 3000),
        "MODERATE": (3000, 5500),
        "SEVERE": (5500, 8000),
        "base": 2500,
    },
    "BUMPER_DAMAGE": {
        "MINOR": (3000, 5000),
        "MODERATE": (5000, 9000),
        "SEVERE": (9000, 18000),
        "base": 4000,
    },
    "GLASS_DAMAGE": {
        "MINOR": (3500, 6000),
        "MODERATE": (6000, 11000),
        "SEVERE": (11000, 22000),
        "base": 4750,
    },
    "LAMP_DAMAGE": {
        "MINOR": (2000, 4000),
        "MODERATE": (4000, 8000),
        "SEVERE": (8000, 16000),
        "base": 3000,
    },
    "PANEL_DEFORMATION": {
        "MINOR": (4000, 7000),
        "MODERATE": (7000, 13000),
        "SEVERE": (13000, 25000),
        "base": 5500,
    },
    "BROKEN_OR_MISSING_PART": {
        "MINOR": (3000, 6000),
        "MODERATE": (6000, 12000),
        "SEVERE": (12000, 25000),
        "base": 4500,
    },
    "MIRROR_DAMAGE": {
        "MINOR": (1500, 3000),
        "MODERATE": (3000, 6000),
        "SEVERE": (6000, 12000),
        "base": 2250,
    },
    "TYRE_OR_WHEEL_DAMAGE": {
        "MINOR": (2000, 4000),
        "MODERATE": (4000, 8000),
        "SEVERE": (8000, 18000),
        "base": 3000,
    },
    "OTHER": {
        "MINOR": (1500, 3000),
        "MODERATE": (3000, 6000),
        "SEVERE": (6000, 12000),
        "base": 2250,
    },
}

# Compatibility alias
RATE_CARD = RATES

LINES = ("parts", "labour", "paint", "other")
LINE_LABELS = {"parts": "Parts", "labour": "Labour", "paint": "Paint / Materials", "other": "Other"}

TYPE_LABELS = {
    "DENT": "Dent",
    "SCRATCH": "Scratch",
    "CRACK": "Crack",
    "PAINT_DAMAGE": "Paint damage",
    "BUMPER_DAMAGE": "Bumper damage",
    "GLASS_DAMAGE": "Glass damage",
    "LAMP_DAMAGE": "Lamp damage",
    "PANEL_DEFORMATION": "Panel deformation",
    "BROKEN_OR_MISSING_PART": "Broken or missing part",
    "MIRROR_DAMAGE": "Mirror damage",
    "TYRE_OR_WHEEL_DAMAGE": "Tyre / wheel damage",
    "OTHER": "Other",
}

SEVERITY_LABELS = {
    "MINOR": "Minor",
    "MODERATE": "Moderate",
    "SEVERE": "Severe",
    "UNCERTAIN": "Needs manual assessment",
}


def item_cost_range(damage_type, severity):
    """Estimated (min_cost, max_cost) of one damage item in INR, or None if uncertain/other."""
    dtype = str(damage_type or "").strip().upper()
    sev = str(severity or "").strip().upper()
    if sev == "UNCERTAIN" or dtype == "OTHER":
        return None
    type_rates = RATES.get(dtype) or RATES.get("OTHER", {})
    return type_rates.get(sev)


def item_cost(damage_type, severity):
    """Estimated representative cost of one damage item, or None if uncertain/other."""
    dtype = str(damage_type or "").strip().upper()
    sev = str(severity or "").strip().upper()
    if sev == "UNCERTAIN" or dtype == "OTHER":
        return None
    r = item_cost_range(dtype, sev)
    if r is None:
        return None
    t_rates = RATES.get(dtype) or {}
    base_val = t_rates.get("base")
    default_base = (r[0] + r[1]) // 2
    if sev == "MINOR" and base_val is not None and base_val != default_base:
        return base_val
    return default_base


def dedupe(detections):
    """The same damage can appear in two photos. Count each part+type once,
    keeping the most confident sighting."""
    best = {}
    for d in detections:
        part = str(d.get("vehicle_part") or "OTHER").strip().upper()
        dtype = str(d.get("damage_type") or "OTHER").strip().upper()
        key = (part, dtype) if part != "OTHER" else (id(d),)
        conf = d.get("confidence")
        conf_val = float(conf) if conf is not None else 0.0
        if key not in best:
            best[key] = d
        else:
            prev_conf = best[key].get("confidence")
            prev_conf_val = float(prev_conf) if prev_conf is not None else 0.0
            if conf_val > prev_conf_val:
                best[key] = d
    return list(best.values())


def estimate(detections, threshold=None):
    """Build the estimate from merged, de-duplicated damage list.

    One line item per damage (type, vehicle part, severity, min-max cost)
    and a total range (sum of mins to sum of maxes).
    Damage with severity "uncertain" or type "other" gets flagged
    "needs manual assessment" and is excluded from the total.
    """
    threshold = config.DAMAGE_CONFIDENCE_THRESHOLD if threshold is None else threshold
    confirmed = []
    possible = []
    for d in detections:
        conf = d.get("confidence")
        if conf is not None and conf < threshold:
            possible.append(d)
        else:
            confirmed.append(d)

    deduped = dedupe(confirmed)
    items = []
    unpriced = []
    total_min = 0
    total_max = 0

    for d in sorted(deduped, key=lambda x: (x.get("confidence") is None, -(x.get("confidence") or 0.0))):
        dtype = str(d.get("damage_type") or "").strip().upper()
        sev = str(d.get("severity") or "").strip().upper()
        label = TYPE_LABELS.get(dtype, dtype.replace("_", " ").title())
        part = str(d.get("vehicle_part") or "OTHER").strip().upper()
        part_label = part.replace("_", " ").title().replace("Tyre", "tyre")
        sev_label = SEVERITY_LABELS.get(sev, sev.title())

        entry = dict(
            d,
            label=label,
            part_label=part_label,
            severity_label=sev_label,
        )

        cost_rng = item_cost_range(dtype, sev)
        if cost_rng is None:
            entry["cost_min"] = None
            entry["cost_max"] = None
            entry["estimated_cost"] = None
            entry["needs_manual_assessment"] = True
            unpriced.append(entry)
        else:
            c_min, c_max = cost_rng
            entry["cost_min"] = c_min
            entry["cost_max"] = c_max
            entry["cost_range"] = (c_min, c_max)
            entry["estimated_cost"] = (c_min + c_max) // 2
            entry["needs_manual_assessment"] = False
            total_min += c_min
            total_max += c_max
            items.append(entry)

    total = sum(i["estimated_cost"] for i in items)
    lines = {
        "parts": total,
        "labour": 0,
        "paint": 0,
        "other": 0,
    }

    return {
        "items": items,
        "unpriced": unpriced,
        "excluded_count": len(unpriced),
        "possible": [dict(d, label=TYPE_LABELS.get(str(d.get("damage_type") or "").upper(), "Damage")) for d in possible],
        "total_min": total_min,
        "total_max": total_max,
        "total_range": (total_min, total_max),
        "total": total,
        "lines": lines,
        "currency": config.CURRENCY_SYMBOL,
        "threshold": threshold,
    }


def format_money(amount, symbol=None):
    """Indian digit grouping: 125000 -> ₹ 1,25,000."""
    symbol = config.CURRENCY_SYMBOL if symbol is None else symbol
    n = int(round(amount or 0))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{symbol} {'-' if n < 0 else ''}{s}"
