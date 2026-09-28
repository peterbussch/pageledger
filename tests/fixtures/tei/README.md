# TEI All schema

`tei_all.rng` is the RELAX NG schema for all of TEI P5, version 4.12.0 (28 July
2026), taken unchanged from the TEI Consortium's release
[P5_Release_4.12.0](https://github.com/TEIC/TEI/releases/tag/P5_Release_4.12.0)
(`tei-4.12.0.zip`, SHA-256
`0195baccee3b1e3e4f75b21fcf3c7c71cbcd75c796678b6f8c33229da13f66c5`, path
`xml/tei/custom/schema/relaxng/tei_all.rng`). The TEI Consortium also
publishes that edition under <https://www.tei-c.org/Vault/P5/4.12.0/>.

It is the schema PageLedger's TEI export is validated against
(`tests/pageledger/test_export.py`). The tests check its SHA-256,
`b0f115095ead2ccc6933aa3365c6f4a82cba3b2ec7eee7f76bb616d7a63b7e48`, so a
changed copy fails rather than silently moving the target. To move to a newer
TEI release, replace the file from that release and update both hashes.

TEI material is available under both the Creative Commons Attribution licence
and the BSD 2-Clause licence, as the comment at the top of `tei_all.rng`
states. © TEI Consortium.
