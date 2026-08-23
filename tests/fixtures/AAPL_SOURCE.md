# AAPL companyfacts fixture

`aapl_CIK0000320193.json.gz` is a gzip-compressed, byte-for-byte copy of a real
SEC companyfacts response for Apple Inc. It is compressed only to keep the test
suite small. Python's standard `gzip` module reads it directly.

- SEC endpoint: `https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json`
- Retrieved from: `https://github.com/mihir-gonsalves/OpenValuation`
- Source commit: `300147ae79c40768a356aa02f798e8b4b39815e2`
- Source path: `backend/tests/fixtures/aapl_CIK0000320193.json`
- Source retrieval date: `2026-08-21`
- Uncompressed SHA-256: `31f9ab4398402faabc733178497af89dbf94dd5038c6e36d4c894317de8a4647`
- Compressed SHA-256: `9ae1c5b27195dd52bcc7174462532420a5359363b9f74d7043bc9dc0e7458957`

The regression is pinned to information filed by `2025-11-01`, one day after
Apple's 2025 Form 10-K. Later facts in the frozen response cannot enter because
all resolution is point-in-time by `filed` date.

The source repository describes these fixtures as real SEC EDGAR fixtures and
is MIT licensed. Its license notice is preserved in
`OPENVALUATION_LICENSE.txt`. The underlying filing data is from SEC EDGAR.

Official filing used for hand checks:
`https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm`
