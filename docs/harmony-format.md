# Harmony File Format (`.harm`) — parser v1

This is the current source format for reusable harmonic/formal data in Bluegrass DB.
The source file is intentionally compact and human-editable. The Python parser turns
it into a more verbose normalized data structure for analysis and rendering.

## 1. Required metadata

A file begins with:

```text
% Title: Blue Moon of Kentucky
% TuneID: bm01
```

`Title` and `TuneID` are required. Other `% Key: Value` fields are preserved as
metadata. A `%` line without a colon is treated as a human comment and ignored.

Recording-specific information—artist, recording date, key, meter, tempo, feel,
release information, and realized recording form—belongs in a version TOML file,
not the reusable harmony file.

## 2. Canonical form

```text
Form: AABA
```

Parser v1 treats the canonical form as a sequence of one-letter formal types.

If the same formal type has distinct harmonic realizations, number them:

```text
A1:
...

A2:
...

B:
...

A3:
...
```

The parser resolves:

```text
AABA -> A1 A2 B A3
```

If there is only one unnumbered section, it is reused:

```text
Form: AABA
A:
...
B:
...
```

resolves to:

```text
A A B A
```

The parser does not guess incomplete numbered sequences. With `Form: AABA`, for
example, `A1` and `A2` without `A3` is an error. Mixing `A:` with `A1:`/`A2:` is
also an error.

### Section aliases

When a formally distinct section has exactly the same harmony as an earlier
section, it may inherit that harmony instead of duplicating the bars:

```text
Form: VC

V:
1   67   27   27
57  57   1    1

C = V
```

`C = V` means that `C` is a distinct formal section whose harmonic bars are
identical to `V`. The normalized parser output expands those bars under `C` and
records `alias_of: "V"`. This is useful for cases such as a simple verse-chorus
tune in which verse and chorus have the same harmonic realization.

Aliases are general, so numbered formal variants may also inherit earlier
variants:

```text
A3 = A2
```

An alias may refer only to a section that appears earlier in the file. This
keeps references unambiguous and prevents circular aliases. Aliasing applies to
harmonic content only; the aliased label remains formally distinct for form,
lyrics, and other downstream annotations.

## 3. Ordinary bars

Outside parentheses, each whitespace-separated harmonic token is one bar.
Line breaks are for human readability only.

```text
1   17   4   4
1   57   1    17
```

is eight bars.

An ordinary chord token creates an onset at the beginning of its bar.
Therefore:

```text
1   1
```

contains two explicit onsets/restrikes, even though the chord identity did not
change.

A standalone dot means a whole bar with **no new chord onset**:

```text
1   .   4
```

The active `1` may continue through the dotted bar; the parser itself records
only that no new onset occurs there.

## 4. Multiple onsets within one bar

Parentheses group several equal slots into one bar. **Every slot must be
whitespace-separated, including dots.**

A chord token creates an onset at the beginning of its slot. A dot occupies a
slot but creates no onset.

```text
(1 4)
```

has two slots:

- `1` at 0/2 = 0% of the bar
- `4` at 1/2 = 50% of the bar

```text
(1 . . 4)
```

has four slots:

- `1` at 0/4 = 0%
- `4` at 3/4 = 75%

```text
(1 4 .)
```

has three slots:

- `1` at 0/3 = 0%
- `4` at 1/3 = 33.333...%

```text
(. 4)
```

has two slots and only one onset:

- `4` at 1/2 = 50%

The parser stores positions as exact fractions (`1/3`, `3/4`), not floating-point
percentages. Meter is version-specific and can later convert these bar fractions
to beats.

### Important whitespace rule

Do **not** write compressed forms such as:

```text
(1..4)
(14.)
(.4)
```

because compact chord notation already uses adjacent characters (`17`, `57`,
`594`, etc.). Write:

```text
(1 . . 4)
(1 4 .)
(. 4)
```

## 5. Chord-token structure

Parser v1 reads a chord as:

```text
[root] [triad quality] [compact modifier] [/ bass]
```

with no literal spaces inside the chord token.

### Root

The root is a Nashville scale degree 1–7, optionally altered:

```text
1
5
b7
#4
```

The altered spelling is the primary root identity. Thus `7` and `b7` are
different roots. The parser also derives an integer degree and alteration for
computation:

```text
b7 -> root="b7", root_degree=7, root_alteration=-1
```

Unicode `♭` and `♯` are accepted but normalized internally to `b` and `#`.

### Triad quality

Triad quality is independent of extensions/modifiers:

```text
(no symbol) = major
-           = minor
o or °      = diminished
+           = augmented
```

Thus:

```text
57
```

is parsed as a major triad on scale degree 5 plus modifier `7`, not as a
special `dominant` triad quality.

Likewise:

```text
6-7
```

is a minor triad on 6 plus modifier `7`.

### Modifiers/extensions

The compact modifier is preserved as a unit in parser v1. Examples include:

```text
17   -> root 1, modifier 7
12   -> root 1, modifier 2
594  -> root 5, modifier 94
5M7  -> root 5, modifier M7
```

This deliberately avoids over-interpreting compound source notation before the
corpus needs that distinction. Downstream analysis can remove modifiers while
retaining root and triad quality.

### Bass

Slash notation gives an explicit bass degree:

```text
57/7
1/3
5/b7
```

For root-position chords, the normalized parser output still fills the bass
with the root so bass-motion searches do not need a null special case:

```text
b7 -> bass="b7", bass_explicit=false
```

whereas:

```text
5/b7 -> bass="b7", bass_explicit=true
```

## 6. Full example

```text
% Title: Blue Moon of Kentucky
% TuneID: bm01

Form: AABA

A1:
1   1    4   4
1   1    57   57

A2:
1   17   4   4
1   57   1    17

B:
4   4    1   1
4   4    1    57

A3:
1   17   4   4
1   57   1    1
```

## 7. What the parser does not decide

The parser does not:

- choose a key or transpose to named chords;
- decide meter, tempo, or feel;
- suppress extensions for a simplified chart;
- infer accompaniment voicings;
- generate notation or playback;
- determine whether two different chord spellings are analytically equivalent.

Those are downstream operations on the normalized parser output.