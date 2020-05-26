#!/usr/bin/env python
"""
Script for syncing translations between the VNT server and a local TSV
format that's human readable and convenient to edit in a text editor.

Uses ~/.netrc for login credentials.
"""

import argparse
import json
import os
from typing import Any, Dict, Hashable, Iterable, Iterator, List, Set, Tuple

import requests
from progressbar import progressbar  # type: ignore

TSVTriple = Tuple[str, str, str]

VNT_ENDPOINT = "https://legacy.vntt.app/api/v1"
UPLOAD_CHUNK_SIZE = 25

dry_run = False


def find_a_duplicate(xs: Iterable[Hashable]) -> bool:
    """
    Check whether an iterator contains multiple copies of the same element.
    The element type must be hashable.
    """
    seen: Set[Hashable] = set()
    for x in xs:
        if x in seen:
            return True
        seen.add(x)
    return False


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
        if "\n" in orig.strip("\n") or "\n" in trans.strip("\n"):
            raise ValueError(
                f"Original or translated text at line {i} ({line_number} on VNT) contains an internal newline"
            )
        if orig.strip("\n") != orig or trans.strip("\n") != trans:
            print(
                # actually only strip if/when this is eventually written to a file
                f"WARNING: stripping leading or trailing newline(s) from original and/or translated text at line {i} ({line_number} on VNT)"
            )
        yield char, orig, trans


def dump_tsv_file(tsv: Iterable[TSVTriple], filename: str):
    """Takes a list of triples and dumps it directly to a local TSV file."""
    with open(filename, "w") as f:
        for char, orig, trans in tsv:
            if trans == "":
                trans = "#"
            print("\t".join((char, orig.strip("\n"), trans.strip("\n"))), file=f)


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

    - If both sides have identical translations, we do nothing.

    - When there is no local translation, we download VNT's translation.

    - When there is no VNT translation, we upload our local translation.

    - If the two sides have differing translations and the local one is present
      in VNT's history list, we download VNT's translation (i.e. we presume the
      local one is outdated).

      * NOTE: To make sure you don't lose any local changes, make sure to
        commit your local files before running this script.

    - If the two sides have differing translations and the local one is not
      present in VNT's history list, we upload the local translation (i.e. we
      presume the VNT one is outdated).

      * NOTE: To make sure you don't clobber any remote changes, you'll have to
        do some manual version control stuff (though the script will prompt you
        before continuing if this case is ever encountered).  Namely:

        1. Before running this script, commit your changes and then check out
           an old commit from the last time you ran the script.

        2. Run the script, which will download anything that appeared on VNT
           since you last ran the script.

        3. Commit any changes that were made by the script onto a new branch.

        4. Merge the branch into your master branch.

        5. Run the script again to sync the merged changes to VNT.

        TODO: Do the above automatically in this script by calling out to git.

        Of course, if you're the only person working on this project on VNT,
        probably don't bother with all this, since you know what you're doing.

    Also, we first check to make sure that the TSV and the VNT script have
    exactly the same original Japanese.  If not, we make the user fix the TSV
    file manually instead of trying to do anything too clever.  A TSV dump of
    the VNT script is saved locally for reference.
    """
    # pylint: disable=too-many-locals, too-many-branches

    if len(tsv_lines) != len(vnt_lines):
        raise ValueError(
            f"Different number of lines in TSV file ({len(tsv_lines)}) and "
            f"VNT script ({len(vnt_lines)}); please reconcile and rerun"
        )

    overwrite_info = []
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
        if trans0 == trans1:
            pass
        elif not trans0 or trans0 in (x["translation"] for x in line["translations"]):
            trans0 = trans1
        else:
            updates.append((line["id"], trans0))
            if trans1:
                author = line["translations"][0]["created_by"]["username"]
                overwrite_info.append(
                    {
                        "line": i + 1,
                        "line_no": line["line_number"] + 1,
                        "char": char0,
                        "orig": orig0,
                        "local": trans0,
                        "vnt": trans1,
                        "vnt-author": author,
                    }
                )
        tsv_lines_new.append((char0, orig0, trans0))

    if overwrite_info:
        print(
            f"WARNING: In {len(overwrite_info)} cases, found a local translation neither matching "
            f"VNT's translation nor existing in VNT's history.  We assume these translations are "
            f"novel and will upload them to VNT, but this will overwrite the corresponding "
            f"translations on VNT.  What should we do?"
        )
        while True:
            action = input("Type 'abort', 'print', or 'proceed': ")
            if action == "abort":
                raise Exception("Operation aborted")
            if action == "print":
                # You can use `jq` or something with `xclip` to comb this
                # output to make sure you're only clobbering your own stuff
                for info in overwrite_info:
                    print(json.dumps(info, ensure_ascii=False))
            if action == "proceed":
                break

    return tsv_lines_new, updates


def submit_updates(updates: List[Tuple[int, str]]):
    """
    Submits updates, i.e. translations found locally that weren't equal to the
    current translation on VNT if any.
    """

    def chunks(l: List, size: int):
        while l:
            yield l[:size]
            l = l[size:]

    for chunk in progressbar(list(chunks(updates, UPLOAD_CHUNK_SIZE))):
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
        if script["line_count"] == 0:
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

    if not all_updates:
        print("Nothing to upload; done.")
        return

    print(f"Submitting {len(all_updates)} updates, please confirm.")
    action = None
    while action not in {"yes", "no"}:
        action = input("Type yes/no: ")
    if action == "yes":
        print(f"Uploading {len(all_updates)} updated translations to VNT...")
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
