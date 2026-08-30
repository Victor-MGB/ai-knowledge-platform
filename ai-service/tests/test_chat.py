def test_chat_returns_mock_completion(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}], "temperature": 0.0},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "hello" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] > 0


def test_chat_rejects_empty_messages(client):
    response = client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 422


def test_chat_rejects_invalid_role(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "admin", "content": "hi"}]},
    )
    assert response.status_code == 422