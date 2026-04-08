# 🔥 GB Heat Demand: Live

A real-time, self-updating static dashboard showing GB building heat demand by technology and sector — the heat equivalent of [grid.iamkate.com](https://grid.iamkate.com/).

Live site → **[EllieEnergy.github.io/uk-heat-live](https://EllieEnergy.github.io/uk-heat-live)**

---

## What it shows

| Panel | Description |
|---|---|
| **Info bar** | Outside temperature, gas & electricity unit costs, emission factors, live grid carbon intensity |
| **Pie chart** | Total heat output split by technology (Gas Boiler, Heat Pump, Heat Network, Direct Electric, Oil/Other) |
| **Technology cards** | Heat output (MW), share of total, and carbon rate per technology |
| **Sector comparison** | Stacked bar charts for Domestic vs Commercial/Public heat mix |
| **Carbon summary** | Gas CO₂, electricity CO₂, total CO₂ (tCO₂/h), and total gas demand |

---

## How it works

1. **`update.py`** runs every 5 minutes via GitHub Actions.
2. It fetches:
   - **Gas demand** from the [National Gas Transmission Data Portal](https://data.nationalgas.com) (LDZ Offtake, all 13 zones aggregated)
   - **Outside temperature** from [Open-Meteo](https://open-meteo.com) (grid centroid 53.5°N, 1.5°W)
   - **Live grid carbon intensity** from the [Carbon Intensity API](https://api.carbonintensity.org.uk) (National Grid ESO)
3. Heat demand is split by sector (domestic 62%, commercial 28%, industrial excluded — A1–A3) and by technology using published mix assumptions (A6–A7).
4. The script renders `public/index.html` (self-contained, no JS framework) and `public/data.json`.
5. The `public/` folder is force-pushed to the **`gh-pages`** branch, which GitHub Pages serves automatically.

---

## Data sources

| Source | Data | URL |
|---|---|---|
| National Gas Transmission | LDZ offtake (kW, ~5 min) | `data.nationalgas.com` |
| Open-Meteo | Current weather | `api.open-meteo.com` |
| Carbon Intensity API | Real-time grid CO₂ (gCO₂/kWh) | `api.carbonintensity.org.uk` |
| DESNZ | Domestic/commercial gas shares | [Energy Consumption in the UK](https://www.gov.uk/government/collections/energy-consumption-in-the-uk) |
| Ofgem | Unit costs (Q1 2026 price cap) | [Ofgem price cap](https://www.ofgem.gov.uk/check-if-energy-price-cap-affects-you) |
| BRE / SAP | Space heating vs DHW split | SAP 10.2 |
| CIBSE TM46 | Non-domestic SH/DHW split | CIBSE TM46 |

---

## Methodology assumptions (A1–A16)

| # | Assumption | Value | Source |
|---|---|---|---|
| A1 | Domestic share of total gas | 62% | DESNZ 2023 |
| A2 | Commercial/public share | 28% | DESNZ |
| A3 | Industrial/power excluded | 10% | — |
| A4 | Domestic: Space heating / DHW | 85% / 15% | BRE/SAP |
| A5 | Non-domestic: SH / DHW | 75% / 25% | CIBSE TM46 |
| A6 | Domestic tech mix | Gas 85%, HP 2.5%, HN 2.5%, DE 2%, Oil 8% | Estimate |
| A7 | Commercial tech mix | Gas 70%, HP 5%, HN 8%, DE 7%, Oil 10% | Estimate |
| A8 | Gas boiler efficiency | 80% | — |
| A9 | Heat pump COP | 3.0 | — |
| A10 | Heat network losses | 15% | — |
| A11 | Gas emission factor | 0.183 kgCO₂e/kWh | DESNZ 2025 |
| A12 | Electricity carbon intensity | Real-time | Carbon Intensity API |
| A13 | Gas unit cost | 6.76 p/kWh | Ofgem Q1 2026 |
| A14 | Electricity unit cost | 24.50 p/kWh | Ofgem Q1 2026 |
| A15 | Weather correction | None (live data) | — |
| A16 | LDZ aggregation | All 13 zones | NGT |

---

## Hosting (GitHub Pages)

The dashboard is served from the `gh-pages` branch, populated automatically by the CI workflow.

1. Go to **Settings → Pages** in this repository.
2. Set source to **Deploy from branch**, branch **`gh-pages`**, folder **`/ (root)`**.
3. Save. The site will be live at `https://<org>.github.io/uk-heat-live/`.

---

## Local development

```bash
# Clone the repo
git clone https://github.com/EllieEnergy/uk-heat-live.git
cd uk-heat-live

# Install dependencies
pip install -r requirements.txt

# Generate the dashboard (writes public/index.html and public/data.json)
python update.py

# Open locally
open public/index.html   # macOS
xdg-open public/index.html  # Linux
```

To override the NGT resource ID:

```bash
NGT_RESOURCE_ID=your-resource-id python update.py
```

---

## Licence

[MIT](LICENSE) — inspired by [grid.iamkate.com](https://grid.iamkate.com/) by Kate Morley.