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
#: It holds both the names a surveyor will look for -- Arial, Times New Roman,
#: Courier New -- and the open faces that carry the same designs, because
#: which of the two a machine has depends on the machine. ``supported()``
#: reports only the ones actually present, so each environment offers the
#: fonts it can really draw rather than a menu that quietly substitutes.
#:
#: The substitutes behind each entry are still used, but only for a plan that
#: was saved with a font this machine has since lost -- not for anything a
#: user can pick today.
SUPPORTED = (
    # -- Sans -------------------------------------------------------------
    FontChoice("Arial", ("Liberation Sans", "Arimo", "Nimbus Sans", "FreeSans",
                         "DejaVu Sans"),
               "Plain sans-serif; the usual choice"),
    FontChoice("Helvetica", ("Nimbus Sans", "Liberation Sans", "Arimo", "FreeSans"),
               "Sans-serif, slightly tighter than Arial"),
    FontChoice("Liberation Sans", ("Arial", "Arimo", "Nimbus Sans"),
               "Sans-serif, Arial widths"),
    FontChoice("Arimo", ("Liberation Sans", "Arial", "Nimbus Sans"),
               "Sans-serif, Arial widths"),
    FontChoice("Nimbus Sans", ("Helvetica", "Liberation Sans", "Arial"),
               "Sans-serif, Helvetica widths"),
    FontChoice("FreeSans", ("Liberation Sans", "DejaVu Sans", "Arial"),
               "Sans-serif, humanist"),
    FontChoice("Verdana", ("DejaVu Sans", "Liberation Sans", "Arimo"),
               "Sans-serif, wide; reads well at small sizes"),
    FontChoice("DejaVu Sans", ("Verdana", "Liberation Sans", "FreeSans"),
               "Sans-serif, wide; very broad character coverage"),
    FontChoice("Tahoma", ("Liberation Sans Narrow", "Nimbus Sans Narrow",
                          "DejaVu Sans Condensed"),
               "Sans-serif, narrow; fits more in a column"),
    FontChoice("Liberation Sans Narrow", ("Nimbus Sans Narrow", "Tahoma",
                                          "DejaVu Sans Condensed"),
               "Sans-serif, narrow"),
    FontChoice("Trebuchet MS", ("FreeSans", "DejaVu Sans", "Liberation Sans"),
               "Sans-serif, humanist"),
    FontChoice("Century Gothic", ("URW Gothic", "FreeSans"),
               "Geometric sans-serif"),
    FontChoice("URW Gothic", ("Century Gothic", "FreeSans"),
               "Geometric sans-serif"),

    # -- Serif ------------------------------------------------------------
    FontChoice("Times New Roman", ("Liberation Serif", "Tinos", "Nimbus Roman",
                                   "FreeSerif", "DejaVu Serif"),
               "Serif; traditional on cadastral sheets"),
    FontChoice("Liberation Serif", ("Times New Roman", "Tinos", "Nimbus Roman"),
               "Serif, Times widths"),
    FontChoice("Tinos", ("Liberation Serif", "Times New Roman", "Nimbus Roman"),
               "Serif, Times widths"),
    FontChoice("Nimbus Roman", ("Times New Roman", "Liberation Serif", "FreeSerif"),
               "Serif, Times widths"),
    FontChoice("FreeSerif", ("Liberation Serif", "DejaVu Serif", "Times New Roman"),
               "Serif"),
    FontChoice("Georgia", ("DejaVu Serif", "P052", "C059"),
               "Serif, wide; sturdy at small sizes"),
    FontChoice("DejaVu Serif", ("Georgia", "FreeSerif", "Liberation Serif"),
               "Serif, wide"),
    FontChoice("Palatino", ("P052", "URW Bookman"),
               "Serif, calligraphic"),
    FontChoice("P052", ("Palatino", "URW Bookman"),
               "Serif, calligraphic; Palatino widths"),
    FontChoice("Century Schoolbook", ("C059", "URW Bookman"),
               "Serif, open; a drafting standard"),
    FontChoice("C059", ("Century Schoolbook", "URW Bookman"),
               "Serif, open; Century Schoolbook widths"),
    FontChoice("Bookman Old Style", ("URW Bookman", "C059"),
               "Serif, heavy"),
    FontChoice("URW Bookman", ("Bookman Old Style", "C059"),
               "Serif, heavy; Bookman widths"),

    # -- Monospaced -------------------------------------------------------
    FontChoice("Courier New", ("Liberation Mono", "Cousine", "Nimbus Mono PS",
                               "FreeMono", "DejaVu Sans Mono"),
               "Monospaced; coordinates line up in columns"),
    FontChoice("Liberation Mono", ("Courier New", "Cousine", "Nimbus Mono PS"),
               "Monospaced, Courier widths"),
    FontChoice("Cousine", ("Liberation Mono", "Courier New", "Nimbus Mono PS"),
               "Monospaced, Courier widths"),
    FontChoice("Nimbus Mono PS", ("Courier New", "Liberation Mono", "FreeMono"),
               "Monospaced, Courier widths"),
    FontChoice("FreeMono", ("Liberation Mono", "DejaVu Sans Mono", "Courier New"),
               "Monospaced"),
    FontChoice("DejaVu Sans Mono", ("Liberation Mono", "FreeMono", "Courier New"),
               "Monospaced, wide"),
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


def supported() -> List[FontReport]:
    """The families this machine can draw as themselves.

    Only the installed ones. Offering a family that will be quietly stood in
    for makes the control a lie -- picking Verdana on the deployed image drew
    DejaVu Sans, and the sheet gave no sign of it. A menu that is shorter on
    one machine than another is the honest version of that: it is the machine
    that differs.

    Substitution still exists in :func:`resolve`, but only as a safety net for
    a plan saved with a font that has since gone -- never for a choice offered
    today.
    """
    seen = set()
    report = []
    for choice in SUPPORTED:
        filename = _installed(choice.family)
        if not filename or filename in seen:
            # Two names for one face -- Arial and Liberation Sans on a machine
            # that maps both to the same file -- would be two menu entries
            # that draw the same sheet.
            continue
        seen.add(filename)
        report.append(FontReport(choice.family, choice.note))
    return report


def default_family() -> str:
    """The family a new plan should start in.

    ``DEFAULT_FAMILY`` when this machine has it, otherwise the first family it
    does have. A default that is itself substituted would put every new plan
    in a face nobody chose.
    """
    if _installed(DEFAULT_FAMILY):
        return DEFAULT_FAMILY
    available = supported()
    return available[0].family if available else DEFAULT_FAMILY
