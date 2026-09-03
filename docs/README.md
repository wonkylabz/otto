# docs

The REFERENCED documentation tier: read only by a session that follows a pointer from
`CLAUDE.md`, and deliberately outside `.claude/rules/` so no convention judge digests it.
See `maintaining-docs.md` for which tier a new rule belongs in.

- **`operating.md`** — setup, the background service, running, restart discipline, global pause.
- **`testing.md`** — the unit suite, the regression corpus, how to write a guard test.
- **`maintaining-docs.md`** — the three tiers, the rule format, and the enforced ceilings.

## Architecture diagram

`otto-architecture.html` is the editable source for the "Otto is the orchestration
layer" diagram (the revised version of the *Where are we going* slide). It's a
self-contained HTML page — no external assets, system fonts only — so it renders in any
browser and as a Claude artifact.

`otto-architecture.pdf` is the rendered single-page export shared with the team.

### Regenerating the PDF

The PDF is a build artifact of the HTML. To rebuild it after editing the source, wrap the
body-only HTML in a print document (sets a single tall page so the whole diagram stays on
one sheet) and render with headless Chrome:

```sh
cd docs
# wrap with a single-page @page rule (300mm wide ≈ the design's 1120px; 590mm tall fits the full flow)
{ printf '<!doctype html><html lang="en"><head><meta charset="utf-8"><style>@page{size:300mm 590mm;margin:0}html,body{-webkit-print-color-adjust:exact;print-color-adjust:exact}body{padding:30px 26px!important}</style></head><body>\n';
  cat otto-architecture.html; printf '\n</body></html>\n'; } > /tmp/otto-arch-print.html

google-chrome --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf=otto-architecture.pdf "file:///tmp/otto-arch-print.html"
```

If you change the diagram's height, adjust the `590mm` so it stays one page
(`pdfinfo otto-architecture.pdf` should report `Pages: 1`).
