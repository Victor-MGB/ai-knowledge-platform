from conftest import l2_norm


def test_embedding_shape_and_normalization(client):
    response = client.post("/v1/embeddings", json={"text": "the water pipe is leaking"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "hash"
    assert body["dimensions"] == 384
    vector = body["data"][0]["embedding"]
    assert len(vector) == 384
    assert abs(l2_norm(vector) - 1.0) < 1e-4


def test_embedding_is_deterministic(client):
    first = client.post("/v1/embeddings", json={"text": "felines hunt rodents"}).json()
    second = client.post("/v1/embeddings", json={"text": "felines hunt rodents"}).json()
    assert first["data"][0]["embedding"] == second["data"][0]["embedding"]
    third = client.post("/v1/embeddings", json={"text": "cakes need sugar"}).json()
    assert first["data"][0]["embedding"] != third["data"][0]["embedding"]


def test_embedding_rejects_empty_text(client):
    response = client.post("/v1/embeddings", json={"text": ""})
    assert response.status_code == 422