from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator


def _is_empty_html_body(body: Any) -> bool:
    """Return True only for values that Streamlit rejects as empty HTML."""
    if body is None:
        return True
    if isinstance(body, str):
        return not body.strip()
    return False


@contextmanager
def allow_legacy_components_with_modern_streamlit(
    streamlit_module: Any,
) -> Iterator[None]:
    """Protect old/custom components from ``st.html(\"\")`` failures.

    Streamlit 1.59+ raises ``StreamlitAPIException`` when ``st.html`` receives
    an empty body. Some releases of streamlit-quill2 execute an empty style
    injection during component initialisation. That call is harmless and was
    accepted by earlier Streamlit versions, so we ignore only the empty call
    while preserving every non-empty ``st.html`` invocation unchanged.

    The patch is installed *before* importing the component so it also covers
    modules that capture ``streamlit.html`` at import time. The original
    function is restored immediately afterwards.
    """
    original_html: Callable[..., Any] | None = getattr(streamlit_module, "html", None)
    if original_html is None:
        yield
        return

    def safe_html(body: Any = "", *args: Any, **kwargs: Any) -> Any:
        if _is_empty_html_body(body):
            return None
        return original_html(body, *args, **kwargs)

    streamlit_module.html = safe_html
    try:
        yield
    finally:
        streamlit_module.html = original_html
