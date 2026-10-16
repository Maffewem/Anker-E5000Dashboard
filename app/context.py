"""State shared by the web routes: the one `state` object the app is wired to
(collectors, storage, controller and so on, set up in main.lifespan) and the
build information baked into the image."""

import os

from starlette.datastructures import State

state = State()

# Set by the image build: a readable version (1.0.57), the commit and the build date.
APP_VERSION = os.environ.get("APP_VERSION", "").strip()[:32] or "dev"
APP_COMMIT = os.environ.get("APP_COMMIT", "").strip()[:40]
APP_BUILT = os.environ.get("APP_BUILT", "").strip()[:10]
