# Shadowfetch Linux 5.0.0: evidence redactions

Some QA evidence printed absolute paths on the build host, which include the
publisher's home directory. Before publication, every literal occurrence of that
home directory in the files below was replaced with `~`, and the build host's
hostname recorded in harness receipts with `<build-host>`. Nothing else was
changed. The unredacted originals are kept privately by the release owner; their
SHA-256 is listed so a reviewer given the originals can confirm the mapping.

| case | evidence file | occurrences | original SHA-256 | published SHA-256 |
|---|---|---|---|---|
| SRC-01 | `gates/source-gate-2d8a.log` | 18 | `c394ad31158133241b79b753238ed6ffc5429e17fe225737f5c5774625497b64` | `fa28f66c167ad8e014699912ebca8a314eef563b0958dda1823126a54f9a6880` |
| PKG-01 | `gates/package-gate-2d8a.log` | 16 | `83e0db35e24ac02f20f75df36356dc4f813584e82d8f9b5750b57bb55086a21b` | `d4a2a700d8fc6894c9ca671cb4ecf38bcdbb8fe61522434ff235b85aa859a3ad` |
| ISO-01 | `gates/iso-gate-2d8a.log` | 1 | `4fd21c44784a60c2e45f3d03d4a4d5b0046e9552a21c6d66412d221755628d18` | `551681bad0071381e7086d7794cbcef483dc3a61e07d63ff758aa249ff489644` |
| FIRE-01 | `qa-harness-5.0.0-2d8a/guest-identity.txt` | 3 | `ed13a644317cdf65607e4609e209e5b4dc132ddc1a310385eadec76cafe5b827` | `d336b5615211cd5fe16befdb02ff1a2aef64c3926f34b0c8fe409fb93ba90195` |
| INSTALL-01 | `vm-acceptance/install-both-firmwares-20260930T195620Z-2d8a72e044e8/install-both-provenance.json` | 2 | `930e70cfbb9a2d58d192e6902597bc84baa10a96585c38da04cb436c91653371` | `343c3d362f48547d252576d8b4ed3a41c8e37a59d0c8ed5a0096e8c00e932afe` |
| INSTALL-01 | `vm-acceptance/install-both-firmwares-20260930T195620Z-2d8a72e044e8/install-both-bios-base.json` | 1 | `e60520d903cf8d4dc12aa7e0c1eba8e7f138cd837c9e89cf46253e70be69933b` | `e2184fb005e094bb1d855656da6b05c8fa5dc4bb9c1ce89fdd8f8bac3b800f71` |
| INSTALL-01 | `vm-acceptance/install-both-firmwares-20260930T195620Z-2d8a72e044e8/install-both-uefi-base.json` | 1 | `45a1874ed7413a630abab173f93db350a5bd62e683e9457325b8121c91b6fac6` | `a6bf3f9e2ace344786e42fa725130feb963e4d51b86188efebb11a882049fb0f` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/harness-upgrade-case.log` | 5 | `6a18d2350e8405b57a1ffea5837e44dbc87ebca7bdd4892ea196a6908894cd81` | `080252732f0f7d3d50df723ccf022be5f77906c049c2538f113e1c5ed5b6d5c1` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/harness-receipt.json` | 3 | `ba31a1299cee858168a509c79817c093ac1f55bb973a5ec70cc9421cfed47148` | `c4bbb01393b233fec653df2241dac2e27f65623f994107631ef023d3813aadd0` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/verify_upgrade_u01.sh` | 1 | `86fbf25902e483b5b21b65cb2aa1a6589f25be0ab251e00410b1624d1d66db1d` | `fe3b688f09078944ff20301ed149333d51392609debd6b2468d78b22e77d7d39` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/extra_checks.sh` | 1 | `c7e11a9c5db96d08b8c564c4d780d29dd11887460d26ce75a10a6b5defad34c0` | `c9275ce8ecc5d3f87e924c537c3f1e72a6ac8984de9059fd03e4e61759006c62` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/login_shots.sh` | 1 | `1b8c730d73fb857e43507b33f8a54bbc8f25ea4fb57edc06027336dda4be6562` | `c9aaa140f3dbf1961ca60baa5e7f34f9c18c05d81edd2669482cf468b93090a4` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/verify-1/verify.log` | 2 | `a413dcfd0f6272a1f069b4e8ffac5feea868cd03568bd5192873d29a6bd4ff78` | `429ac577500f35971902cad1bcd7c948438110160e858d4565b3b526afbc4c4d` |
| UPGRADE-01 | `vm-acceptance/upgrade-20260930T193737Z-2d8a72e044e8/upgrade-base-provenance.json` | 1 | `fdfcab90c912407404802dc46bccba838e46f01a65ac75b059345bfb8f137904` | `fdbf25d8497fee20725ed1d89b217b8be7f6c32154d2aa7b587dba412220f308` |
| RECOVERY-01 | `vm-acceptance/recovery-project-20260930T195923Z-2d8a72e044e8/project-base-provenance.json` | 1 | `e60520d903cf8d4dc12aa7e0c1eba8e7f138cd837c9e89cf46253e70be69933b` | `e2184fb005e094bb1d855656da6b05c8fa5dc4bb9c1ce89fdd8f8bac3b800f71` |
| RECOVERY-01 | `vm-acceptance/recovery-project-20260930T195923Z-2d8a72e044e8/recovery-project-transcript.log` | 1 | `970e11bcea39434ec551df442f366ef1bd52334acc023a906ce9bf6d3c6d3071` | `f2ed763d213035cbcc7e20cbf39994539a5dce619398111ab012494b7cd63184` |
| STRESS-01 | `stress-5.0.0-2d8a/run1-20260930T214006Z-FAIL/run.log` | 4 | `1ffe3b5e62e38b60914de36cd64193f3956609d8aa799ddde087506d2d2d99e4` | `095ff28e9fcca4b36cefa1558d699e2dd498f2a395a3d94e1150f2a5087da64e` |
| STRESS-01 | `stress-5.0.0-2d8a/run2-20260930T230033Z-FAIL/run.log` | 4 | `cc5d5cda44c22d47bc4a7938f128035ce8e17472d1e35a9a1e676fe6c33e71d0` | `ef30b459b51b5a2e31ccce4c6da9e19006f8bb27be3d9221ef285f7b268d3c31` |
| UPGRADE-01 | `upgrade-5.0.0-2d8a/harness-receipt.json` (hostname) | 1 | `c4bbb01393b233fec653df2241dac2e27f65623f994107631ef023d3813aadd0` | `77a57aa39a088f99c26a9df5df2734110e008a82f2efc372fd5bbed749dc468c` |
