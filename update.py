"""
GB Heat Demand: Live Dashboard
Fetches real-time gas transmission data, weather and carbon intensity,
computes heat demand breakdown and renders public/index.html + public/data.json.
"""

import json
import math
import os
import pathlib
import datetime
import pprint
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# National Gas REST API — catalogue-based discovery.
# No authentication required (open data policy).
REST_BASE = "https://api.nationalgas.com/operationaldata/v1"
CATALOGUE_URL = f"{REST_BASE}/publications/catalogue"

# 1 mcm/d → MW
# Calculation: 1,000,000 m³/day × 39.5 MJ/m³ ÷ (3600 s/h × 24 h/day) = MW
MCM_D_TO_MW: float = 1_000_000 * 39.5 / 3600 / 24

# All 13 GB Local Distribution Zones; requiring ≥5 guards against partial responses.
LDZ_CODES = {
    "sc", "no", "nw", "ne", "em", "wm",
    "sw", "se", "so", "ts", "wn", "ea", "nt",
}

# Map from full LDZ region names (as the REST API may return them) to 2-letter codes.
LDZ_NAME_MAP = {
    "scotland": "sc", "northern": "no", "north west": "nw",
    "north east": "ne", "east midlands": "em", "west midlands": "wm",
    "south west": "sw", "south east": "se", "southern": "so",
    "thames": "ts", "wales north": "wn", "wales": "wn",  # "wales" is a fallback alias for "wn"
    "eastern": "ea", "north thames": "nt",
}

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=53.5&longitude=-1.5&current_weather=true&timezone=Europe%2FLondon"
)
CARBON_INTENSITY_URL = "https://api.carbonintensity.org.uk/intensity"

# Seasonal fallback gas demand (GW → MW) keyed by month
SEASONAL_FALLBACK_GW = {
    1: 105, 2: 95, 3: 80, 4: 55, 5: 35, 6: 25,
    7: 22,  8: 22, 9: 30, 10: 55, 11: 75, 12: 100,
}

# ---------------------------------------------------------------------------
# Assumptions (A1–A16)
# ---------------------------------------------------------------------------
DOM_SHARE      = 0.62   # A1: domestic share of total gas
COM_SHARE      = 0.28   # A2: commercial/public share
# A3: industrial/power (10%) excluded
DOM_SH_FRAC    = 0.85   # A4: domestic space heating fraction
DOM_DHW_FRAC   = 0.15   # A4: domestic DHW fraction
COM_SH_FRAC    = 0.75   # A5: non-domestic space heating fraction
COM_DHW_FRAC   = 0.25   # A5: non-domestic DHW fraction

# A6: Domestic technology mix (fractions of domestic gas heat fuel)
DOM_MIX = {
    "Gas Boiler":       0.85,
    "Heat Pump":        0.025, # Amend to only reflect electric, however scale based on at time gas use
    "Heat Network":     0.025, # Amend to reflect electric HP and GAS (CHP, GB etc.), however scale based on at time gas use
    "Direct Electric":  0.02,  # Not linked to gas, however scale based on at time gas use
    "Oil/Other":        0.08,  # Not linked to gas, however scale based on at time gas use
}
# A7: Commercial technology mix
COM_MIX = {
    "Gas Boiler":       0.70,
    "Heat Pump":        0.05,
    "Heat Network":     0.08,
    "Direct Electric":  0.07,
    "Oil/Other":        0.10,
}

BOILER_EFF     = 0.80   # A8: gas boiler efficiency
HP_COP         = 3.0    # A9: heat pump COP
HN_LOSS        = 0.15   # A10: heat network distribution losses → delivery = 1-loss
GAS_EMISSION   = 0.183  # A11: kgCO₂e/kWh (DESNZ 2025)
GAS_COST_P     = 6.76   # A13: p/kWh (Ofgem Q1 2026)
ELEC_COST_P    = 24.50  # A14: p/kWh (Ofgem Q1 2026)

# Technology display colours
TECH_COLOURS = {
    "Gas Boiler":       "#e67e22",
    "Heat Pump":        "#27ae60",
    "Heat Network":     "#8e44ad",
    "Direct Electric":  "#2980b9",
    "Oil/Other":        "#7f8c8d",
}


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_gas_demand_mw() -> tuple[float, bool]:
    categories: list = []
    sub_items: list = []
    urls_tried: list = []

    try:
        result = _fetch_gas_demand_inner(categories, sub_items, urls_tried)
        if result is not None:
            return result
    except Exception as exc:
        print(f"  REST API error ({type(exc).__name__}): {exc}")

    # ------------------------------------------------------------------
    # Fallback — log diagnostics and return a seasonal estimate.
    # ------------------------------------------------------------------
    print(f"  FALLBACK: Using seasonal estimate. Catalogue categories found: {categories}")
    print(f"  Demand sub-items found: {sub_items}")
    print(f"  URLs tried: {urls_tried}")
    month = datetime.datetime.now(datetime.timezone.utc).month
    gw = SEASONAL_FALLBACK_GW[month]
    return gw * 1_000.0, False  # GW → MW


def _fetch_gas_demand_inner(
    categories: list, sub_items: list, urls_tried: list
) -> "tuple[float, bool] | None":
    """Inner implementation of fetch_gas_demand_mw(); mutates the diagnostic lists.

    Returns a ``(mw, live)`` pair on success, or ``None`` if no value could be
    extracted (triggering the seasonal fallback in the caller).
    """
    # ------------------------------------------------------------------
    # Step 1 — Get top-level categories from the catalogue.
    # The catalogue returns categories (e.g. "demand", "supplies") with
    # id=None; we must drill into the "demand" category separately.
    # ------------------------------------------------------------------
    cat_resp = requests.get(CATALOGUE_URL, timeout=20)
    cat_resp.raise_for_status()
    catalogue = cat_resp.json()

    # Normalise catalogue to a flat list of category objects.
    cat_list: list = []
    if isinstance(catalogue, list):
        cat_list = catalogue
    elif isinstance(catalogue, dict):
        for v in catalogue.values():
            if isinstance(v, list):
                cat_list = v
                break

    _TEXT_KEYS = ("text", "name", "label", "description", "title", "category")

    def item_text(item: dict) -> str:
        for k in _TEXT_KEYS:
            if k in item:
                return str(item[k]).lower().strip()
        return ""

    categories[:] = [item_text(c) for c in cat_list if isinstance(c, dict)]
    print(f"  [gas] Catalogue categories found: {categories}")

    # ------------------------------------------------------------------
    # Step 2 — Drill into the "demand" category to find sub-publications.
    # Try candidate URL patterns in order; use the first that returns 200.
    # ------------------------------------------------------------------
    candidate_urls = [
        f"{REST_BASE}/publications/catalogue/demand",
        f"{REST_BASE}/publications/demand",
        f"{REST_BASE}/publications/catalogue?category=demand",
        f"{REST_BASE}/demand",
    ]

    demand_payload = None
    for url in candidate_urls:
        urls_tried.append(url)
        try:
            resp = requests.get(url, timeout=20)
            print(f"  [gas] {resp.status_code} from {url}")
            if resp.status_code == 200:
                demand_payload = resp.json()
                print("  [gas] Demand sub-catalogue response (first 500 chars):")
                print(str(demand_payload)[:500])
                break
        except Exception as url_exc:
            print(f"  [gas] Error fetching {url}: {url_exc}")

    # Normalise sub-catalogue to a flat list.
    sub_list: list = []
    if demand_payload is not None:
        if isinstance(demand_payload, list):
            sub_list = demand_payload
        elif isinstance(demand_payload, dict):
            for v in demand_payload.values():
                if isinstance(v, list):
                    sub_list = v
                    break

    _ID_KEYS = (
        "id", "publicationId", "publicationID", "publication_id",
        "dataItemId", "itemId",
    )
    _PUB_NAME_KEYS = (
        "publicationObjectName", "dataItemName", "name", "description",
        "label", "title", "text",
    )

    def pub_text(pub: dict) -> str:
        parts = []
        for k in _PUB_NAME_KEYS:
            if k in pub:
                parts.append(str(pub[k]).lower())
        return " ".join(parts)

    def pub_id(pub: dict) -> str | None:
        for k in _ID_KEYS:
            if k in pub:
                val = pub[k]
                if val is not None:
                    return str(val)
        return None

    sub_items[:] = [pub_text(s) for s in sub_list if isinstance(s, dict)]
    print(f"  [gas] Demand sub-items found ({len(sub_list)}):")
    for item in sub_list:
        if isinstance(item, dict):
            print(f"    id={pub_id(item)!r}  text={pub_text(item)!r}")

    # Keywords that suggest an item contains demand data.
    _DEMAND_KEYWORDS = (
        "total", "nts", "instantaneous", "ldz", "offtake",
        "actual", "physical", "demand",
    )

    # Collect candidate items (those whose text matches a keyword).
    candidate_items = [
        item for item in sub_list
        if isinstance(item, dict)
        and any(kw in pub_text(item) for kw in _DEMAND_KEYWORDS)
    ]
    # If nothing matches, try all items.
    if not candidate_items:
        candidate_items = [i for i in sub_list if isinstance(i, dict)]

    # ------------------------------------------------------------------
    # Step 3 — Fetch data for each candidate item and attempt to parse.
    # ------------------------------------------------------------------
    def to_float(x) -> float | None:
        try:
            return float(str(x).strip().replace(",", ""))
        except (TypeError, ValueError):
            return None

    _NAME_KEYS = (
        "name", "rowName", "RowName", "zone", "type", "label",
        "description", "siteName", "category", "publicationObjectName",
        "dataItemName", "text",
    )
    _VALUE_KEYS = (
        "value", "flowValue", "FlowValue", "flow", "quantity",
        "instantaneousFlow", "operationalValue", "publishedValue",
        "currentValue",
    )

    def get_record_name(record: dict) -> str:
        for k in _NAME_KEYS:
            if k in record:
                return str(record[k]).lower().strip()
        return ""

    def extract_flow_value(record: dict) -> float | None:
        for k in _VALUE_KEYS:
            if k in record:
                v = to_float(record[k])
                if v is not None:
                    return v
        return None

    def try_parse_records(records: list) -> "tuple[float, bool] | None":
        """Try Strategy 1 then Strategy 2 on a flat list of records."""
        # Strategy 1: find a record whose name indicates total demand.
        for record in records:
            if not isinstance(record, dict):
                continue
            name = get_record_name(record)
            if "total" in name and "demand" in name:
                v = extract_flow_value(record)
                if v is not None and v > 0:
                    print(f"  [gas] Strategy 1 matched: name={name!r} value={v}")
                    return v * MCM_D_TO_MW, True

        # Strategy 2: sum individual LDZ offtake rows.
        ldz_sum = 0.0
        ldz_found: set = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            name = get_record_name(record)
            # Direct 2-letter code match.
            if name in LDZ_CODES:
                v = extract_flow_value(record)
                if v is not None:
                    ldz_sum += v
                    ldz_found.add(name)
                continue
            # Full region name match (e.g. "ldz sc offtake", "scotland offtake").
            matched_code: str | None = None
            for full_name, code in LDZ_NAME_MAP.items():
                if full_name in name and (
                    "ldz" in name or "offtake" in name or "demand" in name
                ):
                    matched_code = code
                    break
            # Also catch patterns like "ldz sc" or "ldz_sc".
            if not matched_code:
                for code in LDZ_CODES:
                    if f"ldz {code}" in name or f"ldz{code}" in name:
                        matched_code = code
                        break
            if matched_code and matched_code not in ldz_found:
                v = extract_flow_value(record)
                if v is not None:
                    ldz_sum += v
                    ldz_found.add(matched_code)

        if len(ldz_found) >= 5:
            print(f"  [gas] Strategy 2 matched {len(ldz_found)} LDZ zones: {ldz_found}")
            return ldz_sum * MCM_D_TO_MW, True

        return None

    # First, check if the demand_payload itself is already a data response
    # (i.e., it contains inline records rather than sub-publications).
    if demand_payload is not None:
        inline_records: list = []
        if isinstance(demand_payload, list):
            inline_records = demand_payload
        elif isinstance(demand_payload, dict):
            for v in demand_payload.values():
                if isinstance(v, list):
                    inline_records = v
                    break
        result = try_parse_records(inline_records)
        if result is not None:
            return result

    # Then try fetching data URLs for each candidate sub-item.
    for item in candidate_items:
        item_id = pub_id(item)
        if not item_id:
            continue
        data_urls = [
            f"{REST_BASE}/publications/{item_id}/data?latestFlag=Y",
            f"{REST_BASE}/publications/{item_id}/latest",
            f"{REST_BASE}/publications/{item_id}",
        ]
        for url in data_urls:
            urls_tried.append(url)
            try:
                resp = requests.get(url, timeout=20)
                print(f"  [gas] {resp.status_code} from {url}")
                if resp.status_code == 200:
                    print(f"  [gas] Response (first 500 chars): {resp.text[:500]}")
                    payload = resp.json()
                    records: list = []
                    if isinstance(payload, list):
                        records = payload
                    elif isinstance(payload, dict):
                        for v in payload.values():
                            if isinstance(v, list):
                                records = v
                                break
                    result = try_parse_records(records)
                    if result is not None:
                        return result
            except Exception as url_exc:
                print(f"  [gas] Error fetching {url}: {url_exc}")

    return None


def fetch_weather() -> dict:
    """Return dict with temperature_c and wind_speed."""
    try:
        resp = requests.get(OPEN_METEO_URL, timeout=10)
        resp.raise_for_status()
        cw = resp.json()["current_weather"]
        return {
            "temperature_c": round(cw["temperature"], 1),
            "wind_speed_kmh": round(cw.get("windspeed", 0), 1),
            "available": True,
        }
    except Exception:
        return {"temperature_c": None, "wind_speed_kmh": None, "available": False}


def fetch_carbon_intensity() -> dict:
    """Return dict with intensity_gco2_kwh and index label."""
    try:
        resp = requests.get(CARBON_INTENSITY_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()["data"][0]["intensity"]
        return {
            "gco2_kwh": data["actual"] or data["forecast"],
            "index": data["index"],
            "available": True,
        }
    except Exception:
        return {"gco2_kwh": None, "index": "unknown", "available": False}


# ---------------------------------------------------------------------------
# Heat demand computation
# ---------------------------------------------------------------------------

def compute_heat(gas_total_mw: float, elec_ci_gco2_kwh: float | None) -> dict:
    """Compute sectoral heat demand, carbon and cost breakdowns."""
    dom_fuel_mw  = gas_total_mw * DOM_SHARE
    com_fuel_mw  = gas_total_mw * COM_SHARE

    elec_emission = (elec_ci_gco2_kwh or 200) / 1_000.0  # gCO₂/kWh → kgCO₂/kWh

    def sector_breakdown(fuel_mw, mix, sh_frac, dhw_frac):
        techs = {}
        total_heat = 0.0
        total_gas_fuel = 0.0
        total_elec_fuel = 0.0
        total_carbon = 0.0
        total_cost_gbp_h = 0.0

        for tech, share in mix.items():
            fuel = fuel_mw * share  # MW of fuel (gas or electricity or both)
            if tech == "Gas Boiler":
                heat = fuel * BOILER_EFF
                gas_f = fuel
                elec_f = 0.0
                carbon = gas_f * GAS_EMISSION        # tCO₂/h (MW×kgCO₂/kWh = t/h)
                cost = gas_f * GAS_COST_P * 10       # £/h  (MW × p/kWh × 1000kWh/MWh / 100p/£)
            elif tech == "Heat Pump":
                heat = fuel * HP_COP
                gas_f = 0.0
                elec_f = fuel
                carbon = elec_f * elec_emission
                cost = elec_f * ELEC_COST_P * 10
            elif tech == "Heat Network":            # amend to consider electric heat pumps
                heat = fuel * (1 - HN_LOSS)
                gas_f = fuel
                elec_f = 0.0
                carbon = gas_f * GAS_EMISSION
                cost = gas_f * GAS_COST_P * 10
            elif tech == "Direct Electric":
                heat = fuel * 1.0  # COP=1
                gas_f = 0.0
                elec_f = fuel
                carbon = elec_f * elec_emission
                cost = elec_f * ELEC_COST_P * 10
            else:  # Oil/Other – treated similar to gas at boiler eff
                heat = fuel * BOILER_EFF
                gas_f = fuel
                elec_f = 0.0
                carbon = gas_f * GAS_EMISSION
                cost = gas_f * GAS_COST_P * 10

            sh_heat  = heat * sh_frac
            dhw_heat = heat * dhw_frac

            techs[tech] = {
                "fuel_mw":     round(fuel, 2),
                "heat_mw":     round(heat, 2),
                "sh_mw":       round(sh_heat, 2),
                "dhw_mw":      round(dhw_heat, 2),
                "gas_fuel_mw": round(gas_f, 2),
                "elec_fuel_mw":round(elec_f, 2),
                "carbon_t_h":  round(carbon, 2),
                "cost_gbp_h":  round(cost, 2),
            }
            total_heat       += heat
            total_gas_fuel   += gas_f
            total_elec_fuel  += elec_f
            total_carbon     += carbon
            total_cost_gbp_h += cost

        return {
            "fuel_mw":       round(fuel_mw, 2),
            "total_heat_mw": round(total_heat, 2),
            "gas_fuel_mw":   round(total_gas_fuel, 2),
            "elec_fuel_mw":  round(total_elec_fuel, 2),
            "carbon_t_h":    round(total_carbon, 2),
            "cost_gbp_h":    round(total_cost_gbp_h, 2),
            "sh_mw":         round(total_heat * sh_frac, 2),
            "dhw_mw":        round(total_heat * dhw_frac, 2),
            "technologies":  techs,
        }

    domestic   = sector_breakdown(dom_fuel_mw, DOM_MIX, DOM_SH_FRAC, DOM_DHW_FRAC)
    commercial = sector_breakdown(com_fuel_mw, COM_MIX, COM_SH_FRAC, COM_DHW_FRAC)

    total_heat_mw   = domestic["total_heat_mw"] + commercial["total_heat_mw"]
    total_carbon    = domestic["carbon_t_h"]    + commercial["carbon_t_h"]
    gas_carbon      = (domestic["gas_fuel_mw"] + commercial["gas_fuel_mw"]) * GAS_EMISSION
    elec_carbon     = (domestic["elec_fuel_mw"] + commercial["elec_fuel_mw"]) * elec_emission

    # Aggregate technology totals across both sectors
    agg_techs = {}
    for tech in DOM_MIX:
        d = domestic["technologies"][tech]
        c = commercial["technologies"][tech]
        agg_techs[tech] = {
            "heat_mw":     round(d["heat_mw"] + c["heat_mw"], 2),
            "fuel_mw":     round(d["fuel_mw"] + c["fuel_mw"], 2),
            "dom_heat_mw": round(d["heat_mw"], 2),
            "com_heat_mw": round(c["heat_mw"], 2),
            "carbon_t_h":  round(d["carbon_t_h"] + c["carbon_t_h"], 2),
            "cost_gbp_h":  round(d["cost_gbp_h"] + c["cost_gbp_h"], 2),
        }

    return {
        "gas_total_mw":   round(gas_total_mw, 2),
        "total_heat_mw":  round(total_heat_mw, 2),
        "total_carbon_t_h": round(total_carbon, 2),
        "gas_carbon_t_h": round(gas_carbon, 2),
        "elec_carbon_t_h":round(elec_carbon, 2),
        "domestic":       domestic,
        "commercial":     commercial,
        "technologies":   agg_techs,
    }


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

def intensity_colour(index: str) -> str:
    mapping = {
        "very low": "#1a7f37",
        "low":      "#0969da",
        "moderate": "#bf8700",
        "high":     "#cf222e",
        "very high": "#cf222e",
    }
    return mapping.get((index or "").lower(), "#656d76")


def co2_colour(kg_kwh: float) -> str:
    """Colour for electricity carbon intensity based on kgCO₂/kWh thresholds."""
    if kg_kwh < 0.150:
        return "#1a7f37"
    if kg_kwh < 0.300:
        return "#bf8700"
    return "#cf222e"


def pie_svg(tech_data: dict, total_heat_mw: float) -> str:
    """Generate a clean SVG pie chart with tooltips."""
    cx, cy, r = 160, 160, 120
    slices = []
    start = -math.pi / 2  # start at top

    items = [
        (tech, v["heat_mw"])
        for tech, v in tech_data.items()
        if v["heat_mw"] > 0
    ]
    total = sum(v for _, v in items) or 1

    paths = []
    for tech, val in items:
        angle = 2 * math.pi * val / total
        end = start + angle
        x1 = cx + r * math.cos(start)
        y1 = cy + r * math.sin(start)
        x2 = cx + r * math.cos(end)
        y2 = cy + r * math.sin(end)
        large = 1 if angle > math.pi else 0
        colour = TECH_COLOURS[tech]
        pct = round(val / total * 100, 1)
        title = f"{tech}: {val:,.0f} MW ({pct}%)"
        paths.append(
            f'<path d="M{cx},{cy} L{x1:.2f},{y1:.2f} A{r},{r} 0 {large},1 {x2:.2f},{y2:.2f} Z" '
            f'fill="{colour}" stroke="#ffffff" stroke-width="2" '
            f'data-tech="{tech}" data-mw="{val:,.0f}" data-pct="{pct}" style="cursor:pointer">'
            f'<title>{title}</title></path>'
        )
        start = end

    return (
        f'<svg viewBox="0 0 320 320" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:320px;display:block;margin:auto">'
        + "".join(paths)
        + "</svg>"
    )


def bar_chart(sector_data: dict, label: str) -> str:
    """Horizontal bar chart for a sector's tech breakdown."""
    techs = sector_data["technologies"]
    total = sector_data["total_heat_mw"] or 1
    rows = []
    for tech, v in techs.items():
        pct = v["heat_mw"] / total * 100
        colour = TECH_COLOURS[tech]
        rows.append(
            f'<div style="margin-bottom:6px">'
            f'<div style="display:flex;justify-content:space-between;font-size:0.78rem;margin-bottom:2px">'
            f'<span style="color:#1f2328">{tech}</span>'
            f'<span style="color:#656d76">{v["heat_mw"]:,.0f} MW</span></div>'
            f'<div style="background:#d0d7de;border-radius:4px;height:8px;overflow:hidden">'
            f'<div style="background:{colour};width:{pct:.1f}%;height:100%;border-radius:4px"></div>'
            f'</div></div>'
        )
    return (
        f'<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:10px;padding:18px">'
        f'<h3 style="margin:0 0 14px;font-size:1rem;color:#1f2328">{label}</h3>'
        + "".join(rows)
        + f'<div style="margin-top:10px;font-size:0.78rem;color:#656d76">'
        f'Total heat: <strong style="color:#0969da">{sector_data["total_heat_mw"]:,.0f} MW</strong> | '
        f'Carbon: <strong style="color:#cf222e">{sector_data["carbon_t_h"]:,.0f} tCO₂/h</strong>'
        f'</div></div>'
    )


def render_html(
    heat: dict,
    weather: dict,
    carbon: dict,
    gas_live: bool,
    timestamp: str,
) -> str:
    ci_gco2   = carbon.get("gco2_kwh")
    ci_index  = carbon.get("index", "unknown")
    ci_kg     = round(ci_gco2 / 1000, 3) if ci_gco2 is not None else None
    ci_colour = co2_colour(ci_kg) if ci_kg is not None else "#656d76"
    temp_str  = f"{weather['temperature_c']}°C" if weather.get("temperature_c") is not None else "N/A"
    ci_str    = f"{ci_kg:.3f} kgCO₂/kWh" if ci_kg is not None else "N/A"

    pie  = pie_svg(heat["technologies"], heat["total_heat_mw"])
    dom_chart = bar_chart(heat["domestic"],   "Domestic")
    com_chart = bar_chart(heat["commercial"], "Commercial / Public")

    # Tech cards
    tech_cards_html = ""
    for tech, v in heat["technologies"].items():
        colour = TECH_COLOURS[tech]
        pct = v["heat_mw"] / (heat["total_heat_mw"] or 1) * 100
        tooltip = (
            f"Fuel input: {v['fuel_mw']:,.0f} MW&lt;br&gt;"
            f"Domestic: {v['dom_heat_mw']:,.0f} MW&lt;br&gt;"
            f"Commercial: {v['com_heat_mw']:,.0f} MW&lt;br&gt;"
            f"Cost: £{v['cost_gbp_h']:,.0f}/h"
        )
        tech_cards_html += (
            f'<div data-tooltip="{tooltip}" style="background:#f6f8fa;border:1px solid {colour};border-radius:10px;'
            f'padding:16px;border-left-width:4px;cursor:default">'
            f'<div style="font-weight:600;color:{colour};margin-bottom:8px">{tech}</div>'
            f'<div style="font-size:1.4rem;font-weight:700;color:#1f2328">{v["heat_mw"]:,.0f} <span style="font-size:0.8rem;color:#656d76">MW</span></div>'
            f'<div style="font-size:0.78rem;color:#656d76;margin-top:4px">{pct:.1f}% of total heat</div>'
            f'<div style="font-size:0.78rem;color:#656d76;margin-top:4px">'
            f'Carbon: {v["carbon_t_h"]:,.1f} tCO₂/h</div>'
            f'</div>'
        )

    # Carbon summary chips
    def chip(label, value, colour="#0969da"):
        return (
            f'<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;'
            f'padding:12px 18px;text-align:center;min-width:130px">'
            f'<div style="font-size:0.72rem;color:#656d76;margin-bottom:4px">{label}</div>'
            f'<div style="font-size:1.1rem;font-weight:700;color:{colour}">{value}</div>'
            f'</div>'
        )

    carbon_chips = (
        chip("Gas CO₂", f"{heat['gas_carbon_t_h']:,.0f} tCO₂/h", "#bf8700")
        + chip("Elec CO₂", f"{heat['elec_carbon_t_h']:,.1f} tCO₂/h", "#0969da")
        + chip("Total CO₂", f"{heat['total_carbon_t_h']:,.0f} tCO₂/h", "#cf222e")
        + chip("Total Gas", f"{heat['gas_total_mw']:,.0f} MW", "#656d76")
    )

    # Info bar chips
    def info_chip(label, value, colour="#1f2328"):
        return (
            f'<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:20px;'
            f'padding:6px 14px;white-space:nowrap">'
            f'<span style="color:#656d76;font-size:0.75rem">{label} </span>'
            f'<span style="color:{colour};font-weight:600;font-size:0.85rem">{value}</span>'
            f'</div>'
        )

    live_badge = (
        '<span style="background:#1a7f37;color:#fff;border-radius:12px;'
        'padding:2px 8px;font-size:0.7rem;font-weight:600;margin-left:8px">LIVE</span>'
        if gas_live else
        '<span style="background:#656d76;color:#fff;border-radius:12px;'
        'padding:2px 8px;font-size:0.7rem;font-weight:600;margin-left:8px">ESTIMATED</span>'
    )

    info_bar = (
        info_chip("🌡 Temp", temp_str)
        + info_chip("Gas", f"{GAS_COST_P}p/kWh")
        + info_chip("Elec", f"{ELEC_COST_P}p/kWh")
        + info_chip("Gas CO₂", f"{GAS_EMISSION} kg/kWh")
        + info_chip("Grid CO₂", ci_str, ci_colour)
        + info_chip("Boiler efficiency", "80%")
        + info_chip("Heat Pump COP", "3.0")
    )

    assumptions_html = "".join(
        f'<li style="margin-bottom:6px">{a}</li>'
        for a in [
            "A1: Domestic share of total gas: 62% (DESNZ 2023)",
            "A2: Commercial/public share: 28% (DESNZ)",
            "A3: Industrial/power (10%) excluded from heat analysis",
            "A4: Domestic gas split: 85% Space Heating, 15% DHW (BRE/SAP)",
            "A5: Non-domestic gas split: 75% Space Heating, 25% DHW (CIBSE TM46)",
            "A6: Domestic tech mix — Gas boiler 85%, HP 2.5%, Heat network 2.5%, Direct electric 2%, Oil/LPG/Other 8%",
            "A7: Commercial tech mix — Gas boiler 70%, HP 5%, Heat network 8%, Direct electric 7%, Oil/Other 10%",
            "A8: Gas boiler efficiency: 80%",
            "A9: Heat pump COP: 3.0",
            "A10: Heat network distribution losses: 15%",
            "A11: Gas emission factor: 0.183 kgCO₂e/kWh (DESNZ 2025)",
            "A12: Electricity carbon intensity: real-time from Carbon Intensity API (National Grid ESO)",
            "A13: Gas unit cost: 6.76 p/kWh (Ofgem Q1 2026)",
            "A14: Electricity unit cost: 24.50 p/kWh (Ofgem Q1 2026)",
            "A15: No weather-correction applied to live transmission data",
            "A16: All 13 LDZs (Local Distribution Zones) aggregated",
        ]
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UK Heat: Live</title>
<style>
  :root{{
    --bg:#ffffff;--surf:#f6f8fa;--bord:#d0d7de;
    --txt:#1f2328;--txt2:#656d76;
    --accent:#0969da;--green:#1a7f37;--orange:#bf8700;--red:#cf222e;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}}
  a{{color:var(--accent);text-decoration:none}}
  a:hover{{text-decoration:underline}}
  header{{background:var(--surf);border-bottom:1px solid var(--bord);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}}
  header h1{{font-size:1.3rem;font-weight:700}}
  .ts{{font-size:0.8rem;color:var(--txt2)}}
  .info-bar{{display:flex;flex-wrap:wrap;gap:8px;padding:16px 24px;background:var(--surf);border-bottom:1px solid var(--bord)}}
  .main{{max-width:1200px;margin:0 auto;padding:24px 16px;display:grid;gap:24px}}
  .grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
  .card{{background:var(--surf);border:1px solid var(--bord);border-radius:12px;padding:20px}}
  .card h2{{font-size:1rem;margin-bottom:16px;color:var(--txt)}}
  .tech-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}}
  .chips{{display:flex;flex-wrap:wrap;gap:12px}}
  footer{{background:var(--surf);border-top:1px solid var(--bord);padding:24px;font-size:0.82rem;color:var(--txt2);max-width:1200px;margin:0 auto}}
  details summary{{cursor:pointer;color:var(--accent);margin-top:12px;font-weight:600}}
  details ul{{margin-top:10px;padding-left:20px;color:var(--txt2)}}
  @media(max-width:700px){{.grid-2{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<header>
  <h1>UK Heat: Live{live_badge}</h1>
  <span class="ts">Updated: {timestamp} UTC</span>
</header>
<div class="info-bar">{info_bar}</div>
<div class="main">

  <!-- Pie + tech cards -->
  <div class="grid-2">
    <div class="card">
      <h2>Heat by Technology</h2>
      {pie}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:16px;font-size:0.78rem">
        {"".join(
          f'<div style="display:flex;align-items:center;gap:6px">'
          f'<span style="background:{TECH_COLOURS[t]};width:10px;height:10px;border-radius:50%;display:inline-block"></span>'
          f'<span style="color:#1f2328">{t}</span></div>'
          for t in TECH_COLOURS
        )}
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;align-content:start">
      {tech_cards_html}
    </div>
  </div>

  <!-- Sector comparison -->
  <div>
    <h2 style="font-size:1rem;margin-bottom:14px">Sector Comparison</h2>
    <div class="grid-2">
      {dom_chart}
      {com_chart}
    </div>
  </div>

  <!-- Carbon summary -->
  <div class="card">
    <h2>Carbon Summary</h2>
    <div class="chips">{carbon_chips}</div>
    <p style="margin-top:12px;font-size:0.78rem;color:#656d76">
      Grid carbon intensity: <strong style="color:{ci_colour}">{ci_str}</strong>
      ({ci_index}) | Gas emission factor: {GAS_EMISSION} kgCO₂e/kWh
    </p>
  </div>

</div><!-- /main -->

<footer>
  <p>
    Real-time UK gas transmission demand decomposed into domestic and commercial heat supply technologies.
    Gas data: <a href="https://data.nationalgas.com" target="_blank">National Gas Transmission Data Portal</a>.
    Weather: <a href="https://open-meteo.com" target="_blank">Open-Meteo</a>.
    Carbon intensity: <a href="https://carbonintensity.org.uk" target="_blank">National Grid ESO Carbon Intensity API</a>.
    Inspired by <a href="https://grid.iamkate.com/" target="_blank">grid.iamkate.com</a>.
  </p>
  <details>
    <summary>Methodology &amp; Assumptions (A1–A16)</summary>
    <ul>{assumptions_html}</ul>
    <p style="margin-top:10px">
      Data sources:
      <a href="https://api.nationalgas.com/operationaldata/v1" target="_blank">National Gas Instantaneous Flow REST API</a> ·
      <a href="https://www.gov.uk/government/collections/energy-consumption-in-the-uk" target="_blank">DESNZ Energy Consumption in the UK</a> ·
      <a href="https://api.carbonintensity.org.uk" target="_blank">Carbon Intensity API</a> ·
      <a href="https://www.ofgem.gov.uk/check-if-energy-price-cap-affects-you" target="_blank">Ofgem Price Cap</a>
    </p>
  </details>
</footer>
<script>
(function(){{
  var tip=document.createElement('div');
  tip.style.cssText='position:fixed;background:#1f2328;color:#fff;padding:8px 12px;border-radius:8px;'
    +'font-size:0.8rem;pointer-events:none;display:none;box-shadow:0 4px 12px rgba(0,0,0,.15);'
    +'z-index:9999;max-width:220px;line-height:1.6;border-top:3px solid #0969da';
  document.body.appendChild(tip);
  function show(e,html){{tip.innerHTML=html;tip.style.display='block';move(e);}}
  function move(e){{
    var cx=e.clientX||0;
    var cy=e.clientY||0;
    var x=cx+14,y=cy+14;
    if(x+230>window.innerWidth)x=cx-230;
    tip.style.left=x+'px';tip.style.top=y+'px';
  }}
  function hide(){{tip.style.display='none';}}
  document.querySelectorAll('svg path[data-tech]').forEach(function(p){{
    var tech=p.getAttribute('data-tech');
    var mw=p.getAttribute('data-mw');
    var pct=p.getAttribute('data-pct');
    var col=p.getAttribute('fill');
    var html='<span style="color:'+col+';font-weight:700">'+tech+'</span><br>'+mw+' MW &nbsp; '+pct+'%';
    p.addEventListener('mouseover',function(e){{show(e,html);}});
    p.addEventListener('mousemove',move);
    p.addEventListener('mouseout',hide);
    p.addEventListener('touchend',function(e){{show(e,html);}});
  }});
  document.querySelectorAll('[data-tooltip]').forEach(function(el){{
    var raw=el.getAttribute('data-tooltip');
    var html=raw.replace(/&lt;br&gt;/g,'<br>');
    el.addEventListener('mouseover',function(e){{show(e,html);}});
    el.addEventListener('mousemove',move);
    el.addEventListener('mouseout',hide);
  }});
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    print("Fetching gas demand…")
    gas_mw, gas_live = fetch_gas_demand_mw()
    print(f"  Gas demand: {gas_mw:,.0f} MW  (live={gas_live})")

    print("Fetching weather…")
    weather = fetch_weather()
    print(f"  Temperature: {weather.get('temperature_c')}°C")

    print("Fetching carbon intensity…")
    carbon = fetch_carbon_intensity()
    print(f"  Carbon intensity: {carbon.get('gco2_kwh')} gCO₂/kWh ({carbon.get('index')})")

    print("Computing heat demand breakdown…")
    heat = compute_heat(gas_mw, carbon.get("gco2_kwh"))

    outdir = pathlib.Path("public")
    outdir.mkdir(exist_ok=True)

    data = {
        "timestamp": timestamp,
        "gas_live": gas_live,
        "gas_total_mw": heat["gas_total_mw"],
        "total_heat_mw": heat["total_heat_mw"],
        "total_carbon_t_h": heat["total_carbon_t_h"],
        "weather": weather,
        "carbon_intensity": carbon,
        "domestic": heat["domestic"],
        "commercial": heat["commercial"],
        "technologies": heat["technologies"],
    }
    (outdir / "data.json").write_text(json.dumps(data, indent=2))
    print("  Written public/data.json")

    html = render_html(heat, weather, carbon, gas_live, timestamp)
    (outdir / "index.html").write_text(html)
    print("  Written public/index.html")

    print("Done.")


if __name__ == "__main__":
    main()
