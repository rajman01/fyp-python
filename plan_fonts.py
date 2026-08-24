"""Which fonts a plan can be drawn in, and what to do when one is missing.

The style a DXF carries names a *font file*, not a family, and the drawing
service used to build that name by sticking ".ttf" on the end of whatever the
user picked. On a machine with the Microsoft core fonts installed that works
by accident -- "Arial" really is Arial.ttf there. On the container the service
actually runs in it does not: that image carries the Liberation faces, whose
files are LiberationSans-Regular.ttf and friends, so *every* choice resolved
to nothing and every plan came out in the one fallback face. The dropdown
offered five fonts and the sheet only ever showed one.

So a family is resolved here rather than guessed:

  * the family itself, if this machine has it;
  * else a metric-compatible substitute that it does have -- Arial and
    Liberation Sans are the same widths by design, which matters because the
    sheet was measured with one and is drawn with the other;
  * else the fallback face, which at least prints.

``supported()`` reports what this machine can actually honour, so the app can
offer the fonts that will be used rather than the fonts we hoped for.
"""
from typing import List, NamedTuple, Optional
import logging

from ezdxf.fonts import fonts

logger = logging.getLogger(__name__)


class FontChoice(NamedTuple):
    """A family the app offers, and what to draw with when it is absent.

    ``substitutes`` are in preference order and are metric-compatible with the
    family wherever possible: the sheet's layout is measured from the font's
    own widths before anything is drawn, so a substitute of different
    proportions moves text that was already fitted to its space.
    """
    family: str
    substitutes: tuple = ()
    #: What the family is for, shown beside it in the app.
    note: str = ""


#: The families the app offers, in the order it offers them.
#:
#: Deliberately a curated list rather than everything installed. A developer
#: machine has hundreds of families and the container a few dozen, so
#: enumerating them would give every environment a different menu -- and most
#: of what a laptop carries is no use on a survey plan anyway.
#:
#: The names are the ones a surveyor will look for, and behind each is the
#: open face that stands in for it where it is absent. Those substitutes are
#: chosen so the *choices stay distinct* on the container as well as on a
#: developer machine: picking Palatino and picking Courier New should not
#: produce the same sheet just because neither font is installed. All twelve
#: resolve to twelve different faces on the deployed image.
SUPPORTED = (
    # Sans
    FontChoice("Arial", ("Liberation Sans", "Arimo", "Nimbus Sans", "FreeSans",
                         "DejaVu Sans"),
               "Plain sans-serif; the usual choice"),
    FontChoice("Helvetica", ("Nimbus Sans", "Liberation Sans", "Arimo", "FreeSans"),
               "Sans-serif, slightly tighter than Arial"),
    FontChoice("Verdana", ("DejaVu Sans", "Liberation Sans", "Arimo"),
               "Sans-serif, wide; reads well at small sizes"),
    FontChoice("Tahoma", ("Liberation Sans Narrow", "Nimbus Sans Narrow",
                          "DejaVu Sans Condensed", "Liberation Sans"),
               "Sans-serif, narrow; fits more in a column"),
    FontChoice("Trebuchet MS", ("FreeSans", "DejaVu Sans", "Liberation Sans"),
               "Sans-serif, humanist"),
    FontChoice("Century Gothic", ("URW Gothic", "FreeSans", "DejaVu Sans"),
               "Geometric sans-serif"),

    # Serif
    FontChoice("Times New Roman", ("Liberation Serif", "Tinos", "Nimbus Roman",
                                   "FreeSerif", "DejaVu Serif"),
               "Serif; traditional on cadastral sheets"),
    FontChoice("Georgia", ("DejaVu Serif", "P052", "C059", "Liberation Serif"),
               "Serif, wide; sturdy at small sizes"),
    FontChoice("Palatino", ("P052", "URW Bookman", "FreeSerif"),
               "Serif, calligraphic"),
    FontChoice("Century Schoolbook", ("C059", "URW Bookman", "Liberation Serif"),
               "Serif, open; a drafting standard"),
    FontChoice("Bookman Old Style", ("URW Bookman", "C059", "P052"),
               "Serif, heavy"),

    # Monospaced
    FontChoice("Courier New", ("Liberation Mono", "Cousine", "Nimbus Mono PS",
                               "FreeMono", "DejaVu Sans Mono"),
               "Monospaced; coordinates line up in columns"),
)

DEFAULT_FAMILY = "Arial"


def _faces_of(family: str) -> list:
    """Every face this machine has for exactly this family.

    Read off the font manager's cache rather than through
    ``find_best_match``, which scores families by similarity and so answers
    "Arial" with Arial Unicode MS -- a different face, different widths, and
    not the one the user picked. Guarded because the cache is ezdxf's own
    structure: if a future version moves it, plans should still be drawn, just
    with the matcher's answer instead.
    """
    wanted = family.casefold()
    try:
        entries = fonts.font_manager._font_cache._cache.values()
    except AttributeError:  # pragma: no cover - depends on the ezdxf version
        logger.warning("cannot read the ezdxf font cache; falling back to matching")
        return []
    return [e.font_face for e in entries if e.font_face.family.casefold() == wanted]


def _installed(family: str) -> Optional[str]:
    """The file this machine would draw ``family`` with, or None."""
    if not family:
        return None

    faces = _faces_of(family)
    if faces:
        # The upright, normal-weight face. Bold and italic are applied per run
        # by the MText markup, so the style has to carry the plain one or the
        # whole sheet comes out bold. Chosen on weight and the is_italic flag
        # rather than on the style name, which is localised -- this machine
        # calls Tahoma's regular face "Κανονικά", and a name test picked the
        # bold one.
        def plainness(face):
            return (bool(face.is_italic or face.is_oblique),
                    abs((face.weight or 400) - 400))

        return min(faces, key=plainness).filename

    # Nothing under that exact name. The matcher is a reasonable last word on
    # a family this machine does not have -- it is only its confidence about
    # families it *does* have that is the problem.
    face = fonts.find_best_match(family=family)
    return face.filename if face is not None else None


def _choice(family: str) -> Optional[FontChoice]:
    wanted = (family or "").strip().casefold()
    for choice in SUPPORTED:
        if choice.family.casefold() == wanted:
            return choice
    return None


def resolve(family: str) -> str:
    """The font file to draw ``family`` with on this machine.

    Never raises and never returns nothing: a plan is still worth drawing in
    the wrong face, and the caller has no better answer than this one.
    """
    filename = _installed(family)
    if filename:
        return filename

    choice = _choice(family)
    for substitute in (choice.substitutes if choice else ()):
        filename = _installed(substitute)
        if filename:
            logger.warning(
                "font %r is not installed; drawing with %r instead",
                family, substitute,
            )
            return filename

    fallback = fonts.font_manager.fallback_font_name()
    logger.warning(
        "font %r is not installed and none of its substitutes are either; "
        "drawing with the fallback %r",
        family, fallback,
    )
    return fallback


class FontReport(NamedTuple):
    family: str
    note: str
    #: Whether this machine has the family itself.
    installed: bool
    #: The family that will actually be drawn -- itself, or a substitute.
    drawn_as: str


def supported() -> List[FontReport]:
    """Every offered family, and what each would really be drawn with here."""
    report = []
    for choice in SUPPORTED:
        installed = _installed(choice.family) is not None
        drawn_as = choice.family
        if not installed:
            drawn_as = next(
                (s for s in choice.substitutes if _installed(s) is not None),
                "",
            )
        report.append(FontReport(choice.family, choice.note, installed, drawn_as))
    return report
