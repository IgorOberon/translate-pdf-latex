# Supported failure patterns

Use these as hypotheses after reading the first error, not unconditional edits.
Make changes in a new working candidate and retain the source and failed evidence.

| Observation | Check / bounded repair |
|---|---|
| `fontspec` cannot find a font, followed by `nullfont` cascades | Check the named font and configured engine; fix the first font error before downstream glyph errors |
| `Missing character` | Identify the source character and font; distinguish math glyphs, text and damaged mask tokens; preserve the character's meaning |
| `Undefined control sequence` around Pandoc output | Compare fragment versus standalone templates and the project's actual preamble; supply the required macro/package rather than replacing the whole preamble |
| `Shaded`, `Highlighting` or `NormalTok` missing | Use the matching Pandoc template/highlighting definitions, or the project's intentional no-highlighting mode |
| Table dimensions using `\real` fail | Check the project's `calc` support and the actual generated table expression |
| `Extra alignment tab` | Check column specification and the source table/formula; an extra `&` may represent lost structure |
| Multiple equation tags or pseudo-math escaped dollars | Compare with the source; do not arbitrarily retain only the last tag or remove math delimiters |
| Image missing or outside the page | Verify the exact asset path, dimensions and frame bounds; repair the derived layout, not the original book/image |
| Engine exit before a TeX log | Inspect stdout/stderr, runtime dependencies, disk and memory evidence; native interruption can resume, identical content failures need a fix |
| ToC destinations differ between passes | Continue within the supported pass limit and compare aux/ToC state; do not publish an unstable ToC |

Installing fonts/packages or changing an engine can affect other projects. Use
the already authorized environment where possible and identify necessary changes
before performing them. A diagnostic run never requires disabling shell safety,
rewriting provenance or cleaning accepted translation caches.
