# Funky Junque — FBA Inventory Dashboard

Live inventory & restock dashboard for the Funky Junque team, built by Burley Media.

- **Dashboard** (`docs/`): single-page app hosted on GitHub Pages. Sign-in required
  (Supabase Auth). All product data, tags, costs and notes are loaded from Supabase
  at runtime — nothing sensitive is stored in this repository.
- **Sync** (`sync/`): GitHub Actions job that pulls live data from Amazon SP-API
  (FBA inventory, inbound, planning/age report, FBM listings, Sales & Traffic
  1/7/30-day windows) and writes a fresh snapshot to Supabase every 6 hours.
  A weekly job refreshes the 12-month sales trend.

## Manual refresh

Actions → "Sync dashboard data" → Run workflow (mode: `fast`).
For the 12-month trend: mode `monthly`.

## Secrets (repo → Settings → Secrets and variables → Actions)

`LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `LWA_REFRESH_TOKEN`, `SELLER_ID`,
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
