"""Vocabulary hints for the transcription prompt, learned from clipboard.

Whisper accepts a `prompt` that biases recognition toward names it would
otherwise mangle, and it is worth a lot: xdotool, flatpak, localStorage,
AltGr and Codex all came back wrong without one and right with one.

The budget is small and hard. Padding a glossary out to 600 terms pushed
the real ones past the model's prompt limit and localStorage regressed to
"local storage"; filling it with unrelated words did the same. So this is
a ranked, capped set that gets rebuilt, never an accumulating list.

Terms are harvested from the clipboard, which holds correctly spelled
identifiers the user actually works with. Deliberately NOT harvested from
our own transcripts: whisper writing "X.Tool" would teach the glossary
"X.Tool" and make the error permanent.

Everything learned is written to glossary.json for inspection, and terms
can be pinned or blocked there by hand.
"""

from __future__ import annotations

import json
import logging
import re

from doubao_murmur.config import get_glossary_path

logger = logging.getLogger(__name__)

#: Splits text into MAXIMAL identifier runs. Matching bounded-length
#: candidates instead would pull substrings out of longer strings, and a
#: 13-char slice of an API key looks nothing like an API key: an early
#: version happily learned "RMyFBaJzEQQBj" out of a Hugging Face token.
#: Length is judged on the whole run, so an over-long one is dropped
#: entirely rather than mined for fragments.
_SPLIT = re.compile(r"[^A-Za-z0-9_.\-]+")

#: Longest run still considered a word rather than an opaque blob.
_MAX_TERM = 24

#: Anything matching these never enters the glossary. The clipboard is a
#: place credentials pass through, and a leaked one would be uploaded
#: with every single dictation, so this errs heavily toward dropping.
_SECRET_MARKERS = re.compile(
    r"""(?xi)
    ^(sk|pk|rk)[-_] |          # OpenAI-style and friends
    ^gh[pousr]_ |              # GitHub tokens
    ^github_pat_ |
    ^hf_ |                     # Hugging Face
    ^xox[baprs]- |             # Slack
    ^AKIA | ^ASIA |            # AWS access key ids
    ^AIza |                    # Google API keys
    ^ya29\. |                  # Google OAuth
    ^glpat- |                  # GitLab
    ^dop_v1_ |                 # DigitalOcean
    ^Bearer$ |
    ^eyJ                       # JWT header
    """
)

#: Words that carry no recognition value and would crowd out real terms.
_STOPWORDS = frozenset("""
about after all also and any are because been before being both but can
could did does doing down during each else few for from further had has
have having here how into its itself just like made make many may more
most much must not now only other our out over own same should since some
such than that the their them then there these they this those through
too under until very was were what when where which while who whom why
will with would you your
true false null none nil void return function class const let var import
export from default public private static async await this new type
http https www com org net html json yaml text file files data test tests
""".split())


def _is_secret(token: str) -> bool:
    """Reject anything that looks like a credential rather than a word."""
    if _SECRET_MARKERS.search(token):
        return True
    if len(token) >= 20:
        # Long tokens mixing cases and digits are keys far more often than
        # they are vocabulary worth biasing toward.
        has_digit = any(c.isdigit() for c in token)
        has_upper = any(c.isupper() for c in token)
        has_lower = any(c.islower() for c in token)
        if has_digit and has_upper and has_lower:
            return True
    if len(token) >= 24 and re.fullmatch(r"[0-9a-fA-F]+", token):
        return True  # hex digest
    return False


def _looks_useful(token: str) -> bool:
    if len(token) < 4 or len(token) > _MAX_TERM:
        return False
    if token.lower() in _STOPWORDS:
        return False
    if token.isdigit():
        return False
    if not any(c.isalpha() for c in token):
        return False
    return not _is_secret(token)


class Glossary:
    """Ranked vocabulary hints persisted to glossary.json."""

    def __init__(self, max_terms: int = 48) -> None:
        self.max_terms = max_terms
        self.pinned: list[str] = []
        self.blocked: set[str] = set()
        self.learned: dict[str, int] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        path = get_glossary_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.pinned = [str(t) for t in data.get("pinned", [])]
            self.blocked = {str(t).lower() for t in data.get("blocked", [])}
            self.learned = {
                str(k): int(v) for k, v in (data.get("learned") or {}).items()
            }
        except Exception as e:
            logger.warning("Could not read glossary.json: %s", e)

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            get_glossary_path().write_text(
                json.dumps(
                    {
                        "_comment": "Edit 'pinned' to force terms in and "
                                    "'blocked' to keep them out; 'learned' "
                                    "is rebuilt from clipboard usage.",
                        "pinned": self.pinned,
                        "blocked": sorted(self.blocked),
                        "learned": dict(
                            sorted(
                                self.learned.items(),
                                key=lambda kv: (-kv[1], kv[0]),
                            )
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._dirty = False
        except Exception as e:
            logger.warning("Could not write glossary.json: %s", e)

    def harvest(self, text: str) -> int:
        """Fold clipboard text into the counts. Returns terms accepted."""
        if not text:
            return 0
        seen = set()
        for run in _SPLIT.split(text[:20000]):
            token = run.strip("._-")
            if token in seen or not _looks_useful(token):
                continue
            if token.lower() in self.blocked:
                continue
            seen.add(token)
        for token in seen:
            self.learned[token] = self.learned.get(token, 0) + 1
        if seen:
            self._dirty = True
            # Keep the store from growing without bound; ranking only ever
            # reads the top slice anyway.
            if len(self.learned) > self.max_terms * 20:
                self.learned = dict(
                    sorted(self.learned.items(), key=lambda kv: -kv[1])[
                        : self.max_terms * 10
                    ]
                )
            self.save()
        return len(seen)

    def terms(self) -> list[str]:
        """Pinned terms first, then the most-seen learned ones."""
        out: list[str] = []
        used = set()
        for token in self.pinned:
            if token.lower() not in self.blocked and token.lower() not in used:
                out.append(token)
                used.add(token.lower())
        ranked = sorted(self.learned.items(), key=lambda kv: (-kv[1], kv[0]))
        for token, _count in ranked:
            if len(out) >= self.max_terms:
                break
            if token.lower() in used or token.lower() in self.blocked:
                continue
            out.append(token)
            used.add(token.lower())
        return out

    def build_prompt(self, base: str | None) -> str | None:
        """Append the ranked terms to the hand-written prompt.

        The base prompt goes first: it is the part the user curated, and
        whatever overflows the model's prompt limit is dropped from the
        end.
        """
        terms = self.terms()
        if not terms:
            return base
        joined = ", ".join(terms)
        if not base:
            return f"术语表：{joined}。"
        stem = base.rstrip("。. ")
        # Extend the user's own list rather than opening a second one.
        if "术语表" in base or "glossary" in base.lower():
            return f"{stem}, {joined}。"
        return f"{stem}。术语表：{joined}。"


_shared: Glossary | None = None


def shared_glossary(max_terms: int = 48) -> Glossary:
    """The one glossary instance: harvested by the manager, read by the
    backend when it builds a request."""
    global _shared
    if _shared is None:
        _shared = Glossary(max_terms=max_terms)
    return _shared
