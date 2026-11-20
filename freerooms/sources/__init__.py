"""Live data sources. Every module in this package that defines `SOURCES` (a list of
`live.Source`) is picked up automatically; no registration needed.

A source's `fetch(http)` returns (places, samples). Keep the parsing in pure functions
(e.g. `parse_places(raw)`, `parse_samples(raw, now)`) so tests can run them on saved fixtures.
Sources that only provide metadata return no samples; samples-only sources return no places.
"""
