#!/usr/bin/env python3
"""Parse Bluegrass DB ``.harm`` files into normalized Python objects.

The parser deliberately does *not* render chord names, transpose chords, or
make playback decisions.  Its job is narrower:

    .harm text -> validated, normalized harmonic/formal data

That normalized data can then be used by chart renderers, MusicXML writers,
corpus-analysis scripts, a website backend, etc.

Current v1 assumptions
----------------------
* ``% Title: ...`` and ``% TuneID: ...`` are required metadata.
* ``Form: AABA`` uses one-letter formal types.  Numbered variants such as
  A1/A2/A3 are matched to repeated A occurrences in order.
* A section may alias an earlier section with ``C = V`` or ``A3 = A2``;
  aliases remain distinct formal sections while inheriting harmonic bars.
* ``Region: 4`` inside a section changes the local tonic region for subsequent
  bars.  Every named section starts in region ``1`` unless changed explicitly.
* Outside parentheses, each whitespace-separated token is one bar.
* Inside parentheses, each whitespace-separated token is one equal slot in
  the bar.  A chord token creates an onset; ``.`` occupies a slot but creates
  no onset.
* A standalone ``.`` therefore means a whole bar with no new chord onset.
* Chord roots are Nashville scale degrees 1-7, optionally altered (b7, #4).
* Triad quality is separate from chord modifiers/extensions:
    no symbol = major, ``-`` = minor, ``o``/``°`` = diminished,
    ``+`` = augmented.
* Slash basses are optional.  In root position, ``bass`` is explicitly set to
  the root in the parsed object and ``bass_explicit`` is False.

No third-party packages are required.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence


class HarmonyParseError(ValueError):
    """Raised when a ``.harm`` file violates the v1 grammar.

    ``line`` is optional because some validation errors (for example, a form
    that cannot be mapped onto the section labels) concern the file as a
    whole rather than one lexical token.
    """

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.message = message
        self.line = line
        prefix = f"line {line}: " if line is not None else ""
        super().__init__(prefix + message)


@dataclass(frozen=True)
class ScaleDegree:
    """A Nashville scale degree, including any chromatic alteration.

    ``text`` is the canonical human-readable identity (for example ``b7``).
    ``degree`` and ``alteration`` are derived computational fields.  An
    alteration of -1 is one flat; +1 is one sharp.
    """

    text: str
    degree: int
    alteration: int = 0


@dataclass(frozen=True)
class Chord:
    """A structurally parsed chord token.

    The root and bass text remain musically readable (``b7``, ``#4``), while
    integer degree/alteration fields are also available for corpus queries.

    ``modifiers`` intentionally preserves the compact modifier text as a
    unit.  For example, ``594`` parses as root 5 with ``modifiers=["94"]``.
    We do not yet pretend that every compact modifier has been semantically
    decomposed into independent extensions/suspensions.
    """

    raw: str
    root: str
    root_degree: int
    root_alteration: int
    quality: str
    modifiers: list[str]
    bass: str
    bass_degree: int
    bass_alteration: int
    bass_explicit: bool


@dataclass(frozen=True)
class ChordEvent:
    """One explicit chord onset within a bar.

    ``slot`` is zero-based. ``position`` is an exact fraction of the bar:
    slot / subdivisions.  It is never stored as a floating-point number.
    """

    slot: int
    position: Fraction
    chord: Chord


@dataclass
class Bar:
    """One bar of harmonic data.

    ``subdivisions`` records the slot grid used by the source.  This matters
    even when some slots contain dots.  For example ``(1 .)`` has two slots
    but only one onset, so the subdivision count cannot always be recovered
    from the events alone.
    """

    number: int
    raw: str
    subdivisions: int
    region: str = "1"
    events: list[ChordEvent] = field(default_factory=list)


@dataclass
class Section:
    """A named harmonic section such as A1, A2, B, or A3.

    ``alias_of`` records source-level harmonic inheritance.  An alias is
    still its own formal section, but its bars are copied from the earlier
    target section during parsing.
    """

    label: str
    bars: list[Bar] = field(default_factory=list)
    alias_of: str | None = None


@dataclass
class HarmonyData:
    """Normalized result of parsing one ``.harm`` file."""

    title: str
    tune_id: str
    form: str
    form_sections: list[str]
    sections: dict[str, Section]
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation of this object.

        Fractions are serialized as strings such as ``0/1`` and ``3/4`` to
        avoid rounding errors and to make exact onset positions explicit.
        """

        def chord_to_dict(chord: Chord) -> dict:
            return asdict(chord)

        return {
            "title": self.title,
            "tune_id": self.tune_id,
            "form": self.form,
            "form_sections": list(self.form_sections),
            "metadata": dict(self.metadata),
            "sections": {
                label: {
                    "label": section.label,
                    **(
                        {"alias_of": section.alias_of}
                        if section.alias_of is not None
                        else {}
                    ),
                    "bars": [
                        {
                            "number": bar.number,
                            "region": bar.region,
                            "raw": bar.raw,
                            "subdivisions": bar.subdivisions,
                            "events": [
                                {
                                    "slot": event.slot,
                                    "position": _fraction_string(event.position),
                                    "chord": chord_to_dict(event.chord),
                                }
                                for event in bar.events
                            ],
                        }
                        for bar in section.bars
                    ],
                }
                for label, section in self.sections.items()
            },
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize the normalized data as JSON for inspection/interchange."""

        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


@dataclass(frozen=True)
class _BarToken:
    """Internal lexer output: one complete bar expression and its start line."""

    text: str
    line: int


_METADATA_RE = re.compile(r"^%\s*([^:]+?)\s*:\s*(.*?)\s*$")
_FORM_RE = re.compile(r"^Form\s*:\s*(.*?)\s*$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*$")
_SECTION_ALIAS_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_-]*)\s*=\s*([A-Za-z][A-Za-z0-9_-]*)\s*$"
)
_REGION_RE = re.compile(r"^Region\s*:\s*(.*?)\s*$", re.IGNORECASE)

# A scale-degree string is intentionally a small language.  We accept repeated
# accidentals because supporting bb7/##4 costs essentially nothing, even if
# most source files will only use one accidental.
_SCALE_DEGREE_RE = re.compile(r"^(?P<acc>[b#]*)(?P<degree>[1-7])$")

# Chord modifier text remains compact in v1.  This accepts the de Clercq-style
# numeric/M-prefixed forms we currently need: 7, 2, 64, 74, 94, M7, M9, etc.
_MODIFIER_RE = re.compile(r"^M?\d+$")

_QUALITY_SYMBOLS = {
    "-": "minor",
    "o": "diminished",
    "°": "diminished",
    "+": "augmented",
}


def _fraction_string(value: Fraction) -> str:
    """Serialize an exact Fraction in an intentionally uniform form."""

    return f"{value.numerator}/{value.denominator}"


def _normalize_accidentals(text: str) -> str:
    """Accept Unicode accidentals while using ASCII internally/source-canonically."""

    return text.replace("♭", "b").replace("♯", "#")


def parse_scale_degree(text: str, *, line: int | None = None) -> ScaleDegree:
    """Parse ``1``/``b7``/``#4`` into a canonical scale-degree object."""

    canonical = _normalize_accidentals(text.strip())
    match = _SCALE_DEGREE_RE.fullmatch(canonical)
    if not match:
        raise HarmonyParseError(
            f"invalid scale degree {text!r}; expected 1-7 with optional b/# accidental(s)",
            line=line,
        )

    accidental_text = match.group("acc")
    degree = int(match.group("degree"))
    alteration = accidental_text.count("#") - accidental_text.count("b")
    return ScaleDegree(text=canonical, degree=degree, alteration=alteration)


def parse_chord(token: str, *, line: int | None = None) -> Chord:
    """Parse one compact Nashville chord token.

    Examples
    --------
    ``57``   -> root 5, major triad, modifier 7, root-position bass
    ``6-7``  -> root 6, minor triad, modifier 7
    ``b7``   -> flat-seven major triad
    ``12/3`` -> root 1, modifier 2, bass degree 3
    ``594``  -> root 5, modifier 94
    """

    raw = token.strip()
    if not raw:
        raise HarmonyParseError("empty chord token", line=line)
    if raw == ".":
        raise HarmonyParseError("'.' is an empty onset slot, not a chord", line=line)

    canonical = _normalize_accidentals(raw)

    if canonical.count("/") > 1:
        raise HarmonyParseError(
            f"invalid chord {raw!r}: at most one slash bass is allowed", line=line
        )

    if "/" in canonical:
        chord_part, bass_part = canonical.split("/", 1)
        if not bass_part:
            raise HarmonyParseError(
                f"invalid chord {raw!r}: slash bass is missing", line=line
            )
        bass_degree = parse_scale_degree(bass_part, line=line)
        bass_explicit = True
    else:
        chord_part = canonical
        bass_degree = None
        bass_explicit = False

    # Root = zero or more accidentals + exactly one scale-degree digit.
    root_match = re.match(r"^(?P<acc>[b#]*)(?P<degree>[1-7])(?P<rest>.*)$", chord_part)
    if not root_match:
        raise HarmonyParseError(
            f"invalid chord {raw!r}: expected a Nashville root degree 1-7",
            line=line,
        )

    root_text = root_match.group("acc") + root_match.group("degree")
    root_degree = parse_scale_degree(root_text, line=line)
    rest = root_match.group("rest")

    quality = "major"
    if rest and rest[0] in _QUALITY_SYMBOLS:
        quality = _QUALITY_SYMBOLS[rest[0]]
        rest = rest[1:]

    modifiers: list[str] = []
    if rest:
        if not _MODIFIER_RE.fullmatch(rest):
            hint = ""
            if "." in rest:
                hint = (
                    " Inside parentheses, every dot and chord must be separated "
                    "by whitespace (for example '(1 . . 4)')."
                )
            raise HarmonyParseError(
                f"invalid modifier text {rest!r} in chord {raw!r}."
                " Expected compact numeric/M-prefixed modifier text such as "
                "7, 64, 94, or M7." + hint,
                line=line,
            )
        modifiers.append(rest)

    if bass_degree is None:
        # Root-position chords carry an explicit normalized bass value so that
        # downstream bass-motion queries never need to special-case null.
        bass_degree = root_degree

    return Chord(
        raw=raw,
        root=root_degree.text,
        root_degree=root_degree.degree,
        root_alteration=root_degree.alteration,
        quality=quality,
        modifiers=modifiers,
        bass=bass_degree.text,
        bass_degree=bass_degree.degree,
        bass_alteration=bass_degree.alteration,
        bass_explicit=bass_explicit,
    )


def _tokenize_bars(lines: Sequence[tuple[int, str]], *, section: str) -> list[_BarToken]:
    """Split section text into top-level bar expressions.

    Parentheses may technically span physical lines; line breaks are treated as
    whitespace.  Nested parentheses are rejected because one parenthesized group
    already denotes exactly one bar.
    """

    tokens: list[_BarToken] = []
    plain_buffer: list[str] = []
    plain_start_line: int | None = None
    group_buffer: list[str] | None = None
    group_start_line: int | None = None
    just_closed_group = False

    def flush_plain() -> None:
        nonlocal plain_buffer, plain_start_line
        if plain_buffer:
            text = "".join(plain_buffer)
            tokens.append(_BarToken(text=text, line=plain_start_line or 1))
            plain_buffer = []
            plain_start_line = None

    for line_no, line_text in lines:
        # Add a newline-equivalent separator after each physical line so two
        # neighboring tokens on different lines can never run together.
        stream = line_text + "\n"
        for char in stream:
            if group_buffer is not None:
                if char == "(":
                    raise HarmonyParseError(
                        f"nested parentheses are not allowed in section {section!r}",
                        line=line_no,
                    )
                group_buffer.append(char)
                if char == ")":
                    text = "".join(group_buffer)
                    tokens.append(_BarToken(text=text, line=group_start_line or line_no))
                    group_buffer = None
                    group_start_line = None
                    just_closed_group = True
                continue

            if char.isspace():
                flush_plain()
                just_closed_group = False
                continue

            if just_closed_group:
                raise HarmonyParseError(
                    "bar expressions must be separated by whitespace",
                    line=line_no,
                )

            if char == "(":
                if plain_buffer:
                    raise HarmonyParseError(
                        "bar expressions must be separated by whitespace",
                        line=line_no,
                    )
                group_buffer = ["("]
                group_start_line = line_no
                continue

            if char == ")":
                raise HarmonyParseError(
                    f"unmatched ')' in section {section!r}", line=line_no
                )

            if plain_start_line is None:
                plain_start_line = line_no
            plain_buffer.append(char)

    flush_plain()

    if group_buffer is not None:
        raise HarmonyParseError(
            f"unclosed '(' in section {section!r}", line=group_start_line
        )

    return tokens


def _parse_bar(token: _BarToken, *, number: int, region: str = "1") -> Bar:
    """Parse one top-level bar expression into exact onset events."""

    text = token.text.strip()
    if not text:
        raise HarmonyParseError("empty bar expression", line=token.line)

    if text == ".":
        # Whole-bar continuation: one slot, no explicit onset.
        return Bar(
            number=number,
            raw=text,
            subdivisions=1,
            region=region,
            events=[],
        )

    if text.startswith("("):
        if not text.endswith(")"):
            raise HarmonyParseError("unclosed parenthesized bar", line=token.line)

        inner = text[1:-1].strip()
        if not inner:
            raise HarmonyParseError("empty parenthesized bar '()'", line=token.line)

        slots = inner.split()
        subdivisions = len(slots)
        events: list[ChordEvent] = []

        for slot_index, slot_text in enumerate(slots):
            if slot_text == ".":
                continue
            try:
                chord = parse_chord(slot_text, line=token.line)
            except HarmonyParseError as exc:
                # Compressed dot notation such as (1..4), (14.), or (.4) is a
                # particularly likely human error, so make the repair explicit.
                if "." in slot_text:
                    raise HarmonyParseError(
                        f"invalid slot {slot_text!r} in parenthesized bar {text!r}. "
                        "Every slot must be whitespace-separated; write forms such "
                        "as '(1 . . 4)', '(1 4 .)', or '(. 4)'.",
                        line=token.line,
                    ) from exc
                raise

            events.append(
                ChordEvent(
                    slot=slot_index,
                    position=Fraction(slot_index, subdivisions),
                    chord=chord,
                )
            )

        return Bar(
            number=number,
            raw=text,
            subdivisions=subdivisions,
            region=region,
            events=events,
        )

    if "(" in text or ")" in text:
        raise HarmonyParseError(
            f"malformed parenthesized bar {text!r}", line=token.line
        )

    chord = parse_chord(text, line=token.line)
    return Bar(
        number=number,
        raw=text,
        subdivisions=1,
        region=region,
        events=[ChordEvent(slot=0, position=Fraction(0, 1), chord=chord)],
    )


def _parse_section_bars(
    lines: Sequence[tuple[int, str]], *, section: str
) -> list[Bar]:
    """Parse a section while applying zero-duration ``Region:`` directives.

    Each named section begins in region ``1``.  ``Region: X`` changes the local
    tonic region for subsequent bars in that section until another region
    directive appears.  Region values use the same scale-degree spelling rules
    as chord roots (for example ``4``, ``b7``, or ``#4``).
    """

    bars: list[Bar] = []
    pending_lines: list[tuple[int, str]] = []
    current_region = "1"

    def flush_pending() -> None:
        nonlocal pending_lines
        if not pending_lines:
            return

        bar_tokens = _tokenize_bars(pending_lines, section=section)
        for token in bar_tokens:
            bars.append(
                _parse_bar(
                    token,
                    number=len(bars) + 1,
                    region=current_region,
                )
            )
        pending_lines = []

    for line_no, line_text in lines:
        stripped = line_text.strip()
        region_match = _REGION_RE.fullmatch(stripped)
        if region_match:
            flush_pending()
            region_text = region_match.group(1).strip()
            if not region_text:
                raise HarmonyParseError("Region may not be empty", line=line_no)
            current_region = parse_scale_degree(region_text, line=line_no).text
            continue

        pending_lines.append((line_no, line_text))

    flush_pending()
    return bars


def _form_symbols(form: str) -> list[str]:
    """Convert v1 canonical form text (e.g. AABA) to formal symbols."""

    compact = "".join(form.split())
    if not compact:
        raise HarmonyParseError("Form may not be empty")
    if not compact.isalpha():
        raise HarmonyParseError(
            f"v1 Form must contain only one-letter formal symbols (e.g. AABA), got {form!r}"
        )
    return list(compact)


def _resolve_form_sections(form: str, section_labels: Iterable[str]) -> list[str]:
    """Resolve canonical formal types onto concrete section labels.

    Rules intentionally favor explicitness over guessing:

    * If ``A:`` exists, every A in the form reuses ``A``.
    * If numbered variants exist instead (A1, A2, A3), there must be exactly
      as many as there are A occurrences in the form, and numbering must be
      consecutive from 1.
    * Mixing ``A:`` with A1/A2/... is an error.
    * Extra section labels not used by the canonical form are retained in the
      parsed file but are not inserted into ``form_sections`` automatically.
    """

    labels = list(section_labels)
    symbols = _form_symbols(form)
    counts = Counter(symbols)

    resolution: dict[str, list[str]] = {}

    for symbol, occurrence_count in counts.items():
        exact = symbol if symbol in labels else None
        numbered: list[tuple[int, str]] = []
        numbered_re = re.compile(rf"^{re.escape(symbol)}(\d+)$")
        for label in labels:
            match = numbered_re.fullmatch(label)
            if match:
                numbered.append((int(match.group(1)), label))
        numbered.sort()

        if exact and numbered:
            raise HarmonyParseError(
                f"form symbol {symbol!r} is ambiguous: both {exact!r} and numbered "
                f"variants {[label for _, label in numbered]!r} exist"
            )

        if exact:
            resolution[symbol] = [exact] * occurrence_count
            continue

        if numbered:
            actual_numbers = [number for number, _ in numbered]
            expected_numbers = list(range(1, occurrence_count + 1))
            if len(numbered) != occurrence_count or actual_numbers != expected_numbers:
                raise HarmonyParseError(
                    f"form {form!r} contains {occurrence_count} occurrence(s) of {symbol!r}, "
                    f"so numbered variants must be exactly "
                    f"{[symbol + str(i) for i in expected_numbers]!r}; found "
                    f"{[label for _, label in numbered]!r}"
                )
            resolution[symbol] = [label for _, label in numbered]
            continue

        raise HarmonyParseError(
            f"form symbol {symbol!r} has no matching section ({symbol}: or numbered variants)"
        )

    use_count: defaultdict[str, int] = defaultdict(int)
    resolved: list[str] = []
    for symbol in symbols:
        index = use_count[symbol]
        resolved.append(resolution[symbol][index])
        use_count[symbol] += 1

    return resolved


def parse_harmony_text(text: str, *, source: str = "<string>") -> HarmonyData:
    """Parse ``.harm`` content supplied as a string.

    ``source`` is only used to improve error messages supplied by callers; it
    is not embedded into the normalized data.
    """

    metadata: dict[str, str] = {}
    form: str | None = None
    section_lines: dict[str, list[tuple[int, str]]] = {}
    section_aliases: dict[str, str] = {}
    section_order: list[str] = []
    current_section: str | None = None

    for line_no, original_line in enumerate(text.splitlines(), start=1):
        stripped = original_line.strip()

        if not stripped:
            # Blank lines are purely visual separators.
            continue

        if stripped.startswith("%"):
            metadata_match = _METADATA_RE.fullmatch(stripped)
            if metadata_match:
                key = metadata_match.group(1).strip()
                value = metadata_match.group(2).strip()
                if not key:
                    raise HarmonyParseError("empty metadata key", line=line_no)
                if key in metadata:
                    raise HarmonyParseError(
                        f"duplicate metadata field {key!r}", line=line_no
                    )
                metadata[key] = value
            # A percent line without ':' is simply a human comment.
            continue

        form_match = _FORM_RE.fullmatch(stripped)
        if form_match:
            if form is not None:
                raise HarmonyParseError("duplicate Form field", line=line_no)
            form = form_match.group(1).strip()
            if not form:
                raise HarmonyParseError("Form may not be empty", line=line_no)
            current_section = None
            continue

        region_match = _REGION_RE.fullmatch(stripped)
        if region_match:
            if current_section is None:
                raise HarmonyParseError(
                    "Region directive appears outside a named section",
                    line=line_no,
                )
            section_lines[current_section].append((line_no, original_line))
            continue

        section_match = _SECTION_RE.fullmatch(stripped)
        if section_match:
            label = section_match.group(1)
            if label in section_lines or label in section_aliases:
                raise HarmonyParseError(
                    f"duplicate section label {label!r}", line=line_no
                )
            section_lines[label] = []
            section_order.append(label)
            current_section = label
            continue

        alias_match = _SECTION_ALIAS_RE.fullmatch(stripped)
        if alias_match:
            label = alias_match.group(1)
            target = alias_match.group(2)

            if label in section_lines or label in section_aliases:
                raise HarmonyParseError(
                    f"duplicate section label {label!r}", line=line_no
                )

            # Aliases point backward only.  This keeps parsing deterministic and
            # makes circular references impossible without a separate graph pass.
            if target not in section_lines and target not in section_aliases:
                raise HarmonyParseError(
                    f"section alias {label!r} refers to undefined or later "
                    f"section {target!r}",
                    line=line_no,
                )

            section_aliases[label] = target
            section_order.append(label)
            current_section = None
            continue

        if current_section is None:
            raise HarmonyParseError(
                f"harmonic data appears outside a named section: {stripped!r}",
                line=line_no,
            )

        section_lines[current_section].append((line_no, original_line))

    # Required source-level identifiers.
    title = metadata.get("Title")
    tune_id = metadata.get("TuneID")
    missing = [name for name, value in (("Title", title), ("TuneID", tune_id)) if not value]
    if missing:
        raise HarmonyParseError(
            f"missing required metadata field(s): {', '.join(missing)}"
        )
    if form is None:
        raise HarmonyParseError("missing required Form field")
    if not section_lines:
        raise HarmonyParseError("file contains no harmonic sections")

    sections: dict[str, Section] = {}
    for label in section_order:
        if label in section_aliases:
            target = section_aliases[label]
            sections[label] = Section(
                label=label,
                bars=deepcopy(sections[target].bars),
                alias_of=target,
            )
            continue

        lines = section_lines[label]
        bars = _parse_section_bars(lines, section=label)
        if not bars:
            raise HarmonyParseError(f"section {label!r} contains no bars")
        sections[label] = Section(label=label, bars=bars)

    form_sections = _resolve_form_sections(form, sections.keys())

    return HarmonyData(
        title=title,
        tune_id=tune_id,
        form="".join(_form_symbols(form)),
        form_sections=form_sections,
        sections=sections,
        metadata=metadata,
    )


def parse_harmony(path: str | Path) -> HarmonyData:
    """Read and parse a UTF-8 ``.harm`` file from disk."""

    path_obj = Path(path)
    try:
        text = path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarmonyParseError(f"could not read {path_obj}: {exc}") from exc

    try:
        return parse_harmony_text(text, source=str(path_obj))
    except HarmonyParseError as exc:
        # Keep the exception type useful to library callers while adding the
        # filename to its human-facing message.
        location = f":{exc.line}" if exc.line is not None else ""
        raise HarmonyParseError(
            f"{path_obj}{location}: {exc.message}", line=None
        ) from exc


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse a Bluegrass DB .harm file and emit normalized JSON."
    )
    parser.add_argument("harm_file", type=Path, help="path to the .harm source file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write JSON to this file instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""

    args = _build_arg_parser().parse_args(argv)
    try:
        harmony = parse_harmony(args.harm_file)
    except HarmonyParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    output = harmony.to_json() + "\n"
    if args.output:
        try:
            args.output.write_text(output, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
            return 2
    else:
        print(output, end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())