""" Uses a Wikipedia dump to construct a mapping between Wikidata and Wikipedia.
    Credit: Originally used parts of https://github.com/jamesmishra/mysqldump-to-csv
"""

import gzip
import logging
import os
import re
import sqlite3
from typing import Iterator, Optional, Tuple

_logger = logging.getLogger(__name__)

# A quoted MySQL string with backslash escapes, e.g. 'D\'_boarische_Woocha'. Written as runs of
# unescaped characters (`[^'\\]*`) separated by escapes, which is much faster than a per-character
# alternation like `(?:[^'\\]|\\.)*`
_STRING = r"'([^'\\]*(?:\\.[^'\\]*)*)'"

# Rows are matched by their leading columns only, which works for one and for many rows per line.
# Quotes inside of strings are always escaped, so a match cannot start inside of a string.
# Only the rows that are needed match at all, so all other rows are skipped by the regex engine.
# https://www.mediawiki.org/wiki/Manual:Page_table: page_id, page_namespace = 0, page_title, ...
# Filter the namespace; only use real articles https://www.mediawiki.org/wiki/Manual:Namespace
_PAGE_ROW = re.compile(r"\((\d+),0," + _STRING)
# https://www.mediawiki.org/wiki/Manual:Page_props_table: pp_page, pp_propname, pp_value, ...
# The page property table contains many properties, we only care about the Wikidata id
_PAGE_PROPS_ROW = re.compile(r"\((\d+),'wikibase_item'," + _STRING)
# https://www.mediawiki.org/wiki/Manual:Redirect_table: rd_from, rd_namespace = 0, rd_title, ...
# We only care about redirects to main articles
_REDIRECT_ROW = re.compile(r"\((\d+),0," + _STRING)

_ESCAPE = re.compile(r"\\(.)")


def _unescape(value: str) -> str:
    return _ESCAPE.sub(r"\1", value) if "\\" in value else value


def _data_lines(path: str, errors: str) -> Iterator[str]:
    """Yields the lines of `path` that contain rows of an INSERT statement.

    Older dumps put all rows of an INSERT statement on the same line as `INSERT INTO ... VALUES`,
    newer ones (2025 onwards) put every row on its own line. Both layouts are supported.
    """
    with gzip.open(path, "rt", encoding="utf-8", errors=errors, newline="\n") as f:
        for line in f:
            if line.startswith("(") or line.startswith("INSERT INTO"):
                yield line


def _parse_pages(path: str) -> Iterator[Tuple[int, str]]:
    for line in _data_lines(path, errors="strict"):
        for page_id, title in _PAGE_ROW.findall(line):
            yield int(page_id), _unescape(title)


def _parse_wikidata_ids(path: str) -> Iterator[Tuple[str, int]]:
    # The page properties contain binary values that are not valid UTF-8
    for line in _data_lines(path, errors="ignore"):
        for page_id, value in _PAGE_PROPS_ROW.findall(line):
            yield _unescape(value), int(page_id)


def _parse_redirects(path: str) -> Iterator[Tuple[str, int]]:
    for line in _data_lines(path, errors="ignore"):
        for source_id, target_title in _REDIRECT_ROW.findall(line):
            yield _unescape(target_title), int(source_id)


def create_index(dumpname: str, path_to_dumps: str, path_to_db: Optional[str] = None) -> str:
    """Creates an index mapping Wikipedia page titles to Wikidata IDs and vice versa.
    This requires a previously downloaded dump `dumpname` in `path_to_dumps`.

    Args:
        dumpname(str): Name of the Wikipedia SQL  dump that should be used for creating an index.
        path_to_dumps(str): Folder in which the dump has been downloaded to.
        path_to_db(str): Path where the index will be saved to. Defaults to `index_${dump_name}.db`.

    Returns:
        str: The path to the created database.

    """
    if path_to_db is None:
        path_to_db = "index_{0}.db".format(dumpname)

    _logger.info("Creating index for [%s] in [%s]", dumpname, path_to_db)

    pages_dump = os.path.join(path_to_dumps, dumpname + "-page.sql.gz")
    page_props_dump = os.path.join(path_to_dumps, dumpname + "-page_props.sql.gz")
    redirects_dump = os.path.join(path_to_dumps, dumpname + "-redirect.sql.gz")

    for dump in [pages_dump, page_props_dump, redirects_dump]:
        if not os.path.isfile(dump):
            raise FileNotFoundError("Dump [{0}] does not exist".format(dump))

    # (Re)Create the database file
    try:
        os.remove(path_to_db)
    except FileNotFoundError:
        pass

    conn = sqlite3.connect(path_to_db, isolation_level="EXCLUSIVE")

    with conn:
        conn.execute(
            """CREATE TABLE mapping (
            wikipedia_id int PRIMARY KEY ,
            wikipedia_title text,
            wikidata_id text)"""
        )

    # Parse the Wikipedia page dump; extract page id and page title from the sql
    _logger.info("Parsing pages dump")
    with conn:
        count = conn.executemany(
            "INSERT INTO mapping (wikipedia_id, wikipedia_title) VALUES (?, ?)",
            _parse_pages(pages_dump),
        ).rowcount
    _logger.info("Inserted [%d] pages", count)

    if count == 0:
        conn.close()
        raise ValueError(
            "No pages could be parsed from [{0}], the dump format may have changed".format(
                pages_dump
            )
        )

    # We create this index here as all titles have been inserted now.
    # Doing it earlier would recreate the index on every title insert.
    _logger.info("Creating database index on 'wikipedia_title'")
    with conn:
        conn.execute("""CREATE UNIQUE INDEX idx_wikipedia_title ON mapping(wikipedia_title);""")

    # Parse the Wikipedia page property dump; extract page id and Wikidata id from the sql
    _logger.info("Parsing page properties dump")
    with conn:
        count = conn.executemany(
            "UPDATE mapping SET wikidata_id = ? WHERE wikipedia_id = ?",
            _parse_wikidata_ids(page_props_dump),
        ).rowcount
    _logger.info("Mapped [%d] pages to Wikidata", count)

    # Parse the Wikipedia redirect dump; give redirects the Wikidata id of their target
    _logger.info("Parsing redirects dump")
    with conn:
        conn.executemany(
            """UPDATE mapping SET wikidata_id = COALESCE(
                (SELECT target.wikidata_id FROM mapping AS target WHERE target.wikipedia_title = ?),
                wikidata_id
            ) WHERE wikipedia_id = ?""",
            _parse_redirects(redirects_dump),
        )

    _logger.info("Creating database index on 'wikidata_id'")
    with conn:
        conn.execute("""CREATE INDEX idx_wikidata_id ON mapping(wikidata_id);""")

    total, mapped = conn.execute("SELECT count(*), count(wikidata_id) FROM mapping").fetchone()
    _logger.info("Index contains [%d] pages, [%d] of them mapped to Wikidata", total, mapped)

    conn.close()

    return path_to_db
