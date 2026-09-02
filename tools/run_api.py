from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host=os.getenv("MARKETPLACE_HUB_API_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("MARKETPLACE_HUB_API_PORT", "8000"))),
        workers=max(1, int(os.getenv("MARKETPLACE_HUB_API_WORKERS", "1"))),
        reload=False,
    )
