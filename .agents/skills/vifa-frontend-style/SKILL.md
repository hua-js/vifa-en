---
name: vifa-frontend-style
description: Use when creating or editing VIFA HTML dashboards, energy-monitoring pages, EMS consoles, or static frontend mockups. Do not use for backend-only or data-only tasks.
---

# VIFA Frontend Style

Create calm, information-dense industrial energy interfaces. Existing page conventions take priority; for new pages, use the defaults below.

## Visual system

- Use CSS custom properties and support light/dark themes when appropriate.
- Base palette: paper `#f3f6fb/#0b1020`, panel `#fff/#11182a`, ink `#172033/#edf3ff`, muted `#69778e/#91a1bf`.
- Semantic colors: blue = information/discharge, green = online/charge/positive, amber = warning/peak tariff, red = alarm/unsafe, violet = forecast/AI.
- Use 12–16px card radii, thin low-contrast borders, restrained shadows and subtle gradients. Avoid decorative glassmorphism, heavy glow and oversized headings.
- Use the system Chinese sans-serif stack. Apply `font-variant-numeric: tabular-nums` to measurements and tables.

## Layout and components

- Prefer: compact header → KPI overview → charts/flows → detail cards or tables → actions.
- Build responsive grids; collapse columns cleanly and allow horizontal scrolling only inside dense tables or charts.
- Cards need a clear title, optional context, primary value and unit. Keep units visually secondary but always visible.
- Use inline SVG icons with consistent stroke width; do not use emoji as interface icons.
- Buttons and inputs should be at least 40px high. Provide hover, active, disabled and `focus-visible` states.

## Data and interaction

- Show units, timestamps, data source and loading/empty/error states where relevant.
- Do not communicate status by color alone; pair color with text, icon or shape.
- Use realistic VIFA/EMS mock data and concise Chinese labels. Clearly mark simulated or estimated results.
- Keep interactions functional in static mockups: tabs, filters, theme toggles and detail panels must respond locally.

## Delivery

- For standalone mockups, produce one self-contained HTML file with embedded CSS and JavaScript; avoid external dependencies unless requested.
- Preserve existing behavior and limit changes to the requested page or component.
- Check desktop, tablet and mobile layouts for clipping, overflow and readable hierarchy. Report only checks actually performed.
