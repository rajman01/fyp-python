"""The font a plan is drawn in is the font that was picked.

Run from the repository root:

    python tests/font_test.py

A DXF text style names a font *file*, and the drawing service used to build
that name by adding ".ttf" to the family the user chose. That is right only
where a family and its file happen to share a name. On a developer Mac they
often do, so the bug was invisible there; on the container the service runs
in they never do, so every choice resolved to nothing and every sheet came
out in the same fallback face.

These checks are about resolution and about the menu being honest, not about
which fonts a particular machine has -- that differs between a laptop and the
image, which is the whole reason ``plan_fonts`` exists. So: an offered family
is drawn as itself, no two entries draw the same face, and a family that has
gone missing is still substituted rather than dropped.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plan_fonts  # noqa: E402
from plans import CadastralPlan  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_test import cadastral_payload  # noqa: E402


def check_only_installed_fonts_are_offered():
    """Nothing is offered that would be quietly substituted.

    The menu is what a user can pick, so every entry has to be a family this
    machine draws as itself. Offering one that is stood in for is the same
    fault as the guessed filename, just quieter: picking Verdana on the
    deployed image drew DejaVu Sans and nothing on the sheet said so.
    """
    return [
        f"{report.family!r} is offered but is not installed here, so it would "
        f"be drawn in something else"
        for report in plan_fonts.supported()
        if plan_fonts._installed(report.family) is None
    ]


def check_an_offered_family_is_drawn_as_itself():
    """The face used is one of that family's own.

    The matcher ezdxf offers scores families by similarity, and asked for
    "Arial" on this machine it answers Arial Unicode MS -- a different face
    with different widths. A user who picks Arial and has Arial gets Arial.
    """
    errors = []
    for report in plan_fonts.supported():
        resolved = plan_fonts.resolve(report.family)
        faces = {f.filename for f in plan_fonts._faces_of(report.family)}
        if resolved not in faces:
            errors.append(
                f"{report.family!r} is offered but was drawn with {resolved!r}, "
                f"which is not one of its faces")
    return errors


def check_no_two_entries_draw_the_same_face():
    """Arial and Liberation Sans are one design under two names, and a machine
    with both mapped to one file would list both -- two menu entries producing
    the same sheet."""
    errors = []
    used = {}
    for report in plan_fonts.supported():
        used.setdefault(plan_fonts.resolve(report.family), []).append(report.family)
    for resolved, families in used.items():
        if len(families) > 1:
            errors.append(f"{families} all draw {resolved!r}")
    return errors


def check_the_default_is_installed():
    """A new plan starts in a font this machine actually has, or every one of
    them is drawn in a face nobody chose."""
    default = plan_fonts.default_family()
    if plan_fonts.supported() and plan_fonts._installed(default) is None:
        return [f"the default {default!r} is not installed here"]
    return []


def check_a_missing_font_is_substituted():
    """A plan saved with a font this machine has since lost still draws.

    Substitution is kept for exactly this: not for anything offered today, but
    for a plan that was made when the font was there.
    """
    if not plan_fonts.resolve("No Such Family At All"):
        return ["an unknown family resolved to nothing at all"]
    return []


def check_a_substitution_is_reported_once():
    """A missing font is reported once per worker, not once per sheet.

    Whether a font is installed is a fact about the machine, so it does not
    change between requests -- but it was logged twice for every plan drawn.
    Twice because a plan too big for its paper is rebuilt at the scale that
    fits, and each rebuild sets the text style up again. Repeating a fixed
    fact per drawer build buries the lines that are about the request.
    """
    import logging

    class Collector(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    collector = Collector()
    logger = logging.getLogger("plan_fonts")
    logger.addHandler(collector)
    remembered = set(plan_fonts._reported)
    try:
        plan_fonts._reported.clear()
        for _ in range(6):
            plan_fonts.resolve("No Such Family At All")
    finally:
        logger.removeHandler(collector)
        plan_fonts._reported.clear()
        plan_fonts._reported.update(remembered)

    if len(collector.lines) != 1:
        return [f"six resolutions logged {len(collector.lines)} lines, not one"]
    return []


def check_the_sheet_is_drawn_and_measured_with_it():
    """The resolved file reaches the drawing, and the layout is measured with
    it -- text widths decide the sheet before anything is drawn."""
    errors = []
    widths = {}
    for report in plan_fonts.supported():
        data = cadastral_payload()
        data["font"] = report.family
        plan = CadastralPlan(**data)
        plan.draw()

        style = plan._drawer.doc.styles.get("SURVEY_TEXT").dxf.font
        expected = plan_fonts.resolve(report.family)
        if style != expected:
            errors.append(
                f"{report.family!r}: the sheet's style is {style!r}, "
                f"not {expected!r}")
        widths[report.family] = plan._drawer.text_width("SBD 1201", 2.5)

    if len(widths) > 1 and len({round(w, 3) for w in widths.values()}) < 2:
        errors.append(
            "every font measured the same width, so the layout is not being "
            "measured with the chosen face")
    return errors


def main():
    failures = 0
    for name, fn in (
        ("only installed fonts are offered", check_only_installed_fonts_are_offered),
        ("an offered family is drawn as itself", check_an_offered_family_is_drawn_as_itself),
        ("no two entries draw the same face", check_no_two_entries_draw_the_same_face),
        ("the default is installed", check_the_default_is_installed),
        ("a missing font is substituted", check_a_missing_font_is_substituted),
        ("a substitution is reported once", check_a_substitution_is_reported_once),
        ("the sheet is drawn and measured with it",
         check_the_sheet_is_drawn_and_measured_with_it),
    ):
        print(f"== {name} ==")
        errors = fn()
        for error in errors:
            failures += 1
            print("  FAIL:", error)
        if not errors:
            print("  OK")

    print(f"\n{failures} failure(s)" if failures
          else f"\nall font checks pass ({len(plan_fonts.supported())} fonts offered here)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
