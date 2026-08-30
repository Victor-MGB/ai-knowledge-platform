import math
import os

os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["LLM_PROVIDER"] = "mock"

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.core.config import get_settings


@pytest.fixture()
def client():
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def l2_norm(vector: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))