"""Tests for mak.planner.retrieval: deterministic lexical seeding."""

from __future__ import annotations

import pytest

from mak.planner.retrieval import LexicalRetriever, extract_terms, split_identifier
from tests.planner.fixture_repo import view_of
from tests.planner.synthetic_repo import INVOICE_FILE, REPORT_FILE, SyntheticRepo


class TestTerms:
    @pytest.mark.parametrize(
        ("name", "parts"),
        [
            ("invoice_total", ["invoice", "total"]),
            ("getUserName", ["get", "user", "name"]),
            ("HTTPServerError2", ["http", "server", "error"]),
            ("a_b", []),
        ],
    )
    def test_split_identifier(self, name: str, parts: list[str]) -> None:
        assert split_identifier(name) == parts

    def test_spans_dotted_names_and_paths_are_kept_whole_and_split(self) -> None:
        whole, subtokens = extract_terms(
            "Fix `invoice_total` in billing/invoice.py and pkg.mod.run; add getUserName"
        )
        assert {"invoice_total", "billing/invoice.py", "pkg.mod.run"} <= set(whole)
        assert {"invoice", "total", "billing", "user", "name"} <= set(subtokens)
        # Stop words and short tokens never become terms.
        assert "add" not in whole and "get" not in subtokens
        assert "py" not in subtokens

    def test_stop_words_only(self) -> None:
        assert extract_terms("add the test and get the self init") == ([], [])


class TestSeeding:
    """A lexical guess ranked deterministically and bounded by its budget."""

    @pytest.mark.parametrize(
        ("task", "first"),
        [
            ("Round `invoice_total` to cents", INVOICE_FILE),
            ("the invoice total is wrong", INVOICE_FILE),
            ("change synth/billing/report.py output", REPORT_FILE),
        ],
    )
    def test_a_task_naming_a_rare_symbol_seeds_its_file_first(
        self, synthetic_100: SyntheticRepo, task: str, first: str
    ) -> None:
        view = view_of(synthetic_100.store)
        result = LexicalRetriever().seed(task, view, 4200)
        assert result.files[0] == first

    def test_the_one_hop_neighbour_follows(self, synthetic_100: SyntheticRepo) -> None:
        view = view_of(synthetic_100.store)
        result = LexicalRetriever().seed("Round `invoice_total` to cents", view, 4200)
        assert result.files[:2] == (INVOICE_FILE, REPORT_FILE)

    def test_the_order_is_deterministic(self, synthetic_100: SyntheticRepo) -> None:
        view = view_of(synthetic_100.store)
        task = "rank the cache and merge the shard index"
        first = LexicalRetriever().seed(task, view, 4200)
        second = LexicalRetriever().seed(task, view_of(synthetic_100.store), 4200)
        assert first == second
        scores = [score for _path, score in first.scores]
        assert scores == sorted(scores, reverse=True)

    def test_a_task_of_stop_words_seeds_nothing(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        result = LexicalRetriever().seed("add the test and set it", view, 4200)
        assert result.files == ()
        assert result.terms == ()

    def test_seeds_fit_the_budget(self, synthetic_100: SyntheticRepo) -> None:
        view = view_of(synthetic_100.store)
        budget = 600
        task = "rank the cache and merge the shard"
        result = LexicalRetriever().seed(task, view, budget)
        assert result.files
        assert sum(view.file_tokens(p, budget) for p in result.files) <= budget

    def test_common_words_are_worth_less_than_rare_ones(
        self, synthetic_100: SyntheticRepo
    ) -> None:
        view = view_of(synthetic_100.store)
        result = LexicalRetriever().seed("invoice cache", view, 10_000)
        # "invoice" names two functions, "cache" hundreds: the rare word wins.
        assert result.files[0] == INVOICE_FILE
