# Hermes dashboard

The React/TypeScript dashboard connects to the actual `/api/v1` service. It starts with an empty workspace, and only the explicit **Fixture benchmark** adapter creates synthetic data.

## Run locally

Start the Python API on `127.0.0.1:18877`, then from `frontend/` run `npm ci` and `npm run dev`. Vite serves `http://127.0.0.1:18878` and proxies `/api` to the API. Set `ATLAS_API_URL` when using another API address. `npm run build` type-checks and creates `frontend/dist`; FastAPI serves that directory when it exists at startup.

## Workspace behaviors

- **Overview:** real aggregate row/request counts, products finished, active runs, recent harvests, and service status. Finished products can have partial coverage; completion is reported separately.
- **Harvest runs:** search/filter, inspect per-product coverage and errors, pause/resume/cancel, review events, browse rows, and download exports.
- **Product explorer:** choose a run, search its records, page through 50 rows at a time, and inspect source IDs, configuration values, evidence references, images, and production fields. Quantity-price charts group matching configurations, currency, production, and provenance; they describe only the rows loaded on the current page.
- **Sources & AI:** configure supplier pacing, optional authorized headers, and OpenAI-compatible, Anthropic, or custom JSON model APIs. Test connections before using providers to interpret instructions.
- **Commerce calculators:** human-readable shipping tariff/package and Printograph production/rush forms call the service's deterministic calculation endpoints. Inputs contain no invented rate defaults; results remain explicitly labeled estimates.
- **Run composer:** URL lists, CSV URL import, optional source/title resolution, natural-language instructions with a selected provider, images, explicit configuration filters, shipping contexts, optional per-product request/configuration bounds, and source revalidation.

Visible dashboards poll every three seconds. Row polling runs only for active harvests and prevents overlapping refreshes; pagination/search ignore stale responses. Hidden tabs stop initiating scheduled requests. Server errors remain visible; incomplete extraction is never presented as complete. Browser auto-selection for Printograph uses the backend's normal browser adapter; API-specific access remains optional. Supplier efficiency metrics exclude synthetic fixture rows.

## Authentication

When `/health` reports `authentication_required`, enter the server's API token through **API access**. The token is held in `sessionStorage` for the current browser-tab session and sent as `Authorization: Bearer …`. Export downloads use authenticated requests. Supplier headers and model keys are submitted to the server's encrypted credential store; they are not returned in listing responses.

## Accessibility and responsive behavior

Forms use native inputs and labels. Dialogs include focus trapping, Escape dismissal, focus restoration, and scroll containment. Navigation collapses on small screens; wide tables scroll horizontally. Buttons and pagination are keyboard accessible; motion respects reduced-motion preferences. System fonts provide a fallback when the optional Google Fonts stylesheet is unavailable.

## Validation

`npm run build` passes TypeScript checks and creates a production bundle. CUA browser inspection covered the connected desktop overview and mobile navigation / harvest form at 390 × 844; the temporary viewport was restored afterward. Synthetic API payload checks validated shipping with dimensional weight/surcharges and production/transit dates. Currency formatting checks cover JPY/KWD precision; chart contexts use stable nested serialization and withhold conflicting same-quantity prices. Backend extraction tests are separate. No supplier throughput figure is inferred from dashboard fixture counts.
