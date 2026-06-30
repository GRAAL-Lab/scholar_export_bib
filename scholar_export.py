#!/usr/bin/env python3
"""Export Google Scholar publications matching a configured query to BibTeX."""

from __future__ import annotations

import argparse
import configparser
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
DEFAULT_CONFIG = PROJECT_ROOT / "scholar_export.conf"
DEFAULT_EXPORT_DIR_NAME = "exports"

if (
    PROJECT_VENV_PYTHON.exists()
    and Path(sys.executable).resolve() != PROJECT_VENV_PYTHON.resolve()
):
    os.execv(str(PROJECT_VENV_PYTHON), [str(PROJECT_VENV_PYTHON), __file__, *sys.argv[1:]])

try:
    from scholarly import scholarly
except Exception as exc:  # scholarly can fail while importing optional deps.
    scholarly = None
    SCHOLARLY_IMPORT_ERROR = exc
else:
    SCHOLARLY_IMPORT_ERROR = None


Publication = dict[str, Any]


class ConfigError(ValueError):
    """Raised when the search config is missing or invalid."""


@dataclass(frozen=True)
class SearchConfig:
    keywords: str
    start_year: int
    end_year: int
    output: Path
    write_txt: bool
    max_results: int
    delay_seconds: float
    fill_publications: bool
    patents: bool
    citations: bool
    sort_by: str


def require_scholarly() -> None:
    if scholarly is not None:
        return

    print("Could not import the 'scholarly' package.", file=sys.stderr)
    print(f"Current Python interpreter: {sys.executable}", file=sys.stderr)
    print(f"Original error: {SCHOLARLY_IMPORT_ERROR}", file=sys.stderr)
    print(file=sys.stderr)
    print("Use the project Python environment:", file=sys.stderr)
    print("  .venv/bin/python scholar_export.py", file=sys.stderr)
    print("or activate it first:", file=sys.stderr)
    print("  source .venv/bin/activate", file=sys.stderr)
    print("  python scholar_export.py", file=sys.stderr)
    print(file=sys.stderr)
    print("If you really want to use this current interpreter, install pip first,", file=sys.stderr)
    print("then install the package there:", file=sys.stderr)
    print(f"  {sys.executable} -m pip install --upgrade scholarly lxml", file=sys.stderr)
    raise SystemExit(1)


def normalize_keywords(value: str) -> str:
    return " ".join(line.strip() for line in value.splitlines() if line.strip())


def expand_keyword_blocks(text: str, source: Path) -> str:
    """Allow keywords = \"\"\"...\"\"\" blocks inside the INI config."""
    lines = text.splitlines()
    output: list[str] = []
    keyword_block = re.compile(
        r"^(?P<prefix>\s*keywords\s*=\s*)(?P<quote>\"\"\"|''')(?P<rest>.*)$",
        re.IGNORECASE,
    )

    i = 0
    while i < len(lines):
        line = lines[i]
        match = keyword_block.match(line)
        if not match:
            output.append(line)
            i += 1
            continue

        quote = match.group("quote")
        block_lines: list[str] = []
        rest = match.group("rest")

        while True:
            end_index = rest.find(quote)
            if end_index >= 0:
                before_quote = rest[:end_index]
                if before_quote.strip():
                    block_lines.append(before_quote)

                trailing = rest[end_index + len(quote):].strip()
                if trailing and not trailing.startswith(("#", ";")):
                    raise ConfigError(
                        f"Unexpected text after closing keywords block in {source}: {trailing}"
                    )
                break

            block_lines.append(rest)
            i += 1
            if i >= len(lines):
                raise ConfigError(f"Unclosed triple-quoted keywords block in {source}.")
            rest = lines[i]

        output.append(f"{match.group('prefix')}{normalize_keywords(chr(10).join(block_lines))}")
        i += 1

    return "\n".join(output) + ("\n" if text.endswith("\n") else "")


def read_int(section: configparser.SectionProxy, key: str) -> int:
    raw_value = section.get(key)
    if raw_value is None:
        raise ConfigError(f"Missing required config value: search.{key}")

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"search.{key} must be an integer.") from exc


def resolve_output_path(config_path: Path, output_value: str) -> Path:
    output = Path(output_value)
    if output.is_absolute():
        return output

    if output.parent == Path("."):
        return config_path.parent / DEFAULT_EXPORT_DIR_NAME / output

    return config_path.parent / output


def load_config(path: str | Path) -> SearchConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
    )
    config_text = expand_keyword_blocks(config_path.read_text(encoding="utf-8"), config_path)
    parser.read_string(config_text, source=str(config_path))
    if "search" not in parser:
        raise ConfigError("Config file must contain a [search] section.")

    section = parser["search"]
    keywords = normalize_keywords(section.get("keywords", fallback=""))
    if not keywords:
        raise ConfigError("search.keywords cannot be empty.")

    start_year = read_int(section, "start_year")
    end_year = read_int(section, "end_year")
    if start_year > end_year:
        raise ConfigError("search.start_year must be less than or equal to search.end_year.")

    max_results = section.getint("max_results", fallback=50)
    if max_results < 1:
        raise ConfigError("search.max_results must be at least 1.")

    output_value = section.get("output", fallback=f"publications_{start_year}_{end_year}.bib")
    output = resolve_output_path(config_path, output_value)

    return SearchConfig(
        keywords=keywords,
        start_year=start_year,
        end_year=end_year,
        output=output,
        write_txt=section.getboolean("txt", fallback=True),
        max_results=max_results,
        delay_seconds=max(section.getfloat("delay", fallback=1.0), 0.0),
        fill_publications=section.getboolean("fill_publications", fallback=False),
        patents=section.getboolean("patents", fallback=False),
        citations=section.getboolean("citations", fallback=False),
        sort_by=section.get("sort_by", fallback="relevance").strip() or "relevance",
    )


def parse_year(value: Any) -> int | None:
    if value is None:
        return None

    match = re.search(r"\b(18|19|20)\d{2}\b", str(value))
    if not match:
        return None
    return int(match.group(0))


def bibtex_escape(value: Any) -> str:
    text = str(value).strip()
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
    }
    return "".join(replacements.get(char, char) for char in text)


def make_bibtex_key(pub: Publication, index: int) -> str:
    bib = pub.get("bib", {})
    author = str(bib.get("author", "pub")).split(" and ")[0].split(",")[0]
    year = parse_year(bib.get("pub_year")) or "noyear"
    title = str(bib.get("title", "publication"))

    raw_key = f"{author}_{year}_{title}".lower()
    key = re.sub(r"[^a-z0-9]+", "_", raw_key).strip("_")
    return key[:80] or f"pub_{index:04d}"


def get_pub_year(pub: Publication) -> int | None:
    bib = pub.get("bib", {})
    return parse_year(bib.get("pub_year") or bib.get("year"))


def publication_identity(pub: Publication) -> tuple[str, int | None]:
    bib = pub.get("bib", {})
    title = re.sub(r"\s+", " ", str(bib.get("title", ""))).strip().lower()
    return title, get_pub_year(pub)


def fill_publication(pub: Publication, delay_seconds: float) -> Publication | None:
    require_scholarly()

    try:
        filled = scholarly.fill(pub)
    except Exception as exc:
        title = pub.get("bib", {}).get("title", "unknown title")
        print(f"Skipping '{title}': {exc}", file=sys.stderr)
        return None

    if delay_seconds > 0:
        time.sleep(delay_seconds)

    return filled


def search_publications(config: SearchConfig) -> list[Publication]:
    """Search Google Scholar with the raw configured keyword string."""
    require_scholarly()

    print(f"Searching Google Scholar for: {config.keywords}")
    print(f"Year range: {config.start_year}-{config.end_year}")

    try:
        search_results = scholarly.search_pubs(
            config.keywords,
            patents=config.patents,
            citations=config.citations,
            year_low=config.start_year,
            year_high=config.end_year,
            sort_by=config.sort_by,
        )
    except Exception as exc:
        print(f"Error while starting publication search: {exc}", file=sys.stderr)
        return []

    matches: list[Publication] = []
    seen: set[tuple[str, int | None]] = set()

    try:
        for pub in search_results:
            if len(matches) >= config.max_results:
                break

            title = pub.get("bib", {}).get("title", "untitled")
            publication = pub

            if config.fill_publications:
                filled = fill_publication(pub, config.delay_seconds)
                if not filled:
                    continue
                publication = filled

            year = get_pub_year(publication)
            if year is None:
                print(f"Skipping publication without year: {title}")
                if not config.fill_publications and config.delay_seconds > 0:
                    time.sleep(config.delay_seconds)
                continue

            if not (config.start_year <= year <= config.end_year):
                if not config.fill_publications and config.delay_seconds > 0:
                    time.sleep(config.delay_seconds)
                continue

            identity = publication_identity(publication)
            if identity in seen:
                if not config.fill_publications and config.delay_seconds > 0:
                    time.sleep(config.delay_seconds)
                continue
            seen.add(identity)

            matches.append(publication)
            print(f"Found: {publication.get('bib', {}).get('title', title)} ({year})")

            if not config.fill_publications and config.delay_seconds > 0:
                time.sleep(config.delay_seconds)
    except Exception as exc:
        print(f"Search stopped early: {exc}", file=sys.stderr)

    return matches


def publication_type(pub: Publication) -> str:
    bib = pub.get("bib", {})
    pub_type = str(pub.get("pub_type") or pub.get("type") or "").lower()
    title = str(bib.get("title", "")).lower()
    venue = str(bib.get("venue") or bib.get("journal") or "").lower()

    if "book" in pub_type or "book" in title:
        return "book"
    if "conference" in pub_type or "conference" in venue or "proceedings" in venue:
        return "inproceedings"
    return "article"


def bibtex_fields(pub: Publication) -> Iterable[tuple[str, Any]]:
    bib = pub.get("bib", {})
    field_map = {
        "title": bib.get("title"),
        "author": bib.get("author"),
        "year": bib.get("pub_year") or bib.get("year"),
        "journal": bib.get("journal") or bib.get("venue"),
        "volume": bib.get("volume"),
        "number": bib.get("number"),
        "pages": bib.get("pages"),
        "publisher": bib.get("publisher"),
        "doi": bib.get("doi"),
        "url": pub.get("pub_url") or pub.get("eprint_url") or pub.get("url"),
    }

    for name, value in field_map.items():
        if value not in (None, ""):
            yield name, value


def next_available_path(path: str | Path) -> Path:
    output = Path(path)
    if not output.exists():
        return output

    for number in range(1, 10_000):
        candidate = output.with_name(f"{output.stem}_{number}{output.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find an available output filename for {output}")


def export_to_bibtex(publications: list[Publication], filename: str | Path) -> bool:
    if not publications:
        print("No publications to export.")
        return False

    used_keys: set[str] = set()
    entries: list[str] = []

    for index, pub in enumerate(publications, 1):
        base_key = make_bibtex_key(pub, index)
        key = base_key
        suffix = 2
        while key in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(key)

        lines = [f"@{publication_type(pub)}{{{key},"]
        fields = list(bibtex_fields(pub))
        for field_index, (name, value) in enumerate(fields):
            comma = "," if field_index < len(fields) - 1 else ""
            lines.append(f"  {name} = {{{bibtex_escape(value)}}}{comma}")
        lines.append("}")
        entries.append("\n".join(lines))

    output = Path(filename)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    print(f"Exported {len(entries)} publications to {output}")
    return True


def export_to_txt(
    publications: list[Publication],
    filename: str | Path,
    start_year: int,
    end_year: int,
) -> bool:
    if not publications:
        print("No publications to export.")
        return False

    lines = [
        "=" * 80,
        f"PUBLICATIONS FROM {start_year} TO {end_year}",
        f"Total: {len(publications)} publications",
        "=" * 80,
        "",
    ]

    for index, pub in enumerate(publications, 1):
        bib = pub.get("bib", {})
        lines.extend(
            [
                f"{index:4d}. {bib.get('title', 'No title')}",
                f"     Authors: {bib.get('author', 'Unknown')}",
                f"     Year: {bib.get('pub_year') or bib.get('year') or 'Unknown'}",
                f"     Journal/Venue: {bib.get('journal') or bib.get('venue') or 'Unknown'}",
            ]
        )

        for label, key in (("Volume", "volume"), ("Pages", "pages"), ("DOI", "doi")):
            if bib.get(key):
                lines.append(f"     {label}: {bib[key]}")

        if pub.get("pub_url"):
            lines.append(f"     URL: {pub['pub_url']}")

        lines.append("")

    output = Path(filename)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Exported text summary to {output}")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Google Scholar publications from a config file."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Path to config file. Default: {DEFAULT_CONFIG}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print(f"Using config: {Path(args.config)}")
    print(f"Query: {config.keywords}")

    publications = search_publications(config)
    if not publications:
        print(f"No publications found from {config.start_year} to {config.end_year}.")
        return 1

    output = next_available_path(config.output)
    if output != config.output:
        print(f"Output file exists; using {output} instead.")

    export_to_bibtex(publications, output)

    if config.write_txt:
        export_to_txt(publications, output.with_suffix(".txt"), config.start_year, config.end_year)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
