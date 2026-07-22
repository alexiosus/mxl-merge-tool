# MxlToHtml external data processor

`MxlToHtml.epf` is the platform renderer used by the MXL merge UI. It is an
external data processor with an empty default managed form. The form module is
stored in `MxlToHtml.bsl`.

The processor is launched with `/Execute MxlToHtml.epf /C job.json`. A
single-document JSON job has this shape:

```json
{
  "inputPath": "C:\\Temp\\source.mxl",
  "outputPath": "C:\\Temp\\source.html",
  "statusPath": "C:\\Temp\\status.json"
}
```

The updated form module also accepts a batch job so one platform session can
render all three merge inputs:

```json
{
  "statusPath": "C:\\Temp\\status.json",
  "items": [
    {
      "name": "base",
      "inputPath": "C:\\Temp\\base.mxl",
      "outputPath": "C:\\Temp\\base.html"
    },
    {
      "name": "local",
      "inputPath": "C:\\Temp\\local.mxl",
      "outputPath": "C:\\Temp\\local.html"
    },
    {
      "name": "remote",
      "inputPath": "C:\\Temp\\remote.mxl",
      "outputPath": "C:\\Temp\\remote.html"
    }
  ]
}
```

The conversion is performed in server context because `SpreadsheetDocument.Read`
is unavailable in the thin client. `MxlRendererTemplate.dt` is the minimal
service infobase template restored automatically on first use; it is the same
verified startup template used by KOT. The generated file infobase remains on
the same machine so server context can access the input and output paths.

The checked-in EPF was rebuilt from the batch-capable module and verified on
2026-07-23. SHA-256:
`70ae14205f391cde144aabd291c5d993ecc2adc44de7112cf91900df8e4e15ad`.
The installer recognizes this build and enables `mxl.previewBatchCommand`
automatically. The known legacy single-document hash remains
`aa894caf035962974c1834fa8ae9e123a0f3f89182ce53dfc9ad8d1eae0a1e56`.

For another custom rebuild, open `MxlToHtml.epf` in the English Designer, replace
its default managed form module with the complete contents of `MxlToHtml.bsl`,
save it, and rerun `mxl_tool.py install --onec-client ... --onec-epf ...
--onec-batch-capable`. An arbitrary modified EPF cannot be identified safely by
its hash. As an alternative, place a UTF-8 sidecar named
`MxlToHtml.epf.batch-capable` next to the EPF with the exact contents
`mxl-merge-batch-v1`.

The bundled DT SHA-256 is
`587f2bb76e04b212938d00c2cda0004f0f7a08175abe94ed1b70f0067fd7cd19`.
