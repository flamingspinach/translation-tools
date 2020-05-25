#!/usr/bin/env python
"""
Script for syncing translations between the VNT server and a local TSV
format that's human readable and convenient to edit in a text editor.

Uses ~/.netrc for login credentials.
"""

import argparse
import os
from typing import Any, Dict, Hashable, Iterable, Iterator, List, Optional, Set, Tuple

import requests

VNT_ENDPOINT = "https://legacy.vntt.app/api/v1"
TSVTriple = Tuple[str, str, str]

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


def generate_tsv_from_vnt(vnt: Iterable[dict]) -> Iterator[TSVTriple]:
    """
    Takes JSON data from VNT for a given script file and dumps it to a list of
    triples in the format used by the local TSV files.
    """
    for i, line in enumerate(vnt, start=1):
        char = line["character_name"]
        orig = line["original"]
        trans = ""
        if line["translations"]:
            trans = line["translations"][0]["translation"]
            line_number = line["line_number"] + 1
            if trans == "#":
                raise ValueError(
                    f"Translation at line {i} ({line_number} on VNT) is the reserved string '#'"
                )
            if "\n" in trans:
                trans = trans.strip()
                if "\n" in trans:
                    raise ValueError(
                        f"Translation at line {i} ({line_number} on VNT) contains an internal newline"
                    )
                print(f"Stripped newline(s) from line {i} ({line_number} on VNT)")
        yield char, orig, trans


def dump_tsv_file(tsv: Iterable[TSVTriple], filename: str):
    """Takes a list of triples and dumps it directly to a local TSV file."""

    # if dry_run:
    #     print(f"        Would have dumped the following lines to '{filename}':")
    #     filename = "/dev/stdout"
    with open(filename, "w") as f:
        for char, orig, trans in tsv:
            if trans == "":
                trans = "#"
            print("\t".join((char, orig, trans)), file=f)


def submit_updates(updates: List[Tuple[int, str]]):
    """
    Submits updates, i.e. translations found locally that weren't equal to the
    current translation on VNT if any.
    """

    def chunks(l: List, size: int):
        while l:
            yield l[:size]
            l = l[size:]

    for chunk in chunks(updates, 25):
        if dry_run:
            print("        The following lines would have been uploaded:")
            for (line_id, trans) in chunk:
                print(f"{line_id}: {trans}")
            continue
        payload = [
            {"line": {"id": line_id}, "translation": trans, "language": {"code": "en"}}
            for line_id, trans in chunk
        ]
        res = requests.post(f"{VNT_ENDPOINT}/translations.json", json=payload)
        res.raise_for_status()


def load_tsv_file(filename: str) -> Iterator[TSVTriple]:
    """
    Read a list of triples from a local TSV file.  Each triple is of the form
    (char, orig, trans) where char is the character for dialogue lines else '',
    orig is the original Japanese line, and trans is the translated (English)
    line if any else ''.
    """
    with open(filename, "r") as f:
        for line in f:
            char, orig, trans = line.rstrip("\n").split("\t")
            if trans == "#":
                trans = ""
            yield char, orig, trans


def compare_lines(
    tsv_lines: List[TSVTriple], vnt_triples: Iterable[TSVTriple], vnt_lines: List[dict]
) -> Tuple[List[TSVTriple], List[Tuple[int, str]]]:
    """
    Compares triples from a TSV and JSON objects from a VNT script file, line
    by line, to see if there are any updates that need to be made, i.e. new
    translations locally that need to be uploaded to VNT or new translations on
    VNT that need to be downloaded into the TSV file.

    When translations differ between local and VNT, we push the local
    translation to VNT.  But if there are any cases where a local translation
    is equal to an old translation in the VNT history, we pause to let the user
    abort, to avoid overwriting someone else's work.

    Also, we first check to make sure that the TSV and the VNT script have
    exactly the same original Japanese.  If not, we make the user fix the TSV
    file manually instead of trying to do anything too clever.  A TSV dump of
    the VNT script is saved locally for reference.
    """
    # pylint: disable=too-many-locals

    if len(tsv_lines) != len(vnt_lines):
        raise ValueError(
            f"Different number of lines in TSV file ({len(tsv_lines)}) and "
            f"VNT script ({len(vnt_lines)}); please reconcile and rerun"
        )

    rollback_messages = []
    updates = []
    tsv_lines_new = []
    for i, ((char0, orig0, trans0), (char1, orig1, trans1), line) in enumerate(
        zip(tsv_lines, vnt_triples, vnt_lines)
    ):
        if char0 != char1 or orig0 != orig1:
            raise ValueError(
                f"TSV file and VNT script differ in original text at line {i+1}:\n"
                f"  TSV: char={char0}, orig={orig0}\n"
                f"  VNT: char={char1}, orig={orig1}"
            )
        if not trans0:
            # Local has no translation, so set it from VNT
            trans0 = trans1
        elif trans0 != trans1:
            # Local has a translation and VNT has a different one or none, so
            # set VNT's to what we have.
            # print(f"local={repr(trans0)}, remote={repr(trans1)}")
            updates.append((line["id"], trans0))
            # But if what we have is in the past history on VNT, then what's on
            # VNT may be newer than what we have, so confirm with the user
            # before sending the updates later.
            if trans0 in (x["translation"] for x in line["translations"]):
                rollback_messages.append(
                    f"Line {i+1} ({line['line_number']+1}): "
                    f"char={repr(char0)}, orig={repr(orig0)}, "
                    f"local-translation={repr(trans0)}, "
                    f"current-vnt-translation={repr(trans1)}"
                )
        tsv_lines_new.append((char0, orig0, trans0))

    if rollback_messages:
        print(
            f"WARNING: {len(rollback_messages)} translations found locally appear to equal "
            f"older entries in the history on VNT.  You may be reverting someone else's changes. "
            f"What should we do?"
        )
        while True:
            action = input("Type 'abort', 'print', or 'proceed': ")
            if action == "abort":
                raise Exception("Operation aborted")
            if action == "print":
                for message in rollback_messages:
                    print(message)
            if action == "proceed":
                break

    return tsv_lines_new, updates


def sync_project(codename: str, directory: str):
    """
    Given a project, download all its script files from VNT into the current
    directory as TSV files.  If any of the target TSV files already exist,
    compare the existing TSV file with what would be written, and upload
    locally found translations to VNT.
    """
    os.chdir(directory)
    project_id = get_project_id(codename)
    scripts = get_project_scripts(project_id)

    all_updates: List[Tuple[int, str]] = []
    for script in scripts:
        vnt_filename = script["original_filename"]
        tsv_filename = os.path.splitext(vnt_filename)[0] + ".tsv"
        if script['line_count'] == '0':
            print(f"---x Skipping {vnt_filename} on VNT because it is empty.")
            continue
        print(f"--- Syncing {vnt_filename} on VNT to {tsv_filename} on disk.")

        vnt_lines = get_script_lines(script["id"])
        vnt_triples = list(generate_tsv_from_vnt(vnt_lines))
        if not os.path.exists(tsv_filename):
            print(f"Initial download of {tsv_filename}.")
            dump_tsv_file(vnt_triples, tsv_filename)
            continue
        tsv_lines = list(load_tsv_file(tsv_filename))

        # raise IOError("oops")

        try:
            tsv_lines_new, updates = compare_lines(tsv_lines, vnt_triples, vnt_lines)
        except:
            tsv_filename = tsv_filename + ".1"
            print(f"Dumping VNT script {vnt_filename} to {tsv_filename}.")
            dump_tsv_file(vnt_triples, tsv_filename)
            raise

        num_local_updates = sum(
            1 for (_, _, a), (_, _, b) in zip(tsv_lines, tsv_lines_new) if a != b
        )
        print(f"Updating {num_local_updates} translations in {tsv_filename}.")
        if num_local_updates > 0:
            dump_tsv_file(tsv_lines_new, tsv_filename)

        print(f"Queueing {len(updates)} translations to upload from {tsv_filename}.")
        all_updates += updates

    print("Submitting updates, please confirm.")
    action = input("Type yes/no: ")
    if action == 'yes':
        print("Uploading updates to VNT...")
        submit_updates(all_updates)
        print("Done.")
    else:
        print("Aborting.")


def main():
    """Entrypoint"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "project_name", help="Codename for the project on VNT. Seen in the UI URLs."
    )
    parser.add_argument(
        "--directory", default=".", help="Directory to store the TSV file."
    )
    args = parser.parse_args()

    sync_project(args.project_name, args.directory)


if __name__ == "__main__":
    main()
