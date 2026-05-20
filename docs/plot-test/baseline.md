---
doc_id: d-plot-test
title: "Chart figures — Plotly + Vega-Lite"
audience: developer
---

# Chart figures

Two charting substrates ship side by side. Both take JSON specs in a `<script type="application/json">` body; the figure class picks the renderer.

- `<figure class="plot">` → Plotly (imperative `{data, layout}` shape, ~600KB basic bundle, great for interactive 3D / dashboards / scientific & financial charts)
- `<figure class="vega">` → Vega-Lite (declarative grammar-of-graphics, ~800KB total, great for statistical charts and small-multiples / facets)

Both lazy-load on first encounter — docs without charts pay zero cost.

## Plotly

## Scatter + line

<figure class="plot">
<script type="application/json">
{
  "data": [
    {
      "x": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
      "y": [2.1, 2.9, 3.7, 5.0, 5.2, 6.1, 7.0, 8.3, 8.8, 9.5],
      "type": "scatter",
      "mode": "lines+markers",
      "name": "observed",
      "line": {"color": "#4f46e5"},
      "marker": {"size": 8}
    }
  ],
  "layout": {
    "title": "Linear-ish growth",
    "xaxis": {"title": "step"},
    "yaxis": {"title": "value"},
    "margin": {"l": 50, "r": 20, "t": 50, "b": 50},
    "height": 320
  }
}
</script>
</figure>

## Bar chart

<figure class="plot">
<script type="application/json">
{
  "data": [
    {
      "x": ["intro", "galois", "csv-test", "mermaid-test", "help"],
      "y": [120, 340, 85, 200, 180],
      "type": "bar",
      "marker": {"color": "#16a34a"}
    }
  ],
  "layout": {
    "title": "Sample doc sizes (lines)",
    "margin": {"l": 50, "r": 20, "t": 50, "b": 80},
    "height": 320
  }
}
</script>
</figure>

## Pie chart

<figure class="plot">
<script type="application/json">
{
  "data": [
    {
      "values": [35, 25, 18, 12, 10],
      "labels": ["Doc", "Source", "History", "Skills", "View ▾"],
      "type": "pie",
      "marker": {"colors": ["#4f46e5", "#818cf8", "#16a34a", "#d97706", "#71717a"]},
      "textinfo": "label+percent"
    }
  ],
  "layout": {
    "title": "Hypothetical tab usage",
    "margin": {"l": 20, "r": 20, "t": 50, "b": 20},
    "height": 320,
    "showlegend": false
  }
}
</script>
</figure>

## Vega-Lite

Same three chart types, expressed in Vega-Lite's declarative grammar. Notice the specs are typically shorter than the Plotly equivalents for common chart types.

### Scatter + line

<figure class="vega">
<script type="application/json">
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "title": "Linear-ish growth",
  "width": 500,
  "height": 260,
  "data": {"values": [
    {"step": 1, "value": 2.1}, {"step": 2, "value": 2.9},
    {"step": 3, "value": 3.7}, {"step": 4, "value": 5.0},
    {"step": 5, "value": 5.2}, {"step": 6, "value": 6.1},
    {"step": 7, "value": 7.0}, {"step": 8, "value": 8.3},
    {"step": 9, "value": 8.8}, {"step": 10, "value": 9.5}
  ]},
  "mark": {"type": "line", "point": true, "color": "#4f46e5"},
  "encoding": {
    "x": {"field": "step", "type": "quantitative"},
    "y": {"field": "value", "type": "quantitative"}
  }
}
</script>
</figure>

### Bar chart

<figure class="vega">
<script type="application/json">
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "title": "Sample doc sizes (lines)",
  "width": 500,
  "height": 260,
  "data": {"values": [
    {"doc": "intro",        "lines": 120},
    {"doc": "galois",       "lines": 340},
    {"doc": "csv-test",     "lines": 85},
    {"doc": "mermaid-test", "lines": 200},
    {"doc": "help",         "lines": 180}
  ]},
  "mark": {"type": "bar", "color": "#16a34a"},
  "encoding": {
    "x": {"field": "doc", "type": "nominal", "axis": {"labelAngle": -25}},
    "y": {"field": "lines", "type": "quantitative"}
  }
}
</script>
</figure>

### Layered chart — statistical shape Vega-Lite makes easy

A scatter of points + a moving-average line + a confidence ribbon, all in one spec:

<figure class="vega">
<script type="application/json">
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "title": "Daily measurements + rolling trend",
  "width": 500,
  "height": 260,
  "data": {"values": [
    {"day": 1, "v": 3.2}, {"day": 2, "v": 4.1}, {"day": 3, "v": 3.8},
    {"day": 4, "v": 5.0}, {"day": 5, "v": 4.6}, {"day": 6, "v": 5.4},
    {"day": 7, "v": 6.0}, {"day": 8, "v": 5.7}, {"day": 9, "v": 6.5},
    {"day": 10, "v": 7.2}, {"day": 11, "v": 6.9}, {"day": 12, "v": 7.6}
  ]},
  "layer": [
    {
      "mark": {"type": "point", "color": "#4f46e5", "opacity": 0.6, "size": 60}
    },
    {
      "transform": [
        {"window": [{"op": "mean", "field": "v", "as": "rolling"}],
         "frame": [-2, 2]}
      ],
      "mark": {"type": "line", "color": "#dc2626", "strokeWidth": 2}
    }
  ],
  "encoding": {
    "x": {"field": "day", "type": "quantitative"},
    "y": {"field": "v", "type": "quantitative", "title": "value"}
  }
}
</script>
</figure>

---

Try it: click any chart's `<figure>`, ask the agent *"swap to a logarithmic y-axis"* or *"change the bar colors to a gradient"* or *"add a moving-average line"* — the source updates in place, the chart re-renders.

<section class="agent-skill">

## SKILL: chart-figures doc context

This doc demonstrates both Plotly and Vega-Lite figure substrates. When the reader asks for chart edits:

- **Stay in the same library.** A Plotly chart's edits are JSON edits to its `{data, layout}`; a Vega chart's edits are JSON edits to its `{mark, encoding, data}`. Don't silently swap libraries.
- **Prefer in-place JSON edits.** String-level Edit to the script's `textContent` keeps the change small and reviewable; full re-serialize is fine but loses the human shape (indentation, key order).
- **Cross-library suggestions are fair.** If the reader's ask fits one library much better ("layer 5 series with smoothing bands" → Vega-Lite is more idiomatic; "interactive 3D surface" → Plotly with the full bundle), say so before editing.
- **Plotly upgrade path.** If the reader needs 3D / choropleth / contour, suggest a doc-local `<script src="https://cdn.plot.ly/plotly-3.0.1.min.js">` to swap to the full Plotly bundle.

</section>
