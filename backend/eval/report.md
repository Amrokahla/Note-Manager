# Eval Report — 2026-04-22 16:54 UTC

- Model: `gemini-2.5-flash`
- Pass rate: **14/14** run, 1 skipped, 15 total
- Duration: 91.3s

| # | Scenario | Tags | Result | Notes |
|---|----------|------|--------|-------|
| 01 | `01_add_simple_note` | H | PASS |  |
| 02 | `02_search_by_keyword` | H | PASS |  |
| 03 | `03_list_by_tag` | H | PASS |  |
| 04 | `04_list_recent` | H | PASS |  |
| 05 | `05_search_zero_results` | E | PASS |  |
| 06 | `06_ambiguous_reference` | E | PASS |  |
| 07 | `07_multi_turn_reference` | H | PASS |  |
| 08 | `08_delete_with_confirmation` | D | PASS |  |
| 09 | `09_delete_declined` | D | PASS |  |
| 10 | `10_update_nonexistent` | E | PASS |  |
| 11 | `11_reason_across_notes` | H | PASS |  |
| 13 | `13_malformed_tag` | E | PASS |  |
| 14 | `14_date_range_search` | H | PASS |  |
| 15 | `15_tool_loop_guard` | E | PASS |  |
| 12 | `12_contradiction_probe` | — | SKIP | Requires model reasoning judgment; no stable automated assertion. |
