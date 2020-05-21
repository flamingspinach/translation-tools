#!/usr/bin/env python
"""
Script for syncing translations between the VNT server and a local TSV
format that's human readable and convenient to edit in a text editor.

Uses ~/.netrc for login credentials.
"""

import argparse
import logging as log
import os
from typing import Any, Dict, Hashable, Iterable, Iterator, List, Optional, Set, Tuple

import requests

VNT_ENDPOINT = "https://legacy.vntt.app/api/v1"

dry_run = False


def find_a_duplicate(xs: Iterable) -> Optional[Tuple[Hashable, int]]:
    """
    Check whether an iterator contains multiple copies of the same element.
    The element type must be hashable.
    """
    seen: Set[Hashable] = set()
    for x in xs:
        if x in seen:
            return x
        seen.add(x)
    return None


def get_project_id(codename: str) -> int:
    """
    Find the numeric project ID from the project codename string.
    """
    res = requests.get(f"{VNT_ENDPOINT}/projects.json")
    res.raise_for_status()

    for project in res.json():
        if project["codename"] == codename:
            return int(project["id"])
    raise ValueError(f"Couldn't find project with codename '{codename}'")


def get_project_scripts(project_id: int) -> List[dict]:
    """
    Return all the scripts in a given project.  The information is stored in
    whatever JSON-derived dict format the server returns it in (there's no spec
    currently so I won't write one either).
    """
    res = requests.get(
        f"{VNT_ENDPOINT}/projects/{project_id}/script/files.json?limit=0"
    )
    res.raise_for_status()
    scripts: List[Dict[str, Any]] = res.json()

    # Ensure that we can name local TSV files after the "original_filename"
    # attribute of the files on VNT without there being any clashes.
    assert not find_a_duplicate(
        os.path.splitext(s["original_filename"])[0] for s in scripts
    )
    # Enforce that there's no directory structure in "original_filename" for
    # now.
    assert all("/" not in s["original_filename"] for s in scripts)

    return scripts


def get_script_lines(file_id: int) -> List[dict]:
    """
    Reads JSON data from VNT for all the lines in a script file.  The
    information is stored in whatever JSON-derived dict format the server
    returns it in (there's no spec currently so I won't write one either).
    """
    res = requests.get(f"{VNT_ENDPOINT}/project_files/{file_id}/lines.json?limit=0")
    res.raise_for_status()
    lines: List[dict] = res.json()

    for line in lines:
        # Remove non-English translations since I only know English and
        # Japanese lol (feel free to generalize this if you are not me)
        line["translations"] = [
            x for x in line["translations"] if x["language"]["code"] == "en"
        ]

    return lines


def generate_tsv_from_vnt(vnt: Iterable[dict]) -> Iterator[Tuple[str, str, str]]:
    """
    Takes JSON data from VNT for a given script file and dumps it to a list of
    triples in the format used by the local TSV files.
    """
    for index, line in enumerate(vnt, start=1):
        char = line["character_name"]
        orig = line["original"]
        trans = ""
        if line["translations"]:
            trans = line["translations"][0]["translation"]
            if trans == "#":
                raise ValueError(
                    f"Translation at line {index} ({line['line_number']} on VNT) is the reserved string '#'"
                )
            if "\n" in trans:
                raise ValueError(
                    f"Translation at line {index} ({line['line_number']} on VNT) contains a newline"
                )
        if not trans:
            trans = "#"
        yield char, orig, trans


def dump_tsv(tsv: Iterable[Tuple[str, str, str]], filename: str):
    """Takes a list of triples and dumps it directly to a local TSV file."""
    if dry_run:
        log.info(f"        Would have dumped the following lines to '{filename}':")
        filename = "/dev/stdout"
    with open(filename, "w") as f:
        for char, orig, trans in tsv:
            f.write(f"{char}\t{orig}\t{trans}\n")


def sync_tsv_with_vnt(tsv: List[Tuple[str, str, str]], vnt: List[dict]):
    """
    Compares triples from a TSV and JSON objects from a VNT script file, line
    by line, to see if there are any updates that need to be made, i.e. new
    translations locally that need to be uploaded to VNT or new translations on
    VNT that need to be downloaded into the TSV file.

    When translations differ between local and remote, if the local translation
    is novel (doesn't exist in the history on VNT) we use it, and if it is not
    novel (does exist in the history on VNT), we ask the user whether to use it
    or overwrite it with the one from VNT.

    Also, we first check to make sure that the TSV and the VNT script have
    exactly the same original Japanese.

    If not, we make the user fix the TSV file manually instead of trying to do
    anything too clever.  A TSV dump of the VNT script is saved locally for
    reference.
    """
    vnt_triples = list(generate_tsv_from_vnt(vnt))
    if len(tsv) != len(vnt_triples):
        raise ValueError(
            f"Different number of lines in TSV file ({len(tsv)}) and "
            f"VNT script ({len(vnt_triples)}), please reconcile and rerun"
        )
    for i, ((char, orig, _), line) in enumerate(zip(tsv, vnt), start=1):
        if char != line["character_name"] or orig != line["original"]:
            raise ValueError(
                f"VNT script and TSV file differ in original text at line {i}:\n"
                f"  VNT: char={line['character_name']}, orig={line['original']}\n"
                f"  TSV: char={char}, orig={orig}"
            )


def submit_translations(translations: List[Tuple[int, str]]):
    """
    Submit translations found locally that weren't equal to the current
    translation on VNT if any.

    Of these, if a translation was found to be in VNT's history for that line
    despite not being the current translation, we prompt the user for
    confirmation that they want to rollback to an earlier translation.
    """

    def chunks(l: List, size: int):
        while l:
            yield l[:size]
            l = l[size:]

    for chunk in chunks(translations, 25):
        if dry_run:
            log.info("        The following lines would have been uploaded:")
            for (line_id, trans) in chunk:
                log.info(f"{line_id}: {trans}")
            continue
        payload = [
            {"line": {"id": line_id}, "translation": trans, "language": {"code": "en"}}
            for line_id, trans in chunk
        ]
        res = requests.post(f"{VNT_ENDPOINT}/translations.json", json=payload)
        res.raise_for_status()


def load_tsv_file(filename: str) -> Iterator[Tuple[str, str, str]]:
    """
    Read a list of triples from a local TSV file.  Each triple is of the form
    (char, orig, trans) where char is the character for dialogue lines else '',
    orig is the original Japanese line, and trans is the translated (English)
    line if any else ''.
    """
    with open(filename, "r") as f:
        for line in f:
            char, orig, trans = line.split("\t")
            yield char, orig, trans


def sync_project(codename: str, directory: str = "."):
    """
    Given a project, download all its script files from VNT into the current
    directory as TSV files.  If any of the target TSV files already exist,
    compare the existing TSV file with what would be written, and upload
    locally found translations to VNT.
    """
    os.chdir(directory)
    project_id = get_project_id(codename)
    scripts = get_project_scripts(project_id)
    for script in scripts:
        vnt_filename = script['original_filename']
        tsv_filename = os.path.splitext(vnt_filename)[0] + ".tsv"
        log.info(f"Syncing '{vnt_filename}' on VNT to '{tsv_filename}' on disk")
        vnt_lines = get_script_lines(script["id"])
        if not os.path.exists(tsv_filename):
            dump_tsv(generate_tsv_from_vnt(vnt_lines), tsv_filename)
            continue
        raise IOError("oops")


def main():
    """Entrypoint"""
    parser = argparse.ArgumentParser()
    parser.add_argument()


if __name__ == "__main__":
    main()
