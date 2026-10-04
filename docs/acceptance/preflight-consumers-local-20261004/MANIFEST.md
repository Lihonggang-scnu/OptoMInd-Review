# MANIFEST

SHA-256 manifest for the public handoff. `MANIFEST.md` is excluded from its own rows.

| relative path | purpose | source | bytes | sha256 |
| --- | --- | --- | ---: | --- |
| `ASTRA_ACCEPTANCE.md` | root verdict | parent ASTRA_ACCEPTANCE.md | 5960 | `75f36fff29f886f6fb4bc12d2977dc5f4d49974a827604280a0664f01780ddce` |
| `COMMANDS.md` | local reproduction and review commands | derived from parent acceptance records | 1364 | `ac5a15bf32667ecc05971f222356aaa77c3e80aa5979b94c250e763aa108fb88` |
| `LOCAL_OFFLINE_ACCEPTANCE.md` | offline controls and selected recovery evidence | parent records/LOCAL_OFFLINE_ACCEPTANCE.md | 6423 | `a9f2f5fea82f8fe1db384b328cd33ac60728282b2f995e01583c0b4309d1fa6e` |
| `LOCAL_REAL_PROBE.md` | actual F1/F3 result and limitations | parent records/LOCAL_REAL_PROBE.md | 3841 | `d35b434931df13051fb5f9fe71f3ea5062f9c9ca43ad9d59f93b0f464cda19da` |
| `offline/F1_BLOCKED_CONTRACT_FAILED.json` | selected F1 blocked-run evidence | derived from records/local_replay/f1 | 1054 | `700ef8df1c0e04b6abbb3507693d62b863cec54aba371b63103a90da3be42815` |
| `offline/F3_LOCAL_RETRY_PARTIAL.json` | selected F3 local partial evidence | derived from records/local_replay/f23 | 2080 | `352d03cadfa92deed124438133cd3a837d8296979d142c65ddc50a91d204860f` |
| `pack_public.py` | local manifest and safety checker | new packaging utility | 6395 | `cb999071661779e01063037a166c1b4cb700ffa4487dab6e2ec56623c2171bd1` |
| `PROVENANCE.md` | source and omission boundary | derived | 1281 | `af3ac8ac4a4c4e3b7b8bf4734b1d3e45fe1171871e01b6cb490764dd6bc6a9ce` |
| `README.md` | reading order and scope | derived from parent acceptance records | 3232 | `cb6bfe85d0787a5ea38a583812ec6367426ab9b7ce5701d86e33c60cf99f1d32` |
| `real_f1/MESSAGE_COMPARISON.json` | writer-message hash comparison | derived from F1 message files | 464 | `a6fc73a7d35e996085e8ac677ce0e526dac38844eeff9e4babbe78033739813f` |
| `real_f1/RESULT.json` | sanitized F1 result | runs/real_probe/f1_cache_resume/RESULT.json | 1340 | `cc3170394c0dced1a38ffed603bee31c77dc11907bdc22d6057c639f702442d0` |
| `real_f1/STAGE_METADATA.json` | F1 production entrypoint metadata | derived from PRE_FLIGHT.json and F1 result | 897 | `023fc96f315d65a9cc990c5d412d57e5def5b2c0963eacae33849e70abe6d3f5` |
| `real_f1/TASK_COMPARISON.json` | fresh/resumed task comparison | derived from F1 result | 1431 | `6406067f37f1a1dcd7aed550d9015cc74228a3dbc34b45874b8229e051ed9ad9` |
| `real_f3/BOUNDARIES.md` | content quality limits | root review and actual F3 output | 1069 | `571717522152d846a1214efd16c9498d0a271de829892896a90927247807482e` |
| `real_f3/COMPACT_SUMMARY.json` | sanitized compact handoff summary | derived from FORMAL_COLLECTOR_RESULT.json | 709 | `fa2372d9ba845031cc9c004a83d19b51363429b518c1e8902a5341a8f1769fcd` |
| `real_f3/COMPACT_TOOL_FEEDBACK.json` | actual compact tool-feedback projection | derived from COMPACT_TOOL_FEEDBACK.json | 18852 | `11309afedc5157ac1eaa93d4fec08b1a6fdca824946ab22b13a0819a7e9f2fef` |
| `real_f3/CONFIG.json` | sanitized real F3 configuration | derived from PRE_FLIGHT.json | 1206 | `0900b218b0eed3a4362bbcb7dc2a9fbe5871aef14777bb6cd93425584156caf2` |
| `real_f3/COST.json` | actual shared-ledger cost summary | derived from F3 RESULT.json | 792 | `16a851d82eb57b53799c7638ef67e5404425fb704a8c366a40b08fae5598e484` |
| `real_f3/INPUTS.json` | sanitized matching-input inventory | derived from PRE_FLIGHT.json | 1013 | `cdfe1dae850c9a4987129531c5417789050d93b492a8d682ae86c65a01dee071` |
| `real_f3/LEVEL1_PAYLOAD_PROJECTION.json` | question, provisional scope, and tool-feedback projection | derived from LEVEL1_OUTLINE_PAYLOAD.json | 23037 | `e4ba0cf5ff5f5d123a5660a724436eb017ebb3d6cf233e4e95bc152a9794dc6a` |
| `real_f3/OUTLINE_RESPONSE.json` | raw provider-generated outline response | derived from LEVEL1_OUTLINE_RECORD.json | 12144 | `5518c890c4201969495879feb87581ab4879c95b4acf705e3d91097083f7cfe3` |
| `real_f3/OUTLINE_TELEMETRY.json` | sanitized outline telemetry | derived from LEVEL1_OUTLINE_RECORD.json | 843 | `0443fc0a7962e4468a074fdf737087ca58f32cf59ddaeeccce79ac406bd4160d` |
| `real_f3/PAYLOAD_KEYS.json` | actual stage payload shape | derived from LEVEL1_OUTLINE_PAYLOAD.json | 778 | `678fe22ed86ced2a5d8669d44471a66826496411faf9845ef38773d2dbf5f093` |
| `real_f3/STAGE_MESSAGES.json` | safe actual message metadata | derived from production _messages_for output | 664 | `e1bfe20f0b160fadfd889c6b3dc1c5a60a95be2d2ef206c83e21838633a43ae1` |
| `real_f3/SYSTEM_MESSAGE.md` | actual outline system message | derived from production _messages_for output | 4496 | `36cfe9d31af67ad01a72025bf25351b82cc3e788203b5112d193f2d6ce8c17ee` |
| `real_f3/TOOL_RESULT.md` | AI-generated local answer projection | derived from FORMAL_COLLECTOR_RESULT.json | 16799 | `8b357650005a67761a9a2b41eb0438995143ee9899bb81f85b45dd1696ea4441` |
| `SECURITY_SCAN.md` | public package scan statement | derived | 1139 | `564d0bc716865afbb1091eda94a3c155f32628339124b5f154961cfc552704d0` |
