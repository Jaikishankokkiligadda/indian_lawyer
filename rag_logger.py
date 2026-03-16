"""
rag_logger.py
─────────────────────────────────────────────────────────────
Standalone RAG metrics logger for Nyaya Mitra.

Usage:
    from rag_logger import RAGLogger
    logger = RAGLogger()                  # uses default rag_metrics.csv
    logger = RAGLogger("my_log.csv")      # custom file

    # In your chat handler:
    with logger.timer() as t:
        docs_with_scores = vectorstore.similarity_search_with_score(query, k=5)
        answer = llm.invoke(prompt)

    logger.log(query, docs_with_scores, answer, t.elapsed_ms)

    # Summary stats:
    stats = logger.summary()

Run standalone to see a live summary of your log:
    python rag_logger.py
    python rag_logger.py --file my_log.csv
    python rag_logger.py --last 20          # show last N rows
    python rag_logger.py --clear            # wipe the log
─────────────────────────────────────────────────────────────
"""

import csv
import time
import pathlib
import datetime
import argparse
from contextlib import contextmanager
from typing import List, Tuple, Optional

# ── Try pandas for the CLI summary (optional) ──
try:
    import pandas as pd
    _PANDAS = True
except ImportError:
    _PANDAS = False

# ─────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────
DEFAULT_LOG_FILE = "rag_metrics.csv"

FIELDS = [
    "timestamp",
    "query",
    "retrieved_chunks",
    "retrieval_score",
    "latency_ms",
    "hallucination_score",
    "faithfulness",
    "answer_length",
]


# ─────────────────────────────────────────
#  TIMER CONTEXT MANAGER
# ─────────────────────────────────────────
class _Timer:
    """Simple wall-clock timer returned by RAGLogger.timer()."""
    def __init__(self):
        self.elapsed_ms: float = 0.0
        self._start: float = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000


# ─────────────────────────────────────────
#  MAIN LOGGER CLASS
# ─────────────────────────────────────────
class RAGLogger:
    """
    Logs RAG query metrics to a CSV file and exposes summary statistics.

    Parameters
    ----------
    log_file : str
        Path to the CSV log file (created automatically if missing).
    score_threshold : float
        Cosine similarity threshold below which a chunk is considered
        low-quality (used in summary reporting only, not filtering).
    """

    def __init__(self, log_file: str = DEFAULT_LOG_FILE, score_threshold: float = 0.35):
        self.log_file        = pathlib.Path(log_file)
        self.score_threshold = score_threshold
        self._init_file()

    # ── File setup ──────────────────────────────────────────────
    def _init_file(self):
        """Create the CSV with headers if it does not exist yet."""
        if not self.log_file.exists():
            with open(self.log_file, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    # ── Timer helper ────────────────────────────────────────────
    @contextmanager
    def timer(self):
        """
        Context manager that measures wall-clock time.

        Example
        -------
        with logger.timer() as t:
            answer = llm.invoke(prompt)
        print(t.elapsed_ms)
        """
        t = _Timer()
        with t:
            yield t

    # ── Core logging ────────────────────────────────────────────
    def log(
        self,
        query: str,
        docs_with_scores: List[Tuple],
        answer: str,
        latency_ms: float,
    ) -> dict:
        """
        Compute all 8 metrics and append one row to the CSV.

        Parameters
        ----------
        query            : The user's question string.
        docs_with_scores : List of (Document, float) from
                           vectorstore.similarity_search_with_score().
                           Pass an empty list when no vectorstore is loaded.
        answer           : The LLM's full response string.
        latency_ms       : Total wall-clock time in milliseconds.

        Returns
        -------
        dict with all 8 metric values (useful for in-app display).
        """
        # ── Retrieval score ──────────────────────────────────────
        scores    = [float(s) for _, s in docs_with_scores]
        avg_score = round(sum(scores) / len(scores), 4) if scores else 0.0

        # ── Faithfulness heuristic ───────────────────────────────
        # For each sentence in the answer, check whether any meaningful
        # word (>5 chars) from that sentence appears in the retrieved
        # context. Fraction of grounded sentences = faithfulness.
        context   = " ".join(d.page_content for d, _ in docs_with_scores).lower()
        sentences = [s.strip() for s in answer.split(".") if len(s.strip()) > 20]

        if sentences and context.strip():
            grounded = sum(
                1 for s in sentences
                if any(w in context for w in s.lower().split() if len(w) > 5)
            )
            faithfulness = round(grounded / len(sentences), 4)
        else:
            # No context loaded → cannot assess faithfulness
            faithfulness = 1.0

        hallucination_score = round(1.0 - faithfulness, 4)

        row = {
            "timestamp":           datetime.datetime.now().isoformat(timespec="seconds"),
            "query":               query,
            "retrieved_chunks":    len(docs_with_scores),
            "retrieval_score":     avg_score,
            "latency_ms":          round(latency_ms),
            "hallucination_score": hallucination_score,
            "faithfulness":        faithfulness,
            "answer_length":       len(answer),
        }

        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)

        return row

    # ── Summary statistics ───────────────────────────────────────
    def summary(self) -> dict:
        """
        Read the entire log and return aggregate statistics.

        Returns
        -------
        dict with keys:
            total_queries, avg_latency_ms, avg_retrieval_score,
            avg_faithfulness, hallucination_rate_pct,
            avg_answer_length, low_score_queries (count),
            high_hall_queries (count)
        """
        rows = self._read_all()
        if not rows:
            return {"total_queries": 0}

        n = len(rows)

        def avg(field):
            vals = [float(r[field]) for r in rows if r.get(field) not in (None, "")]
            return round(sum(vals) / len(vals), 4) if vals else 0.0

        return {
            "total_queries":        n,
            "avg_latency_ms":       round(avg("latency_ms")),
            "avg_retrieval_score":  avg("retrieval_score"),
            "avg_faithfulness":     avg("faithfulness"),
            "hallucination_rate_pct": round(avg("hallucination_score") * 100, 1),
            "avg_answer_length":    round(avg("answer_length")),
            "low_score_queries":    sum(
                1 for r in rows
                if float(r.get("retrieval_score", 1)) < self.score_threshold
            ),
            "high_hall_queries":    sum(
                1 for r in rows
                if float(r.get("hallucination_score", 0)) > 0.30
            ),
        }

    def recent(self, n: int = 10) -> List[dict]:
        """Return the last N logged rows as a list of dicts."""
        return self._read_all()[-n:]

    def clear(self):
        """Delete all logged rows (keeps the header)."""
        with open(self.log_file, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    # ── Internal helpers ─────────────────────────────────────────
    def _read_all(self) -> List[dict]:
        if not self.log_file.exists():
            return []
        with open(self.log_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def __repr__(self):
        s = self.summary()
        return (
            f"RAGLogger(file='{self.log_file}', "
            f"queries={s.get('total_queries', 0)}, "
            f"avg_latency={s.get('avg_latency_ms', 0)}ms)"
        )


# ─────────────────────────────────────────
#  CLI — run standalone for live summary
#  python rag_logger.py
#  python rag_logger.py --last 20
#  python rag_logger.py --clear
# ─────────────────────────────────────────
def _cli():
    parser = argparse.ArgumentParser(
        description="Nyaya Mitra – RAG metrics viewer"
    )
    parser.add_argument("--file",  default=DEFAULT_LOG_FILE, help="Path to CSV log file")
    parser.add_argument("--last",  type=int, default=10,     help="Show last N rows")
    parser.add_argument("--clear", action="store_true",      help="Wipe the log file")
    args = parser.parse_args()

    logger = RAGLogger(log_file=args.file)

    if args.clear:
        logger.clear()
        print(f"✅  Log cleared: {args.file}")
        return

    s = logger.summary()

    if s.get("total_queries", 0) == 0:
        print("📭  No queries logged yet. Ask a question in Nyaya Mitra first.")
        return

    # ── Summary banner ───────────────────────────────────────────
    W = 54
    print("\n" + "─" * W)
    print("  Nyaya Mitra · RAG Metrics Summary")
    print("─" * W)
    print(f"  {'Total queries':<28} {s['total_queries']}")
    print(f"  {'Avg latency':<28} {s['avg_latency_ms']} ms")
    print(f"  {'Avg retrieval score':<28} {s['avg_retrieval_score']}")
    print(f"  {'Avg faithfulness':<28} {s['avg_faithfulness']}")
    print(f"  {'Hallucination rate':<28} {s['hallucination_rate_pct']}%")
    print(f"  {'Avg answer length':<28} {s['avg_answer_length']} chars")
    print(f"  {'Low-score retrievals':<28} {s['low_score_queries']}  (score < {logger.score_threshold})")
    print(f"  {'High-hallucination queries':<28} {s['high_hall_queries']}  (score > 0.30)")
    print("─" * W)

    # ── Recent rows ───────────────────────────────────────────────
    rows = logger.recent(args.last)
    print(f"\n  Last {len(rows)} queries\n")

    if _PANDAS:
        import pandas as pd
        df = pd.DataFrame(rows)
        df["query"] = df["query"].str[:45] + "…"
        df["latency_ms"] = df["latency_ms"].astype(int)
        print(df[[
            "timestamp", "query", "retrieved_chunks",
            "retrieval_score", "latency_ms",
            "hallucination_score", "faithfulness"
        ]].to_string(index=False))
    else:
        # Plain-text fallback
        hdr = f"  {'Time':<10} {'Chunks':>6} {'RetScr':>7} {'Lat ms':>7} {'Hall':>6} {'Faith':>6}  Query"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in rows:
            ts    = r.get("timestamp", "")[-8:]
            q     = r.get("query", "")[:40]
            ch    = r.get("retrieved_chunks", "-")
            rs    = float(r.get("retrieval_score", 0))
            lat   = int(float(r.get("latency_ms", 0)))
            hall  = float(r.get("hallucination_score", 0))
            faith = float(r.get("faithfulness", 0))
            print(f"  {ts:<10} {ch:>6} {rs:>7.3f} {lat:>7}  {hall:>5.2f}  {faith:>5.2f}  {q}")

    print("\n  Log file: " + str(pathlib.Path(args.file).resolve()))
    print("─" * W + "\n")


if __name__ == "__main__":
    _cli()