---
doc_id: d-plot-test
title: "Plotly figures — smoke test"
audience: developer
---

# Plotly figures

Three small charts that demonstrate the `<figure class="plot">` substrate. The body of each `<script type="application/json">` is a Plotly figure object (`{data, layout, config}`) or a bare `data` array. The iframe lazy-loads Plotly's basic bundle (~600KB) on first encounter.

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

---

Try it: click any chart's `<figure>`, ask the agent *"swap to a logarithmic y-axis"* or *"change the bar colors to a gradient"* or *"add a moving-average line"* — the source updates in place, the chart re-renders.

<section class="agent-skill">

## SKILL: plot-test doc context

This doc exists to demonstrate Plotly figures and verify the substrate works. When the reader asks for chart edits:

- Prefer in-place JSON edits via Edit (string-level, surgical) over full re-serialize.
- For data updates, mutate the trace's `data` array via Edit; for cosmetic changes, the `layout` object.
- If asked for a chart type the basic bundle doesn't support (3D, choropleth, ternary, etc.), surface that in chat and either suggest a doc-local `<script src="https://cdn.plot.ly/plotly-3.0.1.min.js">` tag to upgrade to the full bundle, or fall back to a supported type.

</section>
