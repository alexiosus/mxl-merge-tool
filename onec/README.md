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

The checked-in EPF was supplied and manually verified on 2026-07-16. It is the
legacy single-document build; SHA-256:
`aa894caf035962974c1834fa8ae9e123a0f3f89182ce53dfc9ad8d1eae0a1e56`.

To enable batch rendering, open `MxlToHtml.epf` in the English Designer, replace
its default managed form module with the complete contents of
`MxlToHtml.bsl`, save the external processor, copy the rebuilt EPF over this
file, and rerun `mxl_tool.py install --onec-client ...`. The installer leaves
the legacy hash on the three-process fallback and enables
`mxl.previewBatchCommand` for the rebuilt processor.

The bundled DT SHA-256 is
`587f2bb76e04b212938d00c2cda0004f0f7a08175abe94ed1b70f0067fd7cd19`.
