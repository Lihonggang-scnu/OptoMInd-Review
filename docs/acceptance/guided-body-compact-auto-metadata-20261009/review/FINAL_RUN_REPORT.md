# Final run report

Run `20261009T143446Z-d113f9fe55` completed with 7/7 chapters and exit code 0.  The production run made nine Plus calls: seven author calls, one automatic metadata recovery call, and one bounded completer call.  No Max, critic, manual metadata declaration, or paid retry was used.

## Paid calls and saved chapter evidence

| Stage | Result | Prompt / completion / total tokens | Actual CNY | Profile |
|---|---|---:|---:|---|
| author_001 / Ch1 | body returned with future chapter todo; recovered and completed | 379820 / 6156 / 385976 | 1.667024 | Plus writer |
| metadata_author_001 | complete metadata decision | 5938 / 9 / 5947 | 0.0047936 | Plus, thinking false, output 2048 |
| complete_001 / Ch1 | complete, 3 bounded anchor insertions | 382512 / 8085 / 390597 | 1.724088 | Plus writer |
| author_002 / Ch2 | complete | 219789 / 6364 / 226153 | 0.515946 | Plus writer |
| author_003 / Ch3 | complete | 207950 / 6115 / 214065 | 0.489280 | Plus writer |
| author_004 / Ch4 | complete | 185401 / 6141 / 191542 | 0.444494 | Plus writer |
| author_005 / Ch5 | complete | 306254 / 6947 / 313201 | 1.391744 | Plus writer |
| author_006 / Ch6 | complete | 258227 / 6590 / 264817 | 1.191068 | Plus writer |
| author_007 / Ch7 | complete | 272484 / 4206 / 276690 | 1.190880 | Plus writer |

Every call has its `MESSAGES.json`, `REQUEST.json`, `ACTUAL_REQUEST.json`, `RAW_RESPONSE.json`, `RESULT.json`, and `USAGE.json` under `LIVE/stages/`.  Chapter BODY paths:

- Ch1: `LIVE/stages/author_001/47be7e22b5ff720e1d0f995795f678f363ed33890ab836f8c1baae006ee530dc/attempt_001/BODY.md`
- Ch2: `LIVE/stages/author_002/2af2f48e7a68a8b8fa9ae892ee0e5e2536f3aee44be4bd76f4e9431182e6f9b9/attempt_001/BODY.md`
- Ch3: `LIVE/stages/author_003/91eee61491f488e16f81be04d8f112d83ba92e728c1e4bb2c3b0864271a87189/attempt_001/BODY.md`
- Ch4: `LIVE/stages/author_004/b995d57956e84cb840b6d426921fbc748165b7d5bcb76b80cd617161c4b4a22f/attempt_001/BODY.md`
- Ch5: `LIVE/stages/author_005/c8a8e196a35945b70bab58ac7d25825d55761abfe16e04e3930ca8753de1bb61/attempt_001/BODY.md`
- Ch6: `LIVE/stages/author_006/b56751a8f394813e6767aa8efd24825055cb0b332ed100bfcc9c8be800506c10/attempt_001/BODY.md`
- Ch7: `LIVE/stages/author_007/64c910a3a897b68c48b39fd88d4fd623cb88438f2742fba7317c27a24293bba7/attempt_001/BODY.md`

The final assembled body is `LIVE/FULL_BODY.md`; its normalized UTF-8 text SHA-256 is `2f280164b8dca17cfeee6d2bf3c2f993398316f55deaa04e9c58662b71c29203`.

## Recovery and prefix checks

The first author response transported successfully but declared a remaining list containing Ch2–Ch7.  Automatic metadata recovery used the separate low-cost profile and preserved the author body byte-for-byte.  Production then issued one bounded completer call.  Its three returned insertions reconstruct the final Ch1 body exactly by the saved anchors; no Ch2 or Ch3 text entered Ch1.  This is recorded as an extra completion call, not as metadata-only recovery.

All seven author requests had exact accepted prefixes.  The full prefix and citation checks are in `records/FINAL_PREFIX_AUDIT.json`; call profiles, usage, response hashes, recovery reconstruction, and read-only budget aggregate are in `records/FINAL_OFFLINE_AUDIT.json`.

## Budget and source/citation statistics

- Run actual cost: `8.6193176 CNY`.
- Shared ledger settled: `41.0338328 CNY`; open physical spend: `0 CNY`; campaign cap: `44.4145152 CNY`.
- Canonical source identities: 221; explicit aliases: 3; unique nonempty DOI identities: 221.
- Semantic source supply union: 194; chapter source identity counts: 55, 32, 23, 23, 37, 28, 46 (Ch5 includes one extra supplied item beyond its task-bound count).
- Final citation occurrences: 409; distinct canonical cited identities: 117; unknown bracketed handles: 0.
- Supplied but uncited identities: 78.  Cited but outside the semantic supply union: `P0589`.
- Canonical identities never supplied: 27.

Citation-to-canonical/DOI/alias mapping and occurrence counts are in `records/FINAL_CITATION_AUDIT.json`.  These are offline identity statistics; no semantic quality conclusion is asserted here.
