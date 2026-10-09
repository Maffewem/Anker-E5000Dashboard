"""Run the dashboard: `python -m app`. Listens on $PORT (default 8080)."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,
    )
