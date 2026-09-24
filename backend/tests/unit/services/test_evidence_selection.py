from src.services.research.evidence_selection import select_relevant_passages


def test_selector_finds_attention_benchmark_evidence_beyond_prefix() -> None:
    source = (
        "[Page 1]\n"
        + ("background unrelated to benchmarks. " * 180)
        + "\n[Page 8]\nThe model achieved 28.4 BLEU on English-to-German."
        + "\n[Page 9]\nIt achieved 41.8 BLEU on English-to-French."
        + "\n[Page 12]\nTraining took 3.5 days on 8 GPUs."
    )

    selected = select_relevant_passages(
        source,
        queries=["28.4 BLEU 41.8 BLEU 3.5 days 8 GPUs"],
        max_chars=700,
    )

    assert "28.4 BLEU" in selected
    assert "41.8 BLEU" in selected
    assert "3.5 days on 8 GPUs" in selected
    assert "[Page 8]" in selected
    assert "[Page 9]" in selected
    assert "[Page 12]" in selected
    assert len(selected) <= 700


def test_selector_does_not_invent_page_for_legacy_text() -> None:
    selected = select_relevant_passages(
        "Legacy extracted prose says the result was 28.4 BLEU.",
        queries=["28.4 BLEU"],
        max_chars=200,
    )

    assert "28.4 BLEU" in selected
    assert "[Page" not in selected


def test_selector_centers_tight_budget_on_late_match() -> None:
    source = "[Page 8]\n" + ("unrelated " * 80) + "28.4 BLEU benchmark result."

    selected = select_relevant_passages(source, queries=["28.4 BLEU"], max_chars=100)

    assert "[Page 8]" in selected
    assert "28.4 BLEU" in selected
    assert len(selected) <= 100


def test_selector_prefers_late_numeric_match_over_early_generic_match() -> None:
    source = (
        "[Page 8]\nBenchmark context "
        + ("unrelated " * 80)
        + "the model achieved 28.4 BLEU."
    )

    selected = select_relevant_passages(
        source,
        queries=["benchmark result 28.4 BLEU"],
        max_chars=100,
    )

    assert "[Page 8]" in selected
    assert "28.4 BLEU" in selected
    assert len(selected) <= 100
