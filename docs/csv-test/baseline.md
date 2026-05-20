---
title: "Data figures smoke test"
---

# Data figures

Both CSV and JSON sources render through the same Tabulator grid.

## CSV

<figure class="data">
<script type="text/csv">
name,age,city
Alice,30,NYC
Bob,25,Berlin
Carol,40,Buenos Aires
</script>
</figure>

## JSON — array of records

Most natural shape: each row is a record with named fields.

<figure class="data">
<script type="application/json">
[
  {"name": "Dana",  "age": 27, "city": "Helsinki"},
  {"name": "Erik",  "age": 35, "city": "Lisbon"},
  {"name": "Farah", "age": 22, "city": "Cairo"},
  {"name": "Gus",   "age": 41, "city": "Auckland"}
]
</script>
</figure>

## JSON — array of arrays with header row

Equivalent shape, sometimes more compact for wide tables.

<figure class="data">
<script type="application/json">
[
  ["product",   "price", "in_stock"],
  ["espresso",   3.50,   true],
  ["cappuccino", 4.25,   true],
  ["mocha",      4.75,   false]
]
</script>
</figure>
