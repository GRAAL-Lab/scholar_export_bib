#!/usr/bin/env python3
"""Export Google Scholar publications matching a configured query to BibTeX."""

from __future__ import annotations

import argparse
import ast
import configparser
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode, unquote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
DEFAULT_CONFIG = PROJECT_ROOT / "scholar_export.conf"
DEFAULT_EXPORT_DIR_NAME = "exports"
CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CROSSREF_TIMEOUT_SECONDS = 10
CROSSREF_USER_AGENT = "scholar_export_bib/1.0"

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


def is_initial_token(value: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", value)
    return bool(letters) and letters.isupper() and len(letters) <= 4


def split_author_string(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None

        if isinstance(parsed, (list, tuple)):
            return [str(author).strip() for author in parsed if str(author).strip()]

    if re.search(r"\s+and\s+", text, flags=re.IGNORECASE):
        return [
            author.strip()
            for author in re.split(r"\s+and\s+", text, flags=re.IGNORECASE)
            if author.strip()
        ]

    return [text]


def format_author_name(author: str) -> str:
    name = re.sub(r"\s+", " ", author).strip()
    if not name or "," in name:
        return name

    parts = name.split()
    if len(parts) == 1:
        return name

    if is_initial_token(parts[0]):
        given = parts[0]
        family = " ".join(parts[1:])
        return f"{family}, {given}"

    surname_particles = {
        "al",
        "bin",
        "da",
        "de",
        "del",
        "della",
        "den",
        "der",
        "di",
        "dos",
        "du",
        "ibn",
        "la",
        "le",
        "van",
        "von",
    }
    family_start = len(parts) - 1
    while family_start > 0 and parts[family_start - 1].lower() in surname_particles:
        family_start -= 1

    given = " ".join(parts[:family_start])
    family = " ".join(parts[family_start:])
    return f"{family}, {given}" if given else family


def format_bibtex_authors(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        authors = [str(author).strip() for author in value if str(author).strip()]
    else:
        authors = split_author_string(str(value))

    return " and ".join(format_author_name(author) for author in authors)


def metadata_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def is_placeholder_metadata(value: Any) -> bool:
    return metadata_text(value).lower() in {"", "na", "n/a", "none", "unknown"}


def is_truncated_metadata(value: Any) -> bool:
    text = metadata_text(value)
    return "…" in text or "..." in text


def usable_metadata(value: Any) -> str | None:
    text = metadata_text(value)
    if is_placeholder_metadata(text) or is_truncated_metadata(text):
        return None
    return text


def publication_has_truncated_venue(pub: Publication) -> bool:
    bib = pub.get("bib", {})
    return any(
        is_truncated_metadata(bib.get(key))
        for key in ("journal", "booktitle", "conference", "venue")
    )


def publication_has_usable_venue(pub: Publication) -> bool:
    bib = pub.get("bib", {})
    return any(
        usable_metadata(bib.get(key)) is not None
        for key in ("journal", "booktitle", "conference", "venue")
    )


def extract_doi(*values: Any) -> str | None:
    for value in values:
        if not value:
            continue

        text = unquote(str(value))
        match = re.search(r"10\.\d{4,9}/[^\s\"<>]+", text, flags=re.IGNORECASE)
        if not match:
            continue

        doi = re.sub(r"[?#].*$", "", match.group(0)).rstrip(".,;:)]}/")
        if doi.lower().endswith(".pdf"):
            doi = doi[:-4]
        return doi

    return None


def crossref_json(url: str) -> dict[str, Any] | None:
    request = Request(url, headers={"User-Agent": CROSSREF_USER_AGENT})
    try:
        with urlopen(request, timeout=CROSSREF_TIMEOUT_SECONDS) as response:
            data = json.load(response)
    except Exception as exc:
        print(f"Crossref lookup failed: {exc}", file=sys.stderr)
        return None

    return data if isinstance(data, dict) else None


def crossref_item_by_doi(doi: str) -> dict[str, Any] | None:
    data = crossref_json(f"{CROSSREF_WORKS_URL}/{quote(doi, safe='')}")
    item = data.get("message") if data else None
    return item if isinstance(item, dict) else None


def normalize_title_for_match(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def crossref_item_by_title(title: str, year: int | None) -> dict[str, Any] | None:
    params = {"query.title": title, "rows": "5"}
    if year is not None:
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"

    data = crossref_json(f"{CROSSREF_WORKS_URL}?{urlencode(params)}")
    items = (data or {}).get("message", {}).get("items", [])
    if not isinstance(items, list):
        return None

    wanted = normalize_title_for_match(title)
    for item in items:
        if not isinstance(item, dict):
            continue

        candidates = item.get("title") or []
        if isinstance(candidates, str):
            candidates = [candidates]

        for candidate in candidates:
            if normalize_title_for_match(candidate) == wanted:
                return item

    return None


def first_crossref_value(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if isinstance(value, list):
        value = next((entry for entry in value if usable_metadata(entry)), None)
    return usable_metadata(value)


def crossref_year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued"):
        date_parts = item.get(key, {}).get("date-parts")
        if (
            isinstance(date_parts, list)
            and date_parts
            and isinstance(date_parts[0], list)
            and date_parts[0]
        ):
            try:
                return int(date_parts[0][0])
            except (TypeError, ValueError):
                return None

    return None


def update_missing_bib_value(bib: dict[str, Any], key: str, value: Any) -> bool:
    clean_value = usable_metadata(value)
    if clean_value is None:
        return False

    current_value = bib.get(key)
    if usable_metadata(current_value) is not None:
        return False

    bib[key] = clean_value
    return True


def apply_crossref_metadata(pub: Publication, item: dict[str, Any]) -> bool:
    bib = pub.setdefault("bib", {})
    updated = False
    item_type = str(item.get("type") or "").lower()
    container = first_crossref_value(item, "container-title")
    venue_key = "booktitle" if "proceedings" in item_type else "journal"

    if container:
        updated = update_missing_bib_value(bib, venue_key, container) or updated

    metadata_map = {
        "doi": first_crossref_value(item, "DOI"),
        "volume": first_crossref_value(item, "volume"),
        "number": first_crossref_value(item, "issue"),
        "pages": first_crossref_value(item, "page"),
        "publisher": first_crossref_value(item, "publisher"),
        "pub_year": crossref_year(item),
    }
    for key, value in metadata_map.items():
        updated = update_missing_bib_value(bib, key, value) or updated

    if "proceedings" in item_type:
        bib["pub_type"] = "inproceedings"
    elif item_type == "journal-article":
        bib["pub_type"] = "article"

    return updated


def resolve_crossref_metadata(pub: Publication) -> bool:
    bib = pub.get("bib", {})
    doi = bib.get("doi") or extract_doi(
        bib.get("url"),
        pub.get("pub_url"),
        pub.get("eprint_url"),
        pub.get("url"),
    )

    item = crossref_item_by_doi(str(doi)) if doi else None
    if item is None:
        title = usable_metadata(bib.get("title"))
        item = crossref_item_by_title(title, get_pub_year(pub)) if title else None

    if item is None:
        return False

    return apply_crossref_metadata(pub, item)


def make_bibtex_key(pub: Publication, index: int) -> str:
    bib = pub.get("bib", {})
    author = str(bib.get("author", "pub")).split(" and ")[0].split(",")[0]
    year = parse_year(bib.get("pub_year")) or "noyear"
    title = str(bib.get("title", "publication"))

    raw_key = f"{author}_{year}".lower()
    key = re.sub(r"[^a-z0-9]+", "_", raw_key).strip("_")
    return key[:80] or f"pub_{index:04d}"


def get_pub_year(pub: Publication) -> int | None:
    bib = pub.get("bib", {})
    return parse_year(bib.get("pub_year") or bib.get("year"))


def publication_identity(pub: Publication) -> tuple[str, int | None]:
    bib = pub.get("bib", {})
    title = re.sub(r"\s+", " ", str(bib.get("title", ""))).strip().lower()
    return title, get_pub_year(pub)


def fill_publication(
    pub: Publication,
    delay_seconds: float,
    *,
    required: bool = True,
) -> Publication | None:
    require_scholarly()

    try:
        filled = scholarly.fill(pub)
    except Exception as exc:
        title = pub.get("bib", {}).get("title", "unknown title")
        action = "Skipping" if required else "Could not fill metadata for"
        print(f"{action} '{title}': {exc}", file=sys.stderr)
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
            filled_for_metadata = False

            if config.fill_publications:
                filled = fill_publication(pub, config.delay_seconds)
                if not filled:
                    continue
                publication = filled

            if publication_has_truncated_venue(publication):
                if resolve_crossref_metadata(publication):
                    print(f"Resolved venue metadata: {title}")

            if (
                not config.fill_publications
                and publication_has_truncated_venue(publication)
                and not publication_has_usable_venue(publication)
            ):
                filled = fill_publication(
                    publication,
                    config.delay_seconds,
                    required=False,
                )
                if filled:
                    publication = filled
                    filled_for_metadata = True

            year = get_pub_year(publication)
            if year is None:
                print(f"Skipping publication without year: {title}")
                if (
                    not config.fill_publications
                    and not filled_for_metadata
                    and config.delay_seconds > 0
                ):
                    time.sleep(config.delay_seconds)
                continue

            if not (config.start_year <= year <= config.end_year):
                if (
                    not config.fill_publications
                    and not filled_for_metadata
                    and config.delay_seconds > 0
                ):
                    time.sleep(config.delay_seconds)
                continue

            identity = publication_identity(publication)
            if identity in seen:
                if (
                    not config.fill_publications
                    and not filled_for_metadata
                    and config.delay_seconds > 0
                ):
                    time.sleep(config.delay_seconds)
                continue
            seen.add(identity)

            matches.append(publication)
            print(f"Found: {publication.get('bib', {}).get('title', title)} ({year})")

            if (
                not config.fill_publications
                and not filled_for_metadata
                and config.delay_seconds > 0
            ):
                time.sleep(config.delay_seconds)
    except Exception as exc:
        print(f"Search stopped early: {exc}", file=sys.stderr)

    return matches


def publication_type(pub: Publication) -> str:
    bib = pub.get("bib", {})
    pub_type = str(
        pub.get("pub_type") or pub.get("type") or bib.get("pub_type") or ""
    ).lower()
    title = str(bib.get("title", "")).lower()
    venue = str(
        bib.get("venue")
        or bib.get("journal")
        or bib.get("booktitle")
        or bib.get("conference")
        or ""
    ).lower()

    if "book" in pub_type or "book" in title:
        return "book"
    if (
        "conference" in pub_type
        or "inproceedings" in pub_type
        or "proceedings" in pub_type
        or "conference" in venue
        or "proceedings" in venue
        or usable_metadata(bib.get("booktitle")) is not None
        or usable_metadata(bib.get("conference")) is not None
    ):
        return "inproceedings"
    return "article"


def bibtex_venue_field(pub: Publication) -> tuple[str, str] | None:
    bib = pub.get("bib", {})

    if publication_type(pub) == "inproceedings":
        candidates = (
            ("booktitle", bib.get("booktitle")),
            ("booktitle", bib.get("conference")),
            ("booktitle", bib.get("venue")),
            ("journal", bib.get("journal")),
        )
    else:
        candidates = (
            ("journal", bib.get("journal")),
            ("journal", bib.get("venue")),
            ("booktitle", bib.get("booktitle")),
            ("booktitle", bib.get("conference")),
        )

    for field_name, value in candidates:
        clean_value = usable_metadata(value)
        if clean_value is not None:
            return field_name, clean_value

    return None


def bibtex_fields(pub: Publication) -> Iterable[tuple[str, Any]]:
    bib = pub.get("bib", {})
    field_map: list[tuple[str, Any]] = [
        ("title", bib.get("title")),
        ("author", bib.get("author")),
        ("year", bib.get("pub_year") or bib.get("year")),
    ]

    venue_field = bibtex_venue_field(pub)
    if venue_field is not None:
        field_map.append(venue_field)

    field_map.extend(
        [
            ("volume", bib.get("volume")),
            ("number", bib.get("number")),
            ("pages", bib.get("pages")),
            ("publisher", bib.get("publisher")),
            ("doi", bib.get("doi")),
            ("url", pub.get("pub_url") or pub.get("eprint_url") or pub.get("url")),
        ]
    )

    for name, value in field_map:
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
            field_value = format_bibtex_authors(value) if name == "author" else value
            lines.append(f"  {name} = {{{bibtex_escape(field_value)}}}{comma}")
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
