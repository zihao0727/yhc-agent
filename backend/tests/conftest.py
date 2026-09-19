import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app


@pytest.fixture(autouse=True)
def isolate_customer_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings(), "deepseek_api_key", "")
    monkeypatch.setattr(settings(), "agent_background", False)


@pytest.fixture
def test_db():
    config = settings()
    assert config.database_url.startswith("postgresql"), "Integration tests require PostgreSQL"
    admin = create_engine(config.database_url)
    schema = "test_firefly_" + uuid.uuid4().hex
    assert re.fullmatch(r"test_firefly_[0-9a-f]{32}", schema)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(config.database_url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def client(test_db):
    def override_db():
        with test_db() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = settings().admin_token
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def price_input():
    return {
        "category": "测试分类", "product": "测试产品", "material": "铝",
        "thickness_mm": "3", "spec": "10-20cm", "language": "en", "process": "烤漆",
        "quality": "普通", "unit": "cm", "amount": "1.234567", "price_kind": "standard",
        "status": "draft", "notes": "测试", "review_reason": "测试创建",
    }
