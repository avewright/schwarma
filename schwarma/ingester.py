"""
Open Problem Ingesters — pull challenges from external sources into Schwarma.

Architecture
------------
``OpenProblemIngester``   abstract base for all source adapters.
``KaggleIngester``        pulls active public competitions from the Kaggle API.
``ArxivIngester``         pulls recent open-access papers / research problems.
``ExternalScore``         record of an automated evaluation from a scoring oracle.
``ExternalScoringOracle`` sends a solution to an external grading endpoint.

Ingesters produce standard ``Problem`` objects tagged with the appropriate
``ProblemOrigin``.  The calling code (typically a hub background scheduler
job) registers these problems via ``Exchange.post_problem()`` under a
system-bot agent.

Usage
-----
.. code-block:: python

    ingester = KaggleIngester(username="myuser", key="abc123")
    bot_agent_id = ...  # system bot registered in exchange
    async for problem in ingester.ingest():
        await exchange.post_problem(problem)

External scoring
----------------
.. code-block:: python

    oracle = ExternalScoringOracle(scoring_url="https://...")
    result = await oracle.score(solution_body, problem)
    print(result.score, result.passed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from schwarma.problem import (
    ChallengeCategory,
    Problem,
    ProblemOrigin,
    ProblemTag,
)
from schwarma.trust import Sensitivity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base ingester
# ---------------------------------------------------------------------------

class OpenProblemIngester(ABC):
    """Abstract base class for all external problem source adapters.

    Subclasses implement :meth:`ingest` which yields ``Problem`` objects.
    The *system_agent_id* is used as ``author_id`` on every ingested problem
    so the exchange has a valid owner.
    """

    def __init__(self, system_agent_id: UUID | None = None) -> None:
        self.system_agent_id = system_agent_id or uuid4()

    @abstractmethod
    async def ingest(self) -> AsyncIterator[Problem]:
        """Yield problems from the external source."""
        # make type checker happy — subclasses override this
        return
        yield  # pragma: no cover

    async def ingest_all(self) -> list[Problem]:
        """Collect all ingested problems into a list (convenience helper)."""
        problems: list[Problem] = []
        async for p in self.ingest():
            problems.append(p)
        return problems

    # ------------------------------------------------------------------
    # HTTP helpers (stdlib only, zero deps)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
        """Synchronous JSON GET via stdlib urllib (run in executor)."""
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            logger.warning("HTTP %d fetching %s: %s", exc.code, url, exc.reason)
            return None
        except Exception as exc:
            logger.error("Error fetching %s: %s", url, exc)
            return None

    @classmethod
    async def _get_json_async(cls, url: str, headers: dict[str, str] | None = None) -> Any:
        """Async wrapper around _get_json via thread executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls._get_json, url, headers or {})


# ---------------------------------------------------------------------------
# Kaggle ingester
# ---------------------------------------------------------------------------

class KaggleIngester(OpenProblemIngester):
    """Pull active public Kaggle competitions.

    Uses the public Kaggle REST API (no private data).  Authentication is
    optional — some endpoints are accessible without credentials; the full
    list requires a Kaggle API key.

    Parameters
    ----------
    username : str | None
        Kaggle username (optional; enables authenticated requests).
    key : str | None
        Kaggle API key (optional).
    max_competitions : int
        Maximum number of competitions to ingest per call.
    only_active : bool
        If True, skip competitions with a past deadline.
    """

    _API_BASE = "https://www.kaggle.com/api/v1"

    def __init__(
        self,
        system_agent_id: UUID | None = None,
        username: str | None = None,
        key: str | None = None,
        max_competitions: int = 20,
        only_active: bool = True,
    ) -> None:
        super().__init__(system_agent_id)
        self.username = username
        self.key = key
        self.max_competitions = max_competitions
        self.only_active = only_active

    def _auth_headers(self) -> dict[str, str]:
        if self.username and self.key:
            import base64
            creds = base64.b64encode(f"{self.username}:{self.key}".encode()).decode()
            return {"Authorization": f"Basic {creds}"}
        return {}

    async def ingest(self) -> AsyncIterator[Problem]:
        """Yield one Problem per active public Kaggle competition."""
        url = (
            f"{self._API_BASE}/competitions/list"
            f"?sortBy=recentlyCreated&pageSize={self.max_competitions}&page=1"
        )
        data = await self._get_json_async(url, self._auth_headers())
        if not data:
            logger.warning("KaggleIngester: no data returned from %s", url)
            return

        competitions = data if isinstance(data, list) else data.get("competitions", [])
        for comp in competitions[:self.max_competitions]:
            try:
                title = comp.get("title", "Unknown Competition")
                slug = comp.get("url", "").rstrip("/").split("/")[-1] or comp.get("ref", "")
                description = comp.get("description") or comp.get("description", title)
                reward = comp.get("reward", "")
                deadline_str = comp.get("deadline") or comp.get("enabledDate")
                deadline: datetime | None = None
                if deadline_str:
                    try:
                        deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                    except ValueError:
                        pass
                if self.only_active and deadline and deadline < datetime.now(timezone.utc):
                    continue

                comp_url = f"https://www.kaggle.com/c/{slug}" if slug else None

                problem = Problem(
                    title=f"[Kaggle] {title}",
                    description=(
                        f"{description}\n\n"
                        f"**Prize**: {reward}\n"
                        f"**Source**: {comp_url or 'https://kaggle.com'}\n"
                    ),
                    author_id=self.system_agent_id,
                    tags={ProblemTag.RESEARCH, ProblemTag.GENERAL},
                    sensitivity=Sensitivity.PUBLIC,
                    priority=1,
                )
                problem.origin = ProblemOrigin.KAGGLE
                problem.external_id = slug
                problem.external_url = comp_url
                problem.challenge_category = ChallengeCategory.MACHINE_LEARNING
                problem.challenge_deadline = deadline
                yield problem
            except Exception as exc:
                logger.warning("KaggleIngester: error processing competition: %s", exc)
                continue


# ---------------------------------------------------------------------------
# arXiv ingester
# ---------------------------------------------------------------------------

class ArxivIngester(OpenProblemIngester):
    """Pull recent papers from the arXiv Atom/RSS feed.

    Searches arXiv using the publicly available API.  Each paper is turned
    into a Problem tagged as a RESEARCH challenge.

    Parameters
    ----------
    query : str
        arXiv search query (e.g. "machine learning", "quantum computing").
    category : str | None
        arXiv category filter (e.g. "cs.LG", "math.CO").  If provided,
        combined with the search query via AND.
    max_results : int
        Maximum papers to fetch per call.
    """

    _API_BASE = "http://export.arxiv.org/api/query"

    def __init__(
        self,
        system_agent_id: UUID | None = None,
        query: str = "open problems",
        category: str | None = None,
        max_results: int = 20,
        challenge_category: ChallengeCategory = ChallengeCategory.SCIENCE,
    ) -> None:
        super().__init__(system_agent_id)
        self.query = query
        self.category = category
        self.max_results = max_results
        self.challenge_category = challenge_category

    def _build_url(self) -> str:
        q = self.query
        if self.category:
            q = f"cat:{self.category} AND {q}"
        params = urllib.parse.urlencode({
            "search_query": q,
            "max_results": self.max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        return f"{self._API_BASE}?{params}"

    @staticmethod
    def _parse_atom(xml_text: str) -> list[dict[str, Any]]:
        """Parse arXiv Atom feed into a list of entry dicts.  Zero-dep XML parse."""
        import re
        entries: list[dict[str, Any]] = []
        # Extract <entry> blocks
        entry_blocks = re.findall(r"<entry>(.*?)</entry>", xml_text, re.DOTALL)
        for block in entry_blocks:
            def _tag(name: str) -> str:
                m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.DOTALL)
                return m.group(1).strip() if m else ""
            entries.append({
                "id": _tag("id"),
                "title": _tag("title").replace("\n", " ").strip(),
                "summary": _tag("summary").replace("\n", " ").strip(),
                "published": _tag("published"),
                "link": _tag("id"),
            })
        return entries

    async def ingest(self) -> AsyncIterator[Problem]:
        """Yield one Problem per recent arXiv paper."""
        url = self._build_url()
        loop = asyncio.get_event_loop()

        def _fetch() -> str | None:
            req = urllib.request.Request(url)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.read().decode()
            except Exception as exc:
                logger.error("ArxivIngester fetch error: %s", exc)
                return None

        xml_text = await loop.run_in_executor(None, _fetch)
        if not xml_text:
            return

        entries = self._parse_atom(xml_text)
        for entry in entries[:self.max_results]:
            title = entry.get("title", "Unknown Paper")
            summary = entry.get("summary", "")
            paper_url = entry.get("link", "")
            arxiv_id = paper_url.rstrip("/").split("/")[-1] if paper_url else ""
            published_str = entry.get("published", "")
            deadline: datetime | None = None

            problem = Problem(
                title=f"[arXiv] {title}",
                description=(
                    f"{summary}\n\n"
                    f"**Source**: {paper_url}\n"
                ),
                author_id=self.system_agent_id,
                tags={ProblemTag.RESEARCH},
                sensitivity=Sensitivity.PUBLIC,
                priority=0,
            )
            problem.origin = ProblemOrigin.ARXIV
            problem.external_id = arxiv_id
            problem.external_url = paper_url
            problem.challenge_category = self.challenge_category
            yield problem


# ---------------------------------------------------------------------------
# External scoring oracle
# ---------------------------------------------------------------------------

@dataclass
class ExternalScore:
    """Result of evaluating a solution against an external scoring endpoint.

    Attributes
    ----------
    solution_id : UUID
    problem_id : UUID
    score : float
        Normalised score in [0, 1].  -1.0 means the external service
        was unavailable or the scoring is not applicable.
    passed : bool
        Whether the solution meets the acceptance threshold.
    threshold : float
        The score that defines "passing" for this problem.
    raw_response : dict
        Full response from the scoring service for debugging.
    scored_at : datetime
    error : str | None
        Set if the scoring call failed; ``passed`` will be False.
    """

    solution_id: UUID
    problem_id: UUID
    score: float = -1.0
    passed: bool = False
    threshold: float = 0.5
    raw_response: dict[str, Any] = field(default_factory=dict)
    scored_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None
    id: UUID = field(default_factory=uuid4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "solution_id": str(self.solution_id),
            "problem_id": str(self.problem_id),
            "score": self.score,
            "passed": self.passed,
            "threshold": self.threshold,
            "raw_response": self.raw_response,
            "scored_at": self.scored_at.isoformat(),
            "error": self.error,
        }


class ExternalScoringOracle:
    """Sends a solution body to an external HTTP scoring endpoint.

    The endpoint should accept POST requests with JSON body::

        {
            "solution": "<solution text>",
            "problem_id": "<uuid>",
            "external_id": "<e.g. kaggle slug>"
        }

    And return::

        {
            "score": 0.87,
            "passed": true,
            "details": {...}   # optional
        }

    Parameters
    ----------
    scoring_url : str
        Base URL for the scoring service.
    threshold : float
        Score at or above which ``passed`` is True.
    timeout : int
        Request timeout in seconds.
    """

    def __init__(
        self,
        scoring_url: str,
        threshold: float = 0.5,
        timeout: int = 30,
    ) -> None:
        self.scoring_url = scoring_url
        self.threshold = threshold
        self.timeout = timeout

    async def score(
        self,
        solution_body: str,
        problem: Problem,
        solution_id: UUID | None = None,
    ) -> ExternalScore:
        """Submit *solution_body* for external scoring and return an ExternalScore."""
        sid = solution_id or uuid4()
        payload = json.dumps({
            "solution": solution_body,
            "problem_id": str(problem.id),
            "external_id": problem.external_id or "",
            "origin": problem.origin.name,
        }).encode()

        def _post() -> dict[str, Any] | None:
            req = urllib.request.Request(
                problem.scoring_url or self.scoring_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                logger.error("ExternalScoringOracle HTTP %d: %s", exc.code, exc.reason)
                return {"error": f"HTTP {exc.code}: {exc.reason}"}
            except Exception as exc:
                logger.error("ExternalScoringOracle error: %s", exc)
                return {"error": str(exc)}

        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, _post) or {}

        error = raw.get("error")
        score_val = float(raw.get("score", -1.0)) if not error else -1.0
        passed = (score_val >= self.threshold) if not error else False

        return ExternalScore(
            solution_id=sid,
            problem_id=problem.id,
            score=score_val,
            passed=passed,
            threshold=self.threshold,
            raw_response=raw,
            error=error,
        )
