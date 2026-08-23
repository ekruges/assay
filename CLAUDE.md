# Assay frontend work

The backend and browser data contract are ready. The current task is the frontend
UI. Do not redesign the research model while implementing the interface.

Read these first:

1. `docs/frontend-handoff.md` for the product, design, states, and completion gate.
2. `frontend/src/lib/assay.ts` for every supported browser call and type.
3. `README.md` for backend boundaries and verification commands.

Work in `frontend/`. Treat `assay/`, `functions/`, and the generated JSON shapes
as stable unless a failing contract test proves that a backend change is needed.

Start with:

```bash
cd frontend
npm run dev
```

The prepare script links the local ignored `data/` tree into `frontend/public`.
Never commit `data/`, `.cache/`, `.env.local`, `node_modules/`, `dist/`, or the
generated `frontend/public/data` symlink.

Use the functions in `frontend/src/lib/assay.ts`. Render
`loadCompanyPage(ticker)` first, then request `loadCompanyLiveData(report)` so a
slow or failed quote cannot block the company page.

Non-negotiable product rules:

- The grade ranks reported financial condition. It is not a return forecast.
- A live quote never changes the grade.
- Missing values stay missing and show their backend reason.
- Active warnings appear before the grade.
- Use the shared segmented A-E half-dial for the main grade and three sleeves.
- Show percentiles, peer-median comparisons, raw values, and evidence receipts.
- Never label a metric quality, mispricing, misvaluation, intrinsic value,
  sentiment, or hype.
- Keep the interface dark, simple, rectangular, accessible, and restrained.
- No chart or animation library. Respect reduced motion.

Before handing work back, run:

```bash
npm test
npm run check
npm run build
```

All eleven real UI fixtures listed in `docs/frontend-handoff.md` must render
without an exception.
