# Historical binding coverage audit — Review-2 C6

## Result

`historical replay incomplete`.

The preserved C6 packet and arrangement describe four units. The preserved
`recordings_real_03.json` has one exact arrangement response covering all four
units, and exact writer recordings for `C6_U01`, `C6_U02`, and `C6_U03` only.
There is no `writer:C6_U04` recording, writer result, body, or raw response.
The existing delivery report therefore correctly records `original_units=4`,
`assembled_units=3`, and `pending_missing_recording` for `C6_U04`.

## Inputs and exact source hashes

The supplied inventory is
`D:\OptoMind-Article-3\local-preparation\old-Review-2-files.csv`
(142,980,688 bytes; SHA-256
`3e89c17802db00f2777b77dae95e6a9459a206f63bcd8c5c811d37144192b95c`). Its
rows establish the old paths and sizes, but the `raw_sha256` cells for these
old-only rows are blank. Hashes below were computed directly from the
preserved `F:\OptoMind-Review-2` files.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/UPDATED_WRITER_PACKET.json` | 637,390 | `1c3470ff492624193838da157ada1b4ba0034cb30aabddb602ba623804997e64` |
| `outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/PRECASE_PACKET.json` | 681,432 | `2c0a06b6b5dbfe749d041df1c32859528d5cd3c7daaa7e70d5cf787d63e2e307` |
| `outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/offline_UPDATED_PACKET.json` | 681,432 | `2c0a06b6b5dbfe749d041df1c32859528d5cd3c7daaa7e70d5cf787d63e2e307` |
| `outputs/review_v2_delivery/plan/recordings_real_03.json` | 21,051 | `2dd39b4cd80ce9c1ec96726e4755fa95d214309a61ef97cbb7caed706d90f47e` |
| `outputs/review_v2_delivery/plan/ID_MAP.json` | 525 | `392141241bf56e7b2a8dc37b4ba247fdc369ec1418cc74844a4d2ab5ad6f7966` |
| `outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/ID_MAP.json` | 525 | `392141241bf56e7b2a8dc37b4ba247fdc369ec1418cc74844a4d2ab5ad6f7966` |
| `outputs/review_v2_repair/05_real_test_20260929/attempt02_production_path/offline_ID_MAP.json` | 525 | `392141241bf56e7b2a8dc37b4ba247fdc369ec1418cc74844a4d2ab5ad6f7966` |
| `local-integration/20261013/HISTORICAL_ID_CONFLICT.md` | 2,184 | `46015b19c6369608973567744280d643d5db6b2c1af4b4ca99279dc66dafcbe4` |
| `local-integration/20261013/history-inputs/COPY_MANIFEST.json` | 1,015,426 | `13196378b33e7c111d61c6e5092f1805c81a71ebaa8cd494245a07dcc8611027` |

The copy manifest preserves the original packet hash above and records a
different relocated packet hash
(`e8f81cbda45b8462e1bd39957f6eb9022dcc01b27f74556ef6259fc14eb475f8`), so
the audit uses the original F-drive bytes. The recordings original and
relocated hashes are both `2dd39b4c...f47e`.

## Packet and arrangement coverage

`UPDATED_WRITER_PACKET.json`, `PRECASE_PACKET.json`, and
`offline_UPDATED_PACKET.json` each contain chapter `C6` with packet unit IDs
`U1`, `U2`, `U3`, `U4`. Their paragraph brief counts are respectively 2, 3,
3, and 2. The delivery arrangement contains explicit IDs
`C6_U01`–`C6_U04` and ten paragraph IDs:
`C6_U01_P01/P02`, `C6_U02_P01/P02/P03`,
`C6_U03_P01/P02/P03`, and `C6_U04_P01/P02`.

The `arrangement:C6` entry in `recordings_real_03.json` is complete and its
recorded response contains all four explicit unit IDs and all ten paragraph
IDs. This establishes arrangement response coverage, but it does not create
writer response bytes for any unit.

## What ID_MAP binds

All three old ID_MAP copies are byte-identical and contain only
`schema_version`, `units`, and `paragraphs`:

```text
C6#u1 -> C6_U01       C6#u2 -> C6_U02
C6#u3 -> C6_U03       C6#u4 -> C6_U04
C6_U01#p1 -> C6_U01_P01   C6_U01#p2 -> C6_U01_P02
C6_U02#p1 -> C6_U02_P01   C6_U02#p2 -> C6_U02_P02
C6_U02#p3 -> C6_U02_P03
C6_U03#p1 -> C6_U03_P01   C6_U03#p2 -> C6_U03_P02
C6_U03#p3 -> C6_U03_P03
C6_U04#p1 -> C6_U04_P01   C6_U04#p2 -> C6_U04_P02
```

The map's SHA-256 proves the bytes of that map file, and the map preserves
the symbolic/explicit labels shown above. It contains no packet hash,
arrangement hash, message hash, writer-response hash, source-content hash, or
semantic owner assertion. Its legacy signatures are positional: `C6#uN`
uses the plan unit index, and `#pN` uses the paragraph ordinal within the
mapped unit. This is lineage by position/ordinal, not cryptographic binding
of a packet unit or response to a source owner. `ARRANGEMENT_INPUT.json`
records packet and map paths, but those path strings add provenance only; they
are not content digests.

## Message and response provenance

The canonical prompt digest is SHA-256 of
`json.dumps([dict(message)...], ensure_ascii=False, sort_keys=True)` encoded
as UTF-8, matching the historical replay implementation. The preserved
delivery message files and their recorded digests classify as follows:

| Step | Raw message-file SHA-256 | Canonical `messages_sha256` | Recording / writer result |
| --- | --- | --- | --- |
| `arrangement:C6` | `b7d5e685abc046bc3e1158ec3ee989f39f0e4030cab0125c18eec4a619349389` | `c555dfc99a0e437ce938c1867c6e8c0b14fb46739bc3ce67b29965cec5885232` | present; exact; four-unit response |
| `writer:C6_U01` | `427b02623a7dfffd122402380c8f307fea7766be91bcb3f3778324be62616071` | `4bb7261e79a0b8c4667668ef0c3fe5531bca1dd205d4934c4051e5a3eec88822` | present; exact; complete result/body/raw response |
| `writer:C6_U02` | `8ad3914a247654cbd014be91f98be73581ac7aa4c5d4c9fa4ea1cb3193bfd601` | `f6c2472acf47190dbdedd422b7272fab98ade0dfbd2b1ac8437cb0395028b5b5` | present; exact; complete result/body/raw response |
| `writer:C6_U03` | `865f2a2ed1476ebd0a08782ac98812e9623633cadf5e85931c4019522d09e176` | `7a0c7d9548baf180a894bd258fe3e9523c34a86298b53f21dc3b5675afb4f636` | present; exact; complete result/body/raw response |
| `writer:C6_U04` | `f0d07d8bf7e43d0a5321c8994021df657d951dde1654ed24a7303edf0e4746b6` | `b675a45d4a9c693df96432014862275758691598fa695ec2e4c1ea04265165f2` | no recording key; no result/body/raw response |

The U4 canonical digest is computed from the preserved U4 message bytes; it
is not a recorded response binding. The five delivery-plan message files
(arrangement plus U1–U4) are byte-identical to their
`outputs/review_v2_delivery/06_integration/plan/messages` counterparts.
Their U1–U3 writer result artifacts are also present in both delivery
provenances; U4 has only `UNIT_INPUT.json` and `UNIT_MESSAGES.json`.

`recordings_real_03.json` has exactly these recording keys:
`arrangement:C6`, `writer:C6_U01`, `writer:C6_U02`, and `writer:C6_U03`.
There is no `writer:C6_U04` key. The plan delivery report (SHA-256
`bac97b77d6317012e47754e128730b93c69a186ccbfd7fd208097b50cff9dc86`)
records `original_units=4`, `assembled_units=3`,
`missing_units=["C6_U04"]`, and `pending_missing_recording`. The historical
consumer copy reports the independent arrangement failure
`unit_id_unknown:C6_U01` through `C6_U04` and
`units_missing:U1,U2,U3,U4` (SHA-256
`d80c3af442ee0c5172b0ea124747b8ba6a3123c9b0410d0ae0ba9dc038fec6f2`).

## Conclusion and limits

The map can normalize the old symbolic IDs to explicit labels, and the
recorded message digests can authenticate the exact arrangement/U1–U3 input
messages. Neither establishes a source-bound, one-to-one replay for all four
writer units. Mapping by order would invent the missing U4 response; a fourth
response cannot be synthesized from the arrangement response or from U4
message bytes. Therefore the correct status is **historical replay
incomplete**.

This audit found no evidence of a fresh-request defect. No fresh request was
reproduced; the observed failure is the historical consumer's ID/recording
coverage conflict only.

## Changed / Verify / Blockers

**Changed:** only this report and the accompanying machine JSON were written
under `D:\OptoMind-Article-3\local-functional-trial\20261013\history-audit`.
No production files, old packet, ID map, recordings, messages, database, or
model/API state was modified.

**Verify:** direct SHA-256 checks above; canonical message hashes match the
four recorded digests where recordings exist; byte comparison confirms the
delivery-plan/content-handoff message pairs; packet and arrangement unit
inventories were read from preserved JSON; writer response presence was
checked by recording keys and artifact files.

**Blockers:** obtain a source-bound `C6_U04` writer recording (and an explicit
migration receipt binding packet, old messages, ID_MAP, and response hashes)
before claiming complete historical replay. Do not remap IDs by order or
relax the explicit-ID validation boundary.
