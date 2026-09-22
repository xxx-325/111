FROM local/session-tool-sandbox:isolated

# Dependencies for the continuous task range, pinned from its lock and hooks.
# Runtime PYTHONPATH selects the mounted candidate over the baseline metadata.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir \
    myst-parser==5.0.0 \
    pallets-sphinx-themes==2.5.0 \
    sphinx==9.1.0 \
    sphinx-tabs==3.5.0 \
    sphinxcontrib-log-cabinet==1.0.1 \
    pytest==9.0.2 \
    ruff==0.15.9 \
    uv==0.11.3 \
    mypy==1.20.0 \
    pyright==1.1.408 \
    pre-commit==4.5.1 \
    pre-commit-uv==4.2.1 \
    tox==4.52.0 \
    tox-uv==1.34.0 \
    flit_core==3.12.0 \
    codespell==2.4.2 \
    typos==1.29.7 \
    pre-commit-hooks==6.0.0 \
    build==1.4.0
