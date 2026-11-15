---
name: translation-glossary-builder
description: >-
  Build, review and maintain terminology for a book or book series. Use for
  glossary creation, conflicting terms/names or scoped glossary injection into
  local MT. Keep the approved master complete and select only relevant request
  entries without silently changing running jobs or invalidating their evidence.
---

# A complete term base and a small request glossary

Respect the user's glossary and approval workflow. In a pipeline project read
its SKILL.md and use its current glossary loader/selector; historical scripts
beside this skill must not overwrite that implementation. Do not automatically
create a glossary when the user/project has disabled that stage.

## Maintain one authoritative source

Use the established format. If starting fresh, a TSV with source, translation,
scope and decision note is convenient; derive the machine-feed form from it.
The pipeline accepts `source => перевод` lines and an optional JSON profile.
Do not maintain two independently edited masters or require a format migration
solely because this skill describes another project.

Include recurring names, author-specific terminology, domain traps and explicit
editorial conventions. Scope ambiguous words with phrases or context instead of
forcing one translation of every occurrence. Names generally need established
transliteration, not translation of surname meanings. Record uncertainty and
the source of an editorial decision; do not invent an authoritative convention.

Extraction of frequent capitalized words is only a candidate generator. Check
lowercase technical terms, accented/OCR-degraded names, exercise imperatives and
capitalized sentence starts. Automatic entry translation requires the user's
chosen model and local/remote policy. A finished glossary must cover the book's
actual terminology, not an arbitrary target number of entries.

## Feed only what the request needs

Keep the complete approved glossary on disk for consistency checks and leak
validation. The current pipeline selects source-relevant base terms plus
explicit critical entries and matching scoped groups. A master with many entries
does not imply that every entry should accompany every paragraph.

Example supported profile (paths and schema belong to the active project):

```json
{
  "schema": 1,
  "critical": ["Schwarz => Шварц"],
  "groups": [
    {"source_pattern": "(?i)power series", "entries": ["power series => степенной ряд"]}
  ]
}
```

Critical entries and a selected group are retained as declared. If their size
exceeds the request budget, review the profile or request layout; do not silently
drop critical terms to make the request fit. For fixed-template MT keep glossary
instructions in the configured auxiliary/system position, outside source payload.

## Changes during a translation

Review before the long run when requested and freeze the approved settings while
it runs. A changed glossary/profile can legitimately change request identities;
never rewrite hashes or relabel old translations as generated under new terms.
Preserve accepted caches and old master revisions. Estimate affected work from
the actual implementation, not a blanket promise that all or none will be reused.
For a necessary correction, record the decision, stop safely if required by the
active job and resume through its normal validation path.

After translation, compare terminology against this same master, including
scope, grammatical forms and deliberate exceptions. Do not force replacements
inside quotations, names, formulas or code. Consistent terminology is one quality
dimension; it does not establish complete or semantically correct translation.
