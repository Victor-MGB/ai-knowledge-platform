"""Day 17 — /v1/rag/generate: grounded answers + the "I don't know" contract.

The default provider is the deterministic extractive baseline, so these tests
are exact: the answer must be a verbatim sentence from the strongest fitting
evidence, and every refusal path returns the canonical answer with
`refused=true` over HTTP 200 (a refusal is a valid answer, not an error).
"""

DONT_KNOW = "I don't know."


def generate(client, payload):
    return client.post("/v1/rag/generate", json=payload)


def test_answers_verbatim_from_the_strongest_overlapping_sentence(client):
    response = generate(
        client,
        {
            "question": "how long is the return window on damaged goods?",
            "context": [
                {
                    "text": (
                        "Gift receipts enable exchanges for up to a year. "
                        "The return window on damaged goods is 30 days from "
                        "the delivery date. Restocking fees apply to open-box "
                        "items."
                    ),
                    "similarity": 0.8,
                    "page": 3,
                    "section": "Return Window",
                },
                {
                    "text": "International orders are final sale. Shipping "
                    "costs are non-refundable. Customs fees are the buyers "
                    "responsibility.",
                    "similarity": 0.3,
                    "page": 7,
                },
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert body["provider"] == "extract"
    assert body["model"] == "knowflow-extract-1"
    assert body["answer"] == (
        "The return window on damaged goods is 30 days from the delivery date. [1]"
    )
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["completion_tokens"] > 0
    assert len(body["evidence"]) == 1
    evidence = body["evidence"][0]
    assert evidence["index"] == 0
    assert evidence["similarity"] == 0.8
    assert evidence["section"] == "Return Window"
    assert evidence["page"] == 3
    # Day 18: the answer cites its source, and the citation maps [1] -> source
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["id"] == 1
    assert citation["section"] == "Return Window"
    assert citation["page"] == 3
    assert citation["similarity"] == 0.8


def test_refuses_when_no_context_is_supplied(client):
    response = generate(client, {"question": "what is the return policy"})
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is True
    assert body["answer"] == DONT_KNOW
    assert body["evidence"] == []


def test_refuses_when_no_sentence_overlaps_the_question(client):
    # every sentence shares zero content tokens with the question
    response = generate(
        client,
        {
            "question": "how much does international shipping cost",
            "context": [
                {
                    "text": "The alps are a folk yodeling tradition. Cheese "
                    "caves stay cool and damp. Bicycles have two wheels.",
                    "similarity": 0.9,
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is True
    assert body["answer"] == DONT_KNOW


def test_accepts_a_question_with_a_partial_sentence_overlap(client):
    response = generate(
        client,
        {
            "question": "how long is the return window",
            "context": [
                {"text": "The return window is thirty days.", "similarity": 0.5}
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert body["answer"] == "The return window is thirty days. [1]"


def test_min_score_refuses_when_the_best_evidence_is_below_the_bar(client):
    response = generate(
        client,
        {
            "question": "what is the return window",
            "context": [{"text": "The return window is thirty days.", "similarity": 0.5}],
            "min_score": 0.9,
        },
    )
    assert response.status_code == 200
    assert response.json()["refused"] is True


def test_min_score_is_respected_when_the_evidence_clears_it(client):
    response = generate(
        client,
        {
            "question": "what is the return window",
            "context": [{"text": "The return window is thirty days.", "similarity": 0.95}],
            "min_score": 0.9,
        },
    )
    assert response.status_code == 200
    assert response.json()["refused"] is False


def test_context_budget_drops_the_lowest_similarity_items(client):
    # the ONLY sentence overlapping the question lives in the long, lowest-
    # similarity item. Inside a tight budget it is dropped -> ungrounded ->
    # refuse; without a budget it survives and answers.
    payload = {
        "question": "what is the return window",
        "context": [
            {"text": "Unrelated filler sentence.", "similarity": 0.9},
            {"text": "More filler content here.", "similarity": 0.5},
            {
                "text": "The return window is thirty days. " + ("padding words " * 260),
                "similarity": 0.1,
            },
        ],
    }
    budgeted = generate(client, {**payload, "max_context_tokens": 50})
    assert budgeted.status_code == 200
    assert budgeted.json()["refused"] is True
    assert budgeted.json()["evidence"] == []

    unbudgeted = generate(client, payload)
    assert unbudgeted.status_code == 200
    body = unbudgeted.json()
    assert body["refused"] is False
    assert body["answer"] == "The return window is thirty days. [3]"
    # the answer's grounding is that low-score item (sole overlap), index 2,
    # which the answer cites as [3] (position in context, 1-based)
    assert body["evidence"][0]["similarity"] == 0.1
    assert body["evidence"][0]["index"] == 2
    assert len(body["citations"]) == 1
    assert body["citations"][0]["id"] == 3
    assert body["citations"][0]["similarity"] == 0.1


def test_budget_keeps_a_single_oversized_item_anyway(client):
    # the strongest item always survives even if it alone exceeds the budget
    response = generate(
        client,
        {
            "question": "what is the return window",
            "context": [
                {
                    "text": "The return window is thirty days. " + ("padding words " * 260),
                    "similarity": 0.9,
                },
            ],
            "max_context_tokens": 50,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert body["answer"] == "The return window is thirty days. [1]"
    assert len(body["citations"]) == 1
    assert body["citations"][0]["id"] == 1


def test_resolves_a_referential_follow_up_against_history(client):
    # Day 20 — with prior history, "what about international customers?"
    # grounds on the refund snippet (the referent) instead of refusing, and
    # cites it.
    response = generate(
        client,
        {
            "question": "What about international customers?",
            "history": [
                {"role": "user", "content": "What is the refund policy?"},
                {"role": "assistant", "content": "The refund period is 30 days. [1]"},
            ],
            "context": [
                {
                    "text": "The refund period is 30 days from the delivery date.",
                    "similarity": 0.9,
                    "document_title": "Returns Handbook",
                    "page": 4,
                },
                {
                    "text": "International orders ship within 10 business days.",
                    "similarity": 0.7,
                    "document_title": "Shipping Policy",
                    "page": 2,
                },
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert "30 days" in body["answer"]
    assert len(body["citations"]) == 1
    assert body["citations"][0]["title"] == "Returns Handbook"


def test_history_validation(client):
    # history is optional
    assert generate(client, {"question": "q", "context": [{"text": "x y z"}]}).status_code == 200
    # roles restricted to user/assistant
    system = generate(
        client,
        {
            "question": "q",
            "context": [{"text": "x y z"}],
            "history": [{"role": "system", "content": "be helpful"}],
        },
    )
    assert system.status_code == 422
    # unknown key inside a history message is rejected
    bad = generate(
        client,
        {
            "question": "q",
            "context": [{"text": "x y z"}],
            "history": [{"role": "user", "content": "hi", "timestamp": 1}],
        },
    )
    assert bad.status_code == 422
    # too many history messages is rejected
    too_many = generate(
        client,
        {
            "question": "q",
            "context": [{"text": "x y z"}],
            "history": [
                {"role": "user", "content": "message"} for _ in range(21)
            ],
        },
    )
    assert too_many.status_code == 422


def test_validation_rejects_malformed_payloads(client):
    blank_question = generate(client, {"question": "   "})
    assert blank_question.status_code == 422

    wrong_temperature = generate(
        client,
        {
            "question": "q",
            "context": [{"text": "x y z"}],
            "temperature": 3,
        },
    )
    assert wrong_temperature.status_code == 422

    unknown_top_level_key = generate(client, {"question": "q", "answers": 1})
    assert unknown_top_level_key.status_code == 422

    too_many_items = generate(
        client,
        {
            "question": "q",
            "context": [{"text": f"sentence {i} here"} for i in range(11)],
        },
    )
    assert too_many_items.status_code == 422

    context_missing_text = generate(
        client, {"question": "q", "context": [{"similarity": 0.5}]}
    )
    assert context_missing_text.status_code == 422

    context_unknown_key = generate(
        client, {"question": "q", "context": [{"text": "x y z", "score": 1}]}
    )
    assert context_unknown_key.status_code == 422

    bad_budget = generate(
        client, {"question": "q", "context": [{"text": "x y z"}], "max_context_tokens": 10}
    )
    assert bad_budget.status_code == 422

    bad_min_score = generate(
        client, {"question": "q", "context": [{"text": "x y z"}], "min_score": 1.5}
    )
    assert bad_min_score.status_code == 422