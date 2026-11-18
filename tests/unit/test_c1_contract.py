"""The committed API contract remains a valid OpenAPI 3.1 document."""

from pathlib import Path

from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename


def test_c1_openapi_document_validates() -> None:
    path = Path(__file__).parents[2] / "openapi" / "raad-money-v1.yaml"
    spec, base_uri = read_from_filename(str(path))
    validate(spec, base_uri=base_uri)


def test_c1_contract_keeps_v1_money_base_path() -> None:
    path = Path(__file__).parents[2] / "openapi" / "raad-money-v1.yaml"
    text = path.read_text()
    assert "https://{apiHost}/money/v1" in text
    assert "openapi: 3.1.0" in text
