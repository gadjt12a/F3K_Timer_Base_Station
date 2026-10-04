# Handover sheets

Three one-page A4 PDFs for someone evaluating the system on a loaned rig. Each is
written to stand alone on a clipboard — no sheet assumes you have read another,
beyond a pointer back to Sheet 01 for "is it powered up".

| Sheet | Covers |
|---|---|
| `01-hardware-setup` | What is in the kit, power-on order, the three networks, audio, the watch's two buttons, what not to touch |
| `02-load-competition` | Exporting `GliderScoreData.mdb`, importing it, the audio pack, why the structure locks, building a comp by hand |
| `03-run-competition` | The screens, the pre-flight checks, the heat cycle, the watch during a round, fixing mistakes, results back to GliderScore |

## The credentials card

**The sheets deliberately do not print the WiFi passwords or the SSH login.** They
say "on the card supplied with the kit", so the kit needs one — a slip of paper
with the `F3K_BASE` / `F3K_OPS` passwords and the `pi` login. Hand it over with
the hardware, not with the documents, and it comes back with the rig.

## Rebuilding the PDFs

The sheets are authored as HTML and rendered with headless Chrome. After editing
any `.html` or `sheet.css`:

```powershell
.\render.ps1     # re-renders all three PDFs, and fails if any is not one page
.\measure.ps1    # says by how much a sheet overflows, while you trim it
```

`render.ps1` is the one that matters: it reports the real page count. Treat
anything other than `1 page` as a bug in the sheet.

`measure.ps1` is a trimming aid, not an oracle. It makes the page report its own
rendered height through the PDF's title metadata, because this box's headless
Chrome has no working `--dump-dom`. Its `OVER` numbers are reliable and tell you
how much to cut; its `FITS` numbers saturate at exactly one page height, so a
sheet reading `0 px spare` may still paginate. Always confirm with `render.ps1`.

## Two layout rules worth knowing before you edit

- **No `break-inside: avoid` on `section`.** On a one-pager it cannot help — if
  the content fits, nothing breaks — and it actively hurts: a sheet that very
  nearly fits gets a whole section thrown onto page two instead of being allowed
  to sit tight.
- **A step's lead-in label is opt-in: `<b class="lead">`.** It was once
  `b:first-child`, which also matches a `<b>` with text in front of it — so
  `press <b>Connect</b>.` became a line of its own with the full stop stranded
  underneath it.

A `.cols` row is as tall as its tallest column, so trimming the short side of a
two-column block saves nothing. Check which side is tall before cutting.
