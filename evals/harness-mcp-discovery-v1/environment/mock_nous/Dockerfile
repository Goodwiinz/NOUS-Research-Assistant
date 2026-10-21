# python:3.11-slim resolved on 2026-10-07 to the index digest below.
FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce

WORKDIR /service
# Only the server and its data enter the image; generate_catalog.py is an
# authoring tool that runs against backend source on the host.
COPY server.py catalog.json connectors.json fixtures.json arxiv_query.json /service/

RUN useradd --create-home --uid 10001 benchmark \
    && chown -R benchmark:benchmark /service

USER benchmark
EXPOSE 8080
ENTRYPOINT ["python", "/service/server.py"]
