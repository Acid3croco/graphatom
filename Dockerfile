FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md schema.sql ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir . \
    && python -c "import graphatom.scheduler"

CMD ["graphatom", "run"]
