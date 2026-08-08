"""Typed failures for the floor-plan pipeline.

Each maps to a distinct HTTP status at the API edge, because "we can't scale
this image" and "this input class isn't supported" need different operator
responses.
"""


class ExtractionError(RuntimeError):
    """Base: extraction ran but could not produce a usable plan."""


class MissingScale(ExtractionError):
    """No way to convert pixels to metres.

    Raised rather than defaulting. A guessed scale multiplies every area and
    length in the rehab estimate by an arbitrary constant, and the result looks
    completely plausible — the worst possible failure mode.
    """


class UnsupportedInput(ExtractionError):
    """Input class the pipeline deliberately declines to process."""


class DegenerateGeometry(ExtractionError):
    """Extraction produced too little structure to be worth persisting."""
