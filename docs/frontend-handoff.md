# Assay page brief

The grade contains thirteen accounting inputs and one market-informed
corporate-failure estimate. A current quote never moves the grade.

## Product promise

Use this wording as the baseline:

> Assay ranks a company's reported financial condition against comparable US
> companies. The grade combines reported accounting evidence with a
> market-informed corporate-failure estimate. It is not a return forecast.

The separate implied-expectations display shows how today's price relates to the
latest reported evidence. It has no verdict and never changes the grade.

Never use these words as product labels: quality, mispricing, misvaluation,
intrinsic value, sentiment, or hype. They make claims the system does not measure.

## Visual system

The company page is static HTML rendered by `assay/render.py` from the
published ticker JSON. It carries one inline script, for tooltips, and makes no
external request.
The style is the Berkshire Hathaway house style and nothing more.

### Palette and type

```
text and rules   #000080 (navy)     page          #ffffff
links            #0000ee            visited links #800080
section rules    #808080            bar track     #c8c8d8
panel headers    #e8e8f0
```

Times for everything, at 15px body, 24px small-caps masthead, 15px small-caps
section headings, 11px small print. Digits use `tabular-nums`. No web fonts.

### Anatomy

1. Masthead, then a two-column navigation table of links with small print
   under each, then a gray rule.
2. The sheet: two framed panels side by side with matching header bars. Left,
   the scoreboard (letter with the A to E ruler, rank, three sleeves each with
   a percentile bar, reliability and peer failure rate for the size band, size
   band, standard universe, warnings, inputs computable, graded-from date, last
   close, "price used in grade: none"). Right, the twelve-month price chart with
   the letter fixed at each rescan and SEC receipt dates ticked on the baseline,
   with the rescan table under it.
3. The company's own description: the opening of Item 1 of its latest 10-K,
   with a link to the document.
4. Warnings: a table of the seven checks with result and measured value.
5. The fourteen inputs: one table, a group row per sleeve, columns for the
   percentile bar, value, source, peer median, percentile, peers, period end.
   Not-computable inputs carry their reason in a small sub-row. Definitions
   follow as footnotes.
6. Peers: a kernel density of composite scores with the five letter bands, the
   company marked, the six largest peers named; the five neighbors above and
   below; the grade under four peer policies.
7. What the price implies, diagnostics in two columns, sources, limitations,
   footer.

### Citations

Every value carries a bracketed number linking to the SEC filing index page it
was read from. Consecutive numbers collapse to a range; a range from one filing
links to it, a range spanning filings links to the sources table. The sources
table lists every receipt with filing, viewer and series links. Company names
in the peer table open a hover card (CSS only) with letter, ruler, percentile,
sector line, market equity, last close and warnings.

### Charts

Inline SVG only, hairline navy strokes, faint area fills, Times labels, no
library, no animation. Labels are clamped inside the drawing and stacked when
they crowd.

### Banned

Gradients, shadows, glow, blur, rounded cards, gauges, colored grade ramps,
quadrants, verdict sentences, decorative motion, em dashes, filler text.

## Copy

Every sentence on a company page does one of three things: states a number with
its source, defines a term, or states a rule of the method. Anything else is cut.

- No first person. Not "we", not "our".
- No sentence fragments for emphasis. No aphorisms, no closers, no summary of
  what the reader just read.
- A warning is label, value, comparison, threshold, in that order.
- Interpretation lives in footnotes and on the methodology page, never inline.
- A limitation is stated as a measurement or as "not measured", never as
  reassurance.
- The Berkshire footer sentence is the one deliberate stylistic quotation on
  the page.

## Renderers

The interface is rendered by `assay/render.py` (company pages), `assay/home.py` (front page) and `assay/site.py` (everything else). Data shapes are documented by the artifacts themselves and by `README.md`.
