"""The font a plan is drawn in is the font that was picked.

Run from the repository root:

    python tests/font_test.py

A DXF text style names a font *file*, and the drawing service used to build
that name by adding ".ttf" to the family the user chose. That is right only
where a family and its file happen to share a name. On a developer Mac they
often do, so the bug was invisible there; on the container the service runs
in they never do, and every choice resolved to nothing and every sheet came
out in the same fallback face.

These checks are about resolution rather than about which fonts a particular
machine has -- that differs between a laptop and the image, which is the whole
reason ``plan_fonts`` exists. So: a family that is installed is drawn in
itself, choices that resolve stay distinct from one another, and a family that
is missing is substituted rather than dropped.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plan_fonts  # noqa: E402
from plans import CadastralPlan  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smoke_test import cadastral_payload  # noqa: E402


def check_every_choice_resolves():
    """No offered font leaves the sheet without one."""
    errors = []
    for choice in plan_fonts.SUPPORTED:
        resolved = plan_fonts.resolve(choice.family)
        if not resolved:
            errors.append(f"{choice.family!r} resolved to nothing")
    return errors


def check_installed_families_are_used():
    """A family this machine has is drawn in that family, not a lookalike.

    The matcher ezdxf offers scores families by similarity, and asked for
    "Arial" on this machine it answers Arial Unicode MS -- a different face
    with different widths. A user who picks Arial and has Arial gets Arial.
    """
    errors = []
    for report in plan_fonts.supported():
        if not report.installed:
            continue
        resolved = plan_fonts.resolve(report.family)
        faces = {f.filename for f in plan_fonts._faces_of(report.family)}
        if resolved not in faces:
            errors.append(
                f"{report.family!r} is installed but was drawn with {resolved!r}, "
                f"which is not one of its faces")
    return errors


def check_choices_stay_distinct():
    """Fonts that resolve at all resolve to different files.

    Not every family is installed everywhere, and one that is missing along
    with all of its substitutes shares the fallback -- that is reported rather
    than hidden. What must not happen is two *available* choices collapsing
    onto one face, which is the state the whole menu was in.
    """
    errors = []
    used = {}
    for report in plan_fonts.supported():
        if not report.drawn_as:
            continue        # nothing to draw it with here; reported as such
        resolved = plan_fonts.resolve(report.family)
        used.setdefault(resolved, []).append(report.family)

    for resolved, families in used.items():
        if len(families) > 1:
            errors.append(
                f"{families} all resolve to {resolved!r}, so those choices "
                f"cannot be told apart on the sheet")

    if len(used) < 2:
        errors.append(
            f"only {len(used)} distinct face(s) available, so this machine "
            f"cannot show the difference either way")
    return errors


def check_a_missing_font_is_substituted():
    """A family nothing can supply still draws, and says so."""
    errors = []
    resolved = plan_fonts.resolve("No Such Family At All")
    if not resolved:
        errors.append("an unknown family resolved to nothing at all")
    return errors


def check_the_sheet_uses_it():
    """The resolved file reaches the drawing, and the sheet is measured with
    it -- text widths decide the layout before anything is drawn."""
    errors = []
    widths = {}
    for report in plan_fonts.supported():
        if not report.installed:
            continue
        data = cadastral_payload()
        data["font"] = report.family
        plan = CadastralPlan(**data)
        plan.draw()

        style = plan._drawer.doc.styles.get("SURVEY_TEXT").dxf.font
        expected = plan_fonts.resolve(report.family)
        if style != expected:
            errors.append(
                f"{report.family!r}: the sheet's style is {style!r}, not {expected!r}")
        widths[report.family] = plan._drawer.text_width("SBD 1201", 2.5)

    if len(set(round(w, 3) for w in widths.values())) < 2:
        errors.append(
            "every font measured the same width, so the layout is not being "
            "measured with the chosen face")
    return errors


def main():
    failures = 0
    for name, fn in (
        ("every choice resolves", check_every_choice_resolves),
        ("an installed family is used", check_installed_families_are_used),
        ("choices stay distinct", check_choices_stay_distinct),
        ("a missing font is substituted", check_a_missing_font_is_substituted),
        ("the sheet is drawn and measured with it", check_the_sheet_uses_it),
    ):
        print(f"== {name} ==")
        errors = fn()
        for error in errors:
            failures += 1
            print("  FAIL:", error)
        if not errors:
            print("  OK")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
