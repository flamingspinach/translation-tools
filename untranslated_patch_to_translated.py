#!/usr/bin/env python
"""
Quick and dirty script that takes a patch file expressing the difference
between (say) an R-18 version of a game's untranslated script and its all-ages
counterpart and produces a patch file that will apply to the current
translation in a given directory of the R-18 version of the games's script.
This will allow the translator to more or less keep their translations in sync
between the two versions of the game script.

For new ('+') lines added by the patch, if the surrounding hunk appears to make
changes only, as opposed to adding or removing lines (i.e. if the hunk adds the
same number of lines it removes), then the script will populate the new lines'
translation fields with the existing translation of the line in the same
position in the "before" scripts.  The idea is that such hunks are probably
just fixing typos or bowdlerizing words or phrases, so it's easier to edit the
existing translation than rewrite it from scratch for these lines.

For other new ('+') lines added by the patch, i.e. in hunks that either have a
net increase or decrease in the number of lines, the translation field is left
blank.

All added translations for new lines will start with "#" to mark them for
review.

The file format the input patch applies to should be tab-separated values with
n columns (typically 2, character and line), and the file format the output
patch will apply to will be tab-separated values with n+1 columns, the new
column being for the translation of the line.

Usage:

  untranslated-patch-to-translated.py /path/to/translations < original.patch > fixed.patch
"""

import logging
import os
import sys

from unidiff import PatchSet  # type: ignore


def main():
    """Main function"""
    patch = PatchSet(sys.stdin)

    os.chdir(sys.argv[1])

    for f in patch:
        # f.path[2:] discards "a/" or "b/" prefixes from the patch's filenames
        with open(f.path[2:]) as handle:
            translations = handle.readlines()

        for h in f:
            for l in h:
                if l.is_context or l.is_removed:
                    linenum = l.source_line_no - 1  # patch line numbers are 1-based
                    # Populate the line the patch is removing or using for
                    # context with its corresponding translated value from the
                    # current translated scripts
                    translation = translations[linenum]
                    try:
                        # Check that the columns other than "translation" are
                        # identical; [:-1] ignores the final "\n".
                        assert translation[:-1].startswith(l.value[:-1])
                    except:
                        logging.warning(f"l.value: {repr(l.value)}")
                        logging.warning(
                            f"translations[linenum]: {repr(translations[linenum])}"
                        )
                        raise
                    l.value = translation
                elif h.added == h.removed:
                    # If this hunk has net 0 lines added/removed, it's probably
                    # some typo fixes or rewording of existing lines, so
                    # populate all new lines with the corresponding existing
                    # translations from the lines they're probably modified
                    # versions of.  But prepend a "#" so that we don't forget
                    # to check them and fix the translations as necessary.
                    linenum = l.target_line_no - h.target_start + h.source_start - 1
                    translation = translations[linenum]
                    l.value = l.value[:-1] + "\t#" + translation.split("\t")[-1]
                else:
                    # Lines the patch is adding should have their translation
                    # set to blank; "#" is a placeholder value for blank
                    # translations (see sync_vnt.py).
                    l.value = l.value[:-1] + "\t#\n"

    # Dump the fixed patch to stdout
    print(patch)


if __name__ == "__main__":
    main()
