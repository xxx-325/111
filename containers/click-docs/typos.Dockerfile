# Add the missing tool to a retained, immutable execution image.
ARG EXECUTION_BASE_IMAGE=local/session-click-docs:recovery-base
FROM ${EXECUTION_BASE_IMAGE}
RUN python -m pip install --no-cache-dir --only-binary=:all: typos==1.29.7 \
    && typos --version \
    && python -m pip check
ENV PRE_COMMIT_HOME=/opt/pre-commit-cache
COPY typos-pre-commit.yaml /opt/typos-pre-commit.yaml
WORKDIR /tmp/typos-preflight
RUN git init . \
    && pre-commit install-hooks --config /opt/typos-pre-commit.yaml \
    && chown -R sandbox:sandbox /opt/pre-commit-cache
WORKDIR /
