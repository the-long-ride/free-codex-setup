from __future__ import annotations

import json

from core.codex_config import (
    default_codex_model_catalog_path,
    render_codex_config_snippet,
)


def test_render_codex_config_snippet_defaults_to_absolute_codex_catalog_path() -> None:
    snippet = render_codex_config_snippet(
        base_url="http://127.0.0.1:8082/v1",
        api_key="generated-key",
    )

    assert (
        f"model_catalog_json = {json.dumps(default_codex_model_catalog_path())}"
        in snippet
    )
