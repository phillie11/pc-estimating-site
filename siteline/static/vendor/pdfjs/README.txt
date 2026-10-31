pdf.js (Mozilla), self-hosted for the drawing viewer (viewer/static/viewer/viewer.js)

Package:   pdfjs-dist 4.10.38 (published to npm 2025-01-01)
Integrity: sha512-/Y3fcFrXEAsMjJXeL9J8+ZG9U01LbuWaYypvDW2ycW1jL269L3js3DVBjDJ0Up9Np1uqDXsDrRihHANhZOlwdQ==
Licence:   Apache License 2.0, in LICENSE. The standard fonts carry their own licences
           (standard_fonts/LICENSE_FOXIT, standard_fonts/LICENSE_LIBERATION).

Files, copied unchanged from the package apart from the file extension:
  pdf.min.js          <- legacy/build/pdf.min.mjs
  pdf.worker.min.js   <- legacy/build/pdf.worker.min.mjs
  standard_fonts/     <- standard_fonts/ (for PDFs that don't embed Helvetica, Times and so on)

Why the legacy build: the modern build needs Safari 17.4 or later (Promise.withResolvers), and
site phones and tablets are often older.

Why .js and not .mjs: some static file servers (older nginx, as on PythonAnywhere) don't send a
JavaScript content type for .mjs, and browsers refuse to run a module served as anything else.

Content-Security-Policy: nothing extra is needed. The worker loads from this site, which
script-src 'self' (and so worker-src) already allows, and the viewer passes
isEvalSupported: false so pdf.js never tries new Function().

To upgrade: `npm pack pdfjs-dist@<version>`, check the tarball against
`npm view pdfjs-dist@<version> dist.integrity`, copy the same files over, then run
`manage.py test viewer` and look at a drawing in the viewer with the browser console open
(no Content-Security-Policy errors, the page draws).
