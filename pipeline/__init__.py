"""The Global Brief — a twice-daily, two-page A4 newspaper generated in code.

Design rule (see DESIGN.md, invariant 1): **code owns everything stable, the LLM
owns editorial judgement only**. Nothing in this package asks a model for HTML,
CSS, URLs, pagination or word counts.

Stage order:

    config.yaml + sources.py  ->  fetch.py       build/articles-<date>-<ed>.json
                              ->  editorial.py   build/edition-<date>-<ed>.json
                              ->  render.py      dist/global-brief-<date>-<ed>.html
                                  render_mobile.py  dist/mobile-<date>-<ed>.html
                              ->  export.py      dist/*.pdf   (+ page/clip assertions)
                              ->  publish.py     Telegram sendDocument
                              ->  run.py         dist/manifest-<date>-<ed>.json

Submodules are intentionally not imported here: every entry point is runnable as
``python -m pipeline.<stage>`` and importing one stage must never drag in the
others' dependencies (feedparser vs playwright vs requests).
"""

__version__ = "1.0.0"

__all__ = [
    "sources",
    "fetch",
    "editorial",
    "render",
    "render_mobile",
    "export",
    "publish",
    "run",
]
