FROM python:3.13-slim-bookworm AS build
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 scanner
COPY --from=build /install /usr/local
USER scanner
WORKDIR /repo
ENTRYPOINT ["docs-drift"]
