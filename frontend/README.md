# Assay frontend foundation

This is a deliberately plain Svelte 5 and Vite shell. It proves that the real
generated data can be loaded, searched, typed, revalued from a live quote, tested,
and bundled. It is not the product design.

```bash
cd frontend
npm install
npm run dev
```

`npm run prepare-data` links the repository's ignored `data/` directory at
`public/data`. It refuses to replace a real directory. A production checkout must
restore the orphan `data` branch into the repository's `data/` directory before
running `npm run build`.

Checks:

```bash
npm test
npm run check
npm run build
```

The complete browser contract is in `src/lib/assay.ts`. It exposes loaders for
the index, universe, company page bundle, peer groups, sectors, methodology,
coverage, construction sensitivity, forecasts, and live quotes. Product
structure, motion rules, copy, states, and deployment settings are in
`../docs/frontend-handoff.md`.
