# Harmony parser: human guide

The parser lives at:

```text
scripts/harmony_parser.py
```

It uses only the Python standard library.

## What it is for

The `.harm` file is the human-edited source. The parser reads that source once
and returns a normalized Python object. Other programs should import the parser
rather than independently reimplementing `.harm` syntax.

```text
.harm
  |
  v
parse_harmony()
  |
  +--> chart renderer
  +--> MusicXML generator
  +--> corpus analysis
  +--> validator
  +--> website/backend
```

JSON is available as an inspection/interchange form, but the parser does not
need to write JSON during normal Python use.

## Command-line use

From the repository root:

```bash
python scripts/harmony_parser.py tests/fixtures/bm01.harm
```

This prints normalized JSON to the terminal.

To save JSON:

```bash
python scripts/harmony_parser.py tests/fixtures/bm01.harm -o bm01.json
```

## Importing it from another Python script

```python
from scripts.harmony_parser import parse_harmony

harmony = parse_harmony("data/bm01-blue-moon-of-kentucky/bm01.harm")

print(harmony.tune_id)
print(harmony.form_sections)
print(harmony.sections["A1"].bars[0].events[0].chord.root)
```

The result is a hierarchy of dataclasses:

```text
HarmonyData
  Section
    Bar
      ChordEvent
        Chord
```

## Timing model

Onset positions are exact `fractions.Fraction` values. For `(1 . . 4)`, the
second event has:

```python
position == Fraction(3, 4)
```

The `Bar.subdivisions` field is also retained. This matters because `(1 .)` has
a two-slot source grid even though its only onset is at zero.

## Chord model

A parsed chord such as `6-7` contains, among other fields:

```text
raw             "6-7"
root            "6"
root_degree     6
root_alteration 0
quality         "minor"
modifiers       ["7"]
bass            "6"
bass_degree     6
bass_alteration 0
bass_explicit   false
```

`raw` is deliberately retained even though the token has been parsed. This
makes later format changes easier to audit against the source transcription.

## Section aliases

If a later formal section has exactly the same harmony as an earlier one, use
source-level alias syntax instead of copying the bars:

```text
V:
1 67 27 27
57 57 1 1

C = V
```

The parser materializes `C` as its own `Section`, copies `V`'s bars into it, and
sets:

```python
harmony.sections["C"].alias_of == "V"
```

This also works for numbered variants such as `A3 = A2`. Alias targets must
appear earlier in the file. Ordinary sections omit `alias_of` from normalized
JSON, so existing expected-output fixtures remain unchanged.

## Running tests

From the repository root:

```bash
python -m unittest discover -s tests -v
```

The tests cover:

- `57` as major triad + `7` modifier;
- minor quality (`6-7`);
- altered roots (`b7`);
- slash basses;
- compact compound modifiers (`594`);
- two-, three-, and four-slot within-bar timing;
- delayed first onsets;
- whole-bar continuation dots;
- A1/A2/B/A3 form resolution;
- reuse of a single `A:` section;
- section aliases such as `C = V` and `A3 = A2`;
- rejection of forward/undefined alias targets;
- rejection of incomplete numbered form variants;
- rejection of compressed dot syntax.

## Error behavior

Malformed source raises `HarmonyParseError`. The command-line interface catches
that exception, prints a human-readable message, and exits with status 2.

The parser is intentionally strict about ambiguous syntax. It is better for a
corpus source file to fail loudly than for the parser to silently guess.