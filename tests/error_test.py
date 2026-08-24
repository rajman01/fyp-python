"""A refused plan says why, in words a surveyor can act on.

Run from the repository root:

    python tests/error_test.py

The engine raises ValueError for the faults a user can fix -- a survey too
large for the sheet it was given, a layout whose parameters produce no plots
-- and those messages name both the problem and what to change. Only building
the plan was guarded, though, so anything raised while *drawing* it fell
through to the catch-all and came back as "An unexpected error occurred". The
API had nothing to pass on and showed its own "Failed to generate plan", so a
fault the engine had described exactly reached the user as a shrug.

What matters here is the status and the body: 4xx with the real reason for
something the user can fix, 5xx and nothing specific for a fault that is ours.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from layout_test import generate_payload  # noqa: E402
from smoke_test import cadastral_payload  # noqa: E402

client = app.test_client()


def _tiny_site():
    """A site far smaller than one plot: generation produces nothing."""
    data = generate_payload()
    data["layout_boundary"]["coordinates"] = [
        {"id": "B1", "easting": 543000, "northing": 712000},
        {"id": "B2", "easting": 543008, "northing": 712000},
        {"id": "B3", "easting": 543008, "northing": 712006},
        {"id": "B4", "easting": 543000, "northing": 712006},
    ]
    return data


def check_a_drawing_fault_is_reported():
    """A failure while drawing comes back with its reason, not a shrug."""
    errors = []
    response = client.post("/layout/plan", json=_tiny_site())
    body = response.get_json() or {}

    if response.status_code != 400:
        errors.append(f"a layout that cannot be generated answered "
                      f"{response.status_code}, not 400")
    reason = body.get("error", "")
    if "no plots" not in reason:
        errors.append(f"the reason was lost: {reason!r}")
    if body.get("stage") != "drawing the plan":
        errors.append(f"the stage was {body.get('stage')!r}, so the user is not "
                      f"told which part of their input is at fault")
    return errors


def check_a_sheet_that_cannot_hold_the_survey_is_reported():
    """The survey is too big for the paper, and the message says so.

    Raised while the plan is being built rather than drawn, so it exercises
    the other guard -- and this one has always been reported. It is here to
    keep it that way.
    """
    errors = []
    data = cadastral_payload()
    data["scale"] = 100          # a 100 m survey at 1:100 needs a metre of paper
    data["fit_scale_to_sheet"] = False

    response = client.post("/cadastral/plan", json=data)
    body = response.get_json() or {}

    if response.status_code != 400:
        errors.append(f"a survey too large for its sheet answered "
                      f"{response.status_code}, not 400")
    reason = body.get("error", "")
    if "does not fit" not in reason:
        errors.append(f"the reason was lost: {reason!r}")
    # The message is only worth passing on if it says what to do next.
    if "or smaller" not in reason and "larger sheet" not in reason:
        errors.append(f"the reason does not say what to change: {reason!r}")
    return errors


def check_a_malformed_payload_is_reported():
    """A payload the model rejects comes back as a validation failure."""
    errors = []
    data = cadastral_payload()
    data["coordinates"] = [{"id": "A", "easting": "not a number", "northing": 1}]

    response = client.post("/cadastral/plan", json=data)
    body = response.get_json() or {}

    if response.status_code != 400:
        errors.append(f"an invalid payload answered {response.status_code}, not 400")
    if body.get("error") != "Invalid plan data":
        errors.append(f"unexpected error text: {body.get('error')!r}")
    if not body.get("details"):
        errors.append("no details, so the caller cannot say which field is wrong")
    return errors


def check_a_bad_request_is_not_turned_into_a_server_fault():
    """Flask's own refusals keep their status.

    The catch-all used to answer every exception with 500, HTTP errors
    included, so a wrong method or an unparseable body was reported as the
    engine having broken.
    """
    errors = []
    for name, response in (
        ("a wrong method", client.get("/layout/plan")),
        ("an unknown route", client.get("/no/such/route")),
        ("a non-JSON body", client.post(
            "/layout/plan", data="not json", content_type="application/json")),
    ):
        if response.status_code >= 500:
            errors.append(f"{name} answered {response.status_code}")
        if not (response.get_json() or {}).get("error"):
            errors.append(f"{name} answered without a JSON reason")
    return errors


def main():
    failures = 0
    for name, fn in (
        ("a drawing fault is reported", check_a_drawing_fault_is_reported),
        ("a sheet that cannot hold the survey is reported",
         check_a_sheet_that_cannot_hold_the_survey_is_reported),
        ("a malformed payload is reported", check_a_malformed_payload_is_reported),
        ("a bad request is not a server fault",
         check_a_bad_request_is_not_turned_into_a_server_fault),
    ):
        print(f"== {name} ==")
        errors = fn()
        for error in errors:
            failures += 1
            print("  FAIL:", error)
        if not errors:
            print("  OK")

    print(f"\n{failures} failure(s)" if failures else "\nall error reporting checks pass")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
