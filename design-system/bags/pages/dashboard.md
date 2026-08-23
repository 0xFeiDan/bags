# Bags Dashboard Overrides

> **Project:** Bags
> **Page type:** Asset Ledger Command Center

These page rules override the project master design system.

## Layout

- Use a 12-column Bento grid with varied spans; avoid a flat row of equal KPI cards.
- Maximum content width: 1560px.
- Order: Portfolio Command hero > Ledger Health + review queue > Asset Flow Rail > Performance + Exposure > Asset Ledger.
- The cross-account transfer flow and cost-basis carry-forward are primary product information, not a secondary table.

## Visual direction

- OLED near-black canvas with restrained surface contrast and crisp 1px borders.
- Green only means positive PnL. Gold only means review or reconciliation state. Violet means chain or system identity. Red means exceptions.
- Avoid neon cyberpunk effects, decorative glass blur, and status indicated by color alone.

## Page components

- **Portfolio Command:** dominant net-worth module with compact range controls and a chart.
- **Ledger Health:** reconcile score, explicit exception count, and a visible review action.
- **Asset Flow Rail:** source, destination, amount, carried cost, fee, and confidence in one readable flow.
- **Review Status:** static status badges; only actual actions are buttons.
- **Exposure bars:** show net direction with text labels as well as color.

## Interaction and accessibility

- Use 150–250ms color/elevation transitions only; no continuous animation.
- Place “Review transfers” beside Ledger Health; keep “Connect account” in the global header.
- Use SVG icons in one outline style. Icon-only controls require descriptive labels.
- Preserve readable labels and provide horizontal containment rather than page-level overflow on narrow screens.
