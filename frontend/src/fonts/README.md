# Bundled web fonts

Self-hosted font binaries loaded by `frontend/app/layout.tsx` through
`next/font/local`. They replace the `next/font/google` declarations that
fetched Inter, JetBrains Mono, Outfit and Source Serif 4 from Google Fonts
during `next build`.

## Why local

`next/font/google` downloads the Google Fonts CSS and font binaries at build
time. Google intermittently answers with valid but extensionless
`/l/font?kit=...&skey=...&v=...` URLs. Next 16.2.11's Turbopack cannot parse
those and fails the build with `Can't resolve
'@vercel/turbopack-next/internal/font/google/font'` and `next/font/google
queries have exactly one entry` (vercel/next.js#99114). The webpack path fails
on the same URLs. Because the response is intermittent, the same Dockerfile
passes on one run and fails on the next. Bundling the authentic files removes
the font network dependency: `next build` works with network access disabled
after the image's package dependencies are installed.

`next/font/local` keeps self-hosting, hashed family names and
`font-display: swap`. Its size-adjusted fallback is computed differently from
the Google loader and defaults to Arial for every face, so Source Serif 4
declares `adjustFontFallback: 'Times New Roman'` to keep a serif fallback. The
three sans/mono families keep the Arial default, which matches what the Google
loader chose for their categories.

## Sources (pinned)

Every binary derives from the variable fonts in the
[google/fonts](https://github.com/google/fonts) repository at commit
`9710da1eacb3be272583c3224dcb70f9da6eadbb`. No Google Fonts API response
snapshots and no substitute families are used. Each family's `OFL.txt` from
that commit sits beside its binary (SIL Open Font License 1.1).

Raw URL prefix:
`https://raw.githubusercontent.com/google/fonts/9710da1eacb3be272583c3224dcb70f9da6eadbb/`
(URL-encode `[` `,` `]` as `%5B` `%2C` `%5D`).

| Upstream file | Bytes | SHA256 |
| --- | ---: | --- |
| `ofl/inter/Inter[opsz,wght].ttf` | 876,576 | `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031` |
| `ofl/jetbrainsmono/JetBrainsMono[wght].ttf` | 187,208 | `48715a42ec242c21e9f02692891e147d022299a52e48d5e413e1a942193ffeda` |
| `ofl/outfit/Outfit[wght].ttf` | 110,884 | `fc7287273e66929776e2ba54f144fe699080bec29f61bf649d70d871468aeade` |
| `ofl/sourceserif4/SourceSerif4[opsz,wght].ttf` | 1,209,508 | `97b2d4da6e3cb494b5a1e66ae176914d852ccabef49e0c02c0df25f3e39aca0b` |
| `ofl/sourceserif4/SourceSerif4-Italic[opsz,wght].ttf` | 855,432 | `15fbc7e4679489a501998c3669272637a6646388ef7e4bd77eebb5bf967a1f42` |

| License copy | Upstream | SHA256 |
| --- | --- | --- |
| `inter/OFL.txt` | `ofl/inter/OFL.txt` | `5b9321a4298cfeb6b34354164a1c3afc3db114569984c502b9b35d988fd58c57` |
| `jetbrains-mono/OFL.txt` | `ofl/jetbrainsmono/OFL.txt` | `b2fe5e8987594e9ffd1d2ca52a2f5d73eb8335243893c5d6254b5ad69269591d` |
| `outfit/OFL.txt` | `ofl/outfit/OFL.txt` | `c676351bf8576b9aba743cd5eaa8c0e7ee0d51f805d720447b4df4ddb6a2e416` |
| `source-serif-4/OFL.txt` | `ofl/sourceserif4/OFL.txt` | `5f94c3fd3a23131a417ab5a0c8452de57e70c3cfb9f604d88241f7065ebf9fd9` |

## Bundled files

Total 1,126,924 bytes across five WOFF2 files. No subsetting: every glyph and
every cmap entry of the upstream file is retained (full glyph coverage, Latin
included).

| Local file | Bytes | SHA256 | Axes (min / default / max) | Glyphs | Codepoints |
| --- | ---: | --- | --- | ---: | ---: |
| `inter/Inter-opsz14-VariableFont_wght.woff2` | 236,444 | `b33cb85c06adfd76d777d9de472c8a33b85154e2e7e686af467387e1e43ed494` | `wght` 100 / 400 / 900 (opsz pinned at 14) | 2,933 | 2,849 |
| `jetbrains-mono/JetBrainsMono-VariableFont_wght.woff2` | 71,736 | `7b7f3419196f675a973d30cb70078749120caddea86c8547ebf54a8db2ca13af` | `wght` 100 / 400 / 800 | 1,179 | 976 |
| `outfit/Outfit-VariableFont_wght.woff2` | 45,100 | `ed629d88c2db8ace1b3ac85fbf1fe0223d7272defaa817d1955100101966f0fb` | `wght` 100 / 100 / 900 | 416 | 360 |
| `source-serif-4/SourceSerif4-VariableFont_opsz-wght.woff2` | 427,556 | `4453d73855919eb67a8291bf2bad07782ecaf8af0e4e363d58948ff993209d51` | `wght` 200 / 400 / 900, `opsz` 8 / 20 / 60 | 1,463 | 918 |
| `source-serif-4/SourceSerif4-Italic-VariableFont_opsz-wght.woff2` | 346,088 | `a5e93099350c2768f7939fa51503641c961550830fb2a02083f5cf11adf227b5` | `wght` 200 / 400 / 900, `opsz` 8 / 20 / 60 | 1,093 | 918 |

Intermediate Inter opsz=14 instance TTF (not shipped): 634,520 bytes, SHA256
`5dc65936f548f56a3c5d11bdeed2299730e3e6c03fee021140c1768bbd35466d`.

### Declarations in `layout.tsx`

| Variable | File(s) | Declared weight | Style |
| --- | --- | --- | --- |
| `--font-inter` | Inter | `100 900` | normal |
| `--font-mono` | JetBrains Mono | `100 800` | normal |
| `--font-display` | Outfit | `300 800` | normal |
| `--font-serif` | Source Serif 4 normal + italic | `200 900` | normal, italic (`adjustFontFallback: 'Times New Roman'`) |

- **Inter opsz=14.** The previous Google declaration did not request the
  `opsz` axis, so Google served Inter's default optical size (14). The
  upstream file carries `opsz` 14..32, and browsers apply
  `font-optical-sizing: auto` to any face exposing the axis, which would
  change heading rendering. Instancing at `opsz=14` keeps the full `wght` axis
  and the previous appearance.
- **Source Serif 4** previously requested `axes: ['opsz']`, so it keeps the
  full `opsz` 8..60 range on purpose.
- **Outfit** asset carries the full 100..900 axis; the declaration restricts
  the CSS range to `300 800`, the six weights the Google declaration loaded.

## Preparation (deterministic, Python API)

Prepared with Python 3.12.3, fontTools 4.62.1, Brotli 1.2.0. The fontTools
instancer CLI recalculates `head.modified` by default (unless
`--no-recalc-timestamp` is supplied); the Python API below opens and saves with
`recalcTimestamp=False` and restores the upstream `head.created` /
`head.modified` values after instancing. Running the process twice produced
byte-identical output and identical manifests.

Steps per file:

1. Verify the upstream TTF's SHA256 against the table above.
2. `TTFont(source, recalcTimestamp=False)`; record glyph order, every cmap
   subtable and the `head` timestamps.
3. Inter only: `instantiateVariableFont(font, {"opsz": 14}, inplace=True)`,
   then set `font.recalcTimestamp = False` and restore the recorded
   timestamps. Assert the remaining axes are exactly `wght` 100/400/900.
4. Set `font.flavor = "woff2"` and save. No subsetting options are applied.
5. Re-open the WOFF2 and assert: identical glyph order, identical cmap
   subtables, identical `fvar` axes, identical `head` timestamps, every table
   other than `head`/`glyf`/`loca`/`DSIG` byte-identical to the source, and every
   decoded glyph equal to the source glyph (WOFF2 re-encodes `glyf`/`loca`
   reversibly). `DSIG` is dropped by the WOFF2 conversion where present
   (JetBrains Mono, both Source Serif 4 files); a signature cannot survive a
   container change and has no rendering effect.
6. Copy the family's `OFL.txt` beside the output.

Reference implementation:

```python
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

def prepare(source, output, *, inter=False):
    font = TTFont(source, recalcTimestamp=False)
    timestamps = font["head"].created, font["head"].modified
    if inter:
        font = instantiateVariableFont(font, {"opsz": 14}, inplace=True)
        font.recalcTimestamp = False
        font["head"].created, font["head"].modified = timestamps
    font.flavor = "woff2"
    font.save(output)
```

Changing any binary requires re-running this process with the pinned tool
versions and updating every hash, byte count and axis row above. If the
upstream commit moves, update the pin at the top of this file as well.

## Rules

- Keep the files under `frontend/` so `Dockerfile.prod`'s `COPY frontend/`
  and Next's output file tracing include them.
- Do not reintroduce `next/font/google` in `layout.tsx`; the build must stay
  offline-safe.
